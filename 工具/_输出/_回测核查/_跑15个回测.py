# -*- coding: utf-8 -*-
"""晚上市品种（纯碱 / 苯乙烯 / 生猪）× 5 策略 = 15 个回测，串行挂后台跑。

背景：这仨品种的练习区间 2026-09-28 由 2016-01-01~2019-12-31 改为
      2020-01-01~2022-12-31（上市晚，2016-2019 无行情）。
      本脚本在新区间上把这 15 个入口各跑一遍，出报告 + 刷账本。

只打桩 os.startfile（不弹浏览器）。账本刷新、代码快照都照常。

用法：python _跑15个回测.py
"""
import os
import subprocess
import sys
import time

这里 = os.path.dirname(os.path.abspath(__file__))
_ROOT = 这里
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)

PY = sys.executable
运行器 = os.path.join(这里, "_跑单个_不弹窗.py")
日志 = os.path.join(这里, "_跑15个回测.log")

策略表 = [("01_双均线", "双均线_回测.py"), ("02_均线排列", "均线排列_回测.py"),
          ("03_均值回归", "均值回归_回测.py"), ("04_通道突破", "通道突破_回测.py"),
          ("05_摆动线", "摆动线_回测.py")]
品种表 = ["纯碱", "苯乙烯", "生猪"]


def 写(s):
    with open(日志, "a", encoding="utf-8", newline="\n") as f:
        f.write(s + "\n")
    print(s, flush=True)


def 报告列表(策略, 品种):
    d = os.path.join(_ROOT, "回测结果", 策略, 品种)
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d) if f.endswith(".html"))


def main():
    open(日志, "w", encoding="utf-8", newline="\n").close()
    写("=" * 78)
    写("  新练习区间 2020-01-01 ~ 2022-12-31 · 15 个回测  开始 %s"
       % time.strftime("%Y-%m-%d %H:%M:%S"))
    写("=" * 78)

    成功, 失败, 明细 = 0, [], []
    for i, (策略, 脚本名) in enumerate(
            [(s, n) for s, n in 策略表 for _ in 品种表], 1):
        品种 = 品种表[(i - 1) % 3]
        路径 = os.path.join(_ROOT, "策略", 策略, "回测", 品种, 脚本名)
        if not os.path.exists(路径):
            写("[%2d] %s / %-6s 跳过：脚本不存在" % (i, 策略, 品种))
            失败.append((策略, 品种, "脚本不存在"))
            continue

        写("")
        写("[%2d/15] %s / %s  开始 %s" % (i, 策略, 品种, time.strftime("%H:%M:%S")))
        t0 = time.time()
        try:
            p = subprocess.run([PY, "-u", 运行器, 路径], capture_output=True,
                               cwd=_ROOT, timeout=3 * 3600)
            out = (p.stdout or b"").decode("utf-8", "replace")
            err = (p.stderr or b"").decode("utf-8", "replace")
            码 = p.returncode
        except subprocess.TimeoutExpired:
            out, err, 码 = "", "子进程 3 小时未退出", -999
        耗时 = time.time() - t0

        报告 = 报告列表(策略, 品种)
        尾巴 = [L for L in (out + "\n" + err).splitlines()
                if ("Error" in L or "Traceback" in L or "没有行情" in L
                    or "没上市" in L)]
        ok = (码 == 0 and 报告)
        if ok:
            成功 += 1
            写("       完成 %.1f 分钟 | 退出码 %d | 报告 %d 份：%s"
               % (耗时 / 60, 码, len(报告), 报告[-1] if 报告 else ""))
        else:
            失败.append((策略, 品种, "退出码 %d / 报告 %d" % (码, len(报告))))
            写("       ✗ 失败 %.1f 分钟 | 退出码 %d | 报告 %d 份"
               % (耗时 / 60, 码, len(报告)))
            for L in 尾巴[:6]:
                写("         " + L[:160])
        明细.append((策略, 品种, 耗时, ok))

    写("")
    写("=" * 78)
    写("  汇总：成功 %d / 15" % 成功)
    for 策略, 品种, 耗时, ok in 明细:
        写("    %-10s %-6s %6.1f 分钟  %s" % (策略, 品种, 耗时 / 60,
                                             "OK" if ok else "✗"))
    if 失败:
        写("  失败：")
        for a, b, c in 失败:
            写("    %s / %s -> %s" % (a, b, c))
    写("  结束 %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    写("=" * 78)
    return 0 if not 失败 else 1


if __name__ == "__main__":
    raise SystemExit(main())
