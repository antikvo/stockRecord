#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""陈学长推荐日报 —— 每日归档到 GitHub（生成记录 → commit → push）。

用法:
    python3 archive_daily.py                 # 归档今天的 v2 报告并推送
    python3 archive_daily.py --date 20260922 # 指定日期
    python3 archive_daily.py --no-push       # 只生成 + commit，不推送
    python3 archive_daily.py --encrypt       # 同时生成加密版（原"次日公布口令"方案）
    python3 archive_daily.py --backfill 20260918 20260922   # 批量补历史

设计说明（2026-09-22）:
    - 默认输出**明文** records/YYYY/MM/DD.md（按用户 2026-09-22 指令：先放开加密）
    - --encrypt 时额外调用 encrypt_daily.py 生成 encrypted/YYYY/MM/DD.md，
      保留"当日只公开密文、次日公布口令"的可验证性设计
    - 幂等：记录内容不变则不产生提交；重复触发安全
    - 找不到当日报告时静默退出 0（由 LaunchAgent 多次触发等待收盘扫描完成）
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import warnings
from datetime import datetime, timedelta

# macOS 系统 Python 的 LibreSSL 会触发 urllib3 的 NotOpenSSLWarning（无害）。
# 压掉它，保证 launchd 的 stderr 为空 —— 这样真出问题时一眼能看见。
warnings.filterwarnings('ignore', message=r'.*LibreSSL.*')
warnings.filterwarnings('ignore', message=r'.*NotOpenSSL.*')

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_DIR = os.path.join(ROOT, '..', 'strategy_v2', 'reports')
ENV_DIR = CSV_DIR
LOG_DIR = os.path.join(ROOT, '.logs')

PLAN_COLS = ['低吸位', '追涨位', '压力位', '止损位', '竞价门槛万', '竞价强势万']


def log(msg: str):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, 'archive.log'), 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


def git(*args, check=True):
    """执行 git 命令；check=False 时返回 CompletedProcess 供调用方判断。"""
    return subprocess.run(['git', *args], cwd=ROOT, capture_output=True,
                          text=True, check=check)


def push_pending() -> bool:
    """推送所有未推送的提交（含此前失败/跳过推送遗留的）。返回是否成功。"""
    up = git('rev-parse', '--abbrev-ref', 'HEAD@{upstream}', check=False)
    ref = up.stdout.strip() if up.returncode == 0 else 'origin/main'
    ahead = git('log', '--oneline', f'{ref}..HEAD', check=False).stdout.strip()
    if not ahead:
        return True
    p = git('push', 'origin', 'HEAD', check=False)
    if p.returncode != 0:
        log(f'❌ 推送失败：{p.stderr.strip()[:300]}')
        return False
    log(f'✅ 已推送 {len(ahead.splitlines())} 个提交到 origin')
    return True


def fnum(v):
    try:
        if v is None or pd.isna(v):
            return ''
        return f'{float(v):g}'
    except Exception:
        return ''


DB_PATH = os.path.join(ROOT, '..', 'strategy_v2', 'data', 'v2.db')
TRACK_DAYS = 5          # 推荐后跟踪的交易日数（T+5 后冻结，历史不再变动）


def _connect():
    import sqlite3
    return sqlite3.connect(DB_PATH)


def market_trade_days() -> list:
    """库内全部交易日（升序）—— 与 v2 扫描同一口径的事实交易日历。"""
    with _connect() as con:
        return [d for (d,) in con.execute(
            "SELECT DISTINCT date FROM bars ORDER BY date")]


def read_bars(codes: list) -> dict:
    """读取指定股票的日线：{code: {date: {open, close, high, low}}}。"""
    out = {}
    if not codes:
        return out
    marks = ','.join('?' * len(codes))
    with _connect() as con:
        for code, d, o, c, h, lo in con.execute(
                f"SELECT symbol, date, open, close, high, low FROM bars "
                f"WHERE symbol IN ({marks})", list(codes)):
            out.setdefault(code, {})[d] = {'open': o, 'close': c,
                                           'high': h, 'low': lo}
    return out


def refresh_tracked(codes: list, lookback_days: int = 45) -> None:
    """尽最大努力把被跟踪个股的K线补到最新（失败不影响归档）。"""
    if not codes:
        return
    try:
        sys.path.insert(0, os.path.join(ROOT, '..', 'strategy_v2', 'src'))
        from data import DataStore, backfill_today_from_tencent
        ds = DataStore()
        for c in codes:
            try:
                ds.fetch_recent(c, lookback_days=lookback_days)
            except Exception:
                pass
        n = backfill_today_from_tencent(sorted(codes))
        log(f'K线刷新：{len(codes)} 只被跟踪个股，补当日 {n} 只')
    except Exception as e:                                    # noqa: BLE001
        log(f'⚠️ 被跟踪个股K线刷新失败（归档继续）：{e}')


def window_contiguous(dates_present: list, tdays: list) -> bool:
    """判断该股在窗口内的K线于全局交易历中是否逐日连续（无缺口）。

    缺口会让「多日累计涨跌幅」被当成单日、成交价也会错位，
    这类样本的涨幅/收益不可信 —— 不计入战绩汇总，并单独标注数量。
    """
    if len(dates_present) < 2:
        return True
    pos = {d: i for i, d in enumerate(tdays)}
    idxs = [pos[d] for d in dates_present if d in pos]
    return all(b - a == 1 for a, b in zip(idxs, idxs[1:]))


def perf_for(date: str, rows: list, bars: dict, trade_days: list) -> list:
    """计算某日推荐股票自推荐日起的逐日表现。

    按交易日推进：推荐日记为第 1 日。
      推荐日收盘 = 第1日收盘   ← 推荐口径
      次日开盘   = 第2日开盘   ← 策略实际可买到的价格
      第三日/第四日/第五日 = 第3/4/5日收盘
      最新收盘   = 窗口内最后一根可用收盘（最多到第 6 日 = T+5）
    返回 [{'code','name','t0','t1_open','t2','t3','t4','last_date','last',
            'ret_rec','ret_open'}, ...]
    """
    if not rows:
        return []
    # 库内日期为 ISO（YYYY-MM-DD），归档脚本用 YYYYMMDD，这里统一转换
    d_iso = f'{date[:4]}-{date[4:6]}-{date[6:8]}'
    if d_iso not in trade_days:
        return []
    i = trade_days.index(d_iso)
    w = trade_days[i:i + 1 + TRACK_DAYS]        # 第1日 .. 第6日（T+5）
    out = []
    for _row in rows:
        # rows 既支持 dict（新）也兼容 (code, name) 元组（旧调用）
        code = _row['code'] if isinstance(_row, dict) else _row[0]
        name = _row['name'] if isinstance(_row, dict) else _row[1]
        b = bars.get(code) or {}

        def close_of(k):
            d = w[k] if k < len(w) else None
            v = b.get(d) if d else None
            return float(v['close']) if v and v.get('close') else None

        def open_of(k):
            d = w[k] if k < len(w) else None
            v = b.get(d) if d else None
            return float(v['open']) if v and v.get('open') else None

        t0 = close_of(0)
        if t0 is None:
            continue
        t1_open = open_of(1)
        last_date, last = None, None
        for k in range(len(w)):
            c = close_of(k)
            if c is not None:
                last_date, last = w[k], c
        if last is None:
            continue
        present = [d for d in w if d in b]
        out.append({
            'code': code, 'name': name,
            't0': t0, 't1_open': t1_open,
            't2': close_of(2), 't3': close_of(3), 't4': close_of(4),
            'last_date': last_date, 'last': last,
            'ret_rec': (last / t0 - 1) * 100,
            'ret_open': ((last / t1_open - 1) * 100) if t1_open else None,
            'data_ok': window_contiguous(present, trade_days),
        })
    return out


def _pct(v):
    return '—' if v is None else f'{v:+.2f}%'


def _num(v):
    return '—' if v is None else f'{v:g}'


PERF_HEADER = ('| 代码 | 名称 | 推荐日收盘 | 次日开盘 | 第三日 | 第四日 '
               '| 第五日 | 最新收盘 | 自推荐日 | 自次日开盘 | 数据截至 |')
PERF_SEP = '|' + '---|' * 11


# ---- 模拟交易参数：直接读 trade_advisor 的配置，保持与实盘一致 ----
def load_exit_rules() -> dict:
    rules = {'hold_days_exit': 5, 'take_profit_pct': 6.0}
    try:
        import yaml
        p = os.path.join(ROOT, '..', 'trade_advisor', 'config.yaml')
        cfg = yaml.safe_load(open(p, encoding='utf-8'))['portfolio']
        rules['hold_days_exit'] = int(cfg.get('hold_days_exit', 5))
        rules['take_profit_pct'] = float(cfg.get('take_profit_pct', 6.0))
    except Exception as e:                                    # noqa: BLE001
        log(f'⚠️ 读取 trade_advisor 离场参数失败，用默认值：{e}')
    return rules


def simulate_trade(date: str, row: dict, bars: dict, trade_days: list,
                   rules: dict) -> dict:
    """模拟「次日开盘买入 → 逐日判定卖出」。

    规则照抄 trade_advisor/src/advisor.py 的 opencheck：
      1. 买入日盘中低点 ≤ 止损位 → 撤单（不成交）
      2. 买入后每个交易日按优先级判定：破止损 > 满 N 日了结 > 触压力+浮盈≥6% 止盈
      3. 买入当日不参与评估（advisor.py:511 的「当日新仓」修复）

    行情只有日线，判定按「最差情况」取价（保守，不美化结果）：
      止损 → 开盘已破则按开盘价，否则按止损价
      止盈 → 开盘已过压力则按开盘价，否则按压力价
    返回 {status, buy_date, buy_price, sell_date, sell_price, held_days,
          reason, ret_pct, trace:[...]}
    """
    if not row or date not in [d.replace('-', '') for d in trade_days]:
        return {}
    d_iso = f'{date[:4]}-{date[4:6]}-{date[6:8]}'
    i = trade_days.index(d_iso)
    b = bars.get(row['code']) or {}
    stop, press = row.get('stop'), row.get('press')
    hold_n = int(rules['hold_days_exit'])
    tp = float(rules['take_profit_pct'])

    # 买入日 = 次日（第 2 日）
    if i + 1 >= len(trade_days):
        return {'status': '待买入', 'reason': '次日行情尚未产生'}
    buy_d = trade_days[i + 1]
    bd = b.get(buy_d)
    if not bd or not bd.get('open'):
        return {'status': '无行情', 'reason': f'{buy_d} 无K线'}
    buy_price = float(bd['open'])
    # 买入日盘中破止损 → 撤单
    if (stop is not None and bd.get('low') is not None
            and float(bd['low']) <= float(stop)):
        return {'status': '撤单', 'buy_date': buy_d, 'buy_price': buy_price,
                'reason': f"买入日盘中低点{float(bd['low']):g}破止损{stop:g}，放弃",
                'ret_pct': 0.0, 'trace': []}

    trace, sell = [], None
    last_eval = min(i + 1 + hold_n, len(trade_days) - 1)
    # 窗口内该股K线是否连续 —— 有缺口则成交价/收益不可信，不计入战绩汇总
    win_dates = [trade_days[k] for k in range(i + 1, last_eval + 1)
                 if trade_days[k] in b]
    data_ok = window_contiguous(win_dates, trade_days)
    for k in range(i + 2, last_eval + 1):          # 买入日次日起评估
        d = trade_days[k]
        v = b.get(d)
        if not v:
            continue
        o, c, h, lo = (v.get('open'), v.get('close'), v.get('high'),
                       v.get('low'))
        held = k - (i + 1)                          # 已持有交易日数（买入日=0）
        rec = {'date': d, 'open': o, 'low': lo, 'high': h, 'close': c,
               'held': held}
        if stop is not None and lo is not None and float(lo) <= float(stop):
            px = float(o) if (o and float(o) <= float(stop)) else float(stop)
            sell = (d, px, f'破止损{stop:g}', held)
        elif held >= hold_n:
            sell = (d, float(c), f'持仓满{hold_n}个交易日了结', held)
        elif (press is not None and h is not None and float(h) >= float(press)
              and (float(press) / buy_price - 1) * 100 >= tp):
            px = float(o) if (o and float(o) >= float(press)) else float(press)
            sell = (d, px, f'触压力{press:g}且浮盈≥{tp:g}%，止盈', held)
        rec['note'] = sell[2] if sell else '持有'
        trace.append(rec)
        if sell:
            break

    if not sell:
        return {'status': '持有中', 'buy_date': buy_d, 'buy_price': buy_price,
                'reason': f'未触发离场（跟踪 {hold_n} 个交易日）',
                'ret_pct': None, 'trace': trace, 'data_ok': data_ok}
    d, px, why, held = sell
    return {'status': '已了结', 'buy_date': buy_d, 'buy_price': buy_price,
            'sell_date': d, 'sell_price': px, 'held_days': held, 'reason': why,
            'ret_pct': (px / buy_price - 1) * 100, 'trace': trace,
            'data_ok': data_ok}


TRADE_HEADER = ('| 代码 | 名称 | 买入日 | 买入价 | 卖出日 | 卖出价 '
                '| 持有(交易日) | 卖出原因 | 收益率 |')
TRADE_SEP = '|' + '---|' * 9


def trade_lines(date: str, rows: list, bars: dict, trade_days: list,
                rules: dict,
                title: str = '## 模拟交易（次日开盘买入 → 逐日判定卖出）') -> list:
    """生成「模拟交易」段落：总结表 + 逐日判定明细（可折叠）。"""
    sims = [(r, simulate_trade(date, r, bars, trade_days, rules)) for r in rows]
    sims = [(r, s) for r, s in sims if s]
    if not sims:
        return []
    L = [title, '',
         f'> 规则取自 `trade_advisor/config.yaml`：**破止损 → 卖出**；'
         f'**满 {rules["hold_days_exit"]} 个交易日 → 了结**；'
         f'**触及压力位且浮盈 ≥{rules["take_profit_pct"]:g}% → 止盈**。',
         '> 判定优先级：破止损 > 满N日了结 > 止盈（与实盘 `opencheck` 一致）；'
         '买入当日不参与评估。',
         '> 只有日线数据，触发时按**最差情况**取价（开盘已破按开盘价，否则按触发价），'
         '不美化结果；买入日盘中破止损则撤单不成交。', '',
         TRADE_HEADER, TRADE_SEP]
    for r, s in sims:
        st = s['status']
        if st == '已了结':
            L.append(f"| {r['code']} | {r['name']} | {s['buy_date']} "
                     f"| {s['buy_price']:g} | {s['sell_date']} | {s['sell_price']:g} "
                     f"| {s['held_days']} | {s['reason']} "
                     f"| {_pct(s['ret_pct'])} |")
        else:
            L.append(f"| {r['code']} | {r['name']} | {s.get('buy_date', '—')} "
                     f"| {_num(s.get('buy_price'))} | — | — | — "
                     f"| {s.get('reason', st)} | — |")
    L.append('')

    det = [(r, s) for r, s in sims if s.get('trace')]
    if det:
        L += ['<details>', '<summary>逐日判定明细</summary>', '',
              '| 日期 | 代码 | 名称 | 开盘 | 最低 | 最高 | 收盘 '
              '| 已持有 | 判定 |', '|' + '---|' * 9]
        for r, s in det:
            for t in s['trace']:
                L.append(f"| {t['date']} | {r['code']} | {r['name']} "
                         f"| {_num(t['open'])} | {_num(t['low'])} "
                         f"| {_num(t['high'])} | {_num(t['close'])} "
                         f"| {t['held']} | {t['note']} |")
        L += ['', '</details>', '']
    return L


def perf_row_lines(perf: list) -> list:
    return [f"| {p['code']} | {p['name']} | {_num(p['t0'])} "
            f"| {_num(p['t1_open'])} | {_num(p['t2'])} | {_num(p['t3'])} "
            f"| {_num(p['t4'])} | {_num(p['last'])} "
            f"| {_pct(p['ret_rec'])} | {_pct(p['ret_open'])} "
            f"| {p['last_date']} |" for p in perf]


def perf_lines(date: str, rows: list, bars: dict, trade_days: list,
               title: str = '## 后续表现（自推荐日起，逐日推进）') -> list:
    """生成单条记录的「后续表现」markdown。"""
    perf = perf_for(date, rows, bars, trade_days)
    if not perf:
        return []
    last = max(p['last_date'] for p in perf)
    L = [title, '',
         '> 按交易日推进：推荐日记为**第 1 日**，次日开盘 = 第 2 日开盘'
         '（策略实际可买到的价格）。',
         f'> 数据截至 `{last}`；第 6 日（T+5）后不再变动。', '',
         PERF_HEADER, PERF_SEP]
    L += perf_row_lines(perf)
    L.append('')
    return L


def build_record(date: str, df: pd.DataFrame, env: dict | None,
                 perf_md: list | None = None,
                 trade_md: list | None = None) -> str:
    """生成明文 markdown 记录（格式对齐历史 records/ 文件）。"""
    date_iso = f'{date[:4]}-{date[4:6]}-{date[6:8]}'
    rec = df[df.get('推荐') == '推荐'].copy() if '推荐' in df.columns else df.iloc[0:0]
    if not rec.empty and '总分' in rec.columns:
        rec = rec.sort_values('总分', ascending=False)

    L = [f'# 陈学长推荐（{date_iso}）', '']
    L.append(f'> 日期：`{date_iso}`　推荐种数：**{len(rec)}**　候选池：{len(df)}')
    if env:
        L.append(f"> 环境：{env.get('label', '?')}（分 {env.get('score', '?')}）"
                 f"｜涨 {env.get('up', '-')} / 跌 {env.get('down', '-')}"
                 f"｜涨停 {env.get('limit_up', '-')}"
                 f"｜炸板率 {env.get('zha_ban_rate', '-')}"
                 f"｜大盘 {env.get('index_pct', '-')}%")
        if env.get('quality') and env['quality'] != '正常':
            L.append(f"> 备注：{env['quality']}")
        if env.get('breaker'):
            L.append('> ⚠️ 熔断/谨慎提示生效')
        dd = env.get('data_date') or ''
        dmin = env.get('data_date_min') or dd
        if env.get('stale'):
            L.append(f'> 🔴 数据截至 `{dd}`，当日K线补齐失败（滞后），信号仅供参考')
        elif dmin and dd and dmin < dd:
            L.append(f'> 🟠 部分候选K线最早仅到 `{dmin}`（市场数据日 `{dd}`），'
                     f'相关个股已剔除推荐')
        if env.get('unusable_count'):
            codes = '、'.join(env.get('unusable_codes') or [])
            L.append(f'> 🟠 K线滞后/缺口剔除 {env["unusable_count"]} 只：{codes}')
    L += ['', '## 推荐股票', '']

    if rec.empty:
        L += ['_今日无推荐（介入信号为空，仅观察池）。_', '']
    else:
        # (显示标签, CSV 列名) —— 注意二者不同名，勿混用
        tcols = [('代码', '代码'), ('名称', '名称'), ('总分', '总分'),
                 ('阶段', '阶段'), ('行业', '行业'),
                 ('流通市值(亿)', '流通市值亿')]
        tcols += [(c, c) for c in PLAN_COLS if c in df.columns]
        L += ['| ' + ' | '.join(lbl for lbl, _ in tcols) + ' |',
              '|' + '---|' * len(tcols)]
        for _, r in rec.iterrows():
            cells = []
            for _, col in tcols:
                v = r.get(col)
                if col in ('总分', '流通市值亿') or col in PLAN_COLS:
                    cells.append(fnum(v))
                else:
                    cells.append('' if v is None or pd.isna(v) else str(v))
            L.append('| ' + ' | '.join(cells) + ' |')
        L.append('')
        L += ['## 买卖参考', '']
        for _, r in rec.iterrows():
            name, code = str(r.get('名称', '')), str(r.get('代码', ''))
            buy_ref = str(r.get('买入参考') or '').strip()
            if not buy_ref:
                key, press = fnum(r.get('低吸位')), fnum(r.get('追涨位'))
                buy_ref = f'回踩{key}不破可低吸；放量突破{press}可追'
            L.append(f'- **{name}**（{code}）：{buy_ref}')
            plan = str(r.get('次日计划') or '').strip()
            if plan:
                L.append(f'    - 次日：{plan}')
        L.append('')

    if perf_md:
        L += perf_md
    if trade_md:
        L += trade_md
    L += ['---', '',
          f'> 完整数据：`reports/v2_candidates_{date}.csv`',
          '> ⚠️ 学习用途，非投资建议。', '']
    return '\n'.join(L)


def aggregate_stats() -> dict:
    """全量模拟战绩（只统计已了结且K线连续、数字可信的样本）。"""
    tdays = market_trade_days()
    rules = load_exit_rules()
    dates, codes = [], set()
    for d_iso in tdays:
        d = d_iso.replace('-', '')
        p = os.path.join(ROOT, 'records', d[:4], d[4:6], f'{d[6:8]}.md')
        if not os.path.exists(p):
            continue
        body = open(p, encoding='utf-8').read()
        sec = body.split('## 推荐股票')[-1].split('## 买卖参考')[0]
        rows = parse_rec_table(sec)
        if rows:
            dates.append(d)
            codes |= {r['code'] for r in rows}
    bars = read_bars(sorted(codes))
    rets, cancelled, unverified = [], 0, 0
    for d in dates:
        p = os.path.join(ROOT, 'records', d[:4], d[4:6], f'{d[6:8]}.md')
        body = open(p, encoding='utf-8').read()
        sec = body.split('## 推荐股票')[-1].split('## 买卖参考')[0]
        for r in parse_rec_table(sec):
            s = simulate_trade(d, r, bars, tdays, rules)
            if not s:
                continue
            if s['status'] == '撤单':
                cancelled += 1
            elif s['status'] == '已了结' and s.get('ret_pct') is not None:
                if s.get('data_ok') is False:
                    unverified += 1
                    continue
                rets.append({'code': r['code'], 'name': r['name'],
                             'ret': s['ret_pct'], 'reason': s['reason'],
                             'date': d})
    if not rets:
        return {'n': 0}
    win = [x for x in rets if x['ret'] > 0]
    loss = [x for x in rets if x['ret'] <= 0]
    srt = sorted(x['ret'] for x in rets)
    mid = srt[len(srt) // 2] if len(srt) % 2 else \
        (srt[len(srt) // 2 - 1] + srt[len(srt) // 2]) / 2
    avg_w = sum(x['ret'] for x in win) / len(win) if win else 0.0
    avg_l = sum(x['ret'] for x in loss) / len(loss) if loss else 0.0
    return {'n': len(rets), 'win': len(win),
            'win_rate': len(win) / len(rets) * 100,
            'avg': sum(x['ret'] for x in rets) / len(rets),
            'median': mid,
            'best': max(rets, key=lambda x: x['ret']),
            'worst': min(rets, key=lambda x: x['ret']),
            'cancelled': cancelled, 'buys': len(rets) + cancelled,
            'unverified': unverified, 'avg_win': avg_w, 'avg_loss': avg_l,
            'pl_ratio': (abs(avg_w / avg_l) if avg_l else 0.0), 'rets': rets}


def rebuild_stats() -> None:
    """重建 README 的 <!-- STATS:BEGIN --> ... <!-- STATS:END --> 战绩总览。

    刻意把「当前是负期望」写明，并把口径/取价假设一起公开，
    使任何人 clone 后能按同样规则复核。
    """
    import re
    readme = os.path.join(ROOT, 'README.md')
    if not os.path.exists(readme):
        return
    txt = open(readme, encoding='utf-8').read()
    if '<!-- STATS:BEGIN -->' not in txt or '<!-- STATS:END -->' not in txt:
        return
    a = aggregate_stats()
    if not a.get('n'):
        return
    # 按月分解
    mon = {}
    for x in a['rets']:
        mon.setdefault(x['date'][:6], []).append(x['ret'])
    L = ['<!-- STATS:BEGIN -->', '',
         '## 累计模拟战绩（全量，可复核）', '',
         '> 本节由 `scripts/archive_daily.py` 自动重建。**当前为负期望**，'
         '如实展示，不做筛选。',
         '> 口径：推荐日收盘出信号 → **次日开盘买入** → 逐日判定离场'
         '（破止损 > 满5交易日了结 > 触压力位且浮盈≥6%止盈）。',
         '> **未计**手续费、印花税、滑点；触发时按最坏价格成交'
         '（开盘已破按开盘价，否则按触发价）；买入日盘中破止损则撤单不成交；',
         '> 跟踪窗口内该股K线有缺口的样本已剔除（不可信），剔除 '
         f'{a.get("unverified", 0)} 笔。', '',
         '| 指标 | 数值 |', '|---|---|',
         f'| 计入战绩样本 | **{a["n"]}** 笔（另有撤单 {a["cancelled"]} 笔） |',
         f'| 胜率 | **{a["win_rate"]:.1f}%**（{a["win"]} 胜 / {a["n"] - a["win"]} 负） |',
         f'| 平均收益 | **{a["avg"]:+.2f}%** |',
         f'| 中位数收益 | {a["median"]:+.2f}% |',
         f'| 平均盈利 / 平均亏损 | {a["avg_win"]:+.2f}% / {a["avg_loss"]:+.2f}% |',
         f'| 盈亏比 | {a["pl_ratio"]:.2f} |',
         f'| 单笔最好 | {a["best"]["name"]} {a["best"]["ret"]:+.2f}% |',
         f'| 单笔最差 | {a["worst"]["name"]} {a["worst"]["ret"]:+.2f}% |', '']
    if mon:
        L += ['按月：', '', '| 月份 | 笔数 | 胜率 | 平均收益 |', '|---|---|---|---|']
        for m in sorted(mon, reverse=True):
            v = mon[m]
            w = len([x for x in v if x > 0])
            L.append(f'| {m[:4]}-{m[4:]} | {len(v)} | '
                     f'{w / len(v) * 100:.0f}% | {sum(v) / len(v):+.2f}% |')
        L.append('')
    L.append('<!-- STATS:END -->')
    new = re.sub(r'<!-- STATS:BEGIN -->.*?<!-- STATS:END -->',
                 '\n'.join(L), txt, flags=re.S)
    if new != txt:
        open(readme, 'w', encoding='utf-8').write(new)
        log(f'README 战绩总览已重建（{a["n"]} 笔样本，平均 {a["avg"]:+.2f}%）')


def rebuild_index() -> None:
    """重建 README 里 <!-- INDEX:BEGIN --> ... <!-- INDEX:END --> 之间的记录索引。

    索引以前是手工维护的，很容易与 records/ 实际内容脱节（已有过一次）。
    这里每次归档后按实际文件重新生成。
    """
    import re
    readme = os.path.join(ROOT, 'README.md')
    if not os.path.exists(readme):
        return
    txt = open(readme, encoding='utf-8').read()
    if '<!-- INDEX:BEGIN -->' not in txt or '<!-- INDEX:END -->' not in txt:
        return

    recs = []
    for dirpath, _, files in os.walk(os.path.join(ROOT, 'records')):
        for fn in files:
            if not fn.endswith('.md'):
                continue
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, ROOT).replace(os.sep, '/')
            parts = rel.split('/')
            if len(parts) != 4:
                continue
            y, m, d = parts[1], parts[2], parts[3][:2]
            body = open(p, encoding='utf-8').read()
            n = re.search(r'推荐种数：\*\*(\d+)\*\*', body)
            lab = re.search(r'> 环境：([^（(]+)', body)
            note = re.search(r'> 备注：(.+)', body)
            env = lab.group(1).strip() if lab else '?'
            if note:
                q = note.group(1).strip()
                if q.startswith(env):
                    q = q[len(env):].lstrip('，,、+·- ')
                if q and q != '正常':
                    env += '，' + q
            recs.append((f'{y}-{m}-{d}', rel, int(n.group(1)) if n else 0, env))
    recs.sort(key=lambda x: x[0], reverse=True)

    out = ['<!-- INDEX:BEGIN -->', '',
           '> 数据来源：陈学长选股策略 v2 的每日推荐报告'
           '（`~/stock/strategy_v2/reports/v2_candidates_*.csv`）。'
           '本索引由 `scripts/archive_daily.py` 自动重建。', '']
    cur = None
    for date, rel, n, env in recs:
        y, m, _ = date.split('-')
        if cur != (y, m):
            if cur is not None:
                out.append('')
            out += [f'### {y} 年 {m} 月', '']
            cur = (y, m)
        out.append(f'- [{date[5:]}]({rel}) — 推荐 {n} 只（环境：{env}）')
    out += ['', '<!-- INDEX:END -->']

    new = re.sub(r'<!-- INDEX:BEGIN -->.*?<!-- INDEX:END -->',
                 '\n'.join(out), txt, flags=re.S)
    if new != txt:
        open(readme, 'w', encoding='utf-8').write(new)
        log(f'README 索引已重建（{len(recs)} 条记录）')


def parse_rec_table(sec: str) -> list:
    """解析记录里的「推荐股票」表格 → [{'code','name','stop','press'}]。

    按表头定位列，避免列序变动导致取错（止损位/压力位是模拟交易必需的）。
    """
    import re
    lines = [l for l in sec.splitlines() if l.strip().startswith('|')]
    if len(lines) < 2:
        return []
    hdr = [c.strip() for c in lines[0].strip().strip('|').split('|')]
    idx = {c: i for i, c in enumerate(hdr)}
    if '代码' not in idx or '名称' not in idx:
        return []
    out = []
    for l in lines[2:]:
        cells = [c.strip() for c in l.strip().strip('|').split('|')]
        if len(cells) < len(hdr):
            continue
        code = cells[idx['代码']]
        if not re.fullmatch(r'\d{6}', code):
            continue

        def num(col):
            v = cells[idx[col]] if col in idx else ''
            try:
                return float(v)
            except Exception:
                return None

        out.append({'code': code, 'name': cells[idx['名称']],
                    'stop': num('止损位'), 'press': num('压力位')})
    return out


def rebuild_perf(days: int = 5) -> None:
    """重建 README 里 <!-- PERF:BEGIN --> ... <!-- PERF:END --> 的「近 N 日推荐表现」。

    直接解析 records/ 里的记录文件取推荐名单（不依赖 CSV 是否还在），
    再用库内K线从推荐日算起：逐日涨幅 + 模拟交易（次日买入、逐日判定卖出）。
    """
    import re
    readme = os.path.join(ROOT, 'README.md')
    if not os.path.exists(readme):
        return
    txt = open(readme, encoding='utf-8').read()
    if '<!-- PERF:BEGIN -->' not in txt or '<!-- PERF:END -->' not in txt:
        return

    trade_days = market_trade_days()
    recent = trade_days[-days:] if len(trade_days) >= days else trade_days
    all_codes, parsed = set(), []
    for d_iso in reversed(recent):                       # 新的在前
        date = d_iso.replace('-', '')
        p = os.path.join(ROOT, 'records', date[:4], date[4:6], f'{date[6:8]}.md')
        if not os.path.exists(p):
            continue
        body = open(p, encoding='utf-8').read()
        sec = body.split('## 推荐股票')[-1].split('## 买卖参考')[0]
        rows = parse_rec_table(sec)
        if not rows:
            continue
        parsed.append((date, rows))
        all_codes |= {r['code'] for r in rows}

    if not parsed:
        return
    bars = read_bars(sorted(all_codes))
    rules = load_exit_rules()

    L = ['<!-- PERF:BEGIN -->', '',
         f'> 近 {len(recent)} 个交易日（`{recent[0]}` ~ `{recent[-1]}`）的推荐表现。',
         '> 逐日推进：推荐日记为**第 1 日**，次日开盘 = 第 2 日开盘'
         '（策略实际可买到的价格）。',
         f'> 模拟交易规则取自 `trade_advisor/config.yaml`：破止损 → 卖出；'
         f'满 {rules["hold_days_exit"]} 个交易日 → 了结；'
         f'触及压力位且浮盈 ≥{rules["take_profit_pct"]:g}% → 止盈。', '']
    for date, rows in parsed:
        iso = f'{date[:4]}-{date[4:6]}-{date[6:8]}'
        perf = perf_for(date, rows, bars, trade_days)
        if perf:
            L += [f'### {iso}（{len(perf)} 只）', '', PERF_HEADER, PERF_SEP]
            L += perf_row_lines(perf)
            L.append('')
        sims = [(r, simulate_trade(date, r, bars, trade_days, rules))
                for r in rows]
        sims = [(r, s) for r, s in sims if s]
        if sims:
            L += ['**模拟交易**（次日开盘买入 → 逐日判定卖出）', '',
                  TRADE_HEADER, TRADE_SEP]
            for r, s in sims:
                if s['status'] == '已了结':
                    L.append(
                        f"| {r['code']} | {r['name']} | {s['buy_date']} "
                        f"| {s['buy_price']:g} | {s['sell_date']} "
                        f"| {s['sell_price']:g} | {s['held_days']} "
                        f"| {s['reason']} | {_pct(s['ret_pct'])} |")
                else:
                    L.append(
                        f"| {r['code']} | {r['name']} | {s.get('buy_date', '—')} "
                        f"| {_num(s.get('buy_price'))} | — | — | — "
                        f"| {s.get('reason', s['status'])} | — |")
            L.append('')
    L.append('<!-- PERF:END -->')

    new = re.sub(r'<!-- PERF:BEGIN -->.*?<!-- PERF:END -->',
                 '\n'.join(L), txt, flags=re.S)
    if new != txt:
        open(readme, 'w', encoding='utf-8').write(new)
        log(f'README 近{len(recent)}日表现已重建（{len(parsed)} 个推荐日）')


def backfill_history() -> int:
    """给还没有表现段落的历史记录补上「历史表现（回填）」。

    老记录的 CSV 多半已不在，所以直接从记录文件本身解析推荐名单
    （含止损位/压力位）再用库内K线计算。这些日期的 T+5 早已走完、
    结果已冻结，回填一次就不会再变。已含段落的文件自动跳过（幂等）。
    """
    import re
    n = 0
    tdays = market_trade_days()
    rules = load_exit_rules()
    # 第一遍：找出待回填的记录（老记录对应的个股多半早已掉出候选池，
    # 库内没有后续K线，必须先补历史，否则会算成 +0.00% / 无K线）
    todo, codes = [], set()
    for dirpath, _, files in sorted(os.walk(os.path.join(ROOT, 'records'))):
        for fn in sorted(files):
            if not fn.endswith('.md'):
                continue
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, ROOT).replace(os.sep, '/')
            parts = rel.split('/')
            if len(parts) != 4:
                continue
            date = f'{parts[1]}{parts[2]}{fn[:2]}'
            body = open(p, encoding='utf-8').read()
            if '## 后续表现' in body or '## 历史表现' in body:
                continue
            sec = body.split('## 推荐股票')[-1].split('## 买卖参考')[0]
            rows = parse_rec_table(sec)
            if not rows:
                continue
            todo.append((p, date, rows, body))
            codes |= {r['code'] for r in rows}
    if not todo:
        return 0
    refresh_tracked(sorted(codes), lookback_days=90)

    # 第二遍：计算并写回
    for p, date, rows, body in todo:
        bars = read_bars([r['code'] for r in rows])
        try:
            add = ['## 历史表现（回填）', '',
                   '> 本段为**事后回填**（该记录发布时尚未实现跟踪），'
                   '数据源为库内日线，非当日原文；正文与提交历史均可核对。', '']
            add += perf_lines(date, rows, bars, tdays,
                              title='### 逐日涨幅（自推荐日起）')
            add += trade_lines(date, rows, bars, tdays, rules,
                               title='### 模拟交易（次日开盘买入 → 逐日判定卖出）')
            if len(add) <= 4:
                continue
            open(p, 'w', encoding='utf-8').write(
                body.rstrip() + '\n\n' + '\n'.join(add) + '\n')
            log(f'{date} 已回填历史表现（{len(rows)} 只）')
            n += 1
        except Exception as e:                                # noqa: BLE001
            log(f'{date} ⚠️ 回填失败：{e}')
    return n


def archive_one(date: str, do_push: bool, do_encrypt: bool,
                force: bool = False) -> str:
    """归档单个日期。返回 'ok' / 'skip' / 'nofile'。"""
    csv_path = os.path.join(CSV_DIR, f'v2_candidates_{date}.csv')
    env_path = os.path.join(ENV_DIR, f'environment_{date}.json')
    if not os.path.exists(csv_path):
        log(f'{date} 无报告（{os.path.basename(csv_path)} 不存在），跳过')
        return 'nofile'

    df = pd.read_csv(csv_path, dtype={'代码': str})
    env = None
    if os.path.exists(env_path):
        try:
            env = json.load(open(env_path, encoding='utf-8'))
        except Exception:
            env = None

    outdir = os.path.join(ROOT, 'records', date[:4], date[4:6])
    os.makedirs(outdir, exist_ok=True)
    outpath = os.path.join(outdir, f'{date[6:8]}.md')

    # 推荐股后续表现 + 模拟交易（自推荐日起算，T+5 冻结）
    perf_md, trade_md = [], []
    if '推荐' in df.columns and '代码' in df.columns:
        rec_df = df[df['推荐'] == '推荐']
        if len(rec_df):
            def _fnum(v):
                try:
                    return float(v)
                except Exception:
                    return None
            rows = [{'code': str(r['代码']).zfill(6),
                     'name': str(r.get('名称', '')),
                     'stop': _fnum(r.get('止损位')),
                     'press': _fnum(r.get('压力位'))}
                    for _, r in rec_df.iterrows()]
            bars = read_bars([r['code'] for r in rows])
            tdays = market_trade_days()
            try:
                perf_md = perf_lines(date, rows, bars, tdays)
                trade_md = trade_lines(date, rows, bars, tdays, load_exit_rules())
            except Exception as e:                            # noqa: BLE001
                log(f'{date} ⚠️ 表现/模拟交易计算失败（记录仍会归档）：{e}')
                perf_md, trade_md = [], []
    body = build_record(date, df, env, perf_md, trade_md)

    changed = True
    if os.path.exists(outpath) and not force:
        try:
            changed = open(outpath, encoding='utf-8').read() != body
        except Exception:
            changed = True

    if not changed:
        log(f'{date} 记录已是最新，无需变更')
    else:
        with open(outpath, 'w', encoding='utf-8') as f:
            f.write(body)
        n_rec = int((df.get('推荐') == '推荐').sum()) if '推荐' in df.columns else 0
        log(f'{date} 记录已生成：{os.path.relpath(outpath, ROOT)}'
            f'（推荐 {n_rec} 只）')

        if do_encrypt:
            r = subprocess.run([sys.executable,
                               os.path.join(ROOT, 'scripts', 'encrypt_daily.py'),
                               '--csv', csv_path, '--date', date,
                               '--env', env_path], cwd=ROOT)
            if r.returncode != 0:
                log(f'{date} ⚠️ 加密版生成失败（明文记录不受影响）')

        git('add', '-A', 'records', 'encrypted', '.gitignore')
        staged = git('diff', '--cached', '--name-only').stdout.strip()
        if staged:
            git('commit', '-m',
                f'记录: 陈学长推荐 {date[:4]}-{date[4:6]}-{date[6:8]}'
                f'（推荐 {n_rec} 只）')
            log(f'{date} 已提交：{staged.replace(chr(10), ", ")}')
            changed = True
        else:
            changed = False

    # 即使本次无变更，也要把此前遗留的未推送提交补推上去
    if do_push:
        push_pending()
    elif changed:
        log(f'{date} 已跳过推送（--no-push）')
    return 'ok' if changed else 'skip'


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=None, help='YYYYMMDD，默认今天')
    ap.add_argument('--backfill', nargs=2, metavar=('START', 'END'),
                    help='补历史区间 YYYYMMDD YYYYMMDD')
    ap.add_argument('--backfill-history', action='store_true',
                    help='给还没有表现段落的老记录补「历史表现（回填）」（幂等）')
    ap.add_argument('--image', action='store_true',
                    help='同时生成当日图片（public 合规版，不含个股）')
    ap.add_argument('--feishu-route', action='store_true',
                    help='按路由推送当日图片：群=完整版，私聊=隐藏版（每天只发一次）')
    ap.add_argument('--encrypt', action='store_true',
                    help='额外生成加密版（原方案：次日公布口令）')
    ap.add_argument('--no-push', action='store_true', help='只提交不推送')
    ap.add_argument('--catchup', type=int, default=10,
                    help='默认模式下回溯天数（补齐漏跑与未冻结的表现窗口，默认 10）')
    ap.add_argument('--force', action='store_true', help='覆盖已存在的记录')
    args = ap.parse_args()

    dates = []
    if args.backfill:
        d0 = datetime.strptime(args.backfill[0], '%Y%m%d')
        d1 = datetime.strptime(args.backfill[1], '%Y%m%d')
        while d0 <= d1:
            dates.append(d0.strftime('%Y%m%d'))
            d0 += timedelta(days=1)
    elif args.date:
        dates = [args.date]
    else:
        # 默认：今天 + 回溯 catchup 天。缺报告的日期自动跳过，因此可自愈
        # 「收盘扫描跑得晚 / 当天漏跑」的情况（幂等，重复触发无副作用）。
        today = datetime.now()
        dates = [(today - timedelta(days=i)).strftime('%Y%m%d')
                 for i in range(args.catchup + 1)]

    # 归档前先把「被跟踪的推荐股」K线补到最新（一次去重，失败不阻断归档）
    tracked = set()
    for d in dates:
        p = os.path.join(CSV_DIR, f'v2_candidates_{d}.csv')
        if not os.path.exists(p):
            continue
        try:
            t = pd.read_csv(p, dtype={'代码': str})
            if '推荐' in t.columns and '代码' in t.columns:
                tracked |= {str(c).zfill(6) for c in t.loc[t['推荐'] == '推荐', '代码']}
        except Exception:
            pass
    refresh_tracked(sorted(tracked))

    def _one(d):
        # 单个日期出错不应中断整轮归档
        try:
            return archive_one(d, not args.no_push, args.encrypt, args.force)
        except Exception as e:                                # noqa: BLE001
            log(f'{d} ❌ 归档失败：{e}')
            return 'error'

    results = [_one(d) for d in dates]
    rebuild_index()
    rebuild_perf()
    rebuild_stats()
    if args.backfill_history:
        log(f'历史回填：{backfill_history()} 条记录已补上表现段落')
    if args.image:
        try:
            import make_image
            for d in dates:
                make_image.one(d, 'public')
        except Exception as e:                                # noqa: BLE001
            log(f'⚠️ 出图失败（不影响归档）：{e}')
    if args.feishu_route:
        # 只推「今天」的图：群=完整版，私聊=隐藏版。
        # 幂等由 make_image 的 sent-* 标记保证（本任务每小时触发一次）。
        try:
            import make_image
            make_image.send_routed(datetime.now().strftime('%Y%m%d'))
        except Exception as e:                                # noqa: BLE001
            log(f'⚠️ 图片推送失败（不影响归档）：{e}')
    # 索引、脚本自身、以及回填产生的记录都要入库
    git('add', '-A', 'README.md', 'scripts', '.gitignore', 'records',
        'encrypted', check=False)
    if git('diff', '--cached', '--name-only', check=False).stdout.strip():
        git('commit', '-m', 'chore: 同步归档脚本与记录索引', check=False)
    if not args.no_push:
        push_pending()
    ok = results.count('ok')
    skip = results.count('skip')
    nofile = results.count('nofile')
    log(f'完成：归档 {ok} / 无变更 {skip} / 无报告 {nofile}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
