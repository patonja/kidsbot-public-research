#!/usr/bin/env python3
"""KIDSBOT V7: first NEW 4h batch; public data + pinned source; no live access.

Does not reuse/open the historical Oct-Dec 2025 validation or SPOT60/REGIME24.
Original strategy code executes only in NO-NETWORK unprivileged read-only Docker.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import run_batch as lib

ROOT=Path.cwd()/'v7_output'
REPORT=ROOT/'summary.json'
WORKER=(Path(__file__).resolve().parent/'offline_worker_v7.py')
NAMES=('AverageStrategy','ReinforcedAverageStrategy','mabStra','Heracles')
IMAGE='freqtradeorg/freqtrade:stable'

# Reuse proven public archive and source ingestion from V5, but only for 4h.
# Both routines receive these constants at call time, no V5 main() executed.
lib.STRATEGIES=NAMES
lib.TIMEFRAME='4h'
lib.INTERVAL_MS=14_400_000
lib.EXPECTED=1638


def need(ok,reason):
    if not ok:
        raise RuntimeError(reason)


def persist(report):
    REPORT.parent.mkdir(parents=True,exist_ok=True)
    temporary=REPORT.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(report,sort_keys=True,indent=2)+'\n')
    temporary.replace(REPORT)


def container(name):
    src=lib.STAGING/name/'strategies'
    return ['docker','run','--rm','--init','--network=none',
            '--user=1000:1000','--cap-drop=ALL','--security-opt=no-new-privileges',
            '--read-only','--pids-limit=100','--memory=1400m','--cpus=2',
            '--tmpfs=/tmp:rw,nosuid,nodev,size=128m,uid=1000,gid=1000',
            '--tmpfs=/home/ftuser/.cache:rw,nosuid,nodev,size=128m,uid=1000,gid=1000',
            '--mount',f'type=bind,src={lib.DATA.resolve()},dst=/research/data,readonly',
            '--mount',f'type=bind,src={src.resolve()},dst=/research/strategies,readonly',
            '--mount',f'type=bind,src={WORKER.resolve()},dst=/research/offline_worker_v7.py,readonly',
            '--entrypoint=python',IMAGE,'-u','/research/offline_worker_v7.py','--name',name]


def execute(name):
    try:
        p=subprocess.run(container(name),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                         text=True,errors='replace',check=False,timeout=700)
    except subprocess.TimeoutExpired:
        return {'status':'WORKER_TIMEOUT','live_permission':False}
    matches=re.findall(r'^KIDSBOT_V7_RESULT=(.+)$',p.stdout,re.M)
    if p.returncode or len(matches)!=1:
        return {'status':'WORKER_ERROR','code':p.returncode,
                'reason':p.stdout[-850:],'live_permission':False}
    try:
        result=json.loads(matches[0])
        need(result.get('strategy')==name and result.get('status')=='SOURCE_4H_PROXY'
             and result.get('live_permission') is False,'BAD_WORKER_RESULT')
        return result
    except (ValueError,KeyError,RuntimeError) as e:
        return {'status':'INVALID_REPORT','reason':str(e)[:200],
                'live_permission':False}


def selftest():
    import ast
    need(lib.TIMEFRAME=='4h' and lib.EXPECTED==1638 and len(NAMES)==4,
         'WRONG_SCOPE')
    ast.parse(WORKER.read_text())
    need('--network=none' in container('AverageStrategy'),'NETWORK_NOT_DISABLED')
    need('--read-only' in container('AverageStrategy'),'FS_NOT_ISOLATED')
    # FAIL-CLOSED on fabricated positive or unexecuted strategy.
    fake={'results':{'AverageStrategy':{'status':'SOURCE_4H_PROXY'}}}
    need(not complete(fake),'INCOMPLETE_FALSE_PASS')
    need(not complete({'results':{n:{'status':'WORKER_ERROR'} for n in NAMES}}),
         'ERROR_FALSE_PASS')
    print('V7 ORCHESTRATOR SELFTEST PASS / NO NETWORK / NO ORDERS')


def complete(report):
    result=report.get('results',{})
    return len(result)==len(NAMES) and all(
        n in result and result[n].get('status')=='SOURCE_4H_PROXY' for n in NAMES)


def main():
    if sys.argv[1:]==['--selftest']:
        return selftest()
    need(not sys.argv[1:],'BAD_ARGS')
    need(os.environ.get('GITHUB_ACTIONS')=='true' and
         os.environ.get('RUNNER_ENVIRONMENT')=='github-hosted' and
         os.environ.get('RUNNER_OS')=='Linux' and os.geteuid()!=0 and
         os.environ.get('KIDSBOT_RESEARCH_NO_SECRETS')=='YES',
         'ONLY_UNPRIVILEGED_GITHUB_HOSTED_LINUX_ALLOWED')
    need(not any(os.environ.get(k) for k in ('BINANCE_API_KEY','BINANCE_SECRET',
         'FT_API_KEY','EXCHANGE_API_KEY','EXCHANGE_SECRET')),
         'NO_EXCHANGE_CREDENTIALS_ALLOWED')
    need(Path(__file__).parent.joinpath('run_batch.py').is_file() and WORKER.is_file(),
         'MISSING_FILES')
    report={'type':'KIDSBOT_V7_NEW_4H_SOURCE_SIGNAL_DISCOVERY_PROXY',
            'strategies':list(NAMES),'period':'2025-01-01_TO_2025-10-01_EXCLUSIVE',
            'source_commit':lib.COMMIT,'source_repo':lib.REPO,
            'bar_timeframe':'4h','fee_per_side':.001,'other_cost_per_side':.001,
            'comparison':'THEORETICAL_FIXED_STAKE_AND_REALISTIC_16_82_WALLET',
            'trailing':'STOP_FROM_PREVIOUS_CLOSED_BAR_ONLY_NOT_FREQTRADE',
            'freqtrade_equivalent':False,'causal_prefix_spotcheck':'NOT_FULL_CAUSAL_PROOF',
            'holdout':'NOT_OPENED','live_authorization':False,
            'results':{},'status':'STARTING'}
    persist(report)
    try:
        need(subprocess.run(['docker','info'],stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL).returncode==0,'DOCKER_UNAVAILABLE')
        subprocess.run(['docker','pull',IMAGE],check=True,timeout=480,
                       stdout=subprocess.DEVNULL)
        digest=subprocess.run(['docker','image','inspect',IMAGE,'--format','{{.Id}}'],
                              capture_output=True,text=True,check=True)
        report['docker_image_id']=digest.stdout.strip()
        (lib.STAGING/'AverageStrategy'/'strategies').mkdir(parents=True,exist_ok=True)
        lib.DATA.mkdir(parents=True,exist_ok=True)
        smoke=subprocess.run(container('AverageStrategy')[:-2]+['--name','SELFTEST'],
                             capture_output=True,text=True,timeout=90,check=False)
        need(smoke.returncode==0 and 'V7 WORKER SELFTEST PASS' in smoke.stdout,
             'ISOLATED_SELFTEST_FAIL:'+(smoke.stdout+smoke.stderr)[-350:])
        sources=lib.fetch_sources()
        report['sources']={k:{x:y for x,y in v.items() if x!='source'} for k,v in sources.items()}
        for name in NAMES:
            need(sources.get(name,{}).get('status')=='SOURCE_VERIFIED_AT_PINNED_COMMIT',
                 'PINNED_SOURCE_MISSING:'+name)
        lib.fetch_candles()  # 4h public ZIP archives WITH CHECKSUM, NO REST API
        for name in NAMES:
            report['results'][name]=execute(name)
            print('V7_RESULT:',name,report['results'][name].get('status'),
                  json.dumps(report['results'][name].get('score',{}),sort_keys=True),
                  report['results'][name].get('reason','')[-180:],flush=True)
            persist(report)
        report['status']='COMPLETED_4H_SOURCE_PROXIES' if complete(report) else 'PARTIAL_4H_PROXIES'
    except Exception as e:
        report['status']='FAIL_CLOSED'
        report['error']=type(e).__name__+': '+str(e)[:230]
        print('V7_FAIL_CLOSED:',report['error'],flush=True)
    persist(report)
    print('=== KIDSBOT V7 FINAL ===',flush=True)
    print('STATUS:',report['status'],flush=True)
    print('PROXIES:',sum(x.get('status')=='SOURCE_4H_PROXY' for x in report['results'].values()),
          'OF',len(NAMES),flush=True)
    print('REPORT: v7_output/summary.json',flush=True)
    print('NO AUTH / NO EXCHANGE API / NO ORDERS / NO HOLDOUT',flush=True)
    need(report['status']=='COMPLETED_4H_SOURCE_PROXIES',report['status'])


if __name__=='__main__':
    try:
        main()
    except Exception as e:
        print('V7_ERROR:',type(e).__name__,str(e)[:230],file=sys.stderr,flush=True)
        sys.exit(2)
