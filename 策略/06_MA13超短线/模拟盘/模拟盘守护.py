# -*- coding: utf-8 -*-
"""兼容入口：真实实现已迁到 ASCII 文件名 ma13_watchdog_main.py。

为什么要有这个文件：启动用的 .bat 里**不能出现中文路径**。
bat 在 chcp 65001 之后，%~dp0 返回的是 ANSI(GBK) 字节，cmd 却按 UTF-8 解读，
中文段会被静默吃掉、路径塌陷到上级目录（2026-10-08 实测：项目根被解析成
C:\\Users\\cc\\Desktop\\，报Errno 2）。所以 .bat 通过
`for %%F in (*.py)` 按**ASCII 文件名**来定位本脚本。

本文件仅为转发层，保留中文名是为了不破坏已有的中文文档引用与手工运行方式。
新代码一律写在 ma13_watchdog_main.py。
"""
import os
import runpy
import sys

_REAL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "ma13_watchdog_main.py")

if not os.path.isfile(_REAL):
    sys.stderr.write(f"[ERROR] missing {_REAL}\n")
    raise SystemExit(1)

# runpy 以 __main__ 执行，直接复用真实脚本的 main() 与参数解析
runpy.run_path(_REAL, run_name="__main__")