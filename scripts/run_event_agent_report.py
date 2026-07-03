#!/usr/bin/env python3
"""Run a full TradingAgents-CN analysis for a triggered monitor event."""

import argparse
import json
import os
import re
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.explain_monitor_event import collect_report_context, fetch_recent_klines, format_klines
from scripts.send_dingtalk import send_markdown
from tradingagents.dataflows.fundamentals_quality import ensure_fundamentals_quality
from tradingagents.dataflows.report_quality import ensure_analysis_sections


def cn_timezone():
    if ZoneInfo is not None:
        return ZoneInfo("Asia/Shanghai")
    return timezone(timedelta(hours=8))


def now_cn():
    return datetime.now(cn_timezone())


def load_env_file(path: Path, override: bool = False) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and (override or key not in os.environ):
            os.environ[key] = value


def safe_filename(value: Any) -> str:
    text = str(value or "").strip() or "unknown"
    return re.sub(r'[\\/:*?"<>|\s]+', "-", text).strip("-") or "unknown"


def normalize_date(value: Any) -> str:
    text = str(value or "").strip()
    if re.match(r"^\d{8}$", text):
        return "%s-%s-%s" % (text[:4], text[4:6], text[6:8])
    if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        return text
    return now_cn().strftime("%Y-%m-%d")


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    return str(value)


def clip(text: Any, limit: int | None = None) -> str:
    # 用户可见报告禁止出现省略占位；完整保留已生成段落。
    return str(text or "").strip()


TRUNCATION_FORBIDDEN_PATTERNS = ("原文过长", "已截断", "内容过长", "内容已截断", "数据已截断", "truncated")
PRICE_DERIVATION_FORBIDDEN_PATTERNS = (
    "隐含",
    "反推",
    "PE×EPS",
    "PE × EPS",
    "EPS×PE",
    "EPS × PE",
    "PE与EPS互算",
    "EPS与PE互算",
)


def ensure_no_truncation_markers(text: str, output_path: Path) -> None:
    hits = [marker for marker in TRUNCATION_FORBIDDEN_PATTERNS if marker in text]
    if hits:
        raise RuntimeError(f"事件报告包含截断标记 {hits}: {output_path}")


def _is_forbidden_price_derivation_line(line: str) -> bool:
    if any(marker in line for marker in PRICE_DERIVATION_FORBIDDEN_PATTERNS):
        return True
    if "÷" in line and any(term in line for term in ("PE", "PB", "市盈率", "市净率", "收盘价", "当前价", "最新价")):
        return True
    if "测算" in line and any(term in line for term in ("PE", "PB", "市盈率", "市净率", "每股收益", "每股净资产")):
        return True
    return False


def sanitize_report_text(text: Any) -> str:
    lines = []
    skip_next_quote_formula = False
    for line in str(text or "").splitlines():
        if any(marker in line for marker in TRUNCATION_FORBIDDEN_PATTERNS):
            continue
        if _is_forbidden_price_derivation_line(line):
            skip_next_quote_formula = True
            continue
        if skip_next_quote_formula and line.strip().startswith(">"):
            continue
        skip_next_quote_formula = False
        lines.append(line)
    return "\n".join(lines).strip()


def sanitize_prior_context(text: Any) -> str:
    forbidden = ("隐含当前价格", "隐含价格", "反推", "PE×EPS", "PE × EPS", "EPS×PE", "EPS × PE")
    lines = []
    for line in str(text or "").splitlines():
        if any(item in line for item in forbidden):
            continue
        if any(marker in line for marker in TRUNCATION_FORBIDDEN_PATTERNS):
            continue
        if _is_forbidden_price_derivation_line(line):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def sanitize_status_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): sanitize_status_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_status_payload(item) for item in value]
    if isinstance(value, str):
        return sanitize_report_text(value)
    return value

def derive_paths(event: Dict[str, Any], stock: Dict[str, Any], quick_report: Optional[Path]) -> Dict[str, Path]:
    stock_name = safe_filename(event.get("name") or stock.get("name") or event.get("symbol") or stock.get("symbol"))
    quote_date = normalize_date(event.get("quote_date") or stock.get("plan_date"))
    event_name = safe_filename(event.get("event") or "event")
    if quick_report:
        report_dir = quick_report.parent
        stem = quick_report.stem
        if stem.endswith("-alert"):
            base_stem = stem[:-6]
        else:
            base_stem = stem
        report_path = report_dir / (base_stem + "-agent-report.md")
    else:
        run_time = now_cn().strftime("%H%M")
        report_dir = ROOT / "analysis_outputs" / stock_name / "alerts" / quote_date
        report_path = report_dir / ("%s-%s-%s-%s-agent-report.md" % (stock_name, quote_date, run_time, event_name))
    report_dir.mkdir(parents=True, exist_ok=True)
    return {
        "report": report_path,
        "state": report_path.with_suffix(".state.json"),
        "status": report_path.with_suffix(".status.json"),
    }


def write_status(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = sanitize_status_payload(dict(payload))
    payload["updated_at"] = now_cn().isoformat()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def build_agent_config() -> Dict[str, Any]:
    load_env_file(ROOT / ".env")
    load_env_file(ROOT / ".stock-monitor.env", override=True)

    monitor_api_key = os.getenv("MONITOR_AI_API_KEY") or os.getenv("CUSTOM_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
    if monitor_api_key:
        os.environ.setdefault("CUSTOM_OPENAI_API_KEY", monitor_api_key)
        os.environ.setdefault("OPENAI_API_KEY", monitor_api_key)
    os.environ.setdefault(
        "OPENAI_COMPAT_USER_AGENT",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    )

    from tradingagents.default_config import DEFAULT_CONFIG

    base_url = os.getenv("MONITOR_AI_BASE_URL") or os.getenv("CUSTOM_OPENAI_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1"
    model = os.getenv("MONITOR_AGENT_MODEL") or os.getenv("MONITOR_AI_MODEL") or os.getenv("TA_LLM_MODEL") or "gpt-5.5"
    config = DEFAULT_CONFIG.copy()
    config.update(
        {
            "project_dir": str(ROOT),
            "results_dir": str(ROOT / "results"),
            "llm_provider": os.getenv("MONITOR_AGENT_PROVIDER", "custom_openai"),
            "deep_think_llm": os.getenv("MONITOR_AGENT_DEEP_MODEL", model),
            "quick_think_llm": os.getenv("MONITOR_AGENT_QUICK_MODEL", model),
            "backend_url": base_url,
            "custom_openai_base_url": base_url,
            "max_debate_rounds": int(os.getenv("MONITOR_AGENT_DEBATE_ROUNDS", "1")),
            "max_risk_discuss_rounds": int(os.getenv("MONITOR_AGENT_RISK_ROUNDS", "1")),
            "online_tools": os.getenv("MONITOR_AGENT_ONLINE_TOOLS", "true").lower() == "true",
            "online_news": os.getenv("MONITOR_AGENT_ONLINE_NEWS", "true").lower() == "true",
            "realtime_data": os.getenv("MONITOR_AGENT_REALTIME_DATA", "true").lower() == "true",
            "memory_enabled": os.getenv("MEMORY_ENABLED", "true").lower() == "true",
            "quick_model_config": {
                "temperature": float(os.getenv("MONITOR_AGENT_TEMPERATURE", "0.2")),
                "max_tokens": int(os.getenv("MONITOR_AGENT_MAX_TOKENS", "2500")),
                "timeout": int(os.getenv("MONITOR_AGENT_TIMEOUT", "180")),
            },
            "deep_model_config": {
                "temperature": float(os.getenv("MONITOR_AGENT_TEMPERATURE", "0.2")),
                "max_tokens": int(os.getenv("MONITOR_AGENT_MAX_TOKENS", "3500")),
                "timeout": int(os.getenv("MONITOR_AGENT_TIMEOUT", "240")),
            },
        }
    )
    return config


def run_tradingagents(symbol: str, analysis_date: str) -> Dict[str, Any]:
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    selected = [item.strip() for item in os.getenv("MONITOR_AGENT_ANALYSTS", "market,news,fundamentals").split(",") if item.strip()]
    graph = TradingAgentsGraph(selected_analysts=selected, debug=False, config=build_agent_config())
    final_state, decision = graph.propagate(symbol, analysis_date)
    return {"graph": graph, "final_state": final_state, "decision": decision, "selected_analysts": selected}


def summarize_memory_status() -> str:
    """Return a human-readable summary of the Agent memory/experience library status."""
    try:
        from tradingagents.agents.utils.memory import FinancialSituationMemory

        config = build_agent_config()
        memory = FinancialSituationMemory("risk_manager_memory", config)
        info = memory.get_cache_info()
        provider = info.get("provider") or config.get("llm_provider") or "unknown"
        embedding_model = info.get("embedding_model") or "unknown"
        client_status = info.get("client_status") or "unknown"
        collection_count = info.get("collection_count", 0)
        if client_status == "enabled" and collection_count:
            status = "可用，已接入历史经验库。"
        elif client_status == "enabled":
            status = "可用，但当前经验库为空或样本较少。"
        else:
            status = "不可用/降级，本次主要依赖历史报告和实时数据。"
        return "\n".join([
            "- Memory用途：检索过去类似行情下 Agent 的经验教训，不替代原始历史报告。",
            f"- 当前状态：{status}",
            f"- Provider：{provider}",
            f"- Embedding模型：{embedding_model}",
            f"- 已存经验条数：{collection_count}",
            "- 使用原则：历史报告提供精确计划，Memory提供相似案例经验，实时行情提供当前事实。",
        ])
    except Exception as exc:
        return "\n".join([
            "- Memory用途：检索过去类似行情下 Agent 的经验教训，不替代原始历史报告。",
            "- 当前状态：读取失败/降级，本次主要依赖历史报告和实时数据。",
            f"- 失败原因：{type(exc).__name__}: {str(exc)[:300]}",
            "- 使用原则：历史报告提供精确计划，Memory提供相似案例经验，实时行情提供当前事实。",
        ])


def build_markdown(event: Dict[str, Any], stock: Dict[str, Any], quick_report: Optional[Path], result: Dict[str, Any]) -> str:
    symbol = str(event.get("symbol") or stock.get("symbol") or "")
    stock_name = str(event.get("name") or stock.get("name") or symbol)
    analysis_date = normalize_date(event.get("quote_date") or stock.get("plan_date"))
    final_state = result.get("final_state") or {}
    decision = result.get("decision") or {}
    klines = fetch_recent_klines(symbol, 20)
    prior_context = sanitize_prior_context(collect_report_context(ROOT, symbol, stock_name, stock.get("plan_date") or analysis_date, total_limit=10000))
    memory_status = summarize_memory_status()

    investment_debate = final_state.get("investment_debate_state") or {}
    risk_debate = final_state.get("risk_debate_state") or {}
    sections = [
        "# %s 关键事件完整 Agent 报告" % stock_name,
        "",
        "> 这是 TradingAgents-CN 完整 Agent 在关键事件触发后重新跑出的事件报告；仅用于学习和辅助风控，不自动交易。",
        "",
        "## 1. 触发事件",
        "",
        "- 生成时间：%s" % now_cn().strftime("%Y-%m-%d %H:%M:%S %Z"),
        "- 股票：%s（%s）" % (stock_name, symbol),
        "- 分析日期：%s" % analysis_date,
        "- 事件：%s" % event.get("event"),
        "- 严重程度：%s" % event.get("severity"),
        "- 当前价：%s" % event.get("price"),
        "- 成交量：%s" % event.get("volume"),
        "- 数据源：%s" % (event.get("source") or "N/A"),
        "- 快速事件报告：%s" % (quick_report if quick_report else "N/A"),
        "",
        "### 触发规则原文",
        "",
        clip(event.get("body"), 5000),
        "",
        "## 2. 本次自动处理流程",
        "",
        "1. 先获取当前价格、最近走势、成交量和技术变化。",
        "2. 再读取之前完整报告里的关键计划，例如跌破/突破/止盈/止损条件。",
        "3. 同时检查 Memory 经验库，检索过去类似案例和 Agent 经验教训。",
        "4. 然后让完整 TradingAgents-CN Agent 基于新数据重新跑一遍。",
        "5. 最后生成新的事件报告，并通过钉钉通知报告路径。",
        "",
        "## 3. 最近走势数据",
        "",
        format_klines(klines),
        "",
        "## 4. 原有报告/计划摘录",
        "",
        clip(prior_context, 10000),
        "",
        "## 5. Memory 经验库状态",
        "",
        memory_status,
        "",
        "## 6. TradingAgents-CN 最终决策",
        "",
        "```json\n%s\n```" % json.dumps(to_jsonable(decision), ensure_ascii=False, indent=2),
        "",
        "## 7. 市场/技术分析师报告",
        "",
        clip(final_state.get("market_report"), 12000),
        "",
        "## 8. 新闻分析师报告",
        "",
        clip(final_state.get("news_report"), 10000),
        "",
        "## 9. 基本面分析师报告",
        "",
        clip(final_state.get("fundamentals_report"), 12000),
        "",
        "## 10. 多空研究员辩论",
        "",
        "### Bull Researcher",
        "",
        clip(investment_debate.get("bull_history"), 8000),
        "",
        "### Bear Researcher",
        "",
        clip(investment_debate.get("bear_history"), 8000),
        "",
        "### Research Manager Decision",
        "",
        clip(investment_debate.get("judge_decision") or final_state.get("investment_plan"), 8000),
        "",
        "## 11. 交易员执行计划",
        "",
        clip(final_state.get("trader_investment_plan"), 10000),
        "",
        "## 12. 风险委员会",
        "",
        "### Risky Analyst",
        "",
        clip(risk_debate.get("risky_history"), 7000),
        "",
        "### Safe Analyst",
        "",
        clip(risk_debate.get("safe_history"), 7000),
        "",
        "### Neutral Analyst",
        "",
        clip(risk_debate.get("neutral_history"), 7000),
        "",
        "### Risk Manager Final Decision",
        "",
        clip(risk_debate.get("judge_decision") or final_state.get("final_trade_decision"), 10000),
        "",
        "---",
        "",
        "再次提醒：这是学习和辅助风控报告，不是保证收益的投资建议；不要自动交易，请人工确认。",
    ]
    return "\n".join(sections).strip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run full TradingAgents-CN report for a triggered event.")
    parser.add_argument("--event-json", required=True)
    parser.add_argument("--stock-json", required=True)
    parser.add_argument("--quick-report", default="")
    parser.add_argument("--notify", action="store_true")
    args = parser.parse_args()

    event_path = Path(args.event_json)
    stock_path = Path(args.stock_json)
    quick_report = Path(args.quick_report) if args.quick_report else None
    event = load_json(event_path)
    stock = load_json(stock_path)
    paths = derive_paths(event, stock, quick_report)
    symbol = str(event.get("symbol") or stock.get("symbol") or "")
    analysis_date = normalize_date(event.get("quote_date") or stock.get("plan_date"))
    stock_name = str(event.get("name") or stock.get("name") or symbol)

    write_status(paths["status"], {"status": "running", "symbol": symbol, "stock_name": stock_name, "analysis_date": analysis_date})
    try:
        result = run_tradingagents(symbol, analysis_date)
        final_state = dict(result.get("final_state") or {})
        fundamentals_text = str(final_state.get("fundamentals_report") or "")
        fundamentals_text, fundamentals_quality = ensure_fundamentals_quality(symbol, fundamentals_text)
        final_state["fundamentals_report"] = fundamentals_text
        try:
            from tradingagents.graph.trading_graph import TradingAgentsGraph
            repair_graph = TradingAgentsGraph(selected_analysts=["market", "news", "fundamentals"], debug=False, config=build_agent_config())
            final_state, analysis_quality = ensure_analysis_sections(symbol, stock_name, analysis_date, final_state, repair_graph.toolkit)
        except Exception:
            raise
        fundamentals_text = str(final_state.get("fundamentals_report") or "")
        fundamentals_text, fundamentals_quality = ensure_fundamentals_quality(symbol, fundamentals_text)
        final_state["fundamentals_report"] = fundamentals_text
        result["final_state"] = final_state
        result["quality"] = {
            "fundamentals_quality": fundamentals_quality,
            "analysis_quality": analysis_quality,
        }
        paths["state"].write_text(
            json.dumps(
                {
                    "event": to_jsonable(event),
                    "stock": to_jsonable(stock),
                    "decision": to_jsonable(result.get("decision")),
                    "selected_analysts": result.get("selected_analysts"),
                    "final_state_subset": to_jsonable(
                        {
                            key: (result.get("final_state") or {}).get(key)
                            for key in [
                                "company_of_interest",
                                "trade_date",
                                "market_report",
                                "news_report",
                                "fundamentals_report",
                                "investment_debate_state",
                                "investment_plan",
                                "trader_investment_plan",
                                "risk_debate_state",
                                "final_trade_decision",
                                "performance_metrics",
                            ]
                        }
                    ),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        markdown = build_markdown(event, stock, quick_report, result)
        markdown = sanitize_report_text(markdown) + "\n"
        ensure_no_truncation_markers(markdown, paths["report"])
        paths["report"].write_text(markdown, encoding="utf-8")
        write_status(paths["status"], {"status": "completed", "report_path": str(paths["report"]), "state_path": str(paths["state"])})
        print("agent_report=%s" % paths["report"])
        if args.notify:
            load_env_file(ROOT / ".stock-monitor.env")
            webhook = os.getenv("DINGTALK_WEBHOOK", "")
            secret = os.getenv("DINGTALK_SECRET", "")
            if webhook:
                server_host = os.getenv("MONITOR_SERVER_HOST", "49.235.148.184")
                server_user = os.getenv("MONITOR_SERVER_USER", "ubuntu")
                server_project = os.getenv("MONITOR_SERVER_PROJECT", str(ROOT))
                text = "## 🤖 %s 完整Agent事件报告已生成\n\n- 股票：%s（%s）\n- 事件：%s\n- 报告路径：%s\n- 服务器地址：%s\n- 登录方式：`ssh %s@%s`\n- 项目目录：`%s`\n\n> 不在钉钉群明文发送服务器密码；仅提醒，不自动交易，请人工确认。" % (
                    stock_name,
                    stock_name,
                    symbol,
                    event.get("event"),
                    paths["report"],
                    server_host,
                    server_user,
                    server_host,
                    server_project,
                )
                print("DingTalk: %s" % send_markdown(webhook, "%s Agent报告已生成" % stock_name, text, secret or None))
    except Exception as exc:
        error_text = "%s: %s" % (type(exc).__name__, exc)
        write_status(paths["status"], {"status": "failed", "error": error_text, "traceback": traceback.format_exc()[-4000:]})
        print("agent_report_failed=%s" % error_text)
        if args.notify:
            load_env_file(ROOT / ".stock-monitor.env")
            webhook = os.getenv("DINGTALK_WEBHOOK", "")
            secret = os.getenv("DINGTALK_SECRET", "")
            if webhook:
                text = "## ⚠️ %s 完整Agent事件报告生成失败\n\n- 股票：%s（%s）\n- 事件：%s\n- 错误：%s\n- 状态文件：%s" % (
                    stock_name,
                    stock_name,
                    symbol,
                    event.get("event"),
                    error_text[:300],
                    paths["status"],
                )
                print("DingTalk: %s" % send_markdown(webhook, "%s Agent报告失败" % stock_name, text, secret or None))
        raise


if __name__ == "__main__":
    main()
