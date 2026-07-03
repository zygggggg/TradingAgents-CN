#!/usr/bin/env python3
"""Run a full TradingAgents-CN stock report and value layer, then save files."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from scripts.report_paths import analysis_stock_dir, safe_name
from scripts.run_event_agent_report import build_agent_config, load_env_file, to_jsonable
from scripts.send_dingtalk import send_markdown
from tradingagents.dataflows.fundamentals_quality import ensure_fundamentals_quality
from tradingagents.dataflows.report_quality import ensure_analysis_sections, ensure_report_price_guard


def cn_timezone():
    if ZoneInfo is not None:
        return ZoneInfo("Asia/Shanghai")
    return timezone(timedelta(hours=8))


def now_cn() -> datetime:
    return datetime.now(cn_timezone())


TRUNCATION_FORBIDDEN_PATTERNS = (
    "原文过长",
    "内容过长",
    "已截断",
    "内容已截断",
    "数据已截断",
    "truncated",
)


def clip(value: Any, limit: int | None = None) -> str:
    del limit
    return str(value or "").strip()


def ensure_no_truncation_markers(outputs: Dict[str, str]) -> None:
    violations: List[str] = []
    for name, text in outputs.items():
        for pattern in TRUNCATION_FORBIDDEN_PATTERNS:
            if pattern in text:
                violations.append(f"{name}: {pattern}")
    if violations:
        raise RuntimeError("报告包含截断标记，禁止写出：" + "; ".join(violations))


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_status(path: Path, payload: Dict[str, Any]) -> None:
    data = dict(payload)
    data["updated_at"] = now_cn().isoformat()
    write_json(path, data)


def build_full_report(symbol: str, stock_name: str, analysis_date: str, state: Dict[str, Any], decision: Dict[str, Any], analysts: List[str]) -> str:
    investment_debate = state.get("investment_debate_state") or {}
    risk_debate = state.get("risk_debate_state") or {}
    sections = [
        f"# {stock_name}-{analysis_date}-report",
        "",
        "> TradingAgents-CN 完整分析报告；仅用于学习和辅助决策，不构成投资建议，不自动交易。",
        "",
        "## 1. 基本信息",
        "",
        f"- 股票：{stock_name}（{symbol}）",
        f"- 分析日期：{analysis_date}",
        f"- 生成时间：{now_cn().strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"- 分析师：{', '.join(analysts)}",
        "",
        "## 2. 最终决策",
        "",
        "```json\n" + json.dumps(to_jsonable(decision), ensure_ascii=False, indent=2) + "\n```",
        "",
        "## 3. 市场/技术分析",
        "",
        clip(state.get("market_report"), 14000),
        "",
        "## 4. 新闻分析",
        "",
        clip(state.get("news_report"), 9000),
        "",
        "## 5. 基本面分析",
        "",
        clip(state.get("fundamentals_report"), 14000),
        "",
        "## 6. 多空研究员辩论",
        "",
        "### 多头研究员",
        "",
        clip(investment_debate.get("bull_history"), 9000),
        "",
        "### 空头研究员",
        "",
        clip(investment_debate.get("bear_history"), 9000),
        "",
        "### 研究经理综合决策",
        "",
        clip(investment_debate.get("judge_decision"), 9000),
        "",
        "## 7. 交易员计划",
        "",
        clip(state.get("trader_investment_plan") or state.get("investment_plan"), 10000),
        "",
        "## 8. 风险委员会",
        "",
        "### 激进风险分析师",
        "",
        clip(risk_debate.get("risky_history"), 8000),
        "",
        "### 保守风险分析师",
        "",
        clip(risk_debate.get("safe_history"), 8000),
        "",
        "### 中性风险分析师",
        "",
        clip(risk_debate.get("neutral_history"), 8000),
        "",
        "### 风险经理最终决策",
        "",
        clip(risk_debate.get("judge_decision"), 10000),
        "",
        "## 9. 风险提示",
        "",
        "- 本报告只做学习和辅助分析，不是确定性荐股。",
        "- A股波动较大，请结合仓位、现金流、风险承受能力人工确认。",
        "- 如核心财务数据缺失，系统应中止或重取数据，不能生成正常价值报告。",
    ]
    return "\n".join(sections).strip() + "\n"


def build_learning_report(symbol: str, stock_name: str, analysis_date: str, decision: Dict[str, Any]) -> str:
    return f"""# {stock_name}-{analysis_date}-learn

> 投资小白学习版：这份报告解释各类 Agent 分别在看什么，以及你应该怎么读完整报告。

## 1. 这次 Agent 怎么分工

- 市场/技术分析师：看趋势、均线、量能、支撑压力，主要服务短线交易和风控。
- 新闻分析师：看新闻、公告、舆情变化，判断是否有事件驱动。
- 基本面分析师：看公司业务、财务质量、盈利和风险，服务中长期判断。
- 多头研究员：尽量找上涨理由和机会。
- 空头研究员：尽量找下跌风险和反证。
- 研究经理：在多空观点之间做综合判断。
- 风险委员会：把交易想法转成仓位、止损、回避条件。

## 2. 先看最终决策，不要只看一句买卖

```json
{json.dumps(to_jsonable(decision), ensure_ascii=False, indent=2)}
```

## 3. 新手读报告顺序

1. 先读 `combined`：看长短线是否一致。
2. 再读 `trading`：看短线支撑压力和风控线。
3. 再读 `value`：看公司是否适合长期持有。
4. 最后读完整 `report`：看多空辩论和风险委员会怎么推理。

## 4. 关键学习点

- 技术指标只能说明短线强弱，不能证明公司便宜。
- 价值投资必须回答：公司好不好、价格贵不贵、什么情况证明自己错了。
- 如果短线破位，不能简单用“长期看好”当作不止损理由。
- 如果长期价值不足，短线反弹也只能当交易，不能当长期持有。

## 5. 本次报告中的主要原文入口

- 市场/技术分析：见 `report` 第3节。
- 基本面分析：见 `report` 第5节。
- 多空辩论：见 `report` 第6节。
- 风险委员会：见 `report` 第8节。
"""


def run_graph(symbol: str, analysis_date: str, analysts: List[str], eastmoney_skills_context: str = ""):
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    graph = TradingAgentsGraph(selected_analysts=analysts, debug=False, config=build_agent_config())
    state, decision = graph.propagate(symbol, analysis_date, initial_context=eastmoney_skills_context)
    return graph, state, decision


def run_value_layer(symbol: str, stock_name: str, analysis_date: str) -> Dict[str, Any]:
    command = [
        sys.executable,
        str(ROOT / "scripts" / "run_value_layer.py"),
        "--symbol",
        symbol,
        "--stock-name",
        stock_name,
        "--date",
        analysis_date,
    ]
    completed = subprocess.run(command, cwd=str(ROOT), text=True, capture_output=True, timeout=600)
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr[-6000:],
    }


def parse_int_tuple(raw: str, default: tuple[int, ...]) -> tuple[int, ...]:
    try:
        values = tuple(int(item.strip()) for item in str(raw or "").split(",") if item.strip())
        return values or default
    except Exception:
        return default


def default_backtest_start(analysis_date: str) -> str:
    try:
        end = datetime.strptime(analysis_date, "%Y-%m-%d")
    except Exception:
        end = now_cn().replace(tzinfo=None)
    days = int(os.getenv("QUANT_BACKTEST_LOOKBACK_DAYS", "730"))
    return (end - timedelta(days=days)).strftime("%Y-%m-%d")


def run_quant_layer(symbol: str, stock_name: str, analysis_date: str, fundamentals_text: str, paths: Dict[str, Path]) -> Dict[str, Any]:
    from tradingagents.quant import generate_quant_report

    quant, report = generate_quant_report(
        stock_symbol=symbol,
        analysis_date=analysis_date,
        market_type="A股",
        fundamentals_report=fundamentals_text,
    )
    paths["quant"].write_text(report.rstrip() + "\n", encoding="utf-8")
    paths["quant_json"].write_text(json.dumps(quant or {}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "status": "completed" if quant else "unavailable",
        "score": (quant or {}).get("score"),
        "signal": (quant or {}).get("signal"),
        "risk_level": (quant or {}).get("risk_level"),
        "data_source": (quant or {}).get("data_source"),
        "model_name": (quant or {}).get("model_name"),
        "paths": {"report": str(paths["quant"]), "json": str(paths["quant_json"])},
    }


def run_quant_backtest(symbol: str, analysis_date: str, fundamentals_text: str, stock_dir: Path) -> Dict[str, Any]:
    if os.getenv("QUANT_BACKTEST_ENABLED", "true").lower() != "true":
        return {"status": "disabled"}

    from tradingagents.quant.backtest import BacktestConfig, run_rolling_backtest, save_backtest_outputs

    horizons = parse_int_tuple(os.getenv("QUANT_BACKTEST_HORIZONS", "5,20,60"), (5, 20, 60))
    config = BacktestConfig(
        symbol=symbol,
        start_date=os.getenv("QUANT_BACKTEST_START_DATE", default_backtest_start(analysis_date)),
        end_date=analysis_date,
        horizons=horizons,
        rebalance_step=int(os.getenv("QUANT_BACKTEST_REBALANCE_STEP", "5")),
        min_history=int(os.getenv("QUANT_BACKTEST_MIN_HISTORY", "80")),
        score_threshold=float(os.getenv("QUANT_BACKTEST_SCORE_THRESHOLD", "52")),
        fundamentals_report=fundamentals_text,
    )
    result = run_rolling_backtest(config)
    output_paths = save_backtest_outputs(result, stock_dir)
    prefixed_paths = rename_backtest_outputs(output_paths, stock_dir, analysis_date)
    return {
        "status": "completed",
        "sample_count": result.summary.get("sample_count"),
        "selected_count": result.summary.get("selected_count"),
        "selected_ratio": result.summary.get("selected_ratio"),
        "data_source": result.data_source,
        "summary": result.summary,
        "paths": prefixed_paths,
    }


def _strip_raw_json_section(markdown_text: str) -> str:
    return str(markdown_text or "").split("## 原始返回JSON", 1)[0].strip()


def build_eastmoney_agent_context(eastmoney_appendix: str) -> str:
    text = str(eastmoney_appendix or "").strip()
    if not text:
        return ""
    lines = text.splitlines()
    if lines and lines[0].startswith("## 10."):
        lines[0] = "# 东方财富 Skills 前置上下文"
    text = "\n".join(lines).strip()
    try:
        max_chars = int(os.getenv("EASTMONEY_SKILLS_AGENT_CONTEXT_CHARS", "24000"))
    except Exception:
        max_chars = 24000
    if max_chars > 0 and len(text) > max_chars:
        text = text[:max_chars].rstrip()
    return text


def build_eastmoney_skills_appendix(
    symbol: str,
    stock_name: str,
    paths: Dict[str, Path],
) -> tuple[str, Dict[str, Any]]:
    load_env_file(ROOT / ".env")
    try:
        from tradingagents.dataflows.providers.china.eastmoney_skills import (
            eastmoney_skills_available,
            get_eastmoney_skills_client,
        )
    except Exception as exc:
        return "", {"status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}

    if not eastmoney_skills_available():
        return "", {"status": "disabled", "reason": "EASTMONEY_SKILLS/MX_APIKEY未配置"}

    client = get_eastmoney_skills_client()
    outputs: Dict[str, Dict[str, Any]] = {}
    sections = [
        "## 10. 东方财富 Skills 增强数据",
        "",
        "数据来源：东方财富 Skills / OpenClaw 金融工具；用于增强行情、财务、公告、研报和综合诊股上下文。",
        "",
    ]
    jobs = [
        ("diagnose", "综合诊股", paths["eastmoney_diagnose"], lambda: client.stock_diagnosis_report(symbol, stock_name=stock_name)),
        ("fundamentals", "金融数据 / 财务透视", paths["eastmoney_fundamentals"], lambda: client.fundamentals_report(symbol, stock_name=stock_name, report_count=5)),
        ("news", "资讯搜索 / 公告研报", paths["eastmoney_news"], lambda: client.news_report(symbol, stock_name=stock_name, limit_hint=10)),
    ]

    for key, title, output_path, runner in jobs:
        try:
            text = runner()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(text, encoding="utf-8")
            outputs[key] = {"status": "completed", "path": str(output_path)}
            readable = _strip_raw_json_section(text)
            if readable:
                sections.extend([f"### {title}", "", f"完整原始返回文件：`{output_path}`", "", readable, ""])
        except Exception as exc:
            outputs[key] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
            sections.extend([f"### {title}", "", f"东方财富 Skills 调用失败：{type(exc).__name__}: {exc}", ""])

    return "\n".join(sections).strip() + "\n", {"status": "completed", "outputs": outputs}


def rename_backtest_outputs(output_paths: Dict[str, str], stock_dir: Path, analysis_date: str) -> Dict[str, str]:
    prefix = f"{safe_name(stock_dir.name)}-{analysis_date}"
    mapping = {
        "report": stock_dir / f"{prefix}-quant-backtest.md",
        "rows_csv": stock_dir / f"{prefix}-quant-backtest-rows.csv",
        "summary_json": stock_dir / f"{prefix}-quant-backtest.json",
    }
    renamed: Dict[str, str] = {}
    for key, old_path in output_paths.items():
        target = mapping.get(key)
        if not target:
            renamed[key] = old_path
            continue
        source = Path(old_path)
        if source.exists():
            source.replace(target)
        renamed[key] = str(target)
    return renamed


def send_completion(args, paths: Dict[str, Path], value_result: Dict[str, Any], status: str) -> None:
    if not args.notify:
        return
    load_env_file(ROOT / ".stock-monitor.env")
    webhook = os.getenv("DINGTALK_WEBHOOK", "")
    secret = os.getenv("DINGTALK_SECRET", "")
    if not webhook:
        return
    server_host = os.getenv("MONITOR_SERVER_HOST", "49.235.148.184")
    server_user = os.getenv("MONITOR_SERVER_USER", "ubuntu")
    server_project = os.getenv("MONITOR_SERVER_PROJECT", str(ROOT))
    value_ok = value_result.get("returncode") == 0
    text = (
        f"## 🤖 {args.stock_name} Agent报告已生成\n\n"
        f"- 股票：{args.stock_name}（{args.symbol}）\n"
        f"- 日期：{args.date}\n"
        f"- 状态：{status}\n"
        f"- 价值层：{'已生成' if value_ok else '生成失败/待检查'}\n"
        f"- 完整报告：{paths['report']}\n"
        f"- 量化评分：{paths.get('quant', 'N/A')}\n"
        f"- 量化回测：{paths.get('quant_backtest', 'N/A')}\n"
        f"- 学习版：{paths['learn']}\n"
        f"- 原始数据：{paths['raw']}\n"
        f"- 服务器地址：{server_host}\n"
        f"- 登录方式：`ssh {server_user}@{server_host}`\n"
        f"- 项目目录：`{server_project}`\n\n"
        "> 仅提醒，不自动交易；请人工确认。"
    )
    print("DingTalk: %s" % send_markdown(webhook, f"{args.stock_name} Agent报告已生成", text, secret or None))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run full stock report and value layer.")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--stock-name", required=True)
    parser.add_argument("--date", default=now_cn().strftime("%Y-%m-%d"))
    parser.add_argument("--analysts", default=os.getenv("MONITOR_AGENT_ANALYSTS", "market,news,fundamentals"))
    parser.add_argument("--notify", action="store_true")
    args = parser.parse_args()

    symbol = args.symbol.strip()
    stock_name = args.stock_name.strip()
    analysts = [item.strip() for item in args.analysts.split(",") if item.strip()]
    stock_dir = analysis_stock_dir(symbol, stock_name)
    prefix = f"{safe_name(stock_name)}-{args.date}"
    paths = {
        "report": stock_dir / f"{prefix}-report.md",
        "learn": stock_dir / f"{prefix}-learn.md",
        "raw": stock_dir / f"{prefix}-raw.json",
        "status": stock_dir / f"{prefix}-run.status.json",
        "quant": stock_dir / f"{prefix}-quant.md",
        "quant_json": stock_dir / f"{prefix}-quant.json",
        "quant_backtest": stock_dir / f"{prefix}-quant-backtest.md",
        "quant_backtest_rows": stock_dir / f"{prefix}-quant-backtest-rows.csv",
        "quant_backtest_json": stock_dir / f"{prefix}-quant-backtest.json",
        "eastmoney_fundamentals": stock_dir / f"{prefix}-eastmoney-fundamentals.md",
        "eastmoney_news": stock_dir / f"{prefix}-eastmoney-news.md",
        "eastmoney_diagnose": stock_dir / f"{prefix}-eastmoney-diagnose.md",
    }

    write_status(paths["status"], {"status": "running", "symbol": symbol, "stock_name": stock_name, "date": args.date})
    value_result: Dict[str, Any] = {}
    try:
        eastmoney_appendix, eastmoney_skills = build_eastmoney_skills_appendix(symbol, stock_name, paths)
        eastmoney_skills_context = build_eastmoney_agent_context(eastmoney_appendix)
        graph, state, decision = run_graph(symbol, args.date, analysts, eastmoney_skills_context)
        if eastmoney_skills_context:
            state["eastmoney_skills_context"] = eastmoney_skills_context
        fundamentals_text = str(state.get("fundamentals_report") or "")
        fundamentals_text, fundamentals_quality = ensure_fundamentals_quality(symbol, fundamentals_text)
        state["fundamentals_report"] = fundamentals_text
        state, analysis_quality = ensure_analysis_sections(symbol, stock_name, args.date, state, graph.toolkit)
        fundamentals_text = str(state.get("fundamentals_report") or "")
        fundamentals_text, fundamentals_quality = ensure_fundamentals_quality(symbol, fundamentals_text)
        state["fundamentals_report"] = fundamentals_text
        raw = {
            "stock_symbol": symbol,
            "symbol": symbol,
            "stock_name": stock_name,
            "analysis_date": args.date,
            "analysts": analysts,
            "state": to_jsonable(state),
            "decision": to_jsonable(decision),
            "fundamentals_quality": fundamentals_quality,
            "analysis_quality": analysis_quality,
            "eastmoney_skills": eastmoney_skills,
            "success": True,
            "generated_at": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        }
        write_json(paths["raw"], raw)
        report_text = build_full_report(symbol, stock_name, args.date, state, decision, analysts)
        if eastmoney_appendix:
            report_text += "\n" + eastmoney_appendix
        learning_text = build_learning_report(symbol, stock_name, args.date, decision)
        ensure_no_truncation_markers({"report": report_text, "learn": learning_text})
        ensure_report_price_guard(report_text, fundamentals_text, label="full_report")
        paths["report"].write_text(report_text, encoding="utf-8")
        paths["learn"].write_text(learning_text, encoding="utf-8")
        quant_result = run_quant_layer(symbol, stock_name, args.date, fundamentals_text, paths)
        backtest_result = run_quant_backtest(symbol, args.date, fundamentals_text, stock_dir)
        for key, path_text in (backtest_result.get("paths") or {}).items():
            if key == "report":
                paths["quant_backtest"] = Path(path_text)
            elif key == "rows_csv":
                paths["quant_backtest_rows"] = Path(path_text)
            elif key == "summary_json":
                paths["quant_backtest_json"] = Path(path_text)
        value_result = run_value_layer(symbol, stock_name, args.date)
        if value_result.get("returncode") != 0:
            raise RuntimeError("价值层生成失败，完整报告不标记completed；请先修复价值层。" + str(value_result.get("stderr") or value_result.get("stdout") or "")[-1200:])
        write_status(
            paths["status"],
            {
                "status": "completed",
                "symbol": symbol,
                "stock_name": stock_name,
                "date": args.date,
                "paths": {key: str(path) for key, path in paths.items()},
                "value_layer": value_result,
                "quant_layer": quant_result,
                "quant_backtest": backtest_result,
                "fundamentals_quality": fundamentals_quality,
                "eastmoney_skills": eastmoney_skills,
            },
        )
        send_completion(args, paths, value_result, "已完成")
        for key, path in paths.items():
            print(f"{key}: {path}")
        print("value_layer_returncode=%s" % value_result.get("returncode"))
        print(value_result.get("stdout", ""))
    except Exception as exc:
        error_text = f"{type(exc).__name__}: {exc}"
        write_status(paths["status"], {"status": "failed", "error": error_text, "traceback": traceback.format_exc()[-4000:]})
        send_completion(args, paths, value_result, "失败：" + error_text)
        raise


if __name__ == "__main__":
    main()
