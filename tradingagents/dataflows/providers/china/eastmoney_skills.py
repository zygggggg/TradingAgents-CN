"""东方财富 Skills / OpenClaw 金融工具适配器。

该模块只负责把公开的东方财富 Skills HTTP 形态封装成项目内可复用的
数据、资讯、选股、热点和组合监控工具。没有配置 API Key 时会明确报错，
不会伪造或兜底生成金融数据。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    import requests


DEFAULT_API_BASE = "https://mkapi2.dfcfs.com"
DEFAULT_TIMEOUT = 20
VAULT_PATH = Path.home() / ".openclaw" / "workspace" / "vault" / "credentials" / "eastmoney.json"


class EastmoneySkillError(RuntimeError):
    """东方财富 Skills 调用失败。"""


@dataclass
class EastmoneySkillCall:
    endpoint: str
    payload: Dict[str, Any]
    response: Dict[str, Any]


def _split_keys(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [item.strip() for item in value.replace(";", ",").split(",") if item.strip()]


def _load_vault_keys(path: Path = VAULT_PATH) -> List[str]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    keys = payload.get("keys") if isinstance(payload, dict) else None
    if isinstance(keys, list):
        return [str(item).strip() for item in keys if str(item).strip()]
    return []


def load_eastmoney_api_keys() -> List[str]:
    """按优先级加载东方财富 Skills API Key。"""

    keys: List[str] = []
    for name in (
        "EASTMONEY_APIKEY",
        "EASTMONEY_SKILLS_API_KEY",
        "EASTMONEY_SKILLS_ACCESS_KEY",
        "MX_APIKEY",
    ):
        keys.extend(_split_keys(os.getenv(name)))

    vault_path = Path(os.getenv("EASTMONEY_SKILLS_VAULT", str(VAULT_PATH))).expanduser()
    keys.extend(_load_vault_keys(vault_path))

    placeholder_tokens = ("your_", "_here", "你的", "示例", "placeholder")
    seen = set()
    unique_keys = []
    for key in keys:
        lowered = key.lower()
        if any(token in lowered for token in placeholder_tokens):
            continue
        if key not in seen:
            seen.add(key)
            unique_keys.append(key)
    return unique_keys


def eastmoney_skills_available() -> bool:
    if os.getenv("EASTMONEY_SKILLS_ENABLED", "true").lower() in {"0", "false", "no", "off"}:
        return False
    return bool(load_eastmoney_api_keys())


def _json_text(payload: Any, max_chars: int = 40000) -> str:
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if max_chars > 0 and len(text) > max_chars:
        return text[:max_chars] + "\n...\n"
    return text


def _is_trivial_text(value: str) -> bool:
    return value.strip().lower() in {"ok", "success", "true", "none", "null"}


def _as_cell(value: Any) -> str:
    if isinstance(value, list):
        if not value:
            return ""
        return str(value[0]) if len(value) == 1 else " / ".join(str(item) for item in value[:6])
    if value is None:
        return ""
    return str(value)


def _markdown_table(headers: List[str], rows: List[List[str]]) -> str:
    if not headers or not rows:
        return ""
    header_line = "| " + " | ".join(headers) + " |"
    sep_line = "| " + " | ".join("---" for _ in headers) + " |"
    row_lines = ["| " + " | ".join(str(cell).replace("\n", " ") for cell in row) + " |" for row in rows]
    return "\n".join([header_line, sep_line, *row_lines])


def _format_data_table(item: Dict[str, Any], max_indicators: int = 18, max_periods: int = 8) -> str:
    table = item.get("table") if isinstance(item, dict) else None
    if not isinstance(table, dict) or not table:
        return ""

    name_map = item.get("nameMap") if isinstance(item.get("nameMap"), dict) else {}
    indicator_order = item.get("indicatorOrder") if isinstance(item.get("indicatorOrder"), list) else []
    keys = [str(key) for key in indicator_order if key in table and key != "headName"]
    keys.extend(str(key) for key in table.keys() if key not in keys and key != "headName")
    keys = keys[:max_indicators]
    if not keys:
        return ""

    head_names = table.get("headName") if isinstance(table.get("headName"), list) else []
    if head_names and any(isinstance(table.get(key), list) and len(table.get(key) or []) > 1 for key in keys):
        periods = [str(item) for item in head_names[:max_periods]]
        rows: List[List[str]] = []
        for key in keys:
            values = table.get(key) or []
            if not isinstance(values, list):
                values = [values]
            label = str(name_map.get(key) or key)
            rows.append([label, *[str(value) for value in values[: len(periods)]]])
        return _markdown_table(["指标", *periods], rows)

    rows = [[str(name_map.get(key) or key), _as_cell(table.get(key))] for key in keys]
    return _markdown_table(["指标", "数值"], rows)


def _format_search_data(payload: Dict[str, Any], max_tables: int = 6) -> str:
    inner = payload.get("data", {}).get("data", {}) if isinstance(payload, dict) else {}
    dto = inner.get("searchDataResultDTO") if isinstance(inner, dict) else None
    tables = dto.get("dataTableDTOList") if isinstance(dto, dict) else None
    if not isinstance(tables, list) or not tables:
        return ""

    parts = []
    for index, item in enumerate(tables[:max_tables], 1):
        if not isinstance(item, dict):
            continue
        title = item.get("title") or item.get("frontendTitle") or item.get("entityName") or f"数据表 {index}"
        rendered = _format_data_table(item)
        if rendered:
            parts.extend([f"### {title}", "", rendered, ""])
    return "\n".join(parts).strip()


def _format_news_data(payload: Dict[str, Any], max_items: int = 10, content_chars: int = 520) -> str:
    inner = payload.get("data", {}).get("data", {}) if isinstance(payload, dict) else {}
    search_response = inner.get("llmSearchResponse") if isinstance(inner, dict) else None
    items = search_response.get("data") if isinstance(search_response, dict) else None
    if not isinstance(items, list) or not items:
        return ""

    lines = [f"共找到 {len(items)} 条相关资讯，以下展示前 {min(max_items, len(items))} 条。", ""]
    for index, item in enumerate(items[:max_items], 1):
        if not isinstance(item, dict):
            continue
        title = item.get("title") or "无标题"
        meta = []
        for key, label in (
            ("entityFullName", "证券"),
            ("insName", "机构"),
            ("date", "日期"),
            ("informationType", "类型"),
            ("rating", "评级"),
        ):
            value = item.get(key)
            if value:
                meta.append(f"{label}: {str(value).split()[0] if key == 'date' else value}")
        content = str(item.get("content") or "").strip().replace("\r", " ")
        if len(content) > content_chars:
            content = content[:content_chars].rstrip() + "……"
        lines.append(f"### {index}. {title}")
        if meta:
            lines.append(" | ".join(meta))
        if content:
            lines.extend(["", content])
        lines.append("")
    return "\n".join(lines).strip()


def _first_text(payload: Any) -> str:
    """从常见返回结构中抽取可读文本，保留原始 JSON 作为兜底展示。"""

    if isinstance(payload, str):
        return payload.strip()
    if isinstance(payload, list):
        parts = [_first_text(item) for item in payload]
        return "\n".join(part for part in parts if part).strip()
    if not isinstance(payload, dict):
        return ""

    search_data_text = _format_search_data(payload)
    if search_data_text:
        return search_data_text

    news_text = _format_news_data(payload)
    if news_text:
        return news_text

    for key in ("answer", "content", "summary", "text", "markdown", "message", "msg"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip() and not _is_trivial_text(value):
            return value.strip()

    for key in ("data", "result", "results", "rows", "list"):
        value = payload.get(key)
        text = _first_text(value)
        if text:
            return text
    return ""


def _common_error_text(payload: Dict[str, Any]) -> str:
    for key in ("message", "msg", "error", "errmsg", "errorMsg"):
        value = payload.get(key)
        if value:
            return str(value)
    return json.dumps(payload, ensure_ascii=False, default=str)


class EastmoneySkillsClient:
    """东方财富 Skills HTTP 客户端。

    公开 OpenClaw Skill 形态目前主要包含三个端点：
    - `/finskillshub/api/claw/query`：金融数据查询
    - `/finskillshub/api/claw/news-search`：资讯搜索
    - `/finskillshub/api/claw/stock-screen`：智能选股

    其他能力（热点、诊股、自选股/模拟组合）先通过这三个基础端点组合出
    只读分析上下文，等拿到官方更细的 Skill 描述后再补专用端点。
    """

    def __init__(
        self,
        api_keys: Optional[Iterable[str]] = None,
        api_base: Optional[str] = None,
        timeout: Optional[int] = None,
        session: Optional["requests.Session"] = None,
    ) -> None:
        self.api_keys = list(api_keys) if api_keys is not None else load_eastmoney_api_keys()
        self.api_base = (api_base or os.getenv("EASTMONEY_SKILLS_API_BASE") or DEFAULT_API_BASE).rstrip("/")
        self.timeout = timeout or int(os.getenv("EASTMONEY_SKILLS_TIMEOUT", str(DEFAULT_TIMEOUT)))
        self.session = session

    @property
    def enabled(self) -> bool:
        return bool(self.api_keys) and os.getenv("EASTMONEY_SKILLS_ENABLED", "true").lower() not in {"0", "false", "no", "off"}

    def _url(self, endpoint: str) -> str:
        endpoint = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        return f"{self.api_base}{endpoint}"

    def _post(self, endpoint: str, payload: Dict[str, Any]) -> EastmoneySkillCall:
        if not self.enabled:
            raise EastmoneySkillError(
                "东方财富 Skills API Key 未配置；请设置 EASTMONEY_APIKEY，或配置 "
                "~/.openclaw/workspace/vault/credentials/eastmoney.json"
            )

        if self.session is None:
            import requests

            self.session = requests.Session()
            self.session.trust_env = os.getenv("EASTMONEY_SKILLS_TRUST_ENV", "false").lower() == "true"

        url = self._url(endpoint)
        errors: List[str] = []
        for api_key in self.api_keys:
            try:
                response = self.session.post(
                    url,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json,text/plain,*/*",
                        "apikey": api_key,
                    },
                    json=payload,
                    timeout=self.timeout,
                )
                response.raise_for_status()
                data = response.json()
                if isinstance(data, dict):
                    status = data.get("status", data.get("code"))
                    if status not in (None, 0, "0", 200, "200", True):
                        errors.append(f"{endpoint}: status={status}, {_common_error_text(data)}")
                        continue
                    return EastmoneySkillCall(endpoint=endpoint, payload=payload, response=data)
                return EastmoneySkillCall(endpoint=endpoint, payload=payload, response={"data": data})
            except Exception as exc:
                errors.append(f"{endpoint}: {exc}")
                continue

        raise EastmoneySkillError("; ".join(errors[-3:]) or f"东方财富 Skills 调用失败: {endpoint}")

    def query(self, tool_query: str) -> EastmoneySkillCall:
        return self._post("/finskillshub/api/claw/query", {"toolQuery": tool_query})

    def search_news(self, query: str) -> EastmoneySkillCall:
        return self._post("/finskillshub/api/claw/news-search", {"query": query})

    def screen_stocks(self, keyword: str, page_no: int = 1, page_size: int = 20) -> EastmoneySkillCall:
        return self._post(
            "/finskillshub/api/claw/stock-screen",
            {"keyword": keyword, "pageNo": page_no, "pageSize": page_size},
        )

    def format_call(self, title: str, call: EastmoneySkillCall, query_text: str) -> str:
        max_chars = int(os.getenv("EASTMONEY_SKILLS_MAX_REPORT_CHARS", "40000"))
        extracted = _first_text(call.response)
        lines = [
            f"# {title}",
            "",
            "数据来源: 东方财富 Skills / OpenClaw 金融工具",
            f"调用端点: `{call.endpoint}`",
            f"查询语句: {query_text}",
            "",
        ]
        if extracted:
            lines.extend(["## 可读结果", extracted, ""])
        lines.extend(["## 原始返回JSON", "```json", _json_text(call.response, max_chars=max_chars), "```"])
        return "\n".join(lines).strip() + "\n"

    def fundamentals_report(self, symbol: str, stock_name: Optional[str] = None, report_count: int = 5) -> str:
        name = f"{stock_name}（{symbol}）" if stock_name else symbol
        query = (
            f"请查询{name}的最新行情、PE、PB、ROE、市值、主营业务、股东结构，"
            f"以及最近{report_count}个报告期的营业收入、归母净利润、扣非净利润、毛利率、净利率、"
            "资产负债率、经营现金流净额，并尽量按表格返回。"
        )
        call = self.query(query)
        return self.format_call(f"{name} 东方财富 Skills 金融数据", call, query)

    def news_report(self, symbol: str, stock_name: Optional[str] = None, limit_hint: int = 10) -> str:
        name = f"{stock_name}（{symbol}）" if stock_name else symbol
        query = f"{name} 最新新闻、公告、研报、政策解读、重大事件，返回最近{limit_hint}条并标注来源和时间。"
        call = self.search_news(query)
        return self.format_call(f"{name} 东方财富 Skills 资讯搜索", call, query)

    def hotspot_report(self, focus: str = "A股市场热点、AI、算力、机器人、军工") -> str:
        query = f"请梳理今日{focus}相关热点板块、领涨股票、新闻催化剂和持续性风险。"
        call = self.search_news(query)
        return self.format_call("东方财富 Skills 热点发现", call, query)

    def stock_diagnosis_report(self, symbol: str, stock_name: Optional[str] = None) -> str:
        name = f"{stock_name}（{symbol}）" if stock_name else symbol
        query = (
            f"请对{name}做综合诊股，包含行情趋势、估值、财务质量、资金流、新闻公告催化剂、"
            "主要风险、支撑压力位和风险收益比。"
        )
        call = self.query(query)
        return self.format_call(f"{name} 东方财富 Skills 综合诊股", call, query)

    def low_risk_repair_screen_report(self, page_size: int = 30) -> str:
        keyword = (
            "A股近60日涨幅小于25%，股价距离MA60小于15%，一季报净利润或扣非净利润改善，"
            "经营现金流没有明显恶化，成交量不是高位爆量滞涨，技术面低位修复，"
            "目标价与止损空间风险收益比大于2比1，排除连续加速缩量新高"
        )
        call = self.screen_stocks(keyword, page_no=1, page_size=page_size)
        return self.format_call("东方财富 Skills 智能选股：低位修复池", call, keyword)

    def watchlist_report(self, symbols: Iterable[str]) -> str:
        items = [str(symbol).strip() for symbol in symbols if str(symbol).strip()]
        query = (
            "请按自选股监控表形式返回以下股票的最新价、涨跌幅、成交额、换手率、PE/PB、"
            "近5日涨跌、支撑压力、异动原因和风险提示：" + "、".join(items)
        )
        call = self.query(query)
        return self.format_call("东方财富 Skills 自选股监控", call, query)

    def portfolio_risk_report(self, positions: Iterable[Dict[str, Any]]) -> str:
        normalized = []
        for item in positions:
            if not isinstance(item, dict):
                continue
            normalized.append(
                {
                    "symbol": item.get("symbol") or item.get("code"),
                    "name": item.get("name"),
                    "weight": item.get("weight"),
                    "cost": item.get("cost"),
                    "shares": item.get("shares"),
                }
            )
        query = (
            "请基于以下模拟组合持仓做组合复盘和风控，返回行业/主题暴露、单票风险、"
            "止损止盈建议、需要关注的公告新闻和明日监控重点："
            + json.dumps(normalized, ensure_ascii=False, default=str)
        )
        call = self.query(query)
        return self.format_call("东方财富 Skills 模拟组合风控", call, query)


_client: Optional[EastmoneySkillsClient] = None


def get_eastmoney_skills_client() -> EastmoneySkillsClient:
    global _client
    if _client is None:
        _client = EastmoneySkillsClient()
    return _client
