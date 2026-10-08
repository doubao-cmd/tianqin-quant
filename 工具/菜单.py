# -*- coding: utf-8 -*-
"""
天勤量化 · 主菜单
==========================================================
（2026-09-26 新建；**2026-09-27 重写**）

【为什么重写】
2026-09-24 目录池化之后，「一个策略 × 一个品种」被拆成三件套
（策略池/ + 品种池/ + 组合/），入口变得不直观，于是有了本菜单。
2026-09-27 用户要求**结构回到最初的样子** —— 池化三件套已整体收进
`_归档/池化架构_20260927/`，入口回到 `策略/<编号>_<名>/回测/<品种>/`。
所以本菜单改成**直接扫 `策略/` 目录**：
    策略/01_双均线/回测/豆粕/双均线_回测.py
菜单不再认任何「组合」，也不再需要 品种池/。

【怎么用】
    双击根目录「跑回测.bat」（推荐，会自动找 python）
    或命令行：python 工具/菜单.py

⚠️ 同一时间只跑一个回测（天勤账户同一时刻只允许一个会话）。
   上一个跑完再点下一个。
"""

import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
根 = os.path.dirname(_HERE)
PY = sys.executable

线 = "=" * 66


# ---------------------------------------------------------------- 工具

# 策略被停用的标记文件名（放在 `策略/<策略目录>/` 下）。
# 停用 = 不进菜单、不参与后续同步；**文件一个字都不删**，删掉标记即自动恢复。
_停用标记 = "_已停用.md"


def _已停用(策略目录):
    return os.path.isfile(os.path.join(策略目录, _停用标记))


def _策略们():
    """扫 `策略/` 下的编号目录，返回 ["01_双均线", "04_通道突破", ...]。

    只认「里面有 `回测/` 子目录」的目录；跳过 `_说明.md` 这类下划线开头项。
    ★ 跳过带 `_已停用.md` 标记的策略（当前：02_均线排列，2026-09-28 用户拍板停用）。
    """
    d = os.path.join(根, "策略")
    out = []
    if os.path.isdir(d):
        for f in sorted(os.listdir(d)):
            p = os.path.join(d, f)
            if f.startswith("_") or not os.path.isdir(p):
                continue
            if not os.path.isdir(os.path.join(p, "回测")):
                continue
            if _已停用(p):
                continue
            out.append(f)
    return out


def _停用策略们():
    """列出被停用的策略目录名 —— 菜单里单独提一句，免得你以为它凭空没了。"""
    d = os.path.join(根, "策略")
    out = []
    if os.path.isdir(d):
        for f in sorted(os.listdir(d)):
            p = os.path.join(d, f)
            if f.startswith("_") or not os.path.isdir(p):
                continue
            if os.path.isdir(os.path.join(p, "回测")) and _已停用(p):
                out.append(f)
    return out


def _品种们(策略目录):
    """扫 `策略/<策略>/回测/` 下的品种目录，返回中文名列表（按拼音排序）。"""
    d = os.path.join(根, "策略", 策略目录, "回测")
    out = []
    if os.path.isdir(d):
        for f in sorted(os.listdir(d)):
            p = os.path.join(d, f)
            if f.startswith("_") or not os.path.isdir(p):
                continue
            out.append(f)
    return out


def _主脚本(策略目录, 品种):
    """定位该品种目录里的**主**回测脚本。

    命名规律：`<策略短名>_回测.py`（"01_双均线" -> "双均线_回测.py"）。
    找不到就退而取目录里第一个 `_回测.py`（跳过「跨年」这类变体）。
    返回 None 表示这个品种目录没得跑。
    """
    短名 = 策略目录.split("_", 1)[-1]
    d = os.path.join(根, "策略", 策略目录, "回测", 品种)
    首选 = os.path.join(d, 短名 + "_回测.py")
    if os.path.isfile(首选):
        return 首选
    if os.path.isdir(d):
        for f in sorted(os.listdir(d)):
            if f.endswith("_回测.py") and "跨年" not in f:
                return os.path.join(d, f)
    return None


def _跑(脚本):
    """在脚本所在目录下调起它，继承当前控制台（能看到实时输出）。"""
    目录 = os.path.dirname(脚本)
    print("\n" + 线)
    print("  执行：python %s" % os.path.relpath(脚本, 根))
    print("  （回测较慢，日线约 1 秒/交易日，请耐心等它自己跑完）")
    print(线 + "\n")
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    try:
        code = subprocess.call([PY, os.path.basename(脚本)], cwd=目录, env=env)
        print("\n  —— 结束（退出码 %d）——" % code)
    except KeyboardInterrupt:
        print("\n  —— 已中断 ——")
    input("\n  回车返回主菜单 ...")


def _开目录(相对):
    p = os.path.join(根, 相对)
    if os.path.isdir(p):
        os.startfile(p)
        print("\n  已打开：%s" % p)
    else:
        print("\n  ⛔ 目录不存在：%s" % p)
    input("\n  回车返回主菜单 ...")


# ---------------------------------------------------------------- 各选项

def 选项_跑回测():
    """选 策略 -> 选 品种 -> 跑该品种的回测脚本。"""
    策略们 = _策略们()
    if not 策略们:
        print("\n  ⛔ 策略/ 下没找到任何策略目录（应有「回测/」子目录）")
        input("\n  回车返回主菜单 ...")
        return

    print("\n  选一个策略：")
    for i, s in enumerate(策略们, 1):
        档案 = "有" if os.path.isfile(os.path.join(根, "策略", s, "_档案.md")) else "—"
        print("    [%d] %-12s  档案:%s" % (i, s, 档案))
    停用 = _停用策略们()
    if 停用:
        print("    （已停用，不在上表：%s —— 见该目录下的 _已停用.md）" % "、".join(停用))
    s = input("\n  输入序号（回车取消）：").strip()
    if not s.isdigit() or not (1 <= int(s) <= len(策略们)):
        return
    策略目录 = 策略们[int(s) - 1]

    品种表 = _品种们(策略目录)
    if not 品种表:
        print("\n  ⛔ 策略/%s/回测/ 下没有品种目录" % 策略目录)
        input("\n  回车返回主菜单 ...")
        return
    print("\n  选一个品种（%s × 该品种）：" % 策略目录)
    每行 = 4
    for i, 名 in enumerate(品种表):
        if i % 每行 == 0:
            print("   ", end="")
        print("[%2d] %-8s" % (i + 1, 名), end="")
        if i % 每行 == 每行 - 1 or i == len(品种表) - 1:
            print()
    t = input("\n  输入序号（回车取消）：").strip()
    if not t.isdigit() or not (1 <= int(t) <= len(品种表)):
        return
    品种 = 品种表[int(t) - 1]

    脚本 = _主脚本(策略目录, 品种)
    if not 脚本:
        print("\n  ⛔ 在 策略/%s/回测/%s/ 里找不到 *_回测.py" % (策略目录, 品种))
        input("\n  回车返回主菜单 ...")
        return

    print("\n  将跑：%s" % os.path.relpath(脚本, 根))
    print("  （区间等参数写在该脚本顶部「可调参数」区，要先改就先编辑它）")
    q = input("\n  确认跑？(回车=跑 / n=取消)：").strip().lower()
    if q == "n":
        return
    _跑(脚本)


def 选项_看地图():
    # ★ 2026-10-08：目录地图.md 已移进 _收纳/（根目录只留 6 项）。
    #   两个候选路径都试，兼容「还在根目录」与「已移进_收纳」两种状态。
    for _rel in (os.path.join("_收纳", "目录地图.md"), "目录地图.md"):
        p = os.path.join(根, _rel)
        if os.path.isfile(p):
            os.startfile(p)
            print(f"\n  已用默认程序打开 {_rel}")
            break
    else:
        print("\n  ⛔ 两处都没找到目录地图.md（_收纳/目录地图.md 或 根目录/目录地图.md）")
    input("\n  回车返回主菜单 ...")


def 选项_看结果():
    _开目录("回测结果")


def 选项_看数据():
    _开目录(os.path.join("工具", "_数据"))


# ---------------------------------------------------------------- 主循环

def main():
    while True:
        os.system("cls" if os.name == "nt" else "clear")
        print(线)
        print("        天勤量化 · 主菜单")
        print(线)
        print("  【跑回测】")
        print("    [1] 选策略 + 选品种 → 跑回测")
        print("    [2] 打开 策略/ 目录（也可以进去双击 回测.bat）")
        print()
        print("  【模拟盘】")
        print("    [6] 模拟盘自检（5 策略 × 12 品种 = 60 个入口逐个真连）")
        print()
        print("  【看东西】")
        print("    [3] 目录地图（哪个文件夹放什么）")
        print("    [4] 打开 回测结果/ 目录")
        print("    [5] 打开 工具/_数据/（取到的行情缓存）")
        print()
        print("    [0] 退出")
        print(线)
        print("  项目根：%s" % 根)
        print(线)
        c = input("  请输入序号：").strip()
        if c == "1":
            选项_跑回测()
        elif c == "2":
            _开目录("策略")
        elif c == "3":
            选项_看地图()
        elif c == "4":
            选项_看结果()
        elif c == "5":
            选项_看数据()
        elif c == "6":
            _跑(os.path.join(根, "工具", "模拟盘自检.py"))
        elif c == "0":
            print("\n  再见。\n")
            return
        else:
            # 别的输入当没看见，重画菜单
            continue


if __name__ == "__main__":
    main()
