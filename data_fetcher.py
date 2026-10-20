"""
data_fetcher.py - 金融新闻行情分析 Agent 行情数据获取模块（重构版）
================================================================
功能：获取沪深300等指数的历史行情数据，支持字段标准化、空数据防御、
      内存缓存、重试抖动、错误信息增强等工程化能力。
"""

import random
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import akshare as ak
import pandas as pd

# ================================================================
# 日志前缀常量
# ================================================================
_LOG_PREFIX = "【行情获取】"

# ================================================================
# 标准列名定义
# ================================================================
STANDARD_COLUMNS: List[str] = ["date", "open", "high", "low", "close", "volume"]

# 指数代码映射：常用简称 → akshare symbol
# 新浪数据源的指数代码格式为 "sh000300" / "sz399001" 等
INDEX_SYMBOL_MAP: Dict[str, str] = {
    "hs300": "sh000300",       # 沪深300
    "000300": "sh000300",      # 沪深300（代码形式）
    "sh000001": "sh000001",    # 上证指数
    "000001": "sh000001",      # 上证指数（代码形式）
    "sz399001": "sz399001",    # 深证成指
    "399001": "sz399001",      # 深证成指（代码形式）
    "sz399006": "sz399006",    # 创业板指
    "399006": "sz399006",      # 创业板指（代码形式）
    "sh000016": "sh000016",    # 上证50
    "000016": "sh000016",      # 上证50（代码形式）
    "sh000905": "sh000905",    # 中证500
    "000905": "sh000905",      # 中证500（代码形式）
}

# ================================================================
# 简易内存缓存
# ================================================================
# 结构: { (symbol, start_date, end_date): (timestamp, DataFrame) }
_cache: Dict[Tuple[str, str, str], Tuple[float, pd.DataFrame]] = {}
_CACHE_TTL_SECONDS: int = 300  # 缓存有效期 5 分钟


def _cache_get(key: Tuple[str, str, str]) -> Optional[pd.DataFrame]:
    """从缓存中读取数据，过期则自动清除。

    Args:
        key: 缓存键，格式为 (symbol, start_date, end_date)。

    Returns:
        缓存命中且未过期时返回 DataFrame，否则返回 None。
    """
    if key in _cache:
        ts, df = _cache[key]
        if time.time() - ts < _CACHE_TTL_SECONDS:
            print(f"{_LOG_PREFIX}缓存命中: {key}")
            return df.copy()
        else:
            del _cache[key]
            print(f"{_LOG_PREFIX}缓存已过期，已清除: {key}")
    return None


def _cache_set(key: Tuple[str, str, str], df: pd.DataFrame) -> None:
    """将数据写入缓存。

    Args:
        key: 缓存键，格式为 (symbol, start_date, end_date)。
        df: 待缓存的 DataFrame。
    """
    _cache[key] = (time.time(), df.copy())
    print(f"{_LOG_PREFIX}数据已写入缓存: {key}")


# ================================================================
# 字段标准化
# ================================================================
def _standardize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """将 akshare 返回的 DataFrame 列名统一为标准列名。

    标准列名为: date, open, high, low, close, volume。
    自动处理 akshare 不同版本可能返回的中文或英文列名。

    Args:
        df: akshare 返回的原始 DataFrame。

    Returns:
        列名标准化后的 DataFrame。若缺少必要列则返回空 DataFrame。
    """
    # 中文列名 → 标准列名的映射
    cn_to_std: Dict[str, str] = {
        "日期": "date",
        "开盘": "open",
        "最高": "high",
        "最低": "low",
        "收盘": "close",
        "成交量": "volume",
    }

    # 先尝试中文列名映射
    rename_map = {k: v for k, v in cn_to_std.items() if k in df.columns}
    if rename_map:
        df = df.rename(columns=rename_map)

    # 小写化所有列名（兼容英文列名大小写差异）
    df.columns = [c.lower() for c in df.columns]

    # 检查标准列是否齐全
    missing = [c for c in STANDARD_COLUMNS if c not in df.columns]
    if missing:
        print(f"{_LOG_PREFIX}警告: 缺少必要列 {missing}，当前列: {list(df.columns)}")
        return pd.DataFrame(columns=STANDARD_COLUMNS)

    # 只保留标准列
    df = df[STANDARD_COLUMNS].reset_index(drop=True)
    return df


# ================================================================
# 空数据防御
# ================================================================
def _validate_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """对返回的 DataFrame 进行空数据防御检查。

    检查项目:
    1. DataFrame 是否为空。
    2. 是否包含所有必要列名。
    3. 数值列是否全为 0 或 NaN。

    Args:
        df: 待验证的 DataFrame。

    Returns:
        验证通过的 DataFrame。若不通过则返回空 DataFrame。
    """
    # 检查 1: 是否为空
    if df is None or df.empty:
        print(f"{_LOG_PREFIX}防御检查: DataFrame 为空")
        return pd.DataFrame(columns=STANDARD_COLUMNS)

    # 检查 2: 必要列是否存在
    missing_cols = [c for c in STANDARD_COLUMNS if c not in df.columns]
    if missing_cols:
        print(f"{_LOG_PREFIX}防御检查: 缺少列 {missing_cols}，当前列: {list(df.columns)}")
        return pd.DataFrame(columns=STANDARD_COLUMNS)

    # 检查 3: 数值列是否全为无效值
    numeric_cols = ["open", "high", "low", "close", "volume"]
    for col in numeric_cols:
        if col in df.columns:
            series = pd.to_numeric(df[col], errors="coerce")
            if series.notna().sum() == 0:
                print(f"{_LOG_PREFIX}防御检查: 列 '{col}' 全为无效值")
                return pd.DataFrame(columns=STANDARD_COLUMNS)

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
            print(
                f"{_LOG_PREFIX}{source_name} 第 {attempt}/{max_retries} 次请求失败: "
                f"{type(e).__name__}: {e}"
            )
            if attempt < max_retries:
                # 指数退避 + 随机抖动（反爬友好）
                base_wait = 2 ** attempt
                jitter = random.uniform(1, 3)
                wait = base_wait + jitter
                print(f"{_LOG_PREFIX}等待 {wait:.1f} 秒后重试 ...")
                time.sleep(wait)

    # 全部失败，打印增强错误信息
    print(f"{_LOG_PREFIX}{'=' * 50}")
    print(f"{_LOG_PREFIX}错误: {source_name} 全部 {max_retries} 次请求均失败")
    print(f"{_LOG_PREFIX}最后异常: {type(last_error).__name__}: {last_error}")
    print(f"{_LOG_PREFIX}建议: 请检查网络连接，或尝试升级 akshare 版本:")
    print(f"{_LOG_PREFIX}      pip install --upgrade akshare")
    print(f"{_LOG_PREFIX}{'=' * 50}")
    return None


# ================================================================
# 核心获取函数
# ================================================================
def fetch_index_history(
    symbol: str = "hs300",
    start_date: str = "",
    end_date: str = "",
    max_retries: int = 3,
) -> pd.DataFrame:
    """获取指数历史行情数据。

    支持多种指数（沪深300、上证指数、深证成指等），自动进行字段标准化、
    空数据防御、缓存管理。

    Args:
        symbol: 指数代码或简称。支持:
            "hs300" / "000300" - 沪深300（默认）
            "000001"           - 上证指数
            "399001"           - 深证成指
            "399006"           - 创业板指
            "000016"           - 上证50
            "000905"           - 中证500
            或直接传入 akshare 原始 symbol（如 "sh000300"）。
        start_date: 起始日期，格式 "YYYYMMDD"。默认为一年前。
        end_date: 结束日期，格式 "YYYYMMDD"。默认为当天。
        max_retries: 最大重试次数，默认 3。

    Returns:
        标准化后的 DataFrame，包含 date/open/high/low/close/volume 列。
        获取失败时返回空 DataFrame（不会抛出异常）。
    """
    # 默认日期：最近一年
    if not end_date:
        end_date = datetime.now().strftime("%Y%m%d")
    if not start_date:
        start_date = (datetime.now() - timedelta(days=365)).strftime("%Y%m%d")

    # 解析指数代码
    ak_symbol = INDEX_SYMBOL_MAP.get(symbol, symbol)

    print(f"{_LOG_PREFIX}请求参数: symbol={symbol} -> {ak_symbol}, "
          f"range={start_date}~{end_date}")

    # ---- 缓存检查 ----
    cache_key = (ak_symbol, start_date, end_date)
    cached = _cache_get(cache_key)
    if cached is not None:
        return _validate_dataframe(cached)

    # ---- 带重试的数据获取 ----
    def _do_fetch() -> pd.DataFrame:
        """实际调用 akshare 接口。"""
        df = ak.stock_zh_index_daily(symbol=ak_symbol)
        if df is None or df.empty:
            return pd.DataFrame()

        # 标准化列名
        df = _standardize_columns(df)
        if df.empty:
            return df

        # 按日期范围过滤
        df["date"] = df["date"].astype(str)
        df = df[(df["date"] >= start_date) & (df["date"] <= end_date)]
        df = df.reset_index(drop=True)
        return df

    raw = _request_with_retry(_do_fetch, max_retries, f"指数{ak_symbol}")

    if raw is None or raw.empty:
        print(f"{_LOG_PREFIX}未获取到有效数据")
        return pd.DataFrame(columns=STANDARD_COLUMNS)

    # 空数据防御
    result = _validate_dataframe(raw)

    # 写入缓存
    if not result.empty:
        _cache_set(cache_key, result)
        print(f"{_LOG_PREFIX}成功获取 {len(result)} 条记录")

    return result


# ================================================================
# 向后兼容的便捷函数
# ================================================================
def fetch_hs300_history(max_retries: int = 3) -> pd.DataFrame:
    """获取沪深300指数最近一年的历史交易数据（向后兼容接口）。

    Args:
        max_retries: 最大重试次数，默认 3。

    Returns:
        沪深300指数历史数据，包含 date/open/high/low/close/volume 列。

    Raises:
        RuntimeError: 当所有重试均失败时抛出。
    """
    result = fetch_index_history(symbol="hs300", max_retries=max_retries)
    if result.empty:
        raise RuntimeError(
            f"{_LOG_PREFIX}获取沪深300数据失败，已重试 {max_retries} 次。"
            f"建议: pip install --upgrade akshare"
        )
    return result


# ================================================================
# 格式化打印
# ================================================================
def print_data_summary(df: pd.DataFrame, title: str = "") -> None:
    """打印行情数据摘要信息。

    Args:
        df: 标准化后的行情 DataFrame。
        title: 标题，用于输出展示。
    """
    if df.empty:
        print(f"{_LOG_PREFIX}{title}: 无数据")
        return

    print(f"\n{'=' * 60}")
    print(f"  {title}  |  共 {len(df)} 条记录")
    print(f"{'=' * 60}")
    print(f"  日期范围: {df['date'].iloc[0]} ~ {df['date'].iloc[-1]}")
    print(f"  收盘均价: {pd.to_numeric(df['close'], errors='coerce').mean():.2f}")
    print(f"  最高收盘: {pd.to_numeric(df['close'], errors='coerce').max():.2f}")
    print(f"  最低收盘: {pd.to_numeric(df['close'], errors='coerce').min():.2f}")
    print(f"  列名: {list(df.columns)}")
    print(f"\n  ----- 前 5 行 -----")
    print(df.head().to_string(index=False))


# ================================================================
# 主程序：演示用法
# ================================================================
if __name__ == "__main__":
    print(f"{_LOG_PREFIX}金融新闻行情分析 Agent - 行情数据获取模块演示")
    print(f"{_LOG_PREFIX}{'=' * 50}")

    # ---- 演示 1：获取沪深300（默认） ----
    print(f"\n--- 演示 1: 沪深300 最近一年 ---")
    df1 = fetch_index_history(symbol="hs300")
    print_data_summary(df1, "沪深300")

    # ---- 演示 2：获取上证指数（自定义日期范围） ----
    print(f"\n\n--- 演示 2: 上证指数 最近半年 ---")
    df2 = fetch_index_history(
        symbol="000001",
        start_date=(datetime.now() - timedelta(days=180)).strftime("%Y%m%d"),
    )
    print_data_summary(df2, "上证指数")

    # ---- 演示 3：缓存验证 ----
    print(f"\n\n--- 演示 3: 缓存验证（重复请求沪深300）---")
    df3 = fetch_index_history(symbol="hs300")
    print(f"{_LOG_PREFIX}数据量: {len(df3)} 条（应来自缓存）")

    # ---- 演示 4：向后兼容接口 ----
    print(f"\n\n--- 演示 4: 向后兼容接口 fetch_hs300_history() ---")
    try:
        df4 = fetch_hs300_history()
        print(f"{_LOG_PREFIX}成功: {len(df4)} 条记录")
        print(df4.head(3).to_string(index=False))
    except RuntimeError as e:
        print(e)
0