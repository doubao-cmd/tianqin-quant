# -*- coding: utf-8 -*-
"""
MA13 模拟盘 · 进程级守护（第二层保险）
==========================================================
第一层保险在 ma13_live_multi.py 内部：断线/被踢 -> 记台账 -> 自动重连。
本脚本是第二层：万一核心整个崩掉（未捕获异常/崩溃），30 秒后重新拉起，
并把每次重启记入 台账/守护日志.txt。

用法（ bat 已接入，一般不手动跑）：
  python 模拟盘守护.py --symbols P,EB [--trade]

停止：Ctrl+C（守护和核心一起退出），或直接关窗口。
日志位置：策略/06_MA13超短线/模拟盘/台账/守护日志.txt
"""
import sys, os, time, subprocess, argparse, io
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.join(_HERE, "ma13_live_multi.py")
LOG = os.path.join(_HERE, "台账", "守护日志.txt")
RESTART_WAIT = 30      # 核心崩掉后等待秒数

PY = sys.executable    # 用同一个解释器拉起核心


def _log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(f"[守护进程] {msg}", flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with io.open(LOG, "a", encoding="utf-8", newline="") as f:
            f.write(line + "\r\n")
    except Exception:
        pass                      # 日志失败绝不能影响守护本身


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="P,EB")
    ap.add_argument("--trade", action="store_true")
    args = ap.parse_args()

    cmd = [PY, "-u", CORE, "--symbols", args.symbols]
    if args.trade:
        cmd.append("--trade")

    n = 0
    _log(f"守护启动 symbols={args.symbols} trade={args.trade}")
    try:
        while True:
            n += 1
            _log(f"第 {n} 次拉起核心：{' '.join(cmd[1:])}")
            try:
                rc = subprocess.call(cmd)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                rc = -1
                _log(f"拉起核心失败：{type(e).__name__}: {e}")
            _log(f"核心退出 rc={rc}，{RESTART_WAIT}s 后重启")
            time.sleep(RESTART_WAIT)
    except KeyboardInterrupt:
        _log("守护收到 Ctrl+C，正常退出")


if __name__ == "__main__":
    main()
