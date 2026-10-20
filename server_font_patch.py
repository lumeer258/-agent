# -*- coding: utf-8 -*-
"""服务器补丁：升级 pdf_exporter.py 的中文字体注册逻辑。

在服务器上、pdf_exporter.py 所在目录执行：
    cp pdf_exporter.py pdf_exporter.py.bak
    python3 server_font_patch.py
"""
import re

PATH = "pdf_exporter.py"

with open(PATH, encoding="utf-8") as f:
    src = f.read()

if "_scan_cjk_fonts" in src:
    print("已经打过补丁，无需重复执行")
    raise SystemExit(0)

# ---- 1) 补充 registerFontFamily 导入 ----
if "registerFontFamily" not in src:
    old_import = "from reportlab.pdfbase import pdfmetrics"
    if old_import not in src:
        raise SystemExit("[失败] 未找到 pdfmetrics 导入行，文件结构与预期不符，请手动检查")
    src = src.replace(
        old_import,
        old_import + "\n    from reportlab.pdfbase.pdfmetrics import registerFontFamily",
        1,
    )

# ---- 2) 整体替换字体注册部分（_scan_cjk_fonts + _register_chinese_font）----
NEW_CODE = r'''def _scan_cjk_fonts(system: str) -> list:
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

    查找顺序：
      1. 环境变量 PDF_FONT_PATH 指定的字体文件（最优先）
      2. 按系统区分的常见中文字体静态路径
      3. 扫描系统字体目录按文件名关键词匹配（兜底）

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
    print(f"{_LOG_PREFIX}  可安装字体后重试，或将字体文件路径写入环境变量 PDF_FONT_PATH")
    return _FONT_NAME'''

pattern = re.compile(r"def _register_chinese_font\(.*?(?=\n# =+\n# 公共工具)", re.S)
src, n = pattern.subn(lambda m: NEW_CODE, src)
if n != 1:
    raise SystemExit(f"[失败] 函数替换匹配到 {n} 处（应为 1），请手动检查文件结构")

with open(PATH, "w", encoding="utf-8") as f:
    f.write(src)

print("[成功] 补丁已应用")
