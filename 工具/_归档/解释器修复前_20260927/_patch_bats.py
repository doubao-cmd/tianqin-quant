# -*- coding: utf-8 -*-
"""修 .bat 的三处缺陷（2026-09-27）：
   ① 解释器选择顺序反了 —— 优先 PATH 里的 python，而本机 PATH 首位是
      C:\\Users\\cc\\AppData\\Local\\Programs\\Python\\Python312\\python.exe（没装 tqsdk）
      ⇒ 双击任何一个 bat 都是 ModuleNotFoundError: No module named 'tqsdk'
   ② 12 个 04 模拟盘 bat 里有 \\r\\r\\n（双 CR）
   ③ 48 个 看台账.bat 带 UTF-8 BOM ⇒ 第一行 @echo off 报「不是内部或外部命令」
默认干跑，--go 才写入。"""
import os
import sys
import shutil
import collections

ROOT = r"C:\Users\cc\Desktop\天勤量化"
ARCH = os.path.join(ROOT, "工具", "_归档", "解释器修复前_20260927")

OLD_C = "rem ---- 解释器：优先 PATH 里的 python，找不到才回退到本机 venv ----"
NEW_C = "rem ---- 解释器：依次试到「真装了 tqsdk」的那个（PATH 里的 python 常常没装 tqsdk）----"

OLD = ('set "PY=python"\n'
       'where python >nul 2>nul || set "PY=%USERPROFILE%\\.workbuddy\\binaries\\python\\envs\\tqsdk\\Scripts\\python.exe"\n')

NEW = ('rem ---- interpreter: pick the first candidate that really has tqsdk ----\n'
       'rem      (the python on PATH is often another install without tqsdk)\n'
       'set "PY="\n'
       'set "_VENV_TQ=%USERPROFILE%\\.workbuddy\\binaries\\python\\envs\\tqsdk\\Scripts\\python.exe"\n'
       'for %%P in ("%_VENV_TQ%" python py) do (\n'
       '  if not defined PY (\n'
       '    "%%~P" -c "import importlib.util as u, sys; sys.exit(0 if u.find_spec(\'tqsdk\') else 1)" >nul 2>nul\n'
       '    if not errorlevel 1 set "PY=%%~P"\n'
       '  )\n'
       ')\n'
       'if not defined PY (\n'
       '  echo.\n'
       '  echo  [错误] 没找到装了 tqsdk 的 Python，程序起不来。\n'
       '  echo         已依次试过：%_VENV_TQ%  /  python  /  py\n'
       '  echo         处理：确认天勤 tqsdk 虚拟环境还在；不在就重装一次 tqsdk。\n'
       '  echo.\n'
       '  pause\n'
       '  exit /b 1\n'
       ')\n')

# 工具\回滚.bat 明令 ASCII ONLY（中文 + chcp 65001 会让 cmd 重解析时错位）
NEW_ASCII = ('rem ---- interpreter: pick the first candidate that really has tqsdk ----\n'
             'rem      (the python on PATH is often another install without tqsdk)\n'
             'set "PY="\n'
             'set "_VENV_TQ=%USERPROFILE%\\.workbuddy\\binaries\\python\\envs\\tqsdk\\Scripts\\python.exe"\n'
             'for %%P in ("%_VENV_TQ%" python py) do (\n'
             '  if not defined PY (\n'
             '    "%%~P" -c "import importlib.util as u, sys; sys.exit(0 if u.find_spec(\'tqsdk\') else 1)" >nul 2>nul\n'
             '    if not errorlevel 1 set "PY=%%~P"\n'
             '  )\n'
             ')\n'
             'if not defined PY (\n'
             '  echo.\n'
             '  echo  [ERROR] No Python with tqsdk found. Tried:\n'
             '  echo          %_VENV_TQ%\n'
             '  echo          python  /  py\n'
             '  echo.\n'
             '  pause\n'
             '  exit /b 1\n'
             ')\n')

SKIP = {os.path.join("工具", "关闭模拟盘.bat")}          # 不调 python，无需改
MOVE = {os.path.join("策略", "01_双均线", "回测", "纯碱", "_probe_self.bat")}   # 调试残渣，挪走

GO = "--go" in sys.argv


def 列表():
    out = []
    for base in ("策略", "工具"):
        for dp, _dn, fn in os.walk(os.path.join(ROOT, base)):
            for f in fn:
                if f.lower().endswith(".bat"):
                    out.append(os.path.join(dp, f))
    out.append(os.path.join(ROOT, "跑回测.bat"))
    return sorted(out)


def main():
    files = 列表()
    统计 = collections.Counter()
    明细 = []
    for p in files:
        rel = os.path.relpath(p, ROOT)
        if rel in SKIP:
            统计["跳过"] += 1
            continue
        raw = open(p, "rb").read()
        有BOM = raw[:3] == b"\xef\xbb\xbf"
        text = raw.decode("utf-8-sig")
        双CR = text.count("\r\r\n")
        孤立CR = text.count("\r") - text.count("\r\n")
        text = text.replace("\r\r\n", "\n").replace("\r\n", "\n").replace("\r", "\n")

        新 = text
        改注释 = 0
        if OLD_C in 新:
            assert 新.count(OLD_C) == 1, (rel, "注释锚点", 新.count(OLD_C))
            新 = 新.replace(OLD_C, NEW_C)
            改注释 = 1

        块 = NEW_ASCII if rel == os.path.join("工具", "回滚.bat") else NEW
        n = 新.count(OLD)
        if n:
            assert n == 1, (rel, "块锚点", n)
            新 = 新.replace(OLD, 块)
            统计["改解释器"] += 1
        else:
            统计["无锚点"] += 1
        明细.append((rel, n, 改注释, 双CR, 孤立CR, int(有BOM),
                     新 != text))

        if 新 != text:
            if GO:
                os.makedirs(os.path.join(ARCH, os.path.dirname(rel)), exist_ok=True)
                shutil.copy2(p, os.path.join(ARCH, rel))
                open(p, "w", encoding="utf-8", newline="\r\n").write(新)

    print("=" * 74)
    print("go =", GO, " 文件数 =", len(files), dict(统计))
    print("=" * 74)
    for r in 明细:
        if r[1] == 0 and r[2] == 0 and r[5] == 0 and r[3] == 0:
            continue
        print(f"  {r[0]:<52} 块{r[1]} 注释{r[2]} 双CR{r[3]} 孤立CR{r[4]} BOM{r[5]}")


main()
