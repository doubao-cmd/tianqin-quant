# -*- coding: utf-8 -*-
"""收尾 bat 补丁（2026-09-27）：
   一、扶正备份。第一次 --go 把 ARCH 顶层副本改成了「已打补丁」的版本，
       而嵌套树里那一份是「打补丁前」的原始字节 ⇒ 用嵌套树覆盖顶层。
   二、单独补「跑回测.bat」——它是隐藏文件，open(...,'w') 会 EPERM，
       须先取消隐藏、写完再藏回。
   三、清理嵌套杂物。
"""
import os
import shutil
import sys
import ctypes

ROOT = r"C:\Users\cc\Desktop\天勤量化"
ARCH = os.path.join(ROOT, "工具", "_归档", "解释器修复前_20260927")
NEST = os.path.join(ARCH, "工具", "_归档", "解释器修复前_20260927")
G = ctypes.windll.kernel32.GetFileAttributesW
S = ctypes.windll.kernel32.SetFileAttributesW

GO = "--go" in sys.argv

OLD = ('set "PY=python"\n'
       'where python >nul 2>nul || set "PY=%USERPROFILE%\\.workbuddy\\binaries\\python\\envs\\tqsdk\\Scripts\\python.exe"\n')
NEW = ('set "PY="\n'
       'set "_VENV_TQ=%USERPROFILE%\\.workbuddy\\binaries\\python\\envs\\tqsdk\\Scripts\\python.exe"\n')


def 归一(t):
    return t.replace("\r\r\n", "\n").replace("\r\n", "\n").replace("\r", "\n")


def main():
    # ---------- 一、扶正备份 ----------
    nested = []
    for dp, _dn, fn in os.walk(NEST):
        for f in fn:
            if f.lower().endswith(".bat"):
                nested.append(os.path.join(dp, f))
    print("嵌套树里的 bat:", len(nested))
    好 = 0
    for p in nested:
        rel = os.path.relpath(p, NEST)          # 工具\... / 策略\... / 跑回测.bat
        t = 归一(open(p, "rb").read().decode("utf-8-sig"))
        assert OLD in t, ("嵌套副本不是原始内容", rel)
        好 += 1
        if GO:
            dst = os.path.join(ARCH, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(p, dst)
    print("确认是「打补丁前」原始内容的:", 好, "/", len(nested))

    # ---------- 二、跑回测.bat（隐藏文件）----------
    p_root = os.path.join(ROOT, "跑回测.bat")
    a0 = G(p_root)
    raw = open(p_root, "rb").read()
    t = 归一(raw.decode("utf-8-sig"))
    n = t.count(OLD)
    print()
    print("跑回测.bat  attrs=0x%X  含旧块=%d  含新块=%d" % (a0, n, t.count('_VENV_TQ')))
    if n == 1:
        if GO:
            S(p_root, a0 & ~0x2)                 # 取消隐藏
            try:
                open(p_root, "w", encoding="utf-8", newline="\r\n").write(t.replace(OLD, NEW))
                print("  已写入（临时取消隐藏）")
            finally:
                S(p_root, G(p_root) | 0x2)       # 藏回
            a1 = G(p_root)
            t2 = open(p_root, "rb").read().decode("utf-8-sig")
            print("  写后 attrs=0x%X（隐藏=%s）  含新块=%d" %
                  (a1, bool(a1 & 2), t2.count('_VENV_TQ')))

    # ---------- 三、清嵌套杂物 ----------
    print()
    if GO:
        sys.path.insert(0, ROOT)
        from lib import recycle
        print("嵌套杂物未进回收站的:", recycle.移入回收站([NEST]))
        剩余 = []
        for dp, _dn, fn in os.walk(ARCH):
            for f in fn:
                if f.lower().endswith(".bat"):
                    剩余.append(os.path.relpath(os.path.join(dp, f), ARCH))
        顶层 = [r for r in 剩余 if not r.startswith("工具" + os.sep + "_归档")]
        print("ARCH 内 bat 总数:", len(剩余), " 其中顶层镜像:", len(顶层))
        bad = []
        for r in 顶层:
            t = 归一(open(os.path.join(ARCH, r), "rb").read().decode("utf-8-sig"))
            if OLD not in t:
                bad.append(r)
        print("顶层镜像里不是原始内容的:", bad[:10], "共", len(bad))


main()
