# -*- coding: utf-8 -*-
"""回测冒烟总控：把「5 策略 × 12 品种」的回测入口各真跑一遍（短窗），
确认每个入口都能（1）起来、（2）取到行情、（3）落一份报告。

做法：复制真脚本 -> 只把练习区间的两个日期常量换成短窗 -> 交给 _冒烟运行器.py 跑。
真脚本一个字节都不改；账本、代码快照、回测结果/ 都不动（见运行器里的三处打桩）。

用法：python 冒烟.py [--窗口 月数]
"""
import os
import re
import subprocess
import sys
from datetime import date, timedelta

这里 = os.path.dirname(os.path.abspath(__file__))
_ROOT = 这里
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)

脚本目录 = os.path.join(这里, "_冒烟脚本")
结果目录 = os.path.join(这里, "_冒烟结果")
运行器 = os.path.join(这里, "_冒烟运行器.py")
PY = sys.executable

策略 = ["01_双均线", "02_均线排列", "03_均值回归", "04_通道突破", "05_摆动线"]
脚本名 = {"01_双均线": "双均线_回测.py", "02_均线排列": "均线排列_回测.py",
          "03_均值回归": "均值回归_回测.py", "04_通道突破": "通道突破_回测.py",
          "05_摆动线": "摆动线_回测.py"}

窗口起 = date(2016, 2, 1)
窗口止 = date(2016, 3, 31)


def 改窗口(src):
    src, n1 = re.subn(r"^IS_START\s*=\s*date\([^)]*\)",
                      "IS_START  = date(%d, %d, %d)" % (窗口起.year, 窗口起.month, 窗口起.day),
                      src, count=1, flags=re.M)
    src, n2 = re.subn(r"^IS_END\s*=\s*date\([^)]*\)",
                      "IS_END    = date(%d, %d, %d)" % (窗口止.year, 窗口止.month, 窗口止.day),
                      src, count=1, flags=re.M)
    assert n1 == 1 and n2 == 1, (n1, n2)
    return src


def 找报告(策略名, 品种名):
    d = os.path.join(结果目录, 策略名, 品种名)
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d) if f.endswith(".html"))


def 跑一个(策略名, 品种名):
    源 = os.path.join(_ROOT, "策略", 策略名, "回测", 品种名, 脚本名[策略名])
    if not os.path.exists(源):
        return {"状态": "无脚本", "说明": 源}
    临时 = os.path.join(脚本目录, 策略名, 品种名)
    os.makedirs(临时, exist_ok=True)
    临时脚本 = os.path.join(临时, 脚本名[策略名])
    open(临时脚本, "w", encoding="utf-8", newline="\n").write(
        改窗口(open(源, encoding="utf-8").read()))

    p = subprocess.run([PY, "-u", 运行器, 临时脚本], capture_output=True,
                       cwd=_ROOT, timeout=900)
    out = (p.stdout or b"").decode("utf-8", "replace")
    err = (p.stderr or b"").decode("utf-8", "replace")
    报告 = 找报告(策略名, 品种名)
    尾部 = [L for L in (out + "\n" + err).splitlines()
            if ("Error" in L or "Traceback" in L or "错误" in L or "异常" in L)]
    return {"状态": "OK" if (p.returncode == 0 and 报告) else "✗",
            "退出码": p.returncode, "报告": 报告, "报错": 尾部[:3],
            "输出行数": len(out.splitlines())}


def main():
    # 5 策略 × PVC（覆盖所有引擎函数）；1 策略（01） × 12 品种（覆盖所有品种的取数）
    任务 = [("01_双均线", v) for v in
            sorted(os.listdir(os.path.join(_ROOT, "策略", "01_双均线", "回测")))]
    任务 = [(s, v) for s, v in 任务 if os.path.isdir(os.path.join(_ROOT, "策略", s, "回测", v))]
    for s in 策略[1:]:
        任务.append((s, "PVC"))

    看了 = set()
    打印 = []
    for s, v in 任务:
        if (s, v) in 看了:
            continue
        看了.add((s, v))
        r = 跑一个(s, v)
        line = "%-12s %-10s %-4s 报告=%d" % (s, v, r["状态"], len(r.get("报告") or []))
        if r.get("报错"):
            line += "   " + " | ".join(x[:90] for x in r["报错"])
        print(line, flush=True)
        打印.append(line)
    print()
    print("完成:", sum(1 for L in 打印 if " OK " in L), "/", len(打印))
    open(os.path.join(这里, "_冒烟结果.txt"), "w", encoding="utf-8", newline="\n").write(
        "\n".join(打印) + "\n")


main()
