"""
diagnose.py - 本地环境诊断脚本
================================
用于检测：
  1. Python 版本
  2. akshare 版本及安装状态
  3. 网络连通性（东方财富数据源）
  4. akshare 核心接口是否可用
  5. 依赖库（pandas / numpy / zhipuai）是否就绪

使用方式：
  python diagnose.py
"""

import sys
import importlib
import traceback

# ================================================================
# 颜色输出（Windows CMD 支持 ANSI）
# ================================================================
def _green(msg):  return f"\033[92m{msg}\033[0m"
def _red(msg):    return f"\033[91m{msg}\033[0m"
def _yellow(msg): return f"\033[93m{msg}\033[0m"
def _bold(msg):   return f"\033[1m{msg}\033[0m"

OK   = _green("  [OK]")
FAIL = _red("  [FAIL]")
WARN = _yellow("  [WARN]")


# ================================================================
# 1. Python 版本
# ================================================================
def check_python_version():
    print(_bold("\n===== 1. Python 版本 ====="))
    ver = sys.version
    print(f"  Python: {ver}")
    if sys.version_info >= (3, 9):
        print(OK + " Python 版本 >= 3.9")
    else:
        print(FAIL + " Python 版本过低，建议 3.9+")


# ================================================================
# 2. 依赖库检查
# ================================================================
def check_dependencies():
    print(_bold("\n===== 2. 依赖库检查 ====="))
    deps = {
        "akshare":  None,       # 版本号稍后打印
        "pandas":   None,
        "numpy":    None,
        "zhipuai":  None,
        "fastapi":  None,
        "uvicorn":  None,
        "requests": None,
    }

    all_ok = True
    for lib in deps:
        try:
            mod = importlib.import_module(lib)
            ver = getattr(mod, "__version__", "未知版本")
            deps[lib] = ver
            print(OK + f" {lib} == {ver}")
        except ImportError:
            print(FAIL + f" {lib} 未安装!")
            all_ok = False

    # akshare 版本检查
    ak_ver = deps.get("akshare")
    if ak_ver and ak_ver != "未知版本":
        try:
            parts = ak_ver.split(".")
            major, minor = int(parts[0]), int(parts[1])
            if major < 1 or (major == 1 and minor < 10):
                print(WARN + f" akshare 版本 {ak_ver} 较老，建议 >= 1.10.0")
                print("       请运行: pip install --upgrade akshare")
        except (ValueError, IndexError):
            pass

    return all_ok


# ================================================================
# 3. 网络连通性测试
# ================================================================
def check_network():
    print(_bold("\n===== 3. 网络连通性测试 ====="))

    # 测试 1：DNS 解析
    import socket
    targets = [
        ("东方财富 push2his", "push2his.eastmoney.com", 443),
        ("东方财富 push2",     "push2.eastmoney.com", 443),
        ("智谱 AI",            "open.bigmodel.cn", 443),
    ]

    for label, host, port in targets:
        try:
            sock = socket.create_connection((host, port), timeout=10)
            sock.close()
            print(OK + f" {label} ({host}:{port}) 连通")
        except socket.timeout:
            print(FAIL + f" {label} ({host}:{port}) 连接超时（10s）")
        except socket.gaierror:
            print(FAIL + f" {label} ({host}:{port}) DNS 解析失败")
        except Exception as e:
            print(FAIL + f" {label} ({host}:{port}) 连接失败: {e}")

    # 测试 2：HTTP 请求
    try:
        import requests
        resp = requests.get("https://www.baidu.com", timeout=10)
        print(OK + f" HTTP 请求 baidu.com -> {resp.status_code}")
    except Exception as e:
        print(FAIL + f" HTTP 请求失败: {e}")


# ================================================================
# 4. akshare 核心接口测试
# ================================================================
def check_akshare():
    print(_bold("\n===== 4. akshare 核心接口测试 ====="))

    try:
        import akshare as ak
    except ImportError:
        print(FAIL + " akshare 未安装，请先运行: pip install akshare")
        return

    print(f"  akshare 版本: {ak.__version__}")

    # 测试 1：全市场行情
    print("\n  --- 测试 ak.stock_zh_a_spot_em() ---")
    try:
        df = ak.stock_zh_a_spot_em()
        if df is not None and not df.empty:
            print(OK + f" 成功获取 {len(df)} 条数据")
            print(f"  列名 ({len(df.columns)} 列): {list(df.columns)}")
            print(f"\n  前 3 行 x 前 3 列:")
            print(df.iloc[:3, :3].to_string())
        else:
            print(FAIL + " 返回数据为空")
    except Exception as e:
        print(FAIL + f" 调用失败: {type(e).__name__}: {e}")
        traceback.print_exc()

    # 测试 2：个股日K线
    print("\n  --- 测试 ak.stock_zh_a_daily(symbol='sz300750') ---")
    try:
        df2 = ak.stock_zh_a_daily(symbol="sz300750", adjust="qfq")
        if df2 is not None and not df2.empty:
            print(OK + f" 成功获取 {len(df2)} 条数据")
            print(f"  列名: {list(df2.columns)}")
        else:
            print(WARN + " 返回数据为空（可能是非交易时间）")
    except Exception as e:
        print(FAIL + f" 调用失败: {type(e).__name__}: {e}")

    # 测试 3：个股信息
    print("\n  --- 测试 ak.stock_individual_info_em(symbol='300750') ---")
    try:
        df3 = ak.stock_individual_info_em(symbol="300750")
        if df3 is not None and not df3.empty:
            print(OK + f" 成功获取 {len(df3)} 条信息")
            print(df3.to_string())
        else:
            print(WARN + " 返回数据为空")
    except Exception as e:
        print(FAIL + f" 调用失败: {type(e).__name__}: {e}")


# ================================================================
# 5. 环境变量检查
# ================================================================
def check_env():
    print(_bold("\n===== 5. 环境变量检查 ====="))
    import os
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    keys_to_check = ["ZHIPU_API_KEY", "DB_PASSWORD"]
    for key in keys_to_check:
        val = os.getenv(key, "")
        if val:
            print(OK + f" {key} = {val[:4]}****（已配置）")
        else:
            print(WARN + f" {key} 未配置")


# ================================================================
# 主入口
# ================================================================
if __name__ == "__main__":
    print(_bold("=" * 60))
    print(_bold("  金融新闻行情分析 Agent - 本地环境诊断工具"))
    print(_bold("=" * 60))

    check_python_version()
    check_dependencies()
    check_network()
    check_akshare()
    check_env()

    print(_bold("\n" + "=" * 60))
    print("  诊断完成！")
    print("  如果以上测试全部通过，说明环境正常。")
    print("  如果有 FAIL 项，请根据提示修复后重试。")
    print(_bold("=" * 60))
