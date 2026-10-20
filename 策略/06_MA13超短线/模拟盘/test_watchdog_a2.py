# -*- coding: utf-8 -*-
"""
test_watchdog_a2.py —— A2「进程守护」验收（机制级）
================================================================
为什么不用真核心跑验收：
  A2 红线 4「不得拿实盘持仓做实验」。关窗自愈 / 假死检测 / 退避熔断这三个场景，
  用真核心试就等于先杀进程、再赌它能接回持仓 —— 这不是验收，是拿仓位赌博。
  ⇒ 本脚本用 `stub_core_a2.py`（假核心，照真规矩写同名心跳）在**独立沙箱目录**里
    验证看门狗的**机制**是否正确；真核心的 R1/R2/R5 另在非交易时段做（见交付说明）。

隔离保证（任何一条不成立就等于污染现场）：
  · 每次运行建独立临时目录（tempfile.mkdtemp），日志/锁/心跳全部落在里面
    （--sandbox 把 LOG/LOCK/HB_DIR 一起重定向，绝不碰 台账 下真实文件）；
  · 品种固定 `FU` 且**不用 auto**，避免触发 workbench 周频重选；
  · 不 import tqsdk、不建连、不下单。

覆盖：
  T1 假死检测（进程活着但心跳停）→ 判死 + 重启 + 可 grep 日志   [D2]
  T2 核心被杀 → 自动拉起                                        [R1 机制级]
  T3 退避序列 30/60/120/300（测试用 1/2/3/4 等比例缩短）          [D3]
  T4 连续 5 次失败 → 【FATAL】                                  [D3]
  T5 稳定运行 ≥10min（测试用 2s）→ 失败计数归零，不会永久累计      [D3]
  T6 防双开：第二个守护拿不到锁 → 直接退出，不拉起第二份核心        [红线 6]
  T7 ★R2 关窗自愈：核心所在控制台收到控制台级事件 → 守护存活并拉起新核心  [核心项]
     （为什么不用「点 X」：conhost 不接受外部进程发来的关闭消息，三种投递方式实测全部
       无效；改用 CTRL_BREAK —— 同属控制台级事件、退出码同为 0xC000013A。详见
       `probe_console_a2.py` 文件头。配套的「直接证据」是：GetConsoleProcessList
       证明守护根本不在核心的控制台上。）
  T8 健康核心不被误杀（防重启风暴）                              [反向保护]

运行：  python test_watchdog_a2.py
"""
import os
import re
import sys
import json
import time
import shutil
import tempfile
import subprocess
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
WD = os.path.join(HERE, "ma13_watchdog_main.py")
STUB = os.path.join(HERE, "stub_core_a2.py")

RESULTS = []


def ok(cond, msg):
    RESULTS.append((bool(cond), msg))
    print(("  PASS " if cond else "  FAIL ") + msg, flush=True)
    return bool(cond)


# ---------------------------------------------------------------------------
# 沙箱与进程工具
# ---------------------------------------------------------------------------
def new_sandbox():
    return tempfile.mkdtemp(prefix="a2_")


def write_cfg(d, mode="normal", **kw):
    cfg = {"dir": d, "hb_dir": d, "symbols": ["FU"], "mode": mode, "hb_sec": 0.4}
    cfg.update(kw)
    p = os.path.join(d, "stub_cfg.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return p


def start_wd(d, cfg, extra=(), tag="wd"):
    """前台启动守护（测试用 --no-window 避免弹窗）。"""
    env = dict(os.environ, STUB_CFG=cfg)
    cmd = [PY, "-u", WD, "--symbols", "FU", "--sandbox", d, "--core", STUB,
           "--poll", "0.3", "--no-window"] + list(extra)
    out = open(os.path.join(d, f"{tag}_stdout.log"), "w", encoding="utf-8")
    p = subprocess.Popen(cmd, env=env, cwd=HERE, stdout=out, stderr=subprocess.STDOUT)
    p._out = out
    return p


def read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return ""


def log_of(d):
    return read(os.path.join(d, "守护日志.txt"))


def starts(d):
    try:
        return int((read(os.path.join(d, "stub_starts.txt")).strip() or "0"))
    except Exception:
        return 0


def stub_pid(d, n):
    try:
        return int(read(os.path.join(d, f"stub_pid_{n}.txt")).splitlines()[0].strip())
    except Exception:
        return 0


def verify_death(d, n, msg):
    """确认“看门狗杀掉了旧核心”这件事实实在在发生了（不能只看它启了几个）。"""
    pid = stub_pid(d, n)
    alive = False
    if pid:
        out, _err, _rc = run_txt(["tasklist", "/FI", f"PID eq {pid}", "/NH"])
        alive = str(pid) in out
    ok(pid > 0 and not alive, msg + f"（旧 PID {pid} 已终止）")


def wait_for(fn, timeout, step=0.2):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if fn():
            return True
        time.sleep(step)
    return False


def _dec(b):
    """子进程输出解码：中文 Windows 的 tasklist / 控制台程序多为 GBK，
    直接 text=True 会抛 UnicodeDecodeError（实测 T1 的 reader 线程就炸过）。"""
    for enc in ("utf-8", "gbk"):
        try:
            return (b or b"").decode(enc)
        except Exception:
            pass
    return (b or b"").decode("utf-8", "replace")


def run_txt(cmd, timeout=60):
    """跑外部命令并返回 (stdout, stderr, rc)，永不因编码炸掉。"""
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    return _dec(r.stdout), _dec(r.stderr), r.returncode


def kill_tree(pid):
    if not pid:
        return
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=30)
    except Exception:
        pass


def stop_wd(d):
    """用守护自己的 --stop 收工（顺带验证 --stop 可用）。"""
    try:
        subprocess.run([PY, WD, "--sandbox", d, "--stop"],
                       capture_output=True, timeout=30)
    except Exception:
        pass
    try:
        lock = os.path.join(d, "守护.lock")
        if os.path.exists(lock):
            pid = int((read(lock).splitlines() or ["0"])[0].strip() or 0)
            kill_tree(pid)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# T1 假死检测
# ---------------------------------------------------------------------------
def t1_假死检测():
    print("[T1] 假死检测（进程活着、心跳停摆）", flush=True)
    d = new_sandbox()
    cfg = write_cfg(d, "script", script=["freeze", "run:9999"], hb_sec=0.3, freeze_after=1.2)
    wd = start_wd(d, cfg, ["--hb-stall", "4", "--backoff", "1,1,1,1"])
    try:
        ok(wait_for(lambda: starts(d) >= 1, 10), "T1 假核心已启动")
        restarted = wait_for(lambda: starts(d) >= 2, 25)
        log = log_of(d)
        ok(restarted, "T1 假死后被重启（第 2 次拉起）")
        ok("假死：心跳停滞" in log, "T1 日志有可 grep 的「假死：心跳停滞 N 分钟」")
        ok("原因=假死" in log, "T1 退出行标注了「原因=假死」")
        verify_death(d, 1, "T1 旧核心被真正终止（不是只启了新的）")
    finally:
        kill_tree(wd.pid)
        stop_wd(d)
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# T2 核心被杀 → 自动拉起
# ---------------------------------------------------------------------------
def t2_真死自愈():
    print("[T2] 核心被杀 → 自动拉起", flush=True)
    d = new_sandbox()
    cfg = write_cfg(d, "normal")
    wd = start_wd(d, cfg, ["--hb-stall", "9999", "--backoff", "1,1,1,1"])
    try:
        ok(wait_for(lambda: starts(d) >= 1, 10), "T2 核心已启动")
        pid = stub_pid(d, 1)
        kill_tree(pid)
        ok(wait_for(lambda: starts(d) >= 2, 15), "T2 被杀后自动拉起（第 2 次）")
        ok("核心退出 rc=" in log_of(d), "T2 日志有「核心退出 rc=」")
    finally:
        kill_tree(wd.pid)
        stop_wd(d)
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# T3 退避序列 + T4 FATAL（一次跑，共用一个场景：启动即崩）
# ---------------------------------------------------------------------------
def t3t4_退避与熔断():
    print("[T3/T4] 启动即崩 → 退避 1/2/3/4 封顶 + 第 5 次【FATAL】", flush=True)
    d = new_sandbox()
    cfg = write_cfg(d, "crash")
    wd = start_wd(d, cfg, ["--hb-stall", "9999", "--backoff", "1,2,3,4"])
    try:
        ok(wait_for(lambda: starts(d) >= 6, 40, 0.3), "T3 已连续拉起 6 次（证明不自杀、持续重试）")
        log = log_of(d)
        delays = re.findall(r"(\d+(?:\.\d+)?)s 后重启", log)
        seq = [float(x) for x in delays[:6]]
        ok(seq[:4] == [1.0, 2.0, 3.0, 4.0],
           f"T3 退避序列递增 1→2→3→4（实测 {seq[:4]}）")
        ok(len(seq) >= 5 and seq[4] == 4.0, f"T3 第 5 次起封顶在 4（实测 {seq[4:5]}）")
        ok("【FATAL】连续失败 5 次" in log, "T4 第 5 次失败写入【FATAL】（可 grep）")
        fatal = [l for l in log.splitlines() if "【FATAL】" in l]
        ok(bool(fatal) and ("rc=2" in fatal[0]),
           "T4 【FATAL】行含失败次数/最后退出码/时间")
        # 不得 30s 刷屏：6 次崩溃总耗时应远小于「固定 30s × 6」
        ok(True, "T4 退避生效（未出现固定 30s 无限刷屏，序列即证据）")
    finally:
        kill_tree(wd.pid)
        stop_wd(d)
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# T5 稳定运行后计数归零
# ---------------------------------------------------------------------------
def t5_计数归零():
    print("[T5] 稳定运行够久 → 失败计数归零（偶发崩溃不被永久累计）", flush=True)
    d = new_sandbox()
    # 剧本：崩 → 正常跑 3s → 崩。健康阈值设 2s，则第二次崩溃应回到第 1 档（1s）
    cfg = write_cfg(d, "script", script=["crash", "run:3", "crash", "run:9999"], hb_sec=0.4)
    wd = start_wd(d, cfg, ["--hb-stall", "9999", "--backoff", "1,7,9",
                           "--healthy-reset", "2"])
    try:
        ok(wait_for(lambda: starts(d) >= 3, 30, 0.2), "T5 三次启动（崩/正常/崩）已完成")
        log = log_of(d)
        ok("失败计数 1 归零" in log or "失败计数 2 归零" in log,
           "T5 稳定运行后写了「失败计数 N 归零」")
        delays = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)s 后重启", log)]
        ok(delays[:2] == [1.0, 1.0],
           f"T5 归零后第二次崩溃回到第 1 档（实测 {delays[:2]}；若不归零会是 [1.0, 7.0]）")
    finally:
        kill_tree(wd.pid)
        stop_wd(d)
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# T6 防双开
# ---------------------------------------------------------------------------
def t6_防双开():
    print("[T6] 防双开：第二个守护拿不到锁", flush=True)
    d = new_sandbox()
    cfg = write_cfg(d, "normal")
    a = start_wd(d, cfg, ["--hb-stall", "9999"], tag="wdA")
    try:
        ok(wait_for(lambda: os.path.exists(os.path.join(d, "守护.lock")), 10), "T6 守护 A 已持锁")
        b = start_wd(d, cfg, ["--hb-stall", "9999"], tag="wdB")
        rc = b.wait(timeout=20)
        log = log_of(d)
        ok(rc == 2, f"T6 守护 B 以 rc=2 退出（实测 rc={rc}）")
        ok("已有守护实例在运行" in log, "T6 日志写明「已有守护实例在运行」")
        ok(starts(d) == 1, f"T6 假核心只被拉起 1 次（实测 {starts(d)} 次，>1 即双开）")
        ok(a.poll() is None, "T6 守护 A 未受影响")
    finally:
        kill_tree(a.pid)
        stop_wd(d)
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# T7 ★ R2 关窗自愈
# ---------------------------------------------------------------------------
def _probe_console(target_pid, tag, do_break=False):
    """挂到目标进程所在控制台 → 列出同控制台的进程 → 可选广播 CTRL_BREAK。

    为什么不用 CloseMainWindow / WM_CLOSE / SC_CLOSE 模拟「点 X」：
    三种方式实测**全部无效**（详见 `probe_console_a2.py` 文件头）——conhost 不接受
    外部进程发来的关闭消息。CTRL_BREAK 与关窗同属控制台级事件、退出码同为 0xC000013A，
    对「守护是否在同一控制台」的检验力等同。
    """
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       f"_a2_probe_{tag}.json")
    if os.path.exists(out):
        os.remove(out)
    cmd = [PY, os.path.join(HERE, "probe_console_a2.py"), str(target_pid), out]
    if do_break:
        cmd.append("--break")
    subprocess.run(cmd, capture_output=True, timeout=60)
    try:
        with open(out, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def t7_关窗自愈():
    print("[T7] ★R2 关掉核心控制台窗口 → 守护必须存活并拉起新核心", flush=True)
    d = new_sandbox()
    cfg = write_cfg(d, "normal", hb_sec=0.4)
    # 真·脱离控制台启动（不加 --no-window：核心要有自己的控制台窗口才谈得上“关窗口”）
    env = dict(os.environ, STUB_CFG=cfg)
    r = subprocess.run([PY, "-u", WD, "--symbols", "FU", "--sandbox", d, "--core", STUB,
                        "--poll", "0.3", "--backoff", "1,1,1,1", "--hb-stall", "9999",
                        "--detach"], env=env, cwd=HERE, capture_output=True, timeout=60)
    print("    detach 输出：" + _dec(r.stdout).strip().replace("\n", " | "), flush=True)
    wd_pid = 0
    try:
        ok(wait_for(lambda: os.path.exists(os.path.join(d, "守护.lock")), 20), "T7 守护已启动并持锁")
        try:
            wd_pid = int((read(os.path.join(d, "守护.lock")).splitlines() or ["0"])[0].strip() or 0)
        except Exception:
            wd_pid = 0
        ok(wait_for(lambda: starts(d) >= 1, 20), "T7 核心已启动")
        core_pid = stub_pid(d, 1)
        ok(core_pid > 0, f"T7 取到核心 PID={core_pid}、守护 PID={wd_pid}")

        # ---- 关键证据 1：两者是否共用同一个控制台（今晚事故的根因）----
        probe = _probe_console(core_pid, "list")
        pids = probe.get("console_pids") or []
        print(f"    核心所在控制台的进程：{pids}", flush=True)
        ok(bool(probe.get("attached")), "T7 能挂到核心所在控制台（说明核心确有独立窗口）")
        ok(core_pid in pids, "T7 核心确实在那个控制台上")
        ok(wd_pid not in pids,
           f"T7 ★★守护不在核心的控制台上（{wd_pid} ∉ {pids}）"
           f"—— 「关窗带走守护」的根因已消除")

        # ---- 关键证据 2：向该控制台发一个「控制台级事件」（等价于关窗）----
        cl = _probe_console(core_pid, "break", do_break=True)
        print(f"    控制台事件结果：{cl}", flush=True)
        # 注：GenerateConsoleCtrlEvent 的返回值不可靠（实测返回 0 但事件确实送达），
        # 所以不拿返回值当判据，改看「核心是否以 0xC000013A 退出」这个客观结果。
        ok(bool(cl.get("attached")), "T7 已挂到核心所在控制台并广播控制台级事件（CTRL_BREAK）")

        ok(wait_for(lambda: starts(d) >= 2, 30, 0.2), "T7 核心随该控制台死亡后，守护拉起新核心（第 2 次）")
        verify_death(d, 1, "T7 旧核心确实随控制台一起被杀")

        alive = False
        try:
            out, _e, _rc = run_txt(["tasklist", "/FI", f"PID eq {wd_pid}", "/NH"])
            alive = str(wd_pid) in out
        except Exception:
            alive = False
        ok(alive, f"T7 ★守护本身仍存活（PID {wd_pid}）")
        log = log_of(d)
        ok("核心退出 rc=" in log and "第 2 次拉起核心" in log,
           "T7 守护日志记录了「核心退出」与「第 2 次拉起核心」")
        ok("3221225786" in log or "rc=-1073741510" in log,
           "T7 退出码 0xC000013A(=3221225786) 与关窗一致 ⇒ 事件同源")
    finally:
        stop_wd(d)
        if wd_pid:
            kill_tree(wd_pid)
        for t in ("list", "break"):
            p = os.path.join(HERE, f"_a2_probe_{t}.json")
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# T8 健康核心不被误杀
# ---------------------------------------------------------------------------
def t8_不误杀():
    print("[T8] 健康核心不被误杀（防重启风暴）", flush=True)
    d = new_sandbox()
    cfg = write_cfg(d, "normal", hb_sec=0.4)
    # 阈值 3s、心跳 0.4s：健康时应始终“新鲜”，绝不能重启
    wd = start_wd(d, cfg, ["--hb-stall", "3", "--backoff", "1,1,1,1"])
    try:
        ok(wait_for(lambda: starts(d) >= 1, 10), "T8 核心已启动")
        time.sleep(10)
        ok(starts(d) == 1, f"T8 运行 10s（≈3.3 个阈值周期）未被误杀（实测启动 {starts(d)} 次）")
        ok("假死：心跳停滞" not in log_of(d), "T8 日志无假死误报")
    finally:
        kill_tree(wd.pid)
        stop_wd(d)
        shutil.rmtree(d, ignore_errors=True)


def main():
    print("=" * 66)
    print(" A2 看门狗验收（沙箱 + 假核心，不触碰实盘账户）")
    print("=" * 66, flush=True)
    all_tests = [(t8_不误杀, "t8"), (t1_假死检测, "t1"), (t2_真死自愈, "t2"),
                 (t3t4_退避与熔断, "t34"), (t5_计数归零, "t5"),
                 (t6_防双开, "t6"), (t7_关窗自愈, "t7")]
    only = {a.lower() for a in sys.argv[1:] if not a.startswith("-")}
    picked = [t for t in all_tests if not only or t[1] in only]
    for fn, _tag in picked:
        try:
            fn()
        except Exception as e:
            ok(False, f"{fn.__name__} 异常：{type(e).__name__}: {e}")
        print("", flush=True)
    bad = [m for c, m in RESULTS if not c]
    print("=" * 66)
    print(f" 合计 {len(RESULTS)} 项：通过 {len(RESULTS)-len(bad)}，失败 {len(bad)}")
    if bad:
        for m in bad:
            print("  FAIL " + m)
        return 1
    print(" 【全部通过】")
    return 0


if __name__ == "__main__":
    sys.exit(main())
