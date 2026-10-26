#!/usr/bin/env python3
"""Locked V8 confirmation of two V7 candidates, Q4 2025, NO LIVE ACCESS."""
import ast
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import run_v7 as v7
lib=v7.lib
NAMES=('AverageStrategy','ReinforcedAverageStrategy')
v7.NAMES=NAMES
v7.WORKER=Path(__file__).resolve().parent/'offline_worker_v8.py'
v7.ROOT=Path.cwd()/'v8_output'
v7.REPORT=v7.ROOT/'summary.json'
lib.STRATEGIES=NAMES
lib.TIMEFRAME='4h'
lib.INTERVAL_MS=14_400_000
lib.END=dt.datetime(2026,1,1,tzinfo=dt.timezone.utc)
lib.END_MS=int(lib.END.timestamp()*1000)
lib.EXPECTED=2190
lib.PERIOD='20250101-20260101'


def need(ok,message):
    if not ok: raise RuntimeError(message)


def fetch_candles_full_year():
    lib.DATA.mkdir(parents=True,exist_ok=True)
    for pair in lib.PAIRS:
        p=lib.DATA/f'{pair[:-4]}_USDC-4h.json'
        rows=[]
        # Archive never downloaded via Binance REST API.
        for month in range(1,13):
            monthly=lib.convert_month(pair,f'2025-{month:02d}')
            rows.extend(monthly)
            print('VERIFIED_PUBLIC_MONTH:',pair,month,len(monthly),flush=True)
        need(len(rows)==lib.EXPECTED and
             all(row[0]==lib.START_MS+i*lib.INTERVAL_MS
                 for i,row in enumerate(rows)),'BAD_BAR_SEQUENCE_'+pair)
        lib.atomic_json(p,rows)
        print('FULL_YEAR_PUBLIC_DATA:',pair,len(rows),flush=True)


def execute(name):
    try:
        r=subprocess.run(v7.container(name),capture_output=True,text=True,
                         errors='replace',timeout=900,check=False)
    except subprocess.TimeoutExpired:
        return {'status':'WORKER_TIMEOUT','live_permission':False}
    lines=re.findall(r'^KIDSBOT_V8_RESULT=(.+)$',r.stdout,re.M)
    if r.returncode!=0 or len(lines)!=1:
        return {'status':'WORKER_ERROR','code':r.returncode,
                'reason':(r.stdout+'\n'+r.stderr)[-800:],
                'live_permission':False}
    data=json.loads(lines[0])
    need(data.get('strategy')==name and
         data.get('status')=='LOCKED_Q4_2025_PROXY' and
         data.get('discovery_reproduced') is True and
         data.get('live_permission') is False,'BAD_VALIDATION_RESULT')
    return data


def selftest():
    need(lib.EXPECTED==2190 and len(NAMES)==2,'WRONG_LOCKED_SCOPE')
    need((Path(__file__).parent/'offline_worker_v8.py').is_file(),'MISSING_WORKER')
    ast.parse(v7.WORKER.read_text())
    cmd=v7.container('AverageStrategy')
    need('--network=none' in cmd and '--read-only' in cmd and
         '--cap-drop=ALL' in cmd,'INVALID_DOCKER_ISOLATION')
    need(not any('BINANCE_API_KEY' in s for s in cmd),'SECRET_EXPOSURE')
    print('V8 LOCKED HOLDOUT SELFTEST PASS / NO NETWORK / NO ORDERS')


def main():
    if sys.argv[1:]==['--selftest']:
        return selftest()
    need(not sys.argv[1:],'BAD_ARGS')
    need(os.environ.get('GITHUB_ACTIONS')=='true' and
         os.environ.get('RUNNER_ENVIRONMENT')=='github-hosted' and
         os.environ.get('RUNNER_OS')=='Linux' and os.geteuid()!=0 and
         os.environ.get('KIDSBOT_RESEARCH_NO_SECRETS')=='YES',
         'NOT_GITHUB_UNPRIVILEGED_RUNNER')
    need(not any(os.environ.get(k) for k in ('BINANCE_API_KEY','BINANCE_SECRET',
         'FT_API_KEY','EXCHANGE_API_KEY','EXCHANGE_SECRET')),
         'EXCHANGE_SECRETS_FORBIDDEN')
    report={'type':'KIDSBOT_V8_LOCKED_Q4_2025_VALIDATION_PROXY',
            'candidates':list(NAMES),
            'selected_from':'V7 2025 JAN-SEP: 29 + 46 positive signals',
            'period':'2025-10-01_TO_2026-01-01_EXCLUSIVE',
            'history_for_indicator_warmup':'2025-01-01_TO_2025-10-01',
            'source_commit':lib.COMMIT,'freqtrade_equivalent':False,
            'no_live_orders':True,'reused_q4_to_select_candidates':False,
            'results':{},'status':'STARTING'}
    v7.persist(report)
    try:
        need(subprocess.run(['docker','info'],capture_output=True).returncode==0,
             'DOCKER_NOT_AVAILABLE')
        subprocess.run(['docker','pull',v7.IMAGE],check=True,timeout=480,
                       stdout=subprocess.DEVNULL)
        report['docker_image_id']=subprocess.run(
            ['docker','image','inspect',v7.IMAGE,'--format','{{.Id}}'],
            capture_output=True,text=True,check=True).stdout.strip()
        (lib.STAGING/'AverageStrategy'/'strategies').mkdir(parents=True,exist_ok=True)
        lib.DATA.mkdir(parents=True,exist_ok=True)
        smoke=subprocess.run(v7.container('AverageStrategy')[:-2]+['--name','SELFTEST'],
                             capture_output=True,text=True,check=False,timeout=90)
        need(smoke.returncode==0 and 'V7 WORKER SELFTEST PASS' in smoke.stdout,
             'WORKER_SMOKE_FAIL: '+(smoke.stdout+smoke.stderr)[-200:])
        src=lib.fetch_sources()
        report['sources']={k:{x:y for x,y in val.items() if x!='source'}
                           for k,val in src.items()}
        need(len(src)==len(NAMES) and all(
             src.get(k,{}).get('status')=='SOURCE_VERIFIED_AT_PINNED_COMMIT'
             for k in NAMES),'PINNED_SOURCES_MISSING')
        fetch_candles_full_year()
        for name in NAMES:
            report['results'][name]=execute(name)
            print('V8_HOLDOUT:',name,report['results'][name].get('status'),
                  json.dumps(report['results'][name].get('validation',{}),
                             sort_keys=True),flush=True)
            v7.persist(report)
        report['status']=('TWO_Q4_PROXIES_COMPLETE' if
            len(report['results'])==len(NAMES) and
            all(x.get('status')=='LOCKED_Q4_2025_PROXY'
                for x in report['results'].values()) else
            'INCOMPLETE_Q4_RESEARCH')
    except Exception as e:
        report['status']='FAIL_CLOSED'
        report['error']=type(e).__name__+': '+str(e)[:200]
    v7.persist(report)
    print('=== V8 LOCKED Q4 FINAL ===',flush=True)
    print('STATUS:',report['status'],flush=True)
    print('REPORT: v8_output/summary.json',flush=True)
    print('NO REAL BINANCE TRADES OR CREDENTIALS',flush=True)
    need(report['status']=='TWO_Q4_PROXIES_COMPLETE',report['status'])

if __name__=='__main__':
    try:
        main()
    except Exception as e:
        print('V8_ERROR:',type(e).__name__,str(e)[:250],file=sys.stderr,flush=True)
        sys.exit(2)
