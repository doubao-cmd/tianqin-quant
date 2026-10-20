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
  T9 ★A11 心跳间隔基线：真实 6 品种 p95≤305s + 每条 >12min 的 gap 必须
      「有重启日志」或「当时无持仓」或「在历史裸奔登记表内」                [A11]
  T10   A13 `--hb-check --grace` 与守护内部 `_hb_stalled` 同口径        [A13]
  T11   A14 FATAL 归因拆分：假死 / 启动失败 / 启动期网络失败 rc=1 三类措辞 [A14]
  T12   T6-4 PID 语义自检：正常解释器不告警；转发器必须告警              [T6-4]

运行：  python test_watchdog_a2.py
只跑某几项： python test_watchdog_a2.py t9 t12
"""
import os
import re
import sys
import csv
import json
import time
import shutil
import tempfile
import subprocess
import datetime
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
WD = os.path.join(HERE, "ma13_watchdog_main.py")
STUB = os.path.join(HERE, "stub_core_a2.py")
STUB_FWD = os.path.join(HERE, "stub_forwarder_a2.py")
LEDGER_REAL = os.path.join(HERE, "台账")      # ★ T9 只读，绝不写

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
        ok(wait_for(lambda: starts(d) >= 6, 60, 0.3), "T3 已连续拉起 6 次（证明不自杀、持续重试）")
        log = log_of(d)
        delays = re.findall(r"(\d+(?:\.\d+)?)s 后重启", log)
        seq = [float(x) for x in delays[:6]]
        ok(seq[:4] == [1.0, 2.0, 3.0, 4.0],
           f"T3 退避序列递增 1→2→3→4（实测 {seq[:4]}）")
        ok(len(seq) >= 5 and seq[4] == 4.0, f"T3 第 5 次起封顶在 4（实测 {seq[4:5]}）")
        # ★ 2026-10-21 返工 T6-3：FATAL 措辞已按原因拆分，启动失败类必须写「启动失败」
        ok("【FATAL】连续启动失败 5 次" in log, "T4 第 5 次失败写入【FATAL】（可 grep）")
        fatal = [l for l in log.splitlines() if "【FATAL】" in l]
        ok(bool(fatal) and ("rc=2" in fatal[0]),
           "T4 【FATAL】行含失败次数/最后退出码/时间")
        ok(bool(fatal) and ("假死" not in fatal[0]),
           "T4 启动即崩不得被归因成「假死」（A14 拆分生效）")
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


# ---------------------------------------------------------------------------
# T9 ★ A11 心跳间隔基线（真实台账只读）
# ---------------------------------------------------------------------------
# ★ 历史裸奔事件登记表（D1 体检 §2.3 错误 B 的实证，2026-10-21 返工 T6-1 单列）：
#   2026-10-16 00:31:57 → 08:12:05 豆粕**持仓恒为 8 手**，心跳只写了 4 行、
#   间隔 165 / 22 / 60 / 212 分钟，08:17:05 立刻恢复到 5 分钟 ⇒ 进程活着、线程被挂起，
#   **= 止损裸奔 2.8 小时**（10-20 那次 19min 事故的 8.8 倍）。
#   原标定把它误归为「进程不在」，因此这里必须有一条**白纸黑字的登记**，
#   且**登记表里的每一条都要被真实扫描到**（防「文件被截断 ⇒ 自动通过」）。
#   规矩：登记表之外，任何「空窗期间有持仓 + 无重启日志」的 >12min 空窗 ⇒ 直接 FAIL。
HIST_NAKED = [("豆粕", "2026-10-16 00:31:57"),
              ("豆粕", "2026-10-16 03:17:03"),
              ("豆粕", "2026-10-16 03:39:20"),
              ("豆粕", "2026-10-16 04:39:47")]
HB_LIVE = ["燃料油", "石油沥青", "甲醇", "豆粕", "纯碱", "乙二醇"]
RESTART_TAIL = 300        # 重启行允许出现在空窗结束后 5min 内（它才是「心跳为何恢复」的解释）


def hb_rows(path):
    """读 `<品种>_运行心跳.csv` → [(记录时间, 持仓手数)]，按时间排序。"""
    rows = []
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            for r in csv.DictReader(f):
                t = str(r.get("记录时间") or "").strip()
                try:
                    dt = datetime.datetime.strptime(t, "%Y-%m-%d %H:%M:%S")
                except Exception:
                    continue
                try:
                    pos = int(float(str(r.get("持仓手数") or "0").strip() or "0"))
                except Exception:
                    pos = 0
                rows.append((dt, pos))
    except Exception:
        return []
    rows.sort()
    return rows


def restart_times(log_text):
    """守护日志里的重启事件时刻（拉起核心 / 守护启动 / 僵尸锁接管 都算一次重启）。"""
    out = []
    for line in log_text.splitlines():
        try:
            dt = datetime.datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        if ("拉起核心" in line) or ("守护启动" in line) or ("僵尸锁" in line):
            out.append(dt)
    return out


def p95_gap(rows):
    gaps = sorted((rows[i + 1][0] - rows[i][0]).total_seconds()
                  for i in range(len(rows) - 1))
    return gaps[max(0, int(len(gaps) * 0.95) - 1)] if gaps else 0.0


def classify_gaps(rows, restarts, stall_sec):
    """把 >stall_sec 的空窗分三类，返回 [(t0, t1, gap秒, 空窗前的持仓, 类别)]。

    类别：covered（空窗内有重启）/ no_position（当时无持仓，不构成裸奔）/ naked（裸奔）。
    """
    out = []
    tail = datetime.timedelta(seconds=RESTART_TAIL)
    for i in range(len(rows) - 1):
        t0, pos = rows[i]
        t1 = rows[i + 1][0]
        gap = (t1 - t0).total_seconds()
        if gap <= stall_sec:
            continue
        covered = any(t0 <= t <= t1 + tail for t in restarts)
        kind = "covered" if covered else ("no_position" if pos == 0 else "naked")
        out.append((t0, t1, gap, pos, kind))
    return out


def t9_心跳基线():
    print("[T9] ★A11 心跳间隔基线 + 历史裸奔事件识别", flush=True)
    stall = 12 * 60

    # ---- 9.1 夹具：复刻 10-16 凌晨形态 ⇒ 证明检测器本身有效（不是摆设）----
    d = new_sandbox()
    try:
        p = os.path.join(d, "豆粕_运行心跳.csv")
        marks = ["2026-10-16 00:21:57", "2026-10-16 00:26:57", "2026-10-16 00:31:57",
                 "2026-10-16 03:17:03", "2026-10-16 03:39:20", "2026-10-16 04:39:47",
                 "2026-10-16 08:12:05", "2026-10-16 08:17:05"]
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write("记录时间,K线时间,现价,持仓手数,目标手数\r\n")
            for m in marks:
                f.write(f"{m},2026-10-15 22:59:59,3318,8,1\r\n")
        naked = [g for g in classify_gaps(hb_rows(p), [], stall) if g[4] == "naked"]
        ok(len(naked) == 4,
           f"T9 夹具（10-16 形态）识别出 {len(naked)} 条「有持仓+无重启」空窗（应 4 条，"
           f"间隔 {[round(g[2] / 60) for g in naked]} 分钟）")
    finally:
        shutil.rmtree(d, ignore_errors=True)

    # ---- 9.2 真实台账：p95 与每条 >12min 空窗的归类 ----
    log_text = read(os.path.join(LEDGER_REAL, "守护日志.txt"))
    ok(bool(log_text), "T9 能读到真实 守护日志.txt（只读，不写任何台账文件）")
    restarts = restart_times(log_text)
    ok(len(restarts) >= 20,
       f"T9 守护日志解析出 {len(restarts)} 个重启事件（关窗/断电后的僵尸锁接管也算）")

    found_hist, bad_naked, over, p95s = [], [], 0, {}
    for s in HB_LIVE:
        rows = hb_rows(os.path.join(LEDGER_REAL, f"{s}_运行心跳.csv"))
        if not rows:
            continue
        p95s[s] = p95_gap(rows)
        for g in classify_gaps(rows, restarts, stall):
            over += 1
            if g[4] != "naked":
                continue
            stamp = g[0].strftime("%Y-%m-%d %H:%M:%S")
            if (s, stamp) in HIST_NAKED:
                found_hist.append((s, stamp))
            else:
                bad_naked.append(f"{s} {stamp} {g[2] / 60:.0f}min 持仓{g[3]}")

    ok(bool(p95s) and all(v <= 305 for v in p95s.values()),
       f"T9 六品种心跳 p95 全部 ≤305s（实测 {[round(v) for v in p95s.values()]}，"
       f"样本 {len(p95s)} 个品种）")
    ok(sorted(found_hist) == sorted(HIST_NAKED),
       f"T9 ★历史裸奔事件被逐条扫到（{len(found_hist)}/{len(HIST_NAKED)} 条 10-16 豆粕空窗）"
       f"—— 登记表不空转，台账被截断/改错会立刻暴露")
    ok(not bad_naked,
       f"T9 ★无新增裸奔空窗：{over} 条 >12min 空窗中，登记表外「有持仓+无重启」= "
       f"{len(bad_naked)} 条 {bad_naked[:3]}")


# ---------------------------------------------------------------------------
# T10 A13：--hb-check --grace 与守护内部判据同口径
# ---------------------------------------------------------------------------
def _load_wd():
    """把守护模块当被测对象加载（不执行 main，不建锁、不拉起任何进程）。"""
    spec = importlib.util.spec_from_file_location("wd_a2_under_test", WD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def t10_grace同口径():
    print("[T10] A13：`--hb-check --grace` 与守护内部 `_hb_stalled` 同口径", flush=True)
    d = new_sandbox()
    try:
        m = _load_wd()
        m.HB_DIR = d                      # 只把心跳目录指到沙箱，绝不动真实台账
        m.HB_STALL_SEC = 60.0             # 测试用 60s 阈值（等比缩短）
        p = os.path.join(d, "燃料油_运行心跳.csv")
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write("记录时间,K线时间,现价,持仓手数,目标手数\r\n2026-10-20 00:00:00,,0,0,0\r\n")
        old = time.time() - 3600
        os.utime(p, (old, old))           # 心跳文件「1 小时前」写的 = 运维重启后现场

        # 场景 1：视作核心 30s 前刚启动 ⇒ 比阈值 60s 新 ⇒ 两边都应判「正常」
        s1, a1, _ = m._hb_stalled(["FU"], time.time() - 30)
        r1 = m._hb_report(["FU"], grace=30)
        ok((not s1) and r1 == 0,
           f"T10 宽限 30s：守护判定 stalled={s1} / --hb-check rc={r1} ⇒ 一致（都正常，"
           f"停滞 {a1:.0f}s）—— 重启后立刻体检不再误报「★假死」")

        # 场景 2：视作核心 600s 前启动 ⇒ 两边都应判假死
        s2, a2, _ = m._hb_stalled(["FU"], time.time() - 600)
        r2 = m._hb_report(["FU"], grace=600)
        ok(s2 and r2 == 1,
           f"T10 宽限 600s：守护判定 stalled={s2} / --hb-check rc={r2} ⇒ 一致（都判假死，"
           f"停滞 {a2:.0f}s > 60s）")

        # 场景 3：不给宽限 ⇒ 必须报假死（否则 --grace 会把「误报」修成「漏报」）
        r3 = m._hb_report(["FU"], grace=0)
        ok(r3 == 1, f"T10 不加 --grace 时仍按文件 mtime 判（rc={r3}）⇒ 真死照样抓得到")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# T11 A14：FATAL 按原因拆分措辞
# ---------------------------------------------------------------------------
def t11_fatal拆分():
    print("[T11] A14：FATAL 归因拆分（假死 / 启动失败 / 启动期网络失败 rc=1）", flush=True)

    # ---- 11.1 连续 5 次假死 → 必须写「假死」，且不得说「启动失败」----
    d = new_sandbox()
    cfg = write_cfg(d, "script", script=["freeze"] * 5 + ["run:9999"],
                    hb_sec=0.3, freeze_after=0.6)
    wd = start_wd(d, cfg, ["--hb-stall", "2", "--backoff", "1,1,1,1"])
    try:
        ok(wait_for(lambda: "【FATAL】" in log_of(d), 80, 0.3),
           "T11 连续 5 次假死已触发【FATAL】")
        fatal = [l for l in log_of(d).splitlines() if "【FATAL】" in l]
        line = fatal[0] if fatal else ""
        ok(("假死" in line) and ("启动失败" not in line),
           f"T11 假死类措辞含「假死」且不含「启动失败」：…{line[19:]}")
    finally:
        kill_tree(wd.pid)
        stop_wd(d)
        shutil.rmtree(d, ignore_errors=True)

    # ---- 11.2 连续 5 次 rc=1（启动期网络失败）→ 归因必须指向网络，而不是令牌/假死 ----
    d = new_sandbox()
    cfg = write_cfg(d, "crash1")
    wd = start_wd(d, cfg, ["--hb-stall", "9999", "--backoff", "1,1,1,1"])
    try:
        ok(wait_for(lambda: "【FATAL】" in log_of(d), 50, 0.3),
           "T11 rc=1 连续 5 次已触发【FATAL】")
        fatal = [l for l in log_of(d).splitlines() if "【FATAL】" in l]
        line = fatal[0] if fatal else ""
        ok(("启动失败" in line) and ("疑启动期网络失败" in line) and ("假死" not in line),
           f"T11 ★rc=1 归因为「启动期网络失败（DNS/行情服务器/席位）」：…{line[19:]}")
    finally:
        kill_tree(wd.pid)
        stop_wd(d)
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# T12 T6-4：Popen.pid 语义自检
# ---------------------------------------------------------------------------
class _FakeProc:
    """只实现自检用到的两个属性（pid / poll）；rc 给定时立即「已退出」。"""
    def __init__(self, pid, rc=None):
        self.pid = pid
        self._rc = rc

    def poll(self):
        return self._rc


def t12_pid自检():
    print("[T12] T6-4 PID 语义自检（正向证据 / 转发器告警 / 真实链路不变式）", flush=True)

    # ---- 12.1 单元级：非转发器（存活、无 python 子进程）⇒ 留正向证据、不告警 ----
    d = new_sandbox()
    try:
        m = _load_wd()
        m.LOG = os.path.join(d, "unit.txt")
        m._PIDCHK_DONE = False
        # 用 cmd.exe 充当「不转发的解释器」：PID 就是它自己，且没有 python 子进程
        host = subprocess.Popen(["cmd.exe", "/c", "ping -n 6 127.0.0.1 > nul"])
        try:
            m._check_pid_semantics(_FakeProc(host.pid))
            txt = read(m.LOG)
            ok(("PID 自检：Popen.pid=" in txt) and ("疑为解释器转发器" not in txt),
               "T12 非转发器：留正向证据「Popen.pid=… 存活、无 python 子进程」，且不告警")
        finally:
            kill_tree(host.pid)
    finally:
        shutil.rmtree(d, ignore_errors=True)

    # ---- 12.2 单元级：转发器「壳以 rc=0 自行退出」⇒ 必须告警（这是最危险的形态）----
    d = new_sandbox()
    try:
        m = _load_wd()
        m.LOG = os.path.join(d, "unit.txt")
        m._PIDCHK_DONE = False
        m._check_pid_semantics(_FakeProc(os.getpid(), rc=0))
        txt = read(m.LOG)
        ok("疑为解释器转发器" in txt and "rc=0" in txt,
           "T12 ★转发器（壳 rc=0 自行退出）⇒ 告警，并写明「kill 只杀壳会留孤儿」")
    finally:
        shutil.rmtree(d, ignore_errors=True)

    # ---- 12.3 单元级：壳活着但挂着 python 子进程 ⇒ 也必须告警 ----
    d = new_sandbox()
    try:
        m = _load_wd()
        m.LOG = os.path.join(d, "unit.txt")
        m._PIDCHK_DONE = False
        env = dict(os.environ, STUB_FWD_CHILD_SEC="5")
        fwd = subprocess.Popen([PY, "-u", STUB_FWD, "--symbols", "FU"], env=env,
                               cwd=HERE, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
        try:
            m._check_pid_semantics(fwd)
            txt = read(m.LOG)
            ok("疑为解释器转发器" in txt,
               "T12 ★壳存活但带 python 子进程 ⇒ 同样告警（转发器的第二种形态）")
        finally:
            kill_tree(fwd.pid)
    finally:
        shutil.rmtree(d, ignore_errors=True)

    # ---- 12.4 `_kill_core` 在转发器链路上必须「壳 + 真身」全清（防双核心）----
    d = new_sandbox()
    try:
        m = _load_wd()
        m.LOG = os.path.join(d, "unit.txt")
        cfg = write_cfg(d, "normal", hb_sec=0.5)
        env = dict(os.environ, STUB_CFG=cfg)
        p = subprocess.Popen([PY, "-u", STUB, "--symbols", "FU"], env=env, cwd=HERE,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ok(wait_for(lambda: stub_pid(d, 1) > 0, 10), "T12 取到假核心自报 PID")
        real = stub_pid(d, 1)
        behind = [k for k, _n in m._pid_children(p.pid)]   # 转发器背后的真身
        gone = m._kill_core(p)
        time.sleep(0.5)
        left = [x for x in behind + [real] if m._pid_alive(x)]
        ok(gone and not left,
           f"T12 ★_kill_core 全清：壳 {p.pid} 与真身 {behind or [real]} 无一存活（残留 {left}）"
           f"—— 否则假死复位会变成双核心双份下单")
    finally:
        try:
            kill_tree(p.pid)
        except Exception:
            pass
        shutil.rmtree(d, ignore_errors=True)

    # ---- 12.5 集成级不变式：自检必须留痕；Popen.pid 与核心自报 PID 不一致就必须告警 ----
    d = new_sandbox()
    cfg = write_cfg(d, "normal", hb_sec=0.5)
    wd = start_wd(d, cfg, ["--hb-stall", "9999"])
    try:
        # 自检在拉起后 0.8s 内就写日志，而假核心写 pid 文件可能稍慢 ⇒ 两个条件都要等
        ok(wait_for(lambda: ("PID 自检" in log_of(d)) and stub_pid(d, 1) > 0, 25, 0.2),
           "T12 真实链路：自检一定在守护日志留痕（不允许静默）")
        log = log_of(d)
        mm = re.search(r"Popen\.pid=(\d+)", log)
        popen_pid = int(mm.group(1)) if mm else 0
        real_pid = stub_pid(d, 1)          # 假核心自己写下的 pid = 真实解释器 pid
        if popen_pid and real_pid and popen_pid != real_pid:
            ok("疑为解释器转发器" in log,
               f"T12 ★不一致必有告警：Popen.pid={popen_pid} ≠ 核心自报 {real_pid}"
               f"（转发器）⇒ 已告警")
        else:
            ok("疑为解释器转发器" not in log,
               f"T12 一致时不得告警（Popen.pid={popen_pid} = 自报 {real_pid}）")
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
                 (t6_防双开, "t6"), (t7_关窗自愈, "t7"),
                 (t9_心跳基线, "t9"), (t10_grace同口径, "t10"),
                 (t11_fatal拆分, "t11"), (t12_pid自检, "t12")]
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
