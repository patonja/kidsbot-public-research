#!/usr/bin/env python3
"""KIDSBOT: extend EXISTING V5/V6/V7 research, never trading or LIVE promotion.

Deterministic 48h batches of NOT-YET-TESTED source classes from locked
V5 + two additional V2 already-cataloged public GitHub commit snapshots. V2 AST filters are reused unchanged. Raw source runs only
in existing-style no-network, read-only, unprivileged Docker with no secrets.
Reports are source-signal PROXIES (not Freqtrade-equivalent or causal proof).
"""
from __future__ import annotations
import datetime as dt
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import run_batch as v5
from kidsbot_universal_discovery_v2 import strategy_objects

BASE = Path.cwd() / 'unified_output'
REPORT = BASE / 'summary.json'
START = dt.datetime(2026, 10, 26, 19, 44, tzinfo=dt.timezone.utc)
END = START + dt.timedelta(hours=48)
SLOT_SEC = 6*3600
MAX_PER_SLOT = 5
MAX_SOURCE_BYTES = 200_000
TESTED = frozenset({
    'ADXMomentum','AdxSmas','AwesomeMacd','BbandRsi',
    'Freqtrade_backtest_validation_freqtrade1','HourBasedStrategy','Supertrend',
    'AverageStrategy','ReinforcedAverageStrategy','mabStra','Heracles',
    'MultiMa',  # 2026-10-26 Unified slot-0: 171 trades, -81.5063bps avg NET proxy
})
REJECT_FLAGS = frozenset({
    'COMPLEX_RUNTIME_CALLBACKS','POSITION_ADJUSTMENT_CAPITAL_REQUIREMENTS',
    'SHORT_OR_DERIVATIVE_CAPABILITY','UNKNOWN_OR_UNSUPPORTED_TIMEFRAME',
    'POSSIBLE_INHERITED_OR_DYNAMIC_RULES','ADDITIONAL_PAIR_OR_TIMEFRAME_DATA',
})
IMAGE = 'freqtradeorg/freqtrade:stable'
WORKERS = {
    '1h': Path(__file__).resolve().parent / 'unified_worker_1h.py',
    '4h': Path(__file__).resolve().parent / 'unified_worker_4h.py',
}
NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,63}\Z')
RESULT_PREFIX = {'1h':'KIDSBOT_OFFLINE_RESULT=', '4h':'KIDSBOT_V7_RESULT='}
RESULT_STATUS = {'1h':'SOURCE_SIGNAL_PROXY', '4h':'SOURCE_4H_PROXY'}


def need(ok: bool, message: str) -> None:
    if not ok:
        raise RuntimeError(message)


def persist(report: dict) -> None:
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    tmp = REPORT.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(report, indent=2, sort_keys=True)+'\n')
    tmp.replace(REPORT)


def slot_at(when: dt.datetime) -> int | None:
    if not START <= when < END:
        return None
    return int((when - START).total_seconds()//SLOT_SEC)


def eligible(row: dict) -> bool:
    name = row.get('strategy','')
    tf = row.get('timeframe')
    flags = set(row.get('review_flags') or [])
    stop = row.get('stoploss')
    return (isinstance(name,str) and NAME.fullmatch(name) is not None
        and name not in TESTED
        and tf in WORKERS
        and row.get('entry_rule_confirmed') is True
        and row.get('exit_rule_confirmed') is True
        and row.get('can_short') is False
        and not flags.intersection(REJECT_FLAGS)
        and isinstance(stop,(float,int)) and -1 <= stop < 0
        and (tf != '1h' or row.get('trailing_stop') is not True)
        and isinstance(row.get('behavior_fingerprint'),str)
        and isinstance(row.get('source_file_sha256'),str))


def verified_source_manifest() -> list[dict]:
    """Frozen exact public SHA snapshots generated at deploy, never moving HEAD.

    Do not import or execute from these repositories on the host. The locked V5
    pinned base is always first. Fail closed if the set or pin is unexpected.
    """
    manifest_path = Path(__file__).resolve().parent / 'unified_source_snapshots.json'
    doc = json.loads(manifest_path.read_text(encoding='utf-8'))
    need(doc.get('schema') == 'KIDSBOT_PINNED_PUBLIC_V2_EXTRA_SOURCES_V1',
         'BAD_PINNED_SOURCE_SCHEMA')
    rows = doc.get('sources')
    expected_repos = (
        'freqtrade/freqtrade-strategies',
        'werkkrew/freqtrade-strategies',
        'iterativv/NostalgiaForInfinity',
    )
    need(isinstance(rows, list) and len(rows)==len(expected_repos),
         'UNEXPECTED_SOURCE_COUNT')
    for row, expected in zip(rows, expected_repos):
        need(isinstance(row, dict) and row.get('repo') == expected and
             isinstance(row.get('commit'), str) and
             re.fullmatch('[a-f0-9]{40}', row['commit']) is not None,
             'UNPINNED_OR_UNEXPECTED_SOURCE')
    need(rows[0]['commit'] == v5.COMMIT and v5.REPO == expected_repos[0] and
         v5.COMMIT == 'f3340ce11f5bdf62f598522e64d1f5638eaa13f5',
         'LOCKED_V5_SOURCE_CHANGED_STOP')
    return rows


def discover_pinned() -> tuple[list[dict], list[dict]]:
    """V2 parser across 3 commit-pinned repositories; never import raw code.

    Only parse strategy source files in memory, bounded for zip members and
    decompressed bytes. No source zip extraction, write or execution on host.
    Reject duplicates rather than silently selecting arbitrary versions.
    """
    found = []
    provenance = []
    for item in verified_source_manifest():
        repo, commit = item['repo'], item['commit']
        raw = v5.get_bytes(f'https://codeload.github.com/{repo}/zip/{commit}',
                           v5.LIMIT_ZIP_REPO)
        parsed, read_files = 0, 0
        cumulative = 0
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            need(len(z.infolist()) <= 8000, 'TOO_MANY_ARCHIVE_MEMBERS')
            for member in z.infolist():
                # Preserve the original V5 restrictions; allow V2's known
                # multi-repo strategy layouts without executing code.
                rel=member.filename.partition('/')[2]
                valid = (rel.startswith('user_data/strategies/') or
                         rel.startswith('strategies/') or
                         'NostalgiaForInfinity' in rel)
                if (not valid or not rel.endswith('.py') or
                    member.file_size > MAX_SOURCE_BYTES or
                    member.file_size < 100 or
                    any(x in '/'+rel.lower() for x in
                        ('/lookahead','/futures/','/archived/','/tests/'))):
                    continue
                need(read_files < 1000, 'MAX_SOURCE_FILES_EXCEEDED')
                cumulative += member.file_size
                need(cumulative <= 24 * 1024 * 1024,
                     'MAX_DECOMPRESSED_STRATEGY_BYTES')
                read_files += 1
                try:
                    src=z.read(member).decode('utf-8-sig')
                except (UnicodeError,ValueError,RuntimeError,zipfile.BadZipFile):
                    continue
                rows, error = strategy_objects(src, repo, commit, rel)
                if error:
                    continue
                parsed += len(rows)
                for row in rows:
                    if eligible(row):
                        found.append({**row, '_source':src})
        provenance.append({'repository':repo, 'commit':commit,
                           'archive_sha256':hashlib.sha256(raw).hexdigest(),
                           'python_files_read':read_files,
                           'classes_parsed':parsed})
    counts={}
    fps={}
    for row in found:
        name=row['strategy']; fp=row['behavior_fingerprint']
        counts[name]=counts.get(name,0)+1
        fps[fp]=fps.get(fp,0)+1
    unique=[r for r in found if counts[r['strategy']]==1 and
            fps[r['behavior_fingerprint']]==1]
    unique.sort(key=lambda r:(-r['research_priority_score_not_pnl'],
                              len(r['review_flags']),r['behavior_fingerprint'],
                              r['strategy'], r['source_repository']))
    return unique, provenance


def configure_timeframe(tf: str) -> None:
    need(tf in WORKERS, 'INVALID_TIMEFRAME')
    v5.TIMEFRAME=tf
    v5.INTERVAL_MS={'1h':3600000,'4h':14400000}[tf]
    v5.EXPECTED={'1h':6552,'4h':1638}[tf]
    v5.DATA=BASE/'data'


def container_cmd(row: dict) -> list[str]:
    name=row['strategy']; tf=row['timeframe']
    need(NAME.fullmatch(name) is not None and tf in WORKERS,'UNSAFE_WORKER_REQUEST')
    src=BASE/'staging'/name/'strategies'
    worker=WORKERS[tf]
    return ['docker','run','--rm','--init','--network=none',
            '--user=1000:1000','--cap-drop=ALL','--security-opt=no-new-privileges',
            '--read-only','--pids-limit=100','--memory=1400m','--cpus=2',
            '--tmpfs=/tmp:rw,nosuid,nodev,size=128m,uid=1000,gid=1000',
            '--tmpfs=/home/ftuser/.cache:rw,nosuid,nodev,size=128m,uid=1000,gid=1000',
            '--mount',f'type=bind,src={(BASE/"data").resolve()},dst=/research/data,readonly',
            '--mount',f'type=bind,src={src.resolve()},dst=/research/strategies,readonly',
            '--mount',f'type=bind,src={worker.resolve()},dst=/research/worker.py,readonly',
            '--entrypoint=python',IMAGE,'-u','/research/worker.py','--name',name]


def execute(row: dict) -> dict:
    name=row['strategy']; tf=row['timeframe']
    source=row['_source']
    need(hashlib.sha256(source.encode()).hexdigest()==row['source_file_sha256'],
         'SOURCE_FINGERPRINT_MISMATCH')
    source_dest=BASE/'staging'/name/'strategies'/(name+'.py')
    source_dest.parent.mkdir(parents=True, exist_ok=True)
    source_dest.write_text(source,encoding='utf-8')
    try:
        p=subprocess.run(container_cmd(row), capture_output=True, text=True,
                         errors='replace',check=False,timeout=600)
    except subprocess.TimeoutExpired:
        return {'status':'WORKER_TIMEOUT','live_permission':False}
    # Never include untrusted worker stdout in published artifacts.
    prefix=RESULT_PREFIX[tf]
    lines=[s[len(prefix):] for s in p.stdout.splitlines() if s.startswith(prefix)]
    if p.returncode or len(lines)!=1:
        return {'status':'WORKER_ERROR','exit_code':p.returncode,'live_permission':False}
    try:
        r=json.loads(lines[0]);need(isinstance(r,dict),'BAD_WORKER_JSON')
        need(r.get('strategy')==name and r.get('status')==RESULT_STATUS[tf] and
             r.get('live_permission') is False,'INVALID_WORKER_ASSERTIONS')
        need(r.get('score',{}).get('live_permission') is False,
             'SCORE_LIVE_PERMISSION_UNSAFE')
        r['live_permission']=False
        return r
    except (ValueError,TypeError,RuntimeError):
        return {'status':'INVALID_WORKER_REPORT','live_permission':False}


def selftest() -> None:
    row={'strategy':'UntestedExample','timeframe':'1h','stoploss':-.05,
         'entry_rule_confirmed':True,'exit_rule_confirmed':True,
         'can_short':False,'review_flags':[], 'trailing_stop':False,
         'behavior_fingerprint':'abcdef', 'source_file_sha256':'f'*64}
    need(eligible(row),'ELIGIBLE_FIXTURE_REJECTED')
    need(not eligible({**row,'strategy':'AverageStrategy'}),'V8_REJECTED_CANDIDATE_SELECTED')
    need(not eligible({**row,'strategy':'MultiMa'}),'OCT26_NEGATIVE_RESELECTED')
    need(not eligible({**row,'strategy':'../evil'}),'PATH_TRAVERSAL_ACCEPTED')
    need(not eligible({**row,'review_flags':['COMPLEX_RUNTIME_CALLBACKS']}),'CALLBACK_ACCEPTED')
    need(not eligible({**row,'can_short':True}),'SHORT_ACCEPTED')
    need(not eligible({**row,'timeframe':'5m'}),'UNSUPPORTED_TIMEFRAME_ACCEPTED')
    need(slot_at(START)==0 and slot_at(END) is None,'BOUNDARY_WRONG')
    need(slot_at(START+dt.timedelta(hours=6))==1,'SLOT_WRONG')
    cmd=container_cmd(row)
    need('--network=none' in cmd and '--read-only' in cmd and '--cap-drop=ALL' in cmd,
         'CONTAINER_GUARD_MISSING')
    need(all(p.is_file() for p in WORKERS.values()),'WORKER_FILE_MISSING')
    need((Path(__file__).parent/'run_batch.py').is_file(), 'V5_INGEST_MISSING')
    verified_source_manifest()
    print('SELFTEST_PASS_V2_AST_V5_DATA_V6_V7_ISOLATED_PROXY_NO_TRADES')


def main() -> None:
    if sys.argv[1:]==['--selftest']:
        return selftest()
    need(not sys.argv[1:],'UNKNOWN_ARGUMENTS')
    need(os.environ.get('GITHUB_ACTIONS')=='true' and
         os.environ.get('RUNNER_ENVIRONMENT')=='github-hosted' and
         os.environ.get('RUNNER_OS')=='Linux' and os.geteuid()!=0 and
         os.environ.get('KIDSBOT_RESEARCH_NO_SECRETS')=='YES',
         'UNTRUSTED_EXECUTION_ENVIRONMENT_STOP')
    need(not any(os.environ.get(k) for k in
         ('BINANCE_API_KEY','BINANCE_SECRET','FT_API_KEY','EXCHANGE_API_KEY',
          'EXCHANGE_SECRET','GITHUB_TOKEN','GH_TOKEN')),
         'NO_CREDENTIALS_IN_RESEARCH_JOB')
    when=dt.datetime.now(dt.timezone.utc)
    slot=slot_at(when)
    report={'schema':'KIDSBOT_EXISTING_RESEARCH_UNIFIED_SPRINT_V1',
            'at_utc':when.isoformat(),'slot':slot,
            'source_repository':v5.REPO,'source_commit':v5.COMMIT,
            'discovery_period':'2025-01-01_TO_2025-10-01_EXCLUSIVE',
            'historical_Q4_2025_holdout':'UNTOUCHED',
            'prospective_SPOT60_REGIME24':'UNTOUCHED',
            'method':'EXISTING_V6_V7_CUSTOM_SIGNAL_PROXY_NOT_FREQTRADE_EQUIVALENT',
            'execution':'ISOLATED_NO_NETWORK_NO_SECRETS',
            'fees_per_side':0.001,'additional_costs_per_side':0.001,
            'approved_for_live':False,'net_edge_proven':False,
            'selected':[],'results':{},'status':'STARTING'}
    persist(report)
    if slot is None:
        report['status']='OUTSIDE_LOCKED_48H_WINDOW_NO_EXECUTION'
        persist(report)
        print(report['status'],flush=True)
        return
    try:
        need(subprocess.run(['docker','info'],stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,timeout=20).returncode==0,
             'DOCKER_UNAVAILABLE')
        for worker in WORKERS.values():
            compile(worker.read_text(),str(worker),'exec')  # compile only; never import raw strategies
        all_eligible, provenance=discover_pinned()
        report['source_snapshots']=provenance
        report['eligible_untested_total']=len(all_eligible)
        selected=all_eligible[slot*MAX_PER_SLOT:(slot+1)*MAX_PER_SLOT]
        report['selected']=[{k:r[k] for k in
            ('strategy','timeframe','behavior_fingerprint','source_repository','source_commit_sha','source_file','source_file_sha256')}
            for r in selected]
        persist(report)
        if not selected:
            report['status']='NO_UNTESTED_ELIGIBLE_IN_THIS_SLOT'
            persist(report)
            print('NO_UNTESTED_ELIGIBLE_IN_SLOT',slot,flush=True)
            return
        subprocess.run(['docker','pull',IMAGE],check=True,timeout=480,
                       stdout=subprocess.DEVNULL)
        dockerid=subprocess.run(['docker','image','inspect',IMAGE,'--format','{{.Id}}'],
                                check=True,timeout=20,capture_output=True,text=True)
        report['docker_image_id']=dockerid.stdout.strip()
        for tf in sorted(set(r['timeframe'] for r in selected)):
            configure_timeframe(tf)
            v5.fetch_candles()  # Published verified public ZIPs, no REST API
        for row in selected:
            # Filename and source are a pinned digest; no untrusted code is run outside container.
            outcome=execute(row)
            report['results'][row['strategy']]=outcome
            persist(report)
            print('PROXY:',row['strategy'],row['timeframe'],outcome.get('status'),
                  outcome.get('score',{}).get('decision','NO_GRADE'),flush=True)
        count=sum(v.get('status') in RESULT_STATUS.values() for v in report['results'].values())
        report['completed_proxy_count']=count
        report['status']=('SOME_ISOLATED_SOURCE_PROXIES_COMPLETED' if count else
                          'NO_WORKING_ISOLATED_PROXY_FAIL_CLOSED')
    except Exception as e:
        report['status']='FAIL_CLOSED'
        report['error']=type(e).__name__+':'+str(e)[:180]
    persist(report)
    print('KIDSBOT_UNIFIED_RESEARCH',report['status'],'proxies=',
          report.get('completed_proxy_count',0),'orders=0 live=false',flush=True)
    need(report['status'] not in ('FAIL_CLOSED','NO_WORKING_ISOLATED_PROXY_FAIL_CLOSED'),
         report['status'])


if __name__=='__main__':
    try: main()
    except Exception as e:
        print('UNIFIED_RESEARCH_STOP:',type(e).__name__,str(e)[:180],file=sys.stderr,flush=True)
        sys.exit(2)
