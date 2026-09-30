# -*- coding: utf-8 -*-
"""批量检测：各 bat 里「调用 python 的那一行」能不能被 cmd 正确解析。

背景
--------------------------------------------------------------
排查模拟盘 bat 时发现：同一位置写成 `"%PY%" -u 双均线_模拟盘.py`，
回测.bat 能正确解析，模拟盘.bat 却把整行吞掉、只剩乱码的脚本名。
怀疑与 cmd 读 UTF-8 bat 时的行为有关，但触发条件不明 ——
所以不猜，逐个实测。

做法
--------------------------------------------------------------
把每个 bat 复制到临时目录，**只在同一位置**把 python 调用换成
    pig.exe -u <原来的脚本名>
（pig.exe 不存在 ⇒ cmd 立刻报「不是内部或外部命令」，不会真跑任何东西）
然后看那句报错里是不是完整出现 `pig.exe`：

    'pig.exe' is not recognized ...            -> OK
    '??_模拟盘.py' is not recognized ...       -> 乱码（该行被吞）

只跑副本，真 bat 一个字节不动；不连网。

用法：
    python _检测bat中文行.py
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

这里 = os.path.dirname(os.path.abspath(__file__))
_ROOT = 这里
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)

临时 = tempfile.mkdtemp(prefix="batprobe_")
结果 = []


def 造副本(源):
    raw = open(源, "rb").read()
    try:
        t = raw.decode("utf-8")
    except UnicodeDecodeError:
        t = raw.decode("gbk", "replace")
    # 找 "%PY%" -u xxx.py  （%PY% 可能被替换过写法）
    命中 = re.findall(r'"?%PY%"?\s+-u\s+(\S+\.py)', t)
    if not 命中:
        return None, None
    目标 = 命中[0]
    新 = t.replace('"%PY%" -u ' + 目标, "pig.exe -u " + 目标, 1)
    if 新 == t:
        新 = re.sub(r'"?%PY%"?\s+-u\s+' + re.escape(目标), "pig.exe -u " + 目标, t, count=1)
    if 新 == t:
        return None, 目标
    p = os.path.join(临时, os.path.basename(os.path.dirname(源)) + "_" + os.path.basename(源))
    open(p, "wb").write(新.encode("utf-8"))
    return p, 目标


def 跑(p):
    """★ 必须喂 stdin：回测.bat 末尾有无条件 pause，不喂输入会挂到超时，
       被误判成「坏」。喂几个回车让它自己走过去。"""
    try:
        r = subprocess.run(["cmd", "/c", p], capture_output=True, timeout=25,
                           cwd=os.path.dirname(p), input=b"\r\n" * 10)
        s = (r.stdout or b"").decode("utf-8", "replace") + "\n" + \
            (r.stderr or b"").decode("utf-8", "replace")
    except subprocess.TimeoutExpired as e:
        s = ((e.stdout or b"") or b"").decode("utf-8", "replace") + "\n" + \
            ((e.stderr or b"") or b"").decode("utf-8", "replace")
        if "pig.exe" in s:
            return "OK(超时，但已见到 pig.exe)", s
        return "TIMEOUT(且没见到 pig.exe)", s
    # 可能有多条 unrecognized；优先取含 pig 的那条，其次取含 .py 的
    行列表 = [L.strip() for L in s.splitlines()
              if "is not recognized" in L or "不是内部或外部命令" in L]
    for L in 行列表:
        if "pig.exe" in L:
            return L, s
    if 行列表:
        return 行列表[0], s
    return "（没有出现 unrecognized 行）", s


bats = []
for r, d, fs in os.walk(os.path.join(_ROOT, "策略")):
    for f in fs:
        if f in ("模拟盘.bat", "回测.bat"):
            bats.append(os.path.join(r, f))
bats.sort()
print("待测 bat：%d 个" % len(bats), flush=True)
print("-" * 100, flush=True)

坏 = []
for i, b in enumerate(bats, 1):
    副本, 目标 = 造副本(b)
    if 副本 is None:
        结果.append((b, "跳过(没找到调用行)", 目标 or ""))
        continue
    报错, 全文 = 跑(副本)
    # ★ 「坏」的判据：报错里没出现完整的 pig.exe（说明这行被吞了）。
    #   注意要排除「TIMEOUT(且没见到 pig.exe)」那种自身就带 pig.exe 字样的描述。
    好 = ("pig.exe" in 报错) and ("没见到" not in 报错)
    if not 好:
        坏.append((b, 报错, 全文))
    结果.append((b, "OK" if 好 else "★乱码", 报错[:80]))
    print("[%2d/%d] %-58s %-6s %s" % (i, len(bats), os.path.relpath(b, _ROOT)[:58],
                                      "OK" if 好 else "★坏", 报错[:60]), flush=True)

print("-" * 100, flush=True)
print("正常 %d / 异常 %d" % (len(bats) - len(坏), len(坏)), flush=True)
if 坏:
    print()
    print("异常清单：")
    for b, e, _ in 坏:
        print("  %s\n      -> %s" % (os.path.relpath(b, _ROOT), e[:100]))

open(os.path.join(这里, "_检测bat中文行_结果.txt"), "w",
     encoding="utf-8", newline="\n").write(
    "\n".join("%-58s %-6s %s" % (os.path.relpath(b, _ROOT), s, e)
              for b, s, e in 结果) + "\n")
shutil.rmtree(临时, ignore_errors=True)
