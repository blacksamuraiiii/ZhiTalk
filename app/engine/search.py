"""
联网搜索客户端 - AnySearch MCP /mcp 垂直域路由

全量走 MCP /mcp 垂直域，判错走本地兜底话术（不降级 REST）。

三个 MCP 调用形态：
1. finance.quote（search tool）：sub_domain=finance.quote，type=stock/index，symbol/cn_code
2. extract（extract tool，独立 tool 非 search sub_domain）：url=中国天气网城市页
3. general（search tool）：无 sub_domain，query 直搜

判错（喂 LLM 前必判）：result.isError==true / content 空 / text 匹配错误正则 → 返回 None。
集成点：audiosocket._llm_producer 判断路由，判空播放 NO_SEARCH_RESULT_TEXT 兜底话术。
"""

import re
import time
import hashlib
import logging
from typing import Optional, Tuple

import requests

logger = logging.getLogger("ai-backend.search")

ENDPOINT = "https://api.anysearch.com/mcp"

# ── 城市代码表（中国天气网，4直辖市+27省会+2特區+江苏13地市+扬州6区县）──
DEFAULT_CITY_MAP = {
    # 直辖市
    "北京": "101010100", "上海": "101020100", "天津": "101030100", "重庆": "101040100",
    # 特别行政区
    "香港": "101320101", "澳门": "101330101",
    # 省会
    "石家庄": "101090101", "太原": "101100101", "呼和浩特": "101080101",
    "沈阳": "101070101", "长春": "101060101", "哈尔滨": "101050101",
    "南京": "101190101", "杭州": "101210101", "合肥": "101220101",
    "福州": "101230101", "南昌": "101240101", "济南": "101120101",
    "郑州": "101180101", "武汉": "101200101", "长沙": "101250101",
    "广州": "101280101", "南宁": "101300101", "海口": "101310101",
    "成都": "101270101", "贵阳": "101260101", "昆明": "101290101",
    "拉萨": "101140101", "西安": "101110101", "兰州": "101160101",
    "西宁": "101150101", "银川": "101170101", "乌鲁木齐": "101130101",
    # 江苏 13 地市
    "无锡": "101190201", "徐州": "101190801", "常州": "101191101",
    "苏州": "101190401", "南通": "101190501", "连云港": "101191001",
    "淮安": "101190901", "盐城": "101190701", "扬州": "101190601",
    "镇江": "101190301", "泰州": "101191201", "宿迁": "101191301",
    # 扬州 6 区县
    "广陵": "101190601", "邗江": "101190601", "江都": "101190601",
    "高邮": "101190604", "宝应": "101190603", "仪征": "101190602",
}
WEATHER_URL = "https://www.weather.com.cn/weather/{code}.shtml"

# ── 内置股票代码表（演示常用，命中跳过 LLM 提取，对称 CITY_MAP）──
# key 按"更具体在前"排（长名优先于短名），避免短名先命中断送长名精确匹配
DEFAULT_STOCK_MAP = {
    # A股核心（市值前列 + 电信产业链）
    "贵州茅台": "600519.SH", "茅台": "600519.SH",
    "工商银行": "601398.SH", "建设银行": "601939.SH",
    "农业银行": "601288.SH", "中国银行": "601988.SH",
    "招商银行": "600036.SH", "中国平安": "601318.SH", "平安": "601318.SH",
    "中国人寿": "601628.SH", "中国石油": "601857.SH",
    "中国石化": "600028.SH", "中国海油": "600938.SH",
    "中国移动": "600941.SH", "移动": "600941.SH",
    "中国联通": "600050.SH", "联通": "600050.SH",
    "中国电信": "601728.SH", "电信": "601728.SH",
    "中国广电": "600831.SH", "广电": "600831.SH",
    "比亚迪": "002594.SZ", "宁德时代": "300750.SZ", "宁德": "300750.SZ",
    "中芯国际": "688981.SH", "五粮液": "000858.SZ",
    "美的集团": "000333.SZ", "美的": "000333.SZ",
    "长江电力": "600900.SH", "中国神华": "601088.SH",
    "海康威视": "002415.SZ",
    "长鑫科技": "688825.SH", "长鑫": "688825.SH",
    # 美股纳斯达克 7 小强（Magnificent 7）
    "英伟达": "NVDA", "苹果": "AAPL", "特斯拉": "TSLA",
    "微软": "MSFT", "亚马逊": "AMZN", "谷歌": "GOOGL", "Meta": "META",
}

# ── 路由表（按 sub_domain 组织，优先级从上到下：finance > weather > general）──
DEFAULT_ROUTES = {
    "finance": r"股价|涨跌|股票|个股|大盘|指数|上证|深证|沪深|创业板|市值|市盈率|汇率|比特币|以太坊",
    "weather": r"天气|气温|下雨|下雪|台风|降温|升温",
    "general": r"最新|新闻|刚刚|上市|发布会|发布了|推出|排名|热搜|大事|什么|怎么|多少|几",
}
# 大盘/指数关键词（finance.quote 内部判 type=index；含美股指数走 ETF 替代）
# A股：上证/深证/沪深/创业板 → cn_code=000001.SH
# 美股：标普/纳斯达克/纳指/道琼斯/道指 → 走 ETF (SPY/QQQ/DIA)
DEFAULT_INDEX_KEYWORDS = r"大盘|指数|上证|深证|沪深|创业板|标普|纳斯达克|纳指|道琼斯|道指|SPX|IXIC|DJI"

# 负面规则：时间词 + 个人意愿/情绪 → 不搜
DEFAULT_NEGATE = (
    r"(今天|昨天|明天|本周|这周|2024|2025|2026).*("
    r"心情|感觉|想吃|打算|去|来|约|请|帮忙|帮|给|让"
    r")"
)

# 判错正则（喂 LLM 前必判）
DEFAULT_ERROR_PATTERNS = [
    r"quota", r"exhausted", r"recharge", r"rate.?limit",   # 额度耗尽
    r"error", r"invalid", r"unauthorized", r"forbidden",  # 权限/参数错误
    r"jsonrpc.*error",                                     # JSON-RPC 错误
    r"required", r"is required",                           # 缺参数
]

# ── 天气段提取：7 天预报从 "# N日" 标题开始 ──
WEATHER_HEAD = re.compile(r"#\s*\d+\s*日")
# 温度行里的 markdown 强调符（*23℃* / **23℃**）剥掉
TEMP_MD = re.compile(r"\*+(\d+)\s*℃\*+")


def _extract_weather(text: str, seg_len: int = 1500) -> str:
    """从 extract 返回的天气页 markdown 截取 7 天预报段。

    页面 markdown 前半是导航/热门城市，7 天预报从 "# N日（今天）" 开始，
    往后 ~1500 字符覆盖 7 天数据。截取后剥掉温度行里的 markdown 强调符。
    """
    if not text:
        return ""
    m = WEATHER_HEAD.search(text)
    if not m:
        return text[:seg_len]
    seg = text[m.start(): m.start() + seg_len]
    seg = TEMP_MD.sub(r"\1℃", seg)
    return seg


class SearchClient:
    """AnySearch MCP 垂直域路由客户端（默认匿名访问，key 可选）。"""

    def __init__(self, config: dict):
        config = config if isinstance(config, dict) else {}
        self.enabled = bool(config.get("enabled", False))
        self.endpoint = str(config.get("endpoint", ENDPOINT))
        self.api_key = config.get("api_key", "") or ""
        self.max_results = int(config.get("max_results", 3))
        self.timeout = float(config.get("timeout", 5.0))

        # 路由关键词：从 config 读，缺省 fallback 默认常量
        routes = config.get("routes", {}) or {}
        self.routes = [
            ("finance", str(routes.get("finance") or DEFAULT_ROUTES["finance"])),
            ("weather", str(routes.get("weather") or DEFAULT_ROUTES["weather"])),
            ("general", str(routes.get("general") or DEFAULT_ROUTES["general"])),
        ]
        self.index_keywords = str(config.get("index_keywords") or DEFAULT_INDEX_KEYWORDS)
        self.city_map = config.get("city_map") or DEFAULT_CITY_MAP
        self.stock_map = config.get("stock_map") or DEFAULT_STOCK_MAP
        self.negate = re.compile(str(config.get("negate") or DEFAULT_NEGATE), re.I)
        err_pats = config.get("error_patterns") or DEFAULT_ERROR_PATTERNS
        self.error_patterns = [re.compile(str(p), re.I) for p in err_pats]

        cache_ttl = config.get("cache_ttl", {}) or {}
        self.cache_ttl = {
            "finance": int(cache_ttl.get("finance", 300)),
            "weather": int(cache_ttl.get("weather", 600)),
            "general": int(cache_ttl.get("general", 3600)),
        }
        self._cache = {}  # key = md5(query|route|type|param) -> (timestamp, ctx)

        # 过滤词白名单（clean_query 用）：config 优先，空则用模块默认
        _apply_filters(config.get("filters", ""))

    # ── 路由判断 ──
    def need_search(self, u: str) -> Tuple[bool, Optional[str]]:
        """返回 (是否需要联网, route)。route ∈ {finance, weather, general} 或 None。"""
        if not self.enabled:
            return False, None
        text = u or ""
        if self.negate.search(text):
            return False, None
        # 先查 stock_map：股票名命中 → 直接 finance（不依赖 routes 关键词）
        for name in self.stock_map:
            if name in text:
                return True, "finance"
        for route, pattern in self.routes:
            if re.search(pattern, text, re.I):
                return True, route
        return False, None

    def extract_city(self, text: str) -> Optional[str]:
        """城市名字典匹配（不调 LLM）。表外城市返回 None。"""
        if not text:
            return None
        for city in self.city_map:
            if city in text:
                return city
        return None

    def lookup_stock(self, text: str) -> Tuple[Optional[str], Optional[str]]:
        """内置股票代码字典匹配（跳过 LLM 提取）。返回 (type, param)，未命中 (None, None)。"""
        if not text:
            return None, None
        for name, code in self.stock_map.items():
            if name in text:
                return "stock", code
        return None, None

    # ── 对外搜索入口 ──
    def search(self, query: str, route: Optional[str] = None,
               finance_type: Optional[str] = None,
               finance_param: Optional[str] = None) -> Optional[str]:
        """带缓存 + 判错。返回格式化检索上下文 str，失败返回 None。

        - route=finance：finance_type ∈ {stock, index}，finance_param = symbol/cn_code
        - route=weather：query 为城市名，内部查 CITY_MAP 转代码 → extract
        - route=general：query 直搜
        """
        if not query:
            return None

        cache_key = hashlib.md5(
            f"{query}|{route}|{finance_type}|{finance_param}".encode()
        ).hexdigest()
        cached = self._cache.get(cache_key)
        if cached:
            ts, ctx = cached
            ttl = self.cache_ttl.get(route or "general", 3600)
            if time.time() - ts < ttl:
                return ctx
            del self._cache[cache_key]

        if route == "finance":
            ctx = self._mcp_finance_quote(finance_type, finance_param)
        elif route == "weather":
            ctx = self._mcp_extract_weather(query)
        else:
            ctx = self._mcp_general(query)

        if ctx:
            self._cache[cache_key] = (time.time(), ctx)
        return ctx

    # ── MCP 底层调用 ──
    def _post_mcp(self, tool: str, arguments: dict, max_retries: int = 3) -> Optional[str]:
        """统一 MCP JSON-RPC 2.0 调用 + 判错 + 网络重试。
        返回 content[0].text，判错/超时/重试耗尽返回 None。"""
        body = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        last_err = None
        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.post(self.endpoint, json=body, headers=headers,
                                     timeout=self.timeout)
                resp.raise_for_status()
                data = resp.json()
                result = data.get("result") or {}
                if result.get("isError"):
                    txt = ""
                    content = result.get("content") or []
                    if content:
                        txt = content[0].get("text", "")
                    logger.warning(f"[Search] MCP isError: {txt[:120]}")
                    return None
                content = result.get("content") or []
                if not content:
                    return None
                text = content[0].get("text", "")
                if not text or not text.strip():
                    return None
                if self._is_error_text(text):
                    logger.warning(f"[Search] MCP 错误串: {text[:120]}")
                    return None
                return text
            except (requests.Timeout, requests.ConnectionError) as e:
                last_err = e
                if attempt < max_retries:
                    import time as _t
                    logger.warning(f"[Search] MCP 请求失败 (第{attempt}/{max_retries}次): {e}，1秒后重试")
                    _t.sleep(1)
            except Exception as e:
                logger.warning(f"[Search] MCP 请求失败: {e}")
                return None

        logger.warning(f"[Search] MCP 重试 {max_retries} 次均失败: {last_err}")
        return None

    def _is_error_text(self, text: str) -> bool:
        low = text.lower()
        return any(p.search(low) for p in self.error_patterns)

    def _mcp_finance_quote(self, ftype: Optional[str], param: Optional[str]) -> Optional[str]:
        """finance.quote 结构化报价。type=stock/index，param=symbol(美股)/cn_code(A股)。"""
        if not ftype or not param:
            return None
        sub_params = {"type": ftype}
        if ftype == "index":
            sub_params["cn_code"] = param
        else:
            # stock：param 可能是美股 symbol 或 A股 cn_code（含 .SH/.SZ 后缀）
            if re.search(r"\.(SH|SZ|BJ)$", param, re.I):
                sub_params["cn_code"] = param
            else:
                sub_params["symbol"] = param
        return self._post_mcp("search", {
            "query": param, "domain": "finance", "sub_domain": "finance.quote",
            "sub_domain_params": sub_params, "max_results": 1,
        })

    def _mcp_extract_weather(self, city: str) -> Optional[str]:
        """extract 中国天气网城市页 → 截取 7 天预报段。"""
        code = self.city_map.get(city)
        if not code:
            return None
        url = WEATHER_URL.format(code=code)
        text = self._post_mcp("extract", {"url": url})
        if not text:
            return None
        return _extract_weather(text)

    def _mcp_general(self, query: str) -> Optional[str]:
        """general 兜底搜索（症状/健康/新闻等未覆盖场景）。"""
        return self._post_mcp("search", {"query": query, "max_results": self.max_results})


# 搜索 query 口语填充词/请求词白名单（只删安全词；否定词"不/别"、形容词"好"、语义词一律不入表）
FILLER_WORDS = [
    # 请求短语（长优先）
    "帮我再查一下", "帮我查一下", "帮我查查", "帮我查下", "帮我看看", "帮我搜一下",
    "然后再", "再查一下", "我知道了", "我知道", "好的吧", "好吧", "查一下", "查查", "请问", "帮我",
    # 语气词/填充词
    "好的", "嗯嗯", "哦哦", "知道了", "那个", "这个", "然后", "就是",
    "嗯", "哦", "额", "呃", "诶", "哎", "啊", "呀",
]
_FILLER_RE = re.compile("|".join(sorted(FILLER_WORDS, key=len, reverse=True)))


def _apply_filters(raw: str) -> None:
    """从 config 的 | 分隔字符串更新模块级过滤词表（clean_query 使用）。空/非法则保留默认。"""
    global _FILLER_RE
    if not raw or not isinstance(raw, str):
        return
    words = [w.strip() for w in raw.split("|") if w.strip()]
    if words:
        _FILLER_RE = re.compile("|".join(sorted(words, key=len, reverse=True)))


def clean_query(q: str) -> str:
    """去掉口语填充词/请求前缀，返回干净意图 query。白名单式，只删安全词。"""
    if not q:
        return q
    s = q.strip()
    s = _FILLER_RE.sub("", s)
    s = re.sub(r"^[，,。!！?？\s]+", "", s)
    s = re.sub(r"[，,。!！?？\s]+$", "", s)
    return s.strip()


def build_query(u: str, history: list) -> str:
    """构造搜索 query。代词类短句补上一轮 user 上下文，返回前做语气词清理。"""
    u = (u or "").strip()
    if re.fullmatch(r"它呢?|然后呢?|还有吗|那.{0,4}呢?", u):
        prev = ""
        for m in reversed(history):
            if m.get("role") == "user" and m.get("content") != u:
                prev = m.get("content", "")
                break
        if prev:
            return clean_query(f"{prev}；{u}")
    return clean_query(u[:100])
