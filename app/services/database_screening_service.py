"""
基于MongoDB的股票筛选服务
利用本地数据库中的股票基础信息进行高效筛选
"""

import logging
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta

from app.core.database import get_mongo_db
# from app.models.screening import ScreeningCondition  # 避免循环导入

logger = logging.getLogger(__name__)


LYNCH_SORT_FIELD = "lynch_score"


class DatabaseScreeningService:
    """基于数据库的股票筛选服务"""
    
    def __init__(self):
        # 使用视图而不是基础信息表，视图已经包含了实时行情数据
        self.collection_name = "stock_screening_view"
        
        # 支持的基础信息字段映射
        self.basic_fields = {
            # 基本信息
            "code": "code",
            "name": "name", 
            "industry": "industry",
            "area": "area",
            "market": "market",
            "list_date": "list_date",
            
            # 市值信息 (亿元)
            "total_mv": "total_mv",      # 总市值
            "circ_mv": "circ_mv",        # 流通市值
            "market_cap": "total_mv",    # 市值别名

            # 财务指标
            "pe": "pe",                  # 市盈率
            "pb": "pb",                  # 市净率
            "pe_ttm": "pe_ttm",         # 滚动市盈率
            "pb_mrq": "pb_mrq",         # 最新市净率
            "roe": "roe",                # 净资产收益率（最近一期）
            LYNCH_SORT_FIELD: LYNCH_SORT_FIELD,  # 彼得林奇式综合评分（运行时计算）

            # 交易指标
            "turnover_rate": "turnover_rate",  # 换手率%
            "volume_ratio": "volume_ratio",    # 量比

            # 实时行情字段（需要从 market_quotes 关联查询）
            "pct_chg": "pct_chg",              # 涨跌幅%
            "amount": "amount",                # 成交额（万元）
            "close": "close",                  # 收盘价
            "volume": "volume",                # 成交量
        }
        
        # 支持的操作符
        self.operators = {
            ">": "$gt",
            "<": "$lt", 
            ">=": "$gte",
            "<=": "$lte",
            "==": "$eq",
            "!=": "$ne",
            "between": "$between",  # 自定义处理
            "in": "$in",
            "not_in": "$nin",
            "contains": "$regex",   # 字符串包含
        }
    
    async def can_handle_conditions(self, conditions: List[Dict[str, Any]]) -> bool:
        """
        检查是否可以完全通过数据库筛选处理这些条件
        
        Args:
            conditions: 筛选条件列表
            
        Returns:
            bool: 是否可以处理
        """
        for condition in conditions:
            field = condition.get("field") if isinstance(condition, dict) else condition.field
            operator = condition.get("operator") if isinstance(condition, dict) else condition.operator
            
            # 检查字段是否支持
            if field not in self.basic_fields:
                logger.debug(f"字段 {field} 不支持数据库筛选")
                return False
            
            # 检查操作符是否支持
            if operator not in self.operators:
                logger.debug(f"操作符 {operator} 不支持数据库筛选")
                return False
        
        return True
    
    async def screen_stocks(
        self,
        conditions: List[Dict[str, Any]],
        limit: int = 50,
        offset: int = 0,
        order_by: Optional[List[Dict[str, str]]] = None,
        source: Optional[str] = None
    ) -> Tuple[List[Dict[str, Any]], int]:
        """
        基于数据库进行股票筛选

        Args:
            conditions: 筛选条件列表
            limit: 返回数量限制
            offset: 偏移量
            order_by: 排序条件 [{"field": "total_mv", "direction": "desc"}]
            source: 数据源（可选），默认使用优先级最高的数据源

        Returns:
            Tuple[List[Dict], int]: (筛选结果, 总数量)
        """
        try:
            db = get_mongo_db()
            collection = db[self.collection_name]

            # 🔥 获取数据源优先级配置
            if not source:
                from app.core.unified_config import UnifiedConfigManager
                config = UnifiedConfigManager()
                data_source_configs = await config.get_data_source_configs_async()

                logger.info(f"🔍 [database_screening] 获取到 {len(data_source_configs)} 个数据源配置")
                for ds in data_source_configs:
                    logger.info(f"   - {ds.name}: type={ds.type}, priority={ds.priority}, enabled={ds.enabled}")

                # 提取启用的数据源，按优先级排序
                enabled_sources = [
                    ds.type.lower() for ds in data_source_configs
                    if ds.enabled and ds.type.lower() in ['tushare', 'akshare', 'baostock']
                ]

                logger.info(f"🔍 [database_screening] 启用的数据源（按优先级）: {enabled_sources}")

                if not enabled_sources:
                    enabled_sources = ['tushare', 'akshare', 'baostock']
                    logger.warning(f"⚠️ [database_screening] 没有启用的数据源，使用默认: {enabled_sources}")

                source = enabled_sources[0] if enabled_sources else 'tushare'
                logger.info(f"✅ [database_screening] 最终使用的数据源: {source}")

            # 构建查询条件（现在视图已包含实时行情数据，可以直接查询所有字段）
            query = await self._build_query(conditions)

            # 🔥 添加数据源筛选
            query["source"] = source

            logger.info(f"📋 数据库查询条件: {query}")

            # 构建排序条件；彼得林奇评分需要运行时计算，不能直接交给 MongoDB 排序
            sort_conditions = self._build_sort_conditions(order_by)
            use_runtime_lynch_sort = self._needs_runtime_lynch_sort(order_by)

            # 获取总数
            total_count = await collection.count_documents(query)

            # 执行查询。林奇评分排序需要先拿到候选池再本地排序，否则分页会截断高分标的。
            cursor = collection.find(query)

            # 应用排序
            if sort_conditions and not use_runtime_lynch_sort:
                cursor = cursor.sort(sort_conditions)

            # 应用分页
            if not use_runtime_lynch_sort:
                cursor = cursor.skip(offset).limit(limit)

            # 获取结果
            results = []
            codes = []
            async for doc in cursor:
                # 转换结果格式
                result = self._format_result(doc)
                results.append(result)
                codes.append(doc.get("code"))

            # 批量查询财务数据（ROE等）- 如果视图中没有包含
            if codes:
                await self._enrich_with_financial_data(results, codes)

            if use_runtime_lynch_sort:
                for result in results:
                    self._attach_lynch_assessment(result)
                results.sort(key=self._lynch_sort_key, reverse=True)
                window_end = min(len(results), offset + max(limit * 3, limit, 200))
                results = results[:window_end]
                await self._enrich_with_historical_returns(results, [result.get("code") for result in results])
                for result in results:
                    self._attach_lynch_assessment(result)
                results.sort(key=self._lynch_sort_key, reverse=True)
                results = results[offset:offset + limit]
            else:
                await self._enrich_with_historical_returns(results, codes)
                for result in results:
                    self._attach_lynch_assessment(result)

            logger.info(f"✅ 数据库筛选完成: 总数={total_count}, 返回={len(results)}, 数据源={source}")

            return results, total_count
            
        except Exception as e:
            logger.error(f"❌ 数据库筛选失败: {e}")
            raise Exception(f"数据库筛选失败: {str(e)}")
    
    async def _build_query(self, conditions: List[Dict[str, Any]]) -> Dict[str, Any]:
        """构建MongoDB查询条件"""
        query = {}

        for condition in conditions:
            field = condition.get("field") if isinstance(condition, dict) else condition.field
            operator = condition.get("operator") if isinstance(condition, dict) else condition.operator
            value = condition.get("value") if isinstance(condition, dict) else condition.value

            logger.info(f"🔍 [_build_query] 处理条件: field={field}, operator={operator}, value={value}")

            # 映射字段名
            db_field = self.basic_fields.get(field)
            if not db_field:
                logger.warning(f"⚠️ [_build_query] 字段 {field} 不在 basic_fields 映射中，跳过")
                continue

            logger.info(f"✅ [_build_query] 字段映射: {field} -> {db_field}")
            
            # 处理不同操作符
            if operator == "between":
                # between操作需要两个值
                if isinstance(value, list) and len(value) == 2:
                    query[db_field] = {
                        "$gte": value[0],
                        "$lte": value[1]
                    }
            elif operator == "contains":
                # 字符串包含（不区分大小写）
                query[db_field] = {
                    "$regex": str(value),
                    "$options": "i"
                }
            elif operator in self.operators:
                # 标准操作符
                mongo_op = self.operators[operator]
                query[db_field] = {mongo_op: value}
            
        return query
    
    def _build_sort_conditions(self, order_by: Optional[List[Dict[str, str]]]) -> List[Tuple[str, int]]:
        """构建排序条件"""
        if not order_by:
            # 默认使用彼得林奇式综合评分，而不是总市值降序，避免天然偏向热门大盘股。
            return []
        
        sort_conditions = []
        for order in order_by:
            field = order.get("field")
            direction = order.get("direction", "desc")

            if field == LYNCH_SORT_FIELD:
                continue
            
            # 映射字段名
            db_field = self.basic_fields.get(field)
            if not db_field:
                continue
            
            # 映射排序方向
            sort_direction = -1 if direction.lower() == "desc" else 1
            sort_conditions.append((db_field, sort_direction))
        
        return sort_conditions

    def _needs_runtime_lynch_sort(self, order_by: Optional[List[Dict[str, str]]]) -> bool:
        """是否需要使用运行时彼得林奇评分排序。"""
        if not order_by:
            return True
        return any(order.get("field") == LYNCH_SORT_FIELD for order in order_by)

    def _safe_number(self, value: Any) -> Optional[float]:
        try:
            if value is None or value == "":
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    def _score_range(self, value: Optional[float], good_low: float, good_high: float, warn_low: float, warn_high: float) -> float:
        if value is None:
            return 0.0
        if good_low <= value <= good_high:
            return 1.0
        if warn_low <= value <= warn_high:
            return 0.55
        return 0.0

    def _amount_yuan(self, amount: Optional[float]) -> Optional[float]:
        """兼容不同数据源的成交额单位，尽量换算为元用于热度标注。"""
        if amount is None:
            return None
        # A股全市场成交额若低于 1000 万，通常是“万元”口径；否则按元处理。
        if amount < 10_000_000:
            return amount * 10_000
        return amount

    def _attach_lynch_assessment(self, result: Dict[str, Any]) -> None:
        """添加彼得林奇式评分与提醒标签。"""
        pe = self._safe_number(result.get("pe_ttm") if result.get("pe_ttm") is not None else result.get("pe"))
        pb = self._safe_number(result.get("pb_mrq") if result.get("pb_mrq") is not None else result.get("pb"))
        roe = self._safe_number(result.get("roe"))
        total_mv = self._safe_number(result.get("total_mv"))
        pct_chg = self._safe_number(result.get("pct_chg"))
        turnover_rate = self._safe_number(result.get("turnover_rate"))
        amount_yuan = self._amount_yuan(self._safe_number(result.get("amount")))

        score = 0.0
        score += min(max((roe or 0.0) / 15.0, 0.0), 1.25) * 28.0
        score += self._score_range(pe, 5.0, 25.0, 0.0, 35.0) * 22.0
        score += self._score_range(pb, 0.8, 3.0, 0.0, 4.0) * 16.0
        score += self._score_range(total_mv, 30.0, 500.0, 10.0, 1000.0) * 18.0

        if pct_chg is None:
            score += 5.0
        elif -5.0 <= pct_chg <= 3.0:
            score += 8.0
        elif pct_chg <= 8.0:
            score += 4.0

        if turnover_rate is None:
            score += 3.0
        elif turnover_rate <= 3.0:
            score += 5.0
        elif turnover_rate <= 6.0:
            score += 2.0

        if amount_yuan is None:
            score += 2.0
        elif amount_yuan <= 3e8:
            score += 3.0
        elif amount_yuan <= 10e8:
            score += 1.0

        notes = []
        priority = "观察池"
        if pe is None or pe <= 0:
            notes.append("PE缺失或为负，需确认连续盈利")
            priority = "低优先级"
        elif pe > 35:
            notes.append("估值偏高，可能已透支预期")
            priority = "低优先级"
        if pb is not None and pb > 4:
            notes.append("PB偏高，安全边际不足")
            priority = "低优先级"
        if roe is None:
            notes.append("ROE缺失，需补充财报验证")
        elif roe < 10:
            notes.append("ROE低于林奇式基本盘门槛")
            priority = "低优先级"
        if total_mv is not None and total_mv > 1000:
            notes.append("市值偏大，成长弹性可能有限")
        if pct_chg is not None and pct_chg > 8:
            notes.append("短期涨幅偏高，推荐后需注明追高风险")
            priority = "低优先级"
        return_60d = self._safe_number(result.get("return_60d"))
        return_250d = self._safe_number(result.get("return_250d"))
        if return_60d is not None and return_60d > 20:
            notes.append("近60日涨幅超过20%，属于相对拥挤/追高候选")
            score -= 10.0
            priority = "低优先级"
        if return_250d is not None and return_250d > 60:
            notes.append("近250日涨幅超过60%，需标注一年涨幅过大")
            score -= 15.0
            priority = "低优先级"
        if turnover_rate is not None and turnover_rate > 8:
            notes.append("换手率偏高，热度和交易拥挤度较高")
            priority = "低优先级"
        if amount_yuan is not None and amount_yuan > 20e8:
            notes.append("成交额极高，可能是热门拥挤交易")
            priority = "低优先级"

        result[LYNCH_SORT_FIELD] = round(max(score, 0.0), 2)
        result["lynch_priority"] = priority
        result["lynch_notes"] = notes

    def _lynch_sort_key(self, result: Dict[str, Any]) -> Tuple[float, float, float, float]:
        score = self._safe_number(result.get(LYNCH_SORT_FIELD)) or 0.0
        roe = self._safe_number(result.get("roe")) or -999.0
        pe = self._safe_number(result.get("pe_ttm") if result.get("pe_ttm") is not None else result.get("pe"))
        pb = self._safe_number(result.get("pb_mrq") if result.get("pb_mrq") is not None else result.get("pb"))
        return (score, roe, -(pe if pe is not None else 9999.0), -(pb if pb is not None else 9999.0))

    async def _enrich_with_historical_returns(self, results: List[Dict[str, Any]], codes: List[str]) -> None:
        """批量计算近60/250个交易日涨幅，用于林奇模式的不过热标注。"""
        try:
            db = get_mongo_db()
            collection = db["stock_daily_quotes"]
            clean_codes = [str(code).zfill(6) for code in codes if code]
            if not clean_codes:
                return

            start_date = (datetime.now() - timedelta(days=420)).strftime("%Y-%m-%d")
            cursor = collection.find(
                {"symbol": {"$in": clean_codes}, "period": "daily", "trade_date": {"$gte": start_date}},
                projection={"_id": 0, "symbol": 1, "trade_date": 1, "close": 1},
            ).sort([("symbol", 1), ("trade_date", -1)])

            history_map: Dict[str, List[Dict[str, Any]]] = {}
            seen_dates: Dict[str, set] = {}
            async for doc in cursor:
                symbol = str(doc.get("symbol") or "").zfill(6)
                if not symbol:
                    continue
                trade_date = str(doc.get("trade_date") or "")
                symbol_seen_dates = seen_dates.setdefault(symbol, set())
                if trade_date in symbol_seen_dates:
                    continue
                symbol_seen_dates.add(trade_date)
                bucket = history_map.setdefault(symbol, [])
                if len(bucket) < 251:
                    bucket.append(doc)

            for result in results:
                code = str(result.get("code") or "").zfill(6)
                history = history_map.get(code) or []
                if len(history) < 2:
                    continue
                latest_close = self._safe_number(history[0].get("close"))
                if latest_close is None or latest_close <= 0:
                    continue
                for days, field in ((60, "return_60d"), (250, "return_250d")):
                    if len(history) > days:
                        base_close = self._safe_number(history[days].get("close"))
                        if base_close and base_close > 0:
                            result[field] = round((latest_close / base_close - 1) * 100, 2)
        except Exception as e:
            logger.warning(f"⚠️ 计算历史涨幅失败: {e}")
    
    async def _enrich_with_financial_data(self, results: List[Dict[str, Any]], codes: List[str]) -> None:
        """
        批量查询财务数据并填充到结果中

        Args:
            results: 筛选结果列表
            codes: 股票代码列表
        """
        try:
            db = get_mongo_db()
            financial_collection = db['stock_financial_data']

            # 🔥 获取数据源优先级配置
            from app.core.unified_config import UnifiedConfigManager
            config = UnifiedConfigManager()
            data_source_configs = await config.get_data_source_configs_async()

            # 提取启用的数据源，按优先级排序
            enabled_sources = [
                ds.type.lower() for ds in data_source_configs
                if ds.enabled and ds.type.lower() in ['tushare', 'akshare', 'baostock']
            ]

            if not enabled_sources:
                enabled_sources = ['tushare', 'akshare', 'baostock']

            # 优先使用优先级最高的数据源
            preferred_source = enabled_sources[0] if enabled_sources else 'tushare'

            # 批量查询最新的财务数据
            # 按 code 分组，取每个 code 的最新一期数据（只查询优先级最高的数据源）
            pipeline = [
                {"$match": {"code": {"$in": codes}, "data_source": preferred_source}},
                {"$sort": {"code": 1, "report_period": -1}},
                {"$group": {
                    "_id": "$code",
                    "roe": {"$first": "$roe"},
                    "roa": {"$first": "$roa"},
                    "netprofit_margin": {"$first": "$netprofit_margin"},
                    "gross_margin": {"$first": "$gross_margin"},
                }}
            ]

            financial_data_map = {}
            async for doc in financial_collection.aggregate(pipeline):
                code = doc.get("_id")
                financial_data_map[code] = {
                    "roe": doc.get("roe"),
                    "roa": doc.get("roa"),
                    "netprofit_margin": doc.get("netprofit_margin"),
                    "gross_margin": doc.get("gross_margin"),
                }

            # 填充财务数据到结果中
            for result in results:
                code = result.get("code")
                if code in financial_data_map:
                    financial_data = financial_data_map[code]
                    # 只更新 ROE（如果 stock_basic_info 中没有的话）
                    if result.get("roe") is None:
                        result["roe"] = financial_data.get("roe")
                    # 可以添加更多财务指标
                    # result["roa"] = financial_data.get("roa")
                    # result["netprofit_margin"] = financial_data.get("netprofit_margin")

            logger.debug(f"✅ 已填充 {len(financial_data_map)} 条财务数据")

        except Exception as e:
            logger.warning(f"⚠️ 填充财务数据失败: {e}")
            # 不抛出异常，允许继续返回基础数据

    def _format_result(self, doc: Dict[str, Any]) -> Dict[str, Any]:
        """格式化查询结果，统一使用后端字段名"""
        # 根据股票代码推断市场类型
        code = doc.get("code", "")
        market_type = "A股"  # 默认A股
        if code:
            if code.startswith("6"):
                market_type = "A股"  # 上海
            elif code.startswith(("0", "3")):
                market_type = "A股"  # 深圳
            elif code.startswith("8") or code.startswith("4"):
                market_type = "A股"  # 北交所

        result = {
            # 基础信息
            "code": doc.get("code"),
            "name": doc.get("name"),
            "industry": doc.get("industry"),
            "area": doc.get("area"),
            "market": market_type,  # 市场类型（A股、美股、港股）
            "board": doc.get("market"),  # 板块（主板、创业板、科创板等）
            "exchange": doc.get("sse"),  # 交易所（上海证券交易所、深圳证券交易所等）
            "list_date": doc.get("list_date"),

            # 市值信息（亿元）
            "total_mv": doc.get("total_mv"),
            "circ_mv": doc.get("circ_mv"),

            # 财务指标
            "pe": doc.get("pe"),
            "pb": doc.get("pb"),
            "pe_ttm": doc.get("pe_ttm"),
            "pb_mrq": doc.get("pb_mrq"),
            "roe": doc.get("roe"),

            # 交易指标
            "turnover_rate": doc.get("turnover_rate"),
            "volume_ratio": doc.get("volume_ratio"),

            # 交易数据（从视图中获取，视图已包含实时行情数据）
            "close": doc.get("close"),              # 收盘价
            "pct_chg": doc.get("pct_chg"),          # 涨跌幅(%)
            "amount": doc.get("amount"),            # 成交额
            "volume": doc.get("volume"),            # 成交量
            "open": doc.get("open"),                # 开盘价
            "high": doc.get("high"),                # 最高价
            "low": doc.get("low"),                  # 最低价

            # 技术指标（基础信息筛选时为None）
            "ma20": None,
            "rsi14": None,
            "kdj_k": None,
            "kdj_d": None,
            "kdj_j": None,
            "dif": None,
            "dea": None,
            "macd_hist": None,

            # 元数据
            "source": doc.get("source", "database"),
            "updated_at": doc.get("updated_at"),
        }
        
        # 移除None值
        return {k: v for k, v in result.items() if v is not None}
    
    async def get_field_statistics(self, field: str) -> Dict[str, Any]:
        """
        获取字段的统计信息
        
        Args:
            field: 字段名
            
        Returns:
            Dict: 统计信息 {min, max, avg, count}
        """
        try:
            db_field = self.basic_fields.get(field)
            if not db_field:
                return {}
            
            db = get_mongo_db()
            collection = db[self.collection_name]
            
            # 使用聚合管道获取统计信息
            pipeline = [
                {"$match": {db_field: {"$exists": True, "$ne": None}}},
                {"$group": {
                    "_id": None,
                    "min": {"$min": f"${db_field}"},
                    "max": {"$max": f"${db_field}"},
                    "avg": {"$avg": f"${db_field}"},
                    "count": {"$sum": 1}
                }}
            ]
            
            result = await collection.aggregate(pipeline).to_list(length=1)
            
            if result:
                stats = result[0]
                avg_value = stats.get("avg")
                return {
                    "field": field,
                    "min": stats.get("min"),
                    "max": stats.get("max"),
                    "avg": round(avg_value, 2) if avg_value is not None else None,
                    "count": stats.get("count", 0)
                }
            
            return {"field": field, "count": 0}
            
        except Exception as e:
            logger.error(f"获取字段统计失败: {e}")
            return {"field": field, "error": str(e)}
    
    def _separate_conditions(self, conditions: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        分离基础信息条件和实时行情条件

        Args:
            conditions: 所有筛选条件

        Returns:
            Tuple[基础信息条件列表, 实时行情条件列表]
        """
        # 实时行情字段（需要从 market_quotes 查询）
        quote_fields = {"pct_chg", "amount", "close", "volume"}

        basic_conditions = []
        quote_conditions = []

        for condition in conditions:
            field = condition.get("field") if isinstance(condition, dict) else condition.field
            if field in quote_fields:
                quote_conditions.append(condition)
            else:
                basic_conditions.append(condition)

        return basic_conditions, quote_conditions

    async def _filter_by_quotes(
        self,
        results: List[Dict[str, Any]],
        codes: List[str],
        quote_conditions: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        根据实时行情数据进行二次筛选

        Args:
            results: 初步筛选结果
            codes: 股票代码列表
            quote_conditions: 实时行情筛选条件

        Returns:
            List[Dict]: 筛选后的结果
        """
        try:
            db = get_mongo_db()
            quotes_collection = db['market_quotes']

            # 批量查询实时行情数据
            quotes_cursor = quotes_collection.find({"code": {"$in": codes}})
            quotes_map = {}
            async for quote in quotes_cursor:
                code = quote.get("code")
                quotes_map[code] = {
                    "close": quote.get("close"),
                    "pct_chg": quote.get("pct_chg"),
                    "amount": quote.get("amount"),
                    "volume": quote.get("volume"),
                }

            logger.info(f"📊 查询到 {len(quotes_map)} 只股票的实时行情数据")

            # 过滤结果
            filtered_results = []
            for result in results:
                code = result.get("code")
                quote_data = quotes_map.get(code)

                if not quote_data:
                    # 没有实时行情数据，跳过
                    continue

                # 检查是否满足所有实时行情条件
                match = True
                for condition in quote_conditions:
                    field = condition.get("field") if isinstance(condition, dict) else condition.field
                    operator = condition.get("operator") if isinstance(condition, dict) else condition.operator
                    value = condition.get("value") if isinstance(condition, dict) else condition.value

                    field_value = quote_data.get(field)
                    if field_value is None:
                        match = False
                        break

                    # 检查条件
                    if operator == "between" and isinstance(value, list) and len(value) == 2:
                        if not (value[0] <= field_value <= value[1]):
                            match = False
                            break
                    elif operator == ">":
                        if not (field_value > value):
                            match = False
                            break
                    elif operator == "<":
                        if not (field_value < value):
                            match = False
                            break
                    elif operator == ">=":
                        if not (field_value >= value):
                            match = False
                            break
                    elif operator == "<=":
                        if not (field_value <= value):
                            match = False
                            break

                if match:
                    # 将实时行情数据合并到结果中
                    result.update(quote_data)
                    filtered_results.append(result)

            logger.info(f"✅ 实时行情筛选完成: 筛选前={len(results)}, 筛选后={len(filtered_results)}")
            return filtered_results

        except Exception as e:
            logger.error(f"❌ 实时行情筛选失败: {e}")
            # 如果失败，返回原始结果
            return results

    async def get_available_values(self, field: str, limit: int = 100) -> List[str]:
        """
        获取字段的可选值列表（用于枚举类型字段）
        
        Args:
            field: 字段名
            limit: 返回数量限制
            
        Returns:
            List[str]: 可选值列表
        """
        try:
            db_field = self.basic_fields.get(field)
            if not db_field:
                return []
            
            db = get_mongo_db()
            collection = db[self.collection_name]
            
            # 获取字段的不重复值
            values = await collection.distinct(db_field)
            
            # 过滤None值并排序
            values = [v for v in values if v is not None]
            values.sort()
            
            return values[:limit]
            
        except Exception as e:
            logger.error(f"获取字段可选值失败: {e}")
            return []


# 全局服务实例
_database_screening_service: Optional[DatabaseScreeningService] = None


def get_database_screening_service() -> DatabaseScreeningService:
    """获取数据库筛选服务实例"""
    global _database_screening_service
    if _database_screening_service is None:
        _database_screening_service = DatabaseScreeningService()
    return _database_screening_service
