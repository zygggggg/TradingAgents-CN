#!/usr/bin/env python3
"""Run Memory auto-reflection from a saved event/full report state file."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.agent_memory_utils import run_auto_reflection, write_memory_status_file
from scripts.run_event_agent_report import build_agent_config, load_env_file, normalize_date, to_jsonable


class MemoryOnlyGraph:
    def __init__(self, config: Dict[str, Any]):
        from tradingagents.agents.utils.memory import FinancialSituationMemory

        self.config = dict(config)
        self.quick_thinking_llm = None
        self.deep_thinking_llm = None
        mode = os.getenv("MEMORY_AUTO_REFLECT_MODE", "hybrid").strip().lower() or "hybrid"
        if mode != "compact":
            try:
                from tradingagents.graph.trading_graph import create_llm_by_provider

                provider = self.config.get("llm_provider", "custom_openai")
                quick_config = self.config.get("quick_model_config", {})
                self.quick_thinking_llm = create_llm_by_provider(
                    provider=provider,
                    model=self.config.get("quick_think_llm") or self.config.get("deep_think_llm"),
                    backend_url=self.config.get("backend_url") or self.config.get("custom_openai_base_url"),
                    temperature=quick_config.get("temperature", 0.2),
                    max_tokens=quick_config.get("max_tokens", 2500),
                    timeout=quick_config.get("timeout", 180),
                    api_key=os.getenv("CUSTOM_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY"),
                )
                self.deep_thinking_llm = self.quick_thinking_llm
            except Exception:
                if mode == "llm":
                    raise
                self.quick_thinking_llm = None
                self.deep_thinking_llm = None

        self.bull_memory = FinancialSituationMemory("bull_memory", self.config)
        self.bear_memory = FinancialSituationMemory("bear_memory", self.config)
        self.trader_memory = FinancialSituationMemory("trader_memory", self.config)
        self.invest_judge_memory = FinancialSituationMemory("invest_judge_memory", self.config)
        self.risk_manager_memory = FinancialSituationMemory("risk_manager_memory", self.config)

    def reflect_and_remember(self, returns_losses):
        raise RuntimeError("MemoryOnlyGraph does not support legacy llm reflection; use hybrid or compact mode")


def load_state(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True, help="Path to *.state.json or raw full report json")
    parser.add_argument("--output", help="Path to write *.memory.json; default derives from state path")
    parser.add_argument("--mode", choices=["hybrid", "compact", "llm"], default=None)
    parser.add_argument("--timeout", type=int, default=None)
    args = parser.parse_args()

    load_env_file(ROOT / ".env")
    load_env_file(ROOT / ".stock-monitor.env", override=True)
    if args.mode:
        os.environ["MEMORY_AUTO_REFLECT_MODE"] = args.mode
    if args.timeout:
        os.environ["MEMORY_AUTO_REFLECT_TIMEOUT"] = str(args.timeout)

    state_path = Path(args.state)
    data = load_state(state_path)
    event = data.get("event") or {}
    stock = data.get("stock") or {}
    final_state = data.get("final_state") or data.get("final_state_subset") or (data.get("state") or {})
    decision = data.get("decision") or data.get("final_trade_decision") or final_state.get("final_trade_decision")

    symbol = str(data.get("symbol") or data.get("stock_symbol") or event.get("symbol") or stock.get("symbol") or "").strip()
    stock_name = str(data.get("stock_name") or event.get("name") or stock.get("name") or symbol or "unknown").strip()
    analysis_date = normalize_date(data.get("analysis_date") or event.get("quote_date") or stock.get("plan_date"))
    report_path = Path(str(data.get("report_path") or event.get("report_path") or "")) if (data.get("report_path") or event.get("report_path")) else None
    output_path = Path(args.output) if args.output else state_path.with_suffix(".memory.json")

    if not symbol:
        raise SystemExit("missing symbol in state file")

    graph = MemoryOnlyGraph(build_agent_config())
    result = run_auto_reflection(
        graph=graph,
        symbol=symbol,
        stock_name=stock_name,
        analysis_date=analysis_date,
        final_state=final_state,
        decision=decision,
        event=event,
        stock=stock,
        report_path=report_path,
    )
    write_memory_status_file(output_path, result)
    print(json.dumps(to_jsonable({"memory_path": str(output_path), "result": result}), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
