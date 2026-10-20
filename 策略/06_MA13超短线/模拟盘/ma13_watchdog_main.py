# -*- coding: utf-8 -*-
"""
MA13 模拟盘 · 进程级守护（第二层保险）
==========================================================
第一层保险在 ma13_live_multi.py 内部：断线/被踢 -> 记台账 -> 自动重连。
本脚本是第二层：万一核心整个崩掉（未捕获异常/崩溃），30 秒后重新拉起，
并把每次重启记入 台账/守护日志.txt。

用法（ bat 已接入，一般不手动跑）：
  python ma13_watchdog_main.py --symbols auto --trade          # 前台（调试用）
  python ma13_watchdog_main.py --symbols auto --trade --detach # ★正式用法：脱离控制台
  python ma13_watchdog_main.py --hb-check                      # 一次性体检：各品种心跳多久没更新
  python ma13_watchdog_main.py --sandbox <目录> ...            # 仅测试用：日志/锁/心跳全部重定向

★★ 2026-10-20 A2 改造（事故驱动，见交接任务书 A2）：
   1) **独立存活**：--detach 用 pythonw + DETACHED_PROCESS 重新拉起自己，
      启动它的那个 cmd 窗口随即退出。⇒ **关掉任何窗口都不会带走守护**。
      核心用 CREATE_NEW_CONSOLE 单独开窗，关掉核心窗口只会让它重启，不会停止程序。
   2) **假死检测**：核心卡死不退出（旧版完全发现不了）时，靠心跳文件 mtime 判死。
      HB_STALL_SEC = 12 分钟（实测正常最大间隔 6.7min，见交接交付说明的标定证据）。
   3) **退避熔断**：30→60→120→300s，连续 5 次失败写【FATAL】，稳定运行 10 分钟计数归零。

停止（★ 重要，因为守护已脱离窗口，关窗口不会停）：
   python ma13_watchdog_main.py --stop    或   见 台账/守护.lock 里的 PID 后
   taskkill /PID <守护PID> /T /F          （/T 会连核心一起收，一步到位）
日志位置：策略/06_MA13超短线/模拟盘/台账/守护日志.txt
          （独立运行时控制台输出另存 台账/守护控制台.log）

防双开（2026-10-07 加）：台账/守护.lock 记录本机 PIDPID。
     每日 08:55 自动启动场景下，锁是必需的——否则重复触发会拉起两个核心、
     双份下单 + 白占一个天勤席位。锁里的 PID 已不存在（崩溃/强杀）会自动接管。
"""
import sys, os, time, subprocess, argparse, io, json, glob
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.join(_HERE, "ma13_live_multi.py")
LEDGER = os.path.join(_HERE, "台账")
LOG = os.path.join(LEDGER, "守护日志.txt")
LOCK = os.path.join(LEDGER, "守护.lock")
HB_DIR = LEDGER                       # 心跳文件所在目录（--sandbox 可重定向）
RESTART_WAIT = 30      # 旧常量：保留为退避序列的第 1 档
DEFAULT_LEGS = "JM,M,JD,P,SR"   # 兜底（2026-10-08 相关性选出）；auto 读不到时用

# ---------------------------------------------------------------------------
# ★ 2026-10-20 A2 新增常量（全部有实测依据，不是拍脑袋）
# ---------------------------------------------------------------------------
# 退避序列（D3）：连续失败第 1/2/3/4+ 次分别等这么久
BACKOFF = (30, 60, 120, 300)
FATAL_AFTER = 5               # 连续失败达此次数 → 写【FATAL】
HEALTHY_RESET_SEC = 10 * 60   # 成功运行超过这么久 → 失败计数归零

# 假死判定（D2）。标定依据（2026-10-20 实测 6 个在跑品种共 3,700+ 条心跳）：
#   · 正常节奏 p50/p95 = 300s，**合法最大间隔 6.7min**（含 6.5/6.6/6.7 三次边缘）；
#   · 所有 >6.7min 的间隙，逐条核对后**全部对应「进程不在」**（例：今晚事故
#     10-20 20:13:08 → 20:35:25 的 22.3min，正是核心被杀到重启的空窗）；
#   · 心跳线程**与行情无关**（`_hb_loop` 独立线程，只读 shared 标量），
#     断网/断线/休市都照写 —— 实测 10-19 21:00~10-20 09:00 每小时稳定 12 行。
#   ⇒ 12 分钟 = 2 个周期 + 余量（1.8× 实测最大值），既能抓住真死，又不会被
#     休市/深夜/周末误杀。**没有「合法但长时间不写心跳」的时段**，故未加交易时段判断。
HB_STALL_SEC = 12 * 60
POLL_SEC = 60                 # 看门狗巡视频率（心跳 5min 一行，1min 够；检测延迟 ≤13min）

# Windows 进程创建标志
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NEW_CONSOLE = 0x00000010

WORKBENCH = os.path.join(os.path.dirname(_HERE), "工作台", "workbench.json")
WB_DIR = os.path.dirname(WORKBENCH)
RESELECT_DAYS = 6.0    # 距上次重选超过这个天数 → 允许重选（用户 2026-10-18 拍板：日历天 6 天）
# ★ 为什么是 6 不是 7：任务只在周一~周五 08:55 跑。周一早上距上周五收盘后的刷新
#   恰好 ≈7.0 天 —— 阈值 7 会被"差一点"跳过，拖到周二才重选；6 保证周一开盘前必刷，
#   选品种永远用的是"刚过去的那个周五收盘"的数据。
#
# ★★ 2026-10-18 追加：**重选只落在周一**（RESELECT_WEEKDAY=0）。
#   原因：若纯按「≥6 天就重选」漂移，重选日会一路往后挪
#   （10-18 周日 → 10-24 周六（休市不跑，实际顺延）→ 10-26 周一 → 11-01 周日…），
#   落在周中时最危险：**手上有持仓却在中途换池**，等于交易做一半换标的。
#   对齐周一 = 每次都在开盘前、用上一个完整交易周的数据换池，是唯一不发散的选择。
#   ⚠️ 但「到期待周一」不能无限等：若一直等不到（节假日连休），日志要说明白。
RESELECT_WEEKDAY = 0   # 0=周一（datetime.weekday() 口径）

def _console_python():
    """拉起核心必须用 **控制台版** python.exe。

    ★ 2026-10-20 A2 踩到的坑：--detach 用 pythonw.exe 重启自己，此时
      sys.executable 指向 pythonw.exe。若照旧 `PY = sys.executable` 去拉核心，
      核心就会在「无控制台」下跑 —— 它满屏的 print 全部丢弃，运维看不见任何东西。
      ⇒ pythonw 启动时显式换回同目录的 python.exe。
    """
    exe = sys.executable or ""
    if os.path.basename(exe).lower().startswith("pythonw"):
        cand = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.isfile(cand):
            return cand
    return exe


PY = _console_python()    # 用同一个解释器（控制台版）拉起核心


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



def _ensure_streams():
    """独立运行（pythonw / DETACHED）时 sys.stdout 为 None，把 print 引到文件。

    否则 `print(..., flush=True)` 在 stdout 被关掉的环境里可能抛异常或静默丢弃，
    而守护进程**绝不能因为打日志失败就停下**。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        f = io.open(os.path.join(os.path.dirname(LOG), "守护控制台.log"),
                    "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = f
        if sys.stderr is None:
            sys.stderr = f
    except Exception:
        pass


def _log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    try:
        print(f"[守护进程] {msg}", flush=True)
    except Exception:
        pass                      # stdout 被关（无控制台）→ 只写文件，不影响守护
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with io.open(LOG, "a", encoding="utf-8", newline="") as f:
            f.write(line + "\r\n")
    except Exception:
        pass                      # 日志失败绝不能影响守护本身


def _cache_age_days():
    """工作台日线缓存距上次更新多少天；无缓存返回 None。取**最旧**那份（一批一起刷的）。"""
    files = glob.glob(os.path.join(WB_DIR, "data", "daily_*.csv"))
    if not files:
        return None
    oldest = min(os.path.getmtime(f) for f in files)
    return (time.time() - oldest) / 86400.0


# ---------------------------------------------------------------------------
# ★★ 重选节奏的单一真相源（2026-10-18 修，P0）
#
# 原实现只看「日线缓存 mtime 有多老」，有两个硬缺陷：
#   ① **mtime ≠ 上次重选时间**：`data_feed.py --repair` / 任何手工刷新都会把
#      mtime 改成今天 ⇒ 计时器被静默重置（今天真发生了：repair 重写 46 个文件）。
#   ② 与 `run_workbench` 的 `data/selection_state.json` 是两套互不相通的状态，
#      同一台机器上两条启动路径会对「该不该重选」给出不同答案。
# ⇒ 统一为：**优先读 selection_state.json 的 last_reselect**（与 run_workbench 共用），
#   只有该文件缺失/损坏时才退回到 mtime 估算。
# ---------------------------------------------------------------------------
SEL_STATE = os.path.join(WB_DIR, "data", "selection_state.json")


def _last_reselect_date():
    """读 selection_state.json 的 last_reselect（'YYYY-MM-DD' → date）；不可用返回 None。"""
    try:
        with open(SEL_STATE, "r", encoding="utf-8") as f:
            st = json.load(f) or {}
        d = str(st.get("last_reselect") or "")[:10]
        if d:
            return datetime.strptime(d, "%Y-%m-%d").date()
    except Exception:
        return None
    return None


def _save_reselect_state(selected, stage1, today) -> None:
    """重选后落盘，供 run_workbench 与下次启动共用同一个计时起点。"""
    try:
        os.makedirs(os.path.dirname(SEL_STATE), exist_ok=True)
        with open(SEL_STATE, "w", encoding="utf-8") as f:
            json.dump({"last_reselect": str(today)[:10],
                       "selected": [str(k).upper() for k in selected],
                       "stage1": [str(k).upper() for k in (stage1 or [])]},
                      f, ensure_ascii=False, indent=2)
    except Exception as e:
        _log(f"[选品种] ⚠️ 重选状态写盘失败（下次可能重复重选）：{e!r}")


def _select_by_method(daily: dict, cfg: dict):
    """按 `selection.method` 分派选品种算法，返回 (selected, stage1)。

    ★★ 2026-10-18 新增（很关键，否则切换无法持久）：
      原来这里硬编码 `select_two_stage`（**手数**口径 + 3日涨跌幅、无相关性约束）。
      四期拍板① 要的是 **成交额降序 + |ρ|≤0.5 贪心加腿**（`工具/复盘/exec4/pool.py`）。
      若不同步这里，手工把 workbench.json 改成 S2 池后，
      **下个周一的重选又会用手数口径把池子覆盖回去** —— 切换会"自己失效"。

    可选值：
      · "two_stage"（默认，保持原行为）：工作台 selection.select_two_stage
      · "pool_s2"：exed4/pool.select_pool（成交额 + ρ 封顶 + 粒度闸门）
    """
    from selection import select_two_stage, filter_tradable
    sc = cfg.get("selection", {}) or {}
    method = str(sc.get("method", "two_stage")).lower()

    # ★ 2026-10-19：人工剔除名单（selection.exclude），两条分支统一生效。
    #   用户主观剔除焦煤 JM；不落这一步的话，下周一自动重选会把它选回来。
    _ex = {str(x).strip().upper() for x in (sc.get("exclude") or []) if str(x).strip()}
    if _ex:
        _n0 = len(daily)
        daily = {k: v for k, v in daily.items() if str(k).upper() not in _ex}
        if len(daily) != _n0:
            _log(f"[选品种] 人工剔除 {_n0 - len(daily)} 个：{','.join(sorted(_ex))}（不参与排序）")

    if method in ("pool_s2", "s2", "pool"):
        # 走 S2：需要 exec4/pool.py，并自带"缺规格/粒度太粗"两道闸门
        # ★ 向上找工程根（含 config_symbols.py），不写死层级 —— 写死过就错（少两级）
        _r = _HERE
        while _r != os.path.dirname(_r) and not os.path.isfile(
                os.path.join(_r, "config_symbols.py")):
            _r = os.path.dirname(_r)
        ex = os.path.join(_r, "工具", "复盘", "exec4")
        if ex not in sys.path:
            sys.path.insert(0, ex)
        import pool as _pool
        import config_symbols as _cs
        specs = {}
        for k in daily:
            if k.upper() in _cs.SYMBOLS:
                _n, _c, _m, _t = _cs.SYMBOLS[k.upper()]
                specs[k] = {"mult": float(_m)}
        r = _pool.select_pool(
            daily, specs,
            n_legs=int(sc.get("ret_top_n", 5) or 5),
            vol_window=int(sc.get("vol_window_days", 20)),
            corr_window=int(sc.get("corr_window_days", 20)),
            notional_cap=float(sc.get("notional_cap", _pool.DEFAULT_NOTIONAL_CAP)),
        )
        _log(f"[选品种] S2 口径：成交额前20 中 |ρ|≤{_pool.MAX_RHO} 贪心加腿；"
             f"最大|ρ|={r['max_rho_used']:.3f}" if r["max_rho_used"] is not None
             else "[选品种] S2 口径：完成")
        if r["dropped_toolarge"]:
            _log("[选品种] S2 粒度太粗排除：" + "；".join(
                f"{k}(1手{ml:,.0f})" for k, ml, _ra in r["dropped_toolarge"]))
        return r["legs"], list(r["ranked"])[:20]

    # 默认：原 two_stage（手数口径）
    daily, _skipped = filter_tradable(daily)
    sel, stage1 = select_two_stage(
        daily,
        vol_window=int(sc.get("vol_window_days", 20)),
        vol_top_m=int(sc.get("vol_top_m", 20)),
        ret_window=int(sc.get("ret_window_days", 3)),
        ret_top_n=int(sc.get("ret_top_n", 6)),
    )
    return sel, stage1


def _maybe_refresh_selection(cfg: dict) -> None:
    """auto 模式的周期自动重选。

    触发（按优先级）：
      1. 环境变量 MA13_FORCE_RESELECT=1 ⇒ 强制；
      2. selection_state.json 有 last_reselect ⇒ 距今天数 ≥ RESELECT_DAYS 才重选；
      3. 无状态文件 ⇒ 退回「日线缓存 mtime 年龄 ≥ RESELECT_DAYS」估算。
    行为：真拉全池日线 → 卡C 两阶段重选 → 写回 workbench.json + selection_state.json。
    任何失败**只记日志、沿用现有 selected** —— 启动主流程绝不被重选卡死。
    """
    forced = os.environ.get("MA13_FORCE_RESELECT", "").strip() == "1"
    last = _last_reselect_date()
    today = datetime.now().date()

    if not forced:
        if last is not None:
            gap = (today - last).days
            if gap < RESELECT_DAYS:
                _log(f"[选品种] 距上次重选（{last}）{gap} 天 < {RESELECT_DAYS:.0f} 天 → 不重选")
                return
            # ★ 到期待周一：避免周中换池（手上有持仓却中途换标的）
            if today.weekday() != RESELECT_WEEKDAY:
                _wd = "一二三四五六日"[today.weekday()]
                _tgt = "一二三四五六日"[RESELECT_WEEKDAY]
                _log(f"[选品种] 距上次重选 {gap} 天（已到期待），但今天周{_wd} "
                     f"≠ 周{_tgt} → 等到周{_tgt}开盘前再换池（避免周中换池）")
                return
            why = f"距上次重选 {gap} 天（≥{RESELECT_DAYS:.0f}）且今天是周一"
        else:
            age = _cache_age_days()
            if age is not None and age < RESELECT_DAYS:
                _log(f"[选品种] 无重选记录；日线缓存 {age:.1f} 天前更新（<{RESELECT_DAYS:.0f}天）→ 不重选")
                return
            why = (f"无重选记录，缓存已 {age if age is not None else '无'} 天未更新"
                   if age is not None else "无重选记录且无缓存")
    else:
        why = "强制"

    _log(f"[选品种] 自动重选开始（{why}）→ 拉全池日线约 1 分钟…")
    try:
        if WB_DIR not in sys.path:
            sys.path.insert(0, WB_DIR)
        import data_feed as _df
        import universe as _u
        sc = cfg.get("selection", {}) or {}
        # ★ use_cache=False：缓存已判旧才走到这里，绝不能再用旧缓存（旧版因此永远不真拉）
        daily = _df.build_universe_daily(_u.all_keys(), use_cache=False, fetch=True,
                                         days=int(sc.get("vol_window_days", 20)) + 10)
        if not daily:
            _log("[选品种] 重选失败：拉不到日线 → 沿用现有 selected")
            return
        # ★ 按 method 分派（two_stage=手数口径 / pool_s2=成交额+ρ封顶）
        sel, stage1 = _select_by_method(daily, cfg)
        skipped = []          # 排除明细已在 _select_by_method 内记录
        if not sel:
            _log("[选品种] 重选结果为空 → 沿用现有 selected")
            return
        # ★ 无论结果是否变化都要落盘计时起点：否则「结果一致」分支会让
        #   last_reselect 永远不动 ⇒ 每次启动都重新拉一遍全池日线。
        _save_reselect_state(sel, stage1, today)
        old = [str(k) for k in (cfg.get("selected") or [])]
        if [str(k).upper() for k in sel] == [str(k).upper() for k in old]:
            _log(f"[选品种] 重选完成，结果与现有一致：{','.join(sel)}"
                 f"（重选日期已记为 {today}）")
            return
        cfg["selected"] = [str(k).upper() for k in sel]
        with open(WORKBENCH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        _log(f"[选品种] ★ 自动重选生效：{','.join(cfg['selected'])}"
             f"（旧：{','.join(old) or '无'}）；排除 {len(skipped)} 个无规格品种")
    except Exception as e:
        _log(f"[选品种] 自动重选失败：{type(e).__name__}: {e} → 沿用现有 selected")


def _resolve_symbols(raw: str) -> str:
    """把 --symbols 解析成逗号分隔的品种串。

    ★ 2026-10-18 新增 auto：`--symbols auto` = 读
      `工作台/workbench.json` 的 `selected`（卡C 两阶段选品种的落地结果）。
      为什么放在守护层：核心 ma13_live_multi 只认显式列表；run_workbench 虽然
      能注入 ctx，但它绕过了核心单实例锁（双开=双份下单），不能作为正式启动路径。
      ⇒ 选品种在启动前解析成显式列表，再交给核心，零侵入。
    校验：每个品种都必须在 config_symbols 里有核验规格，否则**拒绝启动**（不兜底）。
    """
    raw = (raw or "auto").strip()
    if raw.lower() != "auto":
        return raw
    path = WORKBENCH
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        _log(f"[选品种] 读 {path} 失败：{type(e).__name__}: {e} → 回退 {DEFAULT_LEGS}")
        return DEFAULT_LEGS
    _maybe_refresh_selection(cfg)          # ★ 周频自动重选（失败沿用现有 selected）
    keys = [str(k).strip().upper() for k in (cfg.get("selected") or []) if str(k).strip()]
    if not keys:
        _log(f"[选品种] workbench.json selected 为空 → 回退 {DEFAULT_LEGS}")
        return DEFAULT_LEGS

    root = _HERE
    while root != os.path.dirname(root) and not os.path.isdir(os.path.join(root, "lib")):
        root = os.path.dirname(root)
    if root not in sys.path:
        sys.path.insert(0, root)
    import config_symbols                       # 项目内硬依赖，导入失败就该报错
    bad = [k for k in keys if k not in config_symbols.SYMBOLS]
    if bad:
        _log(f"[选品种] ⚠️ 缺合约规格，拒绝启动：{','.join(bad)}")
        raise SystemExit(f"缺合约规格：{bad}（先在 config_symbols 补乘数/tick/成本）")
    _log(f"[选品种] auto → {','.join(keys)}")
    return ",".join(keys)


# ===========================================================================
# ★ 2026-10-20 A2 新增：心跳假死检测（D2）
# ===========================================================================
def _project_root():
    """向上找含 config_symbols.py 的工程根（不写死层级——写死过就错）。"""
    r = _HERE
    while r != os.path.dirname(r) and not os.path.isfile(
            os.path.join(r, "config_symbols.py")):
        r = os.path.dirname(r)
    return r


def _symbols_readonly():
    """只读 workbench.json 的 selected，**不触发周频重选**（供 --hb-check 用）。"""
    try:
        with open(WORKBENCH, "r", encoding="utf-8") as f:
            cfg = json.load(f) or {}
        keys = [str(k).strip().upper() for k in (cfg.get("selected") or []) if str(k).strip()]
        if keys:
            return keys
    except Exception:
        pass
    return [k.strip().upper() for k in DEFAULT_LEGS.split(",") if k.strip()]


def _hb_files(keys):
    """key → 心跳文件路径。文件名用 config_symbols 里的**中文名**拼。

    只认「当前在跑的品种」——台账里还有 棕榈油/焦煤/白糖/鸡蛋 等历史心跳文件，
    它们早就停了，若一起纳入检查会天天误判假死。
    """
    root = _project_root()
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        import config_symbols as _cs
    except Exception as e:
        _log(f"[假死检测] ⚠️ 读不到 config_symbols（{type(e).__name__}: {e}）→ 本轮跳过心跳检查")
        return []
    out, unknown = [], []
    for k in keys:
        info = _cs.SYMBOLS.get(str(k).strip().upper())
        if not info:
            unknown.append(str(k))
            continue
        out.append((str(k).upper(), str(info[0]), os.path.join(HB_DIR, f"{info[0]}_运行心跳.csv")))
    if unknown:
        _log(f"[假死检测] ⚠️ 无中文名映射、无法监控心跳：{','.join(unknown)}（请在 config_symbols 补）")
    return out


def _hb_state(keys, core_started):
    """返回 [(key, 中文名, 路径, mtime|None, 停滞秒数)]。

    ★ 停滞起点取 `max(mtime, core_started)`：核心刚重启时旧心跳文件是几十小时前的，
      若直接比 mtime，会把「刚起来 10 秒的新核心」当场判成假死并杀掉 → 无限重启风暴。
      以启动时刻为中轴，等于给每次重启一个 HB_STALL_SEC 的宽限期。
    """
    now = time.time()
    rows = []
    for key, name, path in _hb_files(keys):
        try:
            mt = os.path.getmtime(path)
        except OSError:
            mt = None
        base = max(mt, core_started) if mt else core_started
        rows.append((key, name, path, mt, now - base))
    return rows


def _hb_stalled(keys, core_started):
    """(是否假死, 最差停滞秒数, 最差文件路径)。"""
    rows = _hb_state(keys, core_started)
    if not rows:
        return False, 0.0, ""
    worst = max(rows, key=lambda r: r[4])
    return (worst[4] > HB_STALL_SEC), worst[4], worst[2]


def _hb_report(keys):
    """--hb-check：一次性打印各品种心跳新鲜度（运维排障 / 应急预案第 1 步靠它）。"""
    rows = _hb_state(keys, 0.0)
    print(f"心跳目录：{HB_DIR}")
    print(f"{'品种':<10}{'中文名':<10}{'最后写入':<21}{'停滞(分)':>10}  判定")
    bad = 0
    for key, name, path, mt, age in rows:
        ts = datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M:%S") if mt else "文件不存在"
        verdict = "★假死" if age > HB_STALL_SEC else "正常"
        if age > HB_STALL_SEC:
            bad += 1
        print(f"{key:<10}{name:<10}{ts:<21}{age/60:>10.1f}  {verdict}")
    print(f"阈值 {HB_STALL_SEC/60:.0f} 分钟；异常 {bad} 个 / 共 {len(rows)} 个")
    return 1 if bad else 0


# ===========================================================================
# ★ 2026-10-20 A2 新增：进程控制（D1 独立存活 / D3 退避熔断）
# ===========================================================================
def _spawn_core(cmd, new_console=True):
    """拉起核心。

    ★ CREATE_NEW_CONSOLE：核心独占一个控制台窗口。
      这样「关掉核心窗口」只会杀掉核心本身，不会顺着共享控制台把守护一起带走
      —— 这正是今晚 20:16–20:35 那 19 分钟空窗的根因（守护与核心同窗口）。
    """
    flags = CREATE_NEW_CONSOLE if new_console else 0
    return subprocess.Popen(cmd, cwd=_HERE, creationflags=flags)


def _kill_core(proc):
    """终止核心（假死时用）。先 kill，再兜底 taskkill /T。"""
    pid = getattr(proc, "pid", None)
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=15)
        return True
    except Exception:
        pass
    if pid:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=30)
        except Exception:
            pass
    try:
        proc.wait(timeout=10)
        return True
    except Exception:
        return False


def _spawn_detached(extra_argv):
    """用 pythonw + DETACHED_PROCESS 重新拉起自己，返回 (Popen, exe)。

    DETACHED_PROCESS = 子进程**不继承任何控制台**。副作用有二，都是我们要的：
      · 关掉启动它的 cmd 窗口 → 守护毫发无伤（今晚事故的直接修复）；
      · 没有任何窗口能「误关」它 —— 停止必须显式 `--stop` 或 taskkill，已写进应急预案。
    CREATE_NEW_PROCESS_GROUP 让它可以独立接收信号，不被父进程的 Ctrl+C 波及。
    """
    exe = sys.executable or ""
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.isfile(pyw):
        exe = pyw
    cmd = [exe, "-u", os.path.abspath(__file__)] + list(extra_argv)
    p = subprocess.Popen(cmd, cwd=_HERE,
                         creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         close_fds=True)
    return p, exe


def _stop_existing():
    """--stop：停掉正在跑的守护（/T 连核心一起收），并清锁。"""
    pid = 0
    try:
        with io.open(LOCK, "r", encoding="utf-8") as f:
            pid = int((f.readline() or "0").strip() or 0)
    except Exception:
        pid = 0
    if pid and _pid_alive(pid):
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=30)
        except Exception as e:
            print(f"taskkill 失败：{type(e).__name__}: {e}")
            return 1
        time.sleep(1)
        _log(f"人工 --stop：已终止守护 PID {pid}（/T 含核心进程树）")
        print(f"已停止守护 PID {pid}（含核心）")
    else:
        print(f"没有在跑的守护（守护.lock PID={pid or '无'}）")
    try:
        if os.path.exists(LOCK):
            os.remove(LOCK)
    except Exception:
        pass
    return 0


def main():
    ap = argparse.ArgumentParser()
    # ★ 2026-10-18：默认改 auto —— 品种列表的唯一真相源 = workbench.json selected。
    #   显式传 --symbols JM,M,... 仍然有效（手工覆盖/排障用）。
    ap.add_argument("--symbols", default="auto",
                    help="品种key逗号分隔，或 auto=读工作台 workbench.json 的 selected")
    ap.add_argument("--trade", action="store_true")
    ap.add_argument("--detach", action="store_true",
                    help="脱离控制台运行（正式用法）：关窗口不影响守护")
    ap.add_argument("--stop", action="store_true", help="停止正在跑的守护（含核心）")
    ap.add_argument("--hb-check", action="store_true", help="一次性心跳体检后退出")
    # 以下为内部/测试用，不写进 help 以免误用
    ap.add_argument("--detached", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--sandbox", default="", help=argparse.SUPPRESS)
    ap.add_argument("--core", default="", help=argparse.SUPPRESS)
    ap.add_argument("--no-window", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--poll", type=float, default=0.0, help=argparse.SUPPRESS)
    ap.add_argument("--backoff", default="", help=argparse.SUPPRESS)
    ap.add_argument("--hb-stall", type=float, default=0.0, help=argparse.SUPPRESS)
    ap.add_argument("--healthy-reset", type=float, default=0.0, help=argparse.SUPPRESS)
    args = ap.parse_args()

    global LOG, LOCK, HB_DIR, CORE, POLL_SEC, BACKOFF, HB_STALL_SEC, HEALTHY_RESET_SEC
    if args.sandbox:                      # 测试：日志/锁/心跳全部落到沙箱目录
        d = os.path.abspath(args.sandbox)
        os.makedirs(d, exist_ok=True)
        LOG, LOCK, HB_DIR = (os.path.join(d, "守护日志.txt"),
                             os.path.join(d, "守护.lock"), d)
    if args.core:
        CORE = os.path.abspath(args.core)
    if args.poll > 0:
        POLL_SEC = float(args.poll)
    if args.backoff:
        BACKOFF = tuple(float(x) for x in args.backoff.split(",") if x.strip())
    if args.hb_stall > 0:
        HB_STALL_SEC = float(args.hb_stall)
    if args.healthy_reset > 0:
        HEALTHY_RESET_SEC = float(args.healthy_reset)

    _ensure_streams()

    if args.stop:
        return _stop_existing()

    if args.detach:
        extra = ["--symbols", args.symbols, "--detached"]
        if args.trade:
            extra.append("--trade")
        if args.sandbox:
            extra += ["--sandbox", args.sandbox]
        if args.core:
            extra += ["--core", args.core]
        if args.no_window:
            extra.append("--no-window")
        if args.poll > 0:
            extra += ["--poll", str(args.poll)]
        if args.backoff:
            extra += ["--backoff", args.backoff]
        if args.hb_stall > 0:
            extra += ["--hb-stall", str(args.hb_stall)]
        if args.healthy_reset > 0:
            extra += ["--healthy-reset", str(args.healthy_reset)]
        p, exe = _spawn_detached(extra)
        # ★ venv 的 pythonw.exe 是转发器：Popen 拿到的 PID 是转发器，真正的守护是它拉起的子进程。
        #   ⇒ 不要报 Popen 的 pid（会误导排障），改为等锁落盘后报真实 PID。
        real = 0
        for _ in range(60):
            try:
                with io.open(LOCK, "r", encoding="utf-8") as f:
                    real = int((f.readline() or "0").strip() or 0)
                if real:
                    break
            except Exception:
                pass
            time.sleep(0.1)
        print(f"[已脱离控制台] 守护作为独立进程启动（{os.path.basename(exe)}），真实 PID={real or '启动中'}")
        print("  本窗口可以直接关闭，守护不受影响。")
        print(f"  守护日志：{LOG}")
        print(f"  真正停止：python {os.path.basename(__file__)} --stop")
        return 0

    if args.hb_check:
        return _hb_report(_symbols_readonly())

    symbols = _resolve_symbols(args.symbols)
    keys = [k.strip().upper() for k in symbols.split(",") if k.strip()]
    cmd = [PY, "-u", CORE, "--symbols", symbols]
    if args.trade:
        cmd.append("--trade")

    n = 0
    if not _抢锁():
        _log("已有守护实例在运行（锁未拿到），本次不重复启动。")
        return 2
    _log(f"守护启动 symbols={symbols}（来源 {args.symbols}） trade={args.trade} "
         f"pid={os.getpid()} detached={bool(args.detached)} "
         f"假死阈值={HB_STALL_SEC/60:.0f}min 退避={list(BACKOFF)} 失败阈={FATAL_AFTER}")
    fail = 0
    try:
        while True:
            n += 1
            _log(f"第 {n} 次拉起核心：{' '.join(cmd[1:])}")
            started = time.time()
            stalled_kill = False
            try:
                proc = _spawn_core(cmd, new_console=not args.no_window)
            except Exception as e:
                rc = -1
                _log(f"拉起核心失败：{type(e).__name__}: {e}")
            else:
                rc = -1
                while True:
                    try:
                        rc = proc.wait(timeout=POLL_SEC)
                        break
                    except subprocess.TimeoutExpired:
                        stalled, age, path = _hb_stalled(keys, started)
                        if stalled:
                            # ★ 红线 5：任何自动干预必须留可 grep 的一行
                            _log(f"假死：心跳停滞 {age/60:.0f} 分钟"
                                 f"（{os.path.basename(path)}）→ 重启")
                            ok = _kill_core(proc)
                            if not ok:
                                _log(f"假死处置失败：PID {getattr(proc,'pid',None)} 未能终止，继续等待其自行退出")
                            stalled_kill = True
                            rc = -9
                            break

            ran = time.time() - started
            # ---- 成功运行足够久 ⇒ 计数归零（避免「偶发一次崩」被永久累计）----
            if ran >= HEALTHY_RESET_SEC:
                if fail:
                    _log(f"核心稳定运行 {ran/60:.1f} 分钟（≥{HEALTHY_RESET_SEC//60}）"
                         f"→ 失败计数 {fail} 归零")
                fail = 0
            fail += 1
            delay = BACKOFF[min(fail - 1, len(BACKOFF) - 1)]
            if fail >= FATAL_AFTER:
                _log(f"【FATAL】连续失败 {fail} 次，最后 rc={rc}，"
                     f"本次运行 {ran/60:.1f} 分钟，{datetime.now():%Y-%m-%d %H:%M:%S} —— "
                     f"核心疑似启动即崩（配置/令牌/席位/依赖），"
                     f"退避 {delay}s 后仍会重试，守护不退出")
            _log(f"核心退出 rc={rc}，{delay}s 后重启（连续失败 {fail} 次"
                 f"{'，原因=假死' if stalled_kill else ''}）")
            time.sleep(delay)
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
    return 0


if __name__ == "__main__":
    sys.exit(main())

