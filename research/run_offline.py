#!/usr/bin/env python3
"""V6 offline research orchestrator for public GitHub-hosted runner only.

NO Binance REST API: source from pinned GitHub commit, candles from the
published data.binance.vision archives with published SHA256 checksums.
Raw strategy source executes only in an unprivileged NO-NETWORK container.
The worker is a custom source-signal PROXY, not a Freqtrade backtest.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# V5 is reused as a verified PUBLIC DATA + PINNED SOURCE ingestion library.
# It is imported only; its main() is never called.
import run_batch as source_lib

ROOT = Path.cwd() / 'offline_output'
RESULTS = ROOT / 'results'
IMAGE = 'freqtradeorg/freqtrade:stable'
WORKER = Path(__file__).resolve().parent / 'offline_worker.py'


def need(ok, reason):
    if not ok:raise RuntimeError(reason)


def write_report(report):
    RESULTS.mkdir(parents=True,exist_ok=True)
    p=RESULTS/'summary.json'
    tmp=p.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(report,sort_keys=True,indent=2)+'\n')
    tmp.replace(p)


def container_cmd(name):
    src = source_lib.STAGING/name/'strategies'
    # No socket mount; network disabled; data/strategies read-only, no secrets.
    return ['docker','run','--rm','--init','--network=none',
            '--user=1000:1000','--cap-drop=ALL',
            '--security-opt=no-new-privileges',
            '--read-only','--pids-limit=100','--memory=1100m','--cpus=2',
            '--tmpfs=/tmp:rw,nosuid,nodev,size=128m,uid=1000,gid=1000',
            '--tmpfs=/home/ftuser/.cache:rw,nosuid,nodev,size=128m,uid=1000,gid=1000',
            '--mount',f'type=bind,src={source_lib.DATA.resolve()},dst=/research/data,readonly',
            '--mount',f'type=bind,src={src.resolve()},dst=/research/strategies,readonly',
            '--mount',f'type=bind,src={WORKER.resolve()},dst=/research/offline_worker.py,readonly',
            '--entrypoint=python',IMAGE,
            '-u','/research/offline_worker.py','--name',name]


def process(name):
    cmd=container_cmd(name)
    try:
        r=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                         text=True,errors='replace',timeout=420,check=False)
    except subprocess.TimeoutExpired:
        return {'status':'WORKER_TIMEOUT','live_permission':False}
    # Avoid masking errors or improperly parsing result.
    matched=re.findall(r'^KIDSBOT_OFFLINE_RESULT=(.+)$',r.stdout,re.M)
    if r.returncode!=0 or len(matched)!=1:
        return {'status':'WORKER_ERROR','exit_code':r.returncode,
                'log_tail':r.stdout[-1000:],'live_permission':False}
    parsed=json.loads(matched[0])
    need(parsed.get('strategy')==name and parsed.get('status')=='SOURCE_SIGNAL_PROXY',
         'INVALID_WORKER_JSON')
    parsed['live_permission']=False
    return parsed


def selftest():
    need(len(source_lib.STRATEGIES)==7 and source_lib.EXPECTED==6552,'LIBRARY_MISMATCH')
    need('network=none' in ' '.join(container_cmd('BbandRsi')),'NETWORK_ISOLATION_MISSING')
    need('--read-only' in container_cmd('BbandRsi'),'FS_ISOLATION_MISSING')
    import ast
    ast.parse(WORKER.read_text())
    print('V6 ORCHESTRATOR SELFTEST PASS / NO NETWORK / NO ORDERS')


def main():
    if sys.argv[1:]==['--selftest']:return selftest()
    need(not sys.argv[1:],'UNKNOWN_ARGUMENTS')
    need(os.environ.get('GITHUB_ACTIONS')=='true' and
         os.environ.get('RUNNER_ENVIRONMENT')=='github-hosted' and
         os.environ.get('RUNNER_OS')=='Linux' and os.geteuid()!=0 and
         os.environ.get('KIDSBOT_RESEARCH_NO_SECRETS')=='YES',
         'NOT_UNPRIVILEGED_GITHUB_HOSTED_RUNNER')
    need(not os.environ.get('BINANCE_API_KEY') and not os.environ.get('BINANCE_SECRET'),
         'FORBIDDEN_EXCHANGE_SECRETS')
    need(WORKER.is_file() and (Path(__file__).parent/'run_batch.py').is_file(),'MISSING_COMPONENT')
    report={'type':'KIDSBOT_V6_OFFLINE_SOURCE_SIGNAL_PROXY',
            'period':'2025-01-01_to_2025-10-01_exclusive',
            'source_commit':source_lib.COMMIT,
            'source_repository':source_lib.REPO,
            'strategies_requested':len(source_lib.STRATEGIES),
            'fee_per_side':.001,'additional_cost_per_side':.001,
            'method':'CUSTOM_NEXT_OPEN_STOP_FIRST_ROI_PROXY_NOT_FREQTRADE_BACKTEST',
            'network_inside_strategy_containers':'DISABLED',
            'binance_rest_api_access':'NONE',
            'live_permission':False,
            'holdout':'NOT_OPENED', 'results':{},'status':'STARTING'}
    write_report(report)
    try:
        need(subprocess.run(['docker','info'],stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL).returncode==0,'DOCKER_UNAVAILABLE')
        subprocess.run(['docker','pull',IMAGE],check=True,timeout=480,
                       stdout=subprocess.DEVNULL)
        meta=subprocess.run(['docker','image','inspect',IMAGE,'--format','{{.Id}}'],
                            capture_output=True,text=True,check=True)
        report['docker_image_id']=meta.stdout.strip()
        # Make an EMPTY public mount for the smoke test (sources not yet downloaded).
        (source_lib.STAGING/'BbandRsi'/'strategies').mkdir(parents=True,exist_ok=True)
        source_lib.DATA.mkdir(parents=True,exist_ok=True)
        # First run actual isolated worker selftest before downloading source/data.
        smoke=subprocess.run(container_cmd('BbandRsi')[:-2]+['--name','SELFTEST'],
                             capture_output=True,text=True,timeout=90,check=False)
        need(smoke.returncode==0 and 'WORKER SELFTEST PASS' in smoke.stdout,
             'CONTAINER_SELFTEST_FAILED: '+(smoke.stdout+smoke.stderr)[-300:])
        sources=source_lib.fetch_sources()
        report['sources']={k:{key:value for key,value in v.items() if key!='source'}
                           for k,v in sources.items()}
        need(all(v['status']=='SOURCE_VERIFIED_AT_PINNED_COMMIT' for v in sources.values()),
             'SOME_STRATEGIES_NOT_VERIFIED')
        source_lib.fetch_candles()   # Public ZIP + .CHECKSUM, not Binance REST API
        for name in source_lib.STRATEGIES:
            report['results'][name]=process(name)
            s=report['results'][name]
            print('V6_RESULT:',name, s.get('status'),
                  json.dumps(s.get('score',{}),sort_keys=True),
                  s.get('log_tail','')[-170:],flush=True)
            write_report(report)
        if all(r['status']=='SOURCE_SIGNAL_PROXY' for r in report['results'].values()):
            report['status']='ALL_SOURCE_SIGNAL_PROXIES_COMPLETED'
        else:
            report['status']='PARTIAL_SOURCE_SIGNAL_PROXIES'
    except Exception as e:
        report['status']='FAIL_CLOSED'
        report['error']=type(e).__name__+': '+str(e)[:200]
        print('V6_FAIL_CLOSED:',report['error'],flush=True)
    write_report(report)
    print('=== KIDSBOT V6 FINAL ===',flush=True)
    print('STATUS:',report['status'],flush=True)
    print('WORKERS_COMPLETED:',sum(v.get('status')=='SOURCE_SIGNAL_PROXY'
                                  for v in report['results'].values()),flush=True)
    print('POSITIVE_PROXIES:',[k for k,v in report['results'].items()
                               if v.get('score',{}).get('decision')==
                               'POSITIVE_PROXY_REQUIRES_CAUSAL_AND_FORWARD_VALIDATION'],flush=True)
    print('REPORT: offline_output/results/summary.json',flush=True)
    print('NO BINANCE API / NO REAL FREQTRADE ENGINE / NO LIVE ORDERS',flush=True)
    need(report['status']=='ALL_SOURCE_SIGNAL_PROXIES_COMPLETED',report['status'])


if __name__=='__main__':
    try:main()
    except Exception as e:
        print('V6_ERROR:',type(e).__name__,str(e)[:250],file=sys.stderr,flush=True)
        sys.exit(2)
