#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""陈学长推荐日报 —— 当日加密版生成（方案2：随机密钥 + 口令解锁）

用法:
    python3 encrypt_daily.py --csv <v2_candidates_YYYYMMDD.csv> [--date YYYYMMDD] [--env <environment.json>]

原理:
    1. 读取当日 v2 推荐 CSV，提取「推荐」股票（名称/代码/总分/阶段/行业/价格等）
    2. 对每只股票的 **代码** 与 **名称** 字段加密（其余字段明文，便于有心人按行业+价位反查）
    3. 随机生成 Fernet key（AES-128-CBC + HMAC）
    4. 用口令（PBKDF2-SHA256 派生）将该 Fernet key 二次加密，存进加密版 markdown 文件头部
    5. 输出:
       - encrypted/YYYY/MM/DD.md       加密版记录（入 git）
       - .keys/YYYYMMDD.key            当日 Fernet key（gitignore，本地留档）
       - .keys/YYYYMMDD.pass           当日明文口令（gitignore，次日公布用）

口令规则: 形如 `CX-YYYYMMDD-<6位随机>`，便于人类传播与次日公布。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys
from datetime import datetime

import pandas as pd
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CSV_DIR = os.path.join(ROOT, '..', 'strategy_v2', 'reports')


def gen_passphrase(date: str) -> str:
    """生成当日口令: CX-YYYYMMDD-XXXXXX"""
    rand = secrets.token_hex(3).upper()  # 6 hex chars
    return f"CX-{date}-{rand}"


def derive_key_from_passphrase(passphrase: str, salt: bytes) -> bytes:
    """口令 + salt → PBKDF2-SHA256 派生 32 字节，转 urlsafe base64 作为 Fernet key。"""
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=200_000)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode('utf-8')))


def enc(s: str, f: Fernet) -> str:
    """加密字符串为 ENC:...（base64 raw）。"""
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return ''
    ct = f.encrypt(str(s).encode('utf-8'))
    return 'ENC:' + ct.decode('ascii')


def fnum(v):
    try:
        if v is None or pd.isna(v):
            return ''
        f = float(v)
        return f'{f:g}'
    except Exception:
        return ''


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv', default=None, help='v2_candidates_YYYYMMDD.csv 路径')
    ap.add_argument('--date', default=None, help='日期 YYYYMMDD，默认从 csv 名推断')
    ap.add_argument('--env', default=None, help='environment_YYYYMMDD.json 路径')
    ap.add_argument('--passphrase', default=None,
                    help='指定口令（默认自动生成 CX-DATE-XXXXXX）')
    args = ap.parse_args()

    date = args.date
    csv = args.csv or os.path.join(DEFAULT_CSV_DIR, f'v2_candidates_{date}.csv')
    if date is None:
        base = os.path.basename(csv)
        date = base.replace('v2_candidates_', '').replace('.csv', '')

    # 生成 / 使用口令
    passphrase = args.passphrase or gen_passphrase(date)

    # 读取 CSV
    if not os.path.exists(csv):
        print(f"[err] 未找到 {csv}")
        return 1
    df = pd.read_csv(csv, dtype={'代码': str})

    # 环境信息
    env = None
    if args.env and os.path.exists(args.env):
        try:
            env = json.load(open(args.env, encoding='utf-8'))
        except Exception:
            env = None

    # 提取推荐股票
    if '推荐' in df.columns:
        rec = df[df['推荐'] == '推荐'].copy()
    else:
        rec = df[df['阶段'].str.contains('介入信号', na=False)].copy()
    rec = rec.sort_values('总分', ascending=False) if not rec.empty else rec

    # 随机 Fernet key
    fernet_key = Fernet.generate_key()
    f = Fernet(fernet_key)

    # 用口令加密 Fernet key（存头部）
    salt = secrets.token_bytes(16)
    wrap_key = derive_key_from_passphrase(passphrase, salt)
    wrapped = Fernet(wrap_key).encrypt(fernet_key).decode('ascii')

    # 组装加密版 markdown
    date_iso = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
    lines = []
    lines.append(f"# 陈学长推荐（{date_iso}）· 加密版")
    lines.append("")
    lines.append(f"> 日期：`{date_iso}`　推荐种数：**{len(rec)}**")
    if env:
        q = env.get('quality', '')
        lines.append(
            f"> 环境：{env.get('label', '?')}（分 {env.get('score', '?')}）｜涨 {env.get('up', '-')}"
            f" / 跌 {env.get('down', '-')}｜涨停 {env.get('limit_up', '-')}"
            f"｜大盘 {env.get('index_pct', '-')}%")
        if q:
            lines.append(f"> 备注：{q}")
    lines.append("")
    lines.append("> 🔐 本文件为加密版：**代码、名称**已加密（`ENC:...`）。")
    lines.append("> 其余字段（总分/阶段/行业/得分/价位）为明文，供有心人按行业与价位对应反查。")
    lines.append("> 解密口令将于**次日公布**，用仓库内 `scripts/decode.py` 可还原并验证真实性。")
    lines.append("")
    lines.append("## 密钥信封（口令解锁 Fernet key）")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps({
        "v": 1,
        "algo": "PBKDF2-SHA256(200000) -> Fernet(AES128-CBC+HMAC)",
        "salt": salt.hex(),
        "wrapped_key": wrapped,
        "fields_encrypted": ["代码", "名称"],
    }, ensure_ascii=False, indent=2))
    lines.append("```")
    lines.append("")
    lines.append("## 推荐股票（加密版）")
    lines.append("")

    if len(rec) == 0:
        lines.append("_" + "今日无推荐（介入信号为空，仅观察池）。" + "_")
    else:
        hdr = "| 代码(ENC) | 名称(ENC) | 总分 | 阶段 | 行业 | 流通市值(亿)"
        for c in ['低吸位', '追涨位', '压力位', '止损位', '竞价门槛万', '竞价强势万']:
            if c in df.columns:
                hdr += f" | {c}"
        lines.append(hdr + " |")
        sep = "|---|---|---|---|---|---|"
        lines.append(sep)
        for _, r in rec.iterrows():
            row = (f"| {enc(r['代码'], f)} | {enc(r['名称'], f)} "
                   f"| {fnum(r.get('总分'))} | {str(r.get('阶段', ''))} "
                   f"| {str(r.get('行业', ''))} | {fnum(r.get('流通市值亿'))}")
            for c in ['低吸位', '追涨位', '压力位', '止损位', '竞价门槛万', '竞价强势万']:
                if c in df.columns:
                    row += f" | {fnum(r.get(c))}"
            lines.append(row + " |")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"> 完整数据：`reports/v2_candidates_{date}.csv`")
    lines.append("> ⚠️ 学习用途，非投资建议。")
    lines.append("")

    # 写出
    outdir = os.path.join(ROOT, 'encrypted', date[:4], date[4:6])
    os.makedirs(outdir, exist_ok=True)
    outpath = os.path.join(outdir, f"{date[6:8]}.md")
    with open(outpath, 'w', encoding='utf-8') as fp:
        fp.write('\n'.join(lines) + '\n')

    # 存 key / 口令（gitignore, 本地留档）
    keydir = os.path.join(ROOT, '.keys')
    os.makedirs(keydir, exist_ok=True)
    with open(os.path.join(keydir, f"{date}.key"), 'wb') as fp:
        fp.write(fernet_key)
    with open(os.path.join(keydir, f"{date}.pass"), 'w', encoding='utf-8') as fp:
        fp.write(passphrase + '\n')

    print(f"[ok] 加密版: {outpath}")
    print(f"[ok] 推荐种数: {len(rec)}")
    print(f"[ok] 口令: {passphrase}  (见 .keys/{date}.pass, 次日公布)")
    return 0


if __name__ == '__main__':
    sys.exit(main())
