#!/usr/bin/env python3
"""Utilities for persistent Agent memory and post-report reflection."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional


def to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    return str(value)


def auto_reflect_enabled() -> bool:
    return os.getenv("MEMORY_AUTO_REFLECT", "true").lower() == "true"


def auto_reflect_fail_on_error() -> bool:
    return os.getenv("MEMORY_AUTO_REFLECT_FAIL_ON_ERROR", "true").lower() == "true"


def _memory_counts(graph: Any) -> Dict[str, Any]:
    memories = {
        "bull_memory": getattr(graph, "bull_memory", None),
        "bear_memory": getattr(graph, "bear_memory", None),
        "trader_memory": getattr(graph, "trader_memory", None),
        "invest_judge_memory": getattr(graph, "invest_judge_memory", None),
        "risk_manager_memory": getattr(graph, "risk_manager_memory", None),
    }
    counts: Dict[str, Any] = {}
    for name, memory in memories.items():
        if memory is None:
            counts[name] = {"enabled": False, "collection_count": 0}
            continue
        try:
            info = memory.get_cache_info()
            counts[name] = {
                "enabled": info.get("client_status") == "enabled",
                "collection_count": info.get("collection_count", 0),
                "embedding_model": info.get("embedding_model"),
                "provider": info.get("provider"),
            }
        except Exception as exc:
            counts[name] = {"enabled": False, "error": f"{type(exc).__name__}: {exc}"}
    return counts


def build_returns_losses(
    *,
    symbol: str,
    stock_name: str,
    analysis_date: str,
    final_state: Dict[str, Any],
    decision: Any,
    event: Optional[Dict[str, Any]] = None,
    stock: Optional[Dict[str, Any]] = None,
    report_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Build a truthful reflection payload.

    This is intentionally marked as pending outcome: at report time we do not yet
    know future P&L, so the reflector should store conditional lessons and
    verification rules rather than pretending the decision was proven right/wrong.
    """
    risk_debate = (final_state or {}).get("risk_debate_state") or {}
    investment_debate = (final_state or {}).get("investment_debate_state") or {}
    return {
        "reflection_type": "post_report_pre_outcome",
        "outcome_status": "pending_future_validation",
        "important_instruction": (
            "未来收益尚未验证。不要把本次决策判定为已经正确或错误；"
            "请提炼当前行情、触发条件、Agent 结论、风险线、反证条件、"
            "后续需要验证的数据，并写成下次类似情况可检索的经验。"
        ),
        "symbol": symbol,
        "stock_name": stock_name,
        "analysis_date": analysis_date,
        "event": to_jsonable(event or {}),
        "watchlist_plan": to_jsonable(stock or {}),
        "report_path": str(report_path) if report_path else "",
        "agent_decision": to_jsonable(decision),
        "trader_plan_excerpt": str((final_state or {}).get("trader_investment_plan") or "")[:4000],
        "risk_manager_excerpt": str(risk_debate.get("judge_decision") or "")[:4000],
        "research_manager_excerpt": str(investment_debate.get("judge_decision") or "")[:3000],
        "market_report_excerpt": str((final_state or {}).get("market_report") or "")[:3000],
        "news_report_excerpt": str((final_state or {}).get("news_report") or "")[:2000],
        "fundamentals_report_excerpt": str((final_state or {}).get("fundamentals_report") or "")[:3000],
    }


def run_auto_reflection(
    *,
    graph: Any,
    symbol: str,
    stock_name: str,
    analysis_date: str,
    final_state: Dict[str, Any],
    decision: Any,
    event: Optional[Dict[str, Any]] = None,
    stock: Optional[Dict[str, Any]] = None,
    report_path: Optional[Path] = None,
) -> Dict[str, Any]:
    if not auto_reflect_enabled():
        return {"enabled": False, "status": "disabled"}
    if graph is None or not hasattr(graph, "reflect_and_remember"):
        message = "graph has no reflect_and_remember"
        if auto_reflect_fail_on_error():
            raise RuntimeError(message)
        return {"enabled": True, "status": "failed", "error": message}

    payload = build_returns_losses(
        symbol=symbol,
        stock_name=stock_name,
        analysis_date=analysis_date,
        final_state=final_state,
        decision=decision,
        event=event,
        stock=stock,
        report_path=report_path,
    )
    before = _memory_counts(graph)
    try:
        graph.reflect_and_remember(payload)
    except Exception as exc:
        if auto_reflect_fail_on_error():
            raise RuntimeError(f"Memory自动复盘写入失败：{type(exc).__name__}: {exc}") from exc
        return {
            "enabled": True,
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "before": before,
            "returns_losses": to_jsonable(payload),
        }
    after = _memory_counts(graph)
    return {
        "enabled": True,
        "status": "completed",
        "before": before,
        "after": after,
        "returns_losses": to_jsonable(payload),
    }


def format_memory_reflection_section(result: Optional[Dict[str, Any]]) -> str:
    result = result or {}
    if not result:
        return "- 状态：未运行。"
    status = result.get("status")
    lines = [f"- 状态：{status or 'unknown'}"]
    if result.get("error"):
        lines.append(f"- 错误：{result.get('error')}")
    after = result.get("after") or {}
    if after:
        lines.append("- 写入后记忆条数：")
        for name, info in after.items():
            lines.append(f"  - {name}: {info.get('collection_count', 0)}")
    if result.get("returns_losses"):
        lines.append("- 复盘类型：post_report_pre_outcome（报告生成后、未来收益验证前）。")
    return "\n".join(lines)


def write_memory_status_file(path: Path, result: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2), encoding="utf-8")
