#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日策略记录 —— 出图（HTML → PNG，供图文发布/存档）。

用法:
    python3 scripts/make_image.py                      # 今天，public（合规版）
    python3 scripts/make_image.py --date 20260922 --profile public
    python3 scripts/make_image.py --date 20260922 --profile full   # 内部版：含个股
    python3 scripts/make_image.py --all-recent         # 近 5 个交易日各出一张

两个档位的区别（重要）:
    public  合规版：**不含任何个股**。只有大盘环境、策略统计口径、累计模拟战绩
            与免责声明。这是可以公开发布的内容。
    full    内部版：含推荐个股明细与模拟交易，仅供自用/内部群，**不要公开发**。

设计原则：合规版是通过「不包含个股」实现合规，不是通过打码/代称把个股藏起来。
马赛克代码名称、谐音、"某XX股份"、"私信领名单" 这类做法改变不了
「向公众提供具体证券买卖建议」的实质，因此本项目不提供此类模式。
"""
from __future__ import annotations

import argparse
import html
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, '..', 'strategy_v2', 'src'))

import archive_daily as A  # noqa: E402

REPORTS = os.path.join(ROOT, '..', 'strategy_v2', 'reports')
OUTDIR = os.path.join(ROOT, 'images')
CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
W, H = 1080, 1440                      # 抖音图文常用 3:4
RENDER_H = 3200                        # 先按大高度渲染，再按内容裁剪（避免截断）
BG = '#0e1117'


def esc(v) -> str:
    return html.escape(str(v if v is not None else ''))


def load_env(date: str) -> dict:
    p = os.path.join(REPORTS, f'environment_{date}.json')
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding='utf-8'))
        except Exception:
            return {}
    return {}


def aggregate_stats() -> dict:
    """复用 archive_daily 的实现，避免两份口径漂移。"""
    return A.aggregate_stats()


def stage_dist(df: pd.DataFrame) -> list:
    if '阶段' not in df.columns:
        return []
    vc = df['阶段'].value_counts()
    return [(str(k), int(v)) for k, v in vc.items()]


def full_table(date: str, rows: list, bars: dict, tdays: list,
               rules: dict) -> str:
    """内部版：推荐个股 + 模拟交易（含个股，不可公开）。"""
    sims = {r['code']: A.simulate_trade(date, r, bars, tdays, rules)
            for r in rows}
    tr = ['<table><thead><tr><th>代码</th><th>名称</th><th>买入价</th>'
          '<th>卖出日</th><th>卖出价</th><th>原因</th><th>收益</th></tr></thead><tbody>']
    for r in rows:
        s = sims.get(r['code']) or {}
        if s.get('status') == '已了结':
            ret = s['ret_pct']
            cls = 'up' if ret > 0 else 'down'
            tr.append(
                f"<tr><td>{esc(r['code'])}</td><td>{esc(r['name'])}</td>"
                f"<td>{s['buy_price']:g}</td><td>{s['sell_date'][5:]}</td>"
                f"<td>{s['sell_price']:g}</td>"
                f"<td class='rs'>{esc(s['reason'])}</td>"
                f"<td class='{cls}'>{ret:+.2f}%</td></tr>")
        else:
            tr.append(
                f"<tr><td>{esc(r['code'])}</td><td>{esc(r['name'])}</td>"
                f"<td>—</td><td>—</td><td>—</td>"
                f"<td class='rs'>{esc(s.get('reason', s.get('status', '—')))}</td>"
                f"<td>—</td></tr>")
    tr.append('</tbody></table>')
    return ''.join(tr)


def render_html(date: str, profile: str, df: pd.DataFrame, env: dict,
                agg: dict) -> str:
    d_iso = f'{date[:4]}-{date[4:6]}-{date[6:8]}'
    try:
        wd = '一二三四五六日'[datetime.strptime(d_iso, '%Y-%m-%d').weekday()]
    except Exception:
        wd = ''
    label = env.get('label', '—')
    score = env.get('score', 0)
    tone = 'bull' if isinstance(score, int) and score > 0 else (
        'bear' if isinstance(score, int) and score < 0 else 'flat')

    stages = stage_dist(df)
    stage_html = '　'.join(
        f'<span class="chip">{esc(k)} <b>{v}</b></span>' for k, v in stages)

    # 行业分布：只做聚合统计（≥2 只才列），不指向任何个股。
    # 刻意不提供「首字 + 收盘价 + 行业」这类可反查到具体标的的组合。
    ind_html = ''
    if '行业' in df.columns:
        vc = df['行业'].value_counts()
        vc = vc[vc >= 2].head(8)
        if len(vc):
            ind_html = ('<div class="sec">候选池行业分布'
                        '<span class="warn">聚合统计 · 不指向个股</span></div>'
                        '<div class="chips">' + ''.join(
                            f'<span class="chip">{esc(k)} <b>{v}</b></span>'
                            for k, v in vc.items()) + '</div>')

    rec_n = 0
    if '推荐' in df.columns:
        rec_n = int((df['推荐'] == '推荐').sum())

    stats_html = '<div class="empty">暂无已了结的模拟交易</div>'
    if agg.get('n'):
        b, w = agg['best'], agg['worst']
        # 合规版绝不出现个股名称，只给统计口径
        if profile == 'public':
            ext = (f"单笔最好 <b class='up'>{b['ret']:+.2f}%</b>"
                   f"　单笔最差 <b class='down'>{w['ret']:+.2f}%</b>")
        else:
            ext = (f"最好 <b class='up'>{esc(b['name'])} {b['ret']:+.2f}%</b>"
                   f"　最差 <b class='down'>{esc(w['name'])} {w['ret']:+.2f}%</b>")
        stats_html = f"""
        <div class="stats">
          <div class="stat"><b>{agg['n']}</b><span>已了结</span></div>
          <div class="stat"><b>{agg['win_rate']:.0f}%</b><span>胜率</span></div>
          <div class="stat"><b class="{'up' if agg['avg'] > 0 else 'down'}">{agg['avg']:+.2f}%</b><span>平均收益</span></div>
          <div class="stat"><b>{agg.get('cancelled', 0)}</b><span>撤单</span></div>
        </div>
        <div class="extreme">{ext}</div>"""

    full_html = ''
    if profile == 'full':
        rows = A.parse_rec_table(
            open(os.path.join(ROOT, 'records', date[:4], date[4:6],
                              f'{date[6:8]}.md'), encoding='utf-8'
                 ).read().split('## 推荐股票')[-1].split('## 买卖参考')[0]) \
            if os.path.exists(os.path.join(ROOT, 'records', date[:4],
                                           date[4:6], f'{date[6:8]}.md')) else []
        if rows:
            bars = A.read_bars([r['code'] for r in rows])
            full_html = ('<div class="sec">推荐明细 · 模拟交易'
                         '<span class="warn">内部版 · 勿公开</span></div>'
                         + full_table(date, rows, bars,
                                      A.market_trade_days(),
                                      A.load_exit_rules()))

    badge = ('合规版 · 不含个股' if profile == 'public'
             else '内部版 · 含个股')
    badge_cls = 'ok' if profile == 'public' else 'warn'

    # 明日介入信号：策略的当日产出，做成醒目卡片。
    # 措辞按真实流程写 —— 信号只是候选，须次日 09:26 竞价确认后才决定是否介入，
    # 避免变成"明日必涨"式的荐股口吻。
    if rec_n > 0:
        sig_cls, sig_txt = 'has', f'{rec_n}'
        sig_note = ('护盘确认 / 放量反包<br>次日 09:26 竞价确认后才决定是否介入')
    else:
        sig_cls, sig_txt = 'none', '0'
        sig_note = ('今日无符合条件的标的<br>明日空仓观望，不新开仓')
    signal_html = f"""<div class="signal {sig_cls}">
  <div><div class="lb">明日介入信号</div>
       <div class="nm">{sig_txt}<span>只</span></div></div>
  <div class="nt">{sig_note}</div>
</div>"""

    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ width:{W}px; min-height:{H}px; font-family:"PingFang SC","Hiragino Sans GB",
         "STHeiti",sans-serif; background:{BG}; color:#e6edf3; }}
  .wrap {{ padding:52px 56px; min-height:{H}px; display:flex; flex-direction:column; }}
  .badge {{ align-self:flex-start; font-size:22px; padding:6px 18px;
            border-radius:999px; margin-bottom:22px; }}
  .badge.ok {{ background:#12351f; color:#3fb950; border:1px solid #238636; }}
  .badge.warn {{ background:#3a1d1d; color:#f85149; border:1px solid #8b2c2c; }}
  h1 {{ font-size:56px; letter-spacing:1px; line-height:1.24; }}
  .sub {{ font-size:28px; color:#8b949e; margin-top:12px; }}
  .env {{ display:flex; align-items:baseline; gap:20px; margin:38px 0 26px; }}
  .env .big {{ font-size:76px; font-weight:800; }}
  .env .big.bull {{ color:#f85149; }} .env .big.bear {{ color:#3fb950; }}
  .env .big.flat {{ color:#d29922; }}
  .env .sc {{ font-size:32px; color:#8b949e; }}
  .grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:14px; }}
  .grid div {{ background:#161b22; border:1px solid #21262d; border-radius:14px;
               padding:18px 20px; text-align:center; }}
  .grid b {{ display:block; font-size:44px; }}
  .grid span {{ font-size:22px; color:#8b949e; }}
  .note {{ margin-top:16px; font-size:24px; color:#d29922; }}
  .sec {{ font-size:30px; font-weight:700; margin:34px 0 14px;
          padding-left:14px; border-left:6px solid #1f6feb; display:flex;
          justify-content:space-between; align-items:center; }}
  .sec .warn {{ font-size:20px; color:#f85149; font-weight:400; }}
  .chips {{ line-height:1.9; }}
  .chip {{ display:inline-block; background:#161b22; border:1px solid #21262d;
           border-radius:10px; padding:6px 14px; font-size:23px;
           color:#c9d1d9; margin:0 8px 8px 0; }}
  .chip b {{ color:#1f6feb; }}
  .stats {{ display:grid; grid-template-columns:repeat(4,1fr); gap:14px; }}
  .stat {{ background:#161b22; border:1px solid #21262d; border-radius:14px;
           padding:16px 10px; text-align:center; }}
  .stat b {{ display:block; font-size:40px; }}
  .stat span {{ font-size:21px; color:#8b949e; }}
  .extreme {{ margin-top:16px; font-size:24px; color:#8b949e; }}
  .up {{ color:#f85149; }} .down {{ color:#3fb950; }}
  table {{ width:100%; border-collapse:collapse; font-size:23px; margin-top:8px; }}
  th,td {{ padding:11px 8px; border-bottom:1px solid #21262d; text-align:left; }}
  th {{ color:#8b949e; font-weight:500; font-size:21px; }}
  td.rs {{ font-size:19px; color:#8b949e; }}
  .empty {{ font-size:24px; color:#8b949e; }}
  .signal {{ margin-top:26px; padding:22px 28px; border-radius:16px;
             display:flex; align-items:center; justify-content:space-between;
             background:#161b22; border:1px solid #21262d; }}
  .signal .lb {{ font-size:25px; color:#8b949e; letter-spacing:2px; }}
  .signal .nm {{ font-size:62px; font-weight:800; line-height:1.1;
                 margin-top:2px; }}
  .signal .nm span {{ font-size:24px; font-weight:400; color:#8b949e;
                      margin-left:8px; }}
  .signal .nt {{ font-size:21px; color:#6e7681; text-align:right;
                 line-height:1.55; }}
  .signal.has {{ background:linear-gradient(90deg,#132a4a,#161b22);
                 border:1px solid #1f6feb; }}
  .signal.has .lb {{ color:#79c0ff; }}
  .signal.has .nm {{ color:#58a6ff; }}
  .signal.has .nt {{ color:#8b949e; }}
  .signal.none .nm {{ color:#6e7681; }}
  .foot {{ margin-top:44px; padding-top:26px; border-top:1px solid #21262d;
           font-size:21px; color:#6e7681; line-height:1.7; }}
</style></head><body><div class="wrap">
  <div class="badge {badge_cls}">{esc(badge)}</div>
  <h1>A股盘面 · N字选股策略记录</h1>
  <div class="sub">{d_iso} 星期{wd} ｜ 数据截至 {esc(env.get('data_date', d_iso))}</div>

  <div class="env">
    <div class="big {tone}">{esc(label)}</div>
    <div class="sc">环境分 {score:+d} ｜ {esc(env.get('quality', ''))}</div>
  </div>
  <div class="grid">
    <div><b class="up">{esc(env.get('up', '—'))}</b><span>上涨</span></div>
    <div><b class="down">{esc(env.get('down', '—'))}</b><span>下跌</span></div>
    <div><b>{esc(env.get('limit_up', '—'))}</b><span>涨停</span></div>
    <div><b>{esc(env.get('limit_down', env.get('ld', '—')))}</b><span>跌停</span></div>
    <div><b>{esc(f"{float(env.get('zha_ban_rate', 0)) * 100:.0f}%")}</b><span>炸板率</span></div>
    <div><b>{esc(env.get('max_board', '—'))}板</b><span>最高连板</span></div>
  </div>

  {signal_html}

  <div class="sec">策略扫描口径</div>
  <div class="chips">
    <span class="chip">候选池 <b>{len(df)}</b> 只</span>
    <span class="chip">介入信号 <b>{rec_n}</b> 只</span>
  </div>
  <div class="chips">{stage_html}</div>
  {ind_html}

  <div class="sec">累计模拟战绩</div>
  {stats_html}
  {full_html}

  <div class="foot">
    ⚠️ 本图为个人模拟盘（虚拟资金）量化研究记录，用于策略验证与复盘。<br>
    {('图中不含任何个股名称、代码与买卖建议，不构成任何投资建议。' if profile == 'public'
      else '图中含个股信息，仅供本人存档，请勿公开传播。')}<br>
    市场有风险，据此操作风险自担。· 生成于 {datetime.now():%Y-%m-%d %H:%M}
  </div>
</div></body></html>"""


def shoot(html_text: str, out_png: str) -> bool:
    if not os.path.exists(CHROME):
        print(f'[err] 未找到 Chrome：{CHROME}', file=sys.stderr)
        return False
    os.makedirs(os.path.dirname(out_png), exist_ok=True)
    with tempfile.NamedTemporaryFile('w', suffix='.html', delete=False,
                                     encoding='utf-8') as f:
        f.write(html_text)
        tmp = f.name
    try:
        r = subprocess.run(
            [CHROME, '--headless', '--disable-gpu', '--hide-scrollbars',
             '--force-device-scale-factor=1',
             f'--window-size={W},{RENDER_H}', f'--screenshot={out_png}',
             f'file://{tmp}'],
            capture_output=True, text=True, timeout=120)
        if not os.path.exists(out_png):
            print(f'[err] 截图失败：{r.stderr[:300]}', file=sys.stderr)
            return False
        return _crop_to_content(out_png)
    finally:
        os.unlink(tmp)


def _crop_to_content(png: str) -> bool:
    """按内容裁剪高度（推荐明细多的日子会超过 1440，避免被截断）。"""
    try:
        from PIL import Image, ImageChops
        im = Image.open(png).convert('RGB')
        bg = im.getpixel((4, 4))                      # 左上内边距处即背景
        diff = ImageChops.difference(im, Image.new('RGB', im.size, bg))
        bbox = diff.getbbox()
        target = max(H, (bbox[3] + 10) if bbox else H)  # 至少 1440，保持 3:4 下限
        target = min(target, im.height)
        if target < im.height:
            im.crop((0, 0, im.width, target)).save(png)
        return True
    except Exception as e:                                # noqa: BLE001
        print(f'[warn] 裁剪失败（保留原始大图）：{e}', file=sys.stderr)
        return True


def one(date: str, profile: str) -> str | None:
    csv = os.path.join(REPORTS, f'v2_candidates_{date}.csv')
    if not os.path.exists(csv):
        print(f'[skip] {date} 无报告')
        return None
    df = pd.read_csv(csv, dtype={'代码': str})
    env = load_env(date)
    agg = aggregate_stats()
    out = os.path.join(OUTDIR, f'{date}-{profile}.png')
    if shoot(render_html(date, profile, df, env, agg), out):
        print(f'[ok] {out}')
        return out
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=None, help='YYYYMMDD，默认今天')
    ap.add_argument('--profile', choices=['public', 'full'], default='public')
    ap.add_argument('--all-recent', action='store_true',
                    help='近 5 个交易日各出一张')
    args = ap.parse_args()
    dates = []
    if args.all_recent:
        td = A.market_trade_days()[-5:]
        dates = [d.replace('-', '') for d in td]
    else:
        dates = [args.date or datetime.now().strftime('%Y%m%d')]
    ok = sum(1 for d in dates if one(d, args.profile))
    print(f'完成：{ok}/{len(dates)} 张 → {OUTDIR}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
