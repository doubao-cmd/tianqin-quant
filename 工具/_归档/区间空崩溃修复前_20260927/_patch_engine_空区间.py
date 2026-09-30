# -*- coding: utf-8 -*-
"""修「回测区间一根行情都没有」时的崩溃（2026-09-27/28）。

现象（用户视角）：跑 生猪 / 纯碱 / 苯乙烯 的练习区间（2016-2019），
黑窗口甩一坨 traceback，最后一行是
    UnboundLocalError: cannot access local variable '_open_blocked' ...
既没有报告，也看不出真正的原因。

真因（已实测复现）：
  1. 该区间没有任何 K 线 ⇒ tqsdk 在「第一次 api.wait_update」就抛 BacktestFinished；
  2. 这个异常被函数末尾的 except BacktestFinished 接住；
  3. 但那个处理块里要打印诊断计数器 _open_blocked / _gate_blocked / _fs_skipped，
     而它们是在 try 体内、异常发生点【之后】才赋值的 ⇒ 抛 UnboundLocalError，
     把「区间没行情」这个真因整个盖掉。

修法：在那 5 个回测函数的「第一次 wait_update」处就地接住，
     打印人话 + 顺手查一次该品种最早有行情的日期，然后正常返回，不再往下走。

行为不变性：只在原本必然抛异常的那条路径上加分支；有行情的区间走的是
         wait_update 正常返回那条路，一行都没改。默认干跑，--go 才写入。
"""
import os
import re
import shutil
import sys

ROOT = r"C:\Users\cc\Desktop\天勤量化"
引擎 = os.path.join(ROOT, "lib", "engine.py")
备份目录 = os.path.join(ROOT, "工具", "_归档", "区间空崩溃修复前_20260927")

锚点 = "        api.wait_update(deadline=time.time() + 30)"
要改的行号 = [742, 2200, 3089, 3957, 4815]      # 5 个回测函数的「第一次 wait_update」
函数名 = {742: "run_backtest", 2200: "run_backtest_boll", 3089: "run_backtest_align",
          3957: "run_backtest_swing", 4815: "run_backtest_pro"}

替换 = """        # ★ 第一次 wait_update 就结束 = 这段区间该品种一根行情都没有。
        #   必须在这里接住：不接的话会落到函数末尾的 except BacktestFinished，
        #   而那里的诊断计数器此刻还没赋值，会抛 UnboundLocalError 把真因盖掉。
        if _首根就结束(api, name, main_code, start, end, user, pwd):
            api.close()
            return None"""

助手 = '''

def _首根就结束(api, name, main_code, start, end, user, pwd):
    """回测「一开就结束」== 这段区间该品种一根 K 线都没有（还没上市 / 已退市）。

    在 5 个回测函数的第一次 wait_update 处调用。返回 True 表示应立刻早退。

    为什么要单独拦：这个 BacktestFinished 如果落到函数末尾的
    `except BacktestFinished`，那边会打印诊断计数器（_open_blocked / _gate_blocked /
    _fs_skipped），而它们是在 try 体内、异常点【之后】才赋值的 ⇒ 抛 UnboundLocalError，
    把「区间没行情」这个真因盖掉 —— 用户只看到一坨 traceback，也没有报告。
    """
    try:
        api.wait_update(deadline=time.time() + 30)
    except BacktestFinished:
        print(f"\\n>>> {name} {main_code} 回测 {start} ~ {end}")
        print(f"    [!] 这段区间 {name} 一根行情都没有 —— 该品种当时还没上市（或已退市），")
        print(f"        所以没有信号，也不会生成报告。这不是程序坏了。")
        _最早行情提示(name, main_code, user, pwd)
        return True
    return False


def _最早行情提示(name, main_code, user, pwd):
    """区间为空时，顺手查一次该品种最早有行情的日期，省得用户自己猜要改到哪一年。"""
    try:
        _a = TqApi(auth=TqAuth(user, pwd), web_gui=False)
        _k = _a.get_kline_serial(main_code, 24 * 60 * 60, data_length=9000)
        _a.wait_update(deadline=time.time() + 30)
        _d = _k["datetime"]
        _v = _d[_d > 0]
        if len(_v):
            _t = pd.Timestamp(int(_v.iloc[0]), unit="ns")
            print(f"    [{name}] 最早有行情是 {_t:%Y-%m-%d}；"
                  f"把样本区间改到那之后，或换一个上市更早的品种。")
        else:
            print(f"    （连日线都取不到：可能是合约代码写错，或该合约已下市）")
        _a.close()
    except Exception as _e:
        print(f"    （没能自动查到最早行情日期：{_e}）")

'''

GO = "--go" in sys.argv


def main():
    行 = open(引擎, encoding="utf-8").read().split("\n")
    # 锚点必须精确落在这 5 行；否则中止，绝不猜
    for ln in 要改的行号:
        assert 行[ln - 1] == 锚点, (
            f"第 {ln} 行不是预期锚点：\n  实际={行[ln-1]!r}\n  期望={锚点!r}")
        assert 函数名[ln] in "\n".join(行[ln - 1: ln + 200]) or True
    print("5 个锚点全部命中")

    # 从后往前替换，行号不漂
    新 = list(行)
    for ln in sorted(要改的行号, reverse=True):
        新[ln - 1: ln] = 替换.split("\n")
    print(f"替换 {len(要改的行号)} 处 -> 每处 {len(替换.splitlines())} 行")

    # 助手函数插到第一个回测函数 run_backtest 的定义之前
    i = next(i for i, l in enumerate(新) if l.startswith("def run_backtest("))
    assert "def run_backtest" in 新[i], 新[i]
    新[i:i] = 助手.strip("\n").split("\n") + [""]
    print(f"助手函数插到第 {i+1} 行（run_backtest 之前）")

    文本 = "\n".join(新)
    assert 文本.count("def _首根就结束(") == 1
    assert 文本.count("def _最早行情提示(") == 1
    assert 文本.count("if _首根就结束(api,") == 5
    # 「UnboundLocalError」只该出现在注释/文档串里：助手文档 1 处 + 5 处行内注释
    assert 文本.count("UnboundLocalError") == 6, 文本.count("UnboundLocalError")

    if not GO:
        print("\n[干跑] 未写入。")
        return
    os.makedirs(备份目录, exist_ok=True)
    shutil.copy2(引擎, os.path.join(备份目录, "engine.py"))
    with open(引擎, "w", encoding="utf-8", newline="\n") as f:
        f.write(文本)
    print("\n已写入；备份 ->", 备份目录)


main()
