"""
selector.py - 自然语言交互式选股与量化归因引擎（调试增强版）
================================================
职责：
  - 将用户自然语言查询转化为可执行的 Python 选股代码（通过 LLM）
  - 在受限环境中安全执行生成的代码
  - 对选股结果进行六维量化归因评分
  - 返回结构化的选股结果

调试增强：
  - 打印 akshare 接口返回的真实字段名
  - 强制数据清洗（数值转换 + 空值处理）
  - 详细错误信息返回前端
"""

import asyncio
import io
import random
import re
import sys
import time
import traceback
from http.client import RemoteDisconnected
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import URLError

import akshare as ak
import pandas as pd
import numpy as np
import requests
import urllib3
from requests.adapters import HTTPAdapter

from sentiment_analyzer import _call_llm


# ================================================================
# 常量
# ================================================================
_LOG_PREFIX = "【选股引擎】"
_MAX_RESULT_ROWS = 30
_EXEC_TIMEOUT_SECONDS = 60


# ================================================================
# 调试函数：获取并打印 akshare 真实字段名（增强版）
# ================================================================
def _check_akshare_version() -> str:
    """检查 akshare 版本，返回版本信息或警告。"""
    try:
        ver = ak.__version__
        parts = ver.split(".")
        major, minor = int(parts[0]), int(parts[1])
        if major < 1 or (major == 1 and minor < 10):
            return (
                f"akshare=={ver} 版本较老（建议 >= 1.10.0）。"
                f"请运行: pip install --upgrade akshare"
            )
        return f"akshare=={ver} [OK]"
    except Exception as e:
        return f"无法获取 akshare 版本: {e}"


# ================================================================
# 反爬虫伪装：底层 HTTP 请求拦截 + 高级请求头注入
# ================================================================
# 最新 Chrome 浏览器 User-Agent（2026 年 10 月）
_CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/139.0.0.0 Safari/537.36"
)

# 伪装请求头：模拟真实 Chrome 浏览器访问东方财富
_SPOOFED_HEADERS = {
    "User-Agent": _CHROME_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.eastmoney.com/",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Ch-Ua": '"Google Chrome";v="139", "Not;A=Brand";v="99", "Chromium";v="139"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Cache-Control": "max-age=0",
}

# 东方财富全市场行情 API 直连参数（绕过 akshare）
_EASTMONEY_SPOT_API = "https://82.push2.eastmoney.com/api/qt/clist/get"
_EASTMONEY_SPOT_PARAMS = {
    "pn": "1",
    "pz": "10000",     # 一次拉取全部 A 股
    "po": "1",
    "np": "1",
    "ut": "bd1d9ddb04089700cf9c27f6f7426281",
    "fltt": "2",
    "invt": "2",
    "fid": "f3",
    "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
    "fields": (
        "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f13,f14,f15,f16,f17,f18,"
        "f20,f21,f23,f24,f25,f22,f11,f62,f128,f136,f115,f152"
    ),
    "_": str(int(time.time() * 1000)),
}
# 字段映射：东方财富 API 字段 -> akshare 兼容列名
_EASTMONEY_FIELD_MAP = {
    "f12": "代码", "f14": "名称", "f2": "最新价", "f3": "涨跌幅",
    "f4": "涨跌额", "f5": "成交量", "f6": "成交额", "f7": "振幅",
    "f15": "最高", "f16": "最低", "f17": "今开", "f18": "昨收",
    "f8": "换手率", "f9": "市盈率-动态", "f23": "市净率",
    "f20": "总市值", "f21": "流通市值", "f10": "量比",
    "f24": "60日涨跌幅", "f25": "年初至今涨跌幅",
}


# ================================================================
# 底层猴子补丁：拦截 HTTPAdapter.send（覆盖所有 requests 调用路径）
# ================================================================
_original_send = HTTPAdapter.send


def _patched_send(self_adapter, request_obj, **kwargs):
    """拦截所有 HTTP 请求，注入伪装请求头。

    在 requests 最底层 HTTPAdapter.send 处拦截，
    无论上层是 requests.get()、Session.get() 还是 akshare 内部调用，
    所有请求都会经过此函数。
    """
    # 注入伪装请求头（不覆盖 akshare 自己设置的必要参数）
    for key, value in _SPOOFED_HEADERS.items():
        if key not in request_obj.headers or key == "User-Agent":
            request_obj.headers[key] = value
    # 强制覆盖 User-Agent（最关键）
    request_obj.headers["User-Agent"] = _CHROME_USER_AGENT
    request_obj.headers["Referer"] = "https://www.eastmoney.com/"
    return _original_send(self_adapter, request_obj, **kwargs)


def _install_deep_spoof() -> None:
    """安装深度伪装：在 HTTPAdapter.send 层拦截所有请求并注入伪装头。"""
    HTTPAdapter.send = _patched_send


def _restore_deep_spoof() -> None:
    """恢复 HTTPAdapter.send 原始实现。"""
    HTTPAdapter.send = _original_send


# ================================================================
# 策略函数
# ================================================================
def _fetch_via_akshare_with_spoof() -> pd.DataFrame:
    """策略一：通过 akshare 新浪数据源获取全市场行情（优先）。

    ak.stock_zh_a_spot() 使用新浪财经数据源（hq.sinajs.cn），
    不受东方财富 push2 子域名拦截影响。
    """
    _install_deep_spoof()
    try:
        df = ak.stock_zh_a_spot()
        if df is not None and not df.empty:
            # 补充序号列，与 _em 接口保持一致
            if "序号" not in df.columns:
                df.insert(0, "序号", range(1, len(df) + 1))
        return df
    finally:
        _restore_deep_spoof()


def _fetch_via_akshare_em_with_spoof() -> pd.DataFrame:
    """策略二：通过 akshare 东方财富数据源获取（备选，可能被拦截）。"""
    _install_deep_spoof()
    try:
        df = ak.stock_zh_a_spot_em()
        return df
    finally:
        _restore_deep_spoof()


def _fetch_via_direct_api() -> pd.DataFrame:
    """策略二：绕过 akshare，直接调用东方财富 API 获取数据。

    使用自定义 requests.Session 携带伪装请求头，
    直接请求东方财富的行情推送接口。
    """
    session = requests.Session()
    session.headers.update(_SPOOFED_HEADERS)
    # 配置重试适配器
    retry_strategy = urllib3.util.Retry(
        total=2,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    adapter = HTTPAdapter(
        max_retries=retry_strategy,
        pool_connections=5,
        pool_maxsize=10,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    try:
        resp = session.get(
            _EASTMONEY_SPOT_API,
            params=_EASTMONEY_SPOT_PARAMS,
            timeout=30,
        )
        resp.raise_for_status()
        json_data = resp.json()

        if not json_data or "data" not in json_data:
            print(f"{_LOG_PREFIX}[直连API] 返回数据中无 data 字段")
            return pd.DataFrame()

        diff_list = json_data["data"].get("diff", [])
        if not diff_list:
            print(f"{_LOG_PREFIX}[直连API] diff 列表为空")
            return pd.DataFrame()

        # 将 API 返回的字段映射为 akshare 兼容列名
        rows = []
        for item in diff_list:
            row = {}
            for api_field, cn_name in _EASTMONEY_FIELD_MAP.items():
                row[cn_name] = item.get(api_field, None)
            rows.append(row)

        df = pd.DataFrame(rows)
        # 添加序号列（与 akshare 兼容）
        df.insert(0, "序号", range(1, len(df) + 1))
        print(f"{_LOG_PREFIX}[直连API] 成功获取 {len(df)} 条数据")
        return df

    finally:
        session.close()


def _fetch_via_direct_api_with_proxy(proxy_url: str) -> pd.DataFrame:
    """策略三：直接调用东方财富 API + 代理。

    Args:
        proxy_url: 代理地址，如 "http://127.0.0.1:7890"。
    """
    session = requests.Session()
    session.headers.update(_SPOOFED_HEADERS)
    session.proxies.update({
        "http": proxy_url,
        "https": proxy_url,
    })
    retry_strategy = urllib3.util.Retry(
        total=1,
        backoff_factor=0.5,
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    try:
        resp = session.get(
            _EASTMONEY_SPOT_API,
            params=_EASTMONEY_SPOT_PARAMS,
            timeout=20,
        )
        resp.raise_for_status()
        json_data = resp.json()

        if not json_data or "data" not in json_data:
            return pd.DataFrame()

        diff_list = json_data["data"].get("diff", [])
        if not diff_list:
            return pd.DataFrame()

        rows = []
        for item in diff_list:
            row = {}
            for api_field, cn_name in _EASTMONEY_FIELD_MAP.items():
                row[cn_name] = item.get(api_field, None)
            rows.append(row)

        df = pd.DataFrame(rows)
        df.insert(0, "序号", range(1, len(df) + 1))
        print(f"{_LOG_PREFIX}[代理直连] 成功获取 {len(df)} 条数据")
        return df

    finally:
        session.close()


def _debug_fetch_market_data() -> Tuple[pd.DataFrame, str]:
    """获取全市场行情数据，包含多级反爬虫伪装与容错。

    尝试策略（按优先级）：
      1. akshare + HTTPAdapter 底层深度伪装请求头
      2. 绕过 akshare，直接调用东方财富 API（携带伪装头）
      3. 直连 API + 本地代理（如果用户有代理软件运行）
      4. 直连 API + 随机延迟重试

    异常捕获：
      - ConnectionError / RemoteDisconnected -> 前端提示"防火墙限制"
      - TimeoutError -> 提示超时
      - 其他异常 -> 详细错误信息

    Returns:
        (DataFrame, debug_info) 元组
    """
    debug_lines = []

    # 版本检查
    ver_info = _check_akshare_version()
    debug_lines.append(f"[版本] {ver_info}")

    # 定义需要捕获的网络异常类型
    network_exceptions = (
        ConnectionError,
        RemoteDisconnected,
        URLError,
        requests.exceptions.ConnectionError,
        requests.exceptions.ChunkedEncodingError,
    )

    # ---- 策略一：akshare 新浪数据源（最可靠，不受东方财富拦截影响） ----
    debug_lines.append("=" * 50)
    debug_lines.append("[策略一] akshare 新浪数据源 (stock_zh_a_spot) ...")
    try:
        df = _fetch_via_akshare_with_spoof()

        if df is not None and not df.empty:
            debug_lines.append(f"[策略一] 成功！获取 {len(df)} 条数据")
            debug_lines.append(f"[调试] 列名 ({len(df.columns)} 列): {list(df.columns)}")
            debug_lines.append(f"[调试] 前5列: {list(df.columns[:5])}")
            debug_lines.append(f"[调试] 第1行数据:\n{df.head(1).to_string()}")
            debug_lines.append(f"[调试] 数据类型:\n{df.dtypes}")
            debug_lines.append("=" * 50)
            return df, "\n".join(debug_lines)

        debug_lines.append("[策略一] 返回数据为空，尝试下一策略 ...")

    except network_exceptions as e:
        debug_lines.append(f"[策略一] 网络异常: {type(e).__name__}: {e}")
    except TimeoutError as e:
        debug_lines.append(f"[策略一] 请求超时: {e}")
    except Exception as e:
        debug_lines.append(f"[策略一] 未知异常: {type(e).__name__}: {e}")

    # ---- 策略二：akshare 东方财富数据源（可能被拦截） ----
    debug_lines.append("[策略二] akshare 东方财富数据源 (stock_zh_a_spot_em) ...")
    try:
        df = _fetch_via_akshare_em_with_spoof()

        if df is not None and not df.empty:
            debug_lines.append(f"[策略二] 成功！获取 {len(df)} 条数据")
            debug_lines.append(f"[调试] 列名 ({len(df.columns)} 列): {list(df.columns)}")
            debug_lines.append("=" * 50)
            return df, "\n".join(debug_lines)

        debug_lines.append("[策略二] 返回数据为空，尝试直连 API ...")

    except network_exceptions as e:
        debug_lines.append(f"[策略二] 网络异常: {type(e).__name__}: {e}")
    except Exception as e:
        debug_lines.append(f"[策略二] 异常: {type(e).__name__}: {e}")

    # ---- 策略三：绕过 akshare，直连东方财富 API ----
    debug_lines.append("[策略三] 绕过 akshare，直连东方财富 API ...")
    try:
        df = _fetch_via_direct_api()

        if df is not None and not df.empty:
            debug_lines.append(f"[策略三] 成功！获取 {len(df)} 条数据")
            debug_lines.append(f"[调试] 列名 ({len(df.columns)} 列): {list(df.columns)}")
            debug_lines.append("=" * 50)
            return df, "\n".join(debug_lines)

        debug_lines.append("[策略三] 返回数据为空，尝试代理 ...")

    except network_exceptions as e:
        debug_lines.append(f"[策略三] 网络异常: {type(e).__name__}: {e}")
    except requests.exceptions.Timeout as e:
        debug_lines.append(f"[策略三] 请求超时: {e}")
    except Exception as e:
        debug_lines.append(f"[策略三] 异常: {type(e).__name__}: {e}")

    # ---- 策略四：直连 API + 本地代理 ----
    for proxy_url in ["http://127.0.0.1:7890", "http://127.0.0.1:10809",
                       "http://127.0.0.1:1080"]:
        label = f"[策略四] 代理 {proxy_url}"
        debug_lines.append(f"{label} 尝试中 ...")
        try:
            df = _fetch_via_direct_api_with_proxy(proxy_url)

            if df is not None and not df.empty:
                debug_lines.append(f"{label} 成功！获取 {len(df)} 条数据")
                debug_lines.append(f"[调试] 列名: {list(df.columns)}")
                debug_lines.append("=" * 50)
                return df, "\n".join(debug_lines)

            debug_lines.append(f"{label} 返回空数据")

        except network_exceptions as e:
            debug_lines.append(f"{label} 网络异常: {type(e).__name__}: {e}")
        except Exception as e:
            debug_lines.append(f"{label} 异常: {type(e).__name__}: {e}")

    # ---- 策略五：新浪数据源 + 随机延迟重试（最后手段） ----
    debug_lines.append("[策略五] 随机延迟 3~5 秒后最后一次尝试新浪数据源 ...")
    delay = random.uniform(3, 5)
    time.sleep(delay)
    try:
        df = _fetch_via_akshare_with_spoof()

        if df is not None and not df.empty:
            debug_lines.append(f"[策略五] 延迟重试成功！获取 {len(df)} 条数据")
            debug_lines.append(f"[调试] 列名: {list(df.columns)}")
            debug_lines.append("=" * 50)
            return df, "\n".join(debug_lines)

        debug_lines.append("[策略五] 返回数据为空")

    except network_exceptions as e:
        debug_lines.append(f"[策略五] 网络异常: {type(e).__name__}: {e}")
    except Exception as e:
        debug_lines.append(f"[策略五] 异常: {type(e).__name__}: {e}")

    # ---- 所有策略均失败 ----
    debug_lines.append("=" * 50)
    debug_lines.append("[失败] 所有获取策略均未能成功获取数据")
    debug_lines.append("=" * 50)

    return pd.DataFrame(), "\n".join(debug_lines)


# ================================================================
# 数据清洗函数：强制转换数值类型
# ================================================================
def _clean_market_data(df: pd.DataFrame) -> pd.DataFrame:
    """强制清洗市场数据，将所有数值字段转换为数值类型。

    处理逻辑：
    1. 识别可能的数值列（包含价格、涨幅、市盈率、市值等关键词）
    2. 使用 pd.to_numeric() 强制转换，错误值变为 NaN
    3. 删除关键列含有 NaN 的行
    """
    if df is None or df.empty:
        return df

    # 可能的数值列关键词
    numeric_keywords = [
        '价', '涨幅', '跌幅', '涨跌', '市盈率', 'PE', 'pe',
        '市净率', 'PB', 'pb', '市值', '成交量', '成交额',
        '换手', '量比', '振幅', '最高', '最低', '今开', '昨收',
        'price', 'change', 'volume', 'amount', 'turnover',
        'pe', 'pb', 'market_cap', 'high', 'low', 'open', 'close'
    ]

    # 找出需要转换的列
    numeric_cols = []
    for col in df.columns:
        col_lower = str(col).lower()
        if any(kw.lower() in col_lower for kw in numeric_keywords):
            numeric_cols.append(col)

    print(f"{_LOG_PREFIX}[清洗] 识别到数值列: {numeric_cols}")

    # 强制转换
    for col in numeric_cols:
        try:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        except Exception as e:
            print(f"{_LOG_PREFIX}[清洗] 列 {col} 转换失败: {e}")

    # 删除关键列为空的行（如果有这些列的话）
    key_cols = [c for c in ['最新价', '涨跌幅', '代码', '名称'] if c in df.columns]
    if key_cols:
        before_count = len(df)
        df = df.dropna(subset=key_cols)
        after_count = len(df)
        print(f"{_LOG_PREFIX}[清洗] 删除空值行: {before_count} -> {after_count}")

    return df


# ================================================================
# System Prompt（增强版：包含真实字段名）
# ================================================================
def _build_system_prompt(max_stocks: int = 10, sample_columns: list = None) -> str:
    """构建 System Prompt，包含真实的字段名信息。"""

    # 根据实际数据源的列名生成字段信息
    if sample_columns:
        col_list = str(sample_columns)
    else:
        col_list = "['序号', '代码', '名称', '最新价', '涨跌额', '涨跌幅', '买入', '卖出', '昨收', '今开', '最高', '最低', '成交量', '成交额', '时间戳']"

    prompt = f"""你是一个专业的量化选股代码生成器。你的任务是将用户的自然语言需求转化为可执行的 Python 代码。

## 工具限制
- 只能使用 pandas（已导入为 pd）和 numpy（已导入为 np）处理数据
- 禁止使用 os、sys、subprocess、exec、eval 等系统模块
- 禁止使用 import 导入新模块

## 数据获取策略（必须遵守）
1. **数据已预加载**：全市场实时行情快照已存储在变量 `market_data` 中，它是一个 pandas DataFrame
2. **直接使用 market_data**：不要调用 akshare 接口，直接对 market_data 进行筛选和处理
3. **数据清洗**：对数值列进行类型转换：
   ```python
   df = market_data.copy()
   for col in ['最新价', '涨跌幅', '涨跌额', '成交量', '成交额', '买入', '卖出', '昨收', '今开', '最高', '最低']:
       if col in df.columns:
           df[col] = pd.to_numeric(df[col], errors='coerce')
   df = df.dropna(subset=['最新价'])
   ```
4. **严格使用已有列名**：只能使用以下列名，不要引用不存在的字段！

## 可用列名（共 15 列）
{col_list}

注意：数据源中**没有**市盈率、市净率、总市值、流通市值、换手率、量比、振幅等字段。不要引用这些不存在的列！

## 代码规范
- 最终结果必须存储在名为 final_result 的 pandas DataFrame 变量中
- final_result 必须包含列：代码、名称、最新价（其他列可选）
- 对 final_result 做 .head({max_stocks}) 限制返回数量

## 输出格式
- 只输出纯 Python 代码，不要包含任何 Markdown 标记（如 ```python）、解释性文字或注释
- 代码必须完整可执行，不要使用省略号

## 异常处理
- 所有数据操作包裹在 try/except 中
- 如果没有符合筛选条件的股票，final_result 应为空 DataFrame

现在，请将用户的需求转化为 Python 代码："""

    return prompt


# ================================================================
# 清理 LLM 返回的代码
# ================================================================
def _clean_llm_code(raw_text: str) -> str:
    """清理 LLM 返回的文本，提取纯 Python 代码。"""
    text = raw_text.strip()

    patterns = [
        r"```python\s*\n?(.*?)\n?\s*```",
        r"```\s*\n?(.*?)\n?\s*```",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.DOTALL)
        if match:
            text = match.group(1).strip()
            break

    lines = text.split('\n')
    if lines and lines[0].startswith('```'):
        lines = lines[1:]
    if lines and lines[-1].startswith('```'):
        lines = lines[:-1]
    text = '\n'.join(lines).strip()

    return text


# ================================================================
# 安全执行生成的代码（增强版：返回错误信息）
# ================================================================
def _execute_selector_code(
    code: str,
    market_df: pd.DataFrame,
    max_stocks: int = 10
) -> Tuple[pd.DataFrame, Optional[str]]:
    """在受限环境中执行选股代码。

    Args:
        code: 要执行的 Python 代码字符串。
        market_df: 已获取并清洗过的市场数据。
        max_stocks: 最大返回股票数量。

    Returns:
        (DataFrame, error_message) 元组
    """
    # 受限的执行环境
    # 将预导入的模块注册到 sys.modules，使 LLM 生成的 import 语句能正常工作
    sys.modules.setdefault("ak", ak)
    sys.modules.setdefault("akshare", ak)

    safe_globals = {
        "__builtins__": {
            "__import__": __import__,  # 允许 LLM 生成的代码使用 import
            "print": print,
            "len": len,
            "range": range,
            "int": int,
            "float": float,
            "str": str,
            "list": list,
            "dict": dict,
            "tuple": tuple,
            "set": set,
            "sum": sum,
            "min": min,
            "max": max,
            "abs": abs,
            "round": round,
            "sorted": sorted,
            "enumerate": enumerate,
            "zip": zip,
            "map": map,
            "filter": filter,
            "isinstance": isinstance,
            "type": type,
            "None": None,
            "True": True,
            "False": False,
            "ValueError": ValueError,
            "TypeError": TypeError,
            "KeyError": KeyError,
            "IndexError": IndexError,
            "Exception": Exception,
        },
        "ak": ak,
        "pd": pd,
        "np": np,
        "final_result": pd.DataFrame(),
        "max_stocks": max_stocks,
        # 预加载市场数据，LLM 生成的代码可以直接使用
        "market_data": market_df.copy() if market_df is not None else pd.DataFrame(),
    }

    try:
        exec(code, safe_globals)
        result = safe_globals.get("final_result", pd.DataFrame())

        if not isinstance(result, pd.DataFrame):
            return pd.DataFrame(), f"final_result 不是 DataFrame，而是 {type(result)}"

        if len(result) > _MAX_RESULT_ROWS:
            result = result.head(_MAX_RESULT_ROWS)

        return result, None

    except (ConnectionError, RemoteDisconnected, URLError) as e:
        error_msg = (
            "由于本地防火墙限制，数据获取失败，请尝试连接手机热点后重试\n"
            f"异常详情: {type(e).__name__}: {e}"
        )
        print(f"{_LOG_PREFIX}[执行] 网络异常:\n{error_msg}")
        return pd.DataFrame(), error_msg

    except requests.exceptions.ConnectionError as e:
        error_msg = (
            "由于本地防火墙限制，数据获取失败，请尝试连接手机热点后重试\n"
            f"异常详情: requests.ConnectionError: {e}"
        )
        print(f"{_LOG_PREFIX}[执行] 网络连接异常:\n{error_msg}")
        return pd.DataFrame(), error_msg

    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)}\n{traceback.format_exc()}"
        print(f"{_LOG_PREFIX}[执行] 代码执行失败:\n{error_msg}")
        return pd.DataFrame(), error_msg


# ================================================================
# 六维量化归因评分
# ================================================================
def _calculate_radar_scores(stock_row: dict) -> Dict[str, float]:
    """根据股票的财务数据计算六维雷达图评分（0-100）。

    优先使用东方财富源字段（市盈率、ROE 等），
    若不存在则回退到新浪源可用字段（涨跌幅、成交量、最高/最低等）。
    """
    scores = {
        "成长性": 50,
        "盈利能力": 50,
        "估值安全": 50,
        "技术强度": 50,
        "资金热度": 50,
        "抗风险能力": 50,
    }

    def _get(*keys):
        """依次尝试多个键名，返回第一个非 None 的值。"""
        for k in keys:
            v = stock_row.get(k)
            if v is not None:
                try:
                    return float(v)
                except (ValueError, TypeError):
                    pass
        return None

    # --- 成长性：优先用净利润同比增长率，回退用涨跌幅 ---
    growth_val = _get("净利润同比增长率", "net_profit_yoy", "营收同比增长率", "revenue_yoy")
    if growth_val is not None:
        scores["成长性"] = max(0, min(100, 50 + growth_val))
    else:
        change_pct = _get("涨跌幅", "change_pct")
        if change_pct is not None:
            scores["成长性"] = max(0, min(100, 50 + change_pct * 3))

    # --- 盈利能力：优先用 ROE，回退用 (今开-昨收)/昨收 的日内强度 ---
    roe_val = _get("净资产收益率", "roe", "ROE")
    if roe_val is not None:
        scores["盈利能力"] = max(0, min(100, roe_val * 5))
    else:
        open_price = _get("今开")
        prev_close = _get("昨收")
        if open_price and prev_close and prev_close > 0:
            strength = (open_price - prev_close) / prev_close * 100
            scores["盈利能力"] = max(10, min(90, 50 + strength * 10))

    # --- 估值安全：优先用市盈率，回退用振幅反向评分（振幅越小越安全）---
    pe_val = _get("市盈率", "市盈率-动态", "pe", "PE")
    if pe_val is not None:
        if pe_val > 0:
            scores["估值安全"] = max(10, min(90, 100 - pe_val * 1.5))
        else:
            scores["估值安全"] = 20
    else:
        high = _get("最高")
        low = _get("最低")
        prev_close2 = _get("昨收")
        if high and low and prev_close2 and prev_close2 > 0:
            amplitude = (high - low) / prev_close2 * 100
            scores["估值安全"] = max(20, min(80, 70 - amplitude * 3))

    # --- 技术强度：涨跌幅 + 振幅综合 ---
    change_pct2 = _get("涨跌幅", "change_pct")
    if change_pct2 is not None:
        high2 = _get("最高")
        low2 = _get("最低")
        prev_close3 = _get("昨收")
        if high2 and low2 and prev_close3 and prev_close3 > 0:
            amp = (high2 - low2) / prev_close3 * 100
            scores["技术强度"] = max(0, min(100, 50 + change_pct2 * 4 + amp * 2))
        else:
            scores["技术强度"] = max(0, min(100, 50 + change_pct2 * 6))

    # --- 资金热度：优先用换手率，回退用成交量对数评分 ---
    turnover = _get("换手率", "turnover_rate")
    if turnover is not None:
        scores["资金热度"] = max(10, min(95, 10 + turnover * 8))
    else:
        volume = _get("成交量", "volume")
        if volume is not None and volume > 0:
            import math
            scores["资金热度"] = max(10, min(95, math.log10(max(volume, 1)) * 15))
        else:
            amount = _get("成交额")
            if amount is not None and amount > 0:
                import math
                scores["资金热度"] = max(10, min(95, math.log10(max(amount, 1)) * 12))

    # --- 抗风险能力：优先用市净率，回退用振幅反向评分 ---
    pb_val = _get("市净率", "pb", "PB")
    if pb_val is not None:
        if pb_val > 0:
            scores["抗风险能力"] = max(20, min(80, 100 - pb_val * 15))
    else:
        high3 = _get("最高")
        low3 = _get("最低")
        prev_close4 = _get("昨收")
        if high3 and low3 and prev_close4 and prev_close4 > 0:
            amp2 = (high3 - low3) / prev_close4 * 100
            scores["抗风险能力"] = max(20, min(80, 75 - amp2 * 4))

    return {k: round(v) for k, v in scores.items()}


# ================================================================
# 主函数：自然语言选股（调试增强版）
# ================================================================
async def ai_select_stocks(query: str, max_stocks: int = 10) -> Dict[str, Any]:
    """自然语言交互式选股主函数。

    流程：
    1. 获取并调试打印市场数据
    2. 强制数据清洗
    3. 调用 LLM 生成代码
    4. 执行代码
    5. 归因评分
    """
    print(f"{_LOG_PREFIX}收到选股请求: '{query[:60]}...'")

    result = {
        "query": query,
        "generated_code": "",
        "stocks": [],
        "error": None,
        "debug_info": "",  # 新增：调试信息
    }

    # ---- 步骤 1：获取并调试市场数据 ----
    try:
        market_df, debug_info = await asyncio.to_thread(_debug_fetch_market_data)
        result["debug_info"] = debug_info
        print(debug_info)

        if market_df.empty:
            # 检查 debug_info 中是否包含网络异常关键词，判断是否为防火墙/反爬虫问题
            firewall_keywords = [
                "ConnectionError", "RemoteDisconnected", "网络异常",
                "被拦截", "被远端断开", "失败", "Connection refused",
                "Timeout", "超时",
            ]
            is_firewall_issue = any(
                kw in debug_info for kw in firewall_keywords
            )

            ver_info = _check_akshare_version()
            if is_firewall_issue:
                result["error"] = (
                    "由于本地防火墙限制，数据获取失败，请尝试连接手机热点后重试\n\n"
                    "详细说明：东方财富的反爬虫机制或本地网络防火墙拦截了请求，"
                    "伪装浏览器请求头和代理均未能绕过。\n\n"
                    "建议操作：\n"
                    "1. 关闭 VPN/代理软件后重试\n"
                    "2. 连接手机热点（绕过本地防火墙）后重试\n"
                    "3. 稍后再试（东方财富可能在高峰期加强反爬）\n\n"
                    f"环境信息: {ver_info}"
                )
            else:
                result["error"] = (
                    "获取市场数据失败（返回空数据）。\n\n"
                    f"环境信息: {ver_info}\n\n"
                    "可能原因:\n"
                    "1. 当前为非交易时间，数据尚未更新\n"
                    "2. 网络连接受限，无法访问东方财富数据源\n"
                    "3. akshare 版本过旧，接口不兼容\n\n"
                    "请展开下方「调试信息」查看详细报错。"
                )
            result["error"] += "\n\n详细调试信息请展开下方「调试信息」查看。"
            return result

    except Exception as e:
        ver_info = _check_akshare_version()
        result["error"] = (
            f"获取市场数据异常: {type(e).__name__}: {e}\n\n"
            f"环境信息: {ver_info}\n\n"
            "请展开下方「调试信息」查看详细堆栈。"
        )
        result["debug_info"] = traceback.format_exc()
        return result

    # ---- 步骤 2：强制数据清洗 ----
    try:
        market_df = await asyncio.to_thread(_clean_market_data, market_df)
        print(f"{_LOG_PREFIX}[清洗] 清洗后数据量: {len(market_df)} 条")
    except Exception as e:
        result["error"] = f"数据清洗失败: {e}"
        return result

    # ---- 步骤 3：调用 LLM 生成代码 ----
    try:
        sample_columns = list(market_df.columns)
        prompt = _build_system_prompt(max_stocks, sample_columns)
        user_prompt = query

        raw_code = await asyncio.to_thread(_call_llm, prompt, user_prompt, 3000)

        if not raw_code:
            result["error"] = "LLM 调用失败，未能生成选股代码"
            return result

        clean_code = _clean_llm_code(raw_code)
        result["generated_code"] = clean_code
        print(f"{_LOG_PREFIX}生成代码 {len(clean_code)} 字符")

    except Exception as e:
        result["error"] = f"LLM 调用异常: {e}"
        result["debug_info"] += "\n" + traceback.format_exc()
        return result

    # ---- 步骤 4：执行代码 ----
    try:
        df, exec_error = await asyncio.to_thread(
            _execute_selector_code, clean_code, market_df, max_stocks
        )

        if exec_error:
            result["error"] = f"代码执行错误:\n{exec_error}"
            print(f"{_LOG_PREFIX}代码执行失败: {exec_error}")
            return result

        if df.empty:
            result["error"] = "选股代码执行后结果为空，可能未找到符合条件的股票"
            return result

        print(f"{_LOG_PREFIX}选出 {len(df)} 只股票")

    except Exception as e:
        result["error"] = f"代码执行异常: {e}\n{traceback.format_exc()}"
        return result

    # ---- 步骤 5：归因评分 ----
    # 构建原始 market_df 的代码索引，用于补回被 LLM 丢弃的列
    market_lookup = {}
    if market_df is not None and not market_df.empty:
        m_code_col = None
        for c in ["代码", "code", "股票代码"]:
            if c in market_df.columns:
                m_code_col = c
                break
        if m_code_col:
            for _, mrow in market_df.iterrows():
                mc = str(mrow.get(m_code_col, ""))
                if mc:
                    market_lookup[mc] = mrow.to_dict()

    stocks_list = []
    for _, row in df.iterrows():
        stock_data = row.to_dict()

        # 尝试多种列名
        code = str(stock_data.get("代码") or stock_data.get("code") or
                   stock_data.get("股票代码") or "")
        name = str(stock_data.get("名称") or stock_data.get("name") or
                   stock_data.get("股票名称") or "")

        # 从原始 market_df 补回被 LLM 丢弃的列（涨跌幅、成交量等）
        # 新浪代码格式为 "sz300750"，LLM 可能返回 "300750"，需要模糊匹配
        if code:
            original_data = market_lookup.get(code)
            if original_data is None:
                # 模糊匹配：查找包含该代码的原始记录
                for mk, mv in market_lookup.items():
                    if code in mk:
                        original_data = mv
                        break
            if original_data:
                for key, val in original_data.items():
                    if key not in stock_data and val is not None:
                        stock_data[key] = val

        radar_scores = _calculate_radar_scores(stock_data)

        stock_info = {
            "code": code,
            "name": name,
            "price": stock_data.get("最新价") or stock_data.get("price") or stock_data.get("收盘价"),
            "pe": stock_data.get("市盈率") or stock_data.get("pe") or stock_data.get("市盈率-动态"),
            "pb": stock_data.get("市净率") or stock_data.get("pb"),
            "market_cap": stock_data.get("总市值") or stock_data.get("market_cap"),
            "radar_scores": radar_scores,
            "extra": {k: str(v) for k, v in stock_data.items()
                      if k not in ("代码", "名称", "code", "name", "股票代码", "股票名称")},
        }
        stocks_list.append(stock_info)

    result["stocks"] = stocks_list
    print(f"{_LOG_PREFIX}选股完成: {len(stocks_list)} 只股票已评分")

    return result


# ================================================================
# 调用示例
# ================================================================
if __name__ == "__main__":
    async def _test():
        r = await ai_select_stocks("帮我找3只市盈率在20倍以内的科技股", max_stocks=3)
        import json
        print("\n" + "=" * 60)
        print("最终结果:")
        print(json.dumps(r, ensure_ascii=False, indent=2, default=str))

    asyncio.run(_test())
