"""
sentiment_analyzer.py - 金融新闻深度投研 Agent
================================================
升级自简单情感分析器，具备以下能力：
  1. 事件解析 —— 识别新闻主体、事件类型、是否需要行情数据
  2. 工具调用 —— 获取 A 股实时行情 / 历史 K 线（akshare）
  3. 多维推理 —— 基本面 / 资金面 / 情绪面 / 政策面
  4. 行情验证 —— 用真实行情数据校验新闻判断
  5. 自我反思 —— 检查矛盾、偏见、误判并修正结论
  6. 结构化输出 —— 完整投研简报
"""

import json
import os
import re
import time
from datetime import datetime, timedelta

# 加载 .env 文件中的环境变量（必须在 os.getenv 之前调用）
from dotenv import load_dotenv
load_dotenv()

import akshare as ak
from zhipuai import ZhipuAI


# ================================================================
# 第一部分：配置
# ================================================================
# 智谱 API 配置（从 .env 文件读取，不硬编码）
ZHIPU_API_KEY = os.getenv("ZHIPU_API_KEY", "")
ZHIPU_MODEL = "glm-4-flash"

# 各阶段 max_tokens 配置（不同阶段需要不同长度的输出）
TOKEN_LIMITS = {
    "parse": 800,       # 事件解析
    "plan": 800,        # 分析规划
    "reason": 2000,     # 多维推理
    "reflect": 1500,    # 自我反思
    "report": 3000,     # 最终报告
}


# ================================================================
# 第二部分：各阶段系统提示词
# ================================================================
# 事件解析提示词 —— 识别新闻主体和事件类型
PROMPT_PARSE_EVENT = """你是一位资深金融分析师。请解析以下财经新闻，识别事件要素。
请严格返回 JSON 格式：
{
    "summary": "一句话事件摘要（20字以内）",
    "entities": ["受影响的公司/行业/市场名称"],
    "event_type": "业绩|并购|监管|政策|产能|资金|技术突破|风险事件|其他",
    "need_market_data": true或false,
    "related_stock_hint": "可能受影响的股票代码提示（如无则填空字符串）"
}"""

# 分析规划提示词 —— 决定分析路径和工具调用
PROMPT_PLAN = """你是投研 Agent 的规划模块。根据事件解析结果，制定分析计划。
请严格返回 JSON 格式：
{
    "key_questions": ["需要回答的2-3个关键问题"],
    "tools_needed": ["realtime_quote"和/或"history_kline"],
    "dimensions": {
        "fundamental": "基本面分析要点提示",
        "capital": "资金面分析要点提示",
        "sentiment": "情绪面分析要点提示",
        "policy": "政策面分析要点提示"
    },
    "analysis_focus": "本次分析的核心关注点"
}"""

# 多维推理提示词 —— 从多个维度分析新闻影响
PROMPT_REASON = """你是投研 Agent 的多维推理引擎。请从多个维度深入分析这条财经新闻的影响。
{market_section}
请严格返回 JSON 格式：
{
    "fundamental": {{"impact": "利好/利空/中性", "score": 0.0~1.0, "detail": "分析内容"}},
    "capital": {{"impact": "利好/利空/中性", "score": 0.0~1.0, "detail": "分析内容"}},
    "sentiment": {{"impact": "利好/利空/中性", "score": 0.0~1.0, "detail": "分析内容"}},
    "policy": {{"impact": "利好/利空/中性", "score": 0.0~1.0, "detail": "分析内容"}},
    "risk_factors": ["风险因素1", "风险因素2"],
    "reasoning_chain": "推理链概述（100字以内）"
}"""

# 行情验证提示词 —— 用真实行情数据校验判断
PROMPT_VERIFY = """你是投研 Agent 的行情验证模块。请根据行情数据验证新闻分析的判断。
请严格返回 JSON 格式：
{
    "consistent": true或false,
    "signals": ["信号1", "信号2"],
    "detail": "验证分析详情（100字以内）",
    "warning": "警告信息（如利好兑现/情绪透支等，无则填空字符串）"
}"""

# 自我反思提示词 —— 检查分析中的问题和偏见
PROMPT_REFLECT = """你是投研 Agent 的自我反思模块。请审视初步分析结论，检查是否存在以下问题：
1. 结论与行情数据矛盾
2. 过度依赖新闻标题而忽略深层信息
3. 忽略利好兑现或估值透支的可能
4. 把短期情绪误判为长期趋势
5. 信息不足导致的误判风险

初步分析结论：
{initial_conclusion}

请严格返回 JSON 格式：
{
    "issues_found": ["发现的问题1", "发现的问题2"],
    "corrections": ["修正建议1", "修正建议2"],
    "confidence_adjustment": -0.2到0.2之间的浮点数（对置信度的调整值）,
    "final_sentiment": "利好/利空/中性（修正后的最终判断）",
    "reflection_detail": "反思总结（100字以内）"
}"""

# 最终报告生成提示词 —— 汇总所有分析生成投研简报
PROMPT_REPORT = """你是投研 Agent 的报告生成模块。请根据所有分析结果，生成一份完整的结构化投研简报。
请严格返回 JSON 格式：
{
    "event_summary": "事件摘要（30字以内）",
    "entities": ["影响主体"],
    "event_type": "事件类型",
    "impact_level": "高/中/低",
    "fundamental_analysis": "基本面分析（50字以内）",
    "capital_analysis": "资金面分析（50字以内）",
    "sentiment_analysis": "情绪面分析（50字以内）",
    "policy_analysis": "政策面分析（50字以内）",
    "market_verification": "行情验证结论（50字以内）",
    "risk_assessment": "风险评估（50字以内）",
    "confidence": 0.0到1.0,
    "final_sentiment": "利好/利空/中性",
    "short_term_view": "短期观点（50字以内）",
    "mid_term_view": "中期观点（50字以内）",
    "action_advice": "操作建议（50字以内）",
    "reasoning_chain": "核心推理链（100字以内）",
    "reflection_summary": "自我反思要点（100字以内）"
}"""


# ================================================================
# 第三部分：全局客户端单例
# ================================================================
_client = None


def _get_client() -> ZhipuAI:
    """获取或创建智谱 AI 客户端单例。未配置 Key 时抛出明确错误。"""
    global _client
    if _client is None:
        if not ZHIPU_API_KEY:
            raise RuntimeError(
                "\n" + "=" * 50
                + "\n  错误：未检测到智谱 API Key (ZHIPU_API_KEY)！"
                + "\n" + "=" * 50
                + "\n  请在项目根目录 .env 文件中配置："
                + "\n  ZHIPU_API_KEY=你的API密钥"
                + "\n  获取地址：https://open.bigmodel.cn/usercenter/apikeys"
                + "\n" + "=" * 50
            )
        _client = ZhipuAI(api_key=ZHIPU_API_KEY)
    return _client


# ================================================================
# 第四部分：通用工具函数
# ================================================================
def _call_llm(system_prompt: str, user_prompt: str,
              max_tokens: int = 1000, max_retries: int = 3) -> str:
    """
    调用智谱大模型的通用封装。

    Parameters
    ----------
    system_prompt : str
        系统提示词。
    user_prompt : str
        用户提示词。
    max_tokens : int
        最大输出 token 数。
    max_retries : int
        失败重试次数。

    Returns
    -------
    str
        模型返回的文本内容。调用全部失败时返回空字符串。
    """
    client = _get_client()
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=ZHIPU_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,
                max_tokens=max_tokens,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            last_error = e
            print(f"    [LLM 重试] 第 {attempt}/{max_retries} 次: {e}")
            if attempt < max_retries:
                time.sleep(2 ** attempt)

    print(f"    [LLM 警告] 全部重试失败: {last_error}")
    return ""


def _clean_json(content: str) -> str:
    """
    清洗大模型返回文本，去除 markdown 代码块标记。

    Parameters
    ----------
    content : str
        模型原始返回文本。

    Returns
    -------
    str
        清洗后的纯文本。
    """
    # 去除 ```json ... ``` 或 ``` ... ``` 包裹
    cleaned = re.sub(r"```\w*\s*", "", content).strip()
    return cleaned


def _extract_json_object(text: str) -> dict:
    """
    从文本中提取最外层 JSON 对象，支持嵌套大括号。

    Parameters
    ----------
    text : str
        可能包含 JSON 的文本。

    Returns
    -------
    dict
        解析后的字典。解析失败返回空字典。
    """
    cleaned = _clean_json(text)

    # 尝试直接解析整个文本
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # 找到第一个 { 的位置
    start = cleaned.find("{")
    if start == -1:
        return {}

    # 用括号计数法找到匹配的 }
    depth = 0
    for i in range(start, len(cleaned)):
        if cleaned[i] == "{":
            depth += 1
        elif cleaned[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(cleaned[start:i + 1])
                except json.JSONDecodeError:
                    break

    # 兜底：用简单正则提取
    match = re.search(r"\{[^{}]*\}", cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    return {}


def _extract_json_array(text: str) -> list:
    """
    从文本中提取最外层 JSON 数组，支持嵌套。

    Parameters
    ----------
    text : str
        可能包含 JSON 数组的文本。

    Returns
    -------
    list
        解析后的列表。解析失败返回空列表。
    """
    cleaned = _clean_json(text)

    try:
        result = json.loads(cleaned)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass

    start = cleaned.find("[")
    if start == -1:
        return []

    depth = 0
    for i in range(start, len(cleaned)):
        if cleaned[i] == "[":
            depth += 1
        elif cleaned[i] == "]":
            depth -= 1
            if depth == 0:
                try:
                    result = json.loads(cleaned[start:i + 1])
                    if isinstance(result, list):
                        return result
                except json.JSONDecodeError:
                    break
    return []


def _safe_float(value, default: float = 0.5) -> float:
    """安全地将值转为浮点数，失败时返回默认值。"""
    try:
        return max(0.0, min(1.0, float(value)))
    except (ValueError, TypeError):
        return default


def _safe_str(value, default: str = "") -> str:
    """安全地将值转为字符串，截断到 500 字符。"""
    return str(value)[:500] if value is not None else default


# ================================================================
# 第五部分：市场数据工具（基于 akshare）
# ================================================================
def get_realtime_quote(stock_code: str) -> dict:
    """
    获取 A 股个股实时行情快照。
    优先使用东方财富数据源，失败时自动回退到最近交易日历史数据。

    Parameters
    ----------
    stock_code : str
        6 位股票代码，如 "600519"。

    Returns
    -------
    dict
        包含代码、名称、现价、涨跌幅、成交量、成交额等信息。
        异常时返回带 error 字段的错误字典。
    """
    # ---- 方案一：东方财富实时行情 ----
    try:
        df = ak.stock_zh_a_spot_em()
        row = df[df["代码"] == stock_code]
        if not row.empty:
            r = row.iloc[0]
            return {
                "code": stock_code,
                "name": str(r.get("名称", "")),
                "price": float(r.get("最新价", 0)),
                "change_pct": float(r.get("涨跌幅", 0)),
                "volume": float(r.get("成交量", 0)),
                "amount": float(r.get("成交额", 0)),
                "high": float(r.get("最高", 0)),
                "low": float(r.get("最低", 0)),
                "open": float(r.get("今开", 0)),
                "prev_close": float(r.get("昨收", 0)),
                "source": "eastmoney",
            }
    except Exception as e:
        print(f"    [数据源切换] 东方财富不可用: {type(e).__name__}，尝试备选方案 ...")

    # ---- 方案二：新浪数据源获取最近交易日数据替代 ----
    try:
        prefix = "sh" if stock_code.startswith("6") else "sz"
        sina_symbol = f"{prefix}{stock_code}"
        df = ak.stock_zh_a_daily(symbol=sina_symbol, adjust="qfq")
        if df is not None and not df.empty:
            # 取最近两个交易日
            df = df.tail(2)
            r = df.iloc[-1]
            prev_close = float(df.iloc[-2].get("close", 0)) if len(df) >= 2 else float(r.get("open", 0))
            close = float(r.get("close", 0))
            change_pct = ((close - prev_close) / prev_close * 100) if prev_close > 0 else 0
            return {
                "code": stock_code,
                "name": stock_code,
                "price": close,
                "change_pct": round(change_pct, 2),
                "volume": float(r.get("volume", 0)),
                "amount": 0,
                "high": float(r.get("high", 0)),
                "low": float(r.get("low", 0)),
                "open": float(r.get("open", 0)),
                "prev_close": prev_close,
                "source": "sina_daily",
                "note": f"非实时，数据日期: {r.get('date', '未知')}",
            }
    except Exception as e2:
        return {"error": f"所有数据源均失败: {e2}"}


def get_history_kline(stock_code: str, days: int = 30) -> dict:
    """
    获取 A 股个股近 N 日历史 K 线数据。
    优先使用东方财富数据源，失败时自动回退到新浪数据源。

    Parameters
    ----------
    stock_code : str
        6 位股票代码。
    days : int
        查询天数，默认 30 天。

    Returns
    -------
    dict
        包含代码、天数、数据列表（每日 OHLCV）和统计摘要。
        异常时返回带 error 字段的错误字典。
    """
    start_date = (datetime.now() - timedelta(days=days + 15)).strftime("%Y%m%d")
    end_date = datetime.now().strftime("%Y%m%d")

    # ---- 方案一：东方财富历史 K 线 ----
    try:
        df = ak.stock_zh_a_hist(
            symbol=stock_code, period="daily",
            start_date=start_date, end_date=end_date, adjust="qfq",
        )
        if df is not None and not df.empty:
            return _build_kline_result(stock_code, df, days, "eastmoney")
    except Exception as e:
        print(f"    [数据源切换] 东方财富K线不可用: {type(e).__name__}，尝试新浪数据源 ...")

    # ---- 方案二：新浪数据源（通过股票代码推断交易所前缀） ----
    try:
        # 沪市股票以 6 开头加 sh，深市加 sz
        prefix = "sh" if stock_code.startswith("6") else "sz"
        sina_symbol = f"{prefix}{stock_code}"
        df = ak.stock_zh_a_daily(symbol=sina_symbol, adjust="qfq")
        if df is not None and not df.empty:
            # 新浪数据列名不同，统一列名
            df = df.rename(columns={
                "date": "日期", "open": "开盘", "close": "收盘",
                "high": "最高", "low": "最低", "volume": "成交量",
            })
            # 过滤日期范围
            df["日期"] = df["日期"].astype(str).str.replace("-", "")
            df = df[(df["日期"] >= start_date) & (df["日期"] <= end_date)]
            if not df.empty:
                return _build_kline_result(stock_code, df, days, "sina")
    except Exception as e2:
        return {"error": f"所有数据源均失败: {e2}"}

    return {"error": f"未获取到 {stock_code} 的历史数据"}


def _build_kline_result(stock_code: str, df, days: int, source: str) -> dict:
    """将 K 线 DataFrame 统一转为标准字典结构。"""
    df = df.tail(days)
    records = []
    for _, row in df.iterrows():
        records.append({
            "date": str(row.get("日期", "")),
            "open": float(row.get("开盘", 0)),
            "close": float(row.get("收盘", 0)),
            "high": float(row.get("最高", 0)),
            "low": float(row.get("最低", 0)),
            "volume": float(row.get("成交量", 0)),
            "change_pct": float(row.get("涨跌幅", 0)) if "涨跌幅" in row.index else 0,
        })

    closes = [r["close"] for r in records if r["close"] > 0]
    volumes = [r["volume"] for r in records]
    summary = {
        "avg_close": round(sum(closes) / len(closes), 2) if closes else 0,
        "max_close": max(closes) if closes else 0,
        "min_close": min(closes) if closes else 0,
        "avg_volume": round(sum(volumes) / len(volumes)) if volumes else 0,
        "total_days": len(records),
    }
    return {
        "code": stock_code,
        "days": len(records),
        "data": records,
        "summary": summary,
        "source": source,
    }


# ================================================================
# 第六部分：Agent 主类
# ================================================================
class InvestmentResearchAgent:
    """
    金融新闻深度投研 Agent。

    执行五阶段分析流水线：
      1. 事件解析 —— 识别新闻主体和事件类型
      2. 分析规划 —— 决定分析路径和工具调用
      3. 工具执行 —— 获取实时行情 / 历史 K 线
      4. 多维推理 —— 基本面 / 资金面 / 情绪面 / 政策面
      5. 行情验证 —— 用行情数据校验新闻判断（如有行情数据）
      6. 自我反思 —— 检查矛盾和偏见，修正结论
      7. 报告生成 —— 输出结构化投研简报
    """

    def analyze(self, news_text: str, stock_code: str = "") -> dict:
        """
        执行完整的投研分析流程。

        Parameters
        ----------
        news_text : str
            待分析的财经新闻文本。
        stock_code : str
            可选的股票代码（6 位数字），提供后将自动获取行情数据辅助分析。

        Returns
        -------
        dict
            完整分析结果，包含以下键：
            - event_parse: 事件解析结果
            - plan: 分析计划
            - market_data: 市场数据
            - reasoning: 多维推理结果
            - verification: 行情验证结果
            - reflection: 自我反思结果
            - report: 最终投研简报
        """
        print(f"\n  [Agent] 开始分析: {news_text[:50]}...")

        # ---- 阶段 1：事件解析 ----
        print("  [阶段 1/7] 事件解析 ...")
        event_parse = self._parse_event(news_text)

        # ---- 阶段 2：制定分析计划 ----
        print("  [阶段 2/7] 制定分析计划 ...")
        plan = self._plan_analysis(event_parse)

        # ---- 阶段 3：调用工具获取市场数据 ----
        print("  [阶段 3/7] 获取市场数据 ...")
        market_data = self._call_tools(stock_code, plan)

        # ---- 阶段 4：多维推理 ----
        print("  [阶段 4/7] 多维推理 ...")
        reasoning = self._multi_dimension_reason(news_text, plan, market_data)

        # ---- 阶段 5：行情验证（有行情数据时才执行） ----
        verification = {"consistent": None, "detail": "无行情数据，跳过验证"}
        if market_data and not market_data.get("error"):
            print("  [阶段 5/7] 行情验证 ...")
            verification = self._verify_with_market(news_text, reasoning, market_data)
        else:
            print("  [阶段 5/7] 行情验证（跳过，无行情数据）")

        # ---- 阶段 6：自我反思 ----
        print("  [阶段 6/7] 自我反思 ...")
        reflection = self._self_reflect(news_text, reasoning, verification, market_data)

        # ---- 阶段 7：生成投研简报 ----
        print("  [阶段 7/7] 生成投研简报 ...")
        report = self._generate_report(
            news_text, event_parse, plan, reasoning,
            verification, reflection, market_data
        )

        print("  [Agent] 分析完成!")

        return {
            "event_parse": event_parse,
            "plan": plan,
            "market_data": market_data,
            "reasoning": reasoning,
            "verification": verification,
            "reflection": reflection,
            "report": report,
        }

    # ---- 阶段 1：事件解析 ----
    def _parse_event(self, news_text: str) -> dict:
        """调用大模型解析新闻事件要素。"""
        user_prompt = f"请解析以下财经新闻的事件要素：\n\n{news_text[:800]}"
        content = _call_llm(PROMPT_PARSE_EVENT, user_prompt, TOKEN_LIMITS["parse"])

        if not content:
            return {"summary": news_text[:30], "entities": [], "event_type": "其他",
                    "need_market_data": False, "related_stock_hint": ""}

        result = _extract_json_object(content)
        # 确保必要字段存在
        result.setdefault("summary", news_text[:30])
        result.setdefault("entities", [])
        result.setdefault("event_type", "其他")
        result.setdefault("need_market_data", False)
        result.setdefault("related_stock_hint", "")
        return result

    # ---- 阶段 2：制定分析计划 ----
    def _plan_analysis(self, event_parse: dict) -> dict:
        """根据事件解析结果制定分析计划。"""
        event_json = json.dumps(event_parse, ensure_ascii=False, indent=2)
        user_prompt = f"根据以下事件解析结果，制定投研分析计划：\n\n{event_json}"
        content = _call_llm(PROMPT_PLAN, user_prompt, TOKEN_LIMITS["plan"])

        if not content:
            return {"key_questions": [], "tools_needed": [],
                    "dimensions": {}, "analysis_focus": ""}

        result = _extract_json_object(content)
        result.setdefault("key_questions", [])
        result.setdefault("tools_needed", [])
        result.setdefault("dimensions", {})
        result.setdefault("analysis_focus", "")
        return result

    # ---- 阶段 3：工具调用 ----
    def _call_tools(self, stock_code: str, plan: dict) -> dict:
        """
        根据分析计划调用市场数据工具。

        如果有股票代码，获取实时行情和历史 K 线。
        工具调用失败不中断主流程，返回错误信息继续分析。
        """
        if not stock_code:
            print("    未提供股票代码，跳过行情数据获取")
            return {}

        tools_needed = plan.get("tools_needed", [])
        market_data = {}

        # 获取实时行情（有计划要求或默认在有股票代码时获取）
        if "realtime_quote" in tools_needed or not tools_needed:
            print(f"    调用工具: 获取 {stock_code} 实时行情 ...")
            market_data["realtime"] = get_realtime_quote(stock_code)
            if "error" in market_data["realtime"]:
                print(f"    [工具警告] {market_data['realtime']['error']}")
            else:
                print(f"    实时行情: {market_data['realtime'].get('name', '')} "
                      f"现价 {market_data['realtime'].get('price', 0)} "
                      f"涨跌幅 {market_data['realtime'].get('change_pct', 0)}%")

        # 获取历史 K 线
        if "history_kline" in tools_needed or not tools_needed:
            print(f"    调用工具: 获取 {stock_code} 近30日K线 ...")
            market_data["history"] = get_history_kline(stock_code, days=30)
            if "error" in market_data["history"]:
                print(f"    [工具警告] {market_data['history']['error']}")
            else:
                s = market_data["history"].get("summary", {})
                print(f"    K线摘要: {s.get('total_days', 0)}天 "
                      f"均价 {s.get('avg_close', 0)} "
                      f"区间 {s.get('min_close', 0)}~{s.get('max_close', 0)}")

        return market_data

    # ---- 阶段 4：多维推理 ----
    def _multi_dimension_reason(self, news_text: str, plan: dict,
                                market_data: dict) -> dict:
        """从基本面、资金面、情绪面、政策面四个维度推理分析。"""
        # 如果有行情数据，拼入提示词供模型参考
        market_section = ""
        if market_data:
            md_summary = json.dumps(market_data, ensure_ascii=False, default=str)
            # 截断过长的行情数据
            if len(md_summary) > 1500:
                md_summary = md_summary[:1500] + "...(数据截断)"
            market_section = f"\n参考行情数据：\n{md_summary}\n"

        plan_json = json.dumps(plan, ensure_ascii=False, indent=2)
        user_prompt = (
            f"新闻内容：\n{news_text[:800]}\n\n"
            f"分析计划：\n{plan_json}"
            f"{market_section}"
        )
        content = _call_llm(PROMPT_REASON, user_prompt, TOKEN_LIMITS["reason"])

        if not content:
            empty = {"impact": "中性", "score": 0.5, "detail": "分析失败"}
            return {
                "fundamental": dict(empty), "capital": dict(empty),
                "sentiment": dict(empty), "policy": dict(empty),
                "risk_factors": [], "reasoning_chain": "分析失败",
            }

        result = _extract_json_object(content)
        # 确保四个维度都存在
        for dim in ["fundamental", "capital", "sentiment", "policy"]:
            if dim not in result:
                result[dim] = {"impact": "中性", "score": 0.5, "detail": "未分析"}
        result.setdefault("risk_factors", [])
        result.setdefault("reasoning_chain", "")
        return result

    # ---- 阶段 5：行情验证 ----
    def _verify_with_market(self, news_text: str, reasoning: dict,
                            market_data: dict) -> dict:
        """用真实行情数据验证新闻分析判断。"""
        # 精简行情数据用于提示词
        md_brief = {}
        if "realtime" in market_data and "error" not in market_data["realtime"]:
            rt = market_data["realtime"]
            md_brief["realtime"] = {
                "price": rt.get("price"), "change_pct": rt.get("change_pct"),
                "volume": rt.get("volume"),
            }
        if "history" in market_data and "error" not in market_data["history"]:
            h = market_data["history"]
            md_brief["history_summary"] = h.get("summary", {})
            # 取最近 5 天的走势概要
            recent = h.get("data", [])[-5:]
            md_brief["recent_5d"] = [
                {"date": d["date"], "close": d["close"], "change_pct": d["change_pct"]}
                for d in recent
            ]

        md_json = json.dumps(md_brief, ensure_ascii=False, default=str)
        reasoning_brief = {
            "fundamental": reasoning.get("fundamental", {}).get("impact", ""),
            "capital": reasoning.get("capital", {}).get("impact", ""),
            "sentiment": reasoning.get("sentiment", {}).get("impact", ""),
            "policy": reasoning.get("policy", {}).get("impact", ""),
        }
        r_json = json.dumps(reasoning_brief, ensure_ascii=False)

        user_prompt = (
            f"新闻：{news_text[:500]}\n\n"
            f"分析判断：{r_json}\n\n"
            f"行情数据：{md_json}"
        )
        content = _call_llm(PROMPT_VERIFY, user_prompt, TOKEN_LIMITS["reason"])

        if not content:
            return {"consistent": None, "signals": [],
                    "detail": "验证分析失败", "warning": ""}

        result = _extract_json_object(content)
        result.setdefault("consistent", None)
        result.setdefault("signals", [])
        result.setdefault("detail", "")
        result.setdefault("warning", "")
        return result

    # ---- 阶段 6：自我反思 ----
    def _self_reflect(self, news_text: str, reasoning: dict,
                      verification: dict, market_data: dict) -> dict:
        """审视初步分析结论，检查矛盾和偏见，必要时修正。"""
        # 构造初步结论摘要
        initial = {
            "reasoning": {
                "fundamental": reasoning.get("fundamental", {}).get("impact", ""),
                "capital": reasoning.get("capital", {}).get("impact", ""),
                "sentiment": reasoning.get("sentiment", {}).get("impact", ""),
                "policy": reasoning.get("policy", {}).get("impact", ""),
                "reasoning_chain": reasoning.get("reasoning_chain", ""),
            },
            "verification": {
                "consistent": verification.get("consistent"),
                "detail": verification.get("detail", ""),
                "warning": verification.get("warning", ""),
            },
        }
        initial_str = json.dumps(initial, ensure_ascii=False, indent=2)

        user_prompt = (
            f"新闻：{news_text[:500]}\n\n"
            f"初步分析结论：\n{initial_str}"
        )
        content = _call_llm(PROMPT_REFLECT, user_prompt, TOKEN_LIMITS["reflect"])

        if not content:
            return {"issues_found": [], "corrections": [],
                    "confidence_adjustment": 0.0,
                    "final_sentiment": "中性",
                    "reflection_detail": "反思环节未返回结果"}

        result = _extract_json_object(content)
        result.setdefault("issues_found", [])
        result.setdefault("corrections", [])
        result["confidence_adjustment"] = _safe_float(
            result.get("confidence_adjustment", 0), 0.0)
        # 限制调整幅度在 -0.2 ~ 0.2
        result["confidence_adjustment"] = max(-0.2, min(0.2, result["confidence_adjustment"]))
        result.setdefault("final_sentiment", "中性")
        if result["final_sentiment"] not in ("利好", "利空", "中性"):
            result["final_sentiment"] = "中性"
        result.setdefault("reflection_detail", "")
        return result

    # ---- 阶段 7：生成投研简报 ----
    def _generate_report(self, news_text, event_parse, plan, reasoning,
                         verification, reflection, market_data) -> dict:
        """汇总所有分析结果，生成结构化投研简报。"""
        # 构造各阶段摘要供报告生成参考
        all_context = {
            "event_parse": event_parse,
            "plan": plan,
            "reasoning": {
                "fundamental": reasoning.get("fundamental", {}),
                "capital": reasoning.get("capital", {}),
                "sentiment": reasoning.get("sentiment", {}),
                "policy": reasoning.get("policy", {}),
                "risk_factors": reasoning.get("risk_factors", []),
                "reasoning_chain": reasoning.get("reasoning_chain", ""),
            },
            "verification": verification,
            "reflection": reflection,
        }
        context_json = json.dumps(all_context, ensure_ascii=False, default=str)
        # 截断过长上下文
        if len(context_json) > 3000:
            context_json = context_json[:3000] + "...(截断)"

        user_prompt = (
            f"原始新闻：{news_text[:500]}\n\n"
            f"各阶段分析结果：\n{context_json}"
        )
        content = _call_llm(PROMPT_REPORT, user_prompt, TOKEN_LIMITS["report"])

        if not content:
            # 降级：用已有数据拼装最简报告
            return self._fallback_report(event_parse, reasoning, reflection)

        result = _extract_json_object(content)
        # 填充缺失字段
        result.setdefault("event_summary", event_parse.get("summary", ""))
        result.setdefault("entities", event_parse.get("entities", []))
        result.setdefault("event_type", event_parse.get("event_type", ""))
        result.setdefault("impact_level", "中")
        result.setdefault("fundamental_analysis", "")
        result.setdefault("capital_analysis", "")
        result.setdefault("sentiment_analysis", "")
        result.setdefault("policy_analysis", "")
        result.setdefault("market_verification", verification.get("detail", ""))
        result.setdefault("risk_assessment", "")
        result["confidence"] = _safe_float(result.get("confidence", 0.5))
        result.setdefault("final_sentiment", reflection.get("final_sentiment", "中性"))
        result.setdefault("short_term_view", "")
        result.setdefault("mid_term_view", "")
        result.setdefault("action_advice", "")
        result.setdefault("reasoning_chain", reasoning.get("reasoning_chain", ""))
        result.setdefault("reflection_summary", reflection.get("reflection_detail", ""))
        return result

    def _fallback_report(self, event_parse: dict, reasoning: dict,
                         reflection: dict) -> dict:
        """当报告生成失败时，用已有数据拼装降级报告。"""
        return {
            "event_summary": event_parse.get("summary", ""),
            "entities": event_parse.get("entities", []),
            "event_type": event_parse.get("event_type", ""),
            "impact_level": "中",
            "fundamental_analysis": reasoning.get("fundamental", {}).get("detail", ""),
            "capital_analysis": reasoning.get("capital", {}).get("detail", ""),
            "sentiment_analysis": reasoning.get("sentiment", {}).get("detail", ""),
            "policy_analysis": reasoning.get("policy", {}).get("detail", ""),
            "market_verification": "无行情数据",
            "risk_assessment": "、".join(reasoning.get("risk_factors", ["未知风险"])),
            "confidence": 0.3,
            "final_sentiment": reflection.get("final_sentiment", "中性"),
            "short_term_view": "报告生成失败，仅供参考",
            "mid_term_view": "报告生成失败，仅供参考",
            "action_advice": "信息不足，建议观望",
            "reasoning_chain": reasoning.get("reasoning_chain", ""),
            "reflection_summary": reflection.get("reflection_detail", ""),
        }


# ================================================================
# 第七部分：便捷函数（向后兼容）
# ================================================================
# 全局 Agent 实例
_agent = InvestmentResearchAgent()


def analyze_sentiment(text: str, stock_code: str = "") -> dict:
    """
    分析新闻文本的情感倾向（统一入口，向后兼容）。

    调用投研 Agent 执行完整的多阶段分析流程。
    返回完整的分析结果（含 plan / market_data / reasoning / reflection / report）。

    Parameters
    ----------
    text : str
        待分析的新闻文本。
    stock_code : str
        可选的股票代码，提供后将获取行情数据辅助分析。

    Returns
    -------
    dict
        完整分析结果。

    Raises
    ------
    RuntimeError
        当 ZHIPU_API_KEY 未配置时抛出。
    """
    if not ZHIPU_API_KEY:
        raise RuntimeError(
            "\n" + "=" * 50
            + "\n  错误：未检测到智谱 API Key (ZHIPU_API_KEY)！"
            + "\n" + "=" * 50
            + "\n  请在 .env 文件中配置：ZHIPU_API_KEY=你的API密钥"
            + "\n  获取地址：https://open.bigmodel.cn/usercenter/apikeys"
            + "\n" + "=" * 50
        )
    return _agent.analyze(text, stock_code=stock_code)


# ================================================================
# 第八部分：结果打印工具
# ================================================================
def print_report(result: dict):
    """
    格式化打印投研 Agent 的完整分析结果。

    Parameters
    ----------
    result : dict
        analyze_sentiment() 返回的完整分析结果。
    """
    report = result.get("report", {})
    reflection = result.get("reflection", {})
    event_parse = result.get("event_parse", {})

    # 打印分隔线
    sep = "=" * 60

    print(f"\n{sep}")
    print("  投研简报")
    print(sep)

    # 基本信息
    tag_map = {"利好": "[+]", "利空": "[-]", "中性": "[=]"}
    sentiment = report.get("final_sentiment", "中性")
    tag = tag_map.get(sentiment, "[=]")
    confidence = report.get("confidence", 0)

    print(f"  {tag} 最终判断：{sentiment}  |  置信度：{confidence:.0%}")
    print(f"  影响级别：{report.get('impact_level', '中')}")
    print(f"  事件类型：{report.get('event_type', event_parse.get('event_type', ''))}")
    print(f"  影响主体：{'、'.join(report.get('entities', event_parse.get('entities', [])))}")

    print(f"\n  -- 事件摘要 --")
    print(f"  {report.get('event_summary', '')}")

    print(f"\n  -- 多维分析 --")
    print(f"  [基本面] {report.get('fundamental_analysis', '')}")
    print(f"  [资金面] {report.get('capital_analysis', '')}")
    print(f"  [情绪面] {report.get('sentiment_analysis', '')}")
    print(f"  [政策面] {report.get('policy_analysis', '')}")

    print(f"\n  -- 行情验证 --")
    print(f"  {report.get('market_verification', '')}")

    print(f"\n  -- 风险评估 --")
    print(f"  {report.get('risk_assessment', '')}")

    print(f"\n  -- 观点与建议 --")
    print(f"  [短期] {report.get('short_term_view', '')}")
    print(f"  [中期] {report.get('mid_term_view', '')}")
    print(f"  [操作] {report.get('action_advice', '')}")

    print(f"\n  -- 推理链 --")
    print(f"  {report.get('reasoning_chain', '')}")

    print(f"\n  -- 自我反思 --")
    issues = reflection.get("issues_found", [])
    if issues:
        for issue in issues:
            print(f"  * {issue}")
    corrections = reflection.get("corrections", [])
    if corrections:
        print(f"  修正：")
        for c in corrections:
            print(f"    -> {c}")
    print(f"  {reflection.get('reflection_detail', '')}")

    print(f"\n{sep}")


# ================================================================
# 第九部分：主程序演示
# ================================================================
if __name__ == "__main__":
    print("=" * 60)
    print("  金融新闻深度投研 Agent 演示")
    print("=" * 60)

    # 测试用例 1：纯新闻分析（无股票代码）
    news1 = "央行宣布降准0.5个百分点，释放长期资金约1万亿元，利好股市走强。"
    print(f"\n{'='*60}")
    print(f"  测试 1：纯新闻分析（无股票代码）")
    print(f"  新闻：{news1}")
    print(f"{'='*60}")

    try:
        result1 = analyze_sentiment(news1)
        print_report(result1)
    except RuntimeError as e:
        print(e)

    # 测试用例 2：新闻 + 股票代码（触发行情数据获取）
    news2 = "贵州茅台发布2025年年报，营收同比增长18%，净利润超预期，市场反应积极。"
    print(f"\n\n{'='*60}")
    print(f"  测试 2：新闻 + 股票代码（触发行情工具调用）")
    print(f"  新闻：{news2}")
    print(f"  股票：600519（贵州茅台）")
    print(f"{'='*60}")

    try:
        result2 = analyze_sentiment(news2, stock_code="600519")
        print_report(result2)
    except RuntimeError as e:
        print(e)
