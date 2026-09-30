# -*- coding: utf-8 -*-
"""
==========================================================
 删报告 —— 走回收站删除 回测结果/<策略>/<品种>/ 下的回测报告
==========================================================

用法（在项目根目录跑）：
    python 工具/删报告.py PVC                 # 干跑：只列清单，绝不删
    python 工具/删报告.py PVC --go            # 真删（走系统回收站，可还原）
    python 工具/删报告.py PVC MA --go         # 多个品种
    python 工具/删报告.py PVC --keep-in       # 保留正式 _in / _out 报告
    python 工具/删报告.py PVC --only 双均线 --go   # 只删「01_双均线」这一层（写短名或 01_双均线 都行）

安全闸门：
  1. 必须显式给出品种名，绝不允许"不写参数就删全部"；
  2. 只删 回测结果/<策略>/<品种>/ 目录内的 .html / .txt，不递归别的目录；
  3. 干跑（默认）只列清单，加 --go 才真删；
  4. 删除走 SHFileOperationW + FOF_ALLOWUNDO（系统回收站，可还原）；
  5. 删完核验，仍有残留则报错退出。
"""
import os
import sys
import ctypes

# 定位项目根：从本文件所在目录逐级向上，找含 lib 的那一层（整目录搬走也不会失效）
ROOT = os.path.dirname(os.path.abspath(__file__))
while ROOT != os.path.dirname(ROOT) and not os.path.isdir(os.path.join(ROOT, "lib")):
    ROOT = os.path.dirname(ROOT)
RESULT_DIR = os.path.join(ROOT, "回测结果")
EXTS = (".html", ".txt")


def 品种目录们(sym, only=None):
    """找出该品种在 回测结果/ 下的所有报告目录（返回相对 RESULT_DIR 的路径）。

    新结构：回测结果/<策略>/<品种>/   ；兼容旧结构残留：回测结果/<品种>/
    """
    found = []
    for top in sorted(os.listdir(RESULT_DIR)):
        tp = os.path.join(RESULT_DIR, top)
        if not os.path.isdir(tp) or top.startswith("_"):
            continue
        if top == sym:                      # 旧结构（未分策略层的残留）
            found.append(top)
            continue
        if only and top != only and not top.endswith("_" + only):
            continue                        # --only 只处理指定策略层；短名 双均线 / 全名 01_双均线 都能对上
        if os.path.isdir(os.path.join(tp, sym)):
            found.append(os.path.join(top, sym))
    return found


def collect(symbols, keep_in, only=None):
    targets, skipped = [], []
    for sym in symbols:
        目录s = 品种目录们(sym, only)
        if not 目录s:
            print(f"!! 找不到该品种的报告目录：回测结果/*/{sym}/"
                  + (f"（--only {only} 时请检查策略名是否拼对）" if only else ""))
            sys.exit(1)
        for rel in 目录s:
            d = os.path.join(RESULT_DIR, rel)
            for fn in sorted(os.listdir(d)):
                p = os.path.join(d, fn)
                if not os.path.isfile(p) or not fn.lower().endswith(EXTS):
                    continue
                if "_汇总" in fn:
                    skipped.append(p)      # 修改记录汇总页：永不删（数据在 工具/_账本/）
                    continue
                if keep_in and ("_in" in fn or "_out" in fn):
                    skipped.append(p)
                    continue
                targets.append(p)
    return targets, skipped


def trash(targets):
    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", ctypes.c_void_p),
                    ("wFunc", ctypes.c_uint),
                    ("pFrom", ctypes.c_void_p),
                    ("pTo", ctypes.c_void_p),
                    ("fFlags", ctypes.c_uint16),
                    ("fAnyOperationsAborted", ctypes.c_bool),
                    ("hNameMappings", ctypes.c_void_p),
                    ("lpszProgressTitle", ctypes.c_wchar_p)]

    FO_DELETE = 3
    FOF_SILENT = 0x4
    FOF_NOCONFIRMATION = 0x10
    FOF_ALLOWUNDO = 0x40
    FOF_NOERRORUI = 0x400

    from_str = "\0".join(os.path.abspath(t) for t in targets) + "\0\0"
    buf = ctypes.create_unicode_buffer(from_str)
    op = SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = ctypes.cast(buf, ctypes.c_void_p)
    op.pTo = None
    op.fFlags = FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI
    res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    print(f"\nSHFileOperationW 返回 {res}（0=成功；本机常返 2 但实际成功，以文件是否消失为准）")
    return [t for t in targets if os.path.exists(t)]


def main():
    原始 = sys.argv[1:]
    go = "--go" in 原始
    keep_in = "--keep-in" in 原始
    only = None
    if "--only" in 原始:
        i = 原始.index("--only")
        only = 原始[i + 1] if i + 1 < len(原始) else None
        原始 = 原始[:i] + 原始[i + 2:]     # 去掉 --only 及其值，免得被当成品种名
    args = [a for a in 原始 if not a.startswith("--")]

    if not args:
        print(__doc__)
        print("!! 必须显式给出品种名（如 PVC）。已中止，未删任何文件。")
        sys.exit(1)

    targets, skipped = collect(args, keep_in, only)

    print("=" * 74)
    print(f"品种：{', '.join(args)}" + (f"   策略层：{only}" if only else "")
          + f"   待删 {len(targets)} 个文件"
          f"{'（走回收站）' if go else '（干跑，不会删）'}")
    print("=" * 74)
    for t in targets:
        print("   ", os.path.relpath(t, RESULT_DIR))
    if skipped:
        print("\n受保护跳过（_汇总 汇总页永不删；--keep-in 时 _in/_out 也跳过）：")
        for t in skipped:
            print("   ", os.path.relpath(t, RESULT_DIR))

    if not targets:
        print("\n没有匹配到任何文件，无事可做。")
        sys.exit(0)

    if not go:
        print(f"\n[干跑] 未删除任何文件。确认清单无误后加 --go 重跑。")
        sys.exit(0)

    left = trash(targets)
    print(f"删除后仍存在 {len(left)} 个")
    if left:
        for t in left:
            print("   仍在:", os.path.relpath(t, RESULT_DIR))
        sys.exit(2)
    print(f"核验通过：{len(targets)} 个文件全部已移入回收站。")


if __name__ == "__main__":
    main()
