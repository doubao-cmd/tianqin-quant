# -*- coding: utf-8 -*-
"""补第二道保险：在 5 个回测函数的 `except BacktestFinished:` 开头，
先判断「这段区间到底有没有取到行情」。

第一道（第一次 wait_update 处）经实测拦不住：tqsdk 的 BacktestFinished 实际是在
`api.get_quote()` 内部那次等待里抛出来的，位置在第一次 wait_update 之后。
所以还得在异常处理入口再判一次 —— 那里正是 UnboundLocalError 发生的地方。

判据：klines 的 datetime 列**任何一根 > 0** 就算有行情（全 0 = 该品种当时还没上市）。
有行情就照旧生成报告，一个字节的行为都不变；没行情就打印结论后正常返回。

默认干跑，--go 才写入。
"""
import ast
import os
import shutil
import sys

ROOT = r"C:\Users\cc\Desktop\天勤量化"
引擎 = os.path.join(ROOT, "lib", "engine.py")
备份目录 = os.path.join(ROOT, "工具", "_归档", "区间空崩溃修复前_20260927")

目标函数 = ["run_backtest", "run_backtest_boll", "run_backtest_align",
            "run_backtest_swing", "run_backtest_pro"]

助手 = '''

def _K线里有没有行情(*系列):
    """这段回测区间到底有没有取到行情：任何一根 K 线 datetime > 0 就算有。

    全 0 表示该品种在那段时间还没上市 —— 这时 tqsdk 会在初始化阶段就抛
    BacktestFinished，报告没法生成，也不该生成。
    """
    for s in 系列:
        if s is None:
            continue
        try:
            if bool((s["datetime"] > 0).any()):
                return True
        except Exception:
            continue
    return False

'''

检查 = '''        # ★ 先确认这段区间到底有没有行情。没有的话（该品种当时还没上市），
        #   tqsdk 会在初始化阶段就抛 BacktestFinished，而不是跑到正常收尾；
        #   此时下面的诊断计数器（_open_blocked / _gate_blocked / _fs_skipped）
        #   还没赋值，直接往下走会抛 UnboundLocalError 把真因盖掉。
        _a_ = locals().get("api")
        _k_ = locals().get("klines")
        if _k_ is None:
            _k_ = locals().get("k5")
        if not _K线里有没有行情(_k_):
            print(f"\\n>>> {name} {main_code} 回测 {start} ~ {end}")
            print(f"    [!] 这段区间 {name} 一根行情都没有 —— 该品种当时还没上市（或已退市），")
            print(f"        所以没有信号，也不会生成报告。这不是程序坏了。")
            _最早行情提示(name, main_code, user, pwd)
            if _a_ is not None:
                try:
                    _a_.close()
                except Exception:
                    pass
            return None
'''

GO = "--go" in sys.argv


def 主():
    文本 = open(引擎, encoding="utf-8").read()
    树 = ast.parse(文本)
    行 = 文本.split("\n")

    位置 = []
    for n in ast.walk(树):
        if not isinstance(n, ast.FunctionDef) or n.name not in 目标函数:
            continue
        for t in ast.walk(n):
            if not isinstance(t, ast.Try):
                continue
            for h in t.handlers:
                if isinstance(h.type, ast.Name) and h.type.id == "BacktestFinished":
                    位置.append((n.name, h.lineno))
    位置.sort(key=lambda x: x[1])
    print("找到 except BacktestFinished：")
    for f, ln in 位置:
        print(f"   {f:<22} 第 {ln} 行：{行[ln-1]!r}")
        assert 行[ln - 1] == "    except BacktestFinished:", 行[ln - 1]
    assert len(位置) == 5, len(位置)

    # 从后往前插，行号不漂
    新 = list(行)
    for f, ln in sorted(位置, key=lambda x: -x[1]):
        新[ln:ln] = 检查.rstrip("\n").split("\n")
    print(f"插入 {len(位置)} 处检查，每处 {len(检查.rstrip(chr(10)).splitlines())} 行")

    i = next(i for i, l in enumerate(新) if l.startswith("def run_backtest("))
    新[i:i] = 助手.strip("\n").split("\n") + [""]
    print(f"助手 _K线里有没有行情 插到第 {i+1} 行")

    出 = "\n".join(新)
    assert 出.count("def _K线里有没有行情(") == 1
    assert 出.count("if not _K线里有没有行情(_k_):") == 5
    assert 出.count("def _首根就结束(") == 1
    assert 出.count("if _首根就结束(api,") == 5
    ast.parse(出)
    print("结构断言与 AST 全过")

    if not GO:
        print("\n[干跑] 未写入。")
        return
    os.makedirs(备份目录, exist_ok=True)
    shutil.copy2(引擎, os.path.join(备份目录, "engine_第一道之后.py"))
    with open(引擎, "w", encoding="utf-8", newline="\n") as f:
        f.write(出)
    print("\n已写入；备份 ->", os.path.join(备份目录, "engine_第一道之后.py"))


主()
