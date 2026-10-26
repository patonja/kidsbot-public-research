#!/usr/bin/env python3
"""KIDSBOT Universal Discovery V2 — public-source strategy discovery, never execution.

Purpose: replace irrelevant GitHub repo-name results with actual, inspectable
Freqtrade strategy classes and point-in-time source fingerprints. No backtesting,
Binance credentials, trading, or reads of KIDSBOT frozen prospective data.

GPL/public sources remain the copyright of their authors. The report stores
metadata and concise condition excerpts, not the full source code.
"""
from __future__ import annotations

import argparse
import ast
import collections
import hashlib
import io
import json
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

REPOSITORIES = (
    "freqtrade/freqtrade-strategies",
    "werkkrew/freqtrade-strategies",
    "iterativv/NostalgiaForInfinity",
)
WORK = Path('/root/kidsbot-universal-research-v2')
USER_AGENT = 'KIDSBOT-Public-Strategy-Discovery-V2/1.0 (research-only)'
MAX_REPO_BYTES = 35 * 1024 * 1024
MAX_FILE_BYTES = 200_000
MAX_SOURCE_FILES = 1000
MAX_AST_CLASSES = 1000
CANDIDATE_METHODS = {
    'populate_buy_trend', 'populate_sell_trend',
    'populate_entry_trend', 'populate_exit_trend',
}
ENTRY_METHODS = ('populate_buy_trend', 'populate_entry_trend')
EXIT_METHODS = ('populate_sell_trend', 'populate_exit_trend')
ENTRY_COLUMNS = {'buy', 'enter_long'}
EXIT_COLUMNS = {'sell', 'exit_long'}
COMMON_TF = {'1m', '3m', '5m', '15m', '30m', '1h', '2h', '4h', '1d'}
MAX_RETRIES = 2


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def request_bytes(url: str, cap: int = MAX_REPO_BYTES) -> bytes:
    req = urllib.request.Request(url, headers={
        'User-Agent': USER_AGENT,
        'Accept': 'application/vnd.github+json',
    })
    with urllib.request.urlopen(req, timeout=35) as response:
        declared = response.headers.get('Content-Length')
        if declared is not None and int(declared) > cap:
            raise ValueError('DOWNLOAD_EXCEEDS_SIZE_LIMIT')
        chunks = bytearray()
        while True:
            item = response.read(min(65536, cap + 1 - len(chunks)))
            if not item:
                break
            chunks.extend(item)
            if len(chunks) > cap:
                raise ValueError('DOWNLOAD_EXCEEDS_SIZE_LIMIT')
        return bytes(chunks)


def fetch(url: str, cap: int = MAX_REPO_BYTES) -> bytes:
    for retry in range(MAX_RETRIES):
        try:
            return request_bytes(url, cap)
        except (TimeoutError, urllib.error.URLError, ConnectionError) as exc:
            if retry == MAX_RETRIES - 1:
                raise RuntimeError(f'PUBLIC_DOWNLOAD_FAILED: {str(exc)[:120]}') from exc
            time.sleep(1 + retry)


def repo_snapshot(repo: str):
    """Get default branch and pinned commit SHA, then fetch that commit as a ZIP.

    Raw strategy files are NEVER imported or executed; only parsed using ast.
    """
    api = json.loads(fetch('https://api.github.com/repos/' + repo, 200_000))
    branch = api['default_branch']
    head = json.loads(fetch(
        f'https://api.github.com/repos/{repo}/commits/{branch}', 200_000
    ))
    commit = head['sha']
    if not re.fullmatch(r'[a-f0-9]{40}', commit):
        raise ValueError('INVALID_COMMIT_SHA')
    archive = fetch('https://codeload.github.com/' + repo + '/zip/' + commit)
    return branch, commit, archive


def member_source_path(name: str) -> str | None:
    parts = name.split('/', 1)
    if len(parts) != 2:
        return None
    subpath = parts[1]
    if not subpath.endswith('.py'):
        return None
    if ('/tests/' in '/' + subpath or '__pycache__' in subpath or
        subpath.endswith('__init__.py') or subpath.startswith('.github/')):
        return None
    if not ('/strategies/' in '/' + subpath or
            subpath.startswith('strategies/') or
            'NostalgiaForInfinity' in subpath):
        return None
    return subpath


def literal_attrs(klass: ast.ClassDef):
    out = {}
    for item in klass.body:
        if isinstance(item, ast.Assign):
            keys = [x.id for x in item.targets if isinstance(x, ast.Name)]
            if len(keys) != 1:
                continue
            key = keys[0]
            if key in {
                'timeframe', 'ticker_interval', 'stoploss', 'minimal_roi',
                'trailing_stop', 'can_short', 'position_adjustment_enable',
                'use_exit_signal', 'startup_candle_count', 'max_entry_position_adjustment',
            }:
                try:
                    out[key] = ast.literal_eval(item.value)
                except (ValueError, TypeError, SyntaxError, RecursionError):
                    pass
    return out


def indicators(node: ast.AST):
    names = set()
    mapping = {
        'rsi': 'RSI', 'ema': 'EMA', 'sma': 'SMA', 'bbands': 'BOLLINGER',
        'macd': 'MACD', 'adx': 'ADX', 'atr': 'ATR', 'stoch': 'STOCH',
        'mfi': 'MFI', 'cci': 'CCI', 'ichimoku': 'ICHIMOKU',
        'sar': 'SAR', 'heikinashi': 'HEIKIN_ASHI',
        'crossed_above': 'CROSSOVER', 'crossed_below': 'CROSSOVER',
        'vwap': 'VWAP', 'supertrend': 'SUPERTREND', 'obv': 'OBV',
        'roc': 'ROC', 'bollinger_bands': 'BOLLINGER', 'williams_r': 'WILLIAMS_R',
    }
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            func = n.func
            if isinstance(func, ast.Attribute):
                name = func.attr.lower()
            elif isinstance(func, ast.Name):
                name = func.id.lower()
            else:
                continue
            if name in mapping:
                names.add(mapping[name])
    return sorted(names)


def written_columns(node: ast.AST):
    """Extract literal DataFrame signal-column names from the strategy method.

    Name appearance is not sufficient: it must occur in an assignment target.
    This is a conservative scanner; complex writers are flagged as unknown.
    """
    found = set()
    for n in ast.walk(node):
        targets = []
        if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            if isinstance(n, ast.Assign):
                targets.extend(n.targets)
            else:
                targets.append(n.target)
        for target in targets:
            for part in ast.walk(target):
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    if part.value in ENTRY_COLUMNS | EXIT_COLUMNS:
                        found.add(part.value)
    return found


def extract_conditions(source: str, method: ast.FunctionDef, limit: int = 260):
    """Return brief, sanitized source excerpts for informative comparison only."""
    examples = []
    for n in ast.walk(method):
        if not isinstance(n, (ast.Assign, ast.AugAssign)):
            continue
        targets = n.targets if isinstance(n, ast.Assign) else [n.target]
        if not any(
            isinstance(v, ast.Constant) and isinstance(v.value, str)
            and v.value in ENTRY_COLUMNS | EXIT_COLUMNS
            for target in targets for v in ast.walk(target)
        ):
            continue
        # For dataframe.loc[conditions, signal] = 1, conditions are in slice.Tuple[0].
        target = targets[0]
        if isinstance(target, ast.Subscript):
            sl = target.slice
            if isinstance(sl, ast.Tuple) and sl.elts:
                expr = ast.get_source_segment(source, sl.elts[0])
                if expr:
                    short = ' '.join(expr.split())[:limit]
                    examples.append(short)
        if len(examples) >= 2:
            break
    return examples


def class_methods(klass):
    return {n.name: n for n in klass.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def strategy_objects(source: str, repo: str, commit: str, path: str):
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError, TypeError, RecursionError) as exc:
        return [], 'SYNTAX_OR_PARSE_ERROR:' + type(exc).__name__
    result = []
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef)][:MAX_AST_CLASSES]
    for klass in classes:
        methods = class_methods(klass)
        entry = [methods[k] for k in ENTRY_METHODS if k in methods]
        exits = [methods[k] for k in EXIT_METHODS if k in methods]
        if not entry:
            continue
        attrs = literal_attrs(klass)
        entry_writes = set().union(*(written_columns(x) for x in entry))
        exit_writes = set().union(*(written_columns(x) for x in exits)) if exits else set()
        entry_ok = bool(entry_writes & ENTRY_COLUMNS)
        exit_ok = bool(exit_writes & EXIT_COLUMNS)
        # Class may subclass another strategy; without importing any code,
        # its inherited behavior cannot be represented accurately.
        inherited_unknown = not entry_ok or not exit_ok
        complex_methods = sorted(set(methods) & {
            'custom_exit', 'custom_stoploss', 'custom_entry_price',
            'custom_exit_price', 'adjust_trade_position', 'leverage',
            'informative_pairs', 'populate_indicators_btc',
            'confirm_trade_entry', 'confirm_trade_exit',
        })
        tf = str(attrs.get('timeframe', attrs.get('ticker_interval', '?')))
        indicator_set = sorted(set().union(*(set(indicators(x)) for x in
                                            [*entry, *exits, *(list(methods.values()))])))
        combined = '\n'.join(ast.dump(x, annotate_fields=True, include_attributes=False)
                             for x in [*entry, *exits])
        fingerprint = sha(combined.encode())[:20]
        roi = attrs.get('minimal_roi')
        roi0 = None
        if isinstance(roi, dict):
            try:
                roi0 = float(roi.get('0', roi.get(0))) * 10000
            except (ValueError, TypeError):
                pass
        # Conservative pre-screen. This does not mean any strategy is profitable.
        reasons = []
        if not entry_ok:
            reasons.append('ENTRY_NOT_STATICALLY_CONFIRMED')
        if not exit_ok:
            reasons.append('EXIT_NOT_STATICALLY_CONFIRMED')
        if tf not in COMMON_TF:
            reasons.append('UNKNOWN_OR_UNSUPPORTED_TIMEFRAME')
        if attrs.get('can_short'):
            reasons.append('SHORT_OR_DERIVATIVE_CAPABILITY')
        if attrs.get('position_adjustment_enable'):
            reasons.append('POSITION_ADJUSTMENT_CAPITAL_REQUIREMENTS')
        if complex_methods:
            reasons.append('COMPLEX_RUNTIME_CALLBACKS')
        if 'informative_pairs' in methods:
            reasons.append('ADDITIONAL_PAIR_OR_TIMEFRAME_DATA')
        if 'stoploss' not in attrs:
            reasons.append('STOPLOSS_NOT_STATICALLY_DECLARED')
        if inherited_unknown:
            reasons.append('POSSIBLE_INHERITED_OR_DYNAMIC_RULES')
        # For small available balance, timeframes 5m/15m generally cause high
        # turnover. This is prioritization only, not a statistical conclusion.
        score = (int(entry_ok)*3 + int(exit_ok)*3 + int(tf in COMMON_TF)*2
                 + int(isinstance(attrs.get('stoploss'), (float, int)))*1
                 - (2 if attrs.get('can_short') else 0)
                 - (2 if attrs.get('position_adjustment_enable') else 0)
                 - (2 if complex_methods else 0)
                 + (1 if tf in {'1h', '4h', '1d'} else 0))
        examples = []
        for method in entry[:1] + exits[:1]:
            examples.extend(extract_conditions(source, method))
        result.append({
            'strategy': klass.name,
            'source_repository': repo,
            'source_commit_sha': commit,
            'source_file': path,
            'source_url': f'https://github.com/{repo}/blob/{commit}/{path}',
            'timeframe': tf,
            'stoploss': attrs.get('stoploss'),
            'minimal_roi_0_bps': roi0,
            'can_short': attrs.get('can_short', False),
            'trailing_stop': attrs.get('trailing_stop'),
            'entry_rule_confirmed': entry_ok,
            'exit_rule_confirmed': exit_ok,
            'entry_methods': [n.name for n in entry],
            'exit_methods': [n.name for n in exits],
            'indicators': indicator_set,
            'complex_callbacks': complex_methods,
            'condition_examples': examples[:2],
            'behavior_fingerprint': fingerprint,
            'source_file_sha256': sha(source.encode()),
            'research_priority_score_not_pnl': score,
            'review_flags': sorted(set(reasons)),
            'status': 'DISCOVERED_NOT_BACKTESTED',
            'live_permission': False,
        })
    return result, None


def scan_archive(archive: bytes, repo: str, commit: str, max_files: int):
    found = []
    issues = collections.Counter()
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        if len(z.infolist()) > 8000:
            raise ValueError('ARCHIVE_TOO_MANY_MEMBERS')
        total_bytes = 0
        for member in z.infolist():
            if len(found) >= max_files:
                issues['MAX_STRATEGIES_REACHED'] += 1
                break
            path = member_source_path(member.filename)
            if path is None:
                continue
            issues['CANDIDATE_PY_FILES'] += 1
            if member.file_size > MAX_FILE_BYTES or member.file_size < 100:
                issues['SKIPPED_FILE_SIZE'] += 1
                continue
            total_bytes += member.file_size
            if total_bytes > 24 * 1024 * 1024:
                issues['MAX_DECOMPRESSED_SOURCE_LIMIT'] += 1
                break
            try:
                code = z.read(member).decode('utf-8-sig', errors='replace')
            except (zipfile.BadZipFile, RuntimeError, ValueError):
                issues['BAD_ARCHIVE_MEMBER'] += 1
                continue
            parsed, problem = strategy_objects(code, repo, commit, path)
            if problem:
                issues[problem] += 1
            else:
                found.extend(parsed)
    return found, dict(issues)


def deduplicate(strategies):
    chosen = {}
    for candidate in strategies:
        fp = candidate['behavior_fingerprint']
        if fp not in chosen or (
            candidate['research_priority_score_not_pnl'] >
            chosen[fp]['research_priority_score_not_pnl']
        ):
            chosen[fp] = candidate
    return list(chosen.values())


def select(strategies, count: int = 15):
    eligible = [x for x in strategies if x['entry_rule_confirmed']
                and x['exit_rule_confirmed'] and not x['can_short']]
    eligible.sort(key=lambda x: (
        -x['research_priority_score_not_pnl'],
        len(x['review_flags']),
        x['strategy'].lower(), x['source_repository'],
    ))
    # diversity: avoid generating 15 near-identical choices from one family
    chosen = []
    seen = collections.Counter()
    for item in eligible:
        family = tuple(item['indicators'][:2]) or ('PRICE_ONLY',)
        if seen[family] >= 3:
            continue
        seen[family] += 1
        chosen.append(item)
        if len(chosen) >= count:
            break
    return chosen


def write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2)
    tmp = path.with_name('.' + path.name + '.tmp.' + str(time.time_ns()))
    try:
        with tmp.open('x', encoding='utf-8') as f:
            f.write(raw + '\n')
        tmp.chmod(0o600)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def selftest():
    sample = '''from freqtrade.strategy import IStrategy
class IndependentSample(IStrategy):
    timeframe = '1h'
    stoploss = -0.08
    minimal_roi = {"0": 0.01}
    def populate_indicators(self, dataframe, metadata):
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        return dataframe
    def populate_entry_trend(self, dataframe, metadata):
        dataframe.loc[(dataframe["rsi"] < 30), "enter_long"] = 1
        return dataframe
    def populate_exit_trend(self, dataframe, metadata):
        dataframe.loc[(dataframe["rsi"] > 70), "exit_long"] = 1
        return dataframe
'''
    result, error = strategy_objects(sample, 'example/fixture', '0'*40, 'strategy.py')
    assert error is None and len(result) == 1
    a = result[0]
    assert a['entry_rule_confirmed'] and a['exit_rule_confirmed']
    assert a['timeframe'] == '1h' and a['minimal_roi_0_bps'] == 100
    assert 'RSI' in a['indicators']
    assert a['condition_examples']
    assert a['status'] == 'DISCOVERED_NOT_BACKTESTED'
    b, error = strategy_objects('import os\nos.system("echo never_execute")', 'example/fixture', '0'*40, 'bad.py')
    assert b == [] and error is None
    assert member_source_path('repo-main/user_data/strategies/X.py') == 'user_data/strategies/X.py'
    assert member_source_path('repo-main/user_data/strategy_list.md') is None
    print('SELFTEST: PASS / STATIC_AST_ONLY / ZERO_EXECUTION')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--selftest', action='store_true')
    ap.add_argument('--max-source-files', type=int, default=600)
    ap.add_argument('--output', default=str(WORK / 'real_strategy_catalog_v2.json'))
    args = ap.parse_args()
    if args.selftest:
        selftest()
        return
    if not 1 <= args.max_source_files <= MAX_SOURCE_FILES:
        raise ValueError('INVALID_MAX_SOURCE_FILES')

    print('=== KIDSBOT UNIVERSAL V2 / REAL STRATEGY SOURCE DISCOVERY ===', flush=True)
    print('PUBLIC_GITHUB_ONLY / EXACT_COMMIT_SNAPSHOT / NO_REMOTE_CODE_EXECUTION', flush=True)
    discovered = []
    failures = {}
    provenance = []
    for repo in REPOSITORIES:
        try:
            branch, commit, archive = repo_snapshot(repo)
            catalog, issues = scan_archive(archive, repo, commit, args.max_source_files)
            discovered.extend(catalog)
            provenance.append({
                'repository': repo, 'branch': branch,
                'commit': commit, 'archive_sha256': sha(archive),
                'strategies': len(catalog), 'issues': issues,
            })
            print(f'REPO: {repo} / COMMIT={commit[:12]} / PARSED_STRATEGIES={len(catalog)} '
                  f'/ PY_FILES={issues.get("CANDIDATE_PY_FILES", 0)}', flush=True)
        except (ValueError, KeyError, OSError, RuntimeError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
            failures[repo] = str(exc)[:180]
            print(f'REPO_UNAVAILABLE: {repo}: {str(exc)[:180]}', flush=True)

    if not discovered:
        raise RuntimeError('NO_REAL_STRATEGY_SOURCES_FOUND; NO_RESULT_TO_RANK')

    unique = deduplicate(discovered)
    shortlist = select(unique, count=15)
    families = collections.Counter(','.join(x['indicators'][:2]) or 'PRICE_ONLY' for x in unique)
    result = {
        'name': 'KIDSBOT_UNIVERSAL_V2_REAL_STRATEGY_CATALOG',
        'timestamp_utc': datetime.now(timezone.utc).isoformat(),
        'repo_snapshots': provenance,
        'repo_failures': failures,
        'raw_strategy_classes': len(discovered),
        'unique_strategy_behaviors': len(unique),
        'with_explicit_entry_exit': sum(x['entry_rule_confirmed'] and x['exit_rule_confirmed'] for x in unique),
        'shortlist_count': len(shortlist),
        'strategy_indicators_family_counts': dict(families.most_common(25)),
        'shortlist': shortlist,
        'catalog': unique,
        'conclusion': 'REAL_SOURCE_CATALOG_ONLY_NO_NET_PNL_YET',
        'not_independent_holdout': True,
        'research_only': True,
        'live_permission': False,
    }
    out = Path(args.output)
    write_json(out, result)
    print('\n=== REAL SOURCE SHORTLIST / NOT YET PROFITABLE ===')
    for item in shortlist:
        print(f'{item["strategy"]} / tf={item["timeframe"]} / '
              f'indicators={",".join(item["indicators"][:4]) or "PRICE"} / '
              f'stop={item["stoploss"]} / flags={",".join(item["review_flags"][:2]) or "NONE"}')
        print('  SOURCE:', item['source_url'])
        print('  ENTRY_SAMPLE:', (item['condition_examples'][:1] or ['NOT_EXTRACTED'])[0][:130])
    print('\n=== FINAL V2 DECISION ===')
    print('REAL_STRATEGY_CLASSES:', len(discovered))
    print('UNIQUE_RULESETS:', len(unique))
    print('REVIEW_SHORTLIST:', len(shortlist))
    print('CATALOG:', out)
    print('NEXT_REQUIRED: REPRODUCE_ENTRY_EXIT_IN_ISOLATED_BACKTEST_WITH_TRADING_COSTS')
    print('NET_PROFITABILITY_PROVEN: NO')
    print('BINANCE_AUTH_CALLS: 0 / NEW_ORDERS: 0 / LIVE_PERMISSION: NO')
    print('FROZEN_SPOT60_REGIME24_ACCESSED: NO / SOURCE_CODE_EXECUTED: NO')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('INCONCLUSIVE_OR_ERROR:', type(exc).__name__, str(exc)[:220])
        sys.exit(2)
