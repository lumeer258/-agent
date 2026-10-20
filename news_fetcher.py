"""
news_fetcher.py - 金融新闻行情分析 Agent 新闻获取模块（重构版）
=============================================================
功能：获取东方财富 / 新浪财经的财经快讯，支持字段标准化、关键词检索、
      优雅降级、内存缓存、反爬抖动等能力。
"""

import random
import time
from typing import Dict, List, Optional, Tuple

import akshare as ak
import pandas as pd

# ================================================================
# 日志前缀常量
# ================================================================
_LOG_PREFIX = "【新闻获取】"

# ================================================================
# 简易内存缓存
# ================================================================
# 结构: { (source, keyword): (timestamp, DataFrame) }
_cache: Dict[Tuple[str, str], Tuple[float, pd.DataFrame]] = {}
_CACHE_TTL_SECONDS = 300  # 缓存有效期 5 分钟


def _cache_get(key: Tuple[str, str]) -> Optional[pd.DataFrame]:
    """从缓存中读取数据，过期则自动清除。

    Args:
        key: 缓存键，格式为 (source, keyword)。

    Returns:
        缓存命中时返回 DataFrame，未命中或已过期返回 None。
    """
    if key in _cache:
        ts, df = _cache[key]
        if time.time() - ts < _CACHE_TTL_SECONDS:
            print(f"{_LOG_PREFIX}缓存命中: {key}")
            return df.copy()
        else:
            # 过期，清除
            del _cache[key]
    return None


def _cache_set(key: Tuple[str, str], df: pd.DataFrame) -> None:
    """将数据写入缓存。

    Args:
        key: 缓存键，格式为 (source, keyword)。
        df: 待缓存的 DataFrame。
    """
    _cache[key] = (time.time(), df.copy())


# ================================================================
# 字段标准化
# ================================================================
# 各数据源原始列名 → 标准列名的映射表
_COLUMN_MAP_EM: Dict[str, str] = {
    "标题": "title",
    "摘要": "content",
    "发布时间": "publish_time",
    "链接": "url",
}

_COLUMN_MAP_SINA: Dict[str, str] = {
    "内容": "content",
    "时间": "publish_time",
}

# 标准列名列表
STANDARD_COLUMNS: List[str] = ["title", "publish_time", "content", "url"]


def standardize_news_df(df: pd.DataFrame, source: str) -> pd.DataFrame:
    """将不同数据源的字段统一映射为标准列名。

    标准列名为: title, publish_time, content, url。
    对于缺少某些列的数据源（如新浪没有 title/url），自动填充空值。

    Args:
        df: 原始 DataFrame。
        source: 数据源标识，"em" 代表东方财富，"sina" 代表新浪财经。

    Returns:
        字段标准化后的 DataFrame，包含标准列名。
    """
    if source == "em":
        col_map = _COLUMN_MAP_EM
    elif source == "sina":
        col_map = _COLUMN_MAP_SINA
    else:
        col_map = {}

    # 重命名已有列
    rename_map = {k: v for k, v in col_map.items() if k in df.columns}
    result = df.rename(columns=rename_map)

    # 补齐缺失的标准列
    for col in STANDARD_COLUMNS:
        if col not in result.columns:
            result[col] = ""

    # 只保留标准列 + 保留原始额外列
    extra_cols = [c for c in result.columns if c not in STANDARD_COLUMNS]
    result = result[STANDARD_COLUMNS + extra_cols].reset_index(drop=True)

    return result


# ================================================================
# 关键词过滤
# ================================================================
def _filter_by_keyword(df: pd.DataFrame, keyword: str) -> pd.DataFrame:
    """按关键词过滤新闻标题和内容。

    支持多关键词，使用 '|' 分隔，任一关键词命中即保留。

    Args:
        df: 标准化后的 DataFrame。
        keyword: 关键词字符串，多个关键词用 '|' 分隔。
            例如 "降准|降息|央行"。

    Returns:
        过滤后的 DataFrame。若 keyword 为空则不过滤。
    """
    if not keyword or not keyword.strip():
        return df

    keywords = [kw.strip() for kw in keyword.split("|") if kw.strip()]
    if not keywords:
        return df

    # 在 title 和 content 中搜索任一关键词
    pattern = "|".join(keywords)
    mask = (
        df["title"].astype(str).str.contains(pattern, na=False)
        | df["content"].astype(str).str.contains(pattern, na=False)
    )
    filtered = df[mask].reset_index(drop=True)
    print(f"{_LOG_PREFIX}关键词过滤 '{keyword}': {len(filtered)}/{len(df)} 条命中")
    return filtered


# ================================================================
# 时间过滤（时区安全）
# ================================================================
def _filter_recent(df: pd.DataFrame, hours: int = 24) -> pd.DataFrame:
    """过滤出最近 N 小时内的新闻（时区安全）。

    使用 pd.Timestamp.now() 进行时区感知的时间比较。

    Args:
        df: 包含 publish_time 列的 DataFrame。
        hours: 回溯小时数，默认 24 小时。

    Returns:
        过滤后的 DataFrame。
    """
    if "publish_time" not in df.columns:
        return df

    df = df.copy()
    df["_dt"] = pd.to_datetime(df["publish_time"].astype(str), errors="coerce")
    df = df.dropna(subset=["_dt"])

    # 时区安全：使用 pd.Timestamp.now() 获取当前时间
    cutoff = pd.Timestamp.now() - pd.Timedelta(hours=hours)
    df = df[df["_dt"] >= cutoff]
    df = df.drop(columns=["_dt"]).reset_index(drop=True)

    return df


# ================================================================
# 带重试和反爬抖动的请求封装
# ================================================================
def _request_with_retry(
    fetch_fn,
    max_retries: int = 3,
    source_name: str = "",
) -> Optional[pd.DataFrame]:
    """带指数退避 + 随机抖动的重试请求。

    Args:
        fetch_fn: 无参可调用函数，返回 DataFrame。
        max_retries: 最大重试次数。
        source_name: 数据源名称，用于日志输出。

    Returns:
        成功时返回 DataFrame，全部失败返回 None。
    """
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            return fetch_fn()
        except Exception as e:
            last_error = e
            print(f"{_LOG_PREFIX}{source_name} 第 {attempt}/{max_retries} 次请求失败: {e}")
            if attempt < max_retries:
                # 指数退避 + 随机抖动（反爬友好）
                base_wait = 2 ** attempt
                jitter = random.uniform(1, 3)
                wait = base_wait + jitter
                print(f"{_LOG_PREFIX}等待 {wait:.1f} 秒后重试 ...")
                time.sleep(wait)

    print(f"{_LOG_PREFIX}{source_name} 全部 {max_retries} 次重试失败: {last_error}")
    return None


# ================================================================
# 数据源获取函数
# ================================================================
def _fetch_raw_em() -> pd.DataFrame:
    """调用 akshare 获取东方财富原始数据。

    Returns:
        东方财富快讯原始 DataFrame。

    Raises:
        Exception: 网络或接口异常。
    """
    return ak.stock_info_global_em()


def _fetch_raw_sina() -> pd.DataFrame:
    """调用 akshare 获取新浪财经原始数据。

    Returns:
        新浪财经快讯原始 DataFrame。

    Raises:
        Exception: 网络或接口异常。
    """
    return ak.stock_info_global_sina()


# ================================================================
# 单数据源获取（含标准化 + 时间过滤）
# ================================================================
def fetch_news_em(
    max_retries: int = 3,
    keyword: str = "",
    hours: int = 24,
) -> pd.DataFrame:
    """从东方财富获取财经快讯。

    自动进行字段标准化和最近 N 小时过滤。

    Args:
        max_retries: 最大重试次数，默认 3。
        keyword: 关键词过滤，多个用 '|' 分隔。
        hours: 回溯小时数，默认 24。

    Returns:
        标准化后的 DataFrame。失败时返回空 DataFrame。
    """
    print(f"{_LOG_PREFIX}数据源: 东方财富，正在获取 ...")

    raw = _request_with_retry(_fetch_raw_em, max_retries, "东方财富")
    if raw is None or raw.empty:
        print(f"{_LOG_PREFIX}东方财富: 未获取到数据")
        return pd.DataFrame(columns=STANDARD_COLUMNS)

    # 标准化字段
    df = standardize_news_df(raw, source="em")
    # 时间过滤
    df = _filter_recent(df, hours=hours)
    # 关键词过滤
    df = _filter_by_keyword(df, keyword)

    print(f"{_LOG_PREFIX}东方财富: 获取 {len(df)} 条快讯")
    return df


def fetch_news_sina(
    max_retries: int = 3,
    keyword: str = "",
    hours: int = 24,
) -> pd.DataFrame:
    """从新浪财经获取财经快讯。

    自动进行字段标准化和最近 N 小时过滤。

    Args:
        max_retries: 最大重试次数，默认 3。
        keyword: 关键词过滤，多个用 '|' 分隔。
        hours: 回溯小时数，默认 24。

    Returns:
        标准化后的 DataFrame。失败时返回空 DataFrame。
    """
    print(f"{_LOG_PREFIX}数据源: 新浪财经，正在获取 ...")

    raw = _request_with_retry(_fetch_raw_sina, max_retries, "新浪财经")
    if raw is None or raw.empty:
        print(f"{_LOG_PREFIX}新浪财经: 未获取到数据")
        return pd.DataFrame(columns=STANDARD_COLUMNS)

    # 标准化字段
    df = standardize_news_df(raw, source="sina")
    # 时间过滤
    df = _filter_recent(df, hours=hours)
    # 关键词过滤
    df = _filter_by_keyword(df, keyword)

    print(f"{_LOG_PREFIX}新浪财经: 获取 {len(df)} 条快讯")
    return df


# ================================================================
# 统一入口（优雅降级 + 缓存）
# ================================================================
def fetch_latest_news(
    source: str = "auto",
    keyword: str = "",
    max_retries: int = 3,
    hours: int = 24,
) -> Dict:
    """获取最新财经快讯的统一入口。

    支持自动降级：主数据源失败时自动切换备选源。
    支持内存缓存：相同 (source, keyword) 在 5 分钟内直接返回缓存。
    所有源均失败时不抛异常，返回结构化结果。

    Args:
        source: 数据源选择。
            "auto" - 自动降级（东方财富 → 新浪财经），默认。
            "em"   - 仅东方财富。
            "sina" - 仅新浪财经。
        keyword: 关键词过滤，多个用 '|' 分隔，如 "降准|降息"。
        max_retries: 每个数据源的最大重试次数，默认 3。
        hours: 回溯小时数，默认 24。

    Returns:
        结构化结果字典，包含以下字段:
            - source (str): 实际使用的数据源名称。
            - success (bool): 是否成功获取到数据。
            - data (pd.DataFrame): 标准化后的新闻数据。
            - count (int): 新闻条数。
            - keyword (str): 使用的关键词。
            - cached (bool): 是否来自缓存。
    """
    # 空结果模板
    empty_result: Dict = {
        "source": "none",
        "success": False,
        "data": pd.DataFrame(columns=STANDARD_COLUMNS),
        "count": 0,
        "keyword": keyword,
        "cached": False,
    }

    # ---- 缓存检查 ----
    cache_key = (source, keyword)
    cached_df = _cache_get(cache_key)
    if cached_df is not None:
        return {
            "source": source,
            "success": True,
            "data": cached_df,
            "count": len(cached_df),
            "keyword": keyword,
            "cached": True,
        }

    # ---- 确定数据源尝试顺序 ----
    if source == "em":
        sources = [("em", fetch_news_em)]
    elif source == "sina":
        sources = [("sina", fetch_news_sina)]
    else:
        # auto 模式：东方财富优先，新浪备选
        sources = [("em", fetch_news_em), ("sina", fetch_news_sina)]

    # ---- 依次尝试各数据源 ----
    for src_name, fetch_fn in sources:
        try:
            df = fetch_fn(max_retries=max_retries, keyword=keyword, hours=hours)
            if df is not None and not df.empty:
                # 写入缓存
                _cache_set(cache_key, df)
                return {
                    "source": src_name,
                    "success": True,
                    "data": df,
                    "count": len(df),
                    "keyword": keyword,
                    "cached": False,
                }
        except Exception as e:
            print(f"{_LOG_PREFIX}{src_name} 异常: {e}，尝试下一个数据源 ...")

    # ---- 所有源均失败，优雅返回 ----
    print(f"{_LOG_PREFIX}所有数据源均失败，返回空结果")
    return empty_result


# ================================================================
# 格式化打印
# ================================================================
def print_news(result: Dict, max_items: int = 10) -> None:
    """格式化打印新闻获取结果。

    Args:
        result: fetch_latest_news() 返回的结构化结果。
        max_items: 最多打印条数，默认 10。
    """
    source = result["source"]
    count = result["count"]
    cached = result["cached"]
    keyword = result["keyword"]
    df = result["data"]

    cache_tag = " (缓存)" if cached else ""
    kw_tag = f" [关键词: {keyword}]" if keyword else ""
    print(f"\n{'=' * 60}")
    print(f"  数据源: {source}  |  共 {count} 条{cache_tag}{kw_tag}")
    print(f"{'=' * 60}")

    for i, row in df.head(max_items).iterrows():
        title = row.get("title", "")
        pub_time = row.get("publish_time", "")
        content = str(row.get("content", ""))
        # 截断过长的内容
        content_short = content[:100] + "..." if len(content) > 100 else content
        print(f"\n  [{pub_time}]")
        if title:
            print(f"    标题: {title}")
        print(f"    内容: {content_short}")


# ================================================================
# 主程序：演示用法
# ================================================================
if __name__ == "__main__":
    print(f"{_LOG_PREFIX}金融新闻行情分析 Agent - 新闻获取模块演示")
    print(f"{_LOG_PREFIX}{'=' * 50}")

    # ---- 演示 1：自动降级获取全量快讯 ----
    print(f"\n--- 演示 1: 自动降级，全量快讯 ---")
    result1 = fetch_latest_news(source="auto")
    print_news(result1, max_items=5)

    # ---- 演示 2：关键词检索 ----
    print(f"\n\n--- 演示 2: 关键词检索 '降准|降息|央行' ---")
    result2 = fetch_latest_news(source="auto", keyword="降准|降息|央行")
    print_news(result2, max_items=5)

    # ---- 演示 3：缓存验证（同一请求第二次应命中缓存） ----
    print(f"\n\n--- 演示 3: 缓存验证 ---")
    result3 = fetch_latest_news(source="auto", keyword="降准|降息|央行")
    print(f"{_LOG_PREFIX}缓存命中: {result3['cached']}, 数据量: {result3['count']}")

    # ---- 演示 4：指定单一数据源 ----
    print(f"\n\n--- 演示 4: 指定新浪财经 ---")
    result4 = fetch_latest_news(source="sina")
    print_news(result4, max_items=5)
