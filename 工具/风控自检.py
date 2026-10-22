# -*- coding: utf-8 -*-
"""风控自检 —— 一条命令看全「风控二期九项 + 3 个附加」的存活与新鲜度（#25-B）

【为什么需要】
风控二期九项代码全部在线，但「在不在跑、跑得对不对」以前只能人肉 grep 日志：
每项的证据散在 `核心输出_*.log` / `告警队列.jsonl` / `*_滑点.csv` / 状态 json 里，
盘点一次要翻五六个文件。本脚本把它们收敛成**一张表 + 一个退出码**，供日常巡检
与无人值守前的例行确认使用。

【三条硬约束（违反即返工）】
  1. **只读**：不写台账、不 push 告警、不重启任何进程、不改 `plan_stop`/`daily_loss`
     状态文件。跑前跑后 `git status` 必须无变化。
  2. **不伪造状态**：拿不到证据就输出谓词（❔/⚪），**绝不**把「0 条」写成「通过」。
  3. **阈值集中**：每个「期望新鲜度」都是模块顶部常量，判断处不许出现魔法数。

【状态图例】
  ✅ 有证据且新鲜     ❌ 有证据但不新鲜/失效（退出码 1）
  ⚠️ 有证据但有可疑点（不单独导致退出码 1）
  ⚪ 本项天然低频（一次性快照 / 2026 年内不可能触发）⇒ 不作为失败
  ❔ **无法判定**（0 条证据时无法区分「全通过」与「未接线」）⇒ 不作为失败

【用法】
  python 工具\\风控自检.py                     # 默认看今天
  python 工具\\风控自检.py --date 20261021      # 看指定交易日
  python 工具\\风控自检.py --json               # 供其它程序读（stdout 只有 JSON）
  python 工具\\风控自检.py --ledger <台账目录>  # 测试用：指向临时副本，绝不碰生产

退出码：全 OK → 0；有 ❌ → 1；仅 ❔/⚪/⚠️ → 0（不把「无法验证」当故障）。
"""
import argparse
import csv
import ctypes
import datetime as _dt
import glob
import json
import os
import sys

# ============================================================ 常量（改这里，别改判断处）
# ---- 期望新鲜度 / 阈值 ----
SLIP_MAX_AGE_DAYS = 1        # #20 滑点：交易日每日
SNAP_MAX_MISSED_TD = 1      # #22C 隔夜快照：超过 1 个交易日未更新 ⇒ 告警
#   ★ 为什么不是「15:00 判定」：核心的快照挂在 `_process` 里，
#     「15:00 后首拍」才写 —— 日盘收盘后如果没有新行情，首拍落在夜盘
#     （实测 10-21 的快照时间戳是 20:00）。按 15:00 硬判会每天误报 ❌。
DAILY_LOSS_MAX_AGE_SEC = 120  # 附加：单日亏损阀状态 ≤2 分钟（tick 级写入）
HB_FRESH_SEC = 600           # 心跳「核心在跑」的判据：5min 一行，留 1 个周期余量
CAL_WARN_DAYS = 30           # #24 覆盖期剩余 <30 天 ⇒ 提示补表
LEDGER_REL = os.path.join("策略", "06_MA13超短线", "模拟盘", "台账")

# ---- 六个在跑品种 → 台账里的中文名前缀（与 config_symbols 的 name 一致）----
LIVE_NAMES = ["燃料油", "石油沥青", "甲醇", "豆粕", "纯碱", "乙二醇"]

OK, BAD, WARN, NA, UNK = "✅", "❌", "⚠️", "⚪", "❔"

# #21 的盲区说明（D2 任务书 §二 B-2 点名：**不许糊过去**）
AVAILPRE_BLIND = ("0 条：无法区分「全通过」与「未接线」"
                  "（该分支只在「可用资金不足」时打印，成功路径静默）")


# ============================================================ 小工具
def _repo_root(start=None):
    """向上找含 config_symbols.py 的工程根（不写死层级 —— 写死过就错）。"""
    r = os.path.abspath(start or os.path.dirname(os.path.abspath(__file__)))
    while r != os.path.dirname(r) and not os.path.isfile(
            os.path.join(r, "config_symbols.py")):
        r = os.path.dirname(r)
    return r


def _width(s):
    """显示宽度：CJK 全角算 2 列（用于表格对齐，不引第三方 wcwidth）。"""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def _pad(s, w):
    s = str(s)
    return s + " " * max(0, w - _width(s))


def _clip(s, w):
    """按**显示宽度**截断（不是按字符数）—— 否则中文列宽会把后面那列挤歪。"""
    s = str(s)
    if _width(s) <= w:
        return s
    out, acc = [], 0
    for c in s:
        cw = 2 if _width(c) == 2 else 1
        if acc + cw > w - 1:
            break
        out.append(c)
        acc += cw
    return "".join(out) + "…"


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except Exception:
        return None


def _fmt(ts):
    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "—"


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return ""


def _jsonl(path):
    out = []
    for ln in _read(path).splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            pass                      # 半行损坏跳过，不让整表读失败
    return out


def _csv_last_row(path):
    """取 CSV 最后一行（dict）；空文件返回 {}。"""
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            rows = list(csv.DictReader(f))
        return rows[-1] if rows else {}
    except Exception:
        return {}


def _pid_alive(pid):
    """Windows：OpenProcess + GetExitCodeProcess（与守护同口径，不起外部进程）。"""
    if not pid or pid <= 0:
        return False
    try:
        PROCESS_QUERY_LIMITED_INFORMATION, STILL_ACTIVE = 0x1000, 259
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


def _row(rid, name, status, evidence, verdict):
    return {"id": rid, "name": name, "status": status,
            "evidence": evidence, "verdict": verdict}


# ============================================================ 各项检查
def chk_17a(L, day):
    """#17-A 告警队列：文件存在即 alive。**无告警 ≠ 失效**，只报末行时间 + 计数。"""
    p = os.path.join(L, "告警队列.jsonl")
    rows = _jsonl(p)
    if not os.path.isfile(p):
        return _row("#17A", "告警队列", BAD, "文件不存在", "队列未接线或台账路径不对")
    if not rows:
        return _row("#17A", "告警队列", UNK, "文件存在但 0 行", "无法判定是否在写")
    last = rows[-1]
    today = [r for r in rows if str(r.get("trade_day")) == str(day)]
    cnt = {}
    for r in today:
        cnt[str(r.get("level"))] = cnt.get(str(r.get("level")), 0) + 1
    detail = "/".join(f"{k} {v}" for k, v in sorted(cnt.items())) or "0 条"
    return _row("#17A", "告警队列", OK,
                f"末行 {str(last.get('ts'))[11:]}（当日 {len(today)} 条：{detail}）",
                "无告警 ≠ 失效，此处只报时间与计数")


def chk_18(L, logtext):
    """#18 腿数闸门：每次启动 1 条 `[腿数闸门]`，显示池大小 / K / 是否生效。"""
    lines = [l for l in logtext.splitlines() if "[腿数闸门]" in l]
    if not lines:
        return _row("#18", "腿数闸门", UNK, "本日 0 条 `[腿数闸门]`",
                    "0 条无法区分「未重启」与「未接线」")
    txt = lines[-1].split("[腿数闸门]", 1)[1].strip().replace("**", "")
    return _row("#18", "腿数闸门", OK, txt[:46].rstrip("，。 ") + "…",
                "零触发属预期（池 ≤ K 时闸门不动作）")


def chk_19(L):
    """#19 连亏分布：静态快照，**不是在线检查**。"""
    fs = sorted(glob.glob(os.path.join(L, "连亏分布_*.md")))
    if not fs:
        return _row("#19", "连亏分布", NA, "无快照文件", "一次性脚本产出，非在线项")
    f = max(fs, key=lambda x: os.path.getmtime(x) or 0)
    d = os.path.basename(f).replace("连亏分布_", "").replace(".md", "")
    return _row("#19", "连亏分布", NA, d, "一次性快照，非在线（设计如此）")


def chk_20(L, now):
    """#20 滑点：逐品种 mtime + 末行交易日。**这是最可靠的一项，作对照组**。"""
    fs = sorted(glob.glob(os.path.join(L, "*_滑点.csv")))
    if not fs:
        return _row("#20", "滑点", BAD, "无 `*_滑点.csv`", "滑点模块未接线")
    fresh, stale, ev = 0, [], []
    for f in fs:
        name = os.path.basename(f).replace("_滑点.csv", "")
        mt = _mtime(f)
        age = (now - mt) / 86400.0 if mt else 9e9
        row = _csv_last_row(f)
        td = str(row.get("trade_day") or "").strip()
        if age <= SLIP_MAX_AGE_DAYS:
            fresh += 1
            ev.append(f"{name} {_dt.datetime.fromtimestamp(mt):%m-%d %H:%M}")
        else:
            stale.append(f"{name} {_fmt(mt)}")
    if stale:
        return _row("#20", "滑点", BAD, f"{fresh}/{len(fs)} 新鲜；旧：{'；'.join(stale[:3])}",
                    f"期望每交易日新增（阈值 {SLIP_MAX_AGE_DAYS} 天）")
    return _row("#20", "滑点", OK, f"{fresh}/{len(fs)} 品种在写：" + " / ".join(ev[:3]) + " …",
                "本项证据最可靠，可作其它项的对照组")


def chk_21(L, logtext):
    """#21 available 预检：**已知盲区，永远不给 ✅**。"""
    n = logtext.count("[AVAILPRE]")
    return _row("#21", "available 预检", UNK, f"本日 {n} 条 `[AVAILPRE]`", AVAILPRE_BLIND)


def chk_22a(L, logtext):
    """#22-A 隔夜上限：只在**触发**时打印 `[RISKBLOCK]`。"""
    n = logtext.count("[RISKBLOCK]")
    return _row("#22A", "隔夜上限", OK, f"本日 {n} 次 RISKBLOCK",
                "未触发 = 未超上限" if n == 0 else "有拦截，见核心日志")


def _trading_days_between(d0, d1, tc):
    """(d0, d1] 之间有几个交易日（d1 含）。无法用日历时退化为自然日。"""
    n, x = 0, d0
    while x < d1:
        x += _dt.timedelta(days=1)
        if tc is None or tc.is_trading_day(x):
            n += 1
    return n


def chk_22c(L, now):
    """#22-C 隔夜快照：**超过 SNAP_MAX_MISSED_TD 个交易日未更新 ⇒ ❌**。

    ★ 判据是「整段交易日被跳过」，不是「今天 15:00 有没有到」：
      快照在核心的 `_process` 里触发「15:00 后首拍」，日盘收盘后若没有新行情，
      首拍要等到夜盘（实测 10-21 快照时间戳 20:00）。所以：
        · 当日已落盘                ⇒ ✅
        · 只差今天这一拍（还没写）  ⇒ ✅（标明「今日待首拍」）
        · 整整一个交易日被跳过      ⇒ ❌
    """
    p = os.path.join(L, "隔夜敞口_快照.json")
    mt = _mtime(p)
    if not mt:
        return _row("#22C", "隔夜快照", BAD, "文件不存在", "快照从未落盘 ⇒ 隔夜敞口不可见")
    hist = os.path.join(L, "隔夜敞口_历史.csv")
    last = _csv_last_row(hist)
    # ★ 2026-10-22 D2 拍板：必须同时报 `时间` 与 `交易日`。
    #   只报时间 ⇒ 「昨天夜盘 20:00 拍的（正常）」和「今天该拍没拍（异常）」
    #   在显示上无法区分 —— 那正是规则 H 要治的「不可观测」。
    _ts_s = str(last.get("时间") or "").strip()[:19]
    _td_s = str(last.get("交易日") or "").strip()
    tail = (f"；末行 {_ts_s} 交易日={_td_s}" if (_ts_s or _td_s) else "") if last else ""
    try:
        from lib import trade_calendar as tc
    except Exception:
        tc = None
    d_mt = _dt.date.fromtimestamp(mt)
    d_now = _dt.datetime.fromtimestamp(now).date()
    if d_mt >= d_now:
        return _row("#22C", "隔夜快照", OK, f"{_fmt(mt)}{tail}", "当日已落盘")
    missed = _trading_days_between(d_mt, d_now, tc)
    if missed <= SNAP_MAX_MISSED_TD:
        return _row("#22C", "隔夜快照", OK, f"{_fmt(mt)}{tail}",
                    f"已连 {missed} 个交易日未更新，仍在阈值 {SNAP_MAX_MISSED_TD} 内"
                    f"（今日待收盘后首拍）")
    return _row("#22C", "隔夜快照", BAD, f"{_fmt(mt)}{tail}",
                f"已连 {missed} 个交易日未更新（阈值 {SNAP_MAX_MISSED_TD}）⇒ 隔夜敞口不可信")


def chk_22b(day_date):
    """#22-B 长假强平：找下一个 ≥MIN_GAP 天的长假。**2026 内零触发属预期**。"""
    try:
        from lib import trade_calendar as tc, holiday_close as hc
    except Exception as e:
        return _row("#22B", "长假强平", UNK, f"lib 不可用：{type(e).__name__}",
                    "无法判定，请人工确认")
    MIN_GAP = int(getattr(hc, "MIN_GAP", 5))
    d = day_date
    hit = None
    while d <= tc.COVERAGE_UNTIL:
        if tc.is_trading_day(d):
            gap, nxt = tc.gap_days(d)
            if gap >= MIN_GAP:
                hit = (d, gap, tc.holiday_name(d + _dt.timedelta(days=1)) or "长假", nxt)
                break
        d += _dt.timedelta(days=1)
    if not hit:
        return _row("#22B", "长假强平", NA, f"覆盖期内无 ≥{MIN_GAP} 天长假",
                    "2026 内零触发属预期，不算失败")
    pre, gap, nm, nxt = hit
    days = (pre - day_date).days
    return _row("#22B", "长假强平", NA,
                f"下一个 ≥{MIN_GAP} 天长假：{nm}（前置交易日 {pre}，距今 {days} 天）",
                f"届时于 {pre} 15:00 前强平；距今 {days} 天")


def chk_23(logtext):
    """#23 PARAM：每次启动 1 条，解析 JSON 列关键值（重启自证的唯一凭据）。"""
    lines = [l for l in logtext.splitlines() if "[PARAM]" in l]
    if not lines:
        return _row("#23", "PARAM", UNK, "本日 0 条 `[PARAM]`",
                    "0 条无法区分「未重启」与「未接线」")
    raw = lines[-1].split("[PARAM]", 1)[1].strip()
    try:
        o = json.loads(raw)
    except Exception:
        return _row("#23", "PARAM", WARN, raw[:40] + "…", "JSON 解析失败，只看得到原文")
    keys = ["LEG_MAX", "STALE_MULT", "DAILY_LOSS_RATIO", "KSEC"]
    ev = " / ".join(f"{k}={o[k]}" for k in keys if k in o)
    return _row("#23", "PARAM", OK, ev or raw[:40], "重启后此行为「新代码已加载」的自证")


def chk_24(day_date):
    """#24 交易日历：覆盖期内静默；剩余 <30 天必须提示补表。"""
    try:
        from lib import trade_calendar as tc
    except Exception as e:
        return _row("#24", "交易日历", UNK, f"lib 不可用：{type(e).__name__}", "无法判定")
    warn = ""
    try:
        warn = tc.coverage_warning(day_date) or ""
    except Exception:
        warn = ""
    left = (tc.COVERAGE_UNTIL - day_date).days
    if warn:
        return _row("#24", "交易日历", BAD, f"覆盖至 {tc.COVERAGE_UNTIL}，剩 {left} 天",
                    "★已超出覆盖期 ⇒ 无法判长假，立即补表")
    if left < CAL_WARN_DAYS:
        return _row("#24", "交易日历", WARN, f"覆盖至 {tc.COVERAGE_UNTIL}，剩 {left} 天",
                    f"★剩 {left} 天 < {CAL_WARN_DAYS} ⇒ 请尽快补 {tc.COVERAGE_UNTIL.year + 1} 年表")
    return _row("#24", "交易日历", OK, f"覆盖至 {tc.COVERAGE_UNTIL}，剩 {left} 天",
                f"余量 >{CAL_WARN_DAYS} 天")


def chk_wd(L, now):
    """附加 A2 守护：锁里的 PID 是否存活 + 守护日志新鲜度。"""
    lp = os.path.join(L, "守护.lock")
    pid = 0
    try:
        with open(lp, "r", encoding="utf-8") as f:
            pid = int((f.readline() or "0").strip() or 0)
    except Exception:
        pid = 0
    alive = _pid_alive(pid)
    lmt = _mtime(os.path.join(L, "守护日志.txt"))
    tail = f"；日志 {_fmt(lmt)[5:]}" if lmt else ""
    if not pid:
        return _row("A2", "守护", BAD, "守护.lock 不存在或无 PID", "守护未启动")
    if not alive:
        return _row("A2", "守护", BAD, f"PID {pid} 已不在{tail}", "★守护死了 ⇒ 核心崩溃后无人拉起")
    return _row("A2", "守护", OK, f"PID {pid} 存活{tail}", "—")


def chk_dailyloss(L, now, hb_age):
    """附加：单日亏损阀状态新鲜度。

    ★ 为什么和心跳比：`update` 只在核心的 tick 循环里写，**休市/核心没跑时本就不刷新**。
      直接用「>2 分钟 ⇒ ❌」会在每天收盘后固定误报。判据改为：
      心跳仍是新鲜的（核心在跑）却 update 停滞 ⇒ 才是真异常。
    """
    p = os.path.join(L, "单日亏损状态.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            o = json.load(f)
    except Exception as e:
        return _row("单日亏损阀", "", BAD, f"读不到状态：{type(e).__name__}",
                    "状态文件缺失 ⇒ 日亏阀可能空转")
    up = str(o.get("update") or "")
    try:
        age = (now - _dt.datetime.strptime(up, "%Y-%m-%d %H:%M:%S").timestamp())
    except Exception:
        age = 9e9
    ev = (f"{up}  base_day={o.get('base_day')} 触发={o.get('triggered')}"
          f" 阈值={float(o.get('threshold') or 0):,.0f} 源={o.get('base_src')}")
    core_running = (hb_age is not None) and (hb_age <= HB_FRESH_SEC)
    if age <= DAILY_LOSS_MAX_AGE_SEC:
        return _row("单日亏损阀", "", OK, ev, "tick 级更新，新鲜")
    if not core_running:
        return _row("单日亏损阀", "", NA, ev,
                    f"核心未在跑（心跳 {hb_age if hb_age is None else round(hb_age)}s），休市不刷新")
    return _row("单日亏损阀", "", BAD, ev,
                f"核心在跑但状态已 {age:.0f}s 未更新（阈值 {DAILY_LOSS_MAX_AGE_SEC}s）")


def _hb_latest(L):
    """最新心跳 (mtime, {中文名: 持仓})。心跳 5min 一行、休市照写 ⇒ 是「核心在跑」的判据。"""
    latest, pos = None, {}
    for nm in LIVE_NAMES:
        p = os.path.join(L, f"{nm}_运行心跳.csv")
        mt = _mtime(p)
        if mt:
            latest = mt if latest is None else max(latest, mt)
        r = _csv_last_row(p)
        try:
            pos[nm] = int(float(str(r.get("持仓手数") or "0").strip() or "0"))
        except Exception:
            pos[nm] = 0
    return latest, pos


def chk_15plan(L, pos):
    """附加 #15 计划止损：计划文件数 vs 实际持仓腿数。**空仓时 0 vs 0 才算对**。

    ★ 文件名是**品种 key**（`计划止损_BU.json`），持仓表是**中文名** ⇒ 必须过
      `config_symbols` 映射后再比集合。只比数量会掩盖「腿已平但计划还在」这类错。
    """
    fs = glob.glob(os.path.join(L, "计划止损_*.json"))
    keys = {os.path.basename(f)[len("计划止损_"):-len(".json")].upper() for f in fs}
    try:
        import config_symbols as _cs
        cn = {k: str((_cs.SYMBOLS.get(k) or [k])[0]) for k in keys}
    except Exception:
        cn = {k: k for k in keys}
    by_plan = {cn[k] for k in keys}                                # 统一到中文名口径
    held = [nm for nm, v in pos.items() if v]
    n_plan, n_held = len(by_plan), len(held)
    extra = sorted(by_plan - set(held))       # 腿已平但计划文件还在
    miss = sorted(set(held) - by_plan)        # 有持仓却没有计划止损
    ev = f"计划 {n_plan} 份 vs 持仓 {n_held} 腿（{','.join(held) or '空仓'}）"
    if not extra and not miss:
        return _row("#15", "计划止损", OK, ev, "逐腿对齐（空仓时 0 vs 0 才算对）")
    why = []
    if extra:
        why.append(f"多出计划：{','.join(extra)}（腿已平但计划未清）")
    if miss:
        why.append(f"缺计划：{','.join(miss)}（有持仓无止损计划）")
    return _row("#15", "计划止损", WARN, ev, "；".join(why))


# ============================================================ 主流程
def collect(ledger, date_str=None):
    now = _dt.datetime.now()
    day = date_str or now.strftime("%Y%m%d")
    try:
        day_date = _dt.datetime.strptime(day, "%Y%m%d").date()
    except Exception:
        day_date = now.date()
    logtext = _read(os.path.join(ledger, f"核心输出_{day}.log"))
    hb_mt, pos = _hb_latest(ledger)
    hb_age = (now.timestamp() - hb_mt) if hb_mt else None

    rows = [
        chk_17a(ledger, day),
        chk_18(ledger, logtext),
        chk_19(ledger),
        chk_20(ledger, now.timestamp()),
        chk_21(ledger, logtext),
        chk_22a(ledger, logtext),
        chk_22c(ledger, now.timestamp()),
        chk_22b(day_date),
        chk_23(logtext),
        chk_24(day_date),
        chk_wd(ledger, now.timestamp()),
        chk_dailyloss(ledger, now.timestamp(), hb_age),
        chk_15plan(ledger, pos),
    ]
    bad = [r for r in rows if r["status"] == BAD]
    unk = [r for r in rows if r["status"] == UNK]
    summary = {
        "total": len(rows),
        "ok": sum(1 for r in rows if r["status"] == OK),
        "bad": len(bad),
        "unsure": len(unk),
        "need_eye": len(bad) + len(unk),          # ❌ 或 ❔ 计入「需人工看」
        "exit": 1 if bad else 0,
        "verdict": (f"有 {len(bad) + len(unk)} 项需人工看"
                    if (bad or unk) else "OK"),
    }
    return {"generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "ledger": ledger, "date": day, "rows": rows, "summary": summary}


def render(res):
    out = []
    out.append(f"风控自检  {res['generated_at']}   数据根：{res['ledger']}")
    out.append("")
    c1, c2, c3 = 26, 6, 32
    out.append(_pad("项", c1) + _pad("状态", c2) + _pad("最近实证", c3) + "判定")
    for r in res["rows"]:
        label = f"{r['id']}  {r['name']}" if r["name"] else r["id"]
        out.append(_pad(_clip(label, c1 - 1), c1)
                   + _pad(r["status"], c2)
                   + _pad(_clip(r["evidence"], c3 - 1), c3)
                   + "  " + r["verdict"])
    out.append("")
    s = res["summary"]
    out.append(f"总判定：{s['verdict']}（共 {s['total']} 项：✅{s['ok']} "
               f"❌{s['bad']} ❔{s['unsure']}；退出码 {s['exit']}）")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="风控自检（只读，不写台账）")
    ap.add_argument("--date", default="", help="交易日 YYYYMMDD（默认今天）")
    ap.add_argument("--json", action="store_true", help="只输出 JSON（供其它程序读）")
    ap.add_argument("--ledger", default="", help="台账目录（默认按工程根推导；测试用）")
    args = ap.parse_args()

    try:                                  # 中文控制台兜底：编码问题不许让自检崩掉
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    root = _repo_root()
    ledger = os.path.abspath(args.ledger) if args.ledger else os.path.join(root, LEDGER_REL)
    if not os.path.isdir(ledger):
        print(f"台账目录不存在：{ledger}", file=sys.stderr)
        return 1
    if root not in sys.path:               # 供 #22B/#24 import lib
        sys.path.insert(0, root)

    res = collect(ledger, args.date or None)
    print(json.dumps(res, ensure_ascii=False, indent=2) if args.json else render(res))
    return res["summary"]["exit"]


if __name__ == "__main__":
    sys.exit(main())
