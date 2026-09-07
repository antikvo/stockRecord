#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""陈学长推荐日报 —— 解密验证脚本

用法:
    python3 decode.py --file encrypted/2026/09/07.md --passphrase CX-20260907-XXXXXX
    # 或自动匹配当日日期（口令 CX-YYYYMMDD-XXXXXX）:
    python3 decode.py --date 20260907 --passphrase CX-20260907-XXXXXX

作用:
    1. 解析加密版 markdown 中的「密钥信封」（salt + wrapped_key）
    2. 用口令 PBKDF2 派生密钥，解开 Fernet key
    3. 用 Fernet key 解密代码/名称字段（ENC:...）
    4. 输出明文对照表，供核对真实性：
       - Decrypt OK          → 与源文件一致，未篡改
       - Decrypt FAIL/Invalid → 口令错 / 文件被改过 / 密钥不匹配

依赖: cryptography (pip install cryptography), pandas
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def derive_key_from_passphrase(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=200_000)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode('utf-8')))


def unwrap_key(passphrase: str, salt: bytes, wrapped_b64: str) -> bytes:
    wrap_key = derive_key_from_passphrase(passphrase, salt)
    return Fernet(wrap_key).decrypt(wrapped_b64.encode('ascii'))


def extract_envelope(md: str) -> dict:
    """从 markdown 中提取 json 密钥信封。"""
    m = re.search(r'```json\n(.*?)\n```', md, re.S)
    if not m:
        raise RuntimeError('未找到密钥信封（```json ... ```）。文件格式不符。')
    return json.loads(m.group(1))


def decrypt_field(token: str, f: Fernet) -> str:
    if token.startswith('ENC:'):
        try:
            return f.decrypt(token[4:].encode('ascii')).decode('utf-8')
        except InvalidToken:
            return '<解密失败：口令错/被篡改>'
    return token


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--file', default=None, help='加密版 markdown 路径')
    ap.add_argument('--date', default=None, help='日期 YYYYMMDD（自动定位 encrypted/YYYY/MM/DD.md）')
    ap.add_argument('--passphrase', required=True, help='当日口令 CX-YYYYMMDD-XXXXXX')
    args = ap.parse_args()

    if args.file:
        fpath = args.file
    elif args.date:
        date = args.date
        fpath = os.path.join(ROOT, 'encrypted', date[:4], date[4:6], f"{date[6:8]}.md")
    else:
        print("[err] 需要 --file 或 --date")
        return 1

    if not os.path.exists(fpath):
        print(f"[err] 未找到 {fpath}")
        return 1

    md = open(fpath, encoding='utf-8').read()
    env = extract_envelope(md)

    try:
        fkey = unwrap_key(args.passphrase, bytes.fromhex(env['salt']),
                          env['wrapped_key'])
        f = Fernet(fkey)
    except InvalidToken:
        print("[[FAIL]] 口令错误或文件密钥信封被篡改，无法解出密钥。")
        return 1

    print(f"[[Decrypt OK]] 口令正确，密钥信封已解开。")
    print(f"   算法: {env.get('algo')}  加密字段: {env.get('fields_encrypted')}")
    print("")

    # 解析表格行
    rows = []
    for line in md.splitlines():
        line = line.strip()
        if line.startswith('|') and 'ENC:' in line and '代码' not in line:
            cells = [c.strip() for c in line.strip('|').split('|')]
            if len(cells) >= 6:
                code_enc, name_enc = cells[0], cells[1]
                rest = cells[2:]
                rows.append((code_enc, name_enc, rest))

    if not rows:
        print("未在文件中找到加密行（可能当日无推荐，仅观察池）。")
        return 0

    print("解密对照（名称/代码还原）：")
    print("")
    print(f"{'代码':<8} {'名称':<10} 总分 阶段  行业")
    print("-" * 60)
    ok = True
    for code_enc, name_enc, rest in rows:
        code = decrypt_field(code_enc, f)
        name = decrypt_field(name_enc, f)
        if code.startswith('<解密失败') or name.startswith('<解密失败'):
            ok = False
        print(f"{code:<8} {name:<10} {rest[0] if rest else ''}")
    print("-" * 60)
    print("")
    if ok:
        print("==" * 20)
        print("[VERIFIED] 解密成功。源文件真实性与当日提交内容一致（加密字段已还原）。")
        print("==" * 20)
    else:
        print("[FAIL] 部分字段解密失败（口令错或内容被篡改）。")
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
