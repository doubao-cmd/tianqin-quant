# -*- coding: utf-8 -*-
"""
天勤量化 · 本地自检（不联网，几秒出结果）
==========================================================
2026-09-30 新建，属于「工作台」系统区的核心件。

【和 工具/环境自检.py 的分工】
    本文件 = 静态体检：文件齐不齐、路径通不通、模块导不进得、
             行尾对不对、git 干不干净、熔断有没有挂着。
             不登录天勤、不联网、不占席位。
    环境自检.py = 联网体检：真登录信易服务器 + 验证品种代码。
两个都绿才算环境真正就绪。

【检查项】
  1. Python 解释器与版本
  2. tqsdk 可导入（只 find_spec，不联网）
  3. 配置三件套（config_symbols / config_account / 模板）
     —— 账号只报「已填 / 未填」，绝不打印内容
  4. 核心目录结构（lib / 策略 / 工具 / 回测结果 / 台账）
  5. lib 核心模块逐个 import
  6. 行尾抽查（约定：*.py = LF，*.bat = CRLF）
  7. git 仓库状态（最近提交 + 未提交变更数）
  8. 模拟盘熔断状态（挂着 就提醒三步复位）

运行：
    python 工具/自检_本地.py
    或 工作台 → [9]
"""

import importlib.util
import os
import subprocess
import sys

根 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if 根 not in sys.path:
    sys.path.insert(0, 根)

线 = "=" * 66
通过 = 警告 = 失败 = 0


def 结果(状态, 文本):
    """状态：ok / warn / bad"""
    global 通过, 警告, 失败
    标 = {"ok": "  ✅ ", "warn": "  ⚠️ ", "bad": "  ⛔ "}[状态]
    print(f"{标}{文本}")
    if 状态 == "ok":
        通过 += 1
    elif 状态 == "warn":
        警告 += 1
    else:
        失败 += 1


def 是LF(p):
    with open(p, "rb") as f:
        d = f.read()
    return b"\r\n" not in d and b"\n" in d


def 是CRLF(p):
    with open(p, "rb") as f:
        d = f.read()
    return b"\r\n" in d


# ---------------------------------------------------------------- 1/8

def 检查_解释器():
    print("\n[1/8] Python 解释器")
    v = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    结果("ok", f"Python {v}   （{sys.executable}）")
    if sys.version_info < (3, 10):
        结果("warn", "版本偏低，项目按 3.12/3.13 开发，建议换新解释器")


def 检查_tqsdk():
    print("\n[2/8] tqsdk（只查安装，不联网）")
    spec = importlib.util.find_spec("tqsdk")
    if spec is None:
        结果("bad", "tqsdk 没装 —— 当前解释器跑不了回测/模拟盘")
        结果("warn", "处理：换 tqsdk 虚拟环境，或 pip install tqsdk")
        return
    try:
        import tqsdk
        结果("ok", f"tqsdk {tqsdk.__version__}")
    except Exception as e:
        结果("bad", f"tqsdk 导入失败：{e}")


def 检查_配置():
    print("\n[3/8] 配置文件")
    三件套 = [
        ("config_symbols.py", True),
        ("config_account.py", True),
        ("config_account_template.py", True),
    ]
    for 名, 必须 in 三件套:
        p = os.path.join(根, 名)
        if os.path.isfile(p):
            结果("ok", 名)
        elif 必须:
            结果("bad", f"{名} 缺失")
        else:
            结果("warn", f"{名} 缺失（git 用户需从模板复制）")

    # 账号是否已填：import 后看字段值（绝不打印内容）。
    # 注意不能拿「文件里有没有『填』字」判断 —— 注释里本来就带「填」。
    p = os.path.join(根, "config_account.py")
    if os.path.isfile(p):
        try:
            import config_account as _cfg
            for 字段 in ("USER", "PASSWORD", "LIVE_USER", "LIVE_PASSWORD"):
                v = str(getattr(_cfg, 字段, "")).strip()
                if not v or "填" in v:
                    结果("warn", f"config_account.{字段} 未填或仍是占位符")
                else:
                    结果("ok", f"config_account.{字段} 已填（内容不展示）")
        except Exception as e:
            结果("bad", f"config_account.py 读不了：{e}")


def 检查_目录():
    print("\n[4/8] 核心目录")
    目录表 = ["lib", "策略", "工具", "回测结果", "工具/_数据",
              "策略/06_MA13超短线/模拟盘/台账"]
    for d in 目录表:
        p = os.path.join(根, d)
        if os.path.isdir(p):
            结果("ok", d + "/")
        else:
            结果("warn", f"{d}/ 不存在（首次使用会自动生成，可忽略）")


def 检查_lib():
    print("\n[5/8] lib 核心模块导入")
    for m in ["lib.engine", "lib.paths", "lib.portfolio",
              "lib.recycle", "lib.account_ledger", "lib.strategy_dirs"]:
        try:
            __import__(m)
            结果("ok", m)
        except Exception as e:
            结果("bad", f"{m} 导入失败：{type(e).__name__}: {e}")


def 检查_行尾():
    print("\n[6/8] 行尾抽查（约定：*.py = LF，*.bat = CRLF）")
    lf文件 = ["lib/engine.py", "lib/paths.py", "工具/菜单.py", "config_symbols.py"]
    crlf文件 = ["跑回测.bat",
                "策略/06_MA13超短线/模拟盘/启动_P_EB_观察模式.bat"]
    for f in lf文件:
        p = os.path.join(根, f)
        if not os.path.isfile(p):
            结果("warn", f"{f} 不存在，跳过")
        elif 是LF(p):
            结果("ok", f"{f} = LF")
        else:
            结果("bad", f"{f} 是 CRLF，应为 LF —— 跑 工具/行尾归一.py")
    for f in crlf文件:
        p = os.path.join(根, f)
        if not os.path.isfile(p):
            结果("warn", f"{f} 不存在，跳过")
        elif 是CRLF(p):
            结果("ok", f"{f} = CRLF")
        else:
            结果("bad", f"{f} 是 LF，应为 CRLF —— cmd 的 goto 在 LF 下可能失灵")


def 检查_git():
    print("\n[7/8] git 仓库")
    if not os.path.isdir(os.path.join(根, ".git")):
        结果("warn", "不是 git 仓库（作品集基座还没建）")
        return
    try:
        r = subprocess.run(
            ["git", "-C", 根, "log", "-1", "--oneline"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace")
        if r.returncode == 0:
            结果("ok", f"最近提交：{r.stdout.strip()}")
        else:
            结果("warn", "git log 失败（仓库可能为空）")
        r2 = subprocess.run(
            ["git", "-C", 根, "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace")
        行 = [x for x in r2.stdout.splitlines() if x.strip()]
        if not 行:
            结果("ok", "工作区干净，没有未提交变更")
        else:
            结果("warn", f"有 {len(行)} 处未提交变更（前 5 条）：")
            for x in 行[:5]:
                print(f"        {x}")
    except FileNotFoundError:
        结果("warn", "git 命令不存在（装 Git for Windows 后重试）")
    except Exception as e:
        结果("warn", f"git 检查异常：{e}")


def 检查_熔断():
    print("\n[8/8] 模拟盘熔断状态")
    p = os.path.join(根, "策略", "06_MA13超短线", "模拟盘", "台账", "熔断状态.json")
    if os.path.isfile(p):
        结果("bad", "熔断状态.json 还挂着！上次会话触发过回撤熔断")
        print("        复位三步：①关掉模拟盘窗口 ②删除该文件 ③重新启动")
        print(f"        文件：{p}")
    else:
        结果("ok", "无熔断挂起")


# ---------------------------------------------------------------- 主入口

def main():
    print(线)
    print(" 天勤量化 · 本地自检（不联网、不占天勤席位）")
    print(线)

    检查_解释器()
    检查_tqsdk()
    检查_配置()
    检查_目录()
    检查_lib()
    检查_行尾()
    检查_git()
    检查_熔断()

    print("\n" + 线)
    print(f" 汇总：通过 {通过} ｜ 警告 {警告} ｜ 失败 {失败}")
    if 失败:
        print(" 有失败项 —— 先照提示处理，再来跑回测/模拟盘。")
    elif 警告:
        print(" 大体就绪，警告项不影响当下使用。")
    else:
        print(" 全绿。环境就绪，可以开工。")
    print(线)
    return 1 if 失败 else 0


if __name__ == "__main__":
    sys.exit(main())
