"""Helpers for passing external Skills data through agent prompts."""

from __future__ import annotations

import os
from typing import Any, Mapping


DEFAULT_CONTEXT_CHARS = 24000


def _context_limit() -> int:
    raw = os.getenv("EASTMONEY_SKILLS_AGENT_CONTEXT_CHARS", str(DEFAULT_CONTEXT_CHARS))
    try:
        return int(raw)
    except Exception:
        return DEFAULT_CONTEXT_CHARS


def get_eastmoney_skills_context(state: Mapping[str, Any] | None) -> str:
    if not isinstance(state, Mapping):
        return ""
    text = str(state.get("eastmoney_skills_context") or "").strip()
    if not text:
        return ""
    limit = _context_limit()
    if limit > 0 and len(text) > limit:
        return text[:limit].rstrip()
    return text


def format_eastmoney_skills_context_block(state: Mapping[str, Any] | None) -> str:
    text = get_eastmoney_skills_context(state)
    if not text:
        return ""
    return (
        "\n\n## 东方财富 Skills 前置上下文\n"
        "以下数据来自东方财富 Skills / OpenClaw，请优先用于校验行情、估值、资金流、"
        "财务质量、新闻催化、支撑压力位和风险收益比；若与其他来源冲突，请显式说明差异。\n\n"
        f"{text}\n"
    )
