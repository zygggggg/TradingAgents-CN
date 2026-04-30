# 东方财富 Skills 接入说明

本项目已预留东方财富 Skills / 妙想 Skills / OpenClaw 金融工具适配层，用于增强 A 股报告的数据、资讯、选股和组合复盘能力。

## 已接入能力

| 项目能力 | 东方财富 Skills 能力 | 接入位置 |
| --- | --- | --- |
| 金融数据查询 / 财务透视 | 行情、估值、财务、股东结构 | `tradingagents/dataflows/providers/china/integrated.py` |
| 金融资讯搜索 | 新闻、公告、研报、政策、事件 | `tradingagents/dataflows/news/realtime_news.py` |
| 智能选股 | 条件选股、低位修复池 | `scripts/run_eastmoney_skills.py screen` |
| 热点发现 | 热点板块、领涨股票、催化剂 | `scripts/run_eastmoney_skills.py hotspot` |
| 综合诊股 | 个股诊断、风险收益比 | `scripts/run_eastmoney_skills.py diagnose` |
| 自选股管理 | 自选股批量监控 | `scripts/run_eastmoney_skills.py watchlist` |
| 模拟组合 | 持仓复盘、组合风控 | `scripts/run_eastmoney_skills.py portfolio` |

## 获取 API Key

1. 打开东方财富 APP，搜索 `skills`，进入东方财富 Skills 首页。
2. 找到 `金融数据`、`资讯搜索`、`智能选股`、`热点发现`、`综合诊股`、`模拟组合`、`自选股管理` 等 Skills。
3. 按页面指引在 OpenClaw / 妙想 Skills 中创建或复制 API Key。
4. 如果使用 OpenClaw，本地通常会有凭据文件：

```bash
~/.openclaw/workspace/vault/credentials/eastmoney.json
```

官方示例文章：<https://caifuhao.eastmoney.com/news/20260313145625783332890>

## 环境变量

在项目 `.env` 中添加：

```bash
EASTMONEY_SKILLS_ENABLED=true
EASTMONEY_APIKEY=你的东方财富Skills_API_Key
EASTMONEY_SKILLS_API_BASE=https://mkapi2.dfcfs.com
EASTMONEY_SKILLS_TIMEOUT=20
EASTMONEY_SKILLS_MAX_REPORT_CHARS=40000
```

兼容字段：`EASTMONEY_APIKEY`、`EASTMONEY_SKILLS_API_KEY`、`MX_APIKEY` 三者任配一个即可。多个 Key 可以用英文逗号分隔。

如果通过代理运行，建议让东财域名直连：

```bash
NO_PROXY=localhost,127.0.0.1,eastmoney.com,mkapi2.dfcfs.com,marketing.dfcfs.com
```

## 使用方式

生成个股金融数据报告：

```bash
python scripts/run_eastmoney_skills.py fundamentals 000977 --name 浪潮信息
```

生成资讯/公告/研报搜索报告：

```bash
python scripts/run_eastmoney_skills.py news 000977 --name 浪潮信息
```

按低位修复策略筛股：

```bash
python scripts/run_eastmoney_skills.py screen
```

查看热点：

```bash
python scripts/run_eastmoney_skills.py hotspot --focus "AI、算力、机器人、军工"
```

综合诊股：

```bash
python scripts/run_eastmoney_skills.py diagnose 002354 --name 天娱数科
```

自选股监控：

```bash
python scripts/run_eastmoney_skills.py watchlist 000977 002354 300921 688258
```

模拟组合复盘：

```bash
python scripts/run_eastmoney_skills.py portfolio '[{"symbol":"000977","name":"浪潮信息","weight":0.3,"cost":70.0}]'
```

## 报告链路

配置 `EASTMONEY_SKILLS_ENABLED=true` 且提供 API Key 后：

- A 股基本面工具会优先尝试东方财富 Skills 金融数据查询，失败后降级到原有东方财富公开财务接口。
- A 股新闻工具会优先尝试东方财富 Skills 资讯搜索，失败后降级到 AKShare/东方财富新闻和其他备用新闻源。
- 行情/K 线仍保留项目原来的结构化东方财富、腾讯、新浪等数据源，因为 Skills 返回更偏投研文本，不适合作为严格 K 线表主源。

## 注意事项

- 不要把 API Key 提交到 Git。
- 东方财富 Skills 是投研辅助工具，不是券商下单接口。
- 自动交易必须另接券商官方 API，并先经过模拟交易和人工确认。
