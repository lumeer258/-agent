"""
main.py - 金融新闻行情分析 Agent FastAPI 接口层
================================================
职责：
  - 初始化 FastAPI 应用，配置 CORS 和请求日志中间件
  - 定义 API 接口（分析、选股、K线、基本面、PDF导出、用户认证）
  - 全局异常处理，返回统一错误格式
  - uvicorn 启动入口
"""
import os
import time
import traceback
from typing import Any, Dict, List
from datetime import datetime, timedelta

import akshare as ak
import pandas as pd
import uvicorn
from fastapi import FastAPI, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# SQLAlchemy 相关
from sqlalchemy import create_engine, Column, Integer, String, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker, Session

# 密码加密与 JWT
from passlib.context import CryptContext
from jose import jwt

# 业务模块
from agent_service import AnalyzeRequest, AnalyzeResponse, run_agent
from storage import save_analysis_result, get_db_connection
from fundamental_analyzer import analyze_fundamental
from selector import ai_select_stocks
from pdf_exporter import generate_stock_pdf, generate_analysis_pdf

# ================================================================
# 常量配置
# ================================================================
_LOG_PREFIX = "【API服务】"

# JWT 配置（生产环境请使用环境变量存储 SECRET_KEY）
SECRET_KEY = "your-secret-key-change-in-production-2024-finance-agent"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # Token 有效期 24 小时

# 密码加密上下文
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# SQLAlchemy 数据库配置（SQLite）
SQLALCHEMY_DATABASE_URL = "sqlite:///./users.db"
engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# ================================================================
# 数据库模型
# ================================================================
class User(Base):
    """用户表模型。"""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    username = Column(String(50), unique=True, index=True, nullable=False)
    hashed_password = Column(String(128), nullable=False)
    created_at = Column(DateTime, default=datetime.now)


# 启动时自动建表
Base.metadata.create_all(bind=engine)


# ================================================================
# Pydantic 请求模型
# ================================================================
class RegisterRequest(BaseModel):
    """注册请求体。"""
    username: str
    password: str
    confirm_password: str


class LoginRequest(BaseModel):
    """登录请求体。"""
    username: str
    password: str


class ExportPdfRequest(BaseModel):
    """PDF 导出请求体。"""
    stocks: list  # 选股结果股票列表


class SelectRequest(BaseModel):
    """AI 选股请求体。"""
    query: str                      # 自然语言选股需求
    max_stocks: int = 10            # 最多返回几只股票


# ================================================================
# 认证工具函数
# ================================================================
def verify_password(plain_password: str, hashed_password: str) -> bool:
    """校验明文密码与哈希密码是否匹配。"""
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    """将明文密码加密为哈希密码。"""
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: timedelta = None) -> str:
    """生成 JWT Token。

    Args:
        data: 要编码到 Token 中的数据（通常包含 sub 字段）。
        expires_delta: Token 过期时间增量。

    Returns:
        编码后的 JWT Token 字符串。
    """
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def get_db():
    """获取数据库会话的依赖函数（用于 FastAPI Depends）。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ================================================================
# 应用初始化
# ================================================================
app = FastAPI(
    title="金融新闻行情分析 Agent",
    description="基于智谱大模型的金融新闻深度投研 API 服务",
    version="1.0.0",
)

# ================================================================
# CORS 中间件：允许跨域请求（前端与后端端口不同时需要）
# ================================================================
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # 生产环境应限制为具体域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ================================================================
# 请求日志中间件：记录每次请求的耗时与状态
# ================================================================
@app.middleware("http")
async def request_log_middleware(request: Request, call_next):
    """记录每个 HTTP 请求的方法、路径、状态码和耗时。

    Args:
        request: 传入的 HTTP 请求。
        call_next: 下一个中间件或路由处理器。

    Returns:
        响应对象，在响应头中附加耗时信息。
    """
    start = time.time()
    method = request.method
    path = request.url.path

    print(f"{_LOG_PREFIX}--> {method} {path} 开始处理")

    try:
        response = await call_next(request)
        duration_ms = int((time.time() - start) * 1000)
        status = response.status_code

        print(f"{_LOG_PREFIX}<-- {method} {path} {status} ({duration_ms}ms)")

        # 在响应头中附加耗时
        response.headers["X-Process-Time-Ms"] = str(duration_ms)
        return response

    except Exception as e:
        duration_ms = int((time.time() - start) * 1000)
        print(f"{_LOG_PREFIX}<-- {method} {path} 500 ({duration_ms}ms) 异常: {e}")
        raise


# ================================================================
# 全局异常处理器
# ================================================================
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """捕获所有未处理的异常，返回统一格式的错误响应。

    Args:
        request: 触发异常的请求。
        exc: 异常对象。

    Returns:
        统一格式的 JSON 错误响应。
    """
    error_detail = str(exc)
    tb = traceback.format_exc()

    print(f"{_LOG_PREFIX}[全局异常] {request.method} {request.url.path}")
    print(f"{_LOG_PREFIX}[全局异常] 类型: {type(exc).__name__}")
    print(f"{_LOG_PREFIX}[全局异常] 详情: {error_detail}")
    print(f"{_LOG_PREFIX}[全局异常] 堆栈:\n{tb}")

    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": {
                "type": type(exc).__name__,
                "message": error_detail,
                "path": str(request.url.path),
            },
        },
    )


# ================================================================
# 接口定义
# ================================================================
@app.post(
    "/api/analyze",
    response_model=AnalyzeResponse,
    summary="金融新闻深度投研分析",
    description="接收用户问题，执行意图识别、数据获取、七阶段投研分析，返回结构化投研简报。",
)
async def analyze(request: AnalyzeRequest) -> AnalyzeResponse:
    """金融新闻深度投研分析接口。

    接收用户问题，编排调用新闻获取、行情获取、七阶段投研分析流水线，
    返回包含结论、证据、思考日志的完整分析结果。

    Args:
        request: 分析请求，包含 query（问题）、stock_code（股票代码）、time_range（时间范围）。

    Returns:
        完整的分析响应，包含结论、置信度、证据、风险提示、思考日志等。
    """
    print(f"{_LOG_PREFIX}[POST /api/analyze] query='{request.query}' "
          f"stock_code={request.stock_code} time_range={request.time_range}")

    result = await run_agent(
        query=request.query,
        stock_code=request.stock_code,
        time_range=request.time_range or "6m",
    )

    print(f"{_LOG_PREFIX}[POST /api/analyze] 完成: "
          f"conclusion='{result.conclusion[:50]}...' "
          f"confidence={result.confidence:.0%} "
          f"evidence={len(result.evidence)} "
          f"steps={len(result.thinking_log)}")

    # ============================================================
    # 将分析结果写入 MySQL 数据库（异常不影响前端返回）
    # ============================================================
    try:
        db_data = {
            "query_text": request.query,
            "stock_code": request.stock_code or "",
            "time_range": request.time_range or "6m",
            "core_conclusion": result.conclusion,
            "confidence_score": result.confidence,
            "impact_level": result.impact_level or "",
            "final_judgement": result.reflection_detail.final_sentiment
                if result.reflection_detail else "",
            "related_tags": result.sub_tasks or [],
        }
        save_success = save_analysis_result(db_data)
        if save_success:
            print(f"{_LOG_PREFIX}[数据库] 分析结果写入 MySQL 成功")
        else:
            print(f"{_LOG_PREFIX}[数据库] 分析结果写入 MySQL 失败（返回 False）")
    except Exception as db_err:
        print(f"{_LOG_PREFIX}[数据库] 写入异常（不影响前端返回）: {db_err}")

    return result


@app.get(
    "/health",
    summary="健康检查",
    description="检查服务是否正常运行。",
)
async def health() -> Dict[str, Any]:
    """健康检查接口。

    Returns:
        包含服务状态信息的字典。
    """
    return {
        "status": "ok",
        "service": "金融新闻行情分析 Agent",
        "version": "1.0.0",
    }

@app.get("/api/history", summary="获取历史分析记录")
async def get_analysis_history():
    """获取数据库中的所有历史分析记录（按时间倒序）"""
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            sql = (
                "SELECT query_text, stock_code, core_conclusion, "
                "impact_level, final_judgement "
                "FROM ai_analysis_results ORDER BY id DESC"
            )
            cursor.execute(sql)
            rows = cursor.fetchall()

        conn.close()
        return {"code": 200, "data": rows}

    except Exception as e:
        print(f"{_LOG_PREFIX}查询历史数据出错: {e}")
        return {"code": 500, "message": "服务器查询历史数据出错"}


@app.get("/api/kline", summary="获取股票K线数据（含均线与成交量）")
async def get_kline(stock_code: str = "300750", days: int = 180) -> Dict[str, Any]:
    """获取指定股票的日线K线数据 + 成交量 + MA均线，供前端 Lightweight Charts 渲染。

    Args:
        stock_code: 6 位股票代码，如 '300750'（宁德时代）。
        days: 返回最近多少条数据，默认 180 条。

    Returns:
        {"code": 200, "data": [{"time", "open", "close", "high", "low",
                                  "volume", "ma5", "ma10", "ma20"}, ...]}
    """
    try:
        print(f"{_LOG_PREFIX}[K线] 获取 {stock_code} 近 {days} 条日线数据 ...")

        # ---------- 1. 确定交易所前缀，获取日线数据（前复权） ----------
        prefix = "sh" if stock_code.startswith("6") else "sz"
        sina_symbol = f"{prefix}{stock_code}"
        df = ak.stock_zh_a_daily(symbol=sina_symbol, adjust="qfq")

        # ---------- 2. 标准化列名（兼容中英文） ----------
        col_map = {
            "date": "date", "日期": "date",
            "open": "open", "开盘": "open", "开盘价": "open",
            "close": "close", "收盘": "close", "收盘价": "close",
            "high": "high", "最高": "high", "最高价": "high",
            "low": "low", "最低": "low", "最低价": "low",
            "volume": "volume", "成交量": "volume",
        }
        df = df.rename(columns={c: col_map.get(c, c) for c in df.columns})

        # ---------- 3. 计算简单移动均线（MA） ----------
        #   多取一些前置数据用于均线预热，避免前 N 条为 null
        preheat = 20  # MA20 需要前置 19 条预热
        df = df.tail(days + preheat).copy()

        df["ma5"]  = df["close"].rolling(window=5).mean()
        df["ma10"] = df["close"].rolling(window=10).mean()
        df["ma20"] = df["close"].rolling(window=20).mean()

        # 去掉预热期，只保留最近 days 条
        df = df.tail(days).copy()

        # ---------- 4. 格式化为 JSON ----------
        # 均线值（NaN → null）—— 定义在循环外避免重复创建
        def _safe_round(val):
            return round(float(val), 2) if pd.notna(val) else None

        kline_data: List[Dict[str, Any]] = []
        for _, row in df.iterrows():
            t = row["date"]
            time_str = t.strftime("%Y-%m-%d") if isinstance(t, pd.Timestamp) else str(t)[:10]
            vol = row.get("volume", 0)
            vol_val = int(float(vol)) if pd.notna(vol) else 0

            kline_data.append({
                "time":   time_str,
                "open":   round(float(row["open"]), 2),
                "close":  round(float(row["close"]), 2),
                "high":   round(float(row["high"]), 2),
                "low":    round(float(row["low"]), 2),
                "volume": vol_val,
                "ma5":    _safe_round(row["ma5"]),
                "ma10":   _safe_round(row["ma10"]),
                "ma20":   _safe_round(row["ma20"]),
            })

        print(f"{_LOG_PREFIX}[K线] 成功: {len(kline_data)} 条记录")
        return {"code": 200, "data": kline_data}

    except Exception as e:
        print(f"{_LOG_PREFIX}[K线] 获取失败: {e}")
        return {"code": 500, "msg": f"获取K线数据失败: {e}"}


@app.get("/api/fundamental", summary="获取个股基本面分析数据")
async def get_fundamental(stock_code: str = "300750") -> Dict[str, Any]:
    """获取指定股票的基本面分析数据（盈利能力、成长性、财务健康、估值）。

    Args:
        stock_code: 6 位股票代码，如 '300750'。

    Returns:
        {"code": 200, "data": {"symbol", "name", "profitability", "growth", "health", "valuation"}}
    """
    try:
        print(f"{_LOG_PREFIX}[基本面] 获取 {stock_code} 基本面数据 ...")
        result = analyze_fundamental(stock_code)
        print(f"{_LOG_PREFIX}[基本面] 成功: {stock_code}")
        return result
    except Exception as e:
        print(f"{_LOG_PREFIX}[基本面] 获取失败: {e}")
        return {"code": 500, "msg": f"获取基本面数据失败: {e}"}


# ================================================================
# AI 智能选股接口
# ================================================================
@app.post("/api/ai-select", summary="AI 自然语言智能选股")
async def ai_select(request: SelectRequest) -> Dict[str, Any]:
    """自然语言交互式选股接口。

    接收用户的自然语言选股需求，调用 LLM 生成 Python 代码并执行，
    返回选股结果及六维雷达图评分。

    Args:
        request: 选股请求，包含 query（自然语言需求）和 max_stocks（最大返回数量）。

    Returns:
        {"code": 200, "data": {"query", "generated_code", "stocks", "error"}}
    """
    try:
        print(f"{_LOG_PREFIX}[AI选股] query='{request.query[:60]}...' max={request.max_stocks}")

        result = await ai_select_stocks(
            query=request.query,
            max_stocks=request.max_stocks,
        )

        print(f"{_LOG_PREFIX}[AI选股] 完成: 选出 {len(result.get('stocks', []))} 只股票")
        return {"code": 200, "data": result}

    except Exception as e:
        print(f"{_LOG_PREFIX}[AI选股] 失败: {e}")
        traceback.print_exc()
        return {
            "code": 500,
            "data": {
                "query": request.query,
                "generated_code": "",
                "stocks": [],
                "error": f"选股失败: {e}",
            },
        }


# ================================================================
# PDF 导出接口（选股报告 + 投研分析报告）
# ================================================================

def _build_pdf_response(generator_func, data, filename: str, download_name: str):
    """PDF 导出的公共逻辑：调用生成器 → 返回 FileResponse 或错误。

    Args:
        generator_func: PDF 生成函数（generate_stock_pdf 或 generate_analysis_pdf）。
        data: 传给生成函数的数据。
        filename: 服务器上的临时文件名。
        download_name: 浏览器下载时的文件名。

    Returns:
        FileResponse 或错误字典。
    """
    try:
        filepath = os.path.join(".", filename)
        success = generator_func(data, filepath)
        if not success:
            return {"code": 500, "message": "PDF 生成失败，请查看控制台日志"}
        print(f"{_LOG_PREFIX}[PDF导出] 成功: {filepath}")
        return FileResponse(filepath, media_type="application/pdf", filename=download_name)
    except Exception as e:
        print(f"{_LOG_PREFIX}[PDF导出] 失败: {e}")
        traceback.print_exc()
        return {"code": 500, "message": f"PDF 导出失败: {e}"}


@app.post("/api/export-pdf", summary="导出选股结果为 PDF")
async def export_pdf(request: ExportPdfRequest):
    """将选股结果导出为 PDF 文件并返回下载。"""
    stocks = request.stocks
    if not stocks:
        return {"code": 400, "message": "没有可导出的选股数据"}

    rows = [{
        "代码": s.get("code", ""),
        "名称": s.get("name", ""),
        "最新价": s.get("price", ""),
        "涨跌幅": s.get("extra", {}).get("涨跌幅", ""),
        "涨跌额": s.get("extra", {}).get("涨跌额", ""),
        "成交量": s.get("extra", {}).get("成交量", ""),
        "成交额": s.get("extra", {}).get("成交额", ""),
    } for s in stocks]
    df = pd.DataFrame(rows)
    return _build_pdf_response(generate_stock_pdf, df, "stock_report.pdf", "AI选股结果报告.pdf")


@app.post("/api/export-analysis-pdf", summary="导出投研分析报告为 PDF")
async def export_analysis_pdf(request: Request):
    """将投研分析结果导出为 PDF 文件并返回下载。"""
    analysis_data = await request.json()
    if not analysis_data:
        return {"code": 400, "message": "没有可导出的分析数据"}
    return _build_pdf_response(generate_analysis_pdf, analysis_data, "analysis_report.pdf", "AI投研分析报告.pdf")


# ================================================================
# 用户注册与登录接口
# ================================================================
@app.get("/", include_in_schema=False)
async def root_redirect():
    """根路径重定向到登录页。"""
    return RedirectResponse(url="/login.html")


@app.post("/api/register", summary="用户注册")
async def register(request: RegisterRequest, db: Session = Depends(get_db)):
    """用户注册接口。

    接收用户名、密码和确认密码，校验后加密存储到数据库。

    Args:
        request: 注册请求，包含 username、password、confirm_password。
        db: 数据库会话（自动注入）。

    Returns:
        注册结果 JSON。
    """
    try:
        username = request.username.strip()
        password = request.password
        confirm_password = request.confirm_password

        # 参数校验
        if not username or not password:
            return {"success": False, "message": "用户名和密码不能为空"}
        if len(username) < 2 or len(username) > 50:
            return {"success": False, "message": "用户名长度需在 2-50 个字符之间"}
        if len(password) < 6:
            return {"success": False, "message": "密码长度不能少于 6 位"}
        if password != confirm_password:
            return {"success": False, "message": "两次输入的密码不一致"}

        # 检查用户名是否已存在
        existing_user = db.query(User).filter(User.username == username).first()
        if existing_user:
            return {"success": False, "message": "用户名已存在"}

        # 创建新用户（密码 bcrypt 加密存储）
        hashed_password = get_password_hash(password)
        new_user = User(
            username=username,
            hashed_password=hashed_password,
        )
        db.add(new_user)
        db.commit()

        print(f"{_LOG_PREFIX}[注册] 新用户注册成功: {username}")
        return {"success": True, "message": "注册成功"}

    except Exception as e:
        print(f"{_LOG_PREFIX}[注册] 异常: {e}")
        traceback.print_exc()
        return {"success": False, "message": "注册失败，服务器异常，请稍后重试"}


@app.post("/api/login", summary="用户登录")
async def login(request: LoginRequest, db: Session = Depends(get_db)):
    """用户登录接口。

    接收用户名和密码，从数据库查询用户并用 bcrypt 校验密码，
    校验成功后生成 JWT Token 返回。
    登录失败统一返回"账号或密码错误"，不暴露用户是否存在。

    Args:
        request: 登录请求，包含 username 和 password。
        db: 数据库会话（自动注入）。

    Returns:
        登录结果 JSON，成功时包含 token 字段。
    """
    try:
        username = request.username.strip()
        password = request.password

        # 参数校验
        if not username or not password:
            return {"success": False, "message": "账号或密码错误"}

        # 查询用户
        user = db.query(User).filter(User.username == username).first()
        if not user:
            print(f"{_LOG_PREFIX}[登录] 登录失败（用户不存在）: {username}")
            return {"success": False, "message": "账号或密码错误"}

        # 校验密码（bcrypt）
        if not verify_password(password, user.hashed_password):
            print(f"{_LOG_PREFIX}[登录] 登录失败（密码错误）: {username}")
            return {"success": False, "message": "账号或密码错误"}

        # 生成 JWT Token（有效期 24 小时）
        access_token = create_access_token(data={"sub": username})

        print(f"{_LOG_PREFIX}[登录] 用户登录成功: {username}")
        return {
            "success": True,
            "message": "登录成功",
            "token": access_token,
        }

    except Exception as e:
        print(f"{_LOG_PREFIX}[登录] 异常: {e}")
        traceback.print_exc()
        return {"success": False, "message": "登录失败，服务器异常，请稍后重试"}


# ================================================================
# 静态文件挂载（必须放在所有 API 路由之后，否则会拦截 POST 请求导致 405）
# ================================================================
app.mount("/", StaticFiles(directory=".", html=True), name="static")


# ================================================================
# 启动入口
# ================================================================
if __name__ == "__main__":
    print(f"{_LOG_PREFIX}启动金融新闻行情分析 Agent API 服务 ...")
    print(f"{_LOG_PREFIX}前端地址: http://localhost:8001")
    print(f"{_LOG_PREFIX}API文档:  http://localhost:8001/docs")
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8001,
        reload=False,
    )
