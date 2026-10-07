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

防双开（2026-10-07 加）：台账/守护.lock 记录本机 PIDPID。
     每日 08:55 自动启动场景下，锁是必需的——否则重复触发会拉起两个核心、
     双份下单 + 白占一个天勤席位。锁里的 PID 已不存在（崩溃/强杀）会自动接管。
"""
import sys, os, time, subprocess, argparse, io
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.join(_HERE, "ma13_live_multi.py")
LOG = os.path.join(_HERE, "台账", "守护日志.txt")
LOCK = os.path.join(_HERE, "台账", "守护.lock")
RESTART_WAIT = 30      # 核心崩掉后等待秒数

PY = sys.executable    # 用同一个解释器拉起核心


def _pid_alive(pid):
    """该 PID 是否还活着（Windows 用 OpenProcess 判，别用 psutil）。"""
    if not pid or pid <= 0:
        return False
    try:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k = ctypes.windll.kernel32
        h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, int(pid))
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == STILL_ACTIVE
    except Exception:
        return False


def _抢锁():
    """抢守护锁。返回 True=抢到（可以跑），False=已有实例在跑。"""
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    me = os.getpid()
    for _ in range(2):
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(f"{me}\r\n{_HERE}\r\n{datetime.now():%Y-%m-%d %H:%M:%S}\r\n")
            return True
        except FileExistsError:
            # 已有锁：里面 PID 还活着 ⇒ 真有实例在跑
            old = 0
            try:
                with io.open(LOCK, "r", encoding="utf-8") as f:
                    old = int((f.readline() or "0").strip() or 0)
            except Exception:
                old = 0
            if _pid_alive(old):
                return False
            # 锁是僵尸（上次崩溃/强杀留下的）⇒ 清掉重抢
            _log(f"发现僵尸锁（PID {old} 已不存在），接管")
            try:
                os.remove(LOCK)
            except Exception:
                return False
    return False



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
    if not _抢锁():
        _log("已有守护实例在运行（锁未拿到），本次不重复启动。")
        return
    _log(f"守护启动 symbols={args.symbols} trade={args.trade} pid={os.getpid()}")
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
    finally:
        # 只删自己的锁（锁里 PID 是我才删，防误删别人的）
        try:
            with io.open(LOCK, "r", encoding="utf-8") as f:
                mine = int((f.readline() or "0").strip() or 0) == os.getpid()
            if mine:
                os.remove(LOCK)
                _log("已释放守护锁")
        except Exception:
            pass


if __name__ == "__main__":
    main()
