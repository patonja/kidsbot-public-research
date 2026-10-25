#!/usr/bin/env python3
"""KIDSBOT V5 public strategy screening — run ONLY on disposable research runners.

No API keys. No orders. Pinned, publicly sourced strategy Python runs only
inside unprivileged Docker containers on ephemeral GitHub-hosted runner.
Discovery Jan-Sep 2025; Oct-Dec 2025 is never accessed here.
"""
import ast
import csv
import datetime as dt
import hashlib
import io
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path.cwd() / 'research_output'
DATA = ROOT / 'data'
RESULTS = ROOT / 'results'
STAGING = ROOT / 'staging'
REPO = 'freqtrade/freqtrade-strategies'
COMMIT = 'f3340ce11f5bdf62f598522e64d1f5638eaa13f5'
STRATEGIES = ('ADXMomentum','AdxSmas','AwesomeMacd','BbandRsi',
              'Freqtrade_backtest_validation_freqtrade1','HourBasedStrategy','Supertrend')
PAIRS = ('ETHUSDC','LINKUSDC','SOLUSDC','SUIUSDC','XRPUSDC')
TIMEFRAME = '1h'
PERIOD = '20250101-20251001'
START = dt.datetime(2025,1,1,tzinfo=dt.timezone.utc)
END = dt.datetime(2025,10,1,tzinfo=dt.timezone.utc)
START_MS = int(START.timestamp()*1000)
END_MS = int(END.timestamp()*1000)
INTERVAL_MS = 3_600_000
EXPECTED = (END_MS - START_MS) // INTERVAL_MS
FEE = 0.001
EXTRA_SIDE = 0.001   # 5 bps slippage + 5 bps half spread / SIDE
IMAGE = 'freqtradeorg/freqtrade:stable'
USER_AGENT = 'KIDSBOT-V5-public-research/1.0'
LIMIT_ZIP_REPO = 32*1024*1024
LIMIT_MONTH_ZIP = 12*1024*1024


def need(ok, reason):
    if not ok: raise RuntimeError(reason)


def atomic_json(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + '.tmp')
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    tmp.replace(p)


def get_bytes(url, limit):
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={'User-Agent':USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as r:
                announced = r.headers.get('Content-Length')
                need(not announced or int(announced)<=limit,'HTTP_TOO_LARGE')
                b = r.read(limit+1)
            need(len(b)<=limit,'HTTP_TOO_LARGE')
            return b
        except (urllib.error.HTTPError,urllib.error.URLError,TimeoutError) as e:
            last=e; time.sleep(1+attempt)
    raise RuntimeError('DOWNLOAD_FAILED: '+str(last)[:110])


def fetch_sources():
    url = f'https://codeload.github.com/{REPO}/zip/{COMMIT}'
    raw = get_bytes(url, LIMIT_ZIP_REPO)
    sources = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        for entry in z.infolist():
            if not entry.filename.endswith('.py') or entry.file_size>200_000: continue
            path = entry.filename.partition('/')[2]
            if not path.startswith('user_data/strategies/') or any(x in '/'+path.lower() for x in
                   ('/lookahead','/futures/','/archived/','/tests/')): continue
            try:
                src = z.read(entry).decode('utf-8-sig')
                tree = ast.parse(src)
            except (SyntaxError,UnicodeError,ValueError,RecursionError): continue
            names = {n.name for n in tree.body if isinstance(n,ast.ClassDef)}
            for name in STRATEGIES:
                if name in names:
                    sources.setdefault(name,[]).append((path,src))
    output = {}
    for name in STRATEGIES:
        matched = sources.get(name,[])
        if len(matched)!=1:
            output[name] = {'status':'MISSING_OR_AMBIGUOUS_PINNED_CLASS','matching_files':len(matched)}
            continue
        rel, src = matched[0]
        p = STAGING/name/'strategies'/(name+'.py')
        p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(src,encoding='utf-8')
        digest = hashlib.sha256(src.encode('utf-8')).hexdigest()
        output[name]={'status':'SOURCE_VERIFIED_AT_PINNED_COMMIT','path':rel,
                      'source_sha256':digest,'source':str(p)}
        print('PINNED_SOURCE:',name,rel,'SHA256:',digest,flush=True)
    return output


def convert_month(pair, yyyy_mm):
    url = f'https://data.binance.vision/data/spot/monthly/klines/{pair}/{TIMEFRAME}/{pair}-{TIMEFRAME}-{yyyy_mm}.zip'
    raw = get_bytes(url, LIMIT_MONTH_ZIP)
    # Published public Binance SHA-256 checksum (archive integrity, not provenance of trading edge).
    checksum_text = get_bytes(url+'.CHECKSUM', 2048).decode('ascii','replace').strip()
    expected_hash = checksum_text.split()[0].lower()
    need(re.fullmatch('[a-f0-9]{64}',expected_hash) is not None,'INVALID_BINANCE_CHECKSUM')
    need(hashlib.sha256(raw).hexdigest()==expected_hash,'BINANCE_CHECKSUM_MISMATCH')
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names=[n for n in z.namelist() if n.endswith('.csv') and '/' not in n]
        need(len(names)==1,'UNEXPECTED_BINANCE_ZIP')
        need(z.getinfo(names[0]).file_size < 8*1024*1024,'CSV_TOO_BIG')
        contents=z.read(names[0]).decode('utf-8')
    rows=[]
    for rawrow in csv.reader(io.StringIO(contents)):
        if not rawrow: continue
        need(len(rawrow)>=12,'BAD_BINANCE_CSV_COLUMNS')
        ts=int(rawrow[0]); close_ts=int(rawrow[6])
        if ts>=10**15: ts//=1000; close_ts//=1000  # public SPOT 2025 uses microseconds
        o,h,l,c,v=(float(rawrow[j]) for j in (1,2,3,4,5))
        need(all(map(math.isfinite,(o,h,l,c,v))) and min(o,h,l,c)>0 and v>=0
             and l<=min(o,h,c) and h>=max(o,l,c),'BAD_OHLCV')
        need(close_ts in (ts+INTERVAL_MS-1, ts+INTERVAL_MS-2), 'BAD_CLOSE_TIMESTAMP')
        rows.append([ts,o,h,l,c,v])
    return rows


def fetch_candles():
    DATA.mkdir(parents=True,exist_ok=True)
    for pair in PAIRS:
        p=DATA/f'{pair[:-4]}_USDC-{TIMEFRAME}.json'
        if p.exists():
            rows=json.loads(p.read_text())
            need(len(rows)==EXPECTED and all(r[0]==START_MS+i*INTERVAL_MS for i,r in enumerate(rows)),
                 'INVALID_CACHE:'+pair)
            print('VALID_PUBLIC_CACHE:',pair,len(rows),flush=True)
            continue
        months=[f'2025-{month:02d}' for month in range(1,10)]
        rows=[]
        for month in months:
            part=convert_month(pair,month)
            rows.extend(part)
            print('PUBLIC_BINANCE_ARCHIVE:',pair,month,len(part),flush=True)
        need(len(rows)==EXPECTED,'INCOMPLETE_PUBLIC_HISTORY:'+pair+' '+str(len(rows)))
        need(all(row[0]==START_MS+i*INTERVAL_MS for i,row in enumerate(rows)),
             'CANDLE_GAPS_OR_ORDER:'+pair)
        atomic_json(p,rows)
        print('DATA_VERIFIED:',pair,len(rows),'bars',flush=True)


def research_config(p):
    cfg={
       'dry_run':True,'trading_mode':'spot',
       'stake_currency':'USDC','stake_amount':10,'dry_run_wallet':16.82,
       'max_open_trades':1,'tradable_balance_ratio':1.0,
       'exchange':{'name':'binance','key':'','secret':'',
                   'pair_whitelist':[x[:-4]+'/USDC' for x in PAIRS], 'pair_blacklist':[]},
       'pairlists':[{'method':'StaticPairList'}], 'timeframe':TIMEFRAME,
       'dataformat_ohlcv':'json','fiat_display_currency':'USD',
       'bot_name':'PUBLIC-HISTORICAL-RESEARCH-NO-TRADING',
    }
    atomic_json(p,cfg)


def load_trades(directory, name):
    files=sorted(directory.glob('backtest_results/*.zip'),key=lambda f:f.stat().st_mtime,reverse=True)
    for zfile in files:
        with zipfile.ZipFile(zfile) as z:
            for f in z.namelist():
                if not f.endswith('.json') or 'config' in f.lower() or 'strategy' in f.lower():continue
                info=z.getinfo(f)
                if info.file_size>15*1024*1024:continue
                try: doc=json.loads(z.read(f))
                except (ValueError,UnicodeError): continue
                if isinstance(doc,dict) and isinstance(doc.get('strategy'),dict):
                    obj=doc['strategy'].get(name)
                    if isinstance(obj,dict) and isinstance(obj.get('trades'),list):
                        return obj['trades']
    raise RuntimeError('MISSING_EXPORTED_TRADE_DETAILS')


def stress_score(trades):
    values=[]; day_return={}; worst=0.0
    for t in trades:
        r=t.get('profit_ratio')
        need(type(r) in (float,int) and math.isfinite(r) and -1 < r < 1000,'BAD_TRADE_RETURN')
        # Freqtrade P&L includes specified fees; overlay additional execution stress.
        net=(1+r)*(1-EXTRA_SIDE)/(1+EXTRA_SIDE)-1
        d=str(t.get('open_date') or t.get('open_date_utc') or '')[:10]
        need(re.fullmatch(r'20\d\d-\d\d-\d\d',d) is not None,'BAD_TRADE_DATE')
        values.append(net)
        day_return[d]=day_return.get(d,0.0)+net
        worst=min(worst,net)
    n=len(values); days=len(day_return)
    day_bps = 10000 * sum(day_return.values())/days if days else None
    mean_bps= 10000 * sum(values)/n if n else None
    # No statistical claims: screening only. Do NOT open Oct-Dec holdout.
    status=('INSUFFICIENT_SAMPLE' if n<50 or days<20 else
            'REJECT_NEGATIVE_STRESS_MEAN' if mean_bps<=0 else
            'POSITIVE_SCREEN_ONLY_NEEDS_INDEPENDENT_VALIDATION')
    return {'decision':status,'trades':n,'active_utc_days':days,
            'stress_mean_trade_bps':round(mean_bps,3) if mean_bps is not None else None,
            'stress_active_day_bps':round(day_bps,3) if day_bps is not None else None,
            'worst_trade_pct':round(worst*100,4) if n else None,
            'live_permission':False,'holdout_opened':False,
            'small_capital_executable':'NOT_VERIFIED',
            'freqtrade_order_equivalence':'NOT_VERIFIED'}


def run_one(name, source):
    p=STAGING/name
    for d in (p,p/'strategies',p/'backtest_results'):
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d,0o777) # ephemeral runner, data public only; container user 1000
    cfg=p/'config.json'
    research_config(cfg)
    cmd=['docker','run','--rm','--init',
         '--network=bridge','--cap-drop=ALL','--security-opt=no-new-privileges',
         '--pids-limit=128','--memory=1400m','--cpus=2',
         '--read-only','--user=1000:1000',
         '--tmpfs=/tmp:rw,nosuid,nodev,size=256m',
         '--tmpfs=/home/ftuser/.cache:rw,nosuid,nodev,size=128m,uid=1000,gid=1000',
         '--mount',f'type=bind,src={p.resolve()},dst=/freqtrade/user_data',
         '--mount',f'type=bind,src={DATA.resolve()},dst=/freqtrade/user_data/data/binance,readonly',
         IMAGE,'backtesting',
         '--config','/freqtrade/user_data/config.json',
         '--strategy-path','/freqtrade/user_data/strategies',
         '--strategy',name,'--timeframe',TIMEFRAME,
         '--data-format-ohlcv','json','--timerange',PERIOD,
         '--fee',str(FEE),'--dry-run-wallet','16.82','--stake-amount','10',
         '--max-open-trades','1','--cache','none','--export','trades']
    print('FREQTRADE_START:',name,flush=True)
    try:
        run=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                           encoding='utf-8',errors='replace',timeout=600,check=False)
        # Scrub accidental generic strings even though no credentials are available.
        log=run.stdout[-18000:]
        (p/'backtest.log').write_text(log,encoding='utf-8')
        if run.returncode:
            print('ENGINE_ERROR:',name,'code=',run.returncode,'tail=',log[-500:],flush=True)
            return {'status':'ENGINE_ERROR','exit_code':run.returncode,'log_tail':log[-900:],
                    'live_permission':False}
        trades=load_trades(p,name)
        return {'status':'HISTORICAL_PROXY_SCREEN','score':stress_score(trades),'source_sha256':source['source_sha256']}
    except subprocess.TimeoutExpired:
        return {'status':'ENGINE_TIMEOUT','live_permission':False}
    except Exception as e:
        return {'status':'ENGINE_OR_PARSE_ERROR','reason':type(e).__name__+': '+str(e)[:150],
                'live_permission':False}


def selftest():
    r=stress_score([{'profit_ratio':0,'open_date':'2025-01-01 00:00:00'}])
    assert r['decision']=='INSUFFICIENT_SAMPLE'
    assert -20.1<r['stress_mean_trade_bps']<-19.9
    assert EXPECTED == 6552
    assert len(STRATEGIES)==7
    print('SELFTEST PASS / NO NETWORK / NO ORDERS')


def main():
    if len(sys.argv)==2 and sys.argv[1]=='--selftest':return selftest()
    need(len(sys.argv)==1,'UNKNOWN_FLAGS')
    need(os.environ.get('GITHUB_ACTIONS')=='true',
         'ONLY_DISPOSABLE_GITHUB_ACTIONS_RUNNER_ALLOWED')
    need(os.environ.get('KIDSBOT_RESEARCH_NO_SECRETS')=='YES',
         'NO_SECRETS_GUARD_MISSING')
    need(os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted' and
         os.environ.get('RUNNER_OS') == 'Linux' and os.geteuid() != 0,
         'ONLY_UNPRIVILEGED_GITHUB_HOSTED_LINUX_RUNNER_ALLOWED')
    try:
        live_file = Path('/root/kidsbot_ena_oco_reconciler_v1.py').exists()
    except PermissionError:
        live_file = False
    need(not live_file, 'LIVE_MACHINE_NOT_ALLOWED')
    RESULTS.mkdir(parents=True,exist_ok=True)
    report={'type':'KIDSBOT_V5_PUBLIC_HISTORICAL_DISCOVERY_SCREEN_ONLY',
            'source_repository':REPO,'commit':COMMIT,'period':PERIOD,
            'fee_per_side':FEE,'additional_cost_per_side':EXTRA_SIDE,
            'starting_balance_usdc':16.82,'stake_usdc':10,
            'real_binance_account_access':False,'live_authorization':False,
            'validation':'NOT_OPENED','results':{}}
    print('=== V5 EPHEMERAL GITHUB RUNNER / REAL SOURCE RESEARCH ===',flush=True)
    try:
        need(subprocess.run(['docker','info'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0,
             'DOCKER_UNAVAILABLE')
        subprocess.run(['docker','pull',IMAGE],check=True,timeout=600)
        info=subprocess.run(['docker','image','inspect',IMAGE,'--format','{{.Id}}'],
                            text=True,capture_output=True,check=True)
        report['docker_image_id']=info.stdout.strip()
        print('FREQTRADE_IMAGE:',report['docker_image_id'],flush=True)
        sources=fetch_sources()
        report['sources']={k:{a:b for a,b in v.items() if a!='source'} for k,v in sources.items()}
        if any(s['status']=='SOURCE_VERIFIED_AT_PINNED_COMMIT' for s in sources.values()):
            fetch_candles()
        for name in STRATEGIES:
            source=sources[name]
            if source['status']!='SOURCE_VERIFIED_AT_PINNED_COMMIT':
                report['results'][name]=source
            else:
                report['results'][name]=run_one(name,source)
            print('RESULT:',name,json.dumps(report['results'][name],sort_keys=True),flush=True)
            atomic_json(RESULTS/'summary.json',report)
        report['status']='SCREENING_COMPLETED_WITH_PER_STRATEGY_STATUS'
    except Exception as e:
        report['status']='FAIL_CLOSED'
        report['error']=type(e).__name__+': '+str(e)[:200]
        print('FAIL_CLOSED:',report['error'],file=sys.stderr,flush=True)
    atomic_json(RESULTS/'summary.json',report)
    print('=== V5 FINAL ===',flush=True)
    print('ENGINE_ATTEMPTS:',sum(v.get('status','').startswith('HISTORICAL') or v.get('status','').startswith('ENGINE') for v in report['results'].values()),flush=True)
    print('POSITIVE_SCREENS:',[k for k,v in report['results'].items() if v.get('score',{}).get('decision')=='POSITIVE_SCREEN_ONLY_NEEDS_INDEPENDENT_VALIDATION'],flush=True)
    print('STATUS:',report['status'],flush=True)
    print('REPORT: research_output/results/summary.json',flush=True)
    print('NEVER LIVE AUTHORIZED / NO AUTH API / NO ORDERS',flush=True)
    need(report['status']!='FAIL_CLOSED',report.get('error','RUN_FAILED'))


if __name__=='__main__':
    try: main()
    except Exception as e:
        print('FAIL_CLOSED:',type(e).__name__,str(e)[:250],file=sys.stderr)
        sys.exit(2)
