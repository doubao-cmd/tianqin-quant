# -*- coding: utf-8 -*-
"""05_摆动线 · 冻结恢复器（2026-10-08 建）

背景
----
05_摆动线冻结时，除了加 `_已冻结.md` 标记，还把 `模拟盘/<12品种>/` 下的
24 个启动 bat 设为隐藏（12 × `模拟盘.bat` + `看台账.bat`）——
因为实测**它们是能真连天勤下单的入口**（锁 20 万基数，锁死风控），
只靠文档标记挡不住误双击。

⚠️ 那些 bat 全部是 `09-27 23:51` 机械批量复制生成的，从未被双击过
（冻结时实测：无进程、无计划任务）。

用法
----
    python 工具/冻结恢复_05摆动线.py            # 只看状态，不改任何东西
    python 工具/冻结恢复_05摆动线.py --显出     # 恢复显示（解除冻结的显示层）
    python 工具/冻结恢复_05摆动线.py --藏回去   # 再次藏起（重新冻结）

★ **恢复的完整步骤**（--显出 只是其中一步，别当成全解冻）：
    1. `python 工具/冻结恢复_05摆动线.py --显出`   ← 24 个 bat 可见
    2. 删掉 `策略/05_摆动线/_已冻结.md`          ← 否则标记仍在，逻辑上算冻结
    3. 想真正跑，还要自己双击 `模拟盘/<品种>/模拟盘.bat`（**这会真下单**）

为什么用 ctypes 而不是 cmd 的 attrib
------------------------------------
cmd 的 attrib 处理中文路径会挂（乱码 + 「参数格式不正确」），
与 `工具/根目录清爽.py` 是同一个理由。

为什么只改属性、不移动
----------------------
隐藏属性**不参与文件读写** ⇒ `模拟盘.bat` 里的路径、脚本的 `RECORD_DIR`、
所有 import 全不受影响；随时可逆。
"""
import ctypes
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

# ★ 本脚本在 `工具/` 下，相对根只有一层；被搬进 `_收纳/脚本/` 后会变成两层。
#   所以向上找 `lib/`（程序必需目录，永远在项目根），与 根目录清爽.py 同款。
_ROOT = _HERE
for _ in range(12):
    if os.path.isdir(os.path.join(_ROOT, "lib")):
        break
    上级 = os.path.dirname(_ROOT)
    if 上级 == _ROOT:
        break
    _ROOT = 上级

_BASE = os.path.join(_ROOT, "策略", "05_摆动线", "模拟盘")
_MARK = os.path.join(_ROOT, "策略", "05_摆动线", "_已冻结.md")

FILE_ATTRIBUTE_HIDDEN = 0x2
INVALID = 0xFFFFFFFF

_k = ctypes.windll.kernel32
_k.GetFileAttributesW.restype = ctypes.c_uint32
_k.GetFileAttributesW.argtypes = [ctypes.c_wchar_p]
_k.SetFileAttributesW.restype = ctypes.c_int
_k.SetFileAttributesW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]


def _藏(p, 藏):
    a = _k.GetFileAttributesW(p)
    if a == INVALID:
        return None
    新 = (a | FILE_ATTRIBUTE_HIDDEN) if 藏 else (a & ~FILE_ATTRIBUTE_HIDDEN)
    return bool(_k.SetFileAttributesW(p, 新))


def _全列():
    out = []
    if not os.path.isdir(_BASE):
        return out
    for d in sorted(os.listdir(_BASE)):
        pd = os.path.join(_BASE, d)
        if not os.path.isdir(pd):
            continue
        for f in sorted(os.listdir(pd)):
            if f.lower().endswith((".bat", ".cmd")):
                out.append((os.path.join(d, f), os.path.join(pd, f)))
    return out


def main():
    模式 = sys.argv[1] if len(sys.argv) > 1 else ""
    条目 = _全列()

    print(f"05_摆动线冻结状态")
    print(f"  冻结标记 _已冻结.md : {'在（逻辑上= 冻结）' if os.path.isfile(_MARK) else '★ 不在'}")
    print(f"  模拟盘目录         : {_BASE}")
    print(f"  启动 bat           : {len(条目)} 个")
    print()

    if 模式 == "--显出":
        n = sum(1 for _, p in 条目 if _藏(p, False))
        print(f"  已解除隐藏 {n} 个。")
        print("  ⚠️ 这**只是恢复显示**。要真解冻还得：")
        print(f"     删掉 {_MARK}")
    elif 模式 == "--藏回去":
        n = sum(1 for _, p in 条目 if _藏(p, True))
        print(f"  已藏起 {n} 个。")
    else:
        if not 条目:
            print("  ⚠️ 没找到任何 bat —— 目录结构变了？先核对 05_摆动线/模拟盘/")
            return
        隐 = 0
        for rel, p in 条目:
            a = _k.GetFileAttributesW(p)
            h = (a != INVALID) and bool(a & FILE_ATTRIBUTE_HIDDEN)
            隐 += bool(h)
            print(f"    [{'隐藏' if h else '显示'}] {rel}")
        print()
        print(f"  隐藏 {隐} / 可见 {len(条目) - 隐}")
        print("  （只看不改。要改用 --显出 / --藏回去）")


if __name__ == "__main__":
    main()