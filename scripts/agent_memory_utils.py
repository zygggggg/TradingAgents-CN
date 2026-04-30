#!/usr/bin/env python3
"""Utilities for persistent Agent memory and post-report reflection."""

from __future__ import annotations

import json
import os
import threading
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
    return False


def auto_reflect_timeout_seconds() -> int:
    raw = os.getenv("MEMORY_AUTO_REFLECT_TIMEOUT", "600")
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 600


def auto_reflect_mode() -> str:
    # hybrid: one high-quality LLM reflection + reliable Chroma write.
    # compact: deterministic, fast, writes structured lessons directly to Chroma.
    # llm: legacy deep reflection, may call the LLM once per role and can be slow.
    mode = os.getenv("MEMORY_AUTO_REFLECT_MODE", "hybrid").strip().lower() or "hybrid"
    if mode not in {"hybrid", "compact", "llm"}:
        return "hybrid"
    return mode


def _clip_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 20)].rstrip() + "……"


def _get_decision_text(decision: Any) -> str:
    if isinstance(decision, dict):
        for key in ("action", "decision", "final_decision", "recommendation", "signal"):
            if decision.get(key):
                return str(decision.get(key))
        return json.dumps(to_jsonable(decision), ensure_ascii=False)[:800]
    return str(decision or "")


def _compact_report_situation(
    *,
    symbol: str,
    stock_name: str,
    analysis_date: str,
    final_state: Dict[str, Any],
    decision: Any,
    event: Optional[Dict[str, Any]],
    stock: Optional[Dict[str, Any]],
    report_path: Optional[Path],
) -> str:
    event = event or {}
    stock = stock or {}
    risk_debate = (final_state or {}).get("risk_debate_state") or {}
    investment_debate = (final_state or {}).get("investment_debate_state") or {}
    parts = [
        f"股票: {stock_name}({symbol})",
        f"日期: {analysis_date}",
        f"复盘类型: post_report_pre_outcome，报告生成后、未来收益验证前",
        f"事件: {_clip_text(event.get('event') or event.get('type') or '', 80)}",
        f"事件价/当前价: {_clip_text(event.get('price') or event.get('current_price') or stock.get('current_price') or '', 80)}",
        f"触发原因: {_clip_text(event.get('reason') or event.get('message') or stock.get('note') or '', 500)}",
        f"最终决策: {_clip_text(_get_decision_text(decision), 800)}",
        f"交易计划: {_clip_text((final_state or {}).get('trader_investment_plan'), 1200)}",
        f"研究经理结论: {_clip_text(investment_debate.get('judge_decision'), 900)}",
        f"风险经理结论: {_clip_text(risk_debate.get('judge_decision'), 900)}",
        f"市场摘要: {_clip_text((final_state or {}).get('market_report'), 900)}",
        f"基本面摘要: {_clip_text((final_state or {}).get('fundamentals_report'), 900)}",
        f"报告路径: {report_path or ''}",
    ]
    return "\n".join(part for part in parts if part and not part.endswith(': '))[:7000]


def _compact_advice(role_name: str, situation: str) -> str:
    return (
        f"{role_name} 自动复盘经验：这是报告生成后的预验证记忆，不能把未来收益当作已发生事实。"
        "下次遇到类似触发时，优先复用本次的触发区间、仓位纪律、止损/减仓线、"
        "成交量确认、基本面质量门禁和反证条件；若价格跌破风控线或财务/新闻数据不完整，"
        "先控风险或补数据，不要让缺失数据进入正式分析。\n\n"
        f"可检索情境：\n{_clip_text(situation, 2500)}"
    )


def _build_hybrid_prompt(situation: str) -> list[tuple[str, str]]:
    system = (
        "你是交易复盘和经验库维护专家。你的任务是把本次报告写成未来可检索、可执行的经验。"
        "不要声称未来收益已经验证；只总结触发条件、关键证据、操作纪律、风险线、反证条件和下次如何复用。"
        "如果输入提到数据不完整，应明确写入：必须先补齐数据或中止分析。"
    )
    human = (
        "请基于以下情境生成一段高质量 Memory 经验，结构包含：\n"
        "1. 适用情境；2. 当时核心证据；3. 操作/仓位纪律；4. 风险线和反证；"
        "5. 下次复用规则；6. 数据质量门禁。\n"
        "控制在1200字以内，必须具体，不要泛泛而谈。\n\n"
        f"情境：\n{situation}"
    )
    return [("system", system), ("human", human)]


def _invoke_hybrid_reflection(graph: Any, situation: str, timeout_seconds: int) -> Dict[str, Any]:
    llm = getattr(graph, "quick_thinking_llm", None) or getattr(graph, "deep_thinking_llm", None)
    if llm is None:
        return {"status": "skipped", "error": "graph has no LLM client"}

    holder: Dict[str, Any] = {}

    def _worker() -> None:
        try:
            result = llm.invoke(_build_hybrid_prompt(situation))
            holder["content"] = str(getattr(result, "content", result) or "").strip()
        except Exception as exc:
            holder["error"] = exc

    worker = threading.Thread(target=_worker, name="memory-hybrid-reflect", daemon=True)
    worker.start()
    worker.join(timeout_seconds)
    if worker.is_alive():
        return {"status": "timeout", "error": f"Memory AI复盘超时({timeout_seconds}s)"}
    if holder.get("error") is not None:
        exc = holder["error"]
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    content = holder.get("content") or ""
    if not content:
        return {"status": "failed", "error": "Memory AI复盘返回空内容"}
    return {"status": "completed", "content": _clip_text(content, 5000)}


def _iter_graph_memories(graph: Any) -> Dict[str, Any]:
    return {
        "bull_memory": getattr(graph, "bull_memory", None),
        "bear_memory": getattr(graph, "bear_memory", None),
        "trader_memory": getattr(graph, "trader_memory", None),
        "invest_judge_memory": getattr(graph, "invest_judge_memory", None),
        "risk_manager_memory": getattr(graph, "risk_manager_memory", None),
    }


def _write_compact_reflection(graph: Any, situation: str, advice_text: Optional[str] = None) -> Dict[str, Any]:
    written = {}
    errors = {}
    role_labels = {
        "bull_memory": "多头研究员",
        "bear_memory": "空头研究员",
        "trader_memory": "交易员",
        "invest_judge_memory": "研究经理",
        "risk_manager_memory": "风险经理",
    }
    for name, memory in _iter_graph_memories(graph).items():
        if memory is None:
            written[name] = {"enabled": False, "written": False}
            continue
        try:
            before_count = 0
            try:
                before_count = int(memory.get_cache_info().get("collection_count", 0))
            except Exception:
                before_count = 0
            recommendation = advice_text or _compact_advice(role_labels.get(name, name), situation)
            if advice_text:
                recommendation = f"{role_labels.get(name, name)} 自动复盘经验：\n{_clip_text(advice_text, 5000)}"
            memory.add_situations([(situation, recommendation)])
            after_info = memory.get_cache_info()
            written[name] = {
                "enabled": True,
                "written": True,
                "before_count": before_count,
                "collection_count": after_info.get("collection_count", 0),
                "embedding_model": after_info.get("embedding_model"),
                "provider": after_info.get("provider"),
            }
        except Exception as exc:
            errors[name] = f"{type(exc).__name__}: {exc}"
            written[name] = {"enabled": True, "written": False, "error": errors[name]}
    if errors:
        return {"status": "failed", "written": written, "errors": errors}
    return {"status": "completed", "written": written}


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
    mode = auto_reflect_mode()

    situation = _compact_report_situation(
        symbol=symbol,
        stock_name=stock_name,
        analysis_date=analysis_date,
        final_state=final_state,
        decision=decision,
        event=event,
        stock=stock,
        report_path=report_path,
    )
    timeout_seconds = auto_reflect_timeout_seconds()

    if mode == "compact":
        compact_result = _write_compact_reflection(graph, situation)
        after = _memory_counts(graph)
        return {
            "enabled": True,
            "mode": "compact",
            "status": compact_result.get("status"),
            "before": before,
            "after": after,
            "written": compact_result.get("written"),
            "errors": compact_result.get("errors", {}),
            "returns_losses": to_jsonable(payload),
        }

    if mode == "hybrid":
        llm_result = _invoke_hybrid_reflection(graph, situation, timeout_seconds)
        advice_text = llm_result.get("content") if llm_result.get("status") == "completed" else None
        compact_result = _write_compact_reflection(graph, situation, advice_text=advice_text)
        after = _memory_counts(graph)
        status = compact_result.get("status")
        return {
            "enabled": True,
            "mode": "hybrid",
            "status": status,
            "llm_reflection": {k: v for k, v in llm_result.items() if k != "content"},
            "fallback_used": advice_text is None,
            "before": before,
            "after": after,
            "written": compact_result.get("written"),
            "errors": compact_result.get("errors", {}),
            "returns_losses": to_jsonable(payload),
        }

    holder: Dict[str, Any] = {}

    def _worker() -> None:
        try:
            graph.reflect_and_remember(payload)
            holder["after"] = _memory_counts(graph)
        except Exception as exc:
            holder["error"] = exc

    worker = threading.Thread(target=_worker, name="memory-auto-reflect", daemon=True)
    worker.start()
    worker.join(timeout_seconds)
    if worker.is_alive():
        message = f"Memory自动复盘写入超时({timeout_seconds}s)，已跳过以避免阻塞报告生成"
        return {
            "enabled": True,
            "mode": "llm",
            "status": "timeout",
            "error": message,
            "before": before,
            "returns_losses": to_jsonable(payload),
        }

    if holder.get("error") is not None:
        exc = holder["error"]
        return {
            "enabled": True,
            "mode": "llm",
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "before": before,
            "returns_losses": to_jsonable(payload),
        }

    after = holder.get("after") or _memory_counts(graph)
    return {
        "enabled": True,
        "mode": "llm",
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
    if result.get("mode"):
        lines.append(f"- 模式：{result.get('mode')}")
    if result.get("llm_reflection"):
        llm_status = result.get("llm_reflection", {}).get("status")
        lines.append(f"- AI复盘状态：{llm_status or 'unknown'}")
    if result.get("fallback_used"):
        lines.append("- 兜底：AI复盘未完成，已写入结构化经验，Memory不空转。")
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
