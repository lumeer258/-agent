"""
fundamental_analyzer.py - 个股基本面分析模块
============================================
职责：
  - 根据股票代码获取基本面数据（盈利能力、成长性、财务健康、估值指标）
  - 使用 akshare 获取数据，优先使用新浪/datacenter 等可用数据源
  - 每个维度独立 try/except，单字段失败不影响整体结构
  - 返回稳定的 JSON 结构

数据源说明：
  - stock_financial_abstract（东方财富 datacenter，可用）→ 核心财务指标
  - stock_financial_report_sina（新浪财经，可用）→ 利润表/资产负债表/现金流量表
  - stock_zh_a_spot（新浪财经，可用）→ 股票名称、最新价
  - stock_individual_info_em（东方财富 push2，被拦截）→ 已移除
"""

import json
import traceback
from typing import Any, Dict, Optional

import akshare as ak
import pandas as pd


# ================================================================
# 辅助工具函数
# ================================================================

def _safe_float(val: Any) -> Optional[float]:
    """安全地将值转换为 float，失败返回 None。"""
    if val is None:
        return None
    try:
        if pd.isna(val):
            return None
        val_str = str(val).strip().replace(',', '')
        if not val_str or val_str == '--' or val_str == '-':
            return None
        return round(float(val_str), 4)
    except (ValueError, TypeError):
        return None


def _safe_str(val: Any) -> Optional[str]:
    """安全地将值转换为字符串，失败返回 None。"""
    if val is None:
        return None
    try:
        if pd.isna(val):
            return None
        return str(val).strip()
    except (ValueError, TypeError):
        return None


def _find_column(df: pd.DataFrame, candidates: list) -> Optional[str]:
    """在 DataFrame 的列名中查找第一个匹配的候选列名（兼容中英文列名变化）。

    Args:
        df: 目标 DataFrame。
        candidates: 候选列名列表，按优先级排列。

    Returns:
        匹配到的实际列名，未找到返回 None。
    """
    cols = set(df.columns)
    for c in candidates:
        if c in cols:
            return c
    return None


def _calc_yoy(current: Optional[float], previous: Optional[float]) -> Optional[float]:
    """计算同比增长率（%）。

    Args:
        current: 本期值。
        previous: 去年同期值。

    Returns:
        同比增长率（%），无法计算时返回 None。
    """
    if current is None or previous is None:
        return None
    if previous == 0:
        return None
    return round((current - previous) / abs(previous) * 100, 4)


# ================================================================
# 核心函数：个股基本面分析
# ================================================================

def analyze_fundamental(symbol: str = "300750") -> Dict[str, Any]:
    """对指定股票进行基本面分析，返回盈利能力、成长性、财务健康、估值四大维度数据。

    每个维度内部独立 try/except，单个字段获取失败不会导致整个函数报错，
    对应字段返回 None，保证返回结构始终稳定。

    Args:
        symbol: 6 位股票代码，如 '300750'（宁德时代）、'600519'（贵州茅台）。

    Returns:
        包含 code 和 data 的字典，结构见模块文档。
    """
    # 返回结构模板（始终稳定返回）
    result: Dict[str, Any] = {
        "code": 200,
        "data": {
            "symbol": symbol,
            "name": None,
            "profitability": {
                "revenue": None,          # 营业收入（元）
                "net_profit": None,       # 净利润（元）
                "gross_margin": None,     # 毛利率（%）
                "net_margin": None,       # 净利率（%）
                "roe": None,              # ROE（%）
            },
            "growth": {
                "revenue_yoy": None,      # 营收同比增长率（%）
                "net_profit_yoy": None,   # 净利润同比增长率（%）
            },
            "health": {
                "debt_ratio": None,       # 资产负债率（%）
                "operating_cashflow": None,  # 经营现金流净额（元）
            },
            "valuation": {
                "pe": None,               # 市盈率（动态）
                "pb": None,               # 市净率
                "market_cap": None,       # 总市值（元）
                "float_market_cap": None, # 流通市值（元）
            },
        },
    }

    # ============================================================
    # 1. 获取股票名称 + 最新价（新浪实时行情）
    #    数据源：stock_zh_a_spot（新浪财经，已确认可用）
    # ============================================================
    # 用于后续估值计算
    current_price = None
    try:
        spot_df = ak.stock_zh_a_spot()
        if spot_df is not None and not spot_df.empty:
            # 匹配股票代码（新浪代码格式：sz300750 / sh600519）
            code_col = _find_column(spot_df, ["代码", "股票代码", "symbol"])
            name_col = _find_column(spot_df, ["名称", "股票名称", "name"])
            price_col = _find_column(spot_df, ["最新价", "收盘价", "trade"])

            if code_col and name_col:
                # 新浪代码格式为 "sz300750" 或 "sh600519"，用 contains 匹配
                match = spot_df[spot_df[code_col].str.contains(symbol, na=False)]
                if not match.empty:
                    row = match.iloc[0]
                    result["data"]["name"] = _safe_str(row[name_col])
                    if price_col:
                        current_price = _safe_float(row[price_col])
    except Exception as e:
        print(f"[基本面] 获取股票名称失败 (symbol={symbol}): {e}")

    # ============================================================
    # 2. 获取核心财务指标（东方财富 datacenter 财务摘要）
    #    数据源：stock_financial_abstract
    #    该接口返回常用指标 + 每股指标 + 盈利能力指标等
    #    列结构：选项 | 指标 | 20260630 | 20260331 | 20251231 | ...
    # ============================================================
    abstract_df = None
    try:
        abstract_df = ak.stock_financial_abstract(symbol=symbol)
        if abstract_df is not None and not abstract_df.empty:
            # 找到最新报告期的列名（第3列，索引2）
            latest_col = abstract_df.columns[2] if len(abstract_df.columns) > 2 else None
            prev_col = abstract_df.columns[3] if len(abstract_df.columns) > 3 else None

            if latest_col:
                # 构建 指标名 -> 最新值 的映射
                indicator_col = _find_column(abstract_df, ["指标", "指标名称"])
                if indicator_col:
                    indicator_map = {}
                    for _, row in abstract_df.iterrows():
                        ind_name = _safe_str(row[indicator_col])
                        ind_val = _safe_float(row[latest_col])
                        if ind_name:
                            indicator_map[ind_name] = ind_val

                    # --- 盈利能力 ---
                    result["data"]["profitability"]["revenue"] = indicator_map.get("营业总收入")
                    result["data"]["profitability"]["net_profit"] = indicator_map.get("净利润")
                    result["data"]["profitability"]["gross_margin"] = indicator_map.get("毛利率")
                    result["data"]["profitability"]["net_margin"] = indicator_map.get("销售净利率")
                    result["data"]["profitability"]["roe"] = indicator_map.get("净资产收益率(ROE)") or indicator_map.get("加权净资产收益率")

                    # --- 财务健康 ---
                    result["data"]["health"]["debt_ratio"] = indicator_map.get("资产负债率")
                    result["data"]["health"]["operating_cashflow"] = indicator_map.get("经营现金流量净额")

                    # --- 估值（用 EPS 和每股净资产计算 PE/PB）---
                    eps = indicator_map.get("基本每股收益")
                    bvps = indicator_map.get("每股净资产") or indicator_map.get("股东权益合计(净资产)")
                    if current_price and current_price > 0:
                        if eps and eps != 0:
                            result["data"]["valuation"]["pe"] = round(current_price / eps, 2)
                        if bvps and bvps != 0:
                            result["data"]["valuation"]["pb"] = round(current_price / bvps, 2)

                    # --- 成长性（同比计算）---
                    # 用最新列和上一期列计算同比增长
                    if prev_col:
                        prev_map = {}
                        for _, row in abstract_df.iterrows():
                            ind_name = _safe_str(row[indicator_col])
                            ind_val = _safe_float(row[prev_col])
                            if ind_name:
                                prev_map[ind_name] = ind_val

                        # 尝试找去年同期（大约4个季度前）
                        # 列顺序：最新, 上一季, 上二季, 上三季, 上四季(去年同期)...
                        yoy_col_idx = min(6, len(abstract_df.columns) - 1)
                        yoy_col = abstract_df.columns[yoy_col_idx]
                        yoy_map = {}
                        for _, row in abstract_df.iterrows():
                            ind_name = _safe_str(row[indicator_col])
                            ind_val = _safe_float(row[yoy_col])
                            if ind_name:
                                yoy_map[ind_name] = ind_val

                        # 营收同比增长率
                        cur_rev = indicator_map.get("营业总收入")
                        yoy_rev = yoy_map.get("营业总收入")
                        result["data"]["growth"]["revenue_yoy"] = _calc_yoy(cur_rev, yoy_rev)

                        # 净利润同比增长率
                        cur_np = indicator_map.get("归母净利润") or indicator_map.get("净利润")
                        yoy_np = yoy_map.get("归母净利润") or yoy_map.get("净利润")
                        result["data"]["growth"]["net_profit_yoy"] = _calc_yoy(cur_np, yoy_np)

    except Exception as e:
        print(f"[基本面] 获取财务摘要失败 (symbol={symbol}): {type(e).__name__}: {e}")

    # ============================================================
    # 3. 兜底：如果财务摘要接口整体失败，用新浪财务报表补充
    #    数据源：stock_financial_report_sina（新浪财经）
    # ============================================================
    if abstract_df is None or abstract_df.empty:
        print(f"[基本面] 财务摘要为空，尝试新浪财务报表兜底 (symbol={symbol})")

        # --- 利润表兜底 ---
        try:
            profit_df = ak.stock_financial_report_sina(stock=symbol, symbol="利润表")
            if profit_df is not None and not profit_df.empty:
                row0 = profit_df.iloc[0]

                # 营业收入
                rev_col = _find_column(profit_df, ["营业总收入", "营业收入"])
                result["data"]["profitability"]["revenue"] = _safe_float(
                    row0.get(rev_col) if rev_col else None
                )

                # 净利润
                np_col = _find_column(profit_df, ["净利润", "归属于母公司所有者的净利润"])
                result["data"]["profitability"]["net_profit"] = _safe_float(
                    row0.get(np_col) if np_col else None
                )

                # 同比增长率（用最新一期和去年同期对比）
                if len(profit_df) >= 5:
                    row_yoy = profit_df.iloc[4]  # 大约去年同期
                    if rev_col:
                        result["data"]["growth"]["revenue_yoy"] = _calc_yoy(
                            _safe_float(row0.get(rev_col)),
                            _safe_float(row_yoy.get(rev_col))
                        )
                    if np_col:
                        result["data"]["growth"]["net_profit_yoy"] = _calc_yoy(
                            _safe_float(row0.get(np_col)),
                            _safe_float(row_yoy.get(np_col))
                        )
        except Exception as e:
            print(f"[基本面] 新浪利润表兜底失败: {e}")

        # --- 资产负债表兜底 ---
        try:
            balance_df = ak.stock_financial_report_sina(stock=symbol, symbol="资产负债表")
            if balance_df is not None and not balance_df.empty:
                row_bs = balance_df.iloc[0]
                ta_col = _find_column(balance_df, ["资产总计", "总资产"])
                tl_col = _find_column(balance_df, ["负债合计", "总负债"])
                if ta_col and tl_col:
                    ta = _safe_float(row_bs.get(ta_col))
                    tl = _safe_float(row_bs.get(tl_col))
                    if ta and tl and ta != 0:
                        result["data"]["health"]["debt_ratio"] = round(tl / ta * 100, 2)
        except Exception as e:
            print(f"[基本面] 新浪资产负债表兜底失败: {e}")

        # --- 现金流量表兜底 ---
        try:
            cashflow_df = ak.stock_financial_report_sina(stock=symbol, symbol="现金流量表")
            if cashflow_df is not None and not cashflow_df.empty:
                row_cf = cashflow_df.iloc[0]
                oc_col = _find_column(cashflow_df, [
                    "经营活动产生的现金流量净额",
                    "经营活动现金流量净额",
                    "经营现金净额",
                ])
                if oc_col:
                    result["data"]["health"]["operating_cashflow"] = _safe_float(
                        row_cf.get(oc_col)
                    )
        except Exception as e:
            print(f"[基本面] 新浪现金流量表兜底失败: {e}")

    return result


# ================================================================
# 调用示例
# ================================================================
if __name__ == "__main__":
    print("=" * 60)
    print("个股基本面分析示例：300750（宁德时代）")
    print("=" * 60)

    result = analyze_fundamental("300750")
    print(json.dumps(result, ensure_ascii=False, indent=2))

    print("\n" + "=" * 60)
    print("个股基本面分析示例：600519（贵州茅台）")
    print("=" * 60)

    result2 = analyze_fundamental("600519")
    print(json.dumps(result2, ensure_ascii=False, indent=2))
