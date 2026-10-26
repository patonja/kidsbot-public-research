#!/usr/bin/env python3
"""KIDSBOT V8: locked Q4 2025 retrospective validation in NO-NETWORK container.

WARNING: NOT Freqtrade equivalent; OHLC stop/ROI execution and fee/slippage
are research proxies. A few causal prefix checks do NOT prove no lookahead.
No credentials. No exchange access. No trading permission.
"""
import argparse
import importlib.util
import inspect
import json
import math
import sys
from pathlib import Path
import pandas as pd

PAIRS = ('ETHUSDC', 'LINKUSDC', 'SOLUSDC', 'SUIUSDC', 'XRPUSDC')
STRATEGIES = ('AverageStrategy', 'ReinforcedAverageStrategy')
TIMEFRAME = '4h'
BAR_MIN = 240
STEP_MS = BAR_MIN * 60_000
START_TS = 1735689600000
EXPECTED = 2190                 # Jan 1, 2025 -> Jan 1, 2026 exclusive (365*6)
Q4_START = 1638               # 2025-10-01 00:00 4h bar
Q4_FIRST_ENTRY = Q4_START + 1 # first signal must be closed IN Q4
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


def replay(dfs,sigs,risk,warmup,funded,start_idx=None,end_idx=None):
    trades=[];position=None;capital=START_CAPITAL;blocked=False
    candles=end_idx if end_idx is not None else EXPECTED
    final_mark=None
    for i in range(max(warmup+1, start_idx if start_idx is not None else warmup+1),candles):
        just_closed=False
        if position:
            pair=position['pair'];row=dfs[pair].iloc[i]
            price,reason,next_stop=step_exit(row,position['entry'],position['stop'],
                                            i-position['idx'],risk,sigs[pair][1][i-1])
            if price is not None:
                gain=price*(1-COST_SIDE)/(position['entry']*(1+COST_SIDE))-1
                trades.append({'pair':pair,'date':position['day'],'net':gain,
                               'reason':reason,'exit_notional_usdc':STAKE*(1+gain),
                               'entry_idx':position['idx']})
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
                                   'reason':reason,'exit_notional_usdc':STAKE*(1+gain),
                               'entry_idx':position['idx']})
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
                    'entry_idx':position['idx'],
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
    assert EXPECTED==2190 and len(STRATEGIES)==2 and Q4_START==1638
    print('V7 WORKER SELFTEST PASS / NO NETWORK / NO TRADES')


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--name',choices=STRATEGIES+('SELFTEST',),required=True)
    args=ap.parse_args()
    if args.name=='SELFTEST':
        return selftest()
    obj,modern,warmup,risk=original(args.name)
    dfs={pair:load(pair) for pair in PAIRS}
    sigs={pair:signals(obj,modern,dfs[pair],pair,warmup) for pair in PAIRS}
    # Confirm that appending unseen Q4 candles cannot retroactively change
    # any observed Jan-Sep buy/sell signals. This is a whole-history
    # append-stability check; it does not prove all kinds of no-lookahead.
    for pair in PAIRS:
        previous=signals(obj,modern,dfs[pair].iloc[:Q4_START].copy(),pair,warmup)
        need(sigs[pair][0][:Q4_START]==previous[0] and
             sigs[pair][1][:Q4_START]==previous[1],
             'APPENDING_Q4_REPAINTS_DISCOVERY_SIGNALS_'+pair)
    # Recreate V7 discovery outcome before opening Q4: refuse changed model.
    previous_dfs={p:df.iloc[:Q4_START].copy() for p,df in dfs.items()}
    previous_sigs={p:(x[0][:Q4_START],x[1][:Q4_START]) for p,x in sigs.items()}
    previous_uncapped=replay(previous_dfs,previous_sigs,risk,warmup,False,
                            end_idx=Q4_START)
    previous_funded=replay(previous_dfs,previous_sigs,risk,warmup,True,
                          end_idx=Q4_START)
    previous_score=grade(previous_uncapped['trades'])
    expected={
        'AverageStrategy':(46,27.3896,18.079923),
        'ReinforcedAverageStrategy':(29,76.9163,19.050573)
    }
    exp_trades,exp_bps,exp_wallet=expected[args.name]
    need(previous_score['trades']==exp_trades and
         abs(previous_score['mean_net_bps']-exp_bps)<0.0002 and
         abs(previous_funded['ending_realized_wallet_usdc']-exp_wallet)<0.00002,
         'V7_DISCOVERY_BASELINE_CHANGED')
    # Fresh hypothetical portfolio, no pre-Q4 positions, starts after the
    # first Q4 bar closes. Q4 candles are never used to choose strategies.
    theoretical=replay(dfs,sigs,risk,warmup,False,start_idx=Q4_FIRST_ENTRY)
    funded=replay(dfs,sigs,risk,warmup,True,start_idx=Q4_FIRST_ENTRY)
    # Independently recalculate the original indicator+entry signal on only
    # data that would have been closed when each simulated entry was placed.
    # All executed Q4 entries are checked, not just 30 arbitrary spotchecks.
    audited=set()
    for t in theoretical['trades']:
        audited.add((t['pair'],t['entry_idx']))
    for t in funded['trades']:
        audited.add((t['pair'],t['entry_idx']))
    # Include end-of-period open positions in entry causality audits.
    for run in (theoretical,funded):
        mark=run['open_at_boundary']
        if mark:
            audited.add((mark['pair'],mark['entry_idx']))
    for pair,idx in sorted(audited):
        need(idx>=Q4_FIRST_ENTRY, 'PRE_VALIDATION_ENTRY')
        prefix=signals(obj,modern,dfs[pair].iloc[:idx].copy(),pair,warmup)
        need(bool(prefix[0][idx-1]) and bool(sigs[pair][0][idx-1]),
             'ENTRY_LOOKAHEAD_MISMATCH_'+pair+'_'+str(idx))
    baseline=causal_spotcheck(obj,modern,dfs,sigs,warmup)
    q4_score=grade(theoretical['trades'])
    # Unrealized open P&L must be included to avoid inflated ending equity.
    open_mtm=(funded['open_at_boundary'] or {}).get('mark_to_market_usdc',0.0)
    liquidated_equity=round(funded['ending_realized_wallet_usdc']+open_mtm,6)
    result={'strategy':args.name,'status':'LOCKED_Q4_2025_PROXY',
            'discovery_reproduced':True,
            'discovery_mean_net_bps':previous_score['mean_net_bps'],
            'validation':q4_score,
            'theoretical':{k:v for k,v in theoretical.items() if k!='trades'},
            'funded_16_82_usdc':{**{k:v for k,v in funded.items() if k!='trades'},
                                'closed_trades':len(funded['trades']),
                                'estimated_ending_equity_with_open_mark':liquidated_equity,
                                'estimated_equity_pnl_usdc':round(liquidated_equity-START_CAPITAL,6)},
            'entry_prefix_audits':len(audited),
            'full_discovery_append_stability':'PASS',
            'supplemental_causal_spotchecks':baseline,
            'lookahead_fully_excluded':False,
            'sample':'2025-10-01_TO_2026-01-01_EXCLUSIVE',
            'signal_source':'PINNED_FREQTRADE_STRATEGY',
            'execution':'CUSTOM_4H_OHLC_PROXY_NOT_EXACT_FREQTRADE',
            'validation_previously_untested_for_these_candidates':True,
            'fee_per_side':FEE_SIDE,'other_cost_per_side':OTHER_SIDE,
            'live_permission':False}
    print('KIDSBOT_V8_RESULT='+json.dumps(result,sort_keys=True),flush=True)


if __name__=='__main__':
    try:
        main()
    except Exception as e:
        print('V7_WORKER_ERROR='+type(e).__name__+': '+str(e)[:250],file=sys.stderr,flush=True)
        sys.exit(2)
