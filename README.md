# stockRecord

本地项目初始化仓库，用于存放并同步 OpenClaw 生成的各类记录，通过 GitHub 共享与备份。

## 说明

- 本仓库由 [OpenClaw](https://openclaw.ai) 自动生成/维护记录内容
- 记录文件会定期提交并推送到 GitHub，实现多端共享与历史追溯

## 使用

```bash
# 拉取最新记录
git pull origin main

# 提交新记录
git add .
git commit -m "update: sync openclaw records"
git push origin main
```

## 目录结构

```
stockRecord/
├── README.md            # 项目说明
├── .gitignore           # 排除 .keys/（口令/密钥，严禁入库）
├── records/             # 陈学长选股策略 v2 明文推荐日报
│   └── YYYY/MM/         #   按 年/月/日 组织
│       └── DD.md
├── encrypted/           # 加密版推荐日报（仅代码/名称加密，其余字段明文）
│   └── YYYY/MM/
│       └── DD.md
└── scripts/
    ├── encrypt_daily.py # 当日加密版生成（方案2：随机密钥+口令解锁）
    └── decode.py        # 解密验证：输入口令还原名称/代码，供 clone 后核真
```

## 加密与解密验证机制

> 核心：**当日只把推荐股的「代码、名称」加密后提交 GitHub（git commit 即时间戳）**，其余字段（总分/阶段/行业/价位等）明文公开；**次日公布当日口令**，任何人 clone 后本地解码，即可核对当日提交内容是否真实、有无被篡改。

### 当日加密版（encrypted/YYYY/MM/DD.md）

- 加密字段：`代码`、`名称`（Fernet：AES-128-CBC + HMAC）
- 明文保留：总分、阶段、行业、流通市值、低吸/追涨/压力/止损位、竞价门槛等 —— 供有心人按行业+价位反查对应关系
- 密钥信封：随机 Fernet key 用口令（PBKDF2-SHA256，20 万次）二次加密，存于加密版文件头部（`salt` + `wrapped_key`）
- 口令规则：`CX-YYYYMMDD-XXXXXX`，当日生成，存本地 `.keys/`（**gitignore，严禁入库**），次日公布

### 次日解密验证（decode.py）

```bash
# 1. clone 仓库
git clone git@github.com:antikvo/stockRecord.git
cd stockRecord

# 2. 安装依赖
pip install cryptography pandas

# 3. 用当日口令解码验证（口令于次日公布）
python3 scripts/decode.py --date 20260907 --passphrase CX-20260907-XXXXXX
# 或直接指定文件
python3 scripts/decode.py --file encrypted/2026/09/07.md --passphrase CX-20260907-XXXXXX
```

- **`[Decrypt OK]` + `[VERIFIED]`** → 口令正确、源文件未篡改，与当日提交内容一致
- **`[FAIL]` / 字段显示「<解密失败>」** → 口令错误，或加密字段被改动过

## 陈学长推荐日报归档（明文）

> 数据来源：陈学长选股策略 v2 的每日推荐报告（`~/stock/strategy_v2/reports/v2_candidates_*.csv`）。

### 2026 年

#### 09 月

- [09-04](records/2026/09/04.md) — 推荐 5 只（环境：混沌，高炸板谨慎）
- [09-03](records/2026/09/03.md) — 推荐 4 只（环境：混沌）
- [09-02](records/2026/09/02.md) — 推荐 2 只（环境：普跌）
- [09-01](records/2026/09/01.md) — 推荐 2 只（环境：普涨）

#### 08 月

- [08-31](records/2026/08/31.md) — 推荐 1 只（环境：修复）
- [08-28](records/2026/08/28.md) — 推荐 4 只（环境：修复）
- [08-27](records/2026/08/27.md) — 推荐 2 只（环境：普涨）
- [08-26](records/2026/08/26.md) — 推荐 2 只（环境：修复）
- [08-25](records/2026/08/25.md) — 推荐 1 只（环境：高位分化）
- [08-24](records/2026/08/24.md) — 推荐 1 只（环境：普跌）
- [08-21](records/2026/08/21.md) — 推荐 13 只（环境：混沌）
- [08-20](records/2026/08/20.md) — 推荐 3 只（环境：高位分化）
- [08-19](records/2026/08/19.md) — 推荐 2 只（环境：普跌）
- [08-18](records/2026/08/18.md) — 推荐 4 只（环境：混沌）
- [08-17](records/2026/08/17.md) — 推荐 9 只（环境：普涨）
- [08-14](records/2026/08/14.md) — 推荐 9 只（环境：混沌）
- [08-13](records/2026/08/13.md) — 推荐 9 只（环境：普跌）
- [08-12](records/2026/08/12.md) — 推荐 3 只（环境：高位分化）
- [08-11](records/2026/08/11.md) — 推荐 3 只（环境：混沌）
- [08-10](records/2026/08/10.md) — 推荐 1 只（环境：普涨）
- [08-07](records/2026/08/07.md) — 推荐 2 只（环境：修复）
- [08-06](records/2026/08/06.md) — 推荐 0 只（环境：修复）

