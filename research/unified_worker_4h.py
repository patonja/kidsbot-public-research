#!/usr/bin/env python3
"""KIDSBOT V7: 4h public-source strategy screening in NO-NETWORK container.

WARNING: NOT Freqtrade equivalent; OHLC stop/ROI execution and fee/slippage
are research proxies. A few causal prefix checks do NOT prove no lookahead.
No credentials. No exchange access. No trading permission.
"""
import argparse
import importlib.util
import inspect
import json
import math
import re
import sys
from pathlib import Path
import pandas as pd

PAIRS = ('ETHUSDC', 'LINKUSDC', 'SOLUSDC', 'SUIUSDC', 'XRPUSDC')
STRATEGIES = ('AverageStrategy', 'ReinforcedAverageStrategy', 'mabStra', 'Heracles')
TIMEFRAME = '4h'
BAR_MIN = 240
STEP_MS = BAR_MIN * 60_000
START_TS = 1735689600000
EXPECTED = 1638                 # Jan 1 -> Oct 1, 2025, exclusive (273*6)
MIN_WARMUP = 150
FEE_SIDE = 0.001
OTHER_SIDE = 0.001
COST_SIDE = FEE_SIDE + OTHER_SIDE
START_CAPITAL = 16.82
STAKE = 10.0
MIN_NOTIONAL = 5.0


def need(ok, reason):
    if not ok:
        raise ValueError(reason)


def load(pair):
    p = Path('/research/data') / (pair[:-4] + '_USDC-4h.json')
    raw = json.loads(p.read_text())
    need(len(raw) == EXPECTED, 'BAD_CANDLE_COUNT_'+pair)
    need(all(len(r)==6 and r[0]==START_TS+i*STEP_MS for i,r in enumerate(raw)),
         'BAD_SEQUENCE_'+pair)
    df = pd.DataFrame(raw,columns=('timestamp','open','high','low','close','volume'))
    df['date']=pd.to_datetime(df.pop('timestamp'),unit='ms',utc=True)
    vals=df[['open','high','low','close','volume']]
    need(bool(vals.notna().all().all()) and bool((vals[['open','high','low','close']]>0).all().all()),
         'INVALID_PRICE_'+pair)
    need(bool((df['high']>=df[['open','close','low']].max(axis=1)).all()) and
         bool((df['low']<=df[['open','close','high']].min(axis=1)).all()) and
         bool((df['volume']>=0).all()),'BAD_OHLC_'+pair)
    return df


def original(name):
    path = Path('/research/strategies') / (name+'.py')
    need(path.is_file(),'MISSING_SOURCE')
    spec=importlib.util.spec_from_file_location('kidsbot_4h_'+name,str(path))
    mod=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)     # only inside docker --network none --read-only
    cls=getattr(mod,name)
    need(inspect.isclass(cls),'NOT_A_CLASS')
    def own(method):
        return callable(getattr(cls,method,None)) and method in cls.__dict__
    need(own('populate_indicators'),'NO_INDICATORS')
    modern=own('populate_entry_trend') and own('populate_exit_trend')
    old=own('populate_buy_trend') and own('populate_sell_trend')
    need(modern or old,'UNSUPPORTED_SIGNALS')
    for method in ('custom_exit','custom_stoploss','custom_roi','adjust_trade_position',
                   'confirm_trade_entry','confirm_trade_exit','custom_entry_price',
                   'custom_exit_price','leverage'):
        need(not own(method),'UNSUPPORTED_CALLBACK_'+method)
    need(not bool(getattr(cls,'position_adjustment_enable',False)),'UNSUPPORTED_DCA')
    need(not bool(getattr(cls,'use_custom_stoploss',False)),'UNSUPPORTED_CUSTOM_STOP')
    need(not bool(getattr(cls,'can_short',False)),'UNSUPPORTED_SHORT')
    need(str(getattr(cls,'timeframe','4h'))=='4h','NOT_4H')
    need(not bool(getattr(cls,'exit_profit_only',False)),'UNMODELED_EXIT_PROFIT_ONLY')
    need(not bool(getattr(cls,'ignore_roi_if_entry_signal',False)),'UNMODELED_ROI_OVERRIDE')
    cfg={'dry_run':True,'trading_mode':'spot','stake_currency':'USDC',
         'stake_amount':STAKE,'max_open_trades':1,'timeframe':'4h',
         'exchange':{'name':'binance','key':'','secret':''}}
    obj=cls(config=cfg)
    roi=getattr(obj,'minimal_roi',{})
    need(isinstance(roi,dict),'ROI_NOT_DICT')
    roi=sorted(((int(t),float(v)) for t,v in roi.items()),reverse=True)
    need(bool(roi) and all(t>=0 and math.isfinite(v) and -1<v<100 for t,v in roi),
         'BAD_ROI')
    sl=float(getattr(obj,'stoploss',-1))
    need(math.isfinite(sl) and -1<=sl<0,'BAD_STOP')
    trailing=bool(getattr(obj,'trailing_stop',False))
    positive=float(getattr(obj,'trailing_stop_positive',0) or 0)
    offset=float(getattr(obj,'trailing_stop_positive_offset',0) or 0)
    only=bool(getattr(obj,'trailing_only_offset_is_reached',False))
    need(all(map(math.isfinite,[positive,offset])) and 0<=positive<1 and 0<=offset<1,
         'BAD_TRAILING_PARAMS')
    warmup=max(MIN_WARMUP,int(getattr(obj,'startup_candle_count',0)))
    need(warmup<EXPECTED-40,'INSUFFICIENT_BARS')
    risk={'roi':roi,'stoploss':sl,'trailing':trailing,'positive':positive,
          'offset':offset,'only_offset':only,'use_exit_signal':bool(getattr(obj,'use_exit_signal',True))}
    return obj,modern,warmup,risk


def signals(obj,modern,df,pair,warmup):
    meta={'pair':pair[:-4]+'/USDC'}
    frame=obj.populate_indicators(df.copy(),meta)
    need(isinstance(frame,pd.DataFrame) and len(frame)==len(df),'BAD_INDICATORS')
    if modern:
        frame=obj.populate_entry_trend(frame,meta)
        frame=obj.populate_exit_trend(frame,meta)
        buy,sell='enter_long','exit_long'
    else:
        frame=obj.populate_buy_trend(frame,meta)
        frame=obj.populate_sell_trend(frame,meta)
        buy,sell='buy','sell'
    need(isinstance(frame,pd.DataFrame) and len(frame)==len(df) and buy in frame and sell in frame,
         'BAD_SIGNAL_COLUMNS')
    def to_boolean(col):
        values=pd.to_numeric(frame[col],errors='coerce').fillna(0).eq(1).tolist()
        values[:warmup]=[False]*min(len(values),warmup)
        return values
    return to_boolean(buy),to_boolean(sell)


def causal_spotcheck(obj,modern,dfs,signalmap,warmup):
    # Prefix snapshots catch obvious negative shifts and future-dependent code.
    # This is *not* exhaustive proof of causality.
    checked=0
    for pair,df in dfs.items():
        full=signalmap[pair]
        # Focus on both active signal times and time-dispersed anchors.
        found=[i for i in range(warmup+16,len(df)-1) if full[0][i] or full[1][i]]
        anchors=[warmup+60, len(df)//2,len(df)-10]
        if found:
            anchors.extend([found[0],found[len(found)//2],found[-1]])
        for idx in sorted(set(x for x in anchors if warmup<=x<len(df)-1)):
            prefix=signals(obj,modern,df.iloc[:idx+1].copy(),pair,warmup)
            checked+=1
            need(prefix[0][idx]==full[0][idx] and prefix[1][idx]==full[1][idx],
                 'CAUSAL_PREFIX_MISMATCH_'+pair+'_BAR_'+str(idx))
    return checked


def roi_at(table, minutes):
    return next((v for m,v in table if minutes>=m),None)


def updated_stop(previous_stop,high,entry,risk):
    if not risk['trailing']:
        return previous_stop
    # Positive trailing activates only after the specified offset.
    reached=high>=entry*(1+risk['offset'])
    if risk['only_offset'] and not reached:
        return previous_stop
    if risk['positive']>0 and reached:
        ratio=-risk['positive']
    else:
        ratio=risk['stoploss']
    return max(previous_stop,high*(1+ratio))


def step_exit(row,entry,previous_stop,held,risk,exit_sig):
    op,hi,lo=(float(row[k]) for k in ('open','high','low'))
    if exit_sig and risk['use_exit_signal']:
        return op,'EXIT_SIGNAL_NEXT_OPEN',previous_stop
    # Gap and intrabar stop use ONLY previous-bar stop. Trailing is raised
    # at the END of current bar to avoid claiming a known intrabar price path.
    if op<=previous_stop:
        return op,'STOP_GAP',previous_stop
    if lo<=previous_stop:
        return previous_stop,'STOP_PREVIOUS_BAR',previous_stop
    threshold=roi_at(risk['roi'],held*BAR_MIN)
    if threshold is not None:
        target=entry*(1+threshold)
        if op>=target:
            return op,'ROI_GAP',previous_stop
        if hi>=target:
            return target,'ROI_INTRABAR_PROXY',previous_stop
    return None,None,updated_stop(previous_stop,hi,entry,risk)


def replay(dfs,sigs,risk,warmup,funded):
    trades=[];position=None;capital=START_CAPITAL;blocked=False
    candles=EXPECTED
    final_mark=None
    for i in range(warmup+1,candles):
        just_closed=False
        if position:
            pair=position['pair'];row=dfs[pair].iloc[i]
            price,reason,next_stop=step_exit(row,position['entry'],position['stop'],
                                            i-position['idx'],risk,sigs[pair][1][i-1])
            if price is not None:
                gain=price*(1-COST_SIDE)/(position['entry']*(1+COST_SIDE))-1
                trades.append({'pair':pair,'date':position['day'],'net':gain,
                               'reason':reason,'exit_notional_usdc':STAKE*(1+gain)})
                capital+=STAKE*gain
                position=None;just_closed=True
            else:
                position['stop']=next_stop
        if position or just_closed:
            continue
        if funded and capital < STAKE*(1+COST_SIDE):
            blocked=True
            break
        for pair in PAIRS:
            if sigs[pair][0][i-1] and float(dfs[pair].iloc[i]['volume'])>0:
                row=dfs[pair].iloc[i]
                entry=float(row['open'])
                position={'pair':pair,'idx':i,'entry':entry,
                          'stop':entry*(1+risk['stoploss']), 'day':str(row['date'])[:10]}
                price,reason,next_stop=step_exit(row,entry,position['stop'],0,risk,False)
                if price is not None:
                    gain=price*(1-COST_SIDE)/(entry*(1+COST_SIDE))-1
                    trades.append({'pair':pair,'date':position['day'],'net':gain,
                                   'reason':reason,'exit_notional_usdc':STAKE*(1+gain)})
                    capital+=STAKE*gain
                    position=None
                else:
                    position['stop']=next_stop
                break
    if position:
        close=float(dfs[position['pair']].iloc[-1]['close'])
        mtm=close*(1-COST_SIDE)/(position['entry']*(1+COST_SIDE))-1
        final_mark={'pair':position['pair'],'open_date':position['day'],
                    'mark_to_market_usdc':round(STAKE*mtm,6),
                    'counted_in_closed_trades':False}
    return {'trades':trades,'ending_realized_wallet_usdc':round(capital,6),
            'blocked_by_10_usdc_stake':blocked,'open_at_boundary':final_mark,
            'exit_below_5_usdc_notional':sum(t['exit_notional_usdc']<MIN_NOTIONAL for t in trades)}


def grade(trades):
    n=len(trades);days={};exits={};minimum=None
    for x in trades:
        net=x['net']
        need(math.isfinite(net) and -1<net<100,'BAD_NET')
        days[x['date']]=days.get(x['date'],0.0)+net
        exits[x['reason']]=exits.get(x['reason'],0)+1
        minimum=net if minimum is None else min(minimum,net)
    day_count=len(days)
    trade_avg=sum(t['net'] for t in trades)/n if n else None
    return {'trades':n,'active_utc_days':day_count,
            'mean_net_bps':round(10000*trade_avg,4) if trade_avg is not None else None,
            'active_day_net_bps':round(10000*sum(days.values())/day_count,4) if day_count else None,
            'worst_closed_trade_pct':round(100*minimum,3) if minimum is not None else None,
            'exit_reasons':exits,
            'decision':('INSUFFICIENT_SAMPLE' if n<50 or day_count<20 else
                        'NEGATIVE_DISCOVERY_PROXY' if trade_avg<=0 else
                        'POSITIVE_DISCOVERY_PROXY_NOT_LIVE_ELIGIBLE'),
            'holdout_opened':False,'live_permission':False}


def selftest():
    risk={'roi':[(0,.08)],'stoploss':-.1,'trailing':True,'positive':.02,
          'offset':.03,'only_offset':True,'use_exit_signal':True}
    assert updated_stop(90,102,100,risk)==90
    assert abs(updated_stop(90,104,100,risk)-101.92)<1e-6
    assert updated_stop(101.92,101,100,risk)==101.92
    # Only PREVIOUS stop can trigger on bar; avoid invented OHLC ordering.
    row={'open':100,'high':108,'low':94}
    assert step_exit(row,100,96,1,risk,False)[1]=='STOP_PREVIOUS_BAR'
    assert step_exit({'open':100,'high':104,'low':99},100,90,1,risk,False)[2]>101
    assert grade([])['decision']=='INSUFFICIENT_SAMPLE'
    assert EXPECTED==1638 and len(STRATEGIES)==4
    print('V7 WORKER SELFTEST PASS / NO NETWORK / NO TRADES')


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--name',required=True)
    args=ap.parse_args()
    if args.name=='SELFTEST':
        return selftest()
    need(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}',args.name) is not None, 'BAD_STRATEGY_NAME')
    obj,modern,warmup,risk=original(args.name)
    dfs={pair:load(pair) for pair in PAIRS}
    sigs={pair:signals(obj,modern,dfs[pair],pair,warmup) for pair in PAIRS}
    prefix_checks=causal_spotcheck(obj,modern,dfs,sigs,warmup)
    unconstrained=replay(dfs,sigs,risk,warmup,funded=False)
    constrained=replay(dfs,sigs,risk,warmup,funded=True)
    result={'strategy':args.name,'status':'SOURCE_4H_PROXY',
            'score':grade(unconstrained['trades']),
            'uncapped_fixed_stake':{key:val for key,val in unconstrained.items() if key!='trades'},
            'funded_16_82_usdc':{**{key:val for key,val in constrained.items() if key!='trades'},
                                'closed_trades':len(constrained['trades'])},
            'causal_prefix_spotchecks_passed':prefix_checks,
            'causality':'NOT_PROVEN_BY_PREFIX_SPOTCHECK',
            'trailing_semantics':'MONOTONIC_PREVIOUS_BAR_STOP; NOT_FREQTRADE_EQUIVALENT',
            'sample':'2025-01-01_TO_2025-10-01_EXCLUSIVE',
            'fee_per_side':FEE_SIDE,'other_cost_per_side':OTHER_SIDE,
            'price_model':'NEXT_OPEN/STOP_PREVIOUS_BAR/ROI_OHLC_PROXY',
            'live_permission':False,'holdout_opened':False}
    print('KIDSBOT_V7_RESULT='+json.dumps(result,sort_keys=True),flush=True)


if __name__=='__main__':
    try:
        main()
    except Exception as e:
        print('V7_WORKER_ERROR='+type(e).__name__+': '+str(e)[:250],file=sys.stderr,flush=True)
        sys.exit(2)
