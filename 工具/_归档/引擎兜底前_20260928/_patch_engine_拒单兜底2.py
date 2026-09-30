# -*- coding: utf-8 -*-
"""兜底补丁 第二刀：让「被拒单提前结束」那条路径也能出**有数字的**报告。

第一刀之后实测（03 均值回归 / 纯碱）：
    退出码 0  ✓ 报告生成了 ✓ 权益打印出来了（7,301 元）✓
    但报告的统计全是 0：
        起始资金 0 / 结束资金 0 / 收益率 0.00% / 最大回撤 0.00%
    同时 tqsdk 自己在 close 时打的是真数：
        收益率 -96.35%, 最大回撤 96.63%, 年化夏普 -3.0783

原因：`sim.tqsdk_stat` 是 tqsdk **在 api.close() 时才结算**的。
      原收尾顺序是「先读 stat -> 再画报告 -> 最后 close」，
      正常跑完时没问题（BacktestFinished 抛出前 tqsdk 已经结算过），
      但被拒单打断时还没结算，读到的是空表 -> 报告等于废纸。

本刀改动（只动异常路径，正常路径逐字不变）：
  1) 在兜底分支末尾先 `api.close()`，把 tqsdk_stat 结算出来；
  2) 把 `trades = _collect_trades(api)` 改成
     `trades = [] if _兜底拒单 else _collect_trades(api)`
     —— api 已关，再取成交明细会失效；本路径放弃买卖点标注，换回真实数字。

用法：
  python _patch_engine_拒单兜底2.py          # 干跑
  python _patch_engine_拒单兜底2.py --go     # 真改
"""
import hashlib
import os
import shutil
import sys

这里 = os.path.dirname(os.path.abspath(__file__))
_ROOT = 这里
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)

引擎 = os.path.join(_ROOT, "lib", "engine.py")

旧1 = (
    "        print(f\"        **不是完整区间，不能当完整结果用**。要看完整区间得改仓位口径。\")\n"
    "        # ★ 先确认这段区间到底有没有行情。没有的话（该品种当时还没上市），\n"
)

新1 = (
    "        print(f\"        **不是完整区间，不能当完整结果用**。要看完整区间得改仓位口径。\")\n"
    "        # ★ 必须先关掉 api：tqsdk 是**在 close 时**才结算 tqsdk_stat 的。\n"
    "        #   不先关就往下走，stat 读到的全是 0（实测：报告收益率 0.00%、结束资金 0），\n"
    "        #   那份报告等于废纸。关掉之后 _collect_trades(api) 也会失效，\n"
    "        #   所以下面取成交明细时要跳过（本路径没有买卖点标注，但数字是真的）。\n"
    "        try:\n"
    "            _兜底局部[\"api\"].close()\n"
    "        except Exception:\n"
    "            pass\n"
    "        # ★ 先确认这段区间到底有没有行情。没有的话（该品种当时还没上市），\n"
)

旧2 = "        trades = _collect_trades(api)\n"
新2 = "        trades = [] if _兜底拒单 else _collect_trades(api)\n"


def 主():
    干跑 = "--go" not in sys.argv
    b = open(引擎, "rb").read()
    文本 = b.decode("utf-8")

    n1 = 文本.count(旧1)
    n2 = 文本.count(旧2)
    print("锚点1（打印行+注释）命中 : %d （应为 5）" % n1)
    print("锚点2（trades 收集行）命中: %d （应为 5）" % n2)
    print("已打过第二刀             : %d （应为 0）"
          % 文本.count("trades = [] if _兜底拒单 else _collect_trades(api)"))
    print("引擎 md5                 : %s" % hashlib.md5(b).hexdigest())

    if n1 != 5 or n2 != 5:
        print("\n✗ 锚点数不对，中止")
        return 1
    if 文本.count("trades = [] if _兜底拒单 else _collect_trades(api)") != 0:
        print("\n✗ 已经打过第二刀了，中止")
        return 1

    新文本 = 文本.replace(旧1, 新1).replace(旧2, 新2)
    print("\n替换后 锚点1 剩余: %d （应为 0）" % 新文本.count(旧1))
    print("替换后 锚点2 剩余: %d （应为 0）" % 新文本.count(旧2))
    print("替换后 新 trades : %d （应为 5）"
          % 新文本.count("trades = [] if _兜底拒单 else _collect_trades(api)"))
    print("替换后 close 插入: %d （应为 5）"
          % 新文本.count("            _兜底局部[\"api\"].close()"))

    try:
        compile(新文本, 引擎, "exec")
        print("语法检查          : 通过")
    except SyntaxError as e:
        print("\n✗ 语法错误：%s" % e)
        return 1

    if 干跑:
        print("\n这是干跑。要真改请加 --go")
        return 0

    备份 = os.path.join(这里, "engine_第二刀前.py")
    shutil.copy2(引擎, 备份)
    with open(引擎, "wb") as f:
        f.write(新文本.encode("utf-8"))

    b2 = open(引擎, "rb").read()
    print("\n已写入。新 md5 : %s" % hashlib.md5(b2).hexdigest())
    print("新行尾        : CRLF %d / 裸LF %d"
          % (b2.count(b"\r\n"), b2.count(b"\n") - b2.count(b"\r\n")))
    print("上一版备份    : %s" % os.path.relpath(备份, _ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(主())
