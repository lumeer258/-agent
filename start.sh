#!/usr/bin/env bash
# ============================================================
# start.sh - 金融新闻行情分析 Agent 一键启动脚本 (Ubuntu/Linux)
# ============================================================
# 用法: bash start.sh <命令>
# 支持命令: install / start / stop / restart / status / logs / help
# ============================================================

set -e

# ============================================================
# 全局变量
# ============================================================
PROJECT_NAME="金融新闻行情分析 Agent"
HOST="0.0.0.0"
PORT=8001
ENTRY_FILE="main.py"
REQ_FILE="requirements.txt"
LOG_DIR="logs"
LOG_FILE="${LOG_DIR}/app.log"
DATA_DIR="data"
PID_FILE=".app.pid"

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# ============================================================
# 工具函数
# ============================================================
log_info()  { echo -e "${BLUE}[信息]${NC} $1"; }
log_ok()    { echo -e "${GREEN}[成功]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[提示]${NC} $1"; }
log_error() { echo -e "${RED}[错误]${NC} $1"; }

# 获取占用端口的进程 PID
get_port_pid() {
    lsof -ti :${PORT} 2>/dev/null || ss -tlnp "sport = :${PORT}" 2>/dev/null | grep -oP 'pid=\K\d+' | head -1
}

# ============================================================
# help - 显示所有可用命令和说明
# ============================================================
cmd_help() {
    echo ""
    echo " ============================================================"
    echo "   ${PROJECT_NAME} - 启动脚本 (Ubuntu/Linux)"
    echo " ============================================================"
    echo ""
    echo "  用法: bash start.sh <命令>"
    echo ""
    echo "  可用命令:"
    echo "    install   安装项目依赖 (pip install -r requirements.txt)"
    echo "    start     启动服务 (启动前自动检查环境和端口)"
    echo "    stop      停止正在运行的服务"
    echo "    restart   重启服务 (先 stop 再 start)"
    echo "    status    查看服务运行状态和进程 PID"
    echo "    logs      查看运行日志文件"
    echo "    help      显示本帮助信息"
    echo ""
    echo "  快速开始:"
    echo "    1. bash start.sh install    安装依赖"
    echo "    2. cp .env.example .env     然后填入 API Key"
    echo "    3. bash start.sh start      启动服务"
    echo "    4. 浏览器打开 http://<服务器IP>:${PORT}"
    echo ""
    echo "  后台运行:"
    echo "    nohup bash start.sh start > /dev/null 2>&1 &"
    echo ""
    echo " ============================================================"
}

# ============================================================
# install - 安装项目依赖
# ============================================================
cmd_install() {
    echo ""
    log_info "开始安装项目依赖 ..."
    echo ""

    # 检查 Python
    if ! command -v python3 &>/dev/null; then
        log_error "未检测到 Python3，请执行: sudo apt install python3 python3-pip"
        exit 1
    fi
    python3 --version

    # 检查 requirements.txt
    if [[ ! -f "${REQ_FILE}" ]]; then
        log_error "未找到 ${REQ_FILE} 文件"
        exit 1
    fi

    # 执行安装
    pip3 install -r "${REQ_FILE}"
    log_ok "依赖安装完成！"
    echo ""
    log_warn "请确保已复制 .env.example 为 .env 并填入你的 API Key"
}

# ============================================================
# start - 启动服务
# ============================================================
cmd_start() {
    echo ""
    echo " ============================================================"
    echo "   启动 ${PROJECT_NAME}"
    echo " ============================================================"
    echo ""

    # 前置检查 1: Python
    log_info "检查 Python 环境 ..."
    if ! command -v python3 &>/dev/null; then
        log_error "未检测到 Python3，请先安装"
        exit 1
    fi
    log_ok "$(python3 --version)"

    # 前置检查 2: 必要文件
    log_info "检查项目文件 ..."
    if [[ ! -f "${ENTRY_FILE}" ]]; then
        log_error "未找到入口文件 ${ENTRY_FILE}"
        exit 1
    fi
    log_ok "${ENTRY_FILE} 存在"

    if [[ ! -f "${REQ_FILE}" ]]; then
        log_error "未找到 ${REQ_FILE}"
        exit 1
    fi

    # 检查 .env
    if [[ ! -f ".env" ]]; then
        log_warn "未找到 .env 文件！"
        log_info "请执行: cp .env.example .env  然后填入 API Key"
        echo ""
        read -p "是否继续启动？(y/n): " confirm
        if [[ "${confirm}" != "y" ]]; then
            log_info "已取消启动"
            exit 0
        fi
    fi

    # 前置检查 3: 端口
    log_info "检查端口 ${PORT} ..."
    local pid
    pid=$(get_port_pid)
    if [[ -n "${pid}" ]]; then
        log_error "端口 ${PORT} 已被占用！(PID: ${pid})"
        log_info "请先执行: bash start.sh stop"
        exit 1
    fi
    log_ok "端口 ${PORT} 可用"

    # 创建必要目录
    mkdir -p "${DATA_DIR}" "${LOG_DIR}"

    # 启动服务
    echo ""
    log_info "正在启动服务 ..."
    log_info "前端地址: http://0.0.0.0:${PORT}"
    log_info "API文档:  http://0.0.0.0:${PORT}/docs"
    log_info "日志文件: ${LOG_FILE}"
    echo ""
    log_ok "服务启动中（按 Ctrl+C 停止）..."
    echo " ------------------------------------------------------------"
    echo ""

    # 前台运行，日志同时输出到控制台和文件
    python3 "${ENTRY_FILE}" 2>&1 | tee -a "${LOG_FILE}"
}

# ============================================================
# stop - 停止服务
# ============================================================
cmd_stop() {
    echo ""
    log_info "正在查找占用端口 ${PORT} 的进程 ..."

    local pid
    pid=$(get_port_pid)

    if [[ -z "${pid}" ]]; then
        log_warn "端口 ${PORT} 上没有正在运行的服务"
        return 0
    fi

    log_info "找到进程 PID: ${pid}"
    kill "${pid}" 2>/dev/null

    # 等待进程退出（最多 5 秒）
    for i in $(seq 1 10); do
        if ! kill -0 "${pid}" 2>/dev/null; then
            break
        fi
        sleep 0.5
    done

    # 如果还没退出，强制杀
    if kill -0 "${pid}" 2>/dev/null; then
        log_warn "进程未响应 SIGTERM，强制终止 ..."
        kill -9 "${pid}" 2>/dev/null
    fi

    log_ok "已停止服务 (PID: ${pid})"
    echo ""
}

# ============================================================
# restart - 重启服务
# ============================================================
cmd_restart() {
    echo ""
    log_info "=== 重启服务 ==="
    cmd_stop
    log_info "等待端口释放 ..."
    sleep 2
    cmd_start
}

# ============================================================
# status - 显示服务状态
# ============================================================
cmd_status() {
    echo ""
    echo " ============================================================"
    echo "   ${PROJECT_NAME} - 服务状态"
    echo " ============================================================"
    echo ""

    local pid
    pid=$(get_port_pid)

    if [[ -z "${pid}" ]]; then
        log_error "服务未运行"
        echo "   端口 ${PORT} 上没有监听进程"
        echo ""
        log_info "使用 bash start.sh start 启动服务"
    else
        log_ok "服务正在运行"
        echo "   端口: ${PORT}"
        echo "   PID:  ${pid}"
        echo ""
        log_info "使用 bash start.sh stop 停止服务"
    fi

    echo ""
}

# ============================================================
# logs - 查看运行日志
# ============================================================
cmd_logs() {
    echo ""

    if [[ ! -f "${LOG_FILE}" ]]; then
        log_warn "日志文件不存在: ${LOG_FILE}"
        log_info "服务启动后会自动生成日志"
        return 0
    fi

    log_info "日志文件: ${LOG_FILE}"
    log_info "显示最后 50 行:"
    echo " ------------------------------------------------------------"
    echo ""

    tail -n 50 "${LOG_FILE}"
}

# ============================================================
# 入口：根据参数分发到对应命令
# ============================================================
case "${1:-}" in
    install)  cmd_install  ;;
    start)    cmd_start    ;;
    stop)     cmd_stop     ;;
    restart)  cmd_restart  ;;
    status)   cmd_status   ;;
    logs)     cmd_logs     ;;
    help|"")  cmd_help     ;;
    *)
        log_error "未知命令: $1"
        echo ""
        cmd_help
        exit 1
        ;;
esac
