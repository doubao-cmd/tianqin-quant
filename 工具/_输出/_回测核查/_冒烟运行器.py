# -*- coding: utf-8 -*-
"""冒烟运行器：跑一个「被临时改过区间的」回测脚本，但把三件有副作用的事关掉。

  ① 不弹浏览器（os.startfile 打桩）—— 否则每跑一个就抢一次屏幕
  ② 不写代码快照（code_snapshot.存 打桩）—— 快照是 工具/回滚.py 的还原依据，
     临时脚本的快照会污染那条链
  ③ 不刷新账本（汇总报告.更新 打桩）—— 短窗冒烟的数字没有统计意义，
     不能写进账本
  另外把报告目录改到 工具/_输出/_回测核查/_冒烟结果/，不碰真的 回测结果/。

用法：python _冒烟运行器.py <临时脚本路径>
"""
import os
import sys
import runpy

_这里 = os.path.dirname(os.path.abspath(__file__))
_ROOT = _这里
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "工具"))

脚本 = sys.argv[1]

# ① 不弹浏览器
os.startfile = lambda *a, **k: None

# ② 不写代码快照
import lib.code_snapshot as _cs          # noqa: E402
_cs.存 = lambda *a, **k: None

# ③ 不刷新账本
import 汇总报告 as _hz                    # noqa: E402
_hz.更新 = lambda *a, **k: None
_hz.渲染 = lambda *a, **k: None

# 报告落到冒烟专用目录，不碰真的 回测结果/
import lib.engine as _E                   # noqa: E402
_E._DIR_回测结果 = os.path.join("工具", "_输出", "_回测核查", "_冒烟结果")

runpy.run_path(脚本, run_name="__main__")
