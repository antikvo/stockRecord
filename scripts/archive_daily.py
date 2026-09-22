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
from datetime import datetime, timedelta

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
    """读取指定股票的开收盘：{code: {date: (open, close, high)}}。"""
    out = {}
    if not codes:
        return out
    marks = ','.join('?' * len(codes))
    with _connect() as con:
        for code, d, o, c, h in con.execute(
                f"SELECT symbol, date, open, close, high FROM bars "
                f"WHERE symbol IN ({marks})", list(codes)):
            out.setdefault(code, {})[d] = (o, c, h)
    return out


def refresh_tracked(codes: list) -> None:
    """尽最大努力把被跟踪个股的K线补到最新（失败不影响归档）。"""
    if not codes:
        return
    try:
        sys.path.insert(0, os.path.join(ROOT, '..', 'strategy_v2', 'src'))
        from data import DataStore, backfill_today_from_tencent
        ds = DataStore()
        for c in codes:
            try:
                ds.fetch_recent(c)
            except Exception:
                pass
        n = backfill_today_from_tencent(sorted(codes))
        log(f'K线刷新：{len(codes)} 只被跟踪个股，补当日 {n} 只')
    except Exception as e:                                    # noqa: BLE001
        log(f'⚠️ 被跟踪个股K线刷新失败（归档继续）：{e}')


def perf_for(date: str, rows: list, bars: dict, trade_days: list) -> list:
    """计算某日推荐股票自推荐日起的表现。

    rows: [(code, name), ...]
    返回 [(code, name, base_close, next_open, last_date, last_close,
            ret_from_rec, ret_from_open, max_gain), ...]
    基准两个都给：推荐日收盘（用户口径）+ 次日开盘（策略实际可买到的价格）。
    """
    if not rows:
        return []
    # 库内日期为 ISO（YYYY-MM-DD），归档脚本用 YYYYMMDD，这里统一转换
    d_iso = f'{date[:4]}-{date[4:6]}-{date[6:8]}'
    if d_iso not in trade_days:
        return []
    i = trade_days.index(d_iso)
    window = trade_days[i:i + 1 + TRACK_DAYS]      # 推荐日 + 之后 5 个交易日
    nxt = trade_days[i + 1] if i + 1 < len(trade_days) else None
    out = []
    for code, name in rows:
        b = bars.get(code) or {}
        base = b.get(d_iso)
        if not base or not base[1]:
            continue
        base_close = float(base[1])
        next_open = float(b[nxt][0]) if nxt and b.get(nxt) and b[nxt][0] else None
        last_date, last_close, peak = None, None, base_close
        for d in window:
            v = b.get(d)
            if not v:
                continue
            last_date, last_close = d, float(v[1])
            peak = max(peak, float(v[2]) if v[2] else float(v[1]))
        if last_close is None:
            continue
        out.append((code, name, base_close, next_open, last_date, last_close,
                    (last_close / base_close - 1) * 100,
                    ((last_close / next_open - 1) * 100) if next_open else None,
                    (peak / base_close - 1) * 100))
    return out


def _pct(v):
    return '—' if v is None else f'{v:+.2f}%'


def perf_lines(date: str, rows: list, bars: dict, trade_days: list) -> list:
    """生成单条记录的「后续表现」markdown。"""
    perf = perf_for(date, rows, bars, trade_days)
    if not perf:
        return []
    last = max(p[4] for p in perf)
    L = ['## 后续表现（自推荐日起，T+5 冻结）', '',
         f'> 基准两个都给：**推荐日收盘**（推荐口径）与**次日开盘**'
         f'（策略实际可买到的价格，更接近真实成交）。',
         f'> 数据截至 `{last}`。', '',
         '| 代码 | 名称 | 推荐日收盘 | 次日开盘 | 最新收盘 | 自推荐日 | 自次日开盘 | 区间最高 |',
         '|---|---|---|---|---|---|---|---|']
    for (code, name, bc, no_, ld, lc, r1, r2, mg) in perf:
        L.append(f'| {code} | {name} | {bc:g} | {no_:g} | {lc:g} '
                 f'| {_pct(r1)} | {_pct(r2)} | {_pct(mg)} |')
    L.append('')
    return L


def build_record(date: str, df: pd.DataFrame, env: dict | None,
                 perf_md: list | None = None) -> str:
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
    L += ['---', '',
          f'> 完整数据：`reports/v2_candidates_{date}.csv`',
          '> ⚠️ 学习用途，非投资建议。', '']
    return '\n'.join(L)


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

    # 推荐股后续表现（自推荐日起算，T+5 冻结）
    perf_md = []
    if '推荐' in df.columns and '代码' in df.columns:
        rec_df = df[df['推荐'] == '推荐']
        if len(rec_df):
            rows = [(str(r['代码']).zfill(6), str(r.get('名称', '')))
                    for _, r in rec_df.iterrows()]
            perf_md = perf_lines(date, rows,
                                 read_bars([c for c, _ in rows]),
                                 market_trade_days())
    body = build_record(date, df, env, perf_md)

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

    results = [archive_one(d, not args.no_push, args.encrypt, args.force)
               for d in dates]
    rebuild_index()
    # 索引与脚本自身也要入库（记录无变更时，这些仍可能有改动）
    git('add', '-A', 'README.md', 'scripts', '.gitignore', check=False)
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
