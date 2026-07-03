#!/usr/bin/env python3
"""Generate long-term value-investing reports on top of TradingAgents-CN outputs.

This is a sidecar integration layer inspired by ai-hedge-fund's value-investing
agents. It keeps TradingAgents-CN as the A-share data/trading-analysis base, then
adds a long-term value panel and a combined long/short decision bridge.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from report_paths import analysis_stock_dir, candidate_analysis_dirs, candidate_result_dirs, safe_name

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv(path):
        env_path = Path(path)
        if not env_path.exists():
            return False
        for raw_line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)
        return True

from langchain_core.messages import HumanMessage, SystemMessage

from tradingagents.llm_clients.openai_client import OpenAIClient
from tradingagents.dataflows.fundamentals_quality import ensure_fundamentals_quality

ROOT = Path(__file__).resolve().parents[1]
ANALYSIS_OUTPUTS = ROOT / "analysis_outputs"
RESULTS_DIR = ROOT / "results"


@dataclass(frozen=True)
class AgentProfile:
    key: str
    name: str
    role: str
    focus: str


VALUE_AGENTS = [
    AgentProfile(
        "ben_graham",
        "Ben Graham Agent（格雷厄姆）",
        "安全边际和低估值审查员",
        "盈利稳定性、资产负债安全、保守估值、是否足够便宜。",
    ),
    AgentProfile(
        "warren_buffett",
        "Warren Buffett Agent（巴菲特）",
        "好公司长期持有审查员",
        "商业模式、护城河、资本回报、长期确定性、管理层质量。",
    ),
    AgentProfile(
        "charlie_munger",
        "Charlie Munger Agent（芒格）",
        "高质量企业和反脆弱审查员",
        "企业质量、竞争优势、激励机制、反脆弱性、避免愚蠢风险。",
    ),
    AgentProfile(
        "michael_burry",
        "Michael Burry Agent（Burry）",
        "逆向深度价值审查员",
        "市场是否过度悲观、隐藏风险、资产/现金流错配、反向机会。",
    ),
    AgentProfile(
        "mohnish_pabrai",
        "Mohnish Pabrai Agent（Pabrai）",
        "低风险高赔率审查员",
        "下行风险、赔率、复制优秀投资逻辑、是否符合低风险高不确定性。",
    ),
    AgentProfile(
        "fundamentals",
        "Fundamentals Agent（基本面）",
        "财务质量审查员",
        "收入、利润、ROE/ROIC、现金流、负债、应收、商誉、分红。",
    ),
    AgentProfile(
        "valuation",
        "Valuation Agent（估值）",
        "内在价值估算员",
        "PE/PB/PS、DCF/情景估值、合理价值区间、安全边际。",
    ),
    AgentProfile(
        "risk_manager",
        "Risk Manager（长期风险）",
        "长期风险控制员",
        "仓位上限、卖出条件、买入逻辑失效条件、估值和财务红线。",
    ),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate TradingAgents-CN + value-investing layer reports.")
    parser.add_argument("--symbol", required=True, help="A-share symbol, e.g. 603588")
    parser.add_argument("--stock-name", default="", help="Stock name, e.g. 高能环境")
    parser.add_argument("--date", default="", help="Analysis date, e.g. 2026-04-29. Default: latest raw/report")
    parser.add_argument("--model", default=os.getenv("CUSTOM_OPENAI_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-5.5")
    parser.add_argument("--provider", default="custom_openai", choices=["custom_openai", "openai", "deepseek", "qwen", "aihubmix", "openrouter"])
    parser.add_argument("--base-url", default=os.getenv("CUSTOM_OPENAI_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "")
    parser.add_argument("--max-tokens", type=int, default=5000)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--timeout", type=int, default=int(os.getenv("VALUE_LAYER_TIMEOUT", "240")))
    parser.add_argument("--retries", type=int, default=int(os.getenv("VALUE_LAYER_RETRIES", "3")))
    parser.add_argument("--out-dir", default=str(ANALYSIS_OUTPUTS))
    parser.add_argument("--no-llm", action="store_true", help="Only compose deterministic scaffold from existing reports")
    parser.add_argument("--allow-fallback", action="store_true", help="Allow deterministic fallback value scaffold when LLM is unavailable")
    return parser.parse_args()


def normalize_symbol(symbol: str) -> str:
    match = re.search(r"\d{6}", str(symbol))
    if not match:
        raise ValueError(f"Invalid A-share symbol: {symbol}")
    return match.group(0)


def compact_date(date: str) -> str:
    return date.replace("-", "")


def read_text(path: Optional[Path]) -> str:
    if not path or not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def load_json(path: Optional[Path]) -> dict[str, Any]:
    if not path or not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def find_latest_raw(symbol: str, date: str, stock_name: str = "") -> Optional[Path]:
    patterns: list[str] = []
    if date:
        patterns.extend([f"*{symbol}*{compact_date(date)}*raw.json", f"*{symbol}*{date}*raw.json", f"*{date}*raw.json"])
    else:
        patterns.extend([f"*{symbol}*raw.json", "*-raw.json", "*_raw.json"])
    candidates: list[Path] = []
    for analysis_dir in candidate_analysis_dirs(symbol, stock_name):
        for pattern in patterns:
            candidates.extend(analysis_dir.glob(pattern))
    candidates = [path for path in candidates if path.is_file() and not path.name.endswith("-value-raw.json")]
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def find_result_reports(symbol: str, date: str, stock_name: str = "") -> dict[str, Path]:
    names = [
        "market_report",
        "fundamentals_report",
        "final_trade_decision",
        "risk_management_decision",
        "investment_plan",
        "trader_investment_plan",
        "research_team_decision",
    ]
    reports: dict[str, Path] = {}
    for result_dir in candidate_result_dirs(symbol, stock_name):
        if date:
            report_dirs = [result_dir / date / "reports"]
        else:
            dated = sorted([path for path in result_dir.iterdir() if path.is_dir()], reverse=True) if result_dir.exists() else []
            report_dirs = [dated[0] / "reports"] if dated else []
        for report_dir in report_dirs:
            for name in names:
                path = report_dir / f"{name}.md"
                if path.exists() and name not in reports:
                    reports[name] = path
        if reports:
            break
    return reports


def infer_date(raw_path: Optional[Path], raw: dict[str, Any], requested_date: str) -> str:
    if requested_date:
        return requested_date
    if raw.get("analysis_date"):
        return str(raw["analysis_date"])
    if raw_path:
        match = re.search(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})", raw_path.name)
        if match:
            return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    return datetime.now().strftime("%Y-%m-%d")


def infer_stock_name(symbol: str, explicit_name: str, raw: dict[str, Any], texts: list[str]) -> str:
    if explicit_name:
        return explicit_name
    candidates = [raw.get("stock_name"), raw.get("name")]
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()
    joined = "\n".join(texts)
    patterns = [
        rf"#\s*([^\n（(]+)[（(]{symbol}[）)]",
        rf"公司名称[：:]\s*([^\n]+)",
        rf"([\u4e00-\u9fffA-Za-z0-9]+)[（(]{symbol}[）)]",
    ]
    for pattern in patterns:
        match = re.search(pattern, joined)
        if match:
            name = re.sub(r"[*#`\s]", "", match.group(1)).strip()
            if name and len(name) <= 12 and not name.isdigit():
                return name
    return f"股票{symbol}"


def get_state_text(raw: dict[str, Any], key: str) -> str:
    state = raw.get("state") if isinstance(raw.get("state"), dict) else {}
    value = state.get(key) or raw.get(key) or ""
    if isinstance(value, str):
        return value
    if value:
        return json.dumps(value, ensure_ascii=False, indent=2)
    return ""


TRUNCATION_FORBIDDEN_PATTERNS = (
    "原文过长",
    "内容过长",
    "已截断",
    "内容已截断",
    "数据已截断",
    "truncated",
)


def clip(text: str, max_chars: int | None = None) -> str:
    del max_chars
    return re.sub(r"\n{3,}", "\n\n", str(text or "").strip())


def ensure_no_truncation_markers(outputs: dict[str, str]) -> None:
    violations: list[str] = []
    for name, text in outputs.items():
        for pattern in TRUNCATION_FORBIDDEN_PATTERNS:
            if pattern in text:
                violations.append(f"{name}: {pattern}")
    if violations:
        raise RuntimeError("报告包含截断标记，禁止写出：" + "; ".join(violations))


def extract_market_price(*texts: str) -> float | None:
    patterns = [
        r"当前行情价格\*{0,2}\s*[:：]\s*[¥￥]?\s*([0-9]+(?:\.[0-9]+)?)",
        r"当前价格\*{0,2}\s*[:：]\s*[¥￥]?\s*([0-9]+(?:\.[0-9]+)?)",
        r"当前价\*{0,2}\s*[:：]\s*[¥￥]?\s*([0-9]+(?:\.[0-9]+)?)",
        r"最新价\*{0,2}\s*[:：]\s*[¥￥]?\s*([0-9]+(?:\.[0-9]+)?)",
        r"最新收盘价\*{0,2}\s*[:：]\s*[¥￥]?\s*([0-9]+(?:\.[0-9]+)?)",
        r"收盘价\*{0,2}\s*[:：]\s*[¥￥]?\s*([0-9]+(?:\.[0-9]+)?)",
        r"当前参考价\*{0,2}\s*[:：]\s*[¥￥]?\s*([0-9]+(?:\.[0-9]+)?)",
        r"\|\s*(?:最新价|当前价|当前价格|收盘价)\s*\|\s*[¥￥]?\s*([0-9]+(?:\.[0-9]+)?)",
        r"当前股价约\s*[¥￥]?\s*([0-9]+(?:\.[0-9]+)?)",
    ]
    for text in texts:
        value = str(text or "")
        for pattern in patterns:
            match = re.search(pattern, value)
            if match:
                try:
                    price = float(match.group(1))
                except Exception:
                    continue
                if price > 0:
                    return price
    return None


def format_price_guard(price: float | None) -> str:
    if price is None:
        return "当前行情价格: 未提取到；禁止用 PE×EPS 或其他估值公式反推当前股价，必须回到行情源补价后再给具体价格结论。"
    return (
        f"当前行情价格: {price:.2f}元。此价格来自行情/技术报告或行情数据源，是全报告唯一当前价锚点；"
        "禁止用 PE×EPS、PB×BVPS 或任何估值公式反推当前股价。"
    )


def ensure_price_consistency(text: str, market_price: float | None, name: str = "report") -> None:
    forbidden = ["隐含当前价格", "隐含价格", "反推", "PE与EPS反推", "PE × EPS", "PE×EPS", "EPS × PE", "EPS×PE"]
    hits = [item for item in forbidden if item in text]
    if hits:
        raise RuntimeError(f"{name} 包含禁止的当前价反推/隐含价格表述: {hits}")
    if market_price is None:
        raise RuntimeError(f"{name} 缺少行情当前价锚点，禁止生成价值层价格结论。")
    if market_price < 20:
        return
    suspicious: list[float] = []
    price_keywords = "当前价|当前价格|参考价|目标价|合理价|合理价值|风险回落|回落区间|买入区|低吸区|支撑|压力|止损|减仓|上涨空间|下跌空间"
    per_share_metric_keywords = r"EPS|每股收益|每股盈利|BVPS|每股净资产|每股|年化EPS|年化为"
    for line in str(text or "").splitlines():
        if not re.search(price_keywords, line):
            continue
        for match in re.finditer(r"(?<![\d.])([0-9]+(?:\.[0-9]+)?)\s*元(?:/股)?", line):
            try:
                value = float(match.group(1))
            except Exception:
                continue
            context = line[max(0, match.start() - 24): match.end() + 24]
            prefix = line[max(0, match.start() - 3): match.start()]
            if re.search(per_share_metric_keywords, context, flags=re.IGNORECASE):
                continue
            if re.search(r"\d\s*[-—–~至到]\s*$", prefix):
                continue
            if re.search(r"(?:价差|收益|亏损|回撤|上涨空间|下跌空间)[^\n]{0,16}\d+(?:\.\d+)?\s*[-—–~至到]\s*\d+(?:\.\d+)?\s*元", context):
                continue
            if 0 < value < market_price * 0.35:
                suspicious.append(value)
    if suspicious:
        raise RuntimeError(
            f"{name} 出现明显脱离当前价{market_price:.2f}元的价格 {suspicious[:8]}；"
            "疑似使用季度EPS或估值公式错误反推，已中止。"
        )


def build_context(symbol: str, stock_name: str, date: str, raw: dict[str, Any], report_paths: dict[str, Path]) -> dict[str, str]:
    market_report = get_state_text(raw, "market_report") or read_text(report_paths.get("market_report"))
    fundamentals_report = get_state_text(raw, "fundamentals_report") or read_text(report_paths.get("fundamentals_report"))
    final_trade = get_state_text(raw, "final_trade_decision") or read_text(report_paths.get("final_trade_decision"))
    risk_report = get_state_text(raw, "risk_debate_state") or read_text(report_paths.get("risk_management_decision"))
    investment_plan = get_state_text(raw, "investment_plan") or get_state_text(raw, "trader_investment_plan") or read_text(report_paths.get("investment_plan")) or read_text(report_paths.get("trader_investment_plan"))
    eastmoney_skills_context = get_state_text(raw, "eastmoney_skills_context")
    decision = raw.get("decision") if isinstance(raw.get("decision"), dict) else {}
    decision_text = json.dumps(decision, ensure_ascii=False, indent=2) if decision else final_trade
    market_price = extract_market_price(market_report, fundamentals_report, final_trade, decision_text, investment_plan)
    price_guard = format_price_guard(market_price)
    return {
        "header": f"股票：{stock_name}（{symbol}）\n分析日期：{date}\n市场：中国A股\n{price_guard}",
        "market_report": market_report,
        "fundamentals_report": fundamentals_report,
        "final_trade": final_trade,
        "risk_report": risk_report,
        "investment_plan": investment_plan,
        "eastmoney_skills_context": eastmoney_skills_context,
        "decision": decision_text,
        "market_price": "" if market_price is None else f"{market_price:.2f}",
        "price_guard": price_guard,
    }


def make_system_prompt() -> str:
    agent_lines = "\n".join(f"- {agent.name}：{agent.role}。重点：{agent.focus}" for agent in VALUE_AGENTS)
    return f"""你是一个A股投资研究委员会，负责把 TradingAgents-CN 的短线交易报告升级为“长短线双视角报告”。

你必须模拟以下价值投资 Agent，但不要暴露隐藏推理链，只输出可公开给投资新手学习的观点、证据、反驳和结论：
{agent_lines}

输出必须是中文 Markdown。必须区分：
1. 短线交易判断：技术面、趋势、支撑压力、止损/减仓；
2. 长线价值判断：商业模式、行业空间、护城河、财务质量、估值、安全边际；
3. 长短线冲突处理：短线可以买但长线不够好怎么办，长线不错但短线破位怎么办。

约束：
- 不得承诺收益；不得把报告写成确定性荐股。
- 当前价格只能引用行情源给出的价格锚点；禁止用PE、PB、EPS、BVPS互相倒算。
- 正文禁止出现“隐含当前价格”“隐含价格”“反推”“PE×EPS”“PE × EPS”“EPS×PE”“EPS × PE”等词串。
- 如果数据不足，必须明确写“数据不足，不能确认”。
- 如果核心财务门禁已通过，不得继续使用“数据不足，不能确认”这句话；对ROIC、管理层、客户集中度等非硬门槛补充项，只能写为“后续跟踪项”，不得把报告整体写成数据不完整。
- 对A股要特别关注经营现金流、应收账款、商誉、资产负债率、ROE/ROIC、周期性和政策风险。
- 价值投资结论必须包含：长期评级、适合/不适合长期持有、合理价值区间、安全边际、买入逻辑失效条件。
- 任何“合理价值区间/合理价位区间/目标价”都必须紧邻说明估值口径：例如 PB/ROE 对照、PE/盈利情景、DCF假设、同业比较、或“情景折价区间”。不能只给一个数字区间。
- 如果PE为负、ROE为负、盈利不可持续，禁止用PE估值；只能用PB/现金流/资产质量/情景折价，并明确这是情景区间不是精确内在价值。
- 最后给出适合投资小白的学习解释。
"""


def make_user_prompt(context: dict[str, str]) -> str:
    return f"""请基于下面 TradingAgents-CN 已有输出，生成三段式报告：

# A. 价值投资 Agent 委员会报告
每个 Agent 单独一节，结构为：
- 核心判断
- 支持证据
- 主要反驳/担忧
- 对长期投资的结论：看多/中性/看空

# B. 长线价值投资总评
必须包含：商业模式、行业空间、护城河、财务质量、估值、安全边际、长期持有条件、卖出/不买条件。

# C. 长短线合并决策
必须给出：
- 短线交易结论
- 长线价值结论
- 如果已持仓怎么办
- 如果未持仓怎么办
- 什么条件下重新评估
- 投资小白学习版解释

{context['header']}

## 价格口径硬约束
{context['price_guard']}
请所有价位判断都围绕上述行情当前价；EPS、BVPS、每股净资产只能引用输入已有字段，不能用当前价和PE/PB倒算，也不要写出任何“反推/隐含价格/PE×EPS”表述。

## 当前最终交易决策
{clip(context['decision'], 1800)}

## 东方财富 Skills 前置上下文
请价值投资 Agent 优先用这部分校验行情、估值、资金流、财务质量、支撑压力和风险收益比。
{clip(context.get('eastmoney_skills_context', ''), 9000)}

## 技术/交易报告
{clip(context['market_report'], 6500)}

## 基本面报告
{clip(context['fundamentals_report'], 8500)}

## 投资/风控计划
{clip(context['investment_plan'], 2800)}

## 风险报告
{clip(context['risk_report'], 3500)}
"""


def make_llm(args: argparse.Namespace):
    kwargs: dict[str, Any] = {
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
        "timeout": args.timeout,
        "max_retries": 0,
    }
    api_key = os.getenv("CUSTOM_OPENAI_API_KEY") if args.provider == "custom_openai" else None
    if api_key:
        kwargs["api_key"] = api_key
    return OpenAIClient(
        model=args.model,
        base_url=args.base_url or None,
        provider=args.provider,
        **kwargs,
    ).get_llm()


def invoke_value_committee(args: argparse.Namespace, context: dict[str, str]) -> str:
    if args.no_llm:
        if args.allow_fallback:
            return build_fallback_value_report(context, "用户指定 --no-llm，未调用大模型。")
        raise RuntimeError("价值层禁止无LLM兜底：收到 --no-llm 但未显式允许 --allow-fallback。")

    attempts = max(1, int(args.retries))
    errors: list[str] = []
    for attempt in range(1, attempts + 1):
        try:
            llm = make_llm(args)
            response = llm.invoke([
                SystemMessage(content=make_system_prompt()),
                HumanMessage(content=make_user_prompt(context)),
            ])
            content = getattr(response, "content", response)
            if isinstance(content, list):
                content = "\n".join(str(item) for item in content)
            content = str(content).strip()
            if content:
                normalized = normalize_nonblocking_missing_language(content)
                try:
                    ensure_value_report_quality(normalized)
                    ensure_price_consistency(normalized, float(context["market_price"]) if context.get("market_price") else None, "value_committee")
                    return normalized
                except Exception as quality_exc:
                    errors.append(f"尝试{attempt}: 质量门禁失败: {type(quality_exc).__name__}: {quality_exc}")
                    if attempt < attempts:
                        continue
            else:
                errors.append(f"尝试{attempt}: 模型返回为空")
        except Exception as exc:
            errors.append(f"尝试{attempt}: {type(exc).__name__}: {exc}")
        if attempt < attempts:
            time.sleep(min(20, 2 ** attempt))

    error_text = "；".join(errors[-attempts:])
    if args.allow_fallback:
        return build_fallback_value_report(context, f"模型调用失败，使用兜底模板：{error_text}")
    raise RuntimeError(f"价值层模型调用失败，已重试{attempts}次；为避免生成兜底版报告，已中止。{error_text}")


def ensure_value_report_quality(text: str) -> None:
    bad_markers = [
        "价值投资 Agent 委员会报告（兜底版）",
        "兜底模板",
        "模型调用失败",
        "模型返回为空",
        "Request timed out",
        "timed out",
    ]
    hits = [marker for marker in bad_markers if marker in text]
    if hits:
        raise RuntimeError(f"价值层报告未达标，包含兜底/失败标记: {hits}")
    ensure_valuation_range_explained(text)


def ensure_valuation_range_explained(text: str) -> None:
    range_patterns = [
        r"合理价值区间[：:为\s]*[^\n]{0,80}\d+(?:\.\d+)?\s*(?:元|¥)?\s*[—\-~至到]\s*\d+(?:\.\d+)?",
        r"合理价位区间[：:为\s]*[^\n]{0,80}\d+(?:\.\d+)?\s*(?:元|¥)?\s*[—\-~至到]\s*\d+(?:\.\d+)?",
        r"基本面合理区间[：:为\s]*[^\n]{0,80}\d+(?:\.\d+)?\s*(?:元|¥)?\s*[—\-~至到]\s*\d+(?:\.\d+)?",
    ]
    method_markers = [
        "估值方法",
        "估值口径",
        "计算口径",
        "测算口径",
        "情景折价",
        "情景区间",
        "PB",
        "PE",
        "DCF",
        "现金流",
        "同业",
        "ROE",
        "资产质量",
        "安全边际",
    ]
    for pattern in range_patterns:
        for match in re.finditer(pattern, text):
            start = max(0, match.start() - 500)
            end = min(len(text), match.end() + 700)
            window = text[start:end]
            if not any(marker in window for marker in method_markers):
                snippet = re.sub(r"\s+", " ", match.group(0))[:120]
                raise RuntimeError(f"合理价值区间缺少估值口径说明: {snippet}")


def normalize_nonblocking_missing_language(text: str) -> str:
    replacements = {
        "数据不足，不能确认": "作为后续跟踪项，当前不作为核心结论依据",
        "技术数据不足，不能确认": "短线技术数据已在交易报告中给出，仍需结合实时盘面跟踪",
        "由于技术数据不足，不能确认短线趋势": "短线趋势以交易报告中的均线、量能和支撑压力为准，仍需结合实时盘面跟踪",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def build_fallback_value_report(context: dict[str, str], reason: str) -> str:
    return f"""# 价值投资 Agent 委员会报告（兜底版）

> {reason}

## Ben Graham Agent（格雷厄姆）
- 核心判断：优先检查安全边际和资产负债安全，当前只能根据已有基本面报告做初步判断。
- 支持证据：请重点查看基本面报告中的收入增速、利润、资产负债率、流动比率、现金储备。
- 主要担忧：如果没有多年度稳定盈利、经营现金流和保守估值数据，不能确认“低估”。
- 长期结论：数据不足时偏中性/谨慎。

## Warren Buffett Agent（巴菲特）
- 核心判断：长期投资首先看商业模式、护城河和资本回报，而不是短线指标。
- 支持证据：若公司具备高毛利、稳定现金流和较高ROE，才更接近长期好公司。
- 主要担忧：若收入下滑、ROE偏低或现金流不稳定，长期确定性不足。
- 长期结论：需要用后续财报验证。

## Charlie Munger Agent（芒格）
- 核心判断：避免因为短期反弹而买入质量不够高或风险复杂的企业。
- 支持证据：关注负债、应收、商誉、行业周期和管理层资本配置。
- 主要担忧：复杂商业模式和高杠杆会降低长期投资胜率。
- 长期结论：质量未确认前不宜重仓。

## Michael Burry Agent（Burry）
- 核心判断：如果市场过度悲观且资产/现金流有保护，可能有逆向机会。
- 支持证据：看估值是否已经充分反映坏消息。
- 主要担忧：便宜可能是价值陷阱，需要现金流验证。
- 长期结论：只在风险收益明显不对称时看多。

## Mohnish Pabrai Agent（Pabrai）
- 核心判断：优先寻找“下行有限、上行较大”的机会。
- 支持证据：需要明确保守价值、基准价值、乐观价值。
- 主要担忧：如果下行空间和基本面不确定性都大，不符合低风险高赔率。
- 长期结论：等待更大安全边际或确认信号。

## Fundamentals Agent（基本面）
- 核心判断：基本面报告是价值投资核心输入。
- 重点检查：收入、利润、经营现金流、ROE/ROIC、资产负债率、应收账款、商誉、分红。
- 长期结论：财务质量决定是否可长期持有。

## Valuation Agent（估值）
- 核心判断：没有合理价值区间，就不能说是价值投资。
- 估值方法：PE/PB/PS、DCF情景估值、同业比较、安全边际。
- 长期结论：只有当前价格显著低于保守价值时，才有安全边际。

## Risk Manager（长期风险）
- 核心判断：长期投资也需要卖出条件。
- 红线：收入持续恶化、经营现金流恶化、ROE长期低迷、负债/商誉/应收异常、估值过高。
- 结论：先控制仓位，再等待价值证据。

## 长短线合并提示

短线报告主要回答“现在能不能交易”，价值报告回答“这家公司值不值得长期拥有”。新手不应把短线被套自动解释成价值投资。

## 原始资料摘要

### 当前最终交易决策
{clip(context['decision'], 1200)}

### 基本面报告摘要
{clip(context['fundamentals_report'], 2200)}
"""


def extract_section(text: str, starts: list[str], fallback_title: str) -> str:
    positions = [text.find(start) for start in starts if text.find(start) >= 0]
    if not positions:
        return f"# {fallback_title}\n\n{text.strip()}\n"
    start = min(positions)
    return text[start:].strip()


def build_trading_report(symbol: str, stock_name: str, date: str, context: dict[str, str]) -> str:
    return f"""# {stock_name}-{date}-trading

> 短线/波段交易版报告。它回答“现在行情和技术结构怎么处理”，不等同于长期价值投资结论。

## 短线交易结论来源

{clip(context['decision'], 1800)}

## 技术分析

{clip(context['market_report'], 9000)}

## 东方财富 Skills 前置上下文

{clip(context.get('eastmoney_skills_context', ''), 5000)}

## 交易/风控计划

{clip(context['investment_plan'] or context['risk_report'], 5000)}

## 学习提示

- MACD、RSI、BOLL、均线、成交量主要服务于短线择时和风控。
- 技术指标不能证明公司有长期投资价值。
- 如果短线破位，长期持有也需要重新检查基本面和估值假设。
"""


def build_value_report(symbol: str, stock_name: str, date: str, value_committee: str) -> str:
    return f"# {stock_name}-{date}-value\n\n> 长线价值投资版报告。它回答“这家公司是否值得长期拥有”，不等同于短线买卖点。\n\n{value_committee.strip()}\n"


def build_combined_report(symbol: str, stock_name: str, date: str, trading_report: str, value_report: str, context: dict[str, str]) -> str:
    return f"""# {stock_name}-{date}-combined

> 长短线合并版。建议日常先看这一份，再分别打开 trading/value 细看证据。

## 怎么读这三份报告

- `trading`：短线行情、技术指标、支撑压力、止损/减仓。
- `value`：商业模式、财务质量、估值、安全边际、长期持有条件。
- `combined`：把短线和长线放在一起，避免把短线被套误当价值投资。

## 当前交易系统结论

{clip(context['decision'], 1600)}

## 东方财富 Skills 关键数据

{clip(context.get('eastmoney_skills_context', ''), 3500)}

## 长线价值委员会摘要

{clip(value_report, 5000)}

## 短线交易摘要

{clip(trading_report, 4200)}

## 长短线冲突处理原则

| 场景 | 处理方式 |
|---|---|
| 长线价值强，短线破位 | 不追涨，不满仓；等技术止跌或分批低吸，并明确基本面失效条件。 |
| 长线价值弱，短线超跌 | 只能按反弹交易处理，不能用“价值投资”当被套理由。 |
| 长线价值强，短线也转强 | 才是相对舒服的配置窗口，但仍需估值安全边际。 |
| 长线价值弱，短线也转弱 | 优先风控，避免补仓摊低。 |

## 给投资小白的重点

价值投资不是“亏了就长期拿”。价值投资必须先回答：公司是否好、价格是否便宜、现金流是否真实、什么情况证明自己错了。
"""


def write_outputs(out_dir: Path, symbol: str, stock_name: str, date: str, trading: str, value: str, combined: str, metadata: dict[str, Any]) -> dict[str, Path]:
    stock_dir = analysis_stock_dir(symbol, stock_name) if out_dir == ANALYSIS_OUTPUTS else out_dir / safe_name(stock_name)
    stock_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"{safe_name(stock_name)}-{date}"
    paths = {
        "trading": stock_dir / f"{prefix}-trading.md",
        "value": stock_dir / f"{prefix}-value.md",
        "combined": stock_dir / f"{prefix}-combined.md",
        "raw": stock_dir / f"{prefix}-value-raw.json",
    }
    paths["trading"].write_text(trading, encoding="utf-8")
    paths["value"].write_text(value, encoding="utf-8")
    paths["combined"].write_text(combined, encoding="utf-8")
    paths["raw"].write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return paths


def main() -> None:
    load_dotenv(ROOT / ".env")
    args = parse_args()
    symbol = normalize_symbol(args.symbol)
    raw_path = find_latest_raw(symbol, args.date, args.stock_name)
    raw = load_json(raw_path)
    date = infer_date(raw_path, raw, args.date)
    report_paths = find_result_reports(symbol, date, args.stock_name)
    probe_texts = [read_text(path) for path in report_paths.values()]
    stock_name = infer_stock_name(symbol, args.stock_name, raw, probe_texts)
    context = build_context(symbol, stock_name, date, raw, report_paths)
    fundamentals_text, fundamentals_quality = ensure_fundamentals_quality(symbol, context.get("fundamentals_report", ""))
    context["fundamentals_report"] = fundamentals_text
    value_committee = invoke_value_committee(args, context)
    trading_report = build_trading_report(symbol, stock_name, date, context)
    value_report = build_value_report(symbol, stock_name, date, value_committee)
    combined_report = build_combined_report(symbol, stock_name, date, trading_report, value_report, context)
    ensure_value_report_quality(value_report)
    ensure_value_report_quality(combined_report)
    market_price = float(context["market_price"]) if context.get("market_price") else None
    ensure_price_consistency(value_report, market_price, "value_report")
    ensure_price_consistency(combined_report, market_price, "combined_report")
    ensure_no_truncation_markers({
        "trading": trading_report,
        "value": value_report,
        "combined": combined_report,
    })

    out_dir = Path(args.out_dir).expanduser()
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    metadata = {
        "symbol": symbol,
        "stock_name": stock_name,
        "date": date,
        "model": args.model,
        "provider": args.provider,
        "raw_path": str(raw_path.relative_to(ROOT)) if raw_path else None,
        "report_paths": {key: str(path.relative_to(ROOT)) for key, path in report_paths.items()},
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "agents": [agent.__dict__ for agent in VALUE_AGENTS],
        "fundamentals_quality": fundamentals_quality,
    }
    paths = write_outputs(out_dir, symbol, stock_name, date, trading_report, value_report, combined_report, metadata)
    for key, path in paths.items():
        print(f"{key}: {path}")


if __name__ == "__main__":
    main()
