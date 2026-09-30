# -*- coding: utf-8 -*-
"""跑单个回测脚本，但不弹浏览器。

只打桩 os.startfile 一件事（否则每跑完一个就抢一次屏幕）。
账本刷新（汇总报告.更新）与代码快照都**照常执行** —— 这是正式回测，不是冒烟。

用法：python _跑单个_不弹窗.py <回测脚本路径>
"""
import os
import runpy
import sys

os.startfile = lambda *a, **k: None
runpy.run_path(sys.argv[1], run_name="__main__")
