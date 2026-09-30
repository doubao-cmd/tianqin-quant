# -*- coding: utf-8 -*-
"""
天勤量化 · 工作台（总入口）
==========================================================
2026-09-30 新建。定位：像金字塔那样「双击打开就是一个软件」的总壳。

【设计原则】
    本文件不实现任何业务逻辑，只把散落的入口收拢成五个区块。
    回测扫描/执行逻辑原样复用 工具/菜单.py 的函数（import 菜单），
    以后菜单改了这里自动跟着变，不存在两份逻辑。

【命令行直通】（不想进菜单时）
    python 工具/工作台.py 自检        # 只跑本地自检，几秒出结果
    python 工具/工作台.py 日报        # 直接出模拟盘台账日报

【区块】
    [回测]     选策略选品种跑回测 / 打开策略与结果目录
    [模拟盘]   启动观察模式 / 启动守护 / 台账日报 / 台账目录
    [体检数据] 05/07 体检 / 行情缓存目录
    [系统]     本地自检 / 联网环境自检 / git 状态 / 目录地图
"""

import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
根 = os.path.dirname(_HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
if 根 not in sys.path:
    sys.path.insert(0, 根)

PY = sys.executable
线 = "=" * 66

# 模拟盘三件（集中定义，路径改一处即可）
模拟盘目录 = os.path.join(根, "策略", "06_MA13超短线", "模拟盘")
BAT_观察 = os.path.join(模拟盘目录, "启动_P_EB_观察模式.bat")
PY_守护 = os.path.join(模拟盘目录, "模拟盘守护.py")
PY_日报 = os.path.join(_HERE, "台账日报.py")
PY_自检本地 = os.path.join(_HERE, "自检_本地.py")
PY_环境自检 = os.path.join(_HERE, "环境自检.py")
PY_体检0507 = os.path.join(_HERE, "体检_05_07.py")
JSON_体检0507 = os.path.join(_HERE, "体检_05_07.json")

import 菜单  # noqa: E402  （复用回测菜单的全部选项函数）


# ---------------------------------------------------------------- 小工具

def _暂停():
    input("\n  回车返回工作台 ...")


def _开目录(相对):
    p = os.path.join(根, 相对)
    if os.path.isdir(p):
        os.startfile(p)
        print(f"\n  已打开：{p}")
    else:
        print(f"\n  ⛔ 目录不存在：{p}")


def _开文件(p):
    if os.path.isfile(p):
        os.startfile(p)
        print(f"\n  已打开：{p}")
    else:
        print(f"\n  ⛔ 文件不存在：{p}")


def _跑脚本(py路径, 额外参数=None, cwd=None):
    """在当前控制台里跑一个工具脚本，跑完暂停。"""
    if not os.path.isfile(py路径):
        print(f"\n  ⛔ 脚本不存在：{py路径}")
        _暂停()
        return
    参数 = [PY, py路径] + (额外参数 or [])
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    print("\n" + 线)
    print(f"  执行：python {os.path.relpath(py路径, 根)} {' '.join(额外参数 or [])}")
    print(线 + "\n")
    try:
        subprocess.call(参数, cwd=cwd or 根, env=env)
    except KeyboardInterrupt:
        print("\n  —— 已中断 ——")
    _暂停()


# ---------------------------------------------------------------- 回测区

def 区_回测():
    while True:
        print(f"""

  ── 回测 ─────────────────────────────
    [1] 选策略 + 选品种 → 跑回测
    [2] 打开 策略/ 目录
    [3] 打开 回测结果/ 目录
    [回车] 返回工作台""")
        c = input("  请输入：").strip()
        if c == "1":
            菜单.选项_跑回测()
        elif c == "2":
            _开目录("策略")
        elif c == "3":
            _开目录("回测结果")
        else:
            return


# ---------------------------------------------------------------- 模拟盘区

def 区_模拟盘():
    while True:
        台账目录 = os.path.join(模拟盘目录, "台账")
        熔断挂着 = os.path.isfile(os.path.join(台账目录, "熔断状态.json"))
        提示 = "  ⚠️ 熔断挂着！先复位再启动（系统区[9]自检会给出三步）" if 熔断挂着 else ""
        print(f"""

  ── 模拟盘（MA13 · P+EB 观察模式）─────────{提示}
    [1] 启动 P_EB 观察模式（新窗口，占天勤席位）
    [2] 启动模拟盘守护（新窗口，崩溃自动拉起）
    [3] 台账日报（--hb 心跳完整率）
    [4] 打开台账目录
    [回车] 返回工作台""")
        c = input("  请输入：").strip()
        if c == "1":
            if 熔断挂着:
                print("\n  ⛔ 熔断未复位：关旧窗口 → 删 台账/熔断状态.json → 再启动。")
                _暂停()
                continue
            if not os.path.isfile(BAT_观察):
                print(f"\n  ⛔ 找不到 {BAT_观察}")
                _暂停()
                continue
            print("\n  提醒：模拟盘占用天勤登录席位 —— 确认主账号没在跑回测/取数。")
            q = input("  确认启动？(回车=启动 / n=取消)：").strip().lower()
            if q == "n":
                continue
            os.startfile(BAT_观察)
            print("  已在新窗口启动观察模式。")
            _暂停()
        elif c == "2":
            if not os.path.isfile(PY_守护):
                print(f"\n  ⛔ 找不到 {PY_守护}")
                _暂停()
                continue
            print("\n  提醒：守护会拉起模拟盘 —— 同样占用天勤席位。")
            q = input("  确认启动守护？(回车=启动 / n=取消)：").strip().lower()
            if q == "n":
                continue
            env = dict(os.environ)
            env["PYTHONUTF8"] = "1"
            subprocess.Popen(
                [PY, PY_守护], cwd=模拟盘目录, env=env,
                creationflags=subprocess.CREATE_NEW_CONSOLE)
            print("  守护已在新窗口启动（日志：模拟盘/台账/守护日志.txt）。")
            _暂停()
        elif c == "3":
            _跑脚本(PY_日报, 额外参数=["--hb"])
        elif c == "4":
            _开目录(os.path.join("策略", "06_MA13超短线", "模拟盘", "台账"))
            _暂停()
        else:
            return


# ---------------------------------------------------------------- 体检数据区

def 区_体检数据():
    while True:
        已有结果 = "有（直接看这个即可）" if os.path.isfile(JSON_体检0507) else "尚无结果文件"
        print(f"""

  ── 体检与数据 ─────────────────────────
    [1] 05/07 策略体检（120 次回测，约数小时；结果：{已有结果}）
    [2] 打开 工具/_数据/（行情缓存）
    [回车] 返回工作台""")
        c = input("  请输入：").strip()
        if c == "1":
            if os.path.isfile(JSON_体检0507):
                print("\n  ⚠️ 已有体检结果 工具/体检_05_07.json —— 重跑会再花数小时。")
                q = input("  确认重跑？(n=取消 / 其他=重跑)：").strip().lower()
                if q == "n":
                    continue
            _跑脚本(PY_体检0507)
        elif c == "2":
            _开目录(os.path.join("工具", "_数据"))
            _暂停()
        else:
            return


# ---------------------------------------------------------------- 系统区

def 区_系统():
    while True:
        print(f"""

  ── 系统 ─────────────────────────────
    [1] 本地自检（配置/路径/行尾/git/熔断，不联网，几秒）
    [2] 环境自检（联网登录天勤 + 验证品种，约 1 分钟）
    [3] git 状态
    [4] 打开 目录地图.md
    [回车] 返回工作台""")
        c = input("  请输入：").strip()
        if c == "1":
            _跑脚本(PY_自检本地)
        elif c == "2":
            _跑脚本(PY_环境自检)
        elif c == "3":
            try:
                r = subprocess.run(
                    ["git", "-C", 根, "status"],
                    capture_output=True, text=True, timeout=10,
                    encoding="utf-8", errors="replace")
                print("\n" + (r.stdout or r.stderr))
            except FileNotFoundError:
                print("\n  ⛔ git 命令不存在（装 Git for Windows 后重试）")
            _暂停()
        elif c == "4":
            _开文件(os.path.join(根, "目录地图.md"))
            _暂停()
        else:
            return


# ---------------------------------------------------------------- 主循环

def 主菜单():
    while True:
        os.system("cls" if os.name == "nt" else "clear")
        print(线)
        print("        天勤量化 · 工作台")
        print(线)
        print("  【回测】")
        print("    [1] 选策略选品种跑回测 / 打开策略与结果目录")
        print("  【模拟盘】")
        print("    [2] 启动观察模式 / 守护 / 台账日报 / 台账目录")
        print("  【体检与数据】")
        print("    [3] 05/07 体检 / 行情缓存目录")
        print("  【系统】")
        print("    [4] 本地自检 / 联网环境自检 / git / 目录地图")
        print()
        print("    [0] 退出")
        print(线)
        print(f"  项目根：{根}")
        print(线)
        c = input("  请输入序号：").strip()
        if c == "1":
            区_回测()
        elif c == "2":
            区_模拟盘()
        elif c == "3":
            区_体检数据()
        elif c == "4":
            区_系统()
        elif c == "0":
            print("\n  再见。\n")
            return


def main():
    # 命令行直通：python 工具/工作台.py 自检 / 日报
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg == "自检":
            sys.exit(subprocess.call([PY, PY_自检本地]))
        if arg == "日报":
            sys.exit(subprocess.call([PY, PY_日报, "--hb"]))
        print(f"  未知参数：{arg}（支持：自检 / 日报）")
        sys.exit(2)
    主菜单()


if __name__ == "__main__":
    main()
