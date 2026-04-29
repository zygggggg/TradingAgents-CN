#!/usr/bin/env python3
"""Explain triggered stock-monitor events with recent market data and prior reports."""

import argparse
import json
import os
import re
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]


def load_env_file(path):
    # type: (Path) -> None
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def safe_float(value):
    # type: (Any) -> Optional[float]
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def exchange_prefix(symbol):
    # type: (str) -> str
    if symbol.startswith(("6", "9")):
        return "sh"
    if symbol.startswith(("4", "8")):
        return "bj"
    return "sz"


def eastmoney_secid(symbol):
    # type: (str) -> str
    return "%s.%s" % ("1" if exchange_prefix(symbol) == "sh" else "0", symbol)


def prefixed_symbol(symbol):
    # type: (str) -> str
    return "%s%s" % (exchange_prefix(symbol), symbol)


def http_get_json(url):
    # type: (str) -> Dict[str, Any]
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; TradingAgentsCNMonitor/1.0)",
            "Referer": "https://quote.eastmoney.com/",
        },
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        raw = response.read().decode("utf-8", errors="ignore")
    return json.loads(raw)


def http_get_text(url, referer=""):
    # type: (str, str) -> str
    headers = {"User-Agent": "Mozilla/5.0 (compatible; TradingAgentsCNMonitor/1.0)"}
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=12) as response:
        raw = response.read()
    for encoding in ("gbk", "utf-8"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def parse_quoted_payload(text, sep):
    # type: (str, str) -> List[str]
    match = re.search(r'="(.*)";?', text, flags=re.S)
    if not match:
        return []
    return match.group(1).split(sep)


def fetch_eastmoney_klines(symbol, limit=20):
    # type: (str, int) -> List[Dict[str, Any]]
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get?"
        "secid=%s&klt=101&fqt=1&lmt=%d&end=20500101&iscca=1&"
        "fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
    ) % (eastmoney_secid(symbol), limit)
    payload = http_get_json(url)
    data = payload.get("data") or {}
    rows = []
    for raw in data.get("klines") or []:
        parts = raw.split(",")
        if len(parts) < 11:
            continue
        rows.append(
            {
                "date": parts[0],
                "open": safe_float(parts[1]),
                "close": safe_float(parts[2]),
                "high": safe_float(parts[3]),
                "low": safe_float(parts[4]),
                "volume": safe_float(parts[5]),
                "amount": safe_float(parts[6]),
                "amplitude": safe_float(parts[7]),
                "pct_change": safe_float(parts[8]),
                "change": safe_float(parts[9]),
                "turnover": safe_float(parts[10]),
                "source": "eastmoney_kline",
            }
        )
    return rows


def fetch_tencent_klines(symbol, limit=20):
    # type: (str, int) -> List[Dict[str, Any]]
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,%d,qfq" % (prefixed_symbol(symbol), limit)
    payload = http_get_json(url)
    data = (payload.get("data") or {}).get(prefixed_symbol(symbol), {})
    raw_rows = data.get("qfqday") or data.get("day") or []
    rows = []
    for row in raw_rows:
        if len(row) < 6:
            continue
        rows.append(
            {
                "date": row[0],
                "open": safe_float(row[1]),
                "close": safe_float(row[2]),
                "high": safe_float(row[3]),
                "low": safe_float(row[4]),
                "volume": safe_float(row[5]),
                "source": "tencent_kline",
            }
        )
    return rows


def fetch_recent_klines(symbol, limit=20):
    # type: (str, int) -> List[Dict[str, Any]]
    for fetcher in (fetch_eastmoney_klines, fetch_tencent_klines):
        try:
            rows = fetcher(symbol, limit)
            if rows:
                return rows
        except Exception:
            continue
    return []


def latest_quote_from_klines(symbol):
    # type: (str) -> Dict[str, Any]
    rows = fetch_recent_klines(symbol, 1)
    if rows:
        row = rows[-1]
        return {
            "symbol": symbol,
            "current_price": row.get("close"),
            "volume": row.get("volume"),
            "quote_date": row.get("date"),
            "source": row.get("source"),
        }
    return {"symbol": symbol, "current_price": None, "source": "none"}


def read_text(path, limit):
    # type: (Path, int) -> str
    text = path.read_text(encoding="utf-8", errors="ignore")
    return text[:limit]


def collect_report_context(root, symbol, stock_name, plan_date="", total_limit=14000):
    # type: (Path, str, str, str, int) -> str
    names = [stock_name, symbol]
    candidate_dirs = []  # type: List[Path]
    for name in names:
        if name:
            candidate_dirs.append(root / "analysis_outputs" / name)
            if plan_date:
                candidate_dirs.append(root / "results" / name / plan_date / "reports")
    candidate_dirs.append(root / "analysis_outputs")

    files = []  # type: List[Path]
    for directory in candidate_dirs:
        if not directory.exists():
            continue
        for pattern in ("*.md", "*.txt"):
            for path in directory.glob(pattern):
                text_name = path.name
                if symbol in text_name or (stock_name and stock_name in text_name) or path.parent.name in names:
                    files.append(path)

    unique = []
    seen = set()
    for path in files:
        if path not in seen:
            unique.append(path)
            seen.add(path)

    def score(path):
        name = path.name
        date_hit = 1 if plan_date and plan_date in name else 0
        combined = 2 if "combined" in name or "综合" in name else 0
        trading = 1 if "trading" in name or "交易" in name else 0
        value = 1 if "value" in name or "价值" in name else 0
        return (date_hit, combined + trading + value, path.stat().st_mtime)

    unique.sort(key=score, reverse=True)
    chunks = []
    used = 0
    for path in unique[:6]:
        remaining = total_limit - used
        if remaining <= 0:
            break
        text = read_text(path, min(remaining, 5000))
        if not text.strip():
            continue
        chunks.append("\n\n### 来源：%s\n%s" % (path.relative_to(root), text))
        used += len(text)
    return "".join(chunks) or "未找到原始报告，只能根据当前价位和近期K线解释。"


def format_klines(rows):
    # type: (List[Dict[str, Any]]) -> str
    if not rows:
        return "未获取到近期K线。"
    lines = ["日期 | 开盘 | 收盘 | 最高 | 最低 | 涨跌幅% | 成交量", "---|---:|---:|---:|---:|---:|---:"]
    for row in rows[-15:]:
        lines.append(
            "%s | %s | %s | %s | %s | %s | %s"
            % (
                row.get("date", ""),
                fmt(row.get("open")),
                fmt(row.get("close")),
                fmt(row.get("high")),
                fmt(row.get("low")),
                fmt(row.get("pct_change")),
                fmt(row.get("volume"), 0),
            )
        )
    return "\n".join(lines)


def fmt(value, digits=2):
    # type: (Any, int) -> str
    number = safe_float(value)
    if number is None:
        return "N/A"
    if digits == 0:
        return str(int(number))
    return ("%%.%df" % digits) % number


def build_prompt(event, stock, quote, klines, report_context):
    # type: (Dict[str, Any], Dict[str, Any], Dict[str, Any], List[Dict[str, Any]], str) -> str
    return """你是一个A股投资学习助手，只做提醒解释，不做自动交易。请结合“原有报告/交易计划”“当前触发事件”“最近走势数据”，给出简洁、可执行、适合投资新手理解的解释。

要求：
1. 不要编造未提供的数据；没有证据就说“需要确认”。
2. 不要直接命令买/卖；用“建议人工确认/优先控制风险/可观察”的表达。
3. 必须解释触发条件为什么重要、与原计划是否一致、下一步重点看什么。
4. 输出固定为四段：AI解释、和原计划的关系、风险/反证、建议。
5. 每段2-4句话，中文。

## 触发事件
%s

## 股票配置/原计划
%s

## 当前/最新行情
%s

## 最近走势（优先东方财富K线，失败则腾讯）
%s

## 原有报告摘录
%s
""" % (
        json.dumps(event, ensure_ascii=False, indent=2),
        json.dumps(stock, ensure_ascii=False, indent=2),
        json.dumps(quote, ensure_ascii=False, indent=2),
        format_klines(klines),
        report_context,
    )


def post_json(url, payload, api_key, timeout=90):
    # type: (str, Dict[str, Any], str, int) -> Dict[str, Any]
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")[:500]
        raise RuntimeError("HTTP %s: %s" % (exc.code, body))
    return json.loads(text)


def extract_responses_text(payload):
    # type: (Dict[str, Any]) -> str
    if payload.get("output_text"):
        return str(payload["output_text"]).strip()
    parts = []
    for item in payload.get("output") or []:
        for content in item.get("content") or []:
            if content.get("text"):
                parts.append(str(content.get("text")))
    return "\n".join(parts).strip()


def extract_chat_text(payload):
    # type: (Dict[str, Any]) -> str
    choices = payload.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        return "\n".join(str(item.get("text") or item) for item in content).strip()
    return str(content or "").strip()


def call_llm(prompt, no_llm=False):
    # type: (str, bool) -> str
    if no_llm:
        return "AI解释：当前为 --no-llm 测试模式，未调用大模型。\n\n和原计划的关系：规则触发后会把当前价、近期K线和原报告一起交给AI解释。\n\n风险/反证：正式运行时如API失败，会保留规则提醒，不影响止盈/止损提醒。\n\n建议：部署后用一次测试事件确认钉钉能收到AI解释。"

    load_env_file(ROOT / ".env")
    load_env_file(ROOT / ".stock-monitor.env")
    api_key = os.getenv("MONITOR_AI_API_KEY") or os.getenv("CUSTOM_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        return "AI解释暂不可用：未配置 MONITOR_AI_API_KEY / CUSTOM_OPENAI_API_KEY / OPENAI_API_KEY。"
    base_url = (os.getenv("MONITOR_AI_BASE_URL") or os.getenv("CUSTOM_OPENAI_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    model = os.getenv("MONITOR_AI_MODEL") or os.getenv("TA_LLM_MODEL") or os.getenv("CUSTOM_OPENAI_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-5.5"
    max_tokens = int(os.getenv("MONITOR_AI_MAX_TOKENS", "1200"))
    reasoning_effort = os.getenv("MONITOR_AI_REASONING_EFFORT", "medium")

    system_text = "你是A股投资学习和风控提醒助手。你只能基于输入资料解释触发原因和风险，不提供确定收益承诺，不自动交易。"
    responses_payload = {
        "model": model,
        "input": [
            {"role": "system", "content": system_text},
            {"role": "user", "content": prompt},
        ],
        "max_output_tokens": max_tokens,
    }
    if reasoning_effort:
        responses_payload["reasoning"] = {"effort": reasoning_effort}
    try:
        response = post_json(base_url + "/responses", responses_payload, api_key)
        text = extract_responses_text(response)
        if text:
            return text
    except Exception as responses_exc:
        chat_payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_text},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.2,
        }
        try:
            response = post_json(base_url + "/chat/completions", chat_payload, api_key)
            text = extract_chat_text(response)
            if text:
                return text
        except Exception as chat_exc:
            return "AI解释暂不可用：模型接口调用失败（responses: %s；chat: %s）。" % (str(responses_exc)[:260], str(chat_exc)[:260])
    return "AI解释暂不可用：模型返回为空。"


def explain_event(event, stock, root=ROOT, no_llm=False):
    # type: (Dict[str, Any], Dict[str, Any], Path, bool) -> str
    symbol = str(event.get("symbol") or stock.get("symbol") or "")
    stock_name = str(event.get("name") or stock.get("name") or symbol)
    plan_date = str(stock.get("plan_date") or event.get("quote_date") or datetime.now().strftime("%Y-%m-%d"))
    quote = latest_quote_from_klines(symbol)
    if event.get("price") not in (None, "N/A"):
        quote["event_price"] = event.get("price")
    if event.get("volume") not in (None, "N/A"):
        quote["event_volume"] = event.get("volume")
    klines = fetch_recent_klines(symbol, 20)
    report_context = collect_report_context(root, symbol, stock_name, plan_date)
    prompt = build_prompt(event, stock, quote, klines, report_context)
    return call_llm(prompt, no_llm=no_llm)


def main():
    parser = argparse.ArgumentParser(description="Explain a stock monitor event with AI.")
    parser.add_argument("--event-json", required=True, help="Path to event JSON")
    parser.add_argument("--stock-json", default="", help="Path to stock config JSON")
    parser.add_argument("--no-llm", action="store_true", help="Validate prompt/data path without calling LLM")
    args = parser.parse_args()
    event = json.loads(Path(args.event_json).read_text(encoding="utf-8"))
    stock = json.loads(Path(args.stock_json).read_text(encoding="utf-8")) if args.stock_json else {"symbol": event.get("symbol"), "name": event.get("name")}
    print(explain_event(event, stock, ROOT, no_llm=args.no_llm))


if __name__ == "__main__":
    main()
