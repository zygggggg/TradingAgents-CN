#!/usr/bin/env python3
"""Monitor A-share watchlist levels and optionally notify DingTalk.

This script is intentionally lightweight: price checks are rule-based and do not
call an LLM. Use it from cron/systemd during A-share trading hours.
"""

import argparse
import json
import os
import re
import sys
import subprocess
import tempfile
import urllib.request
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.send_dingtalk import send_markdown
from scripts.explain_monitor_event import explain_event, latest_quote_from_klines

try:
    from tradingagents.dataflows.providers.china.integrated import get_integrated_china_provider
except Exception:
    get_integrated_china_provider = None

STATE_FILE = ROOT / "data" / "watchlist_monitor_state.json"
DEFAULT_WATCHLIST = ROOT / "watchlists" / "a_share_watchlist.json"


def load_json(path, default):
    # type: (Path, Any) -> Any
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, payload):
    # type: (Path, Any) -> None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def safe_filename(value):
    # type: (Any) -> str
    text = str(value or "").strip() or "unknown"
    return re.sub(r'[\\/:*?"<>|\s]+', "-", text).strip("-") or "unknown"


def save_event_report(event, message, output_root=None):
    # type: (Dict[str, Any], str, Optional[str]) -> Optional[Path]
    if event.get("event") in ("heartbeat", "data_missing"):
        return None
    root = Path(output_root or os.getenv("MONITOR_REPORT_ROOT", str(ROOT / "analysis_outputs")))
    stock_name = safe_filename(event.get("name") or event.get("symbol"))
    quote_date = str(event.get("quote_date") or now_cn().strftime("%Y-%m-%d"))
    if re.match(r"^\d{8}$", quote_date):
        quote_date = "%s-%s-%s" % (quote_date[:4], quote_date[4:6], quote_date[6:8])
    run_time = now_cn().strftime("%H%M")
    event_name = safe_filename(event.get("event"))
    report_dir = root / stock_name / "alerts" / quote_date
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / ("%s-%s-%s-%s-alert.md" % (stock_name, quote_date, run_time, event_name))
    header = [
        "# %s 关键事件报告" % stock_name,
        "",
        "- 生成时间：%s" % now_cn().strftime("%Y-%m-%d %H:%M:%S %Z"),
        "- 股票：%s（%s）" % (event.get("name"), event.get("symbol")),
        "- 事件：%s" % event.get("event"),
        "- 严重程度：%s" % event.get("severity"),
        "- 当前价：%s" % event.get("price"),
        "- 成交量：%s" % event.get("volume"),
        "- 行情日期：%s" % (event.get("quote_date") or "N/A"),
        "- 数据源：%s" % (event.get("source") or "N/A"),
        "",
        "---",
        "",
    ]
    report_path.write_text("\n".join(header) + message + "\n", encoding="utf-8")
    return report_path


def start_agent_report(event, stock, quick_report_path, notify=False):
    # type: (Dict[str, Any], Dict[str, Any], Optional[Path], bool) -> Optional[Path]
    if event.get("event") in ("heartbeat", "data_missing"):
        return None
    if os.getenv("MONITOR_AGENT_ON_TRIGGER", "true").lower() != "true":
        return None

    root = Path(os.getenv("MONITOR_AGENT_WORK_DIR", str(ROOT / "data" / "event_agent_jobs")))
    root.mkdir(parents=True, exist_ok=True)
    job_dir = Path(tempfile.mkdtemp(prefix="event-agent-", dir=str(root)))
    event_path = job_dir / "event.json"
    stock_path = job_dir / "stock.json"
    log_path = job_dir / "agent.log"
    event_path.write_text(json.dumps(event, ensure_ascii=False, indent=2), encoding="utf-8")
    stock_path.write_text(json.dumps(stock, ensure_ascii=False, indent=2), encoding="utf-8")

    python_bin = os.getenv("MONITOR_AGENT_PYTHON") or sys.executable
    command = [
        python_bin,
        str(ROOT / "scripts" / "run_event_agent_report.py"),
        "--event-json",
        str(event_path),
        "--stock-json",
        str(stock_path),
    ]
    if quick_report_path is not None:
        command.extend(["--quick-report", str(quick_report_path)])
    if notify:
        command.append("--notify")

    with log_path.open("ab") as log_file:
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    event["agent_job_pid"] = process.pid
    event["agent_job_dir"] = str(job_dir)
    event["agent_job_log"] = str(log_path)
    return log_path


def cn_timezone():
    if ZoneInfo is not None:
        return ZoneInfo("Asia/Shanghai")
    return timezone(timedelta(hours=8))


def now_cn():
    # type: () -> datetime
    return datetime.now(cn_timezone())


def in_a_share_session(now):
    # type: (datetime) -> bool
    if now.weekday() >= 5:
        return False
    current = now.time()
    return (dt_time(9, 25) <= current <= dt_time(11, 35)) or (dt_time(12, 55) <= current <= dt_time(15, 10))


def exchange_prefix(symbol):
    # type: (str) -> str
    if symbol.startswith(("6", "9")):
        return "sh"
    if symbol.startswith(("4", "8")):
        return "bj"
    return "sz"


def prefixed_symbol(symbol):
    # type: (str) -> str
    return "%s%s" % (exchange_prefix(symbol), symbol)


def safe_float(value):
    # type: (Any) -> Optional[float]
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def http_get_text(url, referer=""):
    # type: (str, str) -> str
    headers = {"User-Agent": "Mozilla/5.0 (compatible; TradingAgentsCNMonitor/1.0)"}
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=10) as response:
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


def normalize_tencent_date(value):
    # type: (str) -> str
    text = str(value or "")
    if len(text) >= 8 and text[:8].isdigit():
        return "%s-%s-%s" % (text[:4], text[4:6], text[6:8])
    return text


def get_tencent_quote(symbol):
    # type: (str) -> Dict[str, Any]
    text = http_get_text("https://qt.gtimg.cn/q=" + prefixed_symbol(symbol), referer="https://gu.qq.com/")
    fields = parse_quoted_payload(text, "~")
    if len(fields) < 7:
        raise RuntimeError("unexpected tencent quote format")
    return {
        "symbol": symbol,
        "name": fields[1] or symbol,
        "current_price": safe_float(fields[3]),
        "change_pct": safe_float(fields[32]) if len(fields) > 32 else None,
        "volume": safe_float(fields[6]),
        "quote_date": normalize_tencent_date(fields[30] if len(fields) > 30 else ""),
        "source": "tencent_direct",
    }


def get_sina_quote(symbol):
    # type: (str) -> Dict[str, Any]
    text = http_get_text("https://hq.sinajs.cn/list=" + prefixed_symbol(symbol), referer="https://finance.sina.com.cn/")
    fields = parse_quoted_payload(text, ",")
    if len(fields) < 31:
        raise RuntimeError("unexpected sina quote format")
    previous = safe_float(fields[2])
    current = safe_float(fields[3])
    change_pct = (current - previous) / previous * 100 if current is not None and previous else None
    return {
        "symbol": symbol,
        "name": fields[0] or symbol,
        "current_price": current,
        "change_pct": change_pct,
        "volume": safe_float(fields[8]),
        "quote_date": fields[30] if len(fields) > 30 else "",
        "source": "sina_direct",
    }


def get_eastmoney_kline_quote(symbol):
    # type: (str) -> Dict[str, Any]
    quote = latest_quote_from_klines(symbol)
    if quote.get("current_price") is None:
        raise RuntimeError("eastmoney kline quote missing price")
    quote["source"] = quote.get("source") or "eastmoney_kline"
    return quote


def get_quote(symbol):
    # type: (str) -> Dict[str, Any]
    errors = []  # type: List[str]
    quote_source = os.getenv("MONITOR_QUOTE_SOURCE", "auto")
    if quote_source != "direct" and get_integrated_china_provider is not None:
        try:
            provider = get_integrated_china_provider()
            quote = provider.get_stock_info(symbol)
            if quote.get("current_price") is not None:
                return quote
        except Exception as exc:
            errors.append("integrated: %s" % exc)
    for fetcher in (get_eastmoney_kline_quote, get_tencent_quote, get_sina_quote):
        try:
            quote = fetcher(symbol)
            if quote.get("current_price") is not None:
                if errors:
                    quote["fallback_errors"] = errors
                return quote
        except Exception as exc:
            errors.append("%s: %s" % (fetcher.__name__, exc))
    return {"symbol": symbol, "current_price": None, "source": "none", "errors": errors}


def pct_distance(price, level):
    # type: (float, float) -> float
    if not level:
        return 0.0
    return (price / level - 1) * 100


def triggered_key(symbol, event, quote_date):
    # type: (str, str, str) -> str
    today = quote_date or now_cn().strftime("%Y-%m-%d")
    return "%s:%s:%s" % (symbol, event, today)


def evaluate_stock(stock, quote):
    # type: (Dict[str, Any], Dict[str, Any]) -> List[Dict[str, Any]]
    symbol = stock["symbol"]
    name = stock.get("name") or quote.get("name") or symbol
    levels = stock.get("levels", {})
    actions = stock.get("actions", {})
    volume_cfg = stock.get("volume", {})
    price = quote.get("current_price")
    volume = quote.get("volume")
    quote_date = str(quote.get("quote_date") or "")
    if price is None:
        return [{"event": "data_missing", "severity": "warning", "title": "%s 行情获取失败" % name, "body": "当前价格为空，请检查数据源。", "symbol": symbol, "name": name, "price": "N/A", "volume": volume, "quote_date": quote_date, "source": quote.get("source")}]

    events = []  # type: List[Dict[str, Any]]
    buy_low = levels.get("buy_zone_low")
    buy_high = levels.get("buy_zone_high")
    hard_stop = levels.get("hard_stop")
    breakout = levels.get("breakout")
    tp1 = levels.get("take_profit_1")
    tp2 = levels.get("take_profit_2")
    tp3_low = levels.get("take_profit_3_low")
    breakout_min_volume = volume_cfg.get("breakout_min_volume")
    selloff_volume = volume_cfg.get("selloff_volume")

    def add(event, severity, title, body):
        events.append({"event": event, "severity": severity, "title": title, "body": body, "symbol": symbol, "name": name, "price": price, "volume": volume, "quote_date": quote_date, "source": quote.get("source")})

    if hard_stop is not None and price <= hard_stop:
        extra = ""
        if selloff_volume and volume and volume >= selloff_volume:
            extra = "\n\n成交量 %.0f 已达到/超过放量阈值 %.0f，风控信号更强。" % (volume, selloff_volume)
        add("hard_stop", "critical", "%s 触发风控线" % name, "当前价 %.2f ≤ 风控线 %.2f。\n\n原计划：%s%s" % (price, hard_stop, actions.get("stop_action", "有效跌破风控线，降低仓位。"), extra))
    elif buy_low is not None and buy_high is not None and buy_low <= price <= buy_high:
        add("buy_zone", "info", "%s 进入低吸观察区" % name, "当前价 %.2f 位于 %.2f-%.2f。\n\n原计划：%s\n\n注意：不是自动买入，请人工确认成交量和大盘环境。" % (price, buy_low, buy_high, actions.get("buy_zone_action", "观察是否缩量止跌。")))
    elif breakout is not None and price >= breakout:
        volume_note = ""
        if breakout_min_volume:
            if volume and volume >= breakout_min_volume:
                volume_note = "\n\n成交量 %.0f ≥ 放量阈值 %.0f，突破质量较好。" % (volume, breakout_min_volume)
            else:
                volume_note = "\n\n当前成交量 %s，尚未确认达到放量阈值 %.0f，谨防假突破。" % (volume if volume is not None else "N/A", breakout_min_volume)
        add("breakout", "important", "%s 触发突破观察线" % name, "当前价 %.2f ≥ 突破线 %.2f。\n\n原计划：%s%s" % (price, breakout, actions.get("breakout_action", "突破后重新评估仓位。"), volume_note))

    if tp3_low is not None and price >= tp3_low:
        add("take_profit_3", "important", "%s 到达第三止盈区" % name, "当前价 %.2f ≥ %.2f。\n\n原计划：%s" % (price, tp3_low, actions.get("tp3_action", "兑现剩余部分或保留底仓。")))
    elif tp2 is not None and price >= tp2:
        add("take_profit_2", "important", "%s 到达第二止盈区" % name, "当前价 %.2f ≥ %.2f。\n\n原计划：%s" % (price, tp2, actions.get("tp2_action", "分批止盈。")))
    elif tp1 is not None and price >= tp1:
        add("take_profit_1", "info", "%s 到达第一止盈区" % name, "当前价 %.2f ≥ %.2f。\n\n原计划：%s" % (price, tp1, actions.get("tp1_action", "分批止盈。")))

    if not events:
        watched = []
        for label, level in [("低吸上沿", buy_high), ("风控线", hard_stop), ("突破线", breakout), ("止盈1", tp1)]:
            if level is not None:
                watched.append("%s %.2f（距离 %+.2f%%）" % (label, level, pct_distance(price, level)))
        events.append({"event": "heartbeat", "severity": "normal", "title": "%s 未触发关键线" % name, "body": "当前价 %.2f，未触发买入/止损/突破/止盈条件。\n%s" % (price, "\n".join(watched)), "symbol": symbol, "name": name, "price": price, "volume": volume, "quote_date": quote_date, "source": quote.get("source")})
    return events


def format_event(event, mode):
    # type: (Dict[str, Any], str) -> str
    severity_icon = {"critical": "🔴", "important": "🟠", "info": "🔵", "warning": "🟡", "normal": "⚪"}.get(event.get("severity"), "⚪")
    lines = [
        "## %s %s" % (severity_icon, event["title"]),
        "",
        "- 股票：%s（%s）" % (event.get("name"), event.get("symbol")),
        "- 当前价：%s" % event.get("price", "N/A"),
        "- 成交量：%s" % event.get("volume", "N/A"),
        "- 行情日期：%s" % (event.get("quote_date") or "N/A"),
        "- 数据源：%s" % (event.get("source") or "N/A"),
        "- 运行模式：%s" % mode,
        "",
        event.get("body", ""),
        "",
        "> 仅提醒，不自动交易；请结合仓位、风险承受能力和最新行情人工确认。",
    ]
    return "\n".join(lines)


def should_notify(event, state, repeat_heartbeat=False):
    # type: (Dict[str, Any], Dict[str, Any], bool) -> bool
    if event["event"] == "heartbeat":
        return repeat_heartbeat
    key = triggered_key(event.get("symbol", ""), event["event"], event.get("quote_date", ""))
    if state.get("sent", {}).get(key):
        return False
    state.setdefault("sent", {})[key] = now_cn().isoformat()
    return True


def main():
    parser = argparse.ArgumentParser(description="Monitor A-share watchlist and notify when levels are triggered.")
    parser.add_argument("--watchlist", default=str(DEFAULT_WATCHLIST))
    parser.add_argument("--state-file", default=str(STATE_FILE))
    parser.add_argument("--notify", action="store_true", help="Send DingTalk notifications when DINGTALK_WEBHOOK is configured")
    parser.add_argument("--force", action="store_true", help="Run outside A-share trading session")
    parser.add_argument("--repeat-heartbeat", action="store_true", help="Also notify non-trigger heartbeat messages")
    parser.add_argument("--ai-on-trigger", action="store_true", help="Use AI to explain triggered non-heartbeat events before notifying")
    parser.add_argument("--ai-no-llm", action="store_true", help="Validate AI explanation path without calling LLM")
    args = parser.parse_args()

    now = now_cn()
    if not args.force and not in_a_share_session(now):
        print("Skip: outside A-share session (%s)" % now.isoformat())
        return

    watchlist_path = Path(args.watchlist)
    config = load_json(watchlist_path, {})
    state_path = Path(args.state_file)
    state = load_json(state_path, {"sent": {}})
    notify_config = config.get("notify", {})
    webhook = os.getenv(notify_config.get("dingtalk_webhook_env", "DINGTALK_WEBHOOK"), "")
    secret = os.getenv(notify_config.get("dingtalk_secret_env", "DINGTALK_SECRET"), "")

    all_events = []  # type: List[Dict[str, Any]]
    for stock in config.get("stocks", []):
        if not stock.get("enabled", True):
            continue
        quote = get_quote(stock["symbol"])
        events = evaluate_stock(stock, quote)
        all_events.extend(events)
        for event in events:
            will_notify = bool(args.notify and webhook and should_notify(event, state, repeat_heartbeat=args.repeat_heartbeat))
            should_explain = args.ai_on_trigger and event.get("event") not in ("heartbeat", "data_missing") and (will_notify or args.force)
            if should_explain:
                explanation = explain_event(event, stock, ROOT, no_llm=args.ai_no_llm)
                if explanation:
                    event["body"] = "%s\n\n## AI解释\n%s" % (event.get("body", ""), explanation)
            message = format_event(event, "force" if args.force else "session")
            report_path = save_event_report(event, message)
            if report_path is not None:
                event["report_path"] = str(report_path)
                agent_log = start_agent_report(event, stock, report_path, notify=bool(args.notify and webhook))
                message = "%s\n\n- 快速事件报告：%s" % (message, report_path)
                if agent_log is not None:
                    message = "%s\n- 完整Agent报告：后台生成中，完成后会另发钉钉；日志：%s" % (message, agent_log)
            print("\n" + "=" * 80)
            print(message)
            if will_notify:
                result = send_markdown(webhook, event["title"], message, secret or None)
                print("DingTalk: %s" % result)

    state["last_run"] = now.isoformat()
    state["last_events"] = all_events
    write_json(state_path, state)


if __name__ == "__main__":
    main()
