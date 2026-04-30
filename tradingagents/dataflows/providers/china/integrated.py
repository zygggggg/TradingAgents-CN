"""Integrated China A-share market data providers.

This module keeps A-share data access out of the agent/validation flow and
offers one stable entry point with deterministic fallback order.  It prefers
official/configured providers when credentials are available and falls back to
public quote endpoints that are reachable from domestic networks.
"""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd
import requests

from tradingagents.utils.logging_manager import get_logger


logger = get_logger("dataflows")


DEFAULT_SOURCE_ORDER = "eastmoney,tencent,sina,jqdata,ricequant,ifind"


@dataclass
class ProviderResult:
    source: str
    ok: bool
    data: object = None
    error: str = ""


class IntegratedChinaMarketDataProvider:
    """Unified A-share data provider with explicit fallback order."""

    def __init__(self, timeout: int = 12):
        self.timeout = timeout
        self.session = requests.Session()
        # macOS/Python may silently pick a stale system proxy.  Public China
        # finance endpoints often fail through it, so ignore ambient proxies by
        # default.  Users can opt in with CHINA_MARKET_DATA_TRUST_ENV=true.
        self.session.trust_env = os.getenv("CHINA_MARKET_DATA_TRUST_ENV", "false").lower() == "true"
        self.headers = {
            "User-Agent": os.getenv(
                "CHINA_MARKET_DATA_USER_AGENT",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            ),
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Connection": "close",
            "Referer": "https://quote.eastmoney.com/",
        }

    def source_order(self) -> List[str]:
        raw = os.getenv("CHINA_MARKET_DATA_SOURCES", DEFAULT_SOURCE_ORDER)
        order = [item.strip().lower() for item in raw.split(",") if item.strip()]
        try:
            from .eastmoney_skills import eastmoney_skills_available

            if eastmoney_skills_available() and "eastmoney_skills" not in order:
                order.insert(0, "eastmoney_skills")
        except Exception:
            pass
        return order

    def get_stock_info(self, symbol: str) -> Dict:
        symbol = normalize_symbol(symbol)
        errors: List[str] = []

        for source in self.source_order():
            result = self._try_info_source(source, symbol)
            if result.ok and isinstance(result.data, dict) and is_valid_stock_info(result.data, symbol):
                return result.data
            if result.error:
                errors.append(f"{source}: {result.error}")

        logger.warning("⚠️ [A股统一数据源] 基本信息全部失败: %s | %s", symbol, "; ".join(errors[-5:]))
        return {
            "symbol": symbol,
            "name": f"股票{symbol}",
            "source": "integrated_failed",
            "error": "; ".join(errors[-5:]),
        }

    def get_stock_data(self, symbol: str, start_date: str, end_date: str, period: str = "daily") -> str:
        symbol = normalize_symbol(symbol)
        errors: List[str] = []

        for source in self.source_order():
            result = self._try_history_source(source, symbol, start_date, end_date, period)
            if result.ok and isinstance(result.data, pd.DataFrame) and not result.data.empty:
                info = self.get_stock_info(symbol)
                stock_name = info.get("name") or f"股票{symbol}"
                return format_stock_data_report(
                    df=result.data,
                    symbol=symbol,
                    stock_name=stock_name,
                    start_date=start_date,
                    end_date=end_date,
                    source=result.source,
                    quote_info=info,
                )
            if result.error:
                errors.append(f"{source}: {result.error}")

        return (
            f"❌ 所有A股统一数据源都无法获取{symbol}的{period}数据\n"
            f"失败详情: {'; '.join(errors[-6:])}"
        )

    def get_fundamentals_data(self, symbol: str, report_count: int = 5) -> str:
        symbol = normalize_symbol(symbol)
        errors: List[str] = []

        for source in self.source_order():
            try:
                if source == "eastmoney_skills":
                    result = self._eastmoney_skills_fundamentals(symbol, report_count)
                    if result.ok and isinstance(result.data, str) and result.data.strip():
                        return result.data
                    errors.append(f"eastmoney_skills: {result.error or 'empty financial payload'}")
                elif source == "eastmoney":
                    payload = self._eastmoney_fundamentals(symbol, report_count=report_count)
                    if payload.get("main_indicators"):
                        return format_fundamentals_report(symbol, payload)
                    errors.append("eastmoney: empty financial payload")
                elif source == "jqdata":
                    result = self._jqdata_fundamentals(symbol, report_count)
                    if result.ok and isinstance(result.data, str) and result.data.strip():
                        return result.data
                    errors.append(f"jqdata: {result.error or 'empty financial payload'}")
                elif source == "ricequant":
                    result = self._ricequant_fundamentals(symbol, report_count)
                    if result.ok and isinstance(result.data, str) and result.data.strip():
                        return result.data
                    errors.append(f"ricequant: {result.error or 'empty financial payload'}")
                elif source == "ifind":
                    result = self._ifind_fundamentals(symbol, report_count)
                    if result.ok and isinstance(result.data, str) and result.data.strip():
                        return result.data
                    errors.append(f"ifind: {result.error or 'empty financial payload'}")
            except Exception as exc:
                logger.debug("A股基本面源 %s 获取失败: %s", source, exc)
                errors.append(f"{source}: {exc}")

        return (
            f"❌ 所有A股统一基本面数据源都无法获取{symbol}的财务数据\n"
            f"失败详情: {'; '.join(errors[-8:])}"
        )

    def _try_info_source(self, source: str, symbol: str) -> ProviderResult:
        try:
            if source == "eastmoney_skills":
                return self._eastmoney_skills_not_structured(symbol, "stock_info")
            if source == "eastmoney":
                return ProviderResult(source, True, self._eastmoney_info(symbol))
            if source == "tencent":
                return ProviderResult(source, True, self._tencent_info(symbol))
            if source == "sina":
                return ProviderResult(source, True, self._sina_info(symbol))
            if source == "jqdata":
                return self._jqdata_info(symbol)
            if source == "ricequant":
                return self._ricequant_info(symbol)
            if source == "ifind":
                return self._ifind_not_ready(symbol)
            return ProviderResult(source, False, error="unknown provider")
        except Exception as exc:
            logger.debug("A股信息源 %s 获取失败: %s", source, exc)
            return ProviderResult(source, False, error=str(exc))

    def _try_history_source(
        self,
        source: str,
        symbol: str,
        start_date: str,
        end_date: str,
        period: str,
    ) -> ProviderResult:
        try:
            if source == "eastmoney_skills":
                return self._eastmoney_skills_not_structured(symbol, "history")
            if source == "eastmoney":
                return ProviderResult(source, True, self._eastmoney_history(symbol, start_date, end_date, period))
            if source == "tencent":
                return ProviderResult(source, True, self._tencent_history(symbol, start_date, end_date, period))
            if source == "sina":
                return ProviderResult(source, True, self._sina_history(symbol, start_date, end_date, period))
            if source == "jqdata":
                return self._jqdata_history(symbol, start_date, end_date, period)
            if source == "ricequant":
                return self._ricequant_history(symbol, start_date, end_date, period)
            if source == "ifind":
                return self._ifind_not_ready(symbol)
            return ProviderResult(source, False, error="unknown provider")
        except Exception as exc:
            logger.debug("A股历史源 %s 获取失败: %s", source, exc)
            return ProviderResult(source, False, error=str(exc))

    def _get(self, url: str, *, params: Optional[Dict] = None, referer: Optional[str] = None) -> requests.Response:
        headers = dict(self.headers)
        if referer:
            headers["Referer"] = referer
        retries = max(1, int(os.getenv("CHINA_MARKET_DATA_RETRIES", "4")))
        backoff = max(0.0, float(os.getenv("CHINA_MARKET_DATA_BACKOFF_SECONDS", "0.8")))
        last_error: Optional[Exception] = None
        candidate_urls = eastmoney_request_urls(url) if "eastmoney.com" in url else [url]

        for attempt in range(1, retries + 1):
            for candidate_url in candidate_urls:
                try:
                    response = self.session.get(candidate_url, params=params, headers=headers, timeout=self.timeout)
                    response.raise_for_status()
                    if not response.text.strip():
                        raise RuntimeError("empty response")
                    return response
                except (requests.RequestException, RuntimeError) as exc:
                    last_error = exc
                    logger.debug("A股数据请求失败 %s/%s [%s]: %s", attempt, retries, candidate_url, exc)
                    if "eastmoney.com" in candidate_url:
                        curl_response = self._curl_get(candidate_url, params=params, headers=headers)
                        if curl_response is not None:
                            return curl_response
            if attempt < retries:
                sleep_seconds = backoff * attempt + random.uniform(0, 0.35)
                if sleep_seconds:
                    time.sleep(sleep_seconds)
        raise RuntimeError(f"request failed after {retries} attempts: {last_error}")

    def _curl_get(self, url: str, *, params: Optional[Dict], headers: Dict[str, str]) -> Optional[requests.Response]:
        full_url = requests.Request("GET", url, params=params).prepare().url or url
        command = [
            "curl",
            "--http1.1",
            "-sS",
            "--compressed",
            "--connect-timeout",
            str(min(self.timeout, 8)),
            "--max-time",
            str(self.timeout + 8),
            "--retry",
            "1",
            "--retry-delay",
            "1",
            "-A",
            headers.get("User-Agent", self.headers["User-Agent"]),
            "-e",
            headers.get("Referer", self.headers["Referer"]),
            full_url,
        ]
        try:
            completed = subprocess.run(command, text=True, capture_output=True, timeout=self.timeout + 12)
        except Exception as exc:
            logger.debug("curl 兜底请求异常 [%s]: %s", url, exc)
            return None
        body = (completed.stdout or "").strip()
        if completed.returncode != 0 or not body:
            logger.debug("curl 兜底请求失败 [%s]: %s", url, (completed.stderr or "")[:200])
            return None
        response = requests.Response()
        response.status_code = 200
        response.url = full_url
        response._content = body.encode("utf-8")
        response.encoding = "utf-8"
        return response

    def _eastmoney_info(self, symbol: str) -> Dict:
        # The realtime push2 endpoint can be unstable on some networks.  The
        # historical endpoint is more resilient and also returns the stock name.
        df, name = self._eastmoney_history_frame(symbol, latest_only=True)
        info = {
            "symbol": symbol,
            "name": name or f"股票{symbol}",
            "area": "未知",
            "industry": "未知",
            "market": infer_market_name(symbol),
            "list_date": "未知",
            "source": "eastmoney",
        }
        if not df.empty:
            last = df.iloc[-1]
            info.update(
                {
                    "current_price": safe_float(last.get("close")),
                    "change_pct": safe_float(last.get("pct_change")),
                    "volume": safe_float(last.get("volume")),
                    "quote_date": str(last.get("date")),
                    "quote_source": "eastmoney_kline",
                }
            )
        return info

    def _eastmoney_history(self, symbol: str, start_date: str, end_date: str, period: str) -> pd.DataFrame:
        df, _ = self._eastmoney_history_frame(symbol, start_date, end_date, period)
        return df

    def _eastmoney_history_frame(
        self,
        symbol: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        period: str = "daily",
        latest_only: bool = False,
    ) -> Tuple[pd.DataFrame, str]:
        klt = {"daily": "101", "weekly": "102", "monthly": "103"}.get(period, "101")
        begin = to_yyyymmdd(start_date) if start_date else "19900101"
        end = to_yyyymmdd(end_date) if end_date else "20500101"
        params = {
            "secid": eastmoney_secid(symbol),
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": klt,
            "fqt": os.getenv("CHINA_MARKET_DATA_FQ", "1"),
            "beg": begin,
            "end": end,
        }
        if latest_only:
            params["lmt"] = "1"
        response = self._get("https://1.push2his.eastmoney.com/api/qt/stock/kline/get", params=params)
        payload = response.json()
        data = payload.get("data") or {}
        klines = data.get("klines") or []
        rows = []
        for item in klines:
            parts = item.split(",")
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
                }
            )
        return normalize_history_dataframe(rows), data.get("name", "")

    def _eastmoney_fundamentals(self, symbol: str, report_count: int = 5) -> Dict[str, Any]:
        em_code = eastmoney_symbol(symbol)
        main_indicators = self._eastmoney_finance_main_indicators(em_code, report_count)
        report_dates = [str(item.get("REPORT_DATE", ""))[:10] for item in main_indicators if item.get("REPORT_DATE")]
        report_dates = [date for date in report_dates if date]
        quote_info = self._eastmoney_info(symbol)

        statements = {
            "balance_sheet": self._eastmoney_finance_statement(em_code, "zcfzb", report_dates),
            "income_statement": self._eastmoney_finance_statement(em_code, "lrb", report_dates),
            "cash_flow": self._eastmoney_finance_statement(em_code, "xjllb", report_dates),
        }

        return {
            "symbol": symbol,
            "eastmoney_code": em_code,
            "source": "eastmoney_finance",
            "quote_info": quote_info,
            "main_indicators": main_indicators,
            **statements,
        }

    def _eastmoney_finance_main_indicators(self, em_code: str, report_count: int) -> List[Dict[str, Any]]:
        payload = self._get(
            "https://datacenter.eastmoney.com/securities/api/data/v1/get",
            params={
                "reportName": "RPT_F10_FINANCE_MAINFINADATA",
                "columns": "ALL",
                "filter": f'(SECUCODE="{em_code}")',
                "pageNumber": "1",
                "pageSize": str(report_count),
                "sortTypes": "-1",
                "sortColumns": "REPORT_DATE",
                "source": "HSF10",
                "client": "PC",
            },
            referer="https://emweb.securities.eastmoney.com/",
        ).json()
        return extract_eastmoney_rows(payload)

    def _eastmoney_finance_statement(self, em_code: str, statement: str, report_dates: List[str]) -> List[Dict[str, Any]]:
        if not report_dates:
            return []

        endpoint_map = {
            "zcfzb": "zcfzbAjaxNew",
            "lrb": "lrbAjaxNew",
            "xjllb": "xjllbAjaxNew",
        }
        endpoint = endpoint_map[statement]
        payload = self._get(
            f"https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/{endpoint}",
            params={
                "companyType": "4",
                "reportDateType": "0",
                "reportType": "1",
                "dates": ",".join(report_dates[:5]),
                "code": em_code,
            },
            referer="https://emweb.securities.eastmoney.com/",
        ).json()
        return extract_eastmoney_rows(payload)

    def _tencent_info(self, symbol: str) -> Dict:
        text = self._get(
            "https://qt.gtimg.cn/q=" + prefixed_symbol(symbol),
            referer="https://gu.qq.com/",
        ).content.decode("gbk", errors="ignore")
        fields = parse_quoted_payload(text, sep="~")
        if len(fields) < 6:
            raise RuntimeError("unexpected tencent quote format")
        current = safe_float(fields[3])
        previous = safe_float(fields[4])
        change_pct = None
        if len(fields) > 32:
            change_pct = safe_float(fields[32])
        if change_pct is None and current is not None and previous:
            change_pct = (current - previous) / previous * 100
        return {
            "symbol": symbol,
            "name": fields[1] or f"股票{symbol}",
            "area": "未知",
            "industry": "未知",
            "market": infer_market_name(symbol),
            "list_date": "未知",
            "current_price": current,
            "change_pct": change_pct,
            "volume": safe_float(fields[6]) if len(fields) > 6 else None,
            "source": "tencent",
        }

    def _tencent_history(self, symbol: str, start_date: str, end_date: str, period: str) -> pd.DataFrame:
        period_key = {"daily": "day", "weekly": "week", "monthly": "month"}.get(period, "day")
        params = {
            "param": f"{prefixed_symbol(symbol)},{period_key},{start_date},{end_date},320,qfq",
        }
        payload = self._get(
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
            params=params,
            referer="https://gu.qq.com/",
        ).json()
        data = (payload.get("data") or {}).get(prefixed_symbol(symbol), {})
        rows = data.get("qfq" + period_key) or data.get(period_key) or []
        parsed = []
        for row in rows:
            if len(row) < 6:
                continue
            parsed.append(
                {
                    "date": row[0],
                    "open": safe_float(row[1]),
                    "close": safe_float(row[2]),
                    "high": safe_float(row[3]),
                    "low": safe_float(row[4]),
                    "volume": safe_float(row[5]),
                }
            )
        return normalize_history_dataframe(parsed)

    def _sina_info(self, symbol: str) -> Dict:
        text = self._get(
            "https://hq.sinajs.cn/list=" + prefixed_symbol(symbol),
            referer="https://finance.sina.com.cn/",
        ).content.decode("gbk", errors="ignore")
        fields = parse_quoted_payload(text, sep=",")
        if len(fields) < 10:
            raise RuntimeError("unexpected sina quote format")
        current = safe_float(fields[3])
        previous = safe_float(fields[2])
        change_pct = (current - previous) / previous * 100 if current is not None and previous else None
        return {
            "symbol": symbol,
            "name": fields[0] or f"股票{symbol}",
            "area": "未知",
            "industry": "未知",
            "market": infer_market_name(symbol),
            "list_date": "未知",
            "current_price": current,
            "change_pct": change_pct,
            "volume": safe_float(fields[8]),
            "quote_date": fields[30] if len(fields) > 30 else "",
            "source": "sina",
        }

    def _sina_history(self, symbol: str, start_date: str, end_date: str, period: str) -> pd.DataFrame:
        scale = {"daily": "240", "weekly": "1200", "monthly": "7200"}.get(period, "240")
        datalen = max(days_between(start_date, end_date) + 30, 90)
        params = {"symbol": prefixed_symbol(symbol), "scale": scale, "ma": "no", "datalen": str(datalen)}
        text = self._get(
            "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
            params=params,
            referer="https://finance.sina.com.cn/",
        ).text
        payload = json.loads(text)
        rows = []
        for item in payload:
            date = item.get("day")
            if start_date <= date <= end_date:
                rows.append(
                    {
                        "date": date,
                        "open": safe_float(item.get("open")),
                        "close": safe_float(item.get("close")),
                        "high": safe_float(item.get("high")),
                        "low": safe_float(item.get("low")),
                        "volume": safe_float(item.get("volume")),
                    }
                )
        return normalize_history_dataframe(rows)

    def _jqdata_info(self, symbol: str) -> ProviderResult:
        try:
            import jqdatasdk as jq
        except Exception as exc:
            return ProviderResult("jqdata", False, error=f"jqdatasdk未安装: {exc}")
        username = os.getenv("JQDATA_USERNAME")
        password = os.getenv("JQDATA_PASSWORD")
        if not username or not password:
            return ProviderResult("jqdata", False, error="JQDATA_USERNAME/JQDATA_PASSWORD未配置")
        jq.auth(username, password)
        security = jq.normalize_code(to_jq_code(symbol))
        info = jq.get_security_info(security)
        return ProviderResult(
            "jqdata",
            True,
            {
                "symbol": symbol,
                "name": getattr(info, "display_name", None) or getattr(info, "name", None) or f"股票{symbol}",
                "area": "未知",
                "industry": "未知",
                "market": infer_market_name(symbol),
                "list_date": str(getattr(info, "start_date", "未知")),
                "source": "jqdata",
            },
        )

    def _jqdata_history(self, symbol: str, start_date: str, end_date: str, period: str) -> ProviderResult:
        try:
            import jqdatasdk as jq
        except Exception as exc:
            return ProviderResult("jqdata", False, error=f"jqdatasdk未安装: {exc}")
        username = os.getenv("JQDATA_USERNAME")
        password = os.getenv("JQDATA_PASSWORD")
        if not username or not password:
            return ProviderResult("jqdata", False, error="JQDATA_USERNAME/JQDATA_PASSWORD未配置")
        jq.auth(username, password)
        frequency = {"daily": "daily", "weekly": "weekly", "monthly": "monthly"}.get(period, "daily")
        df = jq.get_price(
            to_jq_code(symbol),
            start_date=start_date,
            end_date=end_date,
            frequency=frequency,
            fields=["open", "close", "high", "low", "volume", "money"],
        )
        if df is None or df.empty:
            return ProviderResult("jqdata", False, error="返回空数据")
        df = df.reset_index().rename(columns={"index": "date", "money": "amount"})
        return ProviderResult("jqdata", True, normalize_history_dataframe(df.to_dict("records")))

    def _ricequant_info(self, symbol: str) -> ProviderResult:
        init_result = init_ricequant()
        if not init_result.ok:
            return init_result
        import rqdatac

        instrument = rqdatac.instruments(to_rq_code(symbol))
        return ProviderResult(
            "ricequant",
            True,
            {
                "symbol": symbol,
                "name": getattr(instrument, "symbol", None) or getattr(instrument, "abbrev_symbol", None) or f"股票{symbol}",
                "area": "未知",
                "industry": "未知",
                "market": infer_market_name(symbol),
                "list_date": str(getattr(instrument, "listed_date", "未知")),
                "source": "ricequant",
            },
        )

    def _ricequant_history(self, symbol: str, start_date: str, end_date: str, period: str) -> ProviderResult:
        init_result = init_ricequant()
        if not init_result.ok:
            return init_result
        import rqdatac

        frequency = {"daily": "1d", "weekly": "1w", "monthly": "1M"}.get(period, "1d")
        df = rqdatac.get_price(
            to_rq_code(symbol),
            start_date=start_date,
            end_date=end_date,
            frequency=frequency,
            fields=["open", "close", "high", "low", "volume", "total_turnover"],
            expect_df=True,
        )
        if df is None or df.empty:
            return ProviderResult("ricequant", False, error="返回空数据")
        df = df.reset_index().rename(columns={"datetime": "date", "total_turnover": "amount"})
        return ProviderResult("ricequant", True, normalize_history_dataframe(df.to_dict("records")))

    def _ifind_not_ready(self, symbol: str) -> ProviderResult:
        # 同花顺 iFinD 是商业终端 SDK，字段和函数权限依账号而异。
        # 这里明确预留入口，避免使用不稳定/非授权网页抓取。
        if not os.getenv("IFIND_USERNAME") or not os.getenv("IFIND_PASSWORD"):
            return ProviderResult("ifind", False, error="IFIND_USERNAME/IFIND_PASSWORD未配置")
        return ProviderResult("ifind", False, error="iFinD SDK字段映射未配置，请按账号权限补充官方SDK调用")

    def _jqdata_fundamentals(self, symbol: str, report_count: int = 5) -> ProviderResult:
        return ProviderResult("jqdata", False, error="JQData财务指标映射未启用；请配置JQDATA_FUNDAMENTALS_ENABLED=true并补充账号权限字段映射")

    def _ricequant_fundamentals(self, symbol: str, report_count: int = 5) -> ProviderResult:
        return ProviderResult("ricequant", False, error="RiceQuant财务指标映射未启用；请配置RQDATA_FUNDAMENTALS_ENABLED=true并补充账号权限字段映射")

    def _ifind_fundamentals(self, symbol: str, report_count: int = 5) -> ProviderResult:
        if not os.getenv("IFIND_USERNAME") or not os.getenv("IFIND_PASSWORD"):
            return ProviderResult("ifind", False, error="IFIND_USERNAME/IFIND_PASSWORD未配置")
        return ProviderResult("ifind", False, error="iFinD财务指标映射未启用；请按账号权限补充官方SDK字段映射")

    def _eastmoney_skills_not_structured(self, symbol: str, capability: str) -> ProviderResult:
        try:
            from .eastmoney_skills import eastmoney_skills_available

            if not eastmoney_skills_available():
                return ProviderResult("eastmoney_skills", False, error="EASTMONEY_APIKEY/MX_APIKEY未配置")
        except Exception as exc:
            return ProviderResult("eastmoney_skills", False, error=str(exc))
        return ProviderResult(
            "eastmoney_skills",
            False,
            error=f"东方财富Skills的{capability}当前作为投研文本工具使用，不作为结构化行情/K线源",
        )

    def _eastmoney_skills_fundamentals(self, symbol: str, report_count: int = 5) -> ProviderResult:
        try:
            from .eastmoney_skills import get_eastmoney_skills_client

            try:
                info = self._eastmoney_info(symbol)
            except Exception:
                info = {}
            report = get_eastmoney_skills_client().fundamentals_report(
                symbol,
                stock_name=info.get("name") if isinstance(info, dict) else None,
                report_count=report_count,
            )
            return ProviderResult("eastmoney_skills", True, data=report)
        except Exception as exc:
            return ProviderResult("eastmoney_skills", False, error=str(exc))


def normalize_symbol(symbol: str) -> str:
    text = str(symbol).strip().upper()
    text = text.replace("SH", "").replace("SZ", "").replace("BJ", "")
    text = text.replace(".XSHG", "").replace(".XSHE", "").replace(".XBSE", "")
    text = text.replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
    match = re.search(r"(\d{6})", text)
    if not match:
        raise ValueError(f"无效A股代码: {symbol}")
    return match.group(1)


def exchange_prefix(symbol: str) -> str:
    if symbol.startswith(("6", "9")):
        return "sh"
    if symbol.startswith(("4", "8")):
        return "bj"
    return "sz"


def prefixed_symbol(symbol: str) -> str:
    return f"{exchange_prefix(symbol)}{symbol}"


def eastmoney_secid(symbol: str) -> str:
    # Eastmoney market id: 1=上海, 0=深圳/北交所常用接口兜底。
    market_id = "1" if exchange_prefix(symbol) == "sh" else "0"
    return f"{market_id}.{symbol}"


def infer_market_name(symbol: str) -> str:
    prefix = exchange_prefix(symbol)
    if prefix == "sh":
        return "上交所"
    if prefix == "bj":
        return "北交所"
    return "深交所"


def to_jq_code(symbol: str) -> str:
    prefix = exchange_prefix(symbol)
    if prefix == "sh":
        return f"{symbol}.XSHG"
    if prefix == "bj":
        return f"{symbol}.XBSE"
    return f"{symbol}.XSHE"


def to_rq_code(symbol: str) -> str:
    prefix = exchange_prefix(symbol)
    if prefix == "sh":
        return f"{symbol}.XSHG"
    if prefix == "bj":
        return f"{symbol}.XBSE"
    return f"{symbol}.XSHE"


def to_yyyymmdd(date_text: str) -> str:
    return str(date_text).replace("-", "")


def days_between(start_date: str, end_date: str) -> int:
    try:
        return max((datetime.strptime(end_date, "%Y-%m-%d") - datetime.strptime(start_date, "%Y-%m-%d")).days, 1)
    except Exception:
        return 120


def safe_float(value) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def safe_number(value) -> Optional[float]:
    try:
        if value is None or value == "" or value == "-":
            return None
        return float(str(value).replace(",", ""))
    except Exception:
        return None



def eastmoney_request_urls(url: str) -> List[str]:
    """Return Eastmoney URL candidates for networks that drop default push2 hosts."""
    if "push2his.eastmoney.com" in url:
        base_url = re.sub(r"https://(?:\d+\.)?push2his\.eastmoney\.com", "https://push2his.eastmoney.com", url)
        return [
            base_url.replace("push2his.eastmoney.com", "1.push2his.eastmoney.com"),
            base_url.replace("push2his.eastmoney.com", "2.push2his.eastmoney.com"),
            base_url.replace("push2his.eastmoney.com", "8.push2his.eastmoney.com"),
            base_url,
        ]
    if "push2.eastmoney.com" in url:
        base_url = re.sub(r"https://(?:\d+\.)?push2\.eastmoney\.com", "https://push2.eastmoney.com", url)
        return [
            base_url.replace("push2.eastmoney.com", "push2delay.eastmoney.com"),
            base_url.replace("push2.eastmoney.com", "82.push2delay.eastmoney.com"),
            base_url,
        ]
    return [url]

def eastmoney_symbol(symbol: str) -> str:
    suffix = "SH" if exchange_prefix(symbol) == "sh" else "BJ" if exchange_prefix(symbol) == "bj" else "SZ"
    return f"{symbol}.{suffix}"


def extract_eastmoney_rows(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    result = payload.get("result") if isinstance(payload, dict) else None
    if isinstance(result, dict) and isinstance(result.get("data"), list):
        return result.get("data") or []
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return payload.get("data") or []
    return []


def format_money(value) -> str:
    number = safe_number(value)
    if number is None:
        return "N/A"
    sign = "-" if number < 0 else ""
    number = abs(number)
    if number >= 100_000_000:
        return f"{sign}{number / 100_000_000:.2f}亿元"
    if number >= 10_000:
        return f"{sign}{number / 10_000:.2f}万元"
    return f"{sign}{number:.2f}元"


def format_ratio(value, unit: str = "%") -> str:
    number = safe_number(value)
    if number is None:
        return "N/A"
    return f"{number:.2f}{unit}"


def format_multiple(value) -> str:
    number = safe_number(value)
    if number is None:
        return "N/A"
    return f"{number:.2f}倍"


def calculate_ratio(numerator, denominator) -> Optional[float]:
    num = safe_number(numerator)
    den = safe_number(denominator)
    if num is None or den in (None, 0):
        return None
    return num / den


def latest_row(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    return rows[0] if rows else {}


def value_from(rows: List[Dict[str, Any]], field: str):
    for row in rows:
        value = row.get(field)
        if value is not None and value != "":
            return value
    return None


def format_report_row(row: Dict[str, Any], fields: List[Tuple[str, str, str]]) -> str:
    cells = [str(row.get("REPORT_DATE_NAME") or str(row.get("REPORT_DATE", ""))[:10])]
    for field, fmt, _label in fields:
        value = row.get(field)
        if fmt == "money":
            cells.append(format_money(value))
        elif fmt == "ratio":
            cells.append(format_ratio(value))
        elif fmt == "multiple":
            cells.append(format_multiple(value))
        else:
            cells.append(str(value) if value is not None else "N/A")
    return " | ".join(cells)


def format_fundamentals_report(symbol: str, payload: Dict[str, Any]) -> str:
    main_rows = payload.get("main_indicators") or []
    balance_rows = payload.get("balance_sheet") or []
    income_rows = payload.get("income_statement") or []
    cash_rows = payload.get("cash_flow") or []
    quote_info = payload.get("quote_info") or {}

    latest = latest_row(main_rows)
    latest_balance = latest_row(balance_rows)
    stock_name = latest.get("SECURITY_NAME_ABBR") or latest_balance.get("SECURITY_NAME_ABBR") or f"股票{symbol}"
    report_name = latest.get("REPORT_DATE_NAME") or str(latest.get("REPORT_DATE", ""))[:10] or "最新报告期"
    notice_date = str(latest.get("NOTICE_DATE", ""))[:10]
    current_price = safe_number(quote_info.get("current_price"))
    total_share = safe_number(latest.get("TOTAL_SHARE"))
    latest_revenue = safe_number(latest.get("TOTALOPERATEREVE"))
    latest_profit = safe_number(latest.get("PARENTNETPROFIT"))
    total_equity = safe_number(latest.get("TOTAL_EQUITY_PK") or latest_balance.get("TOTAL_EQUITY"))
    market_cap = current_price * total_share if current_price is not None and total_share is not None else None
    pe_simple = calculate_ratio(market_cap, latest_profit)
    pb_simple = calculate_ratio(market_cap, total_equity)
    ps_simple = calculate_ratio(market_cap, latest_revenue)

    lines = [
        f"# {stock_name}（{symbol}）A股基本面财务数据",
        "数据来源: 东方财富公开财务接口（主要指标、资产负债表、利润表、现金流量表）",
        f"最新报告期: {report_name}" + (f"，公告日期: {notice_date}" if notice_date else ""),
        "",
        "## 核心结论数据",
        f"- 营业总收入: {format_money(latest.get('TOTALOPERATEREVE'))}，同比: {format_ratio(latest.get('TOTALOPERATEREVETZ'))}",
        f"- 归母净利润: {format_money(latest.get('PARENTNETPROFIT'))}，同比: {format_ratio(latest.get('PARENTNETPROFITTZ'))}",
        f"- 扣非归母净利润: {format_money(latest.get('KCFJCXSYJLR'))}，同比: {format_ratio(latest.get('KCFJCXSYJLRTZ'))}",
        f"- 每股收益EPS: {latest.get('EPSJB', 'N/A')}，每股净资产BPS: {latest.get('BPS', 'N/A')}",
        f"- ROE: {format_ratio(latest.get('ROEJQ'))}，ROA/总资产净利率: {format_ratio(latest.get('ZZCJLL'))}",
        f"- 毛利率: {format_ratio(latest.get('XSMLL'))}，净利率: {format_ratio(latest.get('XSJLL'))}",
        f"- 资产负债率: {format_ratio(latest.get('ZCFZL') or latest_balance.get('ASSET_LIAB_RATIO'))}",
        f"- 流动比率: {format_multiple(latest.get('LD'))}，速动比率: {format_multiple(latest.get('SD'))}",
    ]

    if current_price is not None or market_cap is not None:
        lines.extend(
            [
                f"- 当前价格: {current_price:.2f}元" if current_price is not None else "- 当前价格: N/A",
                f"- 总股本: {total_share / 100000000:.2f}亿股" if total_share is not None else "- 总股本: N/A",
                f"- 总市值: {format_money(market_cap)}",
                f"- 非TTM PE（按最新报告期累计净利润口径）: {format_multiple(pe_simple)}",
                f"- 最新股东权益PB（按最新股东权益口径）: {format_multiple(pb_simple)}",
                f"- 非TTM PS（按最新报告期累计收入口径）: {format_multiple(ps_simple)}",
            ]
        )

    main_fields = [
        ("TOTALOPERATEREVE", "money", "营业总收入"),
        ("TOTALOPERATEREVETZ", "ratio", "收入同比"),
        ("PARENTNETPROFIT", "money", "归母净利润"),
        ("PARENTNETPROFITTZ", "ratio", "净利同比"),
        ("ROEJQ", "ratio", "ROE"),
        ("XSMLL", "ratio", "毛利率"),
        ("XSJLL", "ratio", "净利率"),
        ("ZCFZL", "ratio", "资产负债率"),
    ]
    lines.extend(
        [
            "",
            "## 近几期主要财务指标",
            "报告期 | " + " | ".join(label for _field, _fmt, label in main_fields),
            "--- | " + " | ".join(["---:"] * len(main_fields)),
        ]
    )
    lines.extend(format_report_row(row, main_fields) for row in main_rows[:5])

    balance_fields = [
        ("TOTAL_ASSETS", "money", "总资产"),
        ("TOTAL_LIABILITIES", "money", "总负债"),
        ("TOTAL_EQUITY", "money", "股东权益"),
        ("MONETARYFUNDS", "money", "货币资金"),
        ("ACCOUNTS_RECE", "money", "应收账款"),
        ("INVENTORY", "money", "存货"),
    ]
    if balance_rows:
        lines.extend(
            [
                "",
                "## 资产负债表摘要",
                "报告期 | " + " | ".join(label for _field, _fmt, label in balance_fields),
                "--- | " + " | ".join(["---:"] * len(balance_fields)),
            ]
        )
        lines.extend(format_report_row(row, balance_fields) for row in balance_rows[:5])

    income_fields = [
        ("TOTAL_OPERATE_INCOME", "money", "营业总收入"),
        ("TOTAL_OPERATE_COST", "money", "营业总成本"),
        ("OPERATE_PROFIT", "money", "营业利润"),
        ("TOTAL_PROFIT", "money", "利润总额"),
        ("PARENT_NETPROFIT", "money", "归母净利润"),
    ]
    if income_rows:
        lines.extend(
            [
                "",
                "## 利润表摘要",
                "报告期 | " + " | ".join(label for _field, _fmt, label in income_fields),
                "--- | " + " | ".join(["---:"] * len(income_fields)),
            ]
        )
        lines.extend(format_report_row(row, income_fields) for row in income_rows[:5])

    cash_fields = [
        ("TOTAL_OPERATE_INFLOW", "money", "经营流入"),
        ("TOTAL_OPERATE_OUTFLOW", "money", "经营流出"),
        ("NETCASH_OPERATE", "money", "经营现金流净额"),
        ("NETCASH_INVEST", "money", "投资现金流净额"),
        ("NETCASH_FINANCE", "money", "筹资现金流净额"),
    ]
    if cash_rows:
        lines.extend(
            [
                "",
                "## 现金流量表摘要",
                "报告期 | " + " | ".join(label for _field, _fmt, label in cash_fields),
                "--- | " + " | ".join(["---:"] * len(cash_fields)),
            ]
        )
        lines.extend(format_report_row(row, cash_fields) for row in cash_rows[:5])

    lines.extend(
        [
            "",
            "## 基本面阅读提示",
            "- 上方非TTM PE/PS使用最新报告期累计利润/收入，不能等同TTM估值；年报口径更适合看全年PE/PS。",
            "- PEG需要未来盈利增速预测，不能只靠历史财报直接给强结论。",
            "- 若收入同比为负但利润同比改善，说明成本/费用控制或非经常因素可能影响利润，需要结合年报附注继续判断。",
            "- 经营现金流为负或明显弱于净利润时，要重点关注回款、合同负债和季节性。",
        ]
    )

    return "\n".join(lines)


def parse_quoted_payload(text: str, sep: str) -> List[str]:
    if '="' not in text:
        raise RuntimeError("missing quoted payload")
    payload = text.split('="', 1)[1].rsplit('"', 1)[0]
    return payload.split(sep)


def normalize_history_dataframe(rows: Iterable[Dict]) -> pd.DataFrame:
    df = pd.DataFrame(list(rows))
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    for col in ["open", "close", "high", "low", "volume", "amount", "pct_change", "change", "turnover"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["date", "open", "close", "high", "low"])
    if "pct_change" not in df.columns or df["pct_change"].isna().all():
        df["pct_change"] = df["close"].pct_change() * 100
    return df.sort_values("date").reset_index(drop=True)


def is_valid_stock_info(info: Dict, symbol: str) -> bool:
    name = str(info.get("name", "")).strip()
    return bool(name and name != f"股票{symbol}" and info.get("source") != "integrated_failed")


def format_stock_data_report(
    df: pd.DataFrame,
    symbol: str,
    stock_name: str,
    start_date: str,
    end_date: str,
    source: str,
    quote_info: Optional[Dict] = None,
) -> str:
    if df is None or df.empty:
        return f"❌ 未获取到{symbol}的股票数据"

    lines = [
        f"# {stock_name} ({symbol}) A股行情数据",
        f"数据来源: {source}",
        f"数据范围: {start_date} 至 {end_date}",
    ]

    if quote_info:
        current_price = quote_info.get("current_price")
        change_pct = quote_info.get("change_pct")
        if current_price is not None:
            lines.append(f"当前价格: {current_price}")
        if change_pct is not None:
            try:
                lines.append(f"涨跌幅: {float(change_pct):+.2f}%")
            except Exception:
                lines.append(f"涨跌幅: {change_pct}")

    latest = df.iloc[-1]
    lines.extend(
        [
            "",
            "## 最新交易日",
            f"日期: {latest.get('date')}",
            f"开盘价: {latest.get('open')}",
            f"收盘价: {latest.get('close')}",
            f"最高价: {latest.get('high')}",
            f"最低价: {latest.get('low')}",
            f"成交量: {latest.get('volume', '未知')}",
            f"成交额: {latest.get('amount', '未知')}",
            "",
            "## 历史K线数据",
            "日期 | 开盘价 | 收盘价 | 最高价 | 最低价 | 成交量 | 成交额 | 涨跌幅",
            "--- | ---: | ---: | ---: | ---: | ---: | ---: | ---:",
        ]
    )

    for _, row in df.tail(120).iterrows():
        pct = row.get("pct_change")
        pct_text = "" if pd.isna(pct) else f"{float(pct):.2f}%"
        lines.append(
            f"{row.get('date')} | {row.get('open')} | {row.get('close')} | {row.get('high')} | "
            f"{row.get('low')} | {row.get('volume', '')} | {row.get('amount', '')} | {pct_text}"
        )

    return "\n".join(lines)


def init_ricequant() -> ProviderResult:
    try:
        import rqdatac
    except Exception as exc:
        return ProviderResult("ricequant", False, error=f"rqdatac未安装: {exc}")
    username = os.getenv("RQDATA_USERNAME")
    password = os.getenv("RQDATA_PASSWORD")
    if not username or not password:
        return ProviderResult("ricequant", False, error="RQDATA_USERNAME/RQDATA_PASSWORD未配置")
    rqdatac.init(username, password)
    return ProviderResult("ricequant", True)


_provider: Optional[IntegratedChinaMarketDataProvider] = None


def get_integrated_china_provider() -> IntegratedChinaMarketDataProvider:
    global _provider
    if _provider is None:
        _provider = IntegratedChinaMarketDataProvider()
    return _provider


def get_integrated_china_stock_info(symbol: str) -> Dict:
    return get_integrated_china_provider().get_stock_info(symbol)


def get_integrated_china_stock_data(symbol: str, start_date: str, end_date: str, period: str = "daily") -> str:
    return get_integrated_china_provider().get_stock_data(symbol, start_date, end_date, period)


def get_integrated_china_fundamentals(symbol: str, report_count: int = 5) -> str:
    return get_integrated_china_provider().get_fundamentals_data(symbol, report_count=report_count)
