#!/usr/bin/env python3
"""Read-only source-signal *proxy* simulation. NOT a Freqtrade backtest.

Runs original pinned source in isolated, unprivileged, NO-NETWORK Docker.
Uses original indicator/entry/exit methods where compatible. Own simplified
next-open/stop-first/ROI simulator. No exchange / CCXT / execution.
"""
import argparse
import importlib.util
import inspect
import json
import math
import sys
from pathlib import Path

import pandas as pd

PAIRS = ('ETHUSDC','LINKUSDC','SOLUSDC','SUIUSDC','XRPUSDC')
FEE_SIDE = 0.001
EXTRA_SIDE = 0.001
COST_SIDE = FEE_SIDE + EXTRA_SIDE
EXPECTED_CANDLES = 6552
START_TS = 1735689600000
STEP_MS = 3600000
MIN_WARMUP = 150


def need(condition, reason):
    if not condition:
        raise ValueError(reason)


def load_candles(pair):
    name = pair[:-4] + '_USDC-1h.json'
    raw = json.loads((Path('/research/data') / name).read_text())
    need(len(raw) == EXPECTED_CANDLES, 'BAD_CANDLE_COUNT_'+pair)
    need(all(row[0] == START_TS + i*STEP_MS and len(row)==6 for i,row in enumerate(raw)),
         'BAD_CANDLE_SEQUENCE_'+pair)
    df = pd.DataFrame(raw, columns=['timestamp','open','high','low','close','volume'])
    df['date'] = pd.to_datetime(df.pop('timestamp'), unit='ms', utc=True)
    need(bool((df['low'] <= df[['open','close','high']].min(axis=1)).all()),'BAD_LOW_'+pair)
    need(bool((df['high'] >= df[['open','close','low']].max(axis=1)).all()),'BAD_HIGH_'+pair)
    return df


def use_original_strategy(name):
    path = Path('/research/strategies') / (name+'.py')
    need(path.is_file(), 'SOURCE_MISSING')
    spec = importlib.util.spec_from_file_location('kidsbot_pinned_'+name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # only within offline Docker, never on VPS
    cls = getattr(module, name)
    need(inspect.isclass(cls), 'STRATEGY_CLASS_NOT_FOUND')
    # Require source-level implementations; inherited no-op default isn't enough.
    def overridden(method):
        return callable(getattr(cls,method,None)) and method in cls.__dict__
    need(overridden('populate_indicators'),'MISSING_INDICATORS')
    modern = overridden('populate_entry_trend') and overridden('populate_exit_trend')
    old = overridden('populate_buy_trend') and overridden('populate_sell_trend')
    need(modern or old, 'UNSUPPORTED_SIGNAL_METHODS')
    # Fail closed for risk callbacks not modeled by own simplified simulator.
    for method in ('custom_exit','custom_stoploss','custom_roi','adjust_trade_position',
                   'confirm_trade_entry','confirm_trade_exit'):
        need(not overridden(method),'UNSUPPORTED_CUSTOM_CALLBACK_'+method)
    need(not bool(getattr(cls,'trailing_stop',False)),'UNSUPPORTED_TRAILING_STOP')
    need(not bool(getattr(cls,'use_custom_stoploss',False)),'UNSUPPORTED_CUSTOM_STOPLOSS')
    need(not bool(getattr(cls,'position_adjustment_enable',False)),'UNSUPPORTED_POSITION_ADJUSTMENT')
    need(str(getattr(cls,'timeframe','1h'))=='1h','NOT_1H')
    need(bool(getattr(cls,'can_short',False)) is False,'SHORT_NOT_ALLOWED')
    # Strategy constructor uses config; never initializes an exchange or calls external services.
    cfg = {'dry_run':True,'trading_mode':'spot','stake_currency':'USDC',
           'stake_amount':10,'max_open_trades':1,'timeframe':'1h',
           'exchange':{'name':'binance','key':'','secret':''}}
    strategy = cls(config=cfg)
    roi = getattr(strategy,'minimal_roi',{})
    need(isinstance(roi,dict), 'ROI_UNSUPPORTED')
    roi = sorted(((int(k),float(v)) for k,v in roi.items()),reverse=True)
    need(all(k>=0 and math.isfinite(v) and -1<v<100 for k,v in roi),'BAD_ROI')
    sl = float(getattr(strategy,'stoploss',-1))
    need(math.isfinite(sl) and -1 <= sl < 0,'BAD_STOPLOSS')
    warmup = max(MIN_WARMUP, int(getattr(strategy,'startup_candle_count',0)))
    need(warmup < EXPECTED_CANDLES-100,'BAD_WARMUP')
    return strategy, modern, roi, sl, warmup


def series_signals(strategy, modern, df, pair, warmup):
    frame = df.copy()
    meta = {'pair':pair[:-4]+'/USDC'}
    # Indicators and signal functions operate on full series. Lookahead NOT ruled out.
    frame = strategy.populate_indicators(frame,meta)
    need(isinstance(frame,pd.DataFrame) and len(frame)==len(df),'BAD_INDICATOR_OUTPUT')
    if modern:
        frame = strategy.populate_entry_trend(frame,meta)
        frame = strategy.populate_exit_trend(frame,meta)
        entry,exit_ = 'enter_long','exit_long'
    else:
        frame = strategy.populate_buy_trend(frame,meta)
        frame = strategy.populate_sell_trend(frame,meta)
        entry,exit_ = 'buy','sell'
    need(isinstance(frame,pd.DataFrame) and len(frame)==len(df),'BAD_SIGNAL_OUTPUT')
    need(entry in frame and exit_ in frame,'MISSING_SIGNAL_COLUMNS')
    def bool_signals(col):
        signals = pd.to_numeric(frame[col],errors='coerce').fillna(0).eq(1).tolist()
        for i in range(min(warmup,len(signals))):signals[i] = False
        return signals
    return bool_signals(entry),bool_signals(exit_)


def roi_at(roi,elapsed_minutes):
    return next((value for mins,value in roi if elapsed_minutes>=mins),None)


def decide_exit(candle, entry_price, held_bars, roi, sl, exit_signal):
    """Exit at next open on previous sell signal; stop-first OHLC otherwise.
    Assumes intrabar worst case when stop and ROI both touched.
    These are custom proxy semantics, explicitly not Freqtrade fill equivalence.
    """
    op,high,low = float(candle[1]),float(candle[2]),float(candle[3])
    if exit_signal:
        return op,'SIGNAL_NEXT_OPEN'
    stop = entry_price*(1+sl)
    if op <= stop:
        return op,'STOP_GAP'
    if low <= stop:
        return stop,'STOP_FIRST'
    threshold=roi_at(roi,held_bars*60)
    if threshold is not None:
        target=entry_price*(1+threshold)
        if op >= target:
            return op,'ROI_GAP'
        if high >= target:
            return target,'ROI_INTRABAR_PROXY'
    return None,None


def evaluate(dfs, signals, roi, sl, warmup):
    trades=[]
    wallet=16.82
    stake=10.0
    min_required=stake*(1+COST_SIDE)
    capital_blocked=None
    current=None
    for i in range(warmup+1,EXPECTED_CANDLES):
        just_closed=False
        if current is not None:
            pair=current['pair']
            candle = dfs[pair].iloc[i]
            # Include previous candle's exit signal (close-confirmed).
            exit_price, reason=decide_exit(
                (0,candle['open'],candle['high'],candle['low']),current['price'],
                i-current['index'],roi,sl,signals[pair][1][i-1])
            if exit_price is not None:
                buy_eff=current['price']*(1+COST_SIDE)
                sell_eff=exit_price*(1-COST_SIDE)
                net=sell_eff/buy_eff-1
                trades.append({'pair':pair,'date':current['day'], 'net':net,
                               'exit_reason':reason,'hours':i-current['index']})
                wallet+=stake*net
                current=None;just_closed=True
        if current is not None or just_closed:
            continue
        if wallet < min_required:
            capital_blocked='WALLET_BELOW_STAKE_AFTER_REALIZED_LOSSES'
            break
        for pair in PAIRS:
            if signals[pair][0][i-1] and df_has_volume(dfs[pair],i):
                candle=dfs[pair].iloc[i]
                current={'pair':pair,'index':i,'price':float(candle['open']),
                         'day':str(candle['date'])[:10]}
                # Protective order could hit during same entry candle.
                exit_price,reason=decide_exit(
                    (0,candle['open'],candle['high'],candle['low']),current['price'],
                    0,roi,sl,False)
                if exit_price is not None:
                    net=exit_price*(1-COST_SIDE)/(current['price']*(1+COST_SIDE))-1
                    trades.append({'pair':pair,'date':current['day'],'net':net,
                                   'exit_reason':reason,'hours':0})
                    wallet+=stake*net
                    current=None
                break
    if current is not None:
        # Explicitly mark unclosed rather than force a fictitious in-range exit.
        return trades,{'status':'OPEN_POSITION_AT_DISCOVERY_BOUNDARY','pair':current['pair']},wallet,capital_blocked
    return trades,None,wallet,capital_blocked


def df_has_volume(df,i):
    return float(df.iloc[i]['volume'])>0 and float(df.iloc[i]['open'])>0


def score(trades):
    days={};reasons={};pairs={};gross=0.0;minimum=0.0
    for tr in trades:
        r=tr['net']; need(math.isfinite(r) and -1<r<1000,'BAD_RETURN')
        days[tr['date']]=days.get(tr['date'],0)+r
        reasons[tr['exit_reason']]=reasons.get(tr['exit_reason'],0)+1
        pairs[tr['pair']]=pairs.get(tr['pair'],0)+1
        gross+=r;minimum=min(minimum,r)
    n=len(trades);d=len(days)
    mean=10000*gross/n if n else None
    return {'trades':n,'active_days':d,'mean_net_bps':round(mean,3) if n else None,
            'equal_active_day_net_bps':round(sum(days.values())/d*10000,3) if d else None,
            'worst_trade_pct':round(100*minimum,3) if n else None,
            'pairs':pairs,'exits':reasons,
            'decision':('INSUFFICIENT_SAMPLE' if n<50 or d<20 else
                        'NEGATIVE_PROXY' if mean is not None and mean<=0 else
                        'POSITIVE_PROXY_REQUIRES_CAUSAL_AND_FORWARD_VALIDATION'),
            'holdout_opened':False,'live_permission':False,
            'signal_equivalence':'SOURCE_METHODS_EXECUTED_WITH_CUSTOM_CONTEXT',
            'exit_equivalence':'NOT_FREQTRADE_EQUIVALENT',
            'lookahead_test':'NOT_PERFORMED',
            'min_notional_and_liquidity':'NOT_VERIFIED'}


def selftest():
    assert decide_exit((0,100,106,85),100,1,[(0,.05)],-.10,False)==(90,'STOP_FIRST')
    assert decide_exit((0,100,106,99),100,1,[(0,.05)],-.10,False)==(105,'ROI_INTRABAR_PROXY')
    assert decide_exit((0,95,120,80),100,1,[(0,.05)],-.10,True)==(95,'SIGNAL_NEXT_OPEN')
    assert score([{'date':'2025-01-01','pair':'ETHUSDC','exit_reason':'X','net':-.01}])['mean_net_bps']==-100
    print('OFFLINE WORKER SELFTEST PASS / NO NETWORK / NO ORDERS')


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--name', choices=list(PAIRS)+['SELFTEST'] + [
        'ADXMomentum','AdxSmas','AwesomeMacd','BbandRsi',
        'Freqtrade_backtest_validation_freqtrade1','HourBasedStrategy','Supertrend'])
    args=ap.parse_args()
    if args.name=='SELFTEST':return selftest()
    if not args.name or args.name in PAIRS:raise ValueError('BAD_STRATEGY_NAME')
    strategy,modern,roi,sl,warmup=use_original_strategy(args.name)
    dfs={};signals={}
    for pair in PAIRS:
        df=load_candles(pair)
        dfs[pair]=df
        signals[pair]=series_signals(strategy,modern,df,pair,warmup)
    trades,open_at_boundary,wallet,capital_blocked=evaluate(dfs,signals,roi,sl,warmup)
    outcome=score(trades)
    if open_at_boundary is not None:
        outcome['decision']='INCOMPLETE_UNCLOSED_POSITION'
    if capital_blocked is not None:
        outcome['decision']='CAPITAL_INSUFFICIENT_FOR_FIXED_10_USDC_STAKE'
    result={'status':'SOURCE_SIGNAL_PROXY','score':outcome,
            'strategy':args.name,'open_at_boundary':open_at_boundary,
            'ending_realized_wallet_usdc':round(wallet,4),
            'capital_blocked':capital_blocked,
            'period':'2025-01-01_2025-10-01_EXCLUSIVE',
            'fee_per_side':FEE_SIDE,'extra_per_side':EXTRA_SIDE,
            'max_parallel_positions':1,'stake_usdc':10,'wallet_usdc':16.82,
            'simulation':'NEXT_OPEN_STOP_FIRST_ROI_APPROXIMATION_NOT_FREQTRADE',
            'trade_validation':'RESEARCH_ONLY_NO_LIVE_PERMISSION',
            'wallet_model':'FIXED_10_USDC_STAKE_REALIZED_RETURN_ONLY'}
    print('KIDSBOT_OFFLINE_RESULT='+json.dumps(result,sort_keys=True),flush=True)


if __name__=='__main__':
    try:
        main()
    except Exception as e:
        print('WORKER_ERROR='+type(e).__name__+': '+str(e)[:250],file=sys.stderr,flush=True)
        sys.exit(2)
