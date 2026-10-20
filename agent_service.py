
"""
agent_service.py - 金融投研 AI Agent 核心服务编排层
====================================================
职责：连接 FastAPI 接口层与现有分析模块，负责：
  - 定义 API 数据模型（Pydantic BaseModel）
  - 意图识别与任务拆解
  - 编排调用流程（新闻获取 -> 行情获取 -> 深度投研分析）
  - 记录思考日志（thinking_log）供前端白盒化展示
  - 封装标准化 JSON 响应
  - 利用 sentiment_analyzer 七阶段分析结果中的完整信息
  - 强制大模型输出结构化 JSON（含 System Prompt 约束 + Markdown 清洗）
  - 预留长期记忆读写接口（retrieve_memory / save_to_memory）

升级说明（对比上一版新增）：
  - 新增 EventParseDetail / PlanDetail / VerificationDetail / ReflectionDetail 模型
  - 新增 MultiDimensionScore 模型（多维推理评分）
  - AnalyzeResponse 新增 event_detail / plan_detail / verification_detail / reflection_detail / multi_dim_scores / impact_level / action_advice 字段
  - _build_evidence_from_agent 新增个股行情证据、行情验证证据、反思修正证据
  - run_agent 新增置信度调整逻辑（应用 reflection 的 confidence_adjustment）
  - run_agent 新增投研简报完整字段提取（impact_level, action_advice, short_term_view, mid_term_view）
"""

import asyncio
import json
import re
import time
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

# ================================================================
# 导入现有模块
# ================================================================
from sentiment_analyzer import InvestmentResearchAgent, analyze_sentiment, _call_llm
from news_fetcher import fetch_latest_news
from data_fetcher import fetch_hs300_history

# ================================================================
# 日志前缀
# ================================================================
_LOG_PREFIX = "【Agent服务】"

# ================================================================
# 全局 Agent 实例（复用，避免重复创建）
# ================================================================
_agent = InvestmentResearchAgent()


# ================================================================
# 强制 JSON 输出的 System Prompt
# ================================================================
JSON_SYSTEM_PROMPT = (
    "你是一个金融分析专家。请只输出纯 JSON 格式，不要包含 markdown 标记（如 ```json）。\n"
    "JSON 结构必须包含：core_conclusion (string), summary_points (list of {title, content}), "
    "risks (list of {level, description}), evidence (list of strings)。\n"
    "如果数据不足，对应字段填 '暂无数据'。"
)


# ================================================================
# 长期记忆接口（预留占位）
# ================================================================
def retrieve_memory(user_query: str) -> str:
    """从长期记忆中检索与用户问题相关的历史分析结果（占位函数）。

    当前为预留接口，返回空字符串。
    未来接入向量数据库后，此函数将返回最相似的历史分析摘要。

    Args:
        user_query: 用户问题文本。

    Returns:
        相关历史分析文本（当前始终返回空字符串）。
    """
    print(f"{_LOG_PREFIX}[记忆检索] query='{user_query[:50]}...' -> 暂无历史记忆（预留接口）")
    return ""


def save_to_memory(user_query: str, analysis_result: dict) -> None:
    """将分析结果保存到长期记忆中（占位函数）。

    当前为预留接口，仅打印日志，不做实际存储。
    未来接入向量数据库后，此函数将把分析结果持久化。

    Args:
        user_query: 用户问题文本。
        analysis_result: 分析结果字典，包含 conclusion / confidence / risks 等。
    """
    print(f"{_LOG_PREFIX}[记忆存储] query='{user_query[:50]}...' -> "
          f"conclusion='{analysis_result.get('conclusion', '')[:50]}...' "
          f"（预留接口，未实际存储）")


# ================================================================
# JSON 解析辅助函数
# ================================================================
def _strip_markdown_json(text: str) -> str:
    """去除 LLM 返回文本中的 Markdown 代码块标记。

    大模型有时会在 JSON 前后添加 ```json ... ``` 标记，
    此函数自动清洗，确保 json.loads 能正常解析。

    Args:
        text: LLM 返回的原始文本。

    Returns:
        清洗后的纯 JSON 字符串。
    """
    text = text.strip()
    # 去除 ```json ... ``` 或 ``` ... ``` 包裹
    pattern = r"```(?:json)?\s*\n?(.*?)\n?\s*```"
    match = re.search(pattern, text, re.DOTALL)
    if match:
        text = match.group(1).strip()
    return text


def _parse_json_output(raw_text: str) -> dict:
    """解析大模型输出的 JSON 文本，包含容错处理。

    解析策略：
      1. 先尝试直接 json.loads
      2. 失败则清洗 Markdown 标记后重试
      3. 再失败则用正则提取第一个 {...} 块重试
      4. 全部失败则返回默认结构

    Args:
        raw_text: LLM 返回的原始文本。

    Returns:
        解析后的字典，包含 core_conclusion / summary_points / risks / evidence。
    """
    # 策略一：直接解析
    try:
        return json.loads(raw_text)
    except (json.JSONDecodeError, TypeError):
        pass

    # 策略二：清洗 Markdown 标记后解析
    cleaned = _strip_markdown_json(raw_text)
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        pass

    # 策略三：正则提取第一个 JSON 对象
    brace_match = re.search(r"\{[\s\S]*\}", cleaned)
    if brace_match:
        try:
            return json.loads(brace_match.group(0))
        except (json.JSONDecodeError, TypeError):
            pass

    # 策略四：全部失败，返回默认结构
    print(f"{_LOG_PREFIX}[JSON解析] 所有策略均失败，返回默认结构")
    return {
        "core_conclusion": "暂无数据",
        "summary_points": [],
        "risks": [],
        "evidence": [],
    }


def _build_json_evidence(json_data: dict) -> list:
    """从 JSON 结构化输出中构建证据列表。

    Args:
        json_data: 解析后的 JSON 字典。

    Returns:
        Evidence 列表。
    """
    evidence_list: List[Evidence] = []

    # 从 summary_points 构建证据
    for point in json_data.get("summary_points", []):
        if isinstance(point, dict):
            title = point.get("title", "")
            content = point.get("content", "")
            if title or content:
                evidence_list.append(Evidence(
                    type="分析摘要",
                    content=f"{title}: {content}" if title else content,
                    source="JSON结构化输出",
                    time="",
                ))

    # 从 evidence 字段构建证据
    for item in json_data.get("evidence", []):
        if isinstance(item, str) and item:
            evidence_list.append(Evidence(
                type="参考来源",
                content=item,
                source="JSON结构化输出",
                time="",
            ))

    return evidence_list


# ================================================================
# 第一部分：Pydantic 数据模型定义
# ================================================================
class AnalyzeRequest(BaseModel):
    """分析请求模型。

    Attributes:
        query: 用户问题文本。
        stock_code: 6 位股票代码（可选）。
        time_range: 时间范围，默认 "6m"（6个月）。
    """

    query: str = Field(..., description="用户问题，如 '分析贵州茅台近期走势'")
    stock_code: Optional[str] = Field(None, description="6 位股票代码，如 '600519'")
    time_range: Optional[str] = Field("6m", description="时间范围: 1m/3m/6m/1y")


class Evidence(BaseModel):
    """证据数据模型。

    Attributes:
        type: 证据类型，如 "行情"/"新闻"/"财报"/"政策"。
        content: 数据摘要。
        source: 数据来源。
        time: 数据时间戳。
    """

    type: str = Field(..., description="证据类型: 行情/新闻/财报/政策")
    content: str = Field(..., description="数据摘要内容")
    source: str = Field(..., description="数据来源名称")
    time: str = Field(..., description="数据时间戳")


class ThinkingStep(BaseModel):
    """思考日志模型（白盒化展示）。

    Attributes:
        step: 步骤名称。
        status: 执行状态: "running"/"success"/"failed"。
        message: 步骤描述信息。
        duration_ms: 耗时毫秒数（可选）。
    """

    step: str = Field(..., description="步骤名称")
    status: str = Field(..., description="状态: running/success/failed")
    message: str = Field(..., description="步骤描述")
    duration_ms: Optional[int] = Field(None, description="耗时（毫秒）")


# ---- 新增：事件解析详情模型 ----
class EventParseDetail(BaseModel):
    """事件解析详情（来自 sentiment_analyzer 的 event_parse 结果）。

    Attributes:
        summary: 一句话事件摘要。
        entities: 受影响的公司/行业/市场名称。
        event_type: 事件类型（业绩/并购/监管/政策/产能/资金/技术突破/风险事件/其他）。
        need_market_data: 是否需要行情数据。
        related_stock_hint: 可能受影响的股票代码提示。
    """

    summary: str = Field("", description="事件摘要")
    entities: List[str] = Field(default_factory=list, description="影响主体列表")
    event_type: str = Field("", description="事件类型")
    need_market_data: bool = Field(False, description="是否需要行情数据")
    related_stock_hint: str = Field("", description="相关股票代码提示")


# ---- 新增：分析规划详情模型 ----
class PlanDetail(BaseModel):
    """分析规划详情（来自 sentiment_analyzer 的 plan 结果）。

    Attributes:
        key_questions: 需要回答的关键问题。
        tools_needed: 需要的工具列表。
        dimensions: 各维度分析要点提示。
        analysis_focus: 本次分析的核心关注点。
    """

    key_questions: List[str] = Field(default_factory=list, description="关键问题列表")
    tools_needed: List[str] = Field(default_factory=list, description="需要的工具列表")
    dimensions: Dict[str, str] = Field(default_factory=dict, description="各维度分析要点")
    analysis_focus: str = Field("", description="分析核心关注点")


# ---- 新增：多维推理评分模型 ----
class DimensionScore(BaseModel):
    """单个维度的推理评分。

    Attributes:
        impact: 影响方向（利好/利空/中性）。
        score: 评分（0.0~1.0）。
        detail: 分析内容。
    """

    impact: str = Field("", description="影响方向: 利好/利空/中性")
    score: float = Field(0.0, ge=0.0, le=1.0, description="评分 0~1")
    detail: str = Field("", description="分析详情")


class MultiDimensionScores(BaseModel):
    """多维推理评分汇总。

    Attributes:
        fundamental: 基本面评分。
        capital: 资金面评分。
        sentiment: 情绪面评分。
        policy: 政策面评分。
        risk_factors: 风险因素列表。
        reasoning_chain: 推理链概述。
    """

    fundamental: DimensionScore = Field(default_factory=DimensionScore, description="基本面")
    capital: DimensionScore = Field(default_factory=DimensionScore, description="资金面")
    sentiment: DimensionScore = Field(default_factory=DimensionScore, description="情绪面")
    policy: DimensionScore = Field(default_factory=DimensionScore, description="政策面")
    risk_factors: List[str] = Field(default_factory=list, description="风险因素")
    reasoning_chain: str = Field("", description="推理链")


# ---- 新增：行情验证详情模型 ----
class VerificationDetail(BaseModel):
    """行情验证详情（来自 sentiment_analyzer 的 verification 结果）。

    Attributes:
        consistent: 分析判断与行情数据是否一致。
        signals: 验证信号列表。
        detail: 验证分析详情。
        warning: 警告信息（如利好兑现/情绪透支等）。
    """

    consistent: Optional[bool] = Field(None, description="是否与行情一致(None=未验证)")
    signals: List[str] = Field(default_factory=list, description="验证信号列表")
    detail: str = Field("", description="验证详情")
    warning: str = Field("", description="警告信息")


# ---- 新增：自我反思详情模型 ----
class ReflectionDetail(BaseModel):
    """自我反思详情（来自 sentiment_analyzer 的 reflection 结果）。

    Attributes:
        issues_found: 发现的问题列表。
        corrections: 修正建议列表。
        confidence_adjustment: 置信度调整值（-0.2~0.2）。
        final_sentiment: 修正后的最终判断。
        reflection_detail: 反思总结。
    """

    issues_found: List[str] = Field(default_factory=list, description="发现的问题")
    corrections: List[str] = Field(default_factory=list, description="修正建议")
    confidence_adjustment: float = Field(0.0, description="置信度调整值(-0.2~0.2)")
    final_sentiment: str = Field("中性", description="修正后最终判断")
    reflection_detail: str = Field("", description="反思总结")


class AnalyzeResponse(BaseModel):
    """分析响应模型。

    Attributes:
        query: 原始用户问题。
        sub_tasks: 拆解的子任务列表。
        data_sources: 使用的数据源列表。
        reasoning_steps: 推理步骤摘要。
        conclusion: 最终结论。
        confidence: 置信度（0~1）。
        risks: 风险提示列表。
        evidence: 证据列表。
        thinking_log: 白盒化思考过程。
        event_detail: 事件解析详情（新增）。
        plan_detail: 分析规划详情（新增）。
        multi_dim_scores: 多维推理评分（新增）。
        verification_detail: 行情验证详情（新增）。
        reflection_detail: 自我反思详情（新增）。
        impact_level: 影响级别（高/中/低）（新增）。
        action_advice: 操作建议（新增）。
        disclaimer: 免责声明。
    """

    query: str = Field(..., description="原始用户问题")
    sub_tasks: List[str] = Field(default_factory=list, description="拆解的子任务")
    data_sources: List[str] = Field(default_factory=list, description="使用的数据源")
    reasoning_steps: List[str] = Field(default_factory=list, description="推理步骤摘要")
    conclusion: str = Field(..., description="最终结论")
    confidence: float = Field(0.0, ge=0.0, le=1.0, description="置信度 0~1")
    risks: List[str] = Field(default_factory=list, description="风险提示")
    evidence: List[Evidence] = Field(default_factory=list, description="证据列表")
    thinking_log: List[ThinkingStep] = Field(default_factory=list, description="思考日志")
    # ---- 新增字段 ----
    event_detail: EventParseDetail = Field(default_factory=EventParseDetail, description="事件解析详情")
    plan_detail: PlanDetail = Field(default_factory=PlanDetail, description="分析规划详情")
    multi_dim_scores: MultiDimensionScores = Field(default_factory=MultiDimensionScores, description="多维推理评分")
    verification_detail: VerificationDetail = Field(default_factory=VerificationDetail, description="行情验证详情")
    reflection_detail: ReflectionDetail = Field(default_factory=ReflectionDetail, description="自我反思详情")
    impact_level: str = Field("", description="影响级别: 高/中/低")
    action_advice: str = Field("", description="操作建议")
    disclaimer: str = Field("本内容仅供参考，不构成投资建议", description="免责声明")


# ================================================================
# 第二部分：辅助函数
# ================================================================
def _make_step(
    step_name: str,
    status: str,
    message: str,
    duration_ms: Optional[int] = None,
) -> ThinkingStep:
    """工厂函数：快速创建 ThinkingStep 对象。

    Args:
        step_name: 步骤名称。
        status: 执行状态 ("running"/"success"/"failed")。
        message: 步骤描述信息。
        duration_ms: 耗时毫秒数（可选）。

    Returns:
        构建好的 ThinkingStep 实例。
    """
    return ThinkingStep(
        step=step_name,
        status=status,
        message=message,
        duration_ms=duration_ms,
    )


def _extract_stock_code(text: str) -> Optional[str]:
    """从文本中用正则提取 6 位股票代码。

    匹配规则：6 位连续数字，且以 0/3/6 开头（A 股代码特征）。

    Args:
        text: 待提取的文本。

    Returns:
        提取到的 6 位股票代码，未找到返回 None。

    Examples:
        >>> _extract_stock_code("分析贵州茅台600519的走势")
        '600519'
        >>> _extract_stock_code("今天大盘怎么样")
        None
    """
    # 匹配 6 位数字，以 0/3/6 开头
    match = re.search(r"(?<!\d)([036]\d{5})(?!\d)", text)
    if match:
        return match.group(1)
    return None


def _build_evidence_from_agent(result: dict) -> List[Evidence]:
    """从 sentiment_analyzer 的分析结果中提取证据列表。

    从七阶段分析结果的各维度中提取结构化证据。
    新增：个股行情证据、行情验证证据、反思修正证据。

    Args:
        result: InvestmentResearchAgent.analyze() 返回的完整分析结果。

    Returns:
        提取到的 Evidence 列表。
    """
    evidence_list: List[Evidence] = []
    report = result.get("report", {})
    reasoning = result.get("reasoning", {})
    event_parse = result.get("event_parse", {})
    market_data = result.get("market_data", {})
    verification = result.get("verification", {})
    reflection = result.get("reflection", {})

    # 从事件解析提取事件证据
    summary = event_parse.get("summary", "")
    if summary:
        entities = event_parse.get("entities", [])
        entity_str = "、".join(entities) if entities else ""
        evidence_list.append(Evidence(
            type="新闻",
            content=f"{summary}" + (f"  影响主体: {entity_str}" if entity_str else ""),
            source="事件解析",
            time="",
        ))

    # 从多维推理提取各维度证据（含评分）
    dim_map = {
        "fundamental": "基本面",
        "capital": "资金面",
        "sentiment": "情绪面",
        "policy": "政策面",
    }
    for dim_key, dim_label in dim_map.items():
        dim_data = reasoning.get(dim_key, {})
        detail = dim_data.get("detail", "")
        impact = dim_data.get("impact", "")
        score = dim_data.get("score", "")
        if detail:
            score_str = f" [评分:{score}]" if score else ""
            impact_str = f" [{impact}]" if impact else ""
            evidence_list.append(Evidence(
                type="政策" if dim_key == "policy" else "行情",
                content=f"[{dim_label}]{impact_str}{score_str} {detail}",
                source=f"大模型{dim_label}分析",
                time="",
            ))

    # ---- 新增：从个股行情数据提取行情证据 ----
    if market_data:
        realtime = market_data.get("realtime", {})
        if realtime and "error" not in realtime:
            evidence_list.append(Evidence(
                type="行情",
                content=(
                    f"{realtime.get('name', realtime.get('code', ''))} "
                    f"现价 {realtime.get('price', 0)} "
                    f"涨跌幅 {realtime.get('change_pct', 0)}% "
                    f"成交量 {realtime.get('volume', 0)} "
                    f"成交额 {realtime.get('amount', 0)}"
                ),
                source=f"实时行情({realtime.get('source', '未知')})",
                time="",
            ))
        history = market_data.get("history", {})
        if history and "error" not in history:
            s = history.get("summary", {})
            evidence_list.append(Evidence(
                type="行情",
                content=(
                    f"近{history.get('days', 0)}日 "
                    f"均价 {s.get('avg_close', 0)} "
                    f"最高 {s.get('max_close', 0)} "
                    f"最低 {s.get('min_close', 0)} "
                    f"均量 {s.get('avg_volume', 0)}"
                ),
                source=f"历史K线({history.get('source', '未知')})",
                time="",
            ))

    # ---- 新增：从行情验证提取验证证据 ----
    if verification:
        ver_detail = verification.get("detail", "")
        if ver_detail:
            consistent = verification.get("consistent")
            consistent_str = ""
            if consistent is True:
                consistent_str = " [一致]"
            elif consistent is False:
                consistent_str = " [不一致]"
            warning = verification.get("warning", "")
            warning_str = f" 警告: {warning}" if warning else ""
            signals = verification.get("signals", [])
            signals_str = f" 信号: {'、'.join(signals)}" if signals else ""
            evidence_list.append(Evidence(
                type="行情验证",
                content=f"行情验证{consistent_str}{warning_str}{signals_str} {ver_detail}",
                source="行情验证模块",
                time="",
            ))

    # ---- 新增：从自我反思提取修正证据 ----
    if reflection:
        corrections = reflection.get("corrections", [])
        if corrections:
            evidence_list.append(Evidence(
                type="反思修正",
                content=f"反思发现并修正: {'、'.join([str(c) for c in corrections[:3]])}",
                source="自我反思模块",
                time="",
            ))
        issues = reflection.get("issues_found", [])
        if issues:
            evidence_list.append(Evidence(
                type="风险提示",
                content=f"反思发现问题: {'、'.join([str(i) for i in issues[:3]])}",
                source="自我反思模块",
                time="",
            ))

    # 从报告提取综合分析证据
    for field_key, field_label in [
        ("fundamental_analysis", "基本面"),
        ("capital_analysis", "资金面"),
        ("sentiment_analysis", "情绪面"),
        ("policy_analysis", "政策面"),
    ]:
        val = report.get(field_key, "")
        if val and not any(e.content.startswith(f"[{field_label}]") for e in evidence_list):
            evidence_list.append(Evidence(
                type="财报" if "基本" in field_label else "政策",
                content=f"[{field_label}] {val}",
                source="投研简报",
                time="",
            ))

    return evidence_list


# ================================================================
# 第三部分：意图识别与任务拆解
# ================================================================
# 意图关键词映射表
_INTENT_KEYWORDS: Dict[str, List[str]] = {
    "行情查询": ["行情", "走势", "股价", "涨跌", "K线", "均线", "量能"],
    "新闻搜索": ["新闻", "政策", "舆情", "消息", "资讯", "动态", "公告"],
    "财报分析": ["财报", "营收", "利润", "毛利", "净利", "年报", "季报", "业绩"],
    "产业链分析": ["产业链", "上下游", "供应链", "行业格局", "竞争"],
}


def plan_tasks(query: str, stock_code: Optional[str] = None) -> Tuple[List[str], Optional[str]]:
    """根据用户输入识别意图并拆解子任务。

    通过关键词匹配识别用户意图，支持多意图组合。
    同时从 query 中提取股票代码。

    Args:
        query: 用户问题文本。
        stock_code: 可选的股票代码（优先使用显式传入的）。

    Returns:
        元组 (sub_tasks, extracted_code):
            - sub_tasks: 拆解后的子任务列表。
            - extracted_code: 提取到的股票代码（可能为 None）。
    """
    # 提取股票代码
    extracted_code = stock_code
    if not extracted_code:
        extracted_code = _extract_stock_code(query)

    # 意图识别
    matched_intents: List[str] = []
    for intent, keywords in _INTENT_KEYWORDS.items():
        if any(kw in query for kw in keywords):
            matched_intents.append(intent)

    # 构建子任务
    sub_tasks: List[str] = []

    if not matched_intents:
        # 无明确意图 -> 默认综合分析
        sub_tasks.append("综合分析")
    else:
        for intent in matched_intents:
            if intent == "行情查询":
                sub_tasks.append("查询实时行情与历史走势")
            elif intent == "新闻搜索":
                sub_tasks.append("搜索最新财经新闻与政策动态")
            elif intent == "财报分析":
                sub_tasks.append("分析财务数据与业绩表现")
            elif intent == "产业链分析":
                sub_tasks.append("分析产业链上下游格局")

    # 如果有股票代码，始终加上深度投研分析
    if extracted_code and "综合分析" not in sub_tasks:
        sub_tasks.append("深度投研分析")

    print(f"{_LOG_PREFIX}意图识别: {matched_intents or ['默认综合分析']}")
    print(f"{_LOG_PREFIX}子任务: {sub_tasks}")
    print(f"{_LOG_PREFIX}股票代码: {extracted_code or '无'}")

    return sub_tasks, extracted_code


# ================================================================
# 第四部分：服务编排主函数
# ================================================================
async def run_agent(
    query: str,
    stock_code: Optional[str] = None,
    time_range: str = "6m",
) -> AnalyzeResponse:
    """执行完整的投研分析流程（异步编排）。

    流程：
      1. 任务拆解（意图识别）
      2. 获取新闻数据
      3. 获取大盘行情
      4. 深度投研分析（七阶段 Agent）
      5. 结果封装

    每个步骤记录执行状态、耗时到 thinking_log。
    单个工具调用失败不中断整体流程，记录错误后继续。

    新增：充分利用 sentiment_analyzer 七阶段分析的完整输出，
    包括事件解析详情、分析规划详情、多维推理评分、行情验证详情、
    自我反思详情、置信度调整、投研简报完整字段等。

    Args:
        query: 用户问题文本。
        stock_code: 6 位股票代码（可选）。
        time_range: 时间范围，默认 "6m"。

    Returns:
        完整的 AnalyzeResponse 分析响应。
    """
    thinking_log: List[ThinkingStep] = []
    data_sources: List[str] = []
    news_text: str = ""
    news_result: dict = {}
    index_data_len: int = 0
    agent_result: dict = {}

    # ============================================================
    # 第零步：长期记忆检索（预留接口）
    # ============================================================
    step_name = "记忆检索"
    t0 = time.time()
    thinking_log.append(_make_step(step_name, "running", "正在检索相关历史分析 ..."))
    print(f"{_LOG_PREFIX}[步骤 0] 记忆检索 ...")

    memory_context: str = ""
    try:
        memory_context = await asyncio.to_thread(retrieve_memory, query)
        duration = int((time.time() - t0) * 1000)
        if memory_context:
            thinking_log.append(_make_step(
                step_name, "success",
                f"检索到相关历史分析（{len(memory_context)} 字符）",
                duration,
            ))
        else:
            thinking_log.append(_make_step(
                step_name, "success",
                "暂无相关历史记忆（预留接口）",
                duration,
            ))
        print(f"{_LOG_PREFIX}记忆检索完成")
    except Exception as e:
        duration = int((time.time() - t0) * 1000)
        thinking_log.append(_make_step(
            step_name, "failed", f"记忆检索异常: {e}", duration,
        ))
        print(f"{_LOG_PREFIX}记忆检索异常: {e}")

    # ============================================================
    # 第一步：任务拆解
    # ============================================================
    step_name = "任务拆解"
    t0 = time.time()
    thinking_log.append(_make_step(step_name, "running", "正在识别意图并拆解子任务 ..."))
    print(f"{_LOG_PREFIX}[步骤 1] 任务拆解 ...")

    try:
        sub_tasks, extracted_code = await asyncio.to_thread(
            plan_tasks, query, stock_code
        )
        # 优先使用从 query 中提取的代码
        if not stock_code and extracted_code:
            stock_code = extracted_code

        duration = int((time.time() - t0) * 1000)
        thinking_log.append(_make_step(
            step_name, "success",
            f"识别到 {len(sub_tasks)} 个子任务: {', '.join(sub_tasks)}"
            + (f"，股票代码: {stock_code}" if stock_code else ""),
            duration,
        ))
    except Exception as e:
        duration = int((time.time() - t0) * 1000)
        thinking_log.append(_make_step(
            step_name, "failed", f"任务拆解异常: {e}", duration,
        ))
        sub_tasks = ["综合分析"]
        print(f"{_LOG_PREFIX}任务拆解异常: {e}")

    # ============================================================
    # 第二步：获取新闻数据
    # ============================================================
    need_news = any("新闻" in t or "综合" in t for t in sub_tasks)
    if need_news:
        step_name = "新闻获取"
        t0 = time.time()
        thinking_log.append(_make_step(step_name, "running", "正在获取最新财经新闻 ..."))
        print(f"{_LOG_PREFIX}[步骤 2] 新闻获取 ...")

        try:
            news_result = await asyncio.to_thread(
                fetch_latest_news, source="auto", keyword=""
            )
            duration = int((time.time() - t0) * 1000)

            if news_result.get("success") and news_result.get("count", 0) > 0:
                data_sources.append(f"新闻({news_result['source']})")
                # 拼接新闻标题作为分析素材
                df_news = news_result.get("data")
                if df_news is not None and not df_news.empty:
                    titles = df_news["title"].head(10).tolist() if "title" in df_news.columns else []
                    contents = df_news["content"].head(10).tolist() if "content" in df_news.columns else []
                    # 优先用标题拼接，若标题为空则用内容
                    news_parts = []
                    for t_val, c_val in zip(titles, contents):
                        text = t_val if t_val else c_val
                        if text:
                            news_parts.append(str(text)[:80])
                    news_text = "；".join(news_parts[:5])

                thinking_log.append(_make_step(
                    step_name, "success",
                    f"获取 {news_result['count']} 条新闻"
                    + (f"（缓存）" if news_result.get("cached") else "")
                    + f"，来源: {news_result['source']}",
                    duration,
                ))
                print(f"{_LOG_PREFIX}新闻获取成功: {news_result['count']} 条")
            else:
                thinking_log.append(_make_step(
                    step_name, "failed",
                    "新闻获取返回空结果，将跳过新闻分析",
                    duration,
                ))
                print(f"{_LOG_PREFIX}新闻获取返回空结果")
        except Exception as e:
            duration = int((time.time() - t0) * 1000)
            thinking_log.append(_make_step(
                step_name, "failed", f"新闻获取异常: {e}", duration,
            ))
            print(f"{_LOG_PREFIX}新闻获取异常: {e}")

    # ============================================================
    # 第三步：获取大盘行情
    # ============================================================
    step_name = "大盘行情"
    t0 = time.time()
    thinking_log.append(_make_step(step_name, "running", "正在获取沪深300大盘行情 ..."))
    print(f"{_LOG_PREFIX}[步骤 3] 大盘行情获取 ...")

    try:
        df_index = await asyncio.to_thread(fetch_hs300_history)
        duration = int((time.time() - t0) * 1000)
        index_data_len = len(df_index)
        data_sources.append("沪深300行情")
        thinking_log.append(_make_step(
            step_name, "success",
            f"获取沪深300历史数据 {index_data_len} 条",
            duration,
        ))
        print(f"{_LOG_PREFIX}大盘行情获取成功: {index_data_len} 条")
    except Exception as e:
        duration = int((time.time() - t0) * 1000)
        thinking_log.append(_make_step(
            step_name, "failed", f"大盘行情获取异常: {e}", duration,
        ))
        print(f"{_LOG_PREFIX}大盘行情获取异常: {e}")

    # ============================================================
    # 第四步：深度投研分析（七阶段 Agent）
    # ============================================================
    step_name = "深度投研分析"
    t0 = time.time()
    thinking_log.append(_make_step(
        step_name, "running",
        "正在执行七阶段投研分析流水线"
        "（事件解析->分析规划->工具调用->多维推理->行情验证->自我反思->报告生成）...",
    ))
    print(f"{_LOG_PREFIX}[步骤 4] 深度投研分析 ...")

    try:
        # 构造分析输入：优先用新闻文本，否则用原始 query
        analysis_input = news_text if news_text else query

        agent_result = await asyncio.to_thread(
            _agent.analyze, analysis_input, stock_code or ""
        )
        duration = int((time.time() - t0) * 1000)

        data_sources.append("智谱GLM大模型")

        thinking_log.append(_make_step(
            step_name, "success",
            "七阶段分析完成（事件解析->分析规划->工具调用->多维推理->行情验证->自我反思->报告生成）",
            duration,
        ))
        print(f"{_LOG_PREFIX}深度投研分析完成")

    except Exception as e:
        duration = int((time.time() - t0) * 1000)
        thinking_log.append(_make_step(
            step_name, "failed", f"深度投研分析异常: {e}", duration,
        ))
        print(f"{_LOG_PREFIX}深度投研分析异常: {e}")

    # ============================================================
    # 第五步：JSON 结构化输出（强制大模型返回标准 JSON）
    # ============================================================
    step_name = "JSON结构化输出"
    t0 = time.time()
    thinking_log.append(_make_step(
        step_name, "running",
        "正在调用大模型生成结构化 JSON ...",
    ))
    print(f"{_LOG_PREFIX}[步骤 5] JSON 结构化输出 ...")

    json_output: dict = {}
    try:
        # 构造分析上下文供 JSON LLM 参考
        analysis_context = ""
        if agent_result:
            _report = agent_result.get("report", {})
            analysis_context = (
                f"事件摘要: {_report.get('event_summary', '')}\n"
                f"短期观点: {_report.get('short_term_view', '')}\n"
                f"中期观点: {_report.get('mid_term_view', '')}\n"
                f"置信度: {_report.get('confidence', 0.5)}"
            )
        if news_text:
            analysis_context += f"\n新闻摘要: {news_text[:200]}"

        raw_json_text = await asyncio.to_thread(
            _call_llm,
            JSON_SYSTEM_PROMPT,
            f"用户问题: {query}\n\n分析上下文:\n{analysis_context}",
            2000,  # max_tokens
        )
        duration = int((time.time() - t0) * 1000)

        # 健壮的 JSON 解析（自动去除 Markdown 标记）
        json_output = _parse_json_output(raw_json_text)

        thinking_log.append(_make_step(
            step_name, "success",
            f"JSON 解析成功: core_conclusion='"
            f"{json_output.get('core_conclusion', '')[:50]}...'",
            duration,
        ))
        print(f"{_LOG_PREFIX}JSON 结构化输出完成")

    except Exception as e:
        duration = int((time.time() - t0) * 1000)
        thinking_log.append(_make_step(
            step_name, "failed", f"JSON 结构化输出异常: {e}", duration,
        ))
        print(f"{_LOG_PREFIX}JSON 结构化输出异常: {e}")

    # ============================================================
    # 第六步：结果封装（融合 JSON 结构化数据）
    # ============================================================
    step_name = "结果封装"
    t0 = time.time()
    thinking_log.append(_make_step(step_name, "running", "正在封装分析结果 ..."))
    print(f"{_LOG_PREFIX}[步骤 6] 结果封装 ...")

    try:
        # ---- 提取 Agent 完整结果 ----
        report = agent_result.get("report", {})
        reflection = agent_result.get("reflection", {})
        reasoning = agent_result.get("reasoning", {})
        plan_data = agent_result.get("plan", {})
        event_parse = agent_result.get("event_parse", {})
        verification = agent_result.get("verification", {})
        market_data = agent_result.get("market_data", {})

        # ---- 新增：事件解析详情 ----
        event_detail = EventParseDetail(
            summary=event_parse.get("summary", ""),
            entities=event_parse.get("entities", []),
            event_type=event_parse.get("event_type", ""),
            need_market_data=event_parse.get("need_market_data", False),
            related_stock_hint=event_parse.get("related_stock_hint", ""),
        )

        # ---- 新增：分析规划详情 ----
        plan_detail = PlanDetail(
            key_questions=plan_data.get("key_questions", []),
            tools_needed=plan_data.get("tools_needed", []),
            dimensions=plan_data.get("dimensions", {}),
            analysis_focus=plan_data.get("analysis_focus", ""),
        )

        # ---- 新增：多维推理评分 ----
        multi_dim_scores = MultiDimensionScores(
            fundamental=DimensionScore(
                impact=reasoning.get("fundamental", {}).get("impact", ""),
                score=reasoning.get("fundamental", {}).get("score", 0.5),
                detail=reasoning.get("fundamental", {}).get("detail", ""),
            ),
            capital=DimensionScore(
                impact=reasoning.get("capital", {}).get("impact", ""),
                score=reasoning.get("capital", {}).get("score", 0.5),
                detail=reasoning.get("capital", {}).get("detail", ""),
            ),
            sentiment=DimensionScore(
                impact=reasoning.get("sentiment", {}).get("impact", ""),
                score=reasoning.get("sentiment", {}).get("score", 0.5),
                detail=reasoning.get("sentiment", {}).get("detail", ""),
            ),
            policy=DimensionScore(
                impact=reasoning.get("policy", {}).get("impact", ""),
                score=reasoning.get("policy", {}).get("score", 0.5),
                detail=reasoning.get("policy", {}).get("detail", ""),
            ),
            risk_factors=reasoning.get("risk_factors", []),
            reasoning_chain=reasoning.get("reasoning_chain", ""),
        )

        # ---- 新增：行情验证详情 ----
        verification_detail = VerificationDetail(
            consistent=verification.get("consistent"),
            signals=verification.get("signals", []),
            detail=verification.get("detail", ""),
            warning=verification.get("warning", ""),
        )

        # ---- 新增：自我反思详情 ----
        reflection_detail = ReflectionDetail(
            issues_found=reflection.get("issues_found", []),
            corrections=reflection.get("corrections", []),
            confidence_adjustment=reflection.get("confidence_adjustment", 0.0),
            final_sentiment=reflection.get("final_sentiment", "中性"),
            reflection_detail=reflection.get("reflection_detail", ""),
        )

        # ---- 最终结论 ----
        conclusion = report.get("event_summary", "")
        if report.get("short_term_view"):
            conclusion += f" 短期观点: {report['short_term_view']}"
        if report.get("mid_term_view"):
            conclusion += f" 中期观点: {report['mid_term_view']}"
        if not conclusion:
            conclusion = query  # 降级：用原始 query 作为结论

        # ---- 新增：影响级别和操作建议 ----
        impact_level = report.get("impact_level", "")
        action_advice = report.get("action_advice", "")

        # ---- 置信度（应用反思调整） ----
        raw_confidence = report.get("confidence", 0.5)
        confidence_adjustment = reflection.get("confidence_adjustment", 0.0)
        # 应用置信度调整：原始置信度 + 调整值，限制在 0~1 范围
        adjusted_confidence = max(0.0, min(1.0, raw_confidence + confidence_adjustment))
        confidence = adjusted_confidence

        # ---- 风险提示 ----
        risks: List[str] = []
        risk_factors = reasoning.get("risk_factors", [])
        if risk_factors:
            risks.extend([str(r) for r in risk_factors])
        corrections = reflection.get("corrections", [])
        if corrections:
            risks.extend([f"反思修正: {c}" for c in corrections])
        # 新增：加入验证警告
        ver_warning = verification.get("warning", "")
        if ver_warning:
            risks.append(f"行情验证警告: {ver_warning}")
        if not risks:
            risks.append("信息有限，存在不确定性")

        # ---- 推理步骤 ----
        reasoning_steps: List[str] = []
        chain = report.get("reasoning_chain", "")
        if chain:
            reasoning_steps.append(f"推理链: {chain}")
        reflection_d = reflection.get("reflection_detail", "")
        if reflection_d:
            reasoning_steps.append(f"自我反思: {reflection_d}")
        # 新增：加入验证信息
        ver_detail = verification.get("detail", "")
        if ver_detail:
            reasoning_steps.append(f"行情验证: {ver_detail}")
        if not reasoning_steps:
            reasoning_steps = ["基于有限信息进行综合分析"]

        # ---- 证据列表 ----
        evidence_list: List[Evidence] = []
        if agent_result:
            evidence_list = _build_evidence_from_agent(agent_result)

        # ---- 新闻证据补充 ----
        if news_result.get("success") and news_result.get("count", 0) > 0:
            evidence_list.insert(0, Evidence(
                type="新闻",
                content=f"获取 {news_result['count']} 条最新财经快讯",
                source=f"东方财富/新浪 ({news_result.get('source', '-')})",
                time="",
            ))

        # ---- 大盘数据证据 ----
        if index_data_len > 0:
            evidence_list.insert(0, Evidence(
                type="行情",
                content=f"沪深300历史数据 {index_data_len} 条",
                source="akshare",
                time="",
            ))

        # ---- 融合 JSON 结构化输出结果 ----
        if json_output:
            # 用 JSON 核心结论覆盖/增强结论
            json_conclusion = json_output.get("core_conclusion", "")
            if json_conclusion and json_conclusion != "暂无数据":
                conclusion = json_conclusion

            # 用 JSON 风险提示补充
            json_risks = json_output.get("risks", [])
            for risk_item in json_risks:
                if isinstance(risk_item, dict):
                    level = risk_item.get("level", "")
                    desc = risk_item.get("description", "")
                    if desc:
                        risks.append(f"[{level}] {desc}" if level else desc)
                elif isinstance(risk_item, str) and risk_item:
                    risks.append(risk_item)

            # 添加 JSON 证据
            json_evidence_list = _build_json_evidence(json_output)
            evidence_list.extend(json_evidence_list)

        # ---- 长期记忆存储（预留接口） ----
        try:
            await asyncio.to_thread(
                save_to_memory, query,
                {"conclusion": conclusion, "confidence": confidence, "risks": risks},
            )
        except Exception as mem_err:
            print(f"{_LOG_PREFIX}记忆存储异常（不影响主流程）: {mem_err}")

        # ---- 如果所有数据源均失败，降级结论 ----
        if not data_sources:
            conclusion = (
                "当前数据获取受限，无法完成完整分析。"
                "建议检查网络连接后重试，或稍后再试。"
            )
            confidence = 0.3

        duration = int((time.time() - t0) * 1000)
        thinking_log.append(_make_step(
            step_name, "success",
            f"封装完成: {len(evidence_list)} 条证据, {len(risks)} 条风险提示"
            + (f"，置信度调整: {confidence_adjustment:+.1f}" if confidence_adjustment != 0 else ""),
            duration,
        ))

        # ---- 构建最终响应 ----
        response = AnalyzeResponse(
            query=query,
            sub_tasks=sub_tasks,
            data_sources=data_sources,
            reasoning_steps=reasoning_steps,
            conclusion=conclusion.strip(),
            confidence=confidence,
            risks=risks,
            evidence=evidence_list,
            thinking_log=thinking_log,
            # ---- 新增字段 ----
            event_detail=event_detail,
            plan_detail=plan_detail,
            multi_dim_scores=multi_dim_scores,
            verification_detail=verification_detail,
            reflection_detail=reflection_detail,
            impact_level=impact_level,
            action_advice=action_advice,
            disclaimer="本内容仅供参考，不构成投资建议",
        )

        print(f"{_LOG_PREFIX}结果封装完成")
        return response

    except Exception as e:
        duration = int((time.time() - t0) * 1000)
        thinking_log.append(_make_step(
            step_name, "failed", f"结果封装异常: {e}", duration,
        ))
        print(f"{_LOG_PREFIX}结果封装异常: {e}")

        # ---- 最终降级：返回包含错误信息的响应 ----
        return AnalyzeResponse(
            query=query,
            sub_tasks=sub_tasks if 'sub_tasks' in dir() else ["综合分析"],
            data_sources=data_sources,
            reasoning_steps=["分析流程异常，已尽力尝试各步骤"],
            conclusion=f"分析过程中出现异常: {e}。建议稍后重试。",
            confidence=0.3,
            risks=["分析流程未完整执行，结果可靠性降低"],
            evidence=[],
            thinking_log=thinking_log,
            # ---- 新增字段（降级时填默认值） ----
            event_detail=EventParseDetail(),
            plan_detail=PlanDetail(),
            multi_dim_scores=MultiDimensionScores(),
            verification_detail=VerificationDetail(),
            reflection_detail=ReflectionDetail(),
            impact_level="",
            action_advice="",
            disclaimer="本内容仅供参考，不构成投资建议",
        )