# -*- coding: utf-8 -*-
"""探测：纯碱 / 苯乙烯 / 生猪 在 2020-01 ~ 2024-12 这段的 30min 数据能不能取到。

背景：这仨品种上市晚（纯碱 2019-12-05 / 苯乙烯 2019-09-25 / 生猪 2021-01-07），
      在当前练习区间 2016-01-01~2019-12-31 上取不到行情。
      本脚本要回答的是：把区间拉到 2020 之后，数据是否可得、能否正常出报告。

做法：完全复用冒烟那套 —— 复制真脚本、只换 IS_START/IS_END、交给 _冒烟运行器.py
      （不弹浏览器 / 不写代码快照 / 不刷账本 / 报告落到 _冒烟结果/）。
      真脚本一个字节都不改。

取三个短窗覆盖「区间起点 / 中段 / 区间终点」，任一段能取到即说明该时段数据在库。
"""
import os
import re
import subprocess
import sys
from datetime import date

这里 = os.path.dirname(os.path.abspath(__file__))
_ROOT = 这里
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)

运行器 = os.path.join(这里, "_冒烟运行器.py")
结果目录 = os.path.join(这里, "_冒烟结果")
PY = sys.executable
策略 = "01_双均线"
脚本名 = "双均线_回测.py"

窗口 = [
    ("区间起点 2020年初", date(2020, 1, 2), date(2020, 1, 31)),
    ("中段 2022年中",     date(2022, 6, 1), date(2022, 6, 30)),
    ("区间终点 2024年末", date(2024, 12, 1), date(2024, 12, 31)),
]
品种表 = ["纯碱", "苯乙烯", "生猪"]


def 改窗口(src, 起, 止):
    src, n1 = re.subn(r"^IS_START\s*=\s*date\([^)]*\)",
                      "IS_START  = date(%d, %d, %d)" % (起.year, 起.month, 起.day),
                      src, count=1, flags=re.M)
    src, n2 = re.subn(r"^IS_END\s*=\s*date\([^)]*\)",
                      "IS_END    = date(%d, %d, %d)" % (止.year, 止.month, 止.day),
                      src, count=1, flags=re.M)
    assert n1 == 1 and n2 == 1, (n1, n2)
    return src


def 跑一个(品种, 名, 起, 止):
    源 = os.path.join(_ROOT, "策略", 策略, "回测", 品种, 脚本名)
    if not os.path.exists(源):
        return "无脚本: " + 源
    临时目录 = os.path.join(这里, "_探测脚本", 品种)
    os.makedirs(临时目录, exist_ok=True)
    临时 = os.path.join(临时目录, 脚本名)
    open(临时, "w", encoding="utf-8", newline="\n").write(
        改窗口(open(源, encoding="utf-8").read(), 起, 止))

    p = subprocess.run([PY, "-u", 运行器, 临时], capture_output=True,
                       cwd=_ROOT, timeout=900)
    out = (p.stdout or b"").decode("utf-8", "replace")
    err = (p.stderr or b"").decode("utf-8", "replace")
    报告目录 = os.path.join(结果目录, 策略, 品种)
    报告 = sorted(f for f in os.listdir(报告目录)
                  if f.endswith(".html")) if os.path.isdir(报告目录) else []
    尾部 = [L for L in (out + "\n" + err).splitlines()
            if ("Error" in L or "Traceback" in L or "没有行情" in L
                or "没上市" in L or "UnboundLocal" in L)]
    line = "%-8s %-16s 退出码=%-3s 报告=%d %s" % (
        品种, 名, p.returncode, len(报告),
        ("OK" if (p.returncode == 0 and 报告) else "✗"))
    if 尾部:
        line += "\n      " + "\n      ".join(x[:150] for x in 尾部[:4])
    return line


print("=== 探测：2020 段 30min 数据可得性（01_双均线）===", flush=True)
行 = []
for 名, 起, 止 in 窗口:
    print("-" * 70, flush=True)
    for v in 品种表:
        r = 跑一个(v, 名, 起, 止)
        print(r, flush=True)
        行.append(r)

print(flush=True)
print("完成。", flush=True)
open(os.path.join(这里, "_探测_2020段_结果.txt"), "w",
     encoding="utf-8", newline="\n").write("\n".join(行) + "\n")
