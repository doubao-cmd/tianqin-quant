# -*- coding: utf-8 -*-
"""
MA13 超短线 · 多品种单进程实时模拟盘（P/EB 双候选版）
==========================================================
为什么单进程跑多品种：同一 tqsdk 账号同时只能一处登录——N 个品种开 N 个
进程就要 N 个账号席位；单进程共用一个会话订阅多合约，只占一个席位。

信号逻辑与 ma13_live_core.py（单品种版）/ 回测脚本逐条一致：
  1. MA13 与其前 SLOPE_N 根的差判趋势方向（斜率）；
  2. |close-MA13| <= RETRACE_TOL×ATR 且趋势明确 → 沿趋势开仓（下一根开盘≈收盘后市价）；
  3. 止损 = 回踩区间极值外侧 STOP_BUFFER×ATR，封顶 STOP_CAP×ATR；时间止损 MAX_HOLD 根；
  4. 手数 = 当前账户权益 ×1% ÷ ATR，夹 [1, 8]（2026-10-07 改复利口径，原为固定 20 万；
     ⚠️ 多品种共用一个账户 ⇒ 各品种手数互相耦合：P 的盈亏会改变 JM 的手数基数）。

与单品种版的差异（仅工程结构，信号零差异）：
  - 每品种独立 quote / kline / 持仓状态 / 台账（成交流水+心跳各一份）
  - 共用一个 TqApi 会话与 wait_update 主循环
  - 账号用 config_account.check_live()（模拟盘专用账号，不与回测抢席位）
  - 修复单品种版的既存 bug：led 未定义（allow_trade=True 时会 NameError）

禁未来函数：只用已收盘 bar；新 bar 出现（=前一根收盘）瞬间判定，立即执行。
默认 ALLOW_TRADE=False 观察模式：只算信号、打印、写台账，不报单。
"""
import sys, os, time, math, json, threading, io, datetime as _dt
import numpy as np

_ROOT = os.path.dirname(os.path.abspath(__file__))
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tqsdk import TqApi, TqSim, TqKq, TqAuth
from lib.engine import _LiveRecorder
from lib.ops import (OrderLedger, RiskGate, atomic_write_json, read_json,
                     leg_prefix, find_orphan)
# ★ 2026-10-20 新增：撤单重报 / 撤单预算 / 反手同拍净仓口径（纯逻辑，可离线单测）
from lib import live_resend as _rs
import config_account
import config_symbols

# ---- 工作台信号接口（单一真相源：MA13 逻辑由此取，不再内联副本）----
import sys as _sys
# ★★ 2026-10-18 修（P0）：这里原本写成 `os.path.join(_ROOT, "工作台")`，
#   而 _ROOT 是**项目根**（天勤量化），其下并没有 `工作台/` 目录
#   ⇒ `from signals import` 必然 ModuleNotFoundError，进程启动即崩。
#   （实测：_ROOT/工作台 不存在，真路径在 _ROOT/策略/06_MA13超短线/工作台。）
#   显式校验，绝不静默跳过 —— 静默跳过会让 06 退回到「没有信号源」的状态。
_WORKBENCH_DIR = os.path.join(_ROOT, "策略", "06_MA13超短线", "工作台")
if not os.path.isdir(_WORKBENCH_DIR):
    raise RuntimeError(
        f"工作台目录不存在：{_WORKBENCH_DIR}\n"
        f"（ma13_live_multi 依赖工作台的 signals.py 作为唯一信号源，缺了无法启动）")
if _WORKBENCH_DIR not in _sys.path:
    _sys.path.insert(0, _WORKBENCH_DIR)
from signals import Ma13Signal
from sizer import get_sizer, RiskParSizer
_SIG = Ma13Signal()   # 默认信号（MA13 占位）；ctx 提供其他因子时以 ctx 为准
_SIZER = get_sizer("risk_par")  # 默认仓位器；ctx 提供其他时以 ctx 为准

# ============================================================
# ★★ 核心层单实例锁（2026-10-09 新增）★★
#   为什么守护层的 守护.lock 还不够（这是实测出来的，不是推演）：
#   今天上午发现**两个守护进程能同时抢到锁** —— 它们同一秒启动，
#   各自的 _抢锁() 读对方锁里的 PID、都判定「已死」，于是双双接管、
#   各自拉起一个核心。两个核心 = 双份下单 + 双份台账写入。
#   当时没出事纯属运气：tqsdk-venv 那条链的核心卡在连天勤之前
#   （1 线程 / CPU 0.0s / 55 句柄），根本没跑起来。
#   ⇒ 「守护锁」防的是重复触发；「核心锁」防的是**重复下单**。
#      后者才是真正不可承受的那道。宁可有冗余，不可有缺口。
_CORE_LOCK = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "台账", "核心.lock")
_core_lock_fh = None


def _pid_alive(pid):
    """该 PID 是否还活着（Windows 用 OpenProcess 判，别用 psutil）。"""
    if not pid or int(pid) <= 0:
        return False
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, 0, int(pid))     # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259      # STILL_ACTIVE
    except Exception:
        return False


def _抢核心锁():
    """抢核心单实例锁。抢到返回句柄（模块级持有，进程活则锁在），否则 None。

    ★ 2026-10-18 修（P0）：加**幂等**分支。原来只有在 `__main__` 里抢锁，
      而 `run_workbench.py` 是 `import ma13_live_multi` 后直接调 `run_live_multi()`，
      **完全绕过这把锁** ⇒ 工作台启动的核心能与 watchdog 拉起的核心并存
      = 双份下单 + 双份台账写入（正是这把锁存在的理由）。
      现在把抢锁下沉进 `run_live_multi`，因此同一进程可能抢两次 ⇒ 必须幂等。
    """
    global _core_lock_fh
    if _core_lock_fh is not None:
        return _core_lock_fh        # 本进程已持有 ⇒ 直接复用（幂等）
    os.makedirs(os.path.dirname(_CORE_LOCK), exist_ok=True)
    for _ in range(2):
        try:
            fh = open(_CORE_LOCK, "x", encoding="utf-8")
            fh.write(f"{os.getpid()}\r\n{time.strftime('%Y-%m-%d %H:%M:%S')}\r\n")
            fh.flush()
            _core_lock_fh = fh          # 模块级持有引用，防被 GC 关闭
            return fh
        except FileExistsError:
            old = 0
            try:
                with io.open(_CORE_LOCK, "r", encoding="utf-8") as f:
                    old = int((f.readline() or "0").strip() or 0)
            except Exception:
                old = 0
            if _pid_alive(old):
                return None             # 真有另一个核心在跑 ⇒ 绝不双开
            try:
                os.remove(_CORE_LOCK)    # 僵尸锁（上次被强杀留下）⇒ 清掉重抢
            except Exception:
                return None
    return None


def _释放核心锁():
    """只在锁里是自己 PID 时才删（防误删别人的）。"""
    try:
        with io.open(_CORE_LOCK, "r", encoding="utf-8") as f:
            mine = int((f.readline() or "0").strip() or 0) == os.getpid()
        if mine:
            os.remove(_CORE_LOCK)
    except Exception:
        pass


MA_N, SLOPE_N, ATR_N = 13, 5, 14
RETRACE_TOL, STOP_BUFFER, STOP_CAP, MAX_HOLD = 0.5, 0.5, 2.0, 15
# ★ 盘中触价扫损（2026-10-17 升级）：每 tick 用实时 last_price 判触价，触及即市价平，
# 成交≈止损线 ±1 跳（与期货止损单一致）。置 False 退回「收盘才查」旧行为。
INTRABAR_STOP = True
RISK_PCT, SLIP, HAND_MIN, HAND_MAX = 0.01, 1, 1, 8
NOTIONAL_CAP = 2.0   # 名义敞口上限（倍权益）：手数 ≤ 权益×2÷每手名义价值（与回测同步 2026-10-07）
OPS_MAX_MARGIN_RATIO = 0.60   # 运维闸门：保证金占用 >60% 拒绝**开仓**（平仓永不被挡）
OPS_MAX_SAME_SIDE = 2.0       # 运维闸门：同向名义敞口 >2.0×权益 拒绝开仓
#                              正常态：4 空腿约 1.28×，5 腿约 1.66× —— 2.0 只在失控时触发
INIT = 200000.0
RISK_EQUITY = 200000.0     # 手数计算基数（锁死 20 万，与回测 INIT 严格对齐）
# ★ 2026-10-08 切 TqKq 云端账户后 RISK_EQUITY 由「兜底值」升级为「强制值」：
#   云端账户真实余额约 100 万，若拿它算手数 ⇒ 名义敞口上限 40万→200万（放大 5 倍），
#   且与回测（INIT=20万）彻底脱钩、无法对账。故 _hand_of 一律吃 RISK_EQUITY，
#   账户真实余额只用于熔断判断与台账展示。做法对齐 07_布林带
#   （engine.run_live_boll 传 risk_equity=400000，注释「账户100万里锁死60万」）。
KSEC = 300
DD_FUSE = 0.20             # 账户级回撤熔断线（自峰值回撤 20%）。纯灾难保护阀，
                           # 非策略参数；数值为占位初值，SOP 欠账#1 用真数据定稿。
RECONNECT_WAIT = 10   # 断线后等待秒数（重连前歇一口气，避免撞上限流）
# ★ 2026-10-08切 TqKq 云端账户后新增：云端持仓对账
#   问题：s["pos"] 原是进程内存变量，只在开/平仓时改。切TqSim 时本地账本与
#   内存天然一致；切 TqKq 后**两者会脱节**——收盘关闭/崩溃重启后进程以为空仓，
#   而云端仍有持仓 ⇒ 不挂止损（持仓裸奔），甚至可能重复开仓把仓位翻倍。
#   做法：对齐 engine.run_live_boll 的 attach_stop_on_start + 定期对账，
#   但**更简单也更安全**：以云端持仓为唯一真相源，每根 bar 前覆盖 s["pos"]。
#   只同步「持仓事实」，不产生任何新开仓意图（规则2：不加条件、不择优）。
SYNC_CLOUD = True        # 是否以云端持仓为准同步（关掉=退回旧的纯内存行为）
# 刚下过单后的「等撮合」窗口（秒）：期间不做云端对账，避免把**自己刚开的仓**
#   误判成外部遗留持仓而假接管。实测撮合延迟 < 2s，取 30s 留足余量
#   （宁可晚 30 秒才对账，也不能错一次止损线）。
FIX_WAIT_SEC = 30

# ---- ★★ P0-1 撤单重报（2026-10-20 新增，见 交接\06模拟盘_风控增强任务书_20261019.md）----
#   缺口：原实现只在 FIX_WAIT_SEC 后判一次「成交/未成交」，**从不撤单**。
#   两种后果：① 该平没平掉 ⇒ 裸奔；② 让价 2 跳不够 ⇒ 单子永久挂着占额度。
#   做法：未成交超 RESEND_WAIT_SEC ⇒ 撤单 → 按未成交量加价重报 **1 次** → 日撤单计数。
#   红线（任务书 §六-1）：必须节流，不得触发交易所报撤单限制。
#     tqsdk 内置 RiskManager 只限**每秒**操作数，**不管日上限** ⇒ 日计数自建（lib/live_resend）。
#   红线（任务书 §六-2）：撤单只为补救成交，**绝不改变出场点位**（不碰提前出场家族）。
RESEND_WAIT_SEC = _rs.RESEND_WAIT_SEC          # 45s（须 > FIX_WAIT_SEC）
MAX_RESEND_PER_ORDER = _rs.MAX_RESEND_PER_ORDER  # 1 次
_RS_BUDGET = None


def _rs_budget(record_dir):
    """进程级撤单预算单例（**跨腿共享**：交易所上限是账户级的，不是每腿一份）。"""
    global _RS_BUDGET
    if _RS_BUDGET is None:
        p = (os.path.join(record_dir, "撤单预算_%s.json" % _rs.trading_day())
             if record_dir else None)
        _RS_BUDGET = _rs.CancelBudget(p)
    return _RS_BUDGET

# ★ 2026-10-18：默认列表同步为当前卡C 选品种结果（MA,JM,EB,FU,M,EG）。
#   ⚠️ 唯一真相源 = 工作台/workbench.json 的 selected；正式启动一律走
#      `ma13_watchdog_main.py --symbols auto`（启动前读该文件并校验规格）。
#      这里的常量只是「有人裸跑 ma13_live_multi.py 且不传 --symbols」时的兜底，
#      换品种后必须同步，否则裸跑会按旧列表下单（2026-10-08 已因此踩过一次）。
def _default_symbols():
    """独立启动时的默认品种：**优先读 workbench.json 的 selected**。

    ★ 2026-10-18 修：原为硬编码 `["MA","JM","EB","FU","M","EG"]`，带来两个问题：
      ① **EB 是僵尸腿**（成交流水停在 10-09、心跳再没更新，四期方案已拍板删掉），
         直接双击启动就会把它重新拉起来；
      ② 与卡C 的实际选池完全脱节（配置里是 JM/FU/BU/MA/M/SA），两条启动路径
         池子不一致，排查时极易误判「为什么持仓和配置对不上」。
      ⇒ 改成读配置：没有配置文件时才回退到一个**不含任何僵尸品种**的保守列表。
      watchdog / run_workbench 永远显式传品种，不受这里影响。
    """
    try:
        _wb = os.path.join(_ROOT, "策略", "06_MA13超短线", "工作台", "workbench.json")
        with open(_wb, "r", encoding="utf-8") as f:
            sel = [str(k).upper() for k in (json.load(f).get("selected") or [])]
        if sel:
            return sel
    except Exception:
        pass
    return ["MA", "JM", "FU", "M"]      # 兜底：均为跑过且有台账的品种


DEFAULT_SYMBOLS = _default_symbols()


def _popup(title, msg):
    """Windows 原生弹窗提醒（独立线程，绝不阻塞交易主循环）。
    无窗口环境/非 Windows 下静默降级为只在控制台响铃。"""
    print("\a" + f"[弹窗] {title}: {msg}", flush=True)

    def _go():
        try:
            import ctypes
            # 48=MB_ICONWARNING 警告图标, 0x40000=MB_SYSTEMMODAL 置顶
            ctypes.windll.user32.MessageBoxW(0, msg, title, 48 | 0x40000)
        except Exception:
            pass                      # 弹不出来不能影响交易
    threading.Thread(target=_go, daemon=True, name="popup").start()


def _connect(user, pwd, specs, web_gui):
    """建立会话并完成全部品种订阅；失败时关闭半开会话再抛出。

    ★ 2026-10-08 改用 TqKq（快期模拟账户，云端）——用户要求「手机能同步看持仓」。
      原 TqSim(INIT) 是**本地内存虚拟账本**（tqsdk tradeable/sim/）：撮合全在本进程内、
      订单不出电脑、**重启即归零**，所以手机 App 永远看不到。TqKq 在 tradeable/otg/
      是**天勤云端快期模拟账户**，订单真的上行，手机/快期客户端都能看到持仓与权益，
      重启后也保留。与 07_布林带（engine.run_live_boll）用的是同一个账户体系。
      ⚠️ 代价：INIT 不再决定余额，账户真实余额（约 100 万）才是基准 ⇒ 必须靠
         RISK_EQUITY 把手数基数锁死回 20 万，否则名义敞口放大 5 倍（见下方常量）。
    """
    api = TqApi(TqKq(), auth=TqAuth(user, pwd), web_gui=web_gui)
    try:
        for s in specs:
            s["q"] = api.get_quote(s["cont"])
            s["k"] = api.get_kline_serial(s["cont"], KSEC, data_length=200)
    except Exception:
        try:
            api.close()
        except Exception:
            pass
        raise
    return api


# ----------------------------------------------------------------------------
# ★ 工作台集成辅助（ctx=None 时完全不启用，保持原行为；懒加载 universe 避免污染）
# ----------------------------------------------------------------------------
def _workbench_sid(q):
    """从行情时间戳取 session_id；失败返回 'closed'。"""
    try:
        _wb = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "工作台")
        if _wb not in sys.path:
            sys.path.insert(0, _wb)
        import universe as _u
        return _u.session_id(q.datetime)
    except Exception:
        return "closed"


def _workbench_gate_tick(ctx, s, sid, q):
    """在每根 bar 驱动时段闸门：检测 session 边界，收盘记收益、开盘消费封禁。"""
    try:
        if not isinstance(ctx, dict):
            return
        gate = ctx.get("gate")
        if gate is None:
            s["_last_sid"] = sid
            return
        lp = float(q.last_price) if (q.last_price and not math.isnan(q.last_price)) else 0.0
        prev = s.get("_last_sid")
        if prev and prev != sid and prev != "closed":
            open_px = s.get("_sess_open_px") or lp
            ret = (lp - open_px) / open_px if open_px else 0.0
            gate.on_session_close(s["key"], prev, ret)
        if prev != sid:
            s["_sess_open_px"] = lp
            gate.on_session_open(s["key"], sid)
            s["_last_sid"] = sid
    except Exception:
        pass


def _init_spec_state(s, record_dir, sizer, heartbeat_sec):
    """初始化单个品种的运行态（持仓/止损/记录器/风控闸门）。

    ★ 2026-10-18 抽出成函数：`_adopt_carried_legs`（换池保护）要在建连后
      动态追加品种，必须复用同一套初始化，否则新腿缺 rec/risk/hand_of 会直接炸。
    """
    # ★ recovered：标记「这个持仓是从云端接管来的，不是本进程开的」。
    #   接管时必须补挂止损（云端不存我们的止损线），且退出时要打醒目事件。
    s.update({"pos": 0, "entry": 0.0, "stop": 0.0, "bars_in": 0,
              "last_id": -1, "skip_first": False, "recovered": False,
              "last_order_ts": 0.0,
              # ★ 工作台集成：session 边界追踪（供时段闸门，默认 None 不启用）
              "_last_sid": None, "_sess_open_px": 0.0,
              # ★★ 换月移交：本地记录的持仓合约（持久化，重启不丢）+ 落盘目录
              "underlying": _load_underlying(record_dir, s["key"]),
              "record_dir": record_dir, "rollover_logged": False})
    # ★ 手数算法改由 sizer 提供（单一真相源，见 sizer.py，Stage 3）。
    #   闭包保留旧签名 (atr, equity, price) 以兼容调用点；equity 入参被忽略
    #   （sizer 内部锁死 equity_base=20万），与旧 _hand_of 行为逐字一致。
    #   tick/mult 仍从本品种 s 取（它们是品种属性，不进 sizer）。
    def _hand_of(atr, equity=None, price=None, _s=s, _sz=sizer):
        return _sz.hand(atr, _s["tick"], _s["mult"], price)
    s["hand_of"] = _hand_of
    s["rec"] = _LiveRecorder(record_dir, s["name"], heartbeat_sec=heartbeat_sec)
    # ---- 运维层（2026-10-17）：订单三层账 + 开仓风控闸门 ----
    #   <品种>_订单生命周期.csv：ORDER_SENT / FILL_CONFIRMED / ORDER_REJECTED / ORDER_UNCONFIRMED
    #   与既有「成交流水.csv」并存（后者保留为"意图时刻"，统计只认 CONFIRMED）。
    try:
        s["ledger"] = (OrderLedger(os.path.join(record_dir, s["name"] + "_订单生命周期.csv"))
                       if record_dir else None)
        s["risk"] = RiskGate(max_hands=HAND_MAX, max_margin_ratio=OPS_MAX_MARGIN_RATIO,
                             max_same_side_ratio=OPS_MAX_SAME_SIDE)
    except Exception:
        s["ledger"], s["risk"] = None, None
    return s


def _adopt_carried_legs(api, specs, shared, record_dir, sizer, heartbeat_sec):
    """★★ 换池保护（2026-10-18 新增，卡C 六天重选引入的新风险的唯一正确解法）。

    问题：止损线是**本进程自己算的**（云端不存），重启后靠 `_sync_cloud_pos` 对
    「已订阅的品种」逐个对账。若某品种被换出池子 ⇒ 根本不订阅 ⇒ 既不对账、
    也没有止损/时间止损 ⇒ 云端持仓**永久裸奔**、永远不会自然出场。
    这直接违背四期红线：有持仓的品种必须留到自然出场，不许为换池平仓。

    做法：反向补齐——扫账户全部持仓，凡是「有仓但不在本次池内」的品种，
      补订阅 + 接管 + 标 `_carried=True`（**只许出场、不许开新仓**）。
    返回新增的 key 列表（供日志与断言）。

    ⚠️ 必须在 `_connect` 之后调用（要先有 api 才读得到持仓），且必须同步补
       `shared[key]`——`_process` 开头就 `shared[s["key"]]`，缺了会 KeyError。
    """
    try:
        all_pos = api.get_position()        # 不传 symbol = 账户全部持仓
        pos_map = {str(sym): int(getattr(p, "pos", 0) or 0) for sym, p in all_pos.items()}
    except Exception as e:
        print(f"[换池保护] ⚠️ 读取云端持仓失败，跳过本次接管：{e!r}", flush=True)
        return []

    have = {str(s["key"]).upper() for s in specs}
    added = []
    for sym, net in sorted(pos_map.items()):
        if not net:
            continue
        if "." not in sym:
            continue
        code = leg_prefix(sym).split(".")[-1]
        if not code:
            continue
        key = code.upper()
        if key in have:
            continue
        try:
            # ★★ 绝不能直接调 config_symbols.get()：它对未知品种 raise **SystemExit**
            #   （config_symbols.py:56），而 SystemExit 不是 Exception 子类，
            #   `except Exception` 捕不到 ⇒ 一个陌生品种的遗留持仓就会 sys.exit 掉
            #   整个模拟盘进程（实测：用 DCE.zz9999 复现，进程直接退出）。
            if key not in config_symbols.SYMBOLS:
                raise KeyError(f"{key} 不在 config_symbols.SYMBOLS（无核验合约规格）")
            name_cn, cont, mult, tick = config_symbols.get(key)
            comm = float(config_symbols.COST.get(key, 2.0))
        except (Exception, SystemExit) as e:
            print(f"[换池保护] ⚠️ {sym} 有 {net} 手，但 {key} 无核验合约规格，"
                  f"无法接管（需人工处理）：{e!r}", flush=True)
            continue
        s = {"key": key, "name": name_cn, "cont": cont,
             "mult": mult, "tick": tick, "comm": comm, "_carried": True}
        _init_spec_state(s, record_dir, sizer, heartbeat_sec)
        try:
            s["q"] = api.get_quote(cont)
            s["k"] = api.get_kline_serial(cont, KSEC, data_length=200)
        except Exception as e:
            print(f"[换池保护] ⚠️ {key} 订阅失败，未接管：{e!r}", flush=True)
            continue
        # ★ 顺序要紧：**先补 shared 再 append specs**。
        #   心跳线程（_hb_loop）此时已在跑并遍历 `list(specs)`，若先 append，
        #   它会瞬间拿到新 key 却还不存在于 shared 里 ⇒ KeyError 打挂心跳线程。
        shared.setdefault(key, {"dt": "", "price": 0.0, "pos": 0, "tgt": 0})
        specs.append(s)
        have.add(key)
        added.append(key)
        msg = (f"换池保护：{sym} 云端有 {net} 手，本品种不在本次选池内 → "
               f"已补订阅接管，只出场不开新仓")
        try:
            s["rec"].event("CARRIED", msg)
        except Exception:
            pass
        print(f"[换池保护] {msg}", flush=True)
    return added


def run_live_multi(symbol_keys=None, allow_trade=False, web_gui=False,
                   record_dir=None, heartbeat_sec=300, drill_fuse=False,
                   ctx=None):
    # ★★ 核心单实例锁（2026-10-18 下沉到这里）：任何入口（__main__ / run_workbench
    #   / 守护）都必经本函数 ⇒ 锁放在这里才真正覆盖所有启动路径，绝不双开下单。
    if _抢核心锁() is None:
        print("[核心] ✗ 另一个核心实例正在运行（核心.lock 被活进程占用）→ 拒绝启动，"
              "绝不双开下单。", flush=True)
        return False
    # 信号实例：优先用 ctx 提供的（未来换正期望因子时由 build_context 注入），
    # 否则默认 MA13 占位。ctx=None（独立启动）也保持原行为不变。
    sig = (ctx or {}).get("signal") or _SIG
    # 仓位器：同理，优先 ctx 提供的 sizer，否则默认 risk_par（与旧 _hand_of 同口径）。
    sizer = (ctx or {}).get("sizer") or _SIZER
    keys = list(symbol_keys or DEFAULT_SYMBOLS)
    specs = []
    for key in keys:
        name_cn, cont, mult, tick = config_symbols.get(key)
        comm = float(config_symbols.COST.get(key, 2.0))
        specs.append({"key": key, "name": name_cn, "cont": cont,
                      "mult": mult, "tick": tick, "comm": comm})

    user, pwd = config_account.check_live()

    if record_dir is None:
        record_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "台账")
    os.makedirs(record_dir, exist_ok=True)

    # ---- 每品种独立状态（与具体会话无关，重连后沿用） ----
    for s in specs:
        _init_spec_state(s, record_dir, sizer, heartbeat_sec)

    names = " / ".join(s["name"] for s in specs)
    for s in specs:
        s["rec"].start(f"MA13 多品种实时盘启动 [{names}] allow_trade={allow_trade} "
                       f"手数基数=锁定20万(与回测对齐) x1%夹[1,8]+名义敞口≤2倍(20261008切TqKq云端) "
                       f"账户=TqKq云端快期模拟(手机可同步)")

    # ★★ A11 版本切换自记录（2026-10-19）★★
    #   复盘按 INTRABAR_STOP 口径分段，需要「口径从哪一刻真正生效」这个事实。
    #   口径值的**代码落地时刻 ≠ 生效时刻**：进程没重启就还在跑旧码
    #   （2026-10-18 22:06 改成 True，但 22:01 起的进程一直跑到今天，跑的全是旧码）。
    #   ⇒ 只在**启动**时写，且只写「口径与上次不同」的那一次；同一口径反复重启不刷屏。
    #   写台账失败只打印，绝不影响交易（这条路径在主循环之前，抛异常就等于起不来）。
    try:
        from lib.version_log import log_version_switch
        _vr = log_version_switch(
            record_dir, INTRABAR_STOP,
            code_paths=[os.path.abspath(__file__),
                        os.path.join(_ROOT, "lib", "engine.py")],
            pool=[s["key"] for s in specs],
            note=f"allow_trade={allow_trade}")
        _tag = {"append": "写入", "correct": "修正", "unchanged": "无需写",
                "error": "失败"}.get(_vr["action"], _vr["action"])
        print(f"[A11] 版本记录{_tag}：{_vr['why']} → {_vr['line']}", flush=True)
    except Exception as _e:
        print(f"[A11] 版本记录异常（不影响交易）：{type(_e).__name__}: {_e}", flush=True)

    # ---- 心跳独立线程：休市/无行情时照写，防台账断层误报 ----
    # 铁律：线程内绝不触碰 tqsdk 对象（q/k/api），只读 shared 里的纯标量缓存。
    # tqsdk 对象跨线程使用会返回全零帧（项目既有教训）。
    shared = {s["key"]: {"dt": "", "price": 0.0, "pos": 0, "tgt": 0} for s in specs}
    import threading

    def _hb_loop():
        while True:
            time.sleep(5)
            # ★ 用 list(...) 快照：specs 可能在建连后被 _adopt_carried_legs 追加，
            #   直接迭代一个正在被追加的列表不安全。
            for s in list(specs):
                sh = shared[s["key"]]
                # ★ 2026-10-19 修（P0，实测）：「持仓」取 s["pos"]（真相源），不取 sh["pos"]（缓存）。
                #   那个缓存只在「有新 bar 且走完流程」时 sh.update（见文件末尾），
                #   而 RECOVER 接管是**不依赖新 bar** 的 —— 10-19 14:55:30 云端接管把
                #   s["pos"] 改回 -6，缓存仍停在 0，之后收盘再无新 bar ⇒ 心跳 5 分钟一行
                #   全写 0，与 EQUITY 行的 -6 打架，看起来像"仓位丢了"。
                #   s["pos"] 是普通 int，跨线程读安全（本线程铁律只禁碰 tqsdk 对象）。
                s["rec"].heartbeat(sh["dt"], sh["price"],
                                   int(s.get("pos", sh["pos"])), sh["tgt"])

    threading.Thread(target=_hb_loop, daemon=True, name="heartbeat").start()

    # ---- 账户级回撤熔断状态（持久化到 台账/熔断状态.json）----
    # 语义：峰值与闩死状态都跨会话保存。触发后即使重启进程也保持"只出不进"，
    # 直到人工删除 熔断状态.json（=确认复位）再重启。
    # ★ 2026-10-08 切 TqKq 后peak 初值仍用 INIT 作**兜底下限**；云端账户真实余额
    #   （约 100 万）会在第一次 _fuse_eval 时把 peak 抬到真实水平——这是正确的：
    #   熔断管的是整个账户的回撤，peak 必须跟着账户走，不能停在 20 万，
    #   否则账户从 100 万跌到 100-4=96 万就被误判为「回撤 380%」而误触发。
    state_path = os.path.join(record_dir, "熔断状态.json")
    fuse = {"peak": INIT, "halted": False, "path": state_path}

    def _save_fuse():
        try:
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump({"peak": fuse["peak"], "halted": fuse["halted"],
                           "update": time.strftime("%Y-%m-%d %H:%M:%S")},
                          f, ensure_ascii=False)
        except Exception:
            pass                      # 状态存不上不能影响交易
    fuse["save"] = _save_fuse

    try:
        with open(state_path, "r", encoding="utf-8") as f:
            st = json.load(f)
        fuse["peak"] = max(INIT, float(st.get("peak") or INIT))
        fuse["halted"] = bool(st.get("halted"))
    except Exception:
        pass                          # 无文件/损坏 = 全新状态
    if fuse["halted"]:
        _popup("MA13 模拟盘：回撤熔断未复位",
               "上一次运行触发了回撤熔断，本次进程将以【只出不进】模式运行"
               "（已有持仓照常出场，禁止一切新开仓）。\n\n"
               f"历史峰值: {fuse['peak']:.0f}\n\n"
               "确认复位方法：\n"
               "  1. 关闭本程序窗口\n"
               "  2. 删除文件 策略\\06_MA13超短线\\模拟盘\\台账\\熔断状态.json\n"
               "  3. 重新双击 启动_P_EB_观察模式.bat")
        for s in specs:
            s["rec"].event("CIRCUIT", "启动时检测到未复位的熔断闩锁 → 只出不进模式")

    api = _connect(user, pwd, specs, web_gui)
    acc_box = {"acc": None}               # 当前会话的账户对象（TqKq 云端账户，随重连刷新）
    acc_box["acc"] = api.get_account()

    # ★★ 换池保护：把「云端有仓但不在本次池内」的品种补订阅接管（只出不进）。
    #   卡C 每 6 天重选一次池子，被换出的腿若还持仓，不补订阅就会永久裸仓。
    _adopt_carried_legs(api, specs, shared, record_dir, sizer, heartbeat_sec)

    # ---- 熔断演练（--drill-fuse）：强制触发一次，走真实触发的那条代码路径 ----
    # 用途：在没有真实回撤的情况下演练「弹窗 → 闩死 → 只出不进 → 复位三步」。
    # 演练同样会闩死并落盘，复位同样要人工删 熔断状态.json —— 这是刻意保持一致。
    if drill_fuse:
        print("\n" + "=" * 62, flush=True)
        print("  熔断演练模式（--drill-fuse）：将立即模拟一次回撤熔断", flush=True)
        print("  预期：弹窗 → 台账 CIRCUIT 行 → 闩死(拒绝开新仓) → 需人工复位")
        print("=" * 62 + "\n", flush=True)
        try:
            _bal0 = float(acc_box["acc"]["balance"])
        except Exception:
            _bal0 = INIT
        # 用真实峰值触发：喂一个**明确低于熔断线**的余额
        # ★ 2026-10-08 切 TqKq 后基准改用**账户真实余额**（云端约 100 万），
        #   演练的 peak 与熔断线都随之抬高，演练语义不变（仍走真实触发那条路径）。
        #   保留 INIT 兜底：账户余额读不到时用 INIT，此时 peak=INIT、熔断线=16万。
        fuse["peak"] = _bal0 if (_bal0 == _bal0 and _bal0 > 0) else INIT
        _trigger_bal = fuse["peak"] * (1 - DD_FUSE) * 0.9
        if _bal0 > 0 and _bal0 < _trigger_bal:
            _trigger_bal = _bal0 * 0.9         # 权益已低于阈值时按权益折算
        _fuse_eval(_trigger_bal, fuse, specs[0]["rec"], specs[0]["name"], drill=True)
        print(f"[演练] 喂入权益 {_trigger_bal:.0f} / 熔断线 "
              f"{fuse['peak'] * (1 - DD_FUSE):.0f} ⇒ 闩死状态 halted={fuse['halted']}；"
              f"新开仓已被拒绝，持仓照常按止损/时间止损出场。", flush=True)
        print(f"[演练] 复位方法：关闭窗口 → 删除 {os.path.basename(fuse['path'])}"
              f"（在 台账 目录）→ 重新启动。\n", flush=True)
    try:
        while True:                       # 守护外层循环：断线后自动重连
            try:
                while True:
                    # ★ 必须带 deadline（项目既有教训）：休市/ 无行情推送时
                    #   wait_update() 会**永久阻塞** —— 那会让整个主循环停摆，
                    #   云端持仓对账、心跳、出场判断全部不再执行（看起来像「程序
                    #   启动了却什么都没做」）。实测 13:25 午休时就是这个现象：
                    #   日志只有三行登录信息，_process 一次都没被调用。
                    #   加了 deadline 后即使无行情也会每 30 秒醒来一次，
                    #   对账与心跳照常跑（休市时能正确接管云端遗留持仓）。
                    api.wait_update(deadline=time.time() + 30)
                    for s in specs:
                        _process(api, s, allow_trade, shared, acc_box, fuse, ctx, sig)
            except KeyboardInterrupt:
                raise                     # 人工 Ctrl+C：正常退出，不重连
            except Exception as e:        # 断线/被踢/网络异常：记账后重连
                msg = f"{type(e).__name__}: {e}"
                print(f"[守护] 连接异常，{RECONNECT_WAIT}s 后自动重连：{msg}", flush=True)
                for s in specs:
                    s["rec"].event("RECONNECT", f"连接异常自动重连 | {msg[:120]}")
            finally:
                try:
                    api.close()
                except Exception:
                    pass
            time.sleep(RECONNECT_WAIT)
            api = _connect(user, pwd, specs, web_gui)
            acc_box["acc"] = api.get_account()
            # 重连后同样补接管：断线期间可能有人工/外部平仓或新开仓
            _adopt_carried_legs(api, specs, shared, record_dir, sizer, heartbeat_sec)
            for s in specs:
                s["skip_first"] = True    # 重连后首根 bar 只认账不处理（防重复信号）
                # ★ 重连后强制回到「未接管」状态：让云端对账重新走一遍。
                #   否则上一次恢复出来的持仓带着 recovered=True 跳过补挂止损，
                #   而重连后 s["stop"] 沿用的可能是旧会话的陈旧止损线。
                s["recovered"] = False
    except KeyboardInterrupt:
        pass
    finally:
        try:
            api.close()
        except Exception:
            pass
        for s in specs:
            s["rec"].stop("程序退出")


def _fuse_eval(bal, fuse, rec, name, drill=False):
    """账户级回撤熔断判定 + 触发动作。抽成函数是为了让 --drill-fuse 演练
    走**与真实触发完全同一条代码路径**（测的就是真跑的那条）。

    返回 True=本次刚好触发（边沿），False=未触发或已闩死。
    """
    if not (bal == bal) or bal <= 0:        # NaN / 非正不计
        return False
    if bal > fuse["peak"]:
        fuse["peak"] = bal
        if fuse.get("save"):
            fuse["save"]()                # 新峰值即落盘（峰值跨会话累计）
    if fuse["halted"] or bal > fuse["peak"] * (1 - DD_FUSE):
        return False
    fuse["halted"] = True
    if fuse.get("save"):
        fuse["save"]()                    # 闩锁立即落盘：重启也不自动复位
    tag = "【演练】" if drill else ""
    rec.event("CIRCUIT",
              f"{tag}回撤熔断触发：权益{bal:.0f} 自峰值{fuse['peak']:.0f} "
              f"回撤≥{DD_FUSE:.0%} → 停止新开仓(持仓照常出场) 恢复需人工重启")
    _popup("MA13 模拟盘：⚠️ 回撤熔断触发" + ("（演练）" if drill else ""),
           f"权益 {bal:.0f} 自峰值 {fuse['peak']:.0f} 回撤达到 {DD_FUSE:.0%}。\n\n"
           "已自动执行：停止一切新开仓；已有持仓照常按止损/时间止损出场。\n"
           "程序不会自动恢复，请尽快人工介入。\n\n"
           "复位方法（确认后）：\n"
           "  1. 关闭本程序窗口\n"
           "  2. 删除文件 策略\\06_MA13超短线\\模拟盘\\台账\\熔断状态.json\n"
           "  3. 重新双击 启动_P_EB_JM_真实下单.bat")
    print(f"[{name}][CIRCUIT] ⚠️ {tag}回撤熔断触发 权益{bal:.0f} / "
          f"峰值{fuse['peak']:.0f} → 停止新开仓，人工介入", flush=True)
    return True


# ============================================================
# ★★ 换月移交：本地记录的持仓合约持久化（2026-10-13 新增）
#   为什么需要持久化：主连换月时 q.underlying_symbol 会跳到新合约，旧持仓仍在
#   旧合约（云端不会自动迁移）。若进程在换月期间重启（守护/计划任务常见），
#   内存里的 s["underlying"] 丢失 ⇒ 旧合约持仓变孤儿（脱管裸奔）。
#   ⇒ 把「当前持仓在哪个合约」落盘成 underlying_{key}.json，重启后据此判断换月。
def _underlying_path(record_dir, key):
    return os.path.join(record_dir, f"underlying_{key}.json")


def _load_underlying(record_dir, key):
    """★ 2026-10-17：改为容错读 —— 半个 JSON / 空文件一律返回 None（旧版已如此），
    但改用 ops.read_json 统一，避免各处 json.load 各自处理。"""
    try:
        d = read_json(_underlying_path(record_dir, key), None) or {}
        return d.get("underlying") or None
    except Exception:
        return None


def _save_underlying(record_dir, key, sym):
    """★ 2026-10-17：改为**原子写**（tmp + os.replace）。
    旧版直接覆盖写 —— 进程在写盘瞬间被强杀会留下半个 JSON，重启后读到坏值。"""
    if not record_dir or not sym:
        return
    atomic_write_json(_underlying_path(record_dir, key),
                      {"underlying": sym,
                       "update": time.strftime("%Y-%m-%d %H:%M:%S")}, indent=None)


def _ops_sent(s, symbol, dir_cn, offset_cn, hands, ref_price, bar_time, key_suffix=""):
    """订单**意图**落三层账（ORDER_SENT）；返回幂等键，供成交回填时配对。
    永不抛异常（运维记账不允许打断交易）。
    key_suffix：上期所平仓拆成今/昨两张单时用于区分幂等键，否则两张单同键会串号。"""
    try:
        L = s.get("ledger")
        if L is None:
            return None
        key = f"{symbol}|{bar_time}|{dir_cn}{offset_cn}|{int(hands)}{key_suffix}"
        L.sent(key, symbol, dir_cn, offset_cn, hands, ref_price)
        return key
    except Exception:
        return None


def _same_side_notional(shared, side_sign):
    """账户级**同向**名义敞口（元）。side_sign=+1 多 / -1 空。

    数据来自 shared（各腿每拍更新纯标量，跨线程安全）。
    正常态参考：4 空腿约 1.28×权益、5 腿约 1.66×权益。"""
    total = 0.0
    for key, sh in (shared or {}).items():
        try:
            p = int(sh.get("pos") or 0)
            if p == 0 or (p > 0) != (side_sign > 0):
                continue
            px = float(sh.get("price") or 0)
            mult = float(config_symbols.SYMBOLS[key][2])
            if px > 0 and mult > 0:
                total += abs(p) * px * mult
        except Exception:
            continue
    return total


def _risk_check(s, acc, hand, shared=None, side_sign=1, price=None):
    """开仓前风控闸门（**只挡开仓，绝不挡平仓**）。异常一律放行。"""
    try:
        g = s.get("risk")
        if g is None:
            return True, ""
        bal = getattr(acc, "balance", None) if acc is not None else None
        mg = getattr(acc, "margin", None) if acc is not None else None
        ssn = _same_side_notional(shared, side_sign) if shared is not None else None
        on = None
        try:
            if price and hand:
                on = abs(float(hand)) * float(price) * float(s["mult"])
        except Exception:
            on = None
        return g.check_open(hand, balance=bal, margin=mg,
                            same_side_notional=ssn, order_notional=on)
    except Exception:
        return True, ""


def _find_orphan(api, s, cur_sym, exclude=None):
    """★ 历史孤儿仓检测（2026-10-13 收尾修复）：修复前开的旧合约持仓，磁盘上
    没有 underlying_{key}.json，启动后 s["underlying"]=None，对代码完全隐形——
    现有换月检测只在 `s["underlying"]!=None` 时生效，所以旧仓永远扫不到。

    本函数不靠猜旧合约代码，而是枚举账户**全部**持仓（api.get_position() 不传
    symbol 返回含所有持仓的 Entity），挑出「同品种不同月份、且非当前主连、且非
    exclude（本地正在跟踪的合约）」的非零仓位。返回旧合约代码或 None。

    ⚠️ exclude 必须传本地正在跟踪的合约：boot 接管会先把 s["underlying"] 落盘为
       当前主连（如 DCE.jd2612），若不排除它，正常持仓会被误判成孤儿。

    仅读取、不改任何状态；_sync_cloud_pos 每次对账都会调用（api.get_position()
    是本地内存查询，无网络开销），扫到并平仓移交后云端归零即不再命中。
    """
    if not cur_sym or "." not in cur_sym:
        return None
    try:
        all_pos = api.get_position()          # 不传 symbol = 账户全部持仓（Entity）
    except Exception:
        return None
    try:
        pos_map = {str(sym): int(getattr(p, "pos", 0) or 0) for sym, p in all_pos.items()}
    except Exception:
        return None
    # ★ 2026-10-17：选择逻辑抽到 lib/ops.find_orphan（纯函数，已入 CI 自检）。
    #   prefix 由 leg_prefix 计算（rstrip 数字），**不用** rsplit('.')（那是交易所级，会跨品种误伤）。
    return find_orphan(pos_map, cur_sym, exclude)


def _cloud_pos(api, s, sym=None):
    """读云端真实持仓（正=多/ 负=空）。读不到返回 None（调用方跳过同步）。

    ⚠️ 云端合约代码 = q.underlying_symbol（已由引擎在_quote_handler 里展开为
       具体月份，如 DCE.p2701）。用主连代码查会返回空持仓 —— 必须在订阅
       underlying_symbol 之后才有意义。sym 可显式指定（换月时查旧合约持仓）。
    """
    q = s.get("q")
    if q is None:
        return None
    if sym is None:
        sym = q.underlying_symbol
    if not sym:
        return None
    try:
        p = api.get_position(sym)
        return int(p["pos"])          # tqsdk 的pos 本身就是带符号净持仓
    except Exception:
        return None


# ★ 止损价改由 signals.Ma13Signal.stop_price 提供（单一真相源，见 signals.py）。
#   原 _stop_price 已删除，避免两份 MA13 逻辑漂移。


def _attach_stop_on_boot(s, pos, c, a, sig=None):
    """启动/重连后补挂止损（对齐 engine.run_live_boll 的 attach_stop_on_start）。

    云端**不存我们的止损线**（那是本策略自算的），所以恢复出的持仓必须重算一条。
    ⚠️ 算法必须与开仓处**逐字一致** ⇒ 统一走 signal.stop_price()，禁止在这里另写一份。
       swing 取近 SLOPE_N 根的极值，且**排除正在形成的当前 bar**（与 entry 的
       low[max(0,i-SLOPE_N):i] 同口径；取不到就用现价兜底）。

    这不是新增过滤条件，纯粹是「止损线丢了必须补回来」，否则持仓会裸奔。
    sig 缺省时回落到模块级 _SIG（MA13），保证旧调用零影响。
    """
    if pos == 0 or a != a or a <= 0:
        return
    sig = sig or _SIG
    c = float(s["q"].last_price or c)
    # 近SLOPE_N 根的极值，**排除当前正在形成的最后一根**（与 entry 口径一致）
    try:
        k = s["k"]
        nk = len(k)
        lo = float(np.min(np.array(k["low"][max(0, nk - 1 - SLOPE_N): nk - 1], dtype=float)))
        hi = float(np.max(np.array(k["high"][max(0, nk - 1 - SLOPE_N): nk - 1], dtype=float)))
        if lo != lo or lo <= 0:
            lo = c
        if hi != hi or hi <= 0:
            hi = c
    except Exception:
        lo = hi = c
    stop = sig.stop_price(1 if pos > 0 else -1, c, a,
                          lo if pos > 0 else hi)
    s["stop"] = stop
    s["entry"] = c
    s["bars_in"] = 0
    s["recovered"] = True
    # ★★ 接管当根禁止判出场（理由见 _process 里的同名字段注释）：
    #   止损是用前 5 根 swing 现算的，不能拿「已经走完的当根 high/low」反证它被击穿。
    s["_skip_exit_once"] = True
    print(f"[{s['name']}][恢复] 检测到云端已有持仓 {pos} 手 → "
          f"以云端为准接管，止损补挂至≈{stop:.1f}(ATR={a:.2f})", flush=True)
    s["rec"].event("RECOVER",
                   f"⚠️ 云端存在持仓 {pos} 手，本进程原以为空仓 → 已接管并补挂止损"
                   f"≈{stop:.1f}（ATR={a:.2f}，算法与开仓一致）。若这是意外持仓请人工核对。")
    # ★★ 换月移交：接管时同步「持仓在哪个合约」，并落盘（重启后据此判换月）
    s["underlying"] = s["q"].underlying_symbol
    _save_underlying(s.get("record_dir"), s["key"], s["underlying"])


# ★ 当前 ATR 改由 signals.Ma13Signal.atr_at 提供（单一真相源，见 signals.py）。
#   原 _now_atr 已删除，避免第三份 ATR 公式。恢复挂单处调用 _SIG.atr_at(k)。


def _cloud_fill_stats(api, s):
    """★ 只读统计（2026-10-09 新增，规则 2 安全：不改信号/不改参数/不改手数）。

    目的：让台账里的价格**从「我打算以什么价成交」变成「云端实际以什么价成交」**。

    为什么必须读云端（不是自己记）：
      台账 FILL 行记的是下单那一刻的 last_price（`_lp`），**那是报价不是成交价**。
      实测两者的差就是滑价——昨晚 9 笔里有 5 笔实际平仓价差于止损价，
      最差一笔超计划风险 2.2 倍。这项差异**只能用云端的真实数据测量**。

    读的是 Position 实体的字段（源码 objs.py:359 `class Position`）：
      · position_price_long / _short  → **云端持仓均价（含真实成交价）**
      · float_profit_long / _short    → **云端算的浮动盈亏**
      · pos_long_his/_today 等        → 老仓/今仓手数
    取不到（未就绪 / NaN / 异常）一律静默跳过，绝不影响交易主流程。

    写入 s 的键（只用于统计，不参与任何判定）：
      s["last_fill_price"]  最近一次读到的云端持仓均价
      s["peak_favorable"]    持仓期间出现过的最大浮盈（元，正数）
      s["peak_adverse"]      持仓期间出现过的最大浮亏（元，正数）
      s["last_cloud_pos"]    上次读到的云端持仓（0=空仓）
    """
    rec = s.get("rec")
    if rec is None:
        return
    try:
        pos = api.get_position(s["q"].underlying_symbol)
    except Exception:
        return
    try:
        n_l = int(getattr(pos, "pos_long_his", 0) or 0) + \
              int(getattr(pos, "pos_long_today", 0) or 0)
        n_s = int(getattr(pos, "pos_short_his", 0) or 0) + \
              int(getattr(pos, "pos_short_today", 0) or 0)
        net = n_l - n_s
        px_l = float(getattr(pos, "position_price_long", float("nan")))
        px_s = float(getattr(pos, "position_price_short", float("nan")))
        fp_l = float(getattr(pos, "float_profit_long", float("nan")))
        fp_s = float(getattr(pos, "float_profit_short", float("nan")))
    except Exception:
        return

    prev = s.get("last_cloud_pos", None)
    avg = px_l if (n_l > 0 and px_l == px_l) else (px_s if (n_s > 0 and px_s == px_s) else None)
    if avg:
        s["last_fill_price"] = float(avg)

    # 浮盈/浮亏极值：只记「本次持仓期间」的极值，持仓归零时清空
    fp = fp_l if (n_l > 0 and fp_l == fp_l) else (fp_s if (n_s > 0 and fp_s == fp_s) else None)
    if fp is not None and net != 0:
        if s.get("peak_favorable") is None:
            s["peak_favorable"] = fp
            s["peak_adverse"] = fp
        else:
            s["peak_favorable"] = max(s["peak_favorable"], fp)
            s["peak_adverse"] = min(s["peak_adverse"], fp)
    if net == 0 and prev != 0:
        # 刚清仓：把这一笔的极值落一条 MFE/MAE 台账行（分析工具靠它算「对了又被扫」）
        pf = s.get("peak_favorable")
        pa = s.get("peak_adverse")
        if pf is not None or pa is not None:
            rec.event("EXCURSION",
                      f"持仓期间  最大浮盈={pf if pf is not None else float('nan'):.2f} 元"
                      f"  最大浮亏={pa if pa is not None else float('nan'):.2f} 元")
        s["peak_favorable"] = None
        s["peak_adverse"] = None
    s["last_cloud_pos"] = net

    # ---- 回填真实成交价：等撮合窗口过了再读云端（详见挂单处的注释）----
    pend = s.get("pending_fill")
    if pend:
        now = time.time()
        # 行情是否还在走（休市/断流时不撤单不重报，避免白烧撤单额度）
        _mkt_live = not _rs.quote_stale(s.get("q"), now)
        _bg = _rs_budget(s.get("record_dir"))
        keep = []
        for p in pend:
            if now - float(p["ts"]) < FIX_WAIT_SEC:
                keep.append(p)              # 还没到窗口，下个节拍再看
                continue
            ref = float(p.get("ref_price") or 0)
            # ★★ 2026-10-18 修（P0，实测事故）：**必须核对持仓真的变了**才算成交。
            #   原来只判「云端有均价」⇒ 只要仓位还在（未成交！），avg 就一直存在，
            #   于是把一笔**根本没成交**的平仓单记成 FILL_CONFIRMED。
            #   实测：鸡蛋 22:02:34 报平仓单（休市，未成交），22:03:05 就被判 CONFIRMED，
            #   而云端持仓始终 -8 ⇒ 台账写了假的平仓，本地 pos 归零、状态与云端脱钩。
            #   expect_net 是下单时按「当前净仓 + 带符号手数」算出的目标净仓。
            # ★★ 2026-10-20 修（P1-1，今日实测 4/4）：反手同拍「平旧+开新」时，
            #   云端净仓是**整批**的结果（0 或 ±8），单张平仓单的 expect_net=0 必然对不上
            #   ⇒ 稳定误报「持仓未变化⇒判为未成交」（豆粕 09:35/14:50、沥青 09:45、eg 13:30）。
            #   改为与**同批最后一张单**的累计目标比，见 lib/live_resend.batch_expect。
            #   对"平腿真没成交"仍然有效：此时净仓会变成 ±16 ≠ ±8，照样报未成交。
            expect = _rs.batch_expect(p, pend)
            filled = (expect is None) or (int(net) == int(expect))
            # ★★ 2026-10-19 新增：未成交时必须**带上委托的真实状态**。
            #   原实现只写"持仓未变化"，交易所的拒单文本被彻底丢掉 ——
            #   10-19 FU 平今废单（offset 用了 CLOSE，昨仓为 0）只能靠横向对比
            #   + 读 tqsdk 文档才定位出来，代价是一整天的仓位裸奔。
            _ord = p.get("order")
            _st = str(getattr(_ord, "status", "") or "") if _ord is not None else ""
            _msg = str(getattr(_ord, "last_msg", "") or "") if _ord is not None else ""
            _dead = bool(getattr(_ord, "is_dead", False)) if _ord is not None else False
            _off = str(p.get("offset") or "")
            # 判定为「拒单/废单」：委托已终结(is_dead) 且带回拒单文本
            # （普通"未撮合/休市排队"的单 is_dead=False，仍记 ORDER_UNCONFIRMED）
            _rej = bool(_dead and _msg)
            # ---- ★★ P0-1 撤单重报（2026-10-20）：未成交先按处置策略走，------
            #   不允许"可能还挂着"就写一句未成交了事（那正是裸奔的来源）。
            #   giveup 才落到下面的原记账路径（废单/已重报过/额度用尽/休市）。
            if not filled:
                _act, _note = _rs.decide(p, now, _bg, market_live=_mkt_live)
                if _act == "wait":
                    keep.append(p)
                    continue
                if _act == "cancel":
                    try:
                        api.cancel_order(_ord)          # 下次 wait_update 生效
                        _used = _bg.add(str(p.get("symbol") or "").split(".")[0])
                        p["rs"]["cancel_ts"] = now
                        rec.event("RSCANCEL",
                                  f"{p['dir']}{p['side']} {p['hands']}手 {p.get('symbol') or '?'} "
                                  f"撤单重报：{_note} | 今日该所撤单 {_used} 笔")
                        print(f"[{s['name']}][RSCANCEL] {_note}", flush=True)
                    except Exception as _e:             # noqa: BLE001
                        # ★ 撤单被风控拒会抛 TqRiskRuleError（risk_manager.py:49）；
                        #   这里**绝不能**让异常冒泡——10-19 市价单异常就是靠异常把连接
                        #   炸掉、每 10 分钟重连一次。撤不掉就放弃，交原路径记未成交。
                        rec.event("RSCANCEL",
                                  f"{p['dir']}{p['side']} {p['hands']}手 撤单失败："
                                  f"{type(_e).__name__}: {_e}")
                        print(f"[{s['name']}][RSCANCEL] 撤单失败 {_e}", flush=True)
                        _act = "giveup"
                    if _act != "giveup":
                        keep.append(p)                  # 下拍看撤单是否生效
                        continue
                elif _act == "resend":
                    _n = int(getattr(_ord, "volume_left", 0) or 0)
                    _sym2 = str(p.get("symbol") or "")
                    _d2 = (("BUY" if p["dir"] == "多" else "SELL") if p["side"] == "开"
                           else ("SELL" if p["dir"] == "多" else "BUY"))
                    _off2 = str(p.get("offset") or "CLOSE")
                    _bt = f"{p.get('bar_time') or ''}#re{int(now)}"
                    try:
                        _ok2, _note2, _o2 = _order(api, s, _sym2, _d2, _off2, _n)
                    except Exception as _e:             # noqa: BLE001
                        _ok2, _note2, _o2 = False, f"{type(_e).__name__}: {_e}", None
                    if _ok2 and _n > 0:
                        s["last_order_ts"] = time.time()
                        _k2 = _ops_sent(s, _sym2, p["dir"], p["side"], _n,
                                        float(p.get("ref_price") or 0), _bt,
                                        key_suffix="#re")
                        # ★ 不再写 FILL 行：FILL 是"下单意图"统计，重报同一笔会虚增成交量
                        keep.append({"ts": s["last_order_ts"], "side": p["side"],
                                     "dir": p["dir"], "hands": _n,
                                     "ref_price": float(p.get("ref_price") or 0),
                                     "key": _k2, "expect_net": _rs.resend_target(p, _n),
                                     "order": _o2, "offset": _off2,
                                     "symbol": _sym2, "bar_time": _bt,
                                     "rs": {"done": True}})   # 只重报一次
                        rec.event("RSEND",
                                  f"{p['dir']}{p['side']} {_n}手 {_sym2} 重报 "
                                  f"({_note2}) | 原委托 {p.get('key') or '?'} | {_note}")
                        print(f"[{s['name']}][RSEND] {p['dir']}{p['side']} {_n}手 "
                              f"{_sym2} {_note2}", flush=True)
                    else:
                        rec.event("RSEND", f"{p['dir']}{p['side']} {_n}手 {_sym2} "
                                           f"重报未报出：{_note2}")
                    # 原本单已终结（撤成功），无论重报是否成功都不再保留
                    continue
            if not filled:
                rec.event("FILLX",
                          f"{p['dir']}{p['side']} {p['hands']}手 决策价={ref:.2f} "
                          f"**持仓未变化（云端净仓 {net}，应为 {expect}）⇒ "
                          f"{'委托被拒/废单' if _rej else '判为未成交'}**"
                          f" | offset={_off or '?'} 状态={_st or '?'} "
                          f"回报={_msg or '无'}")
            elif avg and ref > 0:
                # 滑点 = 决策价 vs 云端成交均价（换算成元，正=更不利方向）
                # 乘数从 config_symbols 取（s 里没存 mult，它只活在 _hand_of 的闭包里）
                try:
                    _t = config_symbols.SYMBOLS[s["key"]]
                    mult = float(_t[2])
                except Exception:
                    mult = 0.0
                slip_yuan = (avg - ref) * (1 if p["dir"] == "多" else -1) * \
                            (mult or 1) * p["hands"]
                rec.event("FILLX",
                          f"{p['dir']}{p['side']} {p['hands']}手 "
                          f"决策价={ref:.2f} 云端成交均价={avg:.2f} "
                          f"滑点≈{slip_yuan:+.2f}元")
            else:
                rec.event("FILLX", f"{p['dir']}{p['side']} {p['hands']}手 "
                                   f"决策价={ref:.2f} 云端均价未就绪（未读到）")
            # ★ 三层账收口（2026-10-17，2026-10-18 加持仓核对）：
            #   ① 持仓没变 ⇒ ORDER_UNCONFIRMED（休市/被拒/未撮合），**绝不记 CONFIRMED**
            #   ② 持仓变了但读不到均价 ⇒ 也算 CONFIRMED（成交为事实，只是价缺）
            #   注意：既有 FILL 行仍然只是"下单意图"，**统计与对账只认 CONFIRMED**。
            try:
                _L = s.get("ledger")
                _k = p.get("key")
                if _L is not None and _k:
                    if not filled:
                        if _rej:
                            # 有明确拒单文本 ⇒ 单独一类，便于 zombies() 与人工核对区分
                            _L.rejected(
                                _k, f"offset={_off or '?'} 状态={_st} 回报={_msg}")
                        else:
                            _L.unconfirmed(
                                _k, f"持仓未变化（云端净仓 {net}，应为 {expect}）⇒ 未成交")
                    elif avg and ref > 0:
                        _L.confirmed(_k, avg, p["hands"])
                    else:
                        _L.unconfirmed(_k, "已成交但云端均价未就绪（读不到）")
            except Exception:
                pass
        s["pending_fill"] = keep


# ================================================================
# ★★ 统一下单入口（2026-10-19 新增，修「上期所不收市价单」）
# ================================================================
# 事故：2026-10-19 09:00 起 FU/BU 每次下单抛
#   「SHFE.bu2611 不支持市价单，请使用 limit_price 参数指定价格。
#     中金所、上期所、原油交易所、上交所、深交所不支持市价单。」
# 触发 RECONNECT（每 10 分钟一次），FU/BU 当天 0 成交。
# 根因：新池（S2）首次引入上期所品种；旧五腿 JM/M/JD/P/SR 全是 DCE/CZCE，从没暴露。
# 修法（单变量、不碰信号）：仅对「不支持市价单」的交易所改用**让价限价单**，
#   BUY 上让 LIMIT_TICKS 跳、SELL 下让 LIMIT_TICKS 跳（等价吃对手价），其余交易所
#   仍走市价单 —— DCE/CZCE 的行为逐字不变。
NO_MARKET_EXCH = {"SHFE", "INE", "CFFEX"}
LIMIT_TICKS = 2          # 让价跳数：1 跳可能排队不成交，2 跳基本立即成交（≈多付 1 跳）


def _tick_of(key):
    """品种一跳（config_symbols 第 4 位）。取不到返回 0 ⇒ 上层拒绝报单。"""
    try:
        return float(config_symbols.get(str(key).upper())[3])
    except Exception:
        return 0.0


def _px_dec(tick: float) -> int:
    """按一跳精度推价格小数位，避免浮点尾巴被交易所拒单（如 3202.0000001）。"""
    if tick <= 0:
        return 2
    txt = f"{tick:.10f}".rstrip("0")
    return len(txt.split(".")[1]) if "." in txt else 0


def _ref_px(api, s, symbol):
    """报单参考价：优先本品种已订阅的 quote，其次临时取一次。取不到返回 None。"""
    try:
        q = s.get("q") if isinstance(s, dict) else None
        if q is not None and getattr(q, "underlying_symbol", None) == symbol:
            p = q.last_price
            if p and math.isfinite(p) and p > 0:
                return float(p)
    except Exception:
        pass
    try:
        p = api.get_quote(symbol).last_price
        if p and math.isfinite(p) and p > 0:
            return float(p)
    except Exception:
        pass
    return None


# ★★ 上期所/上期能源**分平今平昨**（2026-10-19 实盘事故）
#   tqsdk/api.py:1201 官方原文：
#     "offset: OPEN / CLOSE / CLOSETODAY (上期所和上期能源分平今/平昨,
#      平今用 CLOSETODAY, 平昨用 CLOSE; 其他交易所直接用 CLOSE)"
#   事故：FU 10:44 当天开空 6 手（pos_short_today=6, his=0），三处平仓一律发
#   "CLOSE" = 报"平昨 6 手"、昨仓为 0 ⇒ 交易所**废单**。而 insert_order 对废单
#   不抛异常（异步回报），代码照常写 FILL ⇒ 14:04/14:55 两次平仓全落空，
#   仓位裸奔到收盘，台账只能写一句含糊的"持仓未变化⇒未成交"。
#   新池 S2 于 10-19 首次引入上期所品种，这条路径历史台账里从未走过。
CLOSE_SPLIT_EXCH = {"SHFE", "INE"}


def _close_plan(api, symbol, direction, volume):
    """平仓拆单计划 [(offset, vol), ...]。

    非上期所/能源 ⇒ [("CLOSE", volume)]，行为与改动前**逐字相同**。
    上期所/能源 ⇒ 按云端「今仓/昨仓」拆：先 CLOSETODAY（今仓），余量才 CLOSE（昨仓）。
    读不到持仓时（查询滞后）先按"当日开当日平"试 CLOSETODAY，下一拍会再判 ——
    **绝不同时发平今+平昨**，那会稳定制造一张废单。
    """
    v_all = int(volume)
    if v_all <= 0:
        return []
    IF = str(symbol).split(".")[0].upper()
    if IF not in CLOSE_SPLIT_EXCH:
        return [("CLOSE", v_all)]
    try:
        p = api.get_position(symbol)
        if str(direction).upper() == "BUY":        # 平空
            t, h = int(p.pos_short_today or 0), int(p.pos_short_his or 0)
        else:                                      # 平多
            t, h = int(p.pos_long_today or 0), int(p.pos_long_his or 0)
    except Exception:
        t, h = 0, 0
    if t + h <= 0:
        return [("CLOSETODAY", v_all)]
    out, v_today = [], min(v_all, t)
    if v_today > 0:
        out.append(("CLOSETODAY", v_today))
    if v_all - v_today > 0:
        out.append(("CLOSE", v_all - v_today))
    return out


def _order(api, s, symbol, direction, offset, volume, ref_price=None):
    """统一下单入口。返回 (ok, note, order)。

    ok=False 表示**没有发出任何委托**（缺参考价/一跳），调用方必须跳过记账，
    下拍再试 —— 绝不能像以前那样先写 FILL 再发现单据根本没到交易所。
    order = tqsdk 的 Order 对象（2026-10-19 新增）。原实现把它直接丢弃，
    于是废单只能靠 30 秒后"持仓没变"倒推成一句"未成交"，**原因不可见**；
    现在挂进 pending_fill，确认阶段读 status / last_msg / is_dead 落台账。
    """
    exch = str(symbol).split(".")[0].upper()
    if exch not in NO_MARKET_EXCH:
        _o = api.insert_order(symbol, direction, offset, volume)      # 原行为，零改动
        return True, "market", _o
    tick = _tick_of(s.get("key") if isinstance(s, dict) else None)
    px = ref_price
    if px is None:
        px = _ref_px(api, s, symbol)
    try:
        px = float(px)
        if not math.isfinite(px) or px <= 0:
            px = None
    except Exception:
        px = None
    if px is None or tick <= 0:
        return False, f"{exch} 需限价单但参考价/一跳缺失（px={px}, tick={tick}）", None
    sign = 1 if str(direction).upper() == "BUY" else -1
    limit = round(px + sign * LIMIT_TICKS * tick, _px_dec(tick))
    _o = api.insert_order(symbol, direction, offset, volume, limit_price=limit)
    return True, f"limit={limit}", _o


def _send_close(api, s, symbol, side, dir_cn, hands, ref_price, bar_time, pos_before):
    """发平仓单：上期所/能源按今昨仓自动拆成 1~2 张，其余交易所仍是 1 张 CLOSE。

    记账口径与改动前一致：每张单各写 FILL（=下单意图）/ ORDER_SENT / pending_fill。
    真实成交仍由 FILLX 核对云端持仓后确认，**FILL 不是成交凭证**。
    """
    plan = _close_plan(api, symbol, side, hands)
    if not plan:
        return False, f"平仓手数非法（hands={hands}）", []
    rec = s["rec"]
    sign = 1 if int(pos_before) > 0 else -1      # 平多：净仓减少；平空：净仓增加
    cum, sent = 0, []
    for i, (off, vol) in enumerate(plan):
        ok, note, o = _order(api, s, symbol, side, off, vol, ref_price=ref_price)
        if not ok:
            return False, note, sent
        s["last_order_ts"] = time.time()          # 等撮合窗口，防自平仓被误接管
        cum += int(vol)
        expect = int(pos_before) - sign * cum     # 每张单各自的目标净仓
        suffix = "" if len(plan) == 1 else f"#{i + 1}{'今' if off == 'CLOSETODAY' else '昨'}"
        rec.fill(dir_cn, "平", vol, float(ref_price), symbol)
        _k = _ops_sent(s, symbol, dir_cn, "平", vol, float(ref_price), bar_time,
                       key_suffix=suffix)
        s.setdefault("pending_fill", []).append(
            {"ts": s["last_order_ts"], "side": "平", "dir": dir_cn,
             "hands": int(vol), "ref_price": float(ref_price), "key": _k,
             "expect_net": expect, "order": o, "offset": off,
             # ★ 2026-10-20 撤单重报需要：合约（撤/重报都要按合约+交易所）与 bar_time（重报单的幂等键）
             "symbol": symbol, "bar_time": bar_time})
        sent.append((off, int(vol)))
    return True, ("split" if len(plan) > 1 else plan[0][0]), sent


def _sync_cloud_pos(api, s, allow_trade, sig=None):
    """★ 云端持仓对账（切 TqKq 后的核心保护，见常量区SYNC_CLOUD 注释）+ ★★换月移交。

    以**云端为唯一真相源**，纠正进程内存里的 s["pos"]。只在不一致时动手：
      ① 云端有仓、本进程以为空仓 ⇒ 接管 + 补挂止损（否则持仓裸奔 / 可能重复开仓）
      ② 云端空仓、本进程以为有仓 ⇒ 清零（止损线是本进程算的，云端已平就不该再管）

    ★★ 换月移交（2026-10-13 新增，修「鸡蛋 16 手 = 双合约并存 + 旧仓脱管」缺口）：
      主连换月时 q.underlying_symbol 跳到新合约（JD2611→JD2612），旧持仓仍在旧合约、
      云端不自动迁移。若只按 q.underlying_symbol 对账，旧仓被判「无持仓」→ 脱管裸奔。
      ⇒ 检测「本地记录的持仓合约 s["underlying"] ≠ 当前主连 cur_sym」：
        旧仓非空 ⇒ 先平掉旧合约持仓（换月移交），再把本地跟踪切到新合约；
        旧仓已空 ⇒ 直接切到新合约。
      s["underlying"] 持久化到 underlying_{key}.json，覆盖「换月期间进程重启」的孤儿仓场景。

    ⚠️ **必须放在「新 bar 判断之前」调用**（第一版就栽在这：写在 bar 判断之后，
    而 _process 开头有 `if bar["id"] == s["last_id"]: return` 的提前返回，
    于是休市/无新 bar 时这段代码永远走不到，接管功能形同虚设——真跑验证时
    云端明明有 1 手持仓却毫无反应，是靠这次真跑才发现的）。

    不产生任何新的开仓意图，只同步「持仓事实」这一项（规则2：不加条件、不择优）。
    """
    if not SYNC_CLOUD:
        return
    # ★★ 等待撮合窗口：本进程刚下过单时，云端余额还没更新（撮合有几秒延迟）。
    #   此刻读到的云端持仓必然「与 s["pos"] 不一致」，若照下面的逻辑就会把
    #   **自己刚开的仓**误判成「外部遗留持仓」并再接管一次——实测 13:37:17 开仓、
    #   13:37:18 就打出 RECOVER「云端存在持仓 4 手，本进程原以为空仓」（假接管）。
    #   后果：止损被用当时的 ATR 覆盖重算（这次恰好同值，但一般会变）＋
    #   台账多出一条假 RECOVER 污染对账。
    #   ⇒ 下单后 FIX_WAIT 秒内不做对账，等云端自己跟上。
    if time.time() - float(s.get("last_order_ts") or 0.0) < FIX_WAIT_SEC:
        return
    q = s.get("q")
    if q is None:
        return
    cur_sym = q.underlying_symbol
    if not cur_sym:
        return
    rec = s["rec"]

    # ---- ★★ 换月检测 + 孤儿扫描 ----
    #   孤儿扫描**不只**在 s["underlying"] 为 None 时跑（首版条件，实测有盲区）：
    #   boot 接管（_attach_stop_on_boot）会先把 s["underlying"] 落盘为当前主连
    #   合约（如 DCE.jd2612），此后 held_sym 永远非 None ⇒ 历史孤儿（DCE.jd2611）
    #   永远扫不到。⇒ 改为**每次对账都扫**，exclude 本地正在跟踪的合约
    #   （held_sym），保证正常持仓不被误判；api.get_position() 是本地内存查询，
    #   无网络开销。扫到即当作 held_sym，复用下方换月移交/平仓逻辑。
    held_sym = s.get("underlying")
    orphan_hit = False
    if not held_sym or held_sym == cur_sym:
        orphan = _find_orphan(api, s, cur_sym, exclude=held_sym)
        if orphan:
            orphan_hit = True
            rec.event("ORPHAN",
                      f"⚠️ 发现历史孤儿仓：账户在 {orphan} 持有非零仓位、"
                      f"但本进程无记录（修复前遗留）→ 将按换月逻辑移交/平仓")
            print(f"[{s['name']}][孤儿] 发现历史孤儿仓 {orphan}（修复前遗留），"
                  f"将按换月逻辑处理", flush=True)
            held_sym = orphan
        elif s.get("rollover_pending"):
            # 孤儿已平干净（扫描不命中）⇒ 解除防重闸门（旧 pending 指向的合约
            # 已不在账户里，留着只会挡住未来的正常换月报单）
            s["rollover_pending"] = None
            s["rollover_pending_hands"] = 0
            s["rollover_wait_logged"] = False
    if held_sym and held_sym != cur_sym:
        old_pos = _cloud_pos(api, s, held_sym)     # 读旧合约（非当前主连）的持仓
        if old_pos is None:
            if orphan_hit:
                return                              # 孤儿仓云端读不到 ⇒ 下拍再试，禁用本地仓位兜底
            old_pos = s["pos"]                      # 正常换月：云端读不到 ⇒ 兜底用内存记录
        if old_pos != 0:
            # ★★ 防重闸门：平仓单已报、云端仓位仍未归零（撮合窗口内/单被拒/
            #    非交易时段排队）⇒ 不重复报单，只等下拍复查——防「一次测试重复
            #    报约 50 单」事故在换月/孤儿路径重演。
            #    例外：部分成交（云端剩余手数 < 报单手数）⇒ 解除闸门，下面对剩余量补报。
            if s.get("rollover_pending") == held_sym:
                pend_hands = int(s.get("rollover_pending_hands") or 0)
                if abs(old_pos) >= pend_hands > 0:
                    if allow_trade and not s.get("rollover_wait_logged"):
                        rec.event("ROLLOVER_WAIT",
                                  f"{held_sym} 平仓单已报 {pend_hands} 手、云端仍有 "
                                  f"{abs(old_pos)} 手 → 等待撮合/开盘，不重复报单")
                        s["rollover_wait_logged"] = True
                    return
                s["rollover_pending"] = None          # 部分成交 ⇒ 允许补报剩余
            # 平多=SELL / 平空=BUY（与 _process 出场方向一致）
            direction = "SELL" if old_pos > 0 else "BUY"
            if allow_trade:
                # 2026-10-19：旧合约若是**当日开**的（上期所/能源），换月移交同样要平今
                for _off, _vol in _close_plan(api, held_sym, direction, abs(old_pos)):
                    _ok_o, _note_o, _o = _order(api, s, held_sym, direction, _off, _vol)
                    if not _ok_o:
                        # 拿不到参考价 ⇒ 本拍不报单，下拍再试（不能既报不出去又改本地状态）
                        rec.event("ORDERERR", f"{held_sym} 换月平仓未报出：{_note_o}")
                        return
                s["last_order_ts"] = time.time()    # 等撮合窗口，防自平仓被误接管
                s["rollover_pending"] = held_sym    # 防重：归零前不再报
                s["rollover_pending_hands"] = abs(old_pos)
                s["rollover_wait_logged"] = False
                # ★ 换月移交平仓**不记 FILL 行**：held_sym 是旧合约、q.last_price 是本品种现价，
                # 记上去会串品种价格污染台账（本事件 10-15 首次重启即误平 SR701，4 进程各发一笔、
                # 1 成交 3 拒即源于此）。真实成交价由下方 ROLLOVER event 描述 + _cloud_fill_stats 浮盈亏反映。
                pass
                rec.event("ROLLOVER",
                          f"⚠️ 检测到主连换月：本地持仓合约 {held_sym} → 当前主连 {cur_sym}，"
                          f"旧持仓 {old_pos} 手已在 {held_sym} 平仓（换月移交，避免脱管裸奔）")
                print(f"[{s['name']}][换月] {held_sym} → {cur_sym}："
                      f"旧仓 {old_pos} 手已平仓移交", flush=True)
                # 已平仓：本地跟踪切到新合约，继续下方正常同步
                s["pos"] = 0; s["entry"] = 0.0; s["stop"] = 0.0; s["bars_in"] = 0
                s["underlying"] = cur_sym
                _save_underlying(s.get("record_dir"), s["key"], cur_sym)
            else:
                # 观察模式：不报单，但旧仓仍计入本地跟踪 + 只告警一次（防刷屏）
                if not s.get("rollover_logged"):
                    rec.event("ROLLOVER",
                              f"⚠️ 主连换月 {held_sym}→{cur_sym}：旧持仓 {old_pos} 手仍在 {held_sym}，"
                              f"【观察模式不自动平仓，请人工处理或切交易模式】")
                    s["rollover_logged"] = True
                s["pos"] = old_pos                   # 继续跟踪旧合约，等切交易模式再平
                s["underlying"] = held_sym           # 记下旧合约，避免每帧重扫孤儿
                s["recovered"] = True
                return
        else:
            # 旧合约已无持仓（已平/已到期）：直接切到新合约
            if s.get("rollover_logged"):
                s["rollover_logged"] = False
            if s.get("rollover_pending") == held_sym:
                s["rollover_pending"] = None        # 平仓已确认归零，解除防重闸门
                s["rollover_pending_hands"] = 0
            s["pos"] = 0; s["entry"] = 0.0; s["stop"] = 0.0; s["bars_in"] = 0
            s["underlying"] = cur_sym
            _save_underlying(s.get("record_dir"), s["key"], cur_sym)

    # ---- 正常云端对账（基于当前主连 cur_sym）----
    cp = _cloud_pos(api, s, cur_sym)
    if cp is None:
        return# 读不到云端持仓（尚未订阅 / 连接未就绪）⇒ 本次不动，静默跳过
    if cp == s["pos"]:
        # 已一致。若是上一轮接管的持仓，本根起按正常持仓维护（止损已挂好）。
        if cp != 0 and s.get("recovered"):
            s["recovered"] = False
        s["underlying"] = cur_sym
        _save_underlying(s.get("record_dir"), s["key"], cur_sym)
        return
    if cp != 0:
        # 接管：云端有仓而本进程不知道 ⇒ 补挂止损（云端不存我们的止损线）
        c_now = float(q.last_price or 0)
        a_now = s.get("last_atr") or 0.0
        # ★ 若last_atr 还是 0（进程刚起、ATR 尚未算出），就地算一次。
        #   否则 _attach_stop_on_boot 会在 `a <= 0` 处早退⇒ **接管了持仓却没挂
        #   止损**，等于白接管（实测踩到：云端 1 手已接管，止损仍为 0）。
        if not (a_now == a_now and a_now > 0):
            a_now = (sig or _SIG).atr_at(s["k"])
            if a_now == a_now and a_now > 0:
                s["last_atr"] = a_now
        _attach_stop_on_boot(s, cp, c_now, a_now, sig=sig)
        s["underlying"] = cur_sym
        _save_underlying(s.get("record_dir"), s["key"], cur_sym)
    else:
        # 退出：被恢复出来的持仓已在云端消失（可能人工平仓/被其他程序平掉）
        rec.event("SYNC", "云端持仓已清零（可能人工平仓）→ 本进程同步退出该持仓，"
                          "并撤销其止损监控")
        print(f"[{s['name']}][同步] 云端持仓已清零 → 同步退出，撤销止损监控", flush=True)
        s["entry"] = 0.0; s["stop"] = 0.0; s["bars_in"] = 0
        s["underlying"] = cur_sym
        _save_underlying(s.get("record_dir"), s["key"], cur_sym)
    s["pos"] = cp
    s["recovered"] = False


def _process(api, s, allow_trade, shared, acc_box, fuse, ctx=None, sig=None):
    """单品种的一个处理节拍：新 bar 出现（=前一根收盘）才算信号。"""
    sig = sig or _SIG   # 默认 MA13 占位；ctx 提供其他因子时以 ctx 为准
    q, k, rec = s["q"], s["k"], s["rec"]
    sh = shared[s["key"]]
    if len(k) == 0:
        return

    # ---- 工作台集成：时段闸门（ctx=None 时完全不启用，保持原行为）----
    _sid = "closed"
    if ctx is not None:
        _sid = _workbench_sid(q)
        _workbench_gate_tick(ctx, s, _sid, q)

    # ---- 账户级回撤熔断（纯保护结构，非策略参数）----
    # 观察/交易模式都检查。权益 = 账户真实余额（★ 2026-10-08 切 TqKq 云端账户后
    # 约 100 万，熔断管的是**整个账户**的回撤，故用真实余额而非 20 万手数基数）。
    # 触发后闩死：停新开仓、持仓照常按止损/时间止损出场，不自动恢复，必须人工重启进程。
    acc = acc_box["acc"]
    if acc is not None:
        try:
            bal = float(acc["balance"])
        except Exception:
            bal = float("nan")
        if bal == bal and bal > 0:            # 非 NaN 且为正才计入
            _fuse_eval(bal, fuse, rec, s["name"])
            # 权益快照（60秒一次，独立于熔断）：首日对账要靠它核对「台账盈亏 vs 账户权益」
            _now = time.time()
            if _now - s.get("eq_ts", 0) >= 60:
                s["eq_ts"] = _now
                s["rec"].event("EQUITY", f"权益={bal:.2f} 持仓={s['pos']}")
    # 每个事件都刷新心跳缓存（价格实时；tgt 在信号节拍里更新）
    _lp = q.last_price
    sh["dt"] = q.datetime
    sh["price"] = float(_lp) if _lp and not math.isnan(_lp) else sh["price"]

    # ---- 云端持仓对账：必须在「无新bar 就return」之前 ----
    # 每次 wait_update 都问一次云端，不依赖新 bar（休市/ 重启后首个 bar 到来前
    # 也必须生效，否则新仓无人接管）。只在不一致时才动手，见函数内注释。
    _sync_cloud_pos(api, s, allow_trade, sig=sig)
    # ★ 只读统计（不改交易）：读云端真实持仓均价与浮动盈亏 → 供台账算滑点/MFE/MAE
    _cloud_fill_stats(api, s)

    # ---- 盘中触价扫损（★ 2026-10-17 升级：与平台止损单行为一致）----
    # 每 tick 用实时 last_price 判触价，触及即市价平仓；成交≈止损线 ±1 跳（期货最小
    # 变动价位即 1 跳，极端 1~1.5 跳），与交易所/期货公司止损单口径一致。
    # 摆在 early-return 之前 ⇒ 不依赖 bar 收盘，盘中即扫（原逻辑只在 5min 收盘才查）。
    # 无未来函数：只用「当前 last_price」+ 已挂的 s["stop"]（由已收盘 bar 算出），
    # 不引用任何未来/下一根数据。
    # 防休市误扫：报价须 2 分钟内新鲜（q.datetime 与本地时差 <120s）才认定为交易时段，
    # 否则陈旧价（如周五收盘）若恰好≤止损线会误平真实模拟仓。
    if INTRABAR_STOP and s.get("pos", 0) != 0 and s.get("stop", 0) > 0:
        _lp_s = q.last_price
        _fresh = False
        if _lp_s is not None and not math.isnan(_lp_s) and _lp_s > 0:
            try:
                _age = abs((_dt.datetime.now() - q.datetime).total_seconds())
            except Exception:
                _age = 1e9
            _fresh = _age < 120
        if _fresh:
            _hit = (_lp_s <= s["stop"]) if s["pos"] > 0 else (_lp_s >= s["stop"])
            if _hit:
                _side = "SELL" if s["pos"] > 0 else "BUY"
                _dir = "多" if s["pos"] > 0 else "空"
                _hands = abs(s["pos"])
                if allow_trade:
                    # 统一走 _send_close：上期所/能源按今昨仓拆单（2026-10-19）
                    _ok_o, _note_o, _sent = _send_close(
                        api, s, q.underlying_symbol, _side, _dir, _hands,
                        float(_lp_s), q.datetime, s["pos"])
                    if not _ok_o:
                        rec.event("ORDERERR", f"盘中扫损平仓未报出：{_note_o}")
                        s.setdefault("_ordererr_n", 0)
                        s["_ordererr_n"] += 1
                        return            # 止损仍在，下 tick 继续判触价
                rec.stoploss(_dir, s["stop"], float(_lp_s),
                             "盘中触价扫损(平台止损单行为,≈±1跳)")
                print(f"[{s['name']}][STOP] 盘中扫损 {_dir} {_hands}手 @≈{_lp_s:.1f} "
                      f"(stop={s['stop']:.1f})", flush=True)
                s["pos"] = 0; s["bars_in"] = 0

    bar = k.iloc[-1]
    if bar["id"] == s["last_id"] or bar["id"] < 0:
        return                      # 本品种没有新 bar
    s["last_id"] = bar["id"]
    if s.get("skip_first"):
        s["skip_first"] = False     # 重连后首根 bar：记 id 防重复，不处理
        return
    c, h, l = float(bar["close"]), float(bar["high"]), float(bar["low"])
    if c <= 0:
        return

    df = k
    close = df["close"].to_numpy(float); high = df["high"].to_numpy(float); low = df["low"].to_numpy(float)
    n = len(close)
    pos = s["pos"]
    if n < MA_N + SLOPE_N + 2:
        sh.update({"dt": q.datetime, "price": c, "pos": pos, "tgt": 0})
        return
    # ★ 信号决策统一走 Signal 接口（单一真相源；MA13 占位，未来换因子不动此处）
    ind = sig.indicators(df)
    i = n - 1
    a = ind["atr"][i - 1]
    s["last_atr"] = float(a) if (a == a and a > 0) else s.get("last_atr", 0.0)
    dir_trend, stop_new = sig.entry(ind, i)   # 已含回踩(touch)闸门

    pos = s["pos"]
    # ★★ 接管当根不判出场（2026-10-18 修，实测事故）
    #   接管时止损是**用截至上一根的数据**现算的（_sync_cloud_pos 用 k[nk-1-SLOPE_N:nk-1]），
    #   紧接着又用**当根 bar 的 high/low** 去判它是否被击穿 —— 而这一根的价格早就走完了，
    #   击穿可能发生在「止损还不存在」的时候。这是逻辑上不可能成立的自证，不是数据未来函数。
    #   实测：鸡蛋 JD 空 8 手接管，stop 由前 5 根 swing 算得 3335.0，
    #   而当根（周五最后一根）high 已越过 3335 ⇒ 开机 30 秒内立刻发了一笔假平仓单，
    #   云端并无成交（持仓仍 -8），台账却写了 FILL/STOPLOSS/FILLX，且该单可能挂到周一开盘。
    #   ⇒ 接管后的第一根只用于「站稳」，从下一根起才判出场。
    if pos != 0 and s.pop("_skip_exit_once", False):
        rec.event("EXITSKIP", "接管当根不判出场（止损由前序 bar 算出，不能用已走完的当根反证）")
        print(f"[{s['name']}][接管] 当根跳过出场判定，下一根起恢复正常", flush=True)
    elif pos != 0:
        why = sig.exit_hit(ind, i, pos, s["stop"], s["bars_in"])
        if why is not None:
            _dir = "多" if pos > 0 else "空"
            _side = "SELL" if pos > 0 else "BUY"
            if allow_trade:
                # 统一走 _send_close：上期所/能源按今昨仓拆单（2026-10-19）
                _ok_o, _note_o, _sent = _send_close(
                    api, s, q.underlying_symbol, _side, _dir, abs(pos),
                    float(_lp), q.datetime, pos)
                if not _ok_o:
                    rec.event("ORDERERR", f"收盘出场平仓未报出：{_note_o}")
                    return                # 持仓与止损保持不变，下一根再判
            rec.stoploss(_dir, s["stop"], c,
                         "止损触发" if why == "stop" else "时间止损(MAX_HOLD)")
            print(f"[{s['name']}][EXIT] {_dir} {abs(pos)}手 "
                  f"{'止损' if why == 'stop' else '时间止损'} @≈{c:.1f} "
                  f"(stop={s['stop']:.1f})", flush=True)
            s["pos"] = 0; s["bars_in"] = 0

    # ---- 开仓（对齐回测：趋势 + 回踩；熔断闩死期间禁止）----
    pos = s["pos"]
    _open_ok = True
    # ★★ 换池保护：被接管进来的「已换出池子」的腿只许出场，不许再开新仓
    if s.get("_carried"):
        _open_ok = False
    if ctx is not None:
        _strat = ctx.get("strategy") if isinstance(ctx, dict) else None
        _gate = ctx.get("gate") if isinstance(ctx, dict) else None
        if _strat is not None and _strat.force_dir(s["key"]) not in (0, dir_trend):
            _open_ok = False                      # 截面模式：只开在允许方向
        if _gate is not None and not _gate.can_open(s["key"], _sid):
            _open_ok = False                      # 时段闸门：封禁的 session 不开新仓
    if pos == 0 and dir_trend != 0 and not fuse["halted"] and _open_ok:
        try:
            _eq = float(acc["balance"]) if acc is not None else float("nan")
        except Exception:
            _eq = float("nan")                # 权益读不到 → _hand_of 内兜底 20 万
        hand = s["hand_of"](a, _eq, float(_lp) if _lp and not math.isnan(_lp) else None)
        dir_cn = "多" if dir_trend > 0 else "空"
        # stop_new 已由 sig.entry 一并算出（止损价逐字一致）
        rec.signal(dir_cn, hand, c, q.datetime, c, a,
                   f"MA13回踩顺势 stop≈{stop_new:.1f}" + ("" if allow_trade else " 【观察模式不报单】"))
        if allow_trade and hand > 0:
            # ★ 运维闸门（2026-10-17）：只挡**开仓**，平仓永不被挡。异常一律放行。
            _ok, _why = _risk_check(
                s, acc, hand, shared=shared,
                side_sign=(1 if dir_trend > 0 else -1),
                price=(float(_lp) if _lp and not math.isnan(_lp) else None))
            if not _ok:
                rec.event("RISKBLOCK", f"风控拒开 {dir_cn} {hand}手 | {_why}")
                print(f"[{s['name']}][RISKBLOCK] {_why}", flush=True)
                return
            _side_o = "BUY" if dir_trend > 0 else "SELL"
            _ok_o, _note_o, _ord_o = _order(api, s, q.underlying_symbol, _side_o, "OPEN", hand,
                                            ref_price=(float(_lp) if _lp and not math.isnan(_lp) else None))
            if not _ok_o:
                rec.event("ORDERERR", f"开仓未报出 {dir_cn} {hand}手：{_note_o}")
                print(f"[{s['name']}][ORDERERR] {_note_o}", flush=True)
                return
            s["last_order_ts"] = time.time()      # 等撮合窗口，防自开仓被误接管
            rec.fill(dir_cn, "开", hand, float(_lp), q.underlying_symbol)
            _k = _ops_sent(s, q.underlying_symbol, dir_cn, "开", hand, float(_lp), q.datetime)
            # ★ 只补记：把本笔挂进"待回填"队列，等撮合完成（FIX_WAIT_SEC 之后）
            #   由 _cloud_fill_stats 读云端真实成交均价 → FILLX 行。
            #   ★ 不能在下单这一刻读云端：撮合有几秒延迟，此刻读到的还是**旧仓**，
            #     会把上一笔的价格错记成本笔成交价（比原FILL 更糟）。
            s.setdefault("pending_fill", []).append(
                {"ts": s["last_order_ts"], "side": "开", "dir": dir_cn,
                 "hands": hand, "ref_price": float(_lp), "key": _k,
                 # ★ 开仓目标净仓 = 方向 × 手数（开仓前必为空仓，s["pos"]==0 才走到这里）
                 "expect_net": (hand if dir_trend > 0 else -hand),
                 # 订单对象：确认阶段读 status/last_msg 落「废单原因」（2026-10-19）
                 "order": _ord_o, "offset": "OPEN",
                 # ★ 2026-10-20 撤单重报所需（见 _send_close 同名注释）
                 "symbol": q.underlying_symbol, "bar_time": q.datetime})
            # 已修复单品种版 led 未定义的 NameError；账本对接见 运营SOP 欠账#6
        print(f"[{s['name']}][{'OPEN' if allow_trade else 'SIGNAL'}] {dir_cn} {hand}手 "
              f"@≈{c:.1f} 止损≈{stop_new:.1f} ATR={a:.2f}", flush=True)
        if allow_trade:
            s["pos"] = hand if dir_trend > 0 else -hand
            s["entry"] = c; s["stop"] = stop_new; s["bars_in"] = 0
            s["_skip_exit_once"] = False      # ★ 清掉可能残留的接管跳过标记
            s["underlying"] = q.underlying_symbol
            _save_underlying(s.get("record_dir"), s["key"], s["underlying"])
    elif pos != 0:
        s["bars_in"] += 1

    tgt = int(dir_trend) if (s["pos"] == 0 and dir_trend != 0) else \
          (1 if s["pos"] > 0 else -1 if s["pos"] < 0 else 0)
    sh.update({"dt": q.datetime, "price": c, "pos": s["pos"], "tgt": tgt})


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS),
                    help="品种key，逗号分隔，如 JM,M,JD,P,SR")
    ap.add_argument("--trade", action="store_true",
                    help="开真实下单（TqKq 快期模拟账户，手机 App 可同步看到）; 缺省为观察模式")
    ap.add_argument("--gui", action="store_true", help="开 web_gui 图形界面")
    ap.add_argument("--drill-fuse", action="store_true",
                    help="熔断演练：启动后强制触发一次回撤熔断（会闩死，需人工复位）")
    args = ap.parse_args()

    # ★★ 核心层单实例闸门（2026-10-09）★★
    #   抢不到锁 = 已有另一个核心在跑 ⇒ 直接退出，绝不第二个下单。
    #   这是最后一道防线：守护锁挡不住「两个守护同时抢到锁」的竞态。
    if _抢核心锁() is None:
        try:
            with io.open(_CORE_LOCK, "r", encoding="utf-8") as _f:
                _other = (_f.readline() or "0").strip()
        except Exception:
            _other = "?"
        print(f"[核心] 已有核心实例在运行（PID {_other}），本进程拒绝启动以防双份下单。"
              f"若确认那是僵尸锁，删掉 {_CORE_LOCK} 再试。", flush=True)
        raise SystemExit(0)

    try:
        run_live_multi(symbol_keys=[x.strip().upper() for x in args.symbols.split(",") if x.strip()],
                       allow_trade=args.trade, web_gui=args.gui,
                       drill_fuse=args.drill_fuse)
    finally:
        _释放核心锁()
