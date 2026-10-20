@echo off
:: ============================================================
:: start.bat - 金融新闻行情分析 Agent 一键启动脚本
:: ============================================================
:: 用法: start.bat <命令>
:: 支持命令: install / start / stop / restart / status / logs / help
:: ============================================================

:: 设置 UTF-8 编码，支持中文输出
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion

:: ============================================================
:: 全局变量
:: ============================================================
set "PROJECT_NAME=金融新闻行情分析 Agent"
set "HOST=0.0.0.0"
set "PORT=8001"
set "ENTRY_FILE=main.py"
set "REQ_FILE=requirements.txt"
set "LOG_DIR=logs"
set "LOG_FILE=%LOG_DIR%\app.log"
set "DATA_DIR=data"

:: ============================================================
:: 入口：根据参数分发到对应命令
:: ============================================================
if "%~1"=="" goto :cmd_help
if /i "%~1"=="install"  goto :cmd_install
if /i "%~1"=="start"    goto :cmd_start
if /i "%~1"=="stop"     goto :cmd_stop
if /i "%~1"=="restart"  goto :cmd_restart
if /i "%~1"=="status"   goto :cmd_status
if /i "%~1"=="logs"     goto :cmd_logs
if /i "%~1"=="help"     goto :cmd_help

:: 未知参数 -> 显示错误 + 帮助
echo [91m[错误] 未知命令: %~1[0m
echo.
goto :cmd_help

:: ============================================================
:: help - 显示所有可用命令和说明
:: ============================================================
:cmd_help
echo.
echo  ============================================================
echo    %PROJECT_NAME% - 启动脚本
echo  ============================================================
echo.
echo  用法: start.bat ^<命令^>
echo.
echo  可用命令:
echo    install   安装项目依赖 (pip install -r requirements.txt)
echo    start     启动服务 (启动前自动检查环境和端口)
echo    stop      停止正在运行的服务 (终止占用端口 %PORT% 的进程)
echo    restart   重启服务 (先 stop 再 start)
echo    status    查看服务运行状态和进程 PID
echo    logs      查看运行日志文件
echo    help      显示本帮助信息
echo.
echo  快速开始:
echo    1. start.bat install    安装依赖
echo    2. 复制 .env.example 为 .env 并填入 API Key
echo    3. start.bat start      启动服务
echo    4. 浏览器打开 http://localhost:%PORT%
echo.
echo  ============================================================
exit /b 0

:: ============================================================
:: install - 安装项目依赖
:: ============================================================
:cmd_install
echo.
echo [94m[信息] 开始安装项目依赖 ...[0m
echo.

:: 检查 Python 是否已安装
python --version >nul 2>&1
if errorlevel 1 (
    echo [91m[错误] 未检测到 Python，请先安装 Python 3.8+ 并添加到 PATH[0m
    exit /b 1
)

:: 检查 requirements.txt 是否存在
if not exist "%REQ_FILE%" (
    echo [91m[错误] 未找到 %REQ_FILE% 文件[0m
    exit /b 1
)

:: 执行 pip install
pip install -r %REQ_FILE%
if errorlevel 1 (
    echo [91m[错误] 依赖安装失败，请检查网络连接或 requirements.txt 内容[0m
    exit /b 1
)

echo.
echo [92m[成功] 依赖安装完成！[0m
echo.
echo [93m[提示] 请确保已复制 .env.example 为 .env 并填入你的 API Key[0m
echo.
exit /b 0

:: ============================================================
:: start - 启动服务
:: ============================================================
:cmd_start
echo.
echo  ============================================================
echo    启动 %PROJECT_NAME%
echo  ============================================================
echo.

:: ---- 前置检查 1：Python 是否已安装 ----
echo [94m[信息] 检查 Python 环境 ...[0m
python --version >nul 2>&1
if errorlevel 1 (
    echo [91m[错误] 未检测到 Python，请先安装 Python 3.8+[0m
    exit /b 1
)
for /f "tokens=*" %%v in ('python --version 2^>^&1') do echo [92m[成功] %%v[0m

:: ---- 前置检查 2：必要文件是否存在 ----
echo [94m[信息] 检查项目文件 ...[0m
if not exist "%ENTRY_FILE%" (
    echo [91m[错误] 未找到入口文件 %ENTRY_FILE%[0m
    exit /b 1
)
echo [92m[成功] %ENTRY_FILE% 存在[0m

if not exist "%REQ_FILE%" (
    echo [91m[错误] 未找到 %REQ_FILE%[0m
    exit /b 1
)
echo [92m[成功] %REQ_FILE% 存在[0m

:: 检查 .env 文件
if not exist ".env" (
    echo [93m[提示] 未找到 .env 文件！[0m
    echo [94m[信息] 请执行: copy .env.example .env  然后填入 API Key[0m
    echo.
    set /p "CONFIRM=是否继续启动？(y/n): "
    if /i not "!CONFIRM!"=="y" (
        echo [94m[信息] 已取消启动[0m
        exit /b 0
    )
)

:: ---- 前置检查 3：端口是否被占用 ----
echo [94m[信息] 检查端口 %PORT% ...[0m
netstat -ano | findstr ":%PORT% " | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo [91m[错误] 端口 %PORT% 已被占用！[0m
    echo [94m[信息] 请先执行 start.bat stop 停止旧服务，或更换端口[0m
    exit /b 1
)
echo [92m[成功] 端口 %PORT% 可用[0m

:: ---- 自动创建必要目录 ----
echo [94m[信息] 检查目录结构 ...[0m
if not exist "%DATA_DIR%" (
    mkdir "%DATA_DIR%"
    echo [92m[成功] 已创建 %DATA_DIR%\ 目录[0m
)
if not exist "%LOG_DIR%" (
    mkdir "%LOG_DIR%"
    echo [92m[成功] 已创建 %LOG_DIR%\ 目录[0m
)

:: ---- 启动服务 ----
echo.
echo [94m[信息] 正在启动服务 ...[0m
echo [94m[信息] 前端地址: http://localhost:%PORT%[0m
echo [94m[信息] API文档:  http://localhost:%PORT%/docs[0m
echo [94m[信息] 日志文件: %LOG_FILE%[0m
echo.
echo [92m[成功] 服务启动中（按 Ctrl+C 停止）...[0m
echo  ------------------------------------------------------------
echo.

:: 运行 Python 服务（前台运行，日志同时输出到控制台和文件）
python %ENTRY_FILE% 2>&1 | tee -a "%LOG_FILE%" 2>nul || python %ENTRY_FILE%

:: 如果 python 进程退出，会执行到这里
echo.
echo [93m[提示] 服务已停止[0m
exit /b 0

:: ============================================================
:: stop - 停止服务
:: ============================================================
:cmd_stop
echo.
echo [94m[信息] 正在查找占用端口 %PORT% 的进程 ...[0m

:: 查找占用指定端口的进程 PID
set "FOUND_PID="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%PORT% " ^| findstr "LISTENING"') do (
    set "FOUND_PID=%%p"
)

if "!FOUND_PID!"=="" (
    echo [93m[提示] 端口 %PORT% 上没有正在运行的服务[0m
    exit /b 0
)

echo [94m[信息] 找到进程 PID: !FOUND_PID![0m

:: 终止该进程
taskkill /PID !FOUND_PID! /F >nul 2>&1
if errorlevel 1 (
    echo [91m[错误] 终止进程失败，请手动执行: taskkill /PID !FOUND_PID! /F[0m
    exit /b 1
)

echo [92m[成功] 已停止服务 (PID: !FOUND_PID!)[0m
echo.
exit /b 0

:: ============================================================
:: restart - 重启服务
:: ============================================================
:cmd_restart
echo.
echo [94m[信息] === 重启服务 ===[0m
echo.

:: 先停止
call :cmd_stop

:: 等待端口释放
echo [94m[信息] 等待端口释放 ...[0m
timeout /t 2 /nobreak >nul

:: 再启动
echo.
echo [94m[信息] 正在重新启动 ...[0m
goto :cmd_start

:: ============================================================
:: status - 显示服务状态
:: ============================================================
:cmd_status
echo.
echo  ============================================================
echo    %PROJECT_NAME% - 服务状态
echo  ============================================================
echo.

set "STATUS_PID="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%PORT% " ^| findstr "LISTENING"') do (
    set "STATUS_PID=%%p"
)

if "!STATUS_PID!"=="" (
    echo [91m[错误] 服务未运行[0m
    echo    端口 %PORT% 上没有监听进程
    echo.
    echo [94m[信息] 使用 start.bat start 启动服务[0m
) else (
    echo [92m[成功] 服务正在运行[0m
    echo    端口: %PORT%
    echo    PID:  !STATUS_PID!
    echo.
    echo [94m[信息] 使用 start.bat stop 停止服务[0m
)

echo.
exit /b 0

:: ============================================================
:: logs - 查看运行日志
:: ============================================================
:cmd_logs
echo.

if not exist "%LOG_FILE%" (
    echo [93m[提示] 日志文件不存在: %LOG_FILE%[0m
    echo [94m[信息] 服务启动后会自动生成日志[0m
    exit /b 0
)

echo [94m[信息] 日志文件: %LOG_FILE%[0m
echo [94m[信息] 显示最后 50 行:[0m
echo  ------------------------------------------------------------
echo.

:: 显示日志文件最后 50 行
powershell -Command "Get-Content '%LOG_FILE%' -Tail 50"

exit /b 0
