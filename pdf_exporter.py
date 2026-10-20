"""
pdf_exporter.py - 选股结果 PDF 报告导出模块
============================================
功能：将选股结果 DataFrame 导出为排版整洁的 PDF 报告。
依赖：reportlab（pip install reportlab）
"""

# ================================================================
# 依赖检查：检测 reportlab 是否已安装
# ================================================================
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    )
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.pdfmetrics import registerFontFamily
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib.enums import TA_CENTER
except ImportError:
    print("=" * 60)
    print("[错误] 未安装 reportlab 库，无法生成 PDF 报告")
    print("请执行以下命令安装: pip install reportlab")
    print("=" * 60)
    raise

import os
import platform
import pandas as pd
from datetime import datetime


# ================================================================
# 日志前缀
# ================================================================
_LOG_PREFIX = "【PDF导出】"


# ================================================================
# 中文字体注册
# ================================================================
_FONT_REGISTERED = False
_FONT_NAME = "Helvetica"  # 默认回退字体


def _scan_cjk_fonts(system: str) -> list:
    """扫描系统字体目录，返回找到的 CJK 字体文件路径列表。

    不同发行版/安装方式的中文字体路径差异较大，静态路径列表可能
    覆盖不全，此处遍历常见字体目录，按文件名关键词匹配 CJK 字体。
    """
    if system == "Windows":
        roots = [os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")]
    elif system == "Darwin":
        roots = ["/System/Library/Fonts", "/Library/Fonts"]
    else:
        home = os.path.expanduser("~")
        roots = [
            "/usr/share/fonts",
            "/usr/local/share/fonts",
            os.path.join(home, ".fonts"),
            os.path.join(home, ".local", "share", "fonts"),
        ]
    keywords = (
        "zenhei", "wqy", "notosanscjk", "notoserifcjk", "notosanssc",
        "notoserifsc", "sourcehansans", "sourcehans", "simhei", "simsun",
        "msyh", "pingfang", "stheiti", "hiragino", "droidsansfallback",
        "ukai", "uming", "cjk",
    )
    found = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                low = fn.lower()
                if not low.endswith((".ttf", ".ttc", ".otf")):
                    continue
                if any(k in low for k in keywords):
                    found.append(os.path.join(dirpath, fn))
    return found


def _register_chinese_font() -> str:
    """注册中文字体，返回可用的字体名称。

    按优先级依次查找系统自带的中文字体：
      - Windows: simhei.ttf（黑体）、msyh.ttc（微软雅黑）、simsun.ttc（宋体）
      - Linux: wqy-zenhei.ttc（文泉驿正黑）、NotoSansCJK 等
      - macOS: PingFang.ttc（苹方）、STHeiti Light.ttc（华文黑体）

    注册时同时调用 registerFontFamily，将 normal/bold/italic/boldItalic
    全部映射到同一字体文件，确保 <b>、<i> 等标签不会回退到不支持中文的
    Helvetica。对 .ttc 字体集合文件显式指定 subfontIndex=0 以兼容低版本
    reportlab。

    Returns:
        注册成功后的字体名称，若全部失败则返回 "Helvetica"。
    """
    global _FONT_REGISTERED, _FONT_NAME

    if _FONT_REGISTERED:
        return _FONT_NAME

    # 候选字体路径（按优先级排列）
    system = platform.system()

    if system == "Windows":
        font_dir = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
        candidates = [
            ("SimHei", os.path.join(font_dir, "simhei.ttf")),
            ("MSYH", os.path.join(font_dir, "msyh.ttc")),
            ("SimSun", os.path.join(font_dir, "simsun.ttc")),
            ("MSYHBD", os.path.join(font_dir, "msyhbd.ttc")),
        ]
    elif system == "Darwin":
        candidates = [
            ("PingFang", "/System/Library/Fonts/PingFang.ttc"),
            ("STHeiti", "/System/Library/Fonts/STHeiti Light.ttc"),
            ("HiraginoSansGB", "/System/Library/Fonts/Hiragino Sans GB.ttc"),
        ]
    else:
        # Linux: 覆盖常见发行版的字体安装路径
        candidates = [
            ("WenQuanYi", "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
            ("WenQuanYi", "/usr/share/fonts/wqy-zenhei/wqy-zenhei.ttc"),
            ("WenQuanYi", "/usr/share/fonts/wqy/wqy-zenhei.ttc"),
            ("NotoSansCJK", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            ("NotoSansCJK", "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
            ("NotoSansSC", "/usr/share/fonts/opentype/noto/NotoSansSC-Regular.otf"),
            ("NotoSansSC", "/usr/share/fonts/truetype/noto/NotoSansSC-Regular.ttf"),
            ("SourceHanSans", "/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Regular.otf"),
            ("SourceHanSans", "/usr/share/fonts/source-han-sans/SourceHanSansSC-Regular.otf"),
            ("DroidSansFallback", "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"),
        ]

    # 环境变量指定的字体路径最优先
    env_font = os.environ.get("PDF_FONT_PATH")
    if env_font and os.path.exists(env_font):
        candidates.insert(0, ("CustomCJK", env_font))

    # 目录扫描兜底：补充静态列表未覆盖的字体路径
    seen = {p for _n, p in candidates}
    for path in _scan_cjk_fonts(system):
        if path not in seen:
            seen.add(path)
            name = os.path.splitext(os.path.basename(path))[0]
            candidates.append((name, path))

    for font_name, font_path in candidates:
        if not os.path.exists(font_path):
            continue
        try:
            # .ttc 字体集合需要显式指定子字体索引，低版本 reportlab
            # 不指定会抛异常；.ttf/.otf 则不需要该参数。
            if font_path.lower().endswith(".ttc"):
                font_obj = TTFont(font_name, font_path, subfontIndex=0)
            else:
                font_obj = TTFont(font_name, font_path)
            pdfmetrics.registerFont(font_obj)
            # 注册字体族：将粗体/斜体/粗斜体全部指向同一字体，
            # 否则 <b>、<i> 标签会回退到 Helvetica 导致中文丢失。
            registerFontFamily(
                font_name,
                normal=font_name,
                bold=font_name,
                italic=font_name,
                boldItalic=font_name,
            )
            _FONT_NAME = font_name
            _FONT_REGISTERED = True
            print(f"{_LOG_PREFIX}中文字体注册成功: {font_name} ({font_path})")
            return _FONT_NAME
        except Exception as e:
            print(f"{_LOG_PREFIX}字体注册失败 {font_path}: {type(e).__name__}: {e}")
            continue

    print(f"{_LOG_PREFIX}[警告] 未找到可用的中文字体，PDF 中文可能显示为方框")
    print(f"{_LOG_PREFIX}  Windows: 安装 simhei.ttf 或微软雅黑")
    print(f"{_LOG_PREFIX}  Linux:   apt install fonts-wqy-zenhei 或 fonts-noto-cjk")
    return _FONT_NAME


# ================================================================
# 公共工具：创建 PDF 文档和通用样式
# ================================================================
def _create_pdf_doc(filename: str):
    """创建 PDF 文档并注册中文字体，返回 (doc, font_name, styles)。

    Args:
        filename: 输出 PDF 文件路径。

    Returns:
        (SimpleDocTemplate, font_name, getSampleStyleSheet)
    """
    font_name = _register_chinese_font()
    doc = SimpleDocTemplate(
        filename,
        pagesize=A4,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
    )
    styles = getSampleStyleSheet()
    return doc, font_name, styles


def _build_footer_style(styles, font_name):
    """创建通用页脚样式。"""
    return ParagraphStyle(
        "Footer", parent=styles["Normal"],
        fontName=font_name, fontSize=8, leading=12,
        alignment=TA_CENTER, textColor=colors.HexColor("#999999"),
    )


def _build_pdf(elements, doc, filename: str, log_label: str) -> bool:
    """统一构建 PDF 并处理异常。

    Args:
        elements: Platypus 元素列表。
        doc: SimpleDocTemplate 实例。
        filename: 输出文件名。
        log_label: 日志标签（用于打印成功信息）。

    Returns:
        True 表示成功，False 表示失败。
    """
    try:
        doc.build(elements)
        print(f"{_LOG_PREFIX}{log_label} PDF 生成成功: {filename}")
        return True
    except Exception as e:
        print(f"{_LOG_PREFIX}[错误] {log_label} PDF 生成失败: {type(e).__name__}: {e}")
        traceback.print_exc()
        return False


# ================================================================
# 核心函数：生成 PDF 报告
# ================================================================
def generate_stock_pdf(df: pd.DataFrame, filename: str = "stock_report.pdf") -> bool:
    """将选股结果 DataFrame 导出为 PDF 报告。

    Args:
        df: 选股结果 DataFrame，至少包含 代码、名称、最新价 列。
        filename: 输出的 PDF 文件路径，默认 "stock_report.pdf"。

    Returns:
        True 表示生成成功，False 表示生成失败。
    """
    try:
        doc, font_name, styles = _create_pdf_doc(filename)
        page_width, page_height = A4

        # 样式定义
        title_style = ParagraphStyle(
            "ChineseTitle", parent=styles["Title"],
            fontName=font_name, fontSize=22, leading=28,
            alignment=TA_CENTER, spaceAfter=4 * mm,
            textColor=colors.HexColor("#1a1a2e"),
        )
        time_style = ParagraphStyle(
            "ChineseTime", parent=styles["Normal"],
            fontName=font_name, fontSize=10, leading=14,
            alignment=TA_CENTER, spaceAfter=8 * mm,
            textColor=colors.HexColor("#666666"),
        )
        footer_style = _build_footer_style(styles, font_name)

        # 表头样式
        header_style = ParagraphStyle(
            "TableHeader",
            parent=styles["Normal"],
            fontName=font_name,
            fontSize=9,
            leading=12,
            alignment=TA_CENTER,
            textColor=colors.white,
        )

        # 表格内容样式
        cell_style = ParagraphStyle(
            "TableCell",
            parent=styles["Normal"],
            fontName=font_name,
            fontSize=8.5,
            leading=11,
            alignment=TA_CENTER,
        )

        # 左对齐单元格样式（用于名称列）
        cell_left_style = ParagraphStyle(
            "TableCellLeft",
            parent=cell_style,
            alignment=TA_CENTER,
        )

        # ---- 构建文档内容 ----
        elements = []

        # 标题
        elements.append(Paragraph("AI 智能选股结果报告", title_style))

        # 生成时间
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        elements.append(Paragraph(f"报告生成时间：{now_str}", time_style))

        # ---- 构建表格数据 ----
        if df is None or df.empty:
            elements.append(Paragraph("暂无选股结果数据", cell_style))
            doc.build(elements)
            print(f"{_LOG_PREFIX}PDF 已生成（无数据）: {filename}")
            return True

        # 确定要展示的列（优先展示核心列）
        preferred_cols = ["代码", "名称", "最新价", "涨跌幅", "涨跌额",
                          "成交量", "成交额", "买入", "卖出", "昨收", "今开",
                          "最高", "最低"]
        display_cols = [c for c in preferred_cols if c in df.columns]
        # 如果有不在首选列表中的列，也加上
        for c in df.columns:
            if c not in display_cols and c != "序号":
                display_cols.append(c)

        # 限制最多展示 10 列，避免表格过宽
        if len(display_cols) > 10:
            display_cols = display_cols[:10]

        # 表头行
        header_row = [Paragraph(col, header_style) for col in display_cols]

        # 数据行
        data_rows = []
        max_rows = min(len(df), 50)  # 最多展示 50 行
        for i in range(max_rows):
            row = []
            for col in display_cols:
                val = df.iloc[i].get(col, "")
                # 格式化数值
                if pd.notna(val):
                    if isinstance(val, float):
                        if "涨跌幅" in col:
                            cell_text = f"{val:+.2f}%"
                        elif "成交额" in col and val >= 1e8:
                            cell_text = f"{val / 1e8:.2f}亿"
                        elif "成交额" in col and val >= 1e4:
                            cell_text = f"{val / 1e4:.1f}万"
                        elif "成交量" in col and val >= 1e4:
                            cell_text = f"{val / 1e4:.1f}万"
                        elif "价" in col or "买入" in col or "卖出" in col \
                                or "昨收" in col or "今开" in col \
                                or "最高" in col or "最低" in col:
                            cell_text = f"{val:.2f}"
                        else:
                            cell_text = f"{val:.2f}"
                    else:
                        cell_text = str(val)
                else:
                    cell_text = "-"

                # 名称列用左对齐样式
                if col == "名称":
                    row.append(Paragraph(cell_text, cell_left_style))
                else:
                    row.append(Paragraph(cell_text, cell_style))
            data_rows.append(row)

        # 合并表头和数据
        table_data = [header_row] + data_rows

        # ---- 计算列宽 ----
        available_width = page_width - 30 * mm  # 左右各 15mm 边距
        num_cols = len(display_cols)
        if num_cols <= 4:
            col_width = available_width / num_cols
            col_widths = [col_width] * num_cols
        else:
            # 名称列稍宽，数值列窄一些
            col_widths = []
            for col in display_cols:
                if col == "名称":
                    col_widths.append(available_width * 0.15)
                elif col == "代码":
                    col_widths.append(available_width * 0.12)
                else:
                    remaining = available_width * 0.73
                    col_widths.append(remaining / (num_cols - 2))

        # ---- 创建表格 ----
        table = Table(table_data, colWidths=col_widths, repeatRows=1)

        # ---- 表格样式 ----
        table_style_commands = [
            # 表头背景色（深蓝）
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a1a2e")),
            # 表头文字颜色
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            # 网格线
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
            # 表头底线加粗
            ("LINEBELOW", (0, 0), (-1, 0), 1.5, colors.HexColor("#1a1a2e")),
            # 行高
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            # 垂直居中
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            # 对齐
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ]

        # 斑马条纹（交替行背景色）
        for i in range(1, len(table_data)):
            if i % 2 == 0:
                table_style_commands.append(
                    ("BACKGROUND", (0, i), (-1, i), colors.HexColor("#f5f5f5"))
                )

        # 涨跌幅着色
        change_col_idx = None
        for idx, col in enumerate(display_cols):
            if col == "涨跌幅":
                change_col_idx = idx
                break

        if change_col_idx is not None:
            for i in range(max_rows):
                val = df.iloc[i].get("涨跌幅", 0)
                if pd.notna(val):
                    try:
                        v = float(val)
                        if v > 0:
                            color = colors.HexColor("#e74c3c")  # 红色（涨）
                        elif v < 0:
                            color = colors.HexColor("#27ae60")  # 绿色（跌）
                        else:
                            color = colors.HexColor("#333333")  # 灰色（平）
                        table_style_commands.append(
                            ("TEXTCOLOR", (change_col_idx, i + 1),
                             (change_col_idx, i + 1), color)
                        )
                    except (ValueError, TypeError):
                        pass

        table.setStyle(TableStyle(table_style_commands))
        elements.append(table)

        # 数据统计
        elements.append(Spacer(1, 6 * mm))
        stats_style = ParagraphStyle(
            "Stats",
            parent=styles["Normal"],
            fontName=font_name,
            fontSize=9,
            leading=13,
            alignment=TA_CENTER,
            textColor=colors.HexColor("#555555"),
        )
        stats_text = (
            f"共筛选出 <b>{len(df)}</b> 只股票，"
            f"本报告展示前 <b>{max_rows}</b> 条"
        )
        elements.append(Paragraph(stats_text, stats_style))

        # 页脚
        elements.append(Spacer(1, 10 * mm))
        elements.append(Paragraph("Generated by AI Stock System", footer_style))

        return _build_pdf(elements, doc, filename, f"选股报告（{max_rows}/{len(df)} 条）")

    except Exception as e:
        print(f"{_LOG_PREFIX}[错误] PDF 生成失败: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return False


# ================================================================
# 投研分析报告 PDF 生成
# ================================================================
def generate_analysis_pdf(analysis_data: dict, filename: str = "analysis_report.pdf") -> bool:
    """将 AI 投研分析结果导出为 PDF 报告。

    Args:
        analysis_data: 投研分析结果字典（包含 conclusion, confidence, risks, evidence 等）。
        filename: 输出 PDF 文件路径。

    Returns:
        True 表示生成成功，False 表示失败。
    """
    try:
        doc, font_name, styles = _create_pdf_doc(filename)

        # 样式定义
        title_style = ParagraphStyle(
            "AnalysisTitle", parent=styles["Title"],
            fontName=font_name, fontSize=22, leading=28,
            alignment=TA_CENTER, spaceAfter=4 * mm,
            textColor=colors.HexColor("#1a1a2e"),
        )
        time_style = ParagraphStyle(
            "AnalysisTime", parent=styles["Normal"],
            fontName=font_name, fontSize=10, leading=14,
            alignment=TA_CENTER, spaceAfter=6 * mm,
            textColor=colors.HexColor("#666666"),
        )
        section_title_style = ParagraphStyle(
            "SectionTitle", parent=styles["Heading2"],
            fontName=font_name, fontSize=14, leading=18,
            spaceBefore=6 * mm, spaceAfter=3 * mm,
            textColor=colors.HexColor("#1a1a2e"),
        )
        body_style = ParagraphStyle(
            "Body", parent=styles["Normal"],
            fontName=font_name, fontSize=10, leading=15,
            spaceAfter=2 * mm, textColor=colors.HexColor("#333333"),
        )
        risk_style = ParagraphStyle(
            "Risk", parent=body_style,
            leftIndent=10, textColor=colors.HexColor("#e74c3c"),
        )
        evidence_style = ParagraphStyle(
            "Evidence", parent=body_style,
            leftIndent=10, textColor=colors.HexColor("#2563eb"),
        )
        footer_style = _build_footer_style(styles, font_name)

        elements = []

        # ---- 标题 ----
        elements.append(Paragraph("AI 投研综合分析报告", title_style))
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        elements.append(Paragraph(f"报告生成时间：{now_str}", time_style))

        # ---- 原始问题 ----
        query = analysis_data.get("query", "")
        if query:
            elements.append(Paragraph("■ 用户问题", section_title_style))
            elements.append(Paragraph(query, body_style))

        # ---- 核心结论 ----
        conclusion = analysis_data.get("conclusion", "")
        if conclusion:
            elements.append(Paragraph("■ 核心结论", section_title_style))
            elements.append(Paragraph(conclusion, body_style))

        # ---- 置信度 & 影响级别 ----
        confidence = analysis_data.get("confidence", 0)
        impact_level = analysis_data.get("impact_level", "")
        action_advice = analysis_data.get("action_advice", "")
        if confidence or impact_level or action_advice:
            elements.append(Paragraph("■ 综合评估", section_title_style))
            meta_parts = []
            if confidence:
                meta_parts.append(f"置信度: <b>{float(confidence) * 100:.0f}%</b>")
            if impact_level:
                meta_parts.append(f"影响级别: <b>{impact_level}</b>")
            if meta_parts:
                elements.append(Paragraph("  |  ".join(meta_parts), body_style))
            if action_advice:
                elements.append(Paragraph(f"操作建议: {action_advice}", body_style))

        # ---- 事件解析 ----
        event_detail = analysis_data.get("event_detail", {})
        if event_detail and event_detail.get("summary"):
            elements.append(Paragraph("■ 事件解析", section_title_style))
            elements.append(Paragraph(f"摘要: {event_detail['summary']}", body_style))
            if event_detail.get("event_type"):
                elements.append(Paragraph(f"类型: {event_detail['event_type']}", body_style))
            if event_detail.get("entities"):
                elements.append(Paragraph(f"影响主体: {', '.join(event_detail['entities'])}", body_style))

        # ---- 多维评分 ----
        multi_dim = analysis_data.get("multi_dim_scores", {})
        if multi_dim:
            elements.append(Paragraph("■ 多维推理评分", section_title_style))
            dim_labels = {
                "fundamental": "基本面",
                "capital": "资金面",
                "sentiment": "情绪面",
                "policy": "政策面",
            }
            score_rows = []
            for key, label in dim_labels.items():
                dim_data = multi_dim.get(key, {})
                if dim_data:
                    impact = dim_data.get("impact", "")
                    score = dim_data.get("score", 0)
                    detail = dim_data.get("detail", "")
                    score_rows.append([
                        Paragraph(label, body_style),
                        Paragraph(str(impact), body_style),
                        Paragraph(f"{float(score) * 100:.0f}%" if score <= 1 else f"{score}", body_style),
                    ])
            if score_rows:
                score_table = Table(
                    [[Paragraph("维度", body_style), Paragraph("影响", body_style), Paragraph("评分", body_style)]] + score_rows,
                    colWidths=[60, 60, 60],
                )
                score_table.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a1a2e")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]))
                elements.append(score_table)

        # ---- 风险提示 ----
        risks = analysis_data.get("risks", [])
        if risks:
            elements.append(Paragraph("■ 风险提示", section_title_style))
            for r in risks[:10]:
                elements.append(Paragraph(f"• {r}", risk_style))

        # ---- 证据列表 ----
        evidence_list = analysis_data.get("evidence", [])
        if evidence_list:
            elements.append(Paragraph("■ 分析证据", section_title_style))
            for ev in evidence_list[:15]:
                if isinstance(ev, dict):
                    ev_type = ev.get("type", "")
                    ev_content = ev.get("content", "")
                    elements.append(Paragraph(f"[{ev_type}] {ev_content}", evidence_style))
                elif isinstance(ev, str):
                    elements.append(Paragraph(f"• {ev}", evidence_style))

        # ---- 推理步骤 ----
        reasoning_steps = analysis_data.get("reasoning_steps", [])
        if reasoning_steps:
            elements.append(Paragraph("■ 推理过程", section_title_style))
            for i, step in enumerate(reasoning_steps[:10], 1):
                elements.append(Paragraph(f"{i}. {step}", body_style))

        # ---- 页脚 ----
        elements.append(Spacer(1, 10 * mm))
        disclaimer = analysis_data.get("disclaimer", "本内容仅供参考，不构成投资建议")
        elements.append(Paragraph(disclaimer, footer_style))
        elements.append(Paragraph("Generated by AI Stock System", footer_style))

        return _build_pdf(elements, doc, filename, "分析报告")

    except Exception as e:
        print(f"{_LOG_PREFIX}[错误] 分析报告 PDF 生成失败: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return False


# ================================================================
# 主程序：演示用法
# ================================================================
if __name__ == "__main__":
    # 构造示例数据
    demo_data = {
        "代码": ["600519", "000858", "601318", "000001", "300750"],
        "名称": ["贵州茅台", "五粮液", "中国平安", "平安银行", "宁德时代"],
        "最新价": [1850.00, 158.50, 48.20, 12.35, 215.80],
        "涨跌幅": [1.52, -0.83, 2.15, 0.45, 3.28],
    }
    demo_df = pd.DataFrame(demo_data)

    output_file = "demo_stock_report.pdf"
    success = generate_stock_pdf(demo_df, output_file)
    if success:
        print(f"演示 PDF 已生成: {output_file}")
    else:
        print("演示 PDF 生成失败")
