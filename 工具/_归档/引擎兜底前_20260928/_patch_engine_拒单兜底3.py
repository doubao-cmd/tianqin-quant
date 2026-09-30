# -*- coding: utf-8 -*-
"""兜底补丁 第三刀：让「提前结束」的报告在标题栏自己说明白，避免被误读成完整结果。

为什么必须加：
  第一/二刀之后，03 均值回归 / 纯碱 的报告已经能出、数字也是真的（-96.35%）。
  但报告头写的是「信号评估区间：2020-01-01 ~ 2022-12-31」，
  实际只跑到 2020-10-20 —— 拿着这份报告的人会以为它是**三年**的结果。

本刀改动（只动异常路径）：
  5 个回测函数往报告备注 `extra_note` 最前面插一句，
  且只在 `_兜底拒单` 为真（本次回测是被拒单打断的）时才插，正常跑完逐字不变。

用法：
  python _patch_engine_拒单兜底3.py          # 干跑
  python _patch_engine_拒单兜底3.py --go     # 真改
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

警告语 = ("\"⚠️ 本报告不完整：本次回测因下单被柜台拒绝而提前结束，"
          "只覆盖到中断那一刻（不是完整区间，别当完整结果用）\"")

旧1 = (
    "            extra_note=\" ｜ \".join(x for x in [\n"
    "                (f\"信号评估区间：{eval_start} ~ {end}\" if eval_start else \"\"),\n"
)
新1 = (
    "            extra_note=\" ｜ \".join(x for x in [\n"
    "                (" + 警告语 + " if _兜底拒单 else \"\"),\n"
    "                (f\"信号评估区间：{eval_start} ~ {end}\" if eval_start else \"\"),\n"
)

旧2 = "            extra_note=extra, kline_history=kline_history, signal_events=signal_events,\n"
新2 = (
    "            extra_note=((" + 警告语 + " + \" ｜ \") if _兜底拒单 else \"\") + extra,\n"
    "            kline_history=kline_history, signal_events=signal_events,\n"
)


def 主():
    干跑 = "--go" not in sys.argv
    b = open(引擎, "rb").read()
    文本 = b.decode("utf-8")

    n1 = 文本.count(旧1)
    n2 = 文本.count(旧2)
    print("锚点1（run_backtest 的 join 写法）: %d （应为 1）" % n1)
    print("锚点2（其余 4 个函数的 extra）    : %d （应为 4）" % n2)
    print("已打过第三刀                     : %d （应为 0）"
          % 文本.count("if _兜底拒单 else \"\")"))
    print("引擎 md5                         : %s" % hashlib.md5(b).hexdigest())

    if n1 != 1 or n2 != 4:
        print("\n✗ 锚点数不对，中止")
        return 1
    if "本报告不完整" in 文本:
        print("\n✗ 看起来已经打过第三刀了，中止")
        return 1

    新文本 = 文本.replace(旧1, 新1).replace(旧2, 新2)
    print("\n替换后 锚点1 剩余: %d （应为 0）" % 新文本.count(旧1))
    print("替换后 锚点2 剩余: %d （应为 0）" % 新文本.count(旧2))
    print("替换后 警告语数量: %d （应为 5）" % 新文本.count("本报告不完整"))

    try:
        compile(新文本, 引擎, "exec")
        print("语法检查          : 通过")
    except SyntaxError as e:
        print("\n✗ 语法错误：%s" % e)
        return 1

    if 干跑:
        print("\n这是干跑。要真改请加 --go")
        return 0

    备份 = os.path.join(这里, "engine_第三刀前.py")
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
