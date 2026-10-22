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
import sys, os, time, math, json, threading, io, hashlib, datetime as _dt
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
# ★★ 2026-10-20 风控剩余节点（P0-2 / P1-1 / P1-3）：同样抽纯逻辑，主文件只接线
from lib import daily_loss as _dl      # 单日亏损阀（P1-1，口径 D 已拍板）
from lib import recon_halt as _rh      # 对账偏离 ⇒ 硬停机判定（P0-2）
from lib import limit_board as _lb     # 涨跌停封板判定（P1-3）
# ★★ 2026-10-21 #15：计划止损持久化 —— 重启/接管时原样恢复止损线，不再重算放宽
from lib import plan_stop as _ps
# ★★ 2026-10-21 风控二期（D2 已拍板 #17/#18/#20/#22/#23/#24）—— 全部纯逻辑、可离线单测
from lib import alert_queue as _aq     # #17-A 告警队列（文件底座，外送二期否决）
from lib import leg_gate as _lg        # #18   组合腿数闸门（K=6，按品种聚合）
from lib import slippage as _slip      # #20   滑点实测（信号根收盘为基准，只记录）
from lib import overnight as _ovn      # #22-A/C 隔夜上限 + 快照
from lib import trade_calendar as _tc  # #24   交易日历（#22-B 前置）
from lib import holiday_close as _hc   # #22-B 长假强平（唯一授权平仓例外）
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

# ---- ★★ P0-2 / P1-1 / P1-2 / P1-3（2026-10-20，见 交接\06模拟盘_风控增强_剩余任务书_20261020.md）----
#   P0-2 对账偏离超阈 ⇒ 锁「只出不进」（复用 fuse 闩锁），排除 FIX_WAIT / 换月过渡；
#   P1-1 当日亏损（昨收基准）≥1.5% ⇒ **只拒绝新开仓**，平仓/止损/撤单照常，状态落盘；
#   P1-2 真实保证金占用率可见 + 逼近强平线预警（`NOTIONAL_CAP` 只是静态估算，不是占用率）；
#   P1-3 封板方向不报单（只读交易所 upper_limit/lower_limit，不推理行情）。
DAILY_LOSS_RATIO = _dl.DAILY_LOSS_RATIO       # 1.5%（D 拍板，执行员不得改数）
MARGIN_WARN_RATIO = OPS_MAX_MARGIN_RATIO * 0.75   # 0.45：60% 拒开线的 3/4，逼近才预警
                                                 # ★ 派生值（任务书未给数），D 可改；非策略参数
MARGIN_WARN_REPEAT_SEC = 600                  # 同一腿预警最多 10 分钟一条（防刷屏）
LIMIT_BOARD_CHECK = True                      # P1-3 开关（字段不可读时自动放行，不拦）
DLOSS_UPDATE_SEC = 5                          # 亏损阀计数节流（权益每 tick 都变，落盘要省）


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


_ALERT_DIR = None        # #17-A：台账目录，由 run_live_multi 设置；None ⇒ 只弹窗不落队列


def _alert_level_of(title):
    """#17-A 分级（D2 §2-17.2）：熔断/对账停机/日亏阀/只出不进 = 最高级，其余常规。

    判定依据是**标题关键词**而不是调用点 —— 这样新增弹窗不需要同时登记等级，
    漏登记最坏结果是降级为 WARN（仍会落盘、仍可查），不会静默丢告警。
    """
    s = str(title)
    for kw in ("熔断", "亏损阀", "对账", "只出不进", "停机", "强平"):
        if kw in s:
            return _aq.LEVEL_CRITICAL
    return _aq.LEVEL_WARN


def _popup(title, msg, source=None, level=None, dedup_key=None):
    """Windows 原生弹窗提醒（独立线程，绝不阻塞交易主循环）。
    无窗口环境/非 Windows 下静默降级为只在控制台响铃。

    ★★ #17-A（2026-10-21）：弹窗 + 告警队列**双写**。
       弹窗是"人在电脑前"才有效的通道，队列是"事后可查"的底座。
       写队列失败绝不影响弹窗，也不影响交易（`_aq.push` 本身永不抛）。
       dedup_key 缺省取 **title**（稳定）—— 不能用 msg，因为很多 msg 带时间戳，
       每次不同会让"同一事件只落一次"的去重完全失效。
    """
    try:
        if _ALERT_DIR:
            _aq.push(_ALERT_DIR, level or _alert_level_of(title),
                     source or str(title), msg,
                     dedup_key=dedup_key or str(title))
    except Exception:
        pass                          # 告警落盘失败不能挡住下面真正的弹窗
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


# ============================================================
# ★★ 风控二期启动自证（2026-10-21，D2 #23 + #18 + #24）
# ============================================================
def _boot_guard(record_dir, specs, allow_trade=False):
    """启动时把「到底哪套风控参数在跑」写死在日志里（#23）。

    为什么：A10 踩过 —— 代码改了、线上没生效（`base_day` 长期 null），
    事后只能靠翻代码猜。参数散落在本文件顶部十余个常量里，启动时没有任何一行
    能一眼核对。本函数把关键参数打成**一行可解析 JSON**（前缀 `[PARAM]`），
    并落一份告警队列记录；**参数变了才落新记录**（dedup_key 带内容哈希），
    所以"重启后参数没变"不会刷屏，"参数悄悄变了"一定留痕。

    顺带做两件事（同一条启动路径）：
      · #18 打印腿数闸门是否真的生效（池 ≤ K ⇒ 今日零触发，D2 18.1 要求明示）
      · #24 交易日历覆盖期检查（超出 ⇒ 最高级告警，绝不静默）

    ★ 全程 try/except：这段在交易主循环之前，抛异常等于进程起不来。
    """
    try:
        pool = [s["key"] for s in specs]
        snap = {
            "池": pool,
            "allow_trade": bool(allow_trade),
            # 既有风控（一期）
            "DAILY_LOSS_RATIO": DAILY_LOSS_RATIO,
            "DD_FUSE": DD_FUSE,
            "OPS_MAX_MARGIN_RATIO": OPS_MAX_MARGIN_RATIO,
            "MARGIN_WARN_RATIO": MARGIN_WARN_RATIO,
            "NOTIONAL_CAP": NOTIONAL_CAP,
            "LIMIT_BOARD_CHECK": LIMIT_BOARD_CHECK,
            # 策略口径
            "MAX_HOLD": MAX_HOLD,
            "RETRACE_TOL": RETRACE_TOL,
            "STOP_BUFFER": STOP_BUFFER,
            "STOP_CAP": STOP_CAP,
            "INTRABAR_STOP": INTRABAR_STOP,
            "KSEC": KSEC,
            # 风控二期（D2 2026-10-21 拍板）
            "LEG_MAX": _lg.MAX_LEGS,
            "OVN_TOTAL_RATIO": _ovn.TOTAL_RATIO,
            "OVN_SYM_RATIO": _ovn.SYM_RATIO,
            "OVN_HOLIDAY_RATIO": _ovn.HOLIDAY_TOTAL_RATIO,
            "HOLIDAY_CLOSE_MIN_GAP": _hc.MIN_GAP,
        }
        line = "[PARAM] " + json.dumps(snap, ensure_ascii=False, sort_keys=True)
        print(line, flush=True)
        try:
            _h = hashlib.md5(line.encode("utf-8")).hexdigest()[:12]
            _aq.push(record_dir, _aq.LEVEL_INFO, "参数自证", line,
                     dedup_key=f"param:{_h}")
        except Exception:
            pass
    except Exception as e:                              # noqa: BLE001
        print(f"[PARAM] 参数快照失败（不影响交易）：{type(e).__name__}: {e}", flush=True)

    # #18 腿数闸门：池 ≤ K 时今日零触发，必须明示，别让人以为它在保护什么
    try:
        _w = _lg.warn_if_inert([s["key"] for s in specs])
        if _w:
            print(_w, flush=True)
    except Exception:
        pass

    # #24 交易日历覆盖期（D2 §8-④：缺失必须最高级告警交人工）
    try:
        _cw = _tc.coverage_warning()
        if _cw:
            print(f"[日历] ⚠️ {_cw}", flush=True)
            _aq.push(record_dir, _aq.LEVEL_CRITICAL, "交易日历", _cw,
                     dedup_key="calendar-missing")
        else:
            _td = _rs.trading_day()
            _pre, _gap, _hol, _nxt = _tc.is_pre_long_holiday(_td, _hc.MIN_GAP)
            if _pre:
                _msg = (f"今日 {_td} 是{_hol}长假前最后交易日（间隔 {_gap} 天，"
                        f"下一交易日 {_nxt}）⇒ 收盘最后 5min 将强平全部持仓"
                        f"（#22-B，用户 2026-10-21 授权）")
                print(f"[日历] {_msg}", flush=True)
                _aq.push(record_dir, _aq.LEVEL_CRITICAL, "长假强平", _msg,
                         dedup_key=f"pre-holiday:{_td}")
    except Exception:
        pass


# ---------------------------------------------------------------- 周期任务
# ★★ #22-C 隔夜快照 + #22-B 长假强平（2026-10-21）
#   两者都是"每天该做一次"的事，但**触发条件完全不同**：
#     · 快照：任何交易日 15:00 后写一次（纯记录，零风险）
#     · 强平：仅长假前最后交易日、且已过最后一条 5min K 线收盘（唯一授权平仓例外）
#   节流到 60s 一次，避免 wait_update 每 30s 醒来就重算一遍日历。
_RSK_STATE = {"last": 0.0, "snap_day": None}
_RSK_THROTTLE = 60.0


def _bar_times(k, limit=8):
    """取最近 limit 根 K 线的时间（datetime 列表）。失败返回 []。"""
    try:
        import pandas as _pd
        s = _pd.to_datetime(k["datetime"])
        return [x.to_pydatetime() for x in s.tail(int(limit))]
    except Exception:
        return []


def _periodic_risk(api, specs, shared, acc_box, record_dir, allow_trade,
                   drill_holiday=False):
    """主循环里的周期风控任务。**任何异常都不允许打断交易主循环**。"""
    now_wall = time.time()
    if now_wall - float(_RSK_STATE["last"]) < _RSK_THROTTLE:
        return
    _RSK_STATE["last"] = now_wall

    day = str(_rs.trading_day())
    try:
        acc = (acc_box or {}).get("acc")
        eq = getattr(acc, "balance", None) if acc is not None else None
        mg = getattr(acc, "margin", None) if acc is not None else None

        # ---------------- #22-C 每日收盘后一份隔夜敞口快照 ----------------
        try:
            if _dt.datetime.now().hour >= 15 and _RSK_STATE.get("snap_day") != day:
                # ★★ #18 P1-A 第 3 件（2026-10-21）：快照同样读 shared["pos"]。
                #   不先回填就会把**鬼腿**算进隔夜敞口：实测 15:01:04 快照记
                #   FU+EG+BU+SA = 1,243,200 / 1.2642×，而真实只有 FU+SA =
                #   537,040 / 0.546×（EG/BU 早在 14:44:59 已平）⇒ **高估 2.32 倍**。
                #   而 D2 的 #22-C 计划正是拿这份历史回检阈值 ⇒ 数据源必须先干净。
                for _sp in (specs or []):
                    _sync_shared_pos(shared, _sp)
                legs = _ovn.build_legs(shared, config_symbols.SYMBOLS)
                ok, note = _ovn.snapshot(record_dir, day, legs, eq, margin=mg)
                if ok:
                    _RSK_STATE["snap_day"] = day
                    print(f"[隔夜快照] {day} 已落盘：{note}", flush=True)
                    _aq.push(record_dir, _aq.LEVEL_INFO, "隔夜快照",
                             f"{day} 隔夜敞口快照已落盘：{note}",
                             dedup_key=f"ovn-snap:{day}")
                else:
                    print(f"[隔夜快照] 未落盘：{note}", flush=True)
        except Exception as e:                          # noqa: BLE001
            print(f"[隔夜快照] 异常（不影响交易）：{type(e).__name__}: {e}", flush=True)

        # ---------------- #22-B 长假收盘强平（唯一授权平仓例外）----------------
        try:
            pos_list = [{"key": s.get("key"), "hands": int(s.get("pos") or 0)}
                        for s in specs if int(s.get("pos") or 0) != 0]
            bars_dt = []
            for s in specs:
                bars_dt = _bar_times(s.get("k"))
                if bars_dt:
                    break
            r = _hc.decide(day, _dt.datetime.now(), bars_dt, pos_list,
                           ksec=KSEC, record_dir=record_dir, drill=drill_holiday)

            # ⑤ 日历缺失/判定异常 ⇒ 最高级告警，不强行平
            if r.get("warn"):
                _popup("MA13 模拟盘：长假强平判定受阻",
                       r["warn"], source="长假强平",
                       level=_aq.LEVEL_CRITICAL, dedup_key="holiday-close-warn")

            if r.get("act"):
                _legs_txt = "、".join(f"{L['key']}{L['dir']}{L['hands']}手"
                                      for L in r["legs"])
                head = (f"{r['holiday']} 长假前最后交易日（间隔 {r['gap']} 天，"
                        f"下一交易日 {r['next_day']:%Y-%m-%d}）")
                if drill_holiday:
                    print(f"[长假强平][演练] {head} ⇒ 将平：{_legs_txt}"
                          f"（演练不发单、不记已执行）", flush=True)
                    _aq.push(record_dir, _aq.LEVEL_WARN, "长假强平演练",
                             f"{head} 将平：{_legs_txt}（演练）",
                             dedup_key=f"holiday-drill:{day}")
                elif not allow_trade:
                    print(f"[长假强平] {head} 但当前为观察模式（--trade 未开）⇒ 不发单。"
                          f"将平：{_legs_txt}", flush=True)
                    _aq.push(record_dir, _aq.LEVEL_WARN, "长假强平",
                             f"{head} 观察模式不发单，将平：{_legs_txt}",
                             dedup_key=f"holiday-observe:{day}")
                else:
                    _sent_ok, _sent_fail = [], []
                    for s in specs:
                        _p = int(s.get("pos") or 0)
                        if _p == 0:
                            continue
                        try:
                            _dir_cn = "多" if _p > 0 else "空"
                            _side = "SELL" if _p > 0 else "BUY"
                            _q = s["q"]
                            _lp = float(_q.last_price or 0)
                            _okc, _notec, _ = _send_close(
                                api, s, _q.underlying_symbol, _side, _dir_cn,
                                abs(_p), _lp, _q.datetime, _p,
                                sig_close=_lp)     # 强平无"信号根"，用当时价作基准
                            if _okc:
                                _sent_ok.append(f"{s['name']}{_dir_cn}{abs(_p)}手")
                            else:
                                _sent_fail.append(f"{s['name']}：{_notec}")
                        except Exception as e:      # noqa: BLE001
                            _sent_fail.append(f"{s.get('name')}：{type(e).__name__}: {e}")
                    msg = (f"⚠️ {head} ⇒ 长假强平已发单。已发："
                           f"{'、'.join(_sent_ok) or '无'}"
                           f"{'；失败：' + '、'.join(_sent_fail) if _sent_fail else ''}")
                    print(f"[长假强平] {msg}", flush=True)
                    try:
                        s0 = specs[0]
                        s0["rec"].event("HOLIDAYCLOSE", msg)
                    except Exception:
                        pass
                    _popup("MA13 模拟盘：长假收盘强平",
                           f"{msg}\n\n这是用户 2026-10-21 明确授权的唯一自动平仓场景"
                           f"（≥5 天长假）。其余场景仍禁止自动平仓。",
                           source="长假强平", level=_aq.LEVEL_CRITICAL,
                           dedup_key=f"holiday-close:{day}")
                    # ★ 幂等标记：同一长假只触发一次。**只在真的发了单之后**才标记，
                    #   观察模式/演练不标记（下次开 --trade 仍要能触发）。
                    _hc.mark_done(record_dir, day, {
                        "gap": r["gap"], "holiday": r["holiday"],
                        "legs": r["legs"], "ok": _sent_ok, "fail": _sent_fail,
                    })
        except Exception as e:                          # noqa: BLE001
            print(f"[长假强平] 判定异常（不影响交易）：{type(e).__name__}: {e}", flush=True)
    except Exception as e:                              # noqa: BLE001
        print(f"[周期风控] 异常（不影响交易）：{type(e).__name__}: {e}", flush=True)


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
    # ★★ #19（2026-10-21 18:55，D1 直接改，用户授权「工程量小的决策员直接做」）：
    #   `skip_first` 初始化 **False → True**：进程启动后的首根 bar **只记账不处理**。
    #   原因：`last_id` 仅存内存、重启后为 -1 ⇒ 最后一根**已收盘**的 bar 会被当成新 bar 重放。
    #   实测 2026-10-21 18:03:41：BU+8 / EG-7 依据 14:59:59.8 的 bar，在**收盘后 3 小时**成交。
    #   重连路径（本文件 :953）本来就置 True 防重复，只有**进程重启路径**漏了 —— 这里补齐，两条路径对齐。
    #   ★ 不关止损：盘中扫损在 :2265 附近逐 tick 执行，不看 bar，不受本开关影响。
    s.update({"pos": 0, "entry": 0.0, "stop": 0.0, "bars_in": 0,
              "last_id": -1, "skip_first": True, "recovered": False,
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
                   ctx=None, drill_dloss=False, drill_holidayclose=False):
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

    # ★★ #17-A：把台账目录交给 _popup，之后所有弹窗同时落「告警队列.jsonl」。
    #   放在这里而不是函数参数 —— _popup 有 6 处调用，逐个传参易漏。
    global _ALERT_DIR
    _ALERT_DIR = record_dir
    # ★★ #23/#18/#24 启动自证（参数快照 / 腿数闸门是否生效 / 日历覆盖期）
    _boot_guard(record_dir, specs, allow_trade)

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

    # ---- 心跳独立线程：休市/无行情时照写，防台账断层误报 ----    # 铁律：线程内绝不触碰 tqsdk 对象（q/k/api），只读 shared 里的纯标量缓存。
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
                           # ★ 2026-10-20 P0-2：记「为什么锁的」，复盘才不用猜
                           "reason": fuse.get("reason") or "",
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
        fuse["reason"] = str(st.get("reason") or "")
    except Exception:
        pass                          # 无文件/损坏 = 全新状态
    if fuse["halted"]:
        _why0 = fuse.get("reason") or "回撤熔断"
        _popup("MA13 模拟盘：只出不进闩锁未复位",
               "上一次运行触发了【只出不进】闩锁（回撤熔断 或 对账偏离硬停机），"
               "本次进程将以【只出不进】模式运行"
               "（已有持仓照常出场，禁止一切新开仓）。\n\n"
               f"历史峰值: {fuse['peak']:.0f}\n"
               f"锁定原因: {_why0}\n\n"
               "确认复位方法：\n"
               "  1. 关闭本程序窗口\n"
               "  2. 删除文件 策略\\06_MA13超短线\\模拟盘\\台账\\熔断状态.json\n"
               "  3. 重新双击 启动_P_EB_观察模式.bat")
        for s in specs:
            s["rec"].event("CIRCUIT", f"启动时检测到未复位的闩锁 → 只出不进模式 | 原因={_why0}")

    # ---- ★★ P1-1 单日亏损阀（2026-10-20）：账户级，状态落盘，重启不清零 ----
    #   基准 = 上一交易日收盘权益（EQUITY 末行）；阈值 = 基准 × 1.5%。
    #   首次启动（无状态文件）时从台账反查上一交易日收盘权益，**不编造**。
    dloss = _dl.DailyLossGuard(os.path.join(record_dir, _dl.STATE_NAME),
                              ratio=DAILY_LOSS_RATIO)
    if not dloss.st.get("base_equity"):
        try:
            _tape = os.path.join(record_dir, specs[0]["name"] + "_成交流水.csv")
            _eq0, _ts0 = _dl.last_equity_before(_tape, _dl.day_start(_rs.trading_day()))
        except Exception:
            _eq0, _ts0 = None, None
        if _eq0:
            dloss.seed(_eq0, src="tape")
            print(f"[单日亏损阀] 基准播种=上一交易日收盘权益 {_eq0:.0f}（{_ts0}）", flush=True)
        else:
            print("[单日亏损阀] ⚠️ 台账里没有上一交易日 EQUITY ⇒ 首个快照播种"
                  "（当日基准=当时权益，等价于今天从 0 亏损算起）", flush=True)

    # ---- ★★ #16（2026-10-21）：单日亏损阀演练（--drill-dailyloss）----
    #   用途：在**没有真实亏损**的情况下，验证「触阈后只拒新开仓 / 止损不动 / 平仓放行」。
    #   与 --drill-fuse 的差别：熔断演练刻意落盘（复位要人工删文件）；本演练**纯内存**，
    #   不写 `单日亏损状态.json` —— 它验的是「阀生效后的行为」，不是「跨日复位」，
    #   没必要污染真实风控状态，重启即恢复。
    if drill_dloss:
        dloss.force_blocked()
        print("\n" + "=" * 68, flush=True)
        print("  单日亏损阀演练（--drill-dailyloss）：已强制置为「当日已触阀」", flush=True)
        print("  预期三要素：", flush=True)
        print("    ① 一切新开仓被拒 → 台账 RISKBLOCK「停加仓阀拒开…[DRILL]」", flush=True)
        print("    ② 已有持仓的 stop **不变**（风控不碰止损线）", flush=True)
        print("    ③ 平仓 / 止损 / 撤单**照常**执行（只挡开仓入口）", flush=True)
        print("  不写 单日亏损状态.json ⇒ 重启即恢复，无需手工复位。", flush=True)
        print(f"  当前阀状态：{dloss.summary()}", flush=True)
        print("=" * 68 + "\n", flush=True)
        for s in specs:
            s["rec"].event("DAILYSTOP",
                           f"[DRILL] 演练：强制置为已触阀 → 只出不进 | {dloss.summary()}")

    if dloss.blocked:
        _dtag = "（演练）" if getattr(dloss, "_drill", False) else ""
        _popup(f"MA13 模拟盘：单日亏损阀已触发{_dtag}",
               f"当前交易日已触发单日亏损阀（阈值 {DAILY_LOSS_RATIO:.1%}）{_dtag}。\n\n"
               f"{dloss.summary()}\n\n"
               "本次进程【只出不进】：拒绝一切新开仓，已有持仓照常按止损/时间止损出场。\n"
               + ("演练模式：重启进程即恢复，无需手工复位。"
                  if getattr(dloss, "_drill", False) else
                  "下一交易日自动复位，无需人工解锁。"))
        for s in specs:
            s["rec"].event("DAILYSTOP", f"启动时读回已触发的单日亏损阀 → 只出不进 | "
                                        f"{dloss.summary()}")

    api = _connect(user, pwd, specs, web_gui)
    # 当前会话的账户对象（TqKq 云端账户，随重连刷新）+ 账户级风控对象
    #   dloss 挂在这里（而不是每个腿一份）：亏损阀管的是**整个账户**，
    #   6 条腿各算一份会得到 6 个不同基准，且各自落盘互相覆盖。
    acc_box = {"acc": None, "dloss": dloss, "_dl_ts": 0.0}
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
                        # ★★ #15（2026-10-21）：持仓变化时同步「计划止损」落盘
                        #   （空仓清理 / 缺记录补写；不改 s["stop"]、不影响出场）
                        _sync_plan_stop(s)
                    # ★★ 风控二期周期任务（2026-10-21，60s 节流、内部全程吞异常）：
                    #   #22-C 每日收盘后隔夜快照 / #22-B 长假收盘强平（唯一授权平仓例外）
                    _periodic_risk(api, specs, shared, acc_box, record_dir,
                                   allow_trade, drill_holiday=drill_holidayclose)
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


def _sync_shared_pos(shared, s):
    """★ #18 P1-A（2026-10-21）：把**真相源** `s["pos"]` 写回 `shared[s["key"]]["pos"]`。

    凡是**在风控判定前读 shared 持仓**的地方，都必须先调一次：
      · 开仓闸门 `_risk_check`（否则同拍「平仓→反手」会双计）
      · 隔夜敞口快照 `_ovn.build_legs`（否则鬼腿会算进敞口，实测高估 2.32 倍）

    背景：`sh["pos"]` 只在 `_process` 末尾更新一次，同一拍内读到的是**上一根 bar 的
    持仓**。10-19 心跳 P0 已栽过一次同样的坑（当时只改了心跳一处、没固化），
    这次复发 ⇒ 详见 `01_判死与已试清单.md` §3 第 10 条「缓存字段冒充真相源」。

    绝不抛：这是同步元数据，不能影响交易主循环。
    """
    try:
        shared[s["key"]]["pos"] = int(s.get("pos") or 0)
        return True
    except Exception:
        return False


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


def _risk_check(s, acc, hand, shared=None, side_sign=1, price=None, day=None):
    """开仓前风控闸门（**只挡开仓，绝不挡平仓**）。异常一律放行。

    ★★ 2026-10-21 风控二期接线（D2 已拍板，顺序有意义：先组合层、后单账户层）
      ① #18 腿数闸门：`shared` 里 pos≠0 的**品种数** ≥ K(6) ⇒ 拒开。
         口径按品种聚合（18.3），换池双份算 1 腿；`exclude=` 自身不算新增腿。
      ② #22-A 隔夜上限：总名义 ≤1.0×权益 / 单品种 ≤0.3×权益 / 长假前降到 0.5×。
         只在**开新腿**时算新增名义；同品种加仓不重复计（那不改隔夜来源数）。
      ③ 既有 `RiskGate`：手数 / 保证金占用率 / 同向名义敞口。
    三层任一不过即拒，**平仓永不经过本函数**。
    """
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

        # ① 组合腿数（#18）
        if shared is not None:
            _lok, _lwhy = _lg.check_open(shared, key=s.get("key"))
            if not _lok:
                return False, _lwhy

        # ② 隔夜敞口（#22-A）—— 只在有权益、有名义时判；缺数据放行不误杀
        try:
            _legs = _ovn.build_legs(shared, config_symbols.SYMBOLS)
            _ok2, _why2 = _ovn.check_limit(_legs, bal, key=s.get("key"),
                                           new_notional=(on or 0.0), day=day)
            if not _ok2:
                return False, _why2
        except Exception:
            pass

        # ③ 既有单账户闸门
        _ok3, _why3 = g.check_open(hand, balance=bal, margin=mg,
                                   same_side_notional=ssn, order_notional=on)
        # ④ #21 可用资金预检 —— **只观察、不拒单**（D2 21.2）
        if _ok3:
            _avail_precheck(s, acc, hand, side_sign)
        return _ok3, _why3
    except Exception:
        return True, ""


def _avail_precheck(s, acc, hand, side_sign):
    """#21：开仓前用 `available` 与 Quote.margin_long/short 预检"钱够不够"。

    **只打日志 + 落告警，绝不改变是否开仓**（D2 21.2 拍板"只预检+日志、不拒单"，
    理由是误杀正常开仓的代价 > 废单+重报；先观察误杀率再决定是否真的拦）。

    字段语义（tqsdk `objs.py` 源码取证，2026-10-21）：
      · `Account.available` = 「账户权益 − 冻结保证金 − 保证金 − 冻结权利金 − 冻结手续费」
        ⇒ **这才是"可用资金"**，`balance` 是动态权益，两者不是一回事。
      · `Quote.margin_long` / `margin_short` = 每手多头/空头保证金（交易所口径）。
      · ★ 所有字段默认 `float("nan")`（不是 None）⇒ 必须显式判 nan，
        否则 `nan <= 0` 恒 False，会让"未就绪"被当成"充足"。
    """
    try:
        _av = getattr(acc, "available", None)
        if _av is None or not (_av == _av):
            return                                   # 未就绪 ⇒ 不预检（不算误杀）
        _q = s.get("q")
        if _q is None:
            return
        _field = "margin_long" if side_sign > 0 else "margin_short"
        _m = getattr(_q, _field, None)
        if _m is None or not (_m == _m) or float(_m) <= 0:
            return                                   # 每手保证金未就绪
        _need = abs(float(hand)) * float(_m)
        if float(_av) >= _need:
            return
        _name = s.get("name") or s.get("key")
        _msg = (f"可用资金 {float(_av):.0f} < 拟开仓所需保证金 {_need:.0f}"
                f"（{hand}手 × 每手 {float(_m):.0f}）⇒ 本单可能被柜台拒单。"
                f"**预检只记录、不拦截**（#21）。")
        print(f"[{_name}][AVAILPRE] {_msg}", flush=True)
        _aq.push(s.get("record_dir") or _ALERT_DIR, _aq.LEVEL_WARN, _name,
                 _msg, dedup_key=f"availpre:{s.get('key')}")
    except Exception:
        pass                                          # 预检异常绝不介入开仓决策


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


def _bars_since_bar(k, bar_time):
    """自 `bar_time` 那根之后又走完了多少根 K 线。取不到返回 None。

    用于接管时恢复 `bars_in`（时间止损 MAX_HOLD 的计时器）。
    原先一律归零 ⇒ 每次重启时间止损都重新计时，"持仓过久"永远触发不了。
    计数口径：`bar_time` 之后出现的每一根都算走过一根（最后一行是正在形成的
    那根，但它也代表前一根已收盘 ⇒ 不减 1）。

    ★★ 2026-10-21 13:50 修 8 小时时区错配（E1）：
      tqsdk 的 kline `datetime` 是 **UTC 纳秒整数**，`pd.to_datetime()` 出来是 UTC naive；
      而传进来的 `bar_time`（= `quote.datetime` 字符串）是**北京时间**。
      两者直接比较差 8 小时 ⇒ 持仓不足 8 小时时 `kd > bt` 恒为 False ⇒ bars_in 永远 0。
      实证（13:32 核心日志）：`bar_time=11:29:59.980000 | K末行=05:30:00 | kd>bt 计数=0`
      —— 05:30 UTC 正是北京 13:30，而 bars_in 被算成 0。
      修法：**统一抬到北京时间**。`kd` 一律 +8h；`bt` 只在「纳秒整数」分支 +8h
      （字符串分支本就是北京，再加就变成多 8h）。
    """
    if bar_time is None:
        return None
    try:
        import pandas as _pd
        _s = str(bar_time).strip()
        _TZ = _pd.Timedelta(hours=8)          # tqsdk kline datetime 以 UTC 为基准
        # tqsdk 的 quote.datetime 正常是 "YYYY-MM-DD HH:MM:SS.ffffff" 字符串；
        # 但 K线 datetime 是**纳秒整数**。两种都认，认不出就返回 None。
        if _s.isdigit() and len(_s) >= 16:
            bt = _pd.Timestamp(int(_s)) + _TZ          # 纳秒(UTC) ⇒ 北京
        else:
            bt = _pd.Timestamp(_s[:26])                # 字符串已是北京，不再加
        kd = _pd.to_datetime(k["datetime"]) + _TZ      # UTC ⇒ 北京（与 bt 同基准）
        _cnt = max(0, int((kd > bt).sum()))
        # ★ 2026-10-21 D1 取证（#15 体检 P2-6）：**不改计数逻辑**，只把输入暴露出来。
        #   争议点：tqsdk 的 get_kline_serial 返回的 DataFrame 是否含「正在形成」的 bar。
        #   若含，则 kd>bt 会把未收盘那根算进去 ⇒ bars_in 多 1 ⇒ MAX_HOLD 提前 1 根触发。
        #   若 K末行与 bar_time 落在同一时段（差 ≤ 8h 已修正），说明两列同基准、计数可信。
        try:
            print(f"[bars_in取证] bar_time={bt} | K末行={kd.iloc[-1]} | K根数={len(kd)} | "
                  f"kd>bt 计数={_cnt}（末行若为未收盘bar则多算1根）", flush=True)
        except Exception:
            pass
        return _cnt
    except Exception:
        return None


def _attach_stop_on_boot(s, pos, c, a, sig=None):
    """启动/重连后补挂止损。

    ★★ 2026-10-21 改（#15「RECOVER 止损重锚」）：**优先恢复开仓时落盘的计划止损**。
       原实现无条件用「当前收盘 ∓ 1.15×ATR」重算，行情反向时会把止损**放宽**
       （实测豆粕 3276→3271.1、纯碱→951.9），并把 `bars_in` 归零让时间止损
       重新计时。现在分两条路：
         A. 有记录（合约 + 方向都对）⇒ **原样恢复，一个字都不重算**
         B. 无记录（老持仓 / 换月 / 文件被删）⇒ 才按现价重算兜底，并立即固化，
            下次重启起走 A

    ⚠️ 路径 B 的重算算法必须与开仓处**逐字一致** ⇒ 统一走 signal.stop_price()，
       禁止在这里另写一份。
    ⚠️ **不采用「计划和重算取更紧的那个」**：重算在盈利时会收紧（等价于偷偷加了
       移动止损，规则 2 禁止的叠加条件）、在亏损时会放宽（放大风险），两个方向
       都不对，唯一语义正确的解是原样恢复。
    """
    if pos == 0 or a != a or a <= 0:
        return
    sig = sig or _SIG
    _q = s.get("q")
    _lp = getattr(_q, "last_price", None) if _q is not None else None
    c = float(_lp or c)
    d = 1 if pos > 0 else -1
    _sym = getattr(_q, "underlying_symbol", None) if _q is not None else None
    _rd = s.get("record_dir")

    plan = _ps.load(_rd, s.get("key"), _sym, d)

    if plan is not None:
        # ---------------- 路径 A：原样恢复（正常路径）----------------
        stop = float(plan["stop"])
        s["stop"] = stop
        s["entry"] = float(plan.get("entry") or c)
        _bi = _bars_since_bar(s.get("k"), plan.get("bar_time"))
        if _bi is None and plan.get("bar_time"):
            print(f"[{s['name']}]⚠️ 无法从 bar_time={plan.get('bar_time')} 恢复 bars_in"
                  f"（按 0 计，时间止损将重新计时）", flush=True)
        s["bars_in"] = int(_bi) if _bi is not None else 0
        s["recovered"] = True
        _pen = ""
        if d > 0 and c <= stop:
            _pen = "⚠️ 现价已跌破计划止损（本应已出场）"
        elif d < 0 and c >= stop:
            _pen = "⚠️ 现价已涨破计划止损（本应已出场）"
        # ★★ E4（2026-10-21 14:06）：**现价已穿越止损 ⇒ 不允许跳过当根出场**。
        #   原实现无条件 skip，实测代价：13:32 接管 FU 时现价已跌破 4815.64，
        #   但跳过当根、等 13:35 那根收盘才平，成交 4804 ⇒ 多亏 11.64 点 ≈ 815 元。
        #   判定用 `last_price`（当前事实），不是 bar 的 low/high（可能是已走完的历史），
        #   所以这里不跳过**不会**复现"JD 假平仓"那个坑（那个坑是拿历史 bar 反证）。
        s["_skip_exit_once"] = not _pen
        # ★★ E5（2026-10-21）：`entry` 是策略记账价，**不是**真实开仓价（兜底重算会把它
        #   写成当时现价：实测 BU 记 4998、FILL 真实 5003）。有 `open_price` 才敢说"开仓"。
        _op = plan.get("open_price")
        try:
            _px_txt = f"开仓 {float(_op):.1f}" if _op else \
                      f"记录entry {s['entry']:.1f}(非真实开仓价)"
        except Exception:
            _px_txt = f"记录entry {s['entry']:.1f}(非真实开仓价)"
        print(f"[{s['name']}][恢复] 云端持仓 {pos} 手 → 恢复**计划止损** {stop:.1f}"
              f"（{_px_txt}，已走 {s['bars_in']} 根，未重算）{_pen}", flush=True)
        s["rec"].event(
            "RECOVER",
            f"⚠️ 云端存在持仓 {pos} 手 → 已接管，止损**原样恢复**为开仓时的计划值 "
            f"{stop:.1f}（{_px_txt}，已走 {s['bars_in']} 根；未重算）。"
            + (f" | {_pen}" if _pen else ""))
    else:
        # ---------------- 路径 B：无记录才重算（兜底）----------------
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
        stop = sig.stop_price(d, c, a, lo if d > 0 else hi)
        s["stop"] = stop
        s["entry"] = c
        s["bars_in"] = 0
        s["recovered"] = True
        # ★★ E4（2026-10-21 14:06）：同样按「现价是否已越过 stop」决定跳过与否。
        #   ⚠️ 诚实说明：路径 B 的 stop 是用**现价 c** 算的（stop_price(d, c, a, swing)），
        #   多头的 stop = max(c-2a, lo-0.5a) 恒 < c、空头恒 > c ⇒ 本判定在数学上**恒为假**，
        #   即：路径 B 永远仍然跳过当根。保留它是为了防将来 stop_price 换口径（如引入
        #   交易所限价/固定止损）时这里不变成空洞。真正有效的那条在路径 A（用最后一笔
        #   计划止损比对现价，13:32 FU 就是被它救的）。
        _pen_b = ""
        if d > 0 and c <= stop:
            _pen_b = "⚠️ 现价已跌破重算止损"
        elif d < 0 and c >= stop:
            _pen_b = "⚠️ 现价已涨破重算止损"
        s["_skip_exit_once"] = not _pen_b
        print(f"[{s['name']}][恢复] ⚠️ 无计划止损记录（换月/老持仓/文件丢失）→ "
              f"按现价重算兜底 {stop:.1f}(ATR={a:.2f})；已固化，下次重启起沿用本条"
              f" | ★bars_in 归零 ⇒ 时间止损(MAX_HOLD=15)重新计时，本笔持仓可能被延长{_pen_b}",
              flush=True)
        s["rec"].event(
            "RECOVER",
            f"⚠️ 云端存在持仓 {pos} 手，本地无该合约的计划止损记录 → 按现价重算兜底 "
            f"≈{stop:.1f}（ATR={a:.2f}）。若为换月请人工核对；"
            f"该值已固化，之后重启不再重算。")
        # 立即固化 ⇒ 下一次重启走路径 A，不再重算（这是"只放宽一次"的自愈封口）
        _ok = _ps.save(_rd, s.get("key"), _sym, stop, c, d, abs(pos),
                       getattr(_q, "datetime", None) if _q is not None else None, src="SYNC")
        # ★ 2026-10-21 D1（#15 体检 P2-5）：原先返回值被丢弃 ⇒ 落盘失败静默，
        #   下次重启仍走路径 B 重算（止损可能再放宽一次）。这里只告警，不改交易。
        if not _ok:
            print(f"[{s['name']}]⚠️ PLANSTOP_FAIL 计划止损**固化失败**（路径B）"
                  f" key={s.get('key')} stop={stop:.1f} ⇒ 下次重启仍会重算", flush=True)
            try:
                s["rec"].event(
                    "PLANSTOP_FAIL",
                    f"⚠️ 计划止损落盘失败（路径B）key={s.get('key')} stop≈{stop:.1f}；"
                    f"下次重启将再次按现价重算（止损可能被放宽）")
            except Exception:
                pass

    # ★★ 换月移交：接管时同步「持仓在哪个合约」，并落盘（重启后据此判换月）
    s["underlying"] = _sym
    _save_underlying(_rd, s["key"], s["underlying"])


def _sync_plan_stop(s):
    """#15：让「计划止损_<key>.json」永远与当前持仓一致（每个节拍调一次）。

    三条规则：
      · 空仓 ⇒ 清掉本合约记录（否则下次重启会把上一笔的旧止损套到新仓上）
      · 有仓但无记录 ⇒ 补写（把兜底重算 / 外部接管的线固化，下次不再重算）
      · 有仓且有记录 ⇒ **不动**（记录才是真相；现价变动不该改写止损线）

    ⚠️ 只读写文件，**不改 `s["stop"]`、不参与任何交易判定** ⇒ 不触碰出场逻辑。
    ⚠️ 每 tick 都会被调用，用 `_ps_mark` 做脏检查，持仓状态没变就不碰磁盘。
    """
    try:
        _rd = s.get("record_dir")
        _key = s.get("key")
        if not _rd or not _key:
            return
        _q = s.get("q")
        _sym = getattr(_q, "underlying_symbol", None) if _q is not None else None
        pos = int(s.get("pos") or 0)
        _mark = (1 if pos > 0 else -1 if pos < 0 else 0, _sym)
        if s.get("_ps_mark") == _mark:
            return                       # 持仓状态未变 → 不重复读写

        if pos == 0:
            # ★ 只有**对账基线已建立**才敢删记录：启动初期 k 线还没就绪 / 首次对账
            #   还没跑，`pos` 一律是 0 —— 此时清文件会把「上一笔持仓的计划止损」误删，
            #   随后接管只能走重算兜底（等于 #15 没修）。
            # ⚠️ 这里**不写 `_ps_mark`**：否则基线建立后会被脏检查挡住，永远清不掉。
            if not s.get("_recon_base"):
                return
            _ps.clear(_rd, _key, _sym)
            s["_ps_mark"] = _mark
            return

        s["_ps_mark"] = _mark

        stop = float(s.get("stop") or 0)
        if not (stop == stop) or stop <= 0:
            return
        if _ps.load(_rd, _key, _sym, 1 if pos > 0 else -1) is not None:
            return                       # 已有记录：绝不改写
        _ok = _ps.save(_rd, _key, _sym, stop, float(s.get("entry") or 0),
                       1 if pos > 0 else -1, abs(pos),
                       getattr(_q, "datetime", None) if _q is not None else None,
                       src="SYNC")
        # ★ 2026-10-21 D1（#15 体检 P2-5）：每 tick 都会走到这里，故失败只告警前 3 次，防刷屏。
        if not _ok:
            _n = int(s.get("_ps_fail_n") or 0) + 1
            s["_ps_fail_n"] = _n
            if _n <= 3:
                print(f"[{s.get('name')}]⚠️ PLANSTOP_FAIL 计划止损补写失败"
                      f"（第 {_n} 次）key={_key} stop={stop:.1f} ⇒ 下次重启仍会重算", flush=True)
                try:
                    s["rec"].event(
                        "PLANSTOP_FAIL",
                        f"⚠️ 计划止损补写失败（第 {_n} 次）key={_key} stop≈{stop:.1f}；"
                        f"下次重启将再次按现价重算（止损可能被放宽）")
                except Exception:
                    pass
    except Exception:
        pass                             # 记账类动作，永不打断交易


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
                # ⚠️ 已知口径偏差（2026-10-21 执行员标注，**未改动既有统计**）：
                #   这里的符号只按「持仓方向」取，而"不利"应由**买卖方向**决定 ——
                #   对「多平」（卖出）和「空开」（卖出），成交价比决策价低才是更不利，
                #   本式却会算成负值（显示为有利）。#20 的新模块 `lib/slippage.py`
                #   用的是正确口径（is_buy 决定符号），历史 FILLX 行的符号不可与新表混用。
                #   是否统一改台账口径需 D 拍板（会改变历史对比），执行员不擅自改。
                slip_yuan = (avg - ref) * (1 if p["dir"] == "多" else -1) * \
                            (mult or 1) * p["hands"]
                rec.event("FILLX",
                          f"{p['dir']}{p['side']} {p['hands']}手 "
                          f"决策价={ref:.2f} 云端成交均价={avg:.2f} "
                          f"滑点≈{slip_yuan:+.2f}元")
                # ★★ #20（2026-10-21）：结构化落盘「信号根收盘 → 真实成交均价」。
                #   只由**真实成交**路径调用（本分支已确认持仓变化且均价可读），
                #   未成交/部分成交归 P0-1，这里不重复记账（D2 §6-20.4）。
                try:
                    _flags = ["重报"] if p.get("rs") else []
                    _slip.record(
                        s.get("record_dir"), s.get("name"), p.get("key"),
                        p.get("symbol") or q.underlying_symbol, p.get("dir"),
                        p.get("side"), p.get("hands"),
                        sig_close=p.get("sig_close") or ref,
                        fill_avg=avg, tick=float(s.get("tick") or 0),
                        mult=mult, flags=",".join(_flags))
                except Exception:
                    pass
                # ★★ E5（2026-10-21）：把**真实成交均价**回填进计划止损 json 的 `open_price`。
                #   只对「开」回填（平仓不动）；失败静默 —— 这是元数据，绝不能影响交易。
                if str(p.get("side")) == "开":
                    try:
                        _ps.set_open_price(
                            s.get("record_dir"), s.get("key"), avg,
                            symbol=p.get("symbol") or q.underlying_symbol)
                    except Exception:
                        pass
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

    ★★ 2026-10-20 P1-3：下单前先看**交易所涨跌停字段**。封板方向报单必废
    （涨停没人卖 ⇒ BUY 废；跌停没人买 ⇒ SELL 废），废单既占额度又制造
    「以为平了其实没平」。故封板方向直接不报，返回 ok=False（调用方按"本拍不报、
    下拍再试"处理，持仓与止损保持不变）。
    ⚠️ 只对**当前主连**判：换月移交时的旧合约（held_sym）拿不到它的 quote，
       不能用主连的涨跌停去判它（那会张冠李戴），故 `symbol != q.underlying_symbol` 时跳过。
    """
    if LIMIT_BOARD_CHECK:
        try:
            _q = s.get("q") if isinstance(s, dict) else None
            _qsym = str(getattr(_q, "underlying_symbol", "") or "") if _q is not None else ""
            if _qsym and _qsym == str(symbol):
                _blk, _bn = _lb.check(direction,
                                      getattr(_q, "last_price", None),
                                      getattr(_q, "upper_limit", None),
                                      getattr(_q, "lower_limit", None),
                                      tick=_tick_of(s.get("key")))
                if _blk:
                    return False, _bn, None
        except Exception:
            pass                      # 封板检查异常一律放行（不能让风控本身卡死交易）
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


def _send_close(api, s, symbol, side, dir_cn, hands, ref_price, bar_time, pos_before,
                sig_close=None):
    """发平仓单：上期所/能源按今昨仓自动拆成 1~2 张，其余交易所仍是 1 张 CLOSE。

    记账口径与改动前一致：每张单各写 FILL（=下单意图）/ ORDER_SENT / pending_fill。
    真实成交仍由 FILLX 核对云端持仓后确认，**FILL 不是成交凭证**。

    `sig_close`（#20，2026-10-21）：触发本次平仓**信号那根 K 线的收盘价**，
    仅用于滑点测算基准（D2 20.1）；缺省 None 时回落到 ref_price（不理想但不阻断）。
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
             # ★ #20：滑点基准 = 信号根收盘价（缺省回落到 ref_price，不阻断平仓）
             "sig_close": (float(sig_close) if sig_close else float(ref_price)),
             "expect_net": expect, "order": o, "offset": off,
             # ★ 2026-10-20 撤单重报需要：合约（撤/重报都要按合约+交易所）与 bar_time（重报单的幂等键）
             "symbol": symbol, "bar_time": bar_time})
        sent.append((off, int(vol)))
    return True, ("split" if len(plan) > 1 else plan[0][0]), sent


def _sync_cloud_pos(api, s, allow_trade, sig=None, fuse=None, acc=None):
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
        s["_recon_base"] = True            # ★ P0-2：本会话对账基线已建立
        return

    # ---- ★★ P0-2 对账偏离硬停机（2026-10-20）----
    #   原实现：账实不符 ⇒ 只写一条 FILLX / SYNC，然后继续交易 ⇒ 在错账上开仓。
    #   现在：非首次对账 + 本进程无在途委托 + 不在换月过渡 ⇒ 锁「只出不进」，
    #         弹窗 + 台账 HALT + 复用 fuse 闩锁（重启不复位，须人工核对后删文件）。
    #   ⚠️ 排除项（任务书明令）：FIX_WAIT 窗口已在本函数开头 return；
    #      在途委托（pending_fill 非空）与换月移交（rollover_pending）交给 _rh.plan 判 wait。
    #   ⚠️ 锁之前**先把持仓恢复给本地管理**：否则 s["pos"]=0 而云端有仓 ⇒ 该仓无止损、
    #      连出场都不会判（比不锁更危险）。「只出不进」的正确含义是能出、不能进。
    _eq = None
    try:
        _eq = float(acc["balance"]) if acc is not None else None
    except Exception:
        _eq = None
    _act, _why = _rh.plan(
        s.get("pos", 0), cp,
        sync_base=bool(s.get("_recon_base")),
        pending_orders=len(s.get("pending_fill") or []),
        rollover_active=bool(s.get("rollover_pending")),
        equity=_eq,
        price=(float(q.last_price) if q is not None and q.last_price == q.last_price else None),
        mult=s.get("mult"))
    if _act == "wait":
        # 过渡态：既不锁，**也不接管**（旧代码靠 FIX_WAIT 单挡，30s 后仍会假接管；
        # 现在有在途委托就明确不下结论，下拍再判）。
        if not s.get("_recon_wait_logged"):
            s["_recon_wait_logged"] = True
            rec.event("RECONWAIT", "对账暂缓（在途委托/换月过渡中）：" + _why)
        return
    s.pop("_recon_wait_logged", None)
    if _act == "halt":
        if fuse is not None:
            fuse["halted"] = True
            fuse["reason"] = "对账偏离硬停机：" + _why
            if fuse.get("save"):
                fuse["save"]()             # 闩锁立即落盘：重启也不自动复位
        rec.event("HALT", "⚠️ 对账偏离硬停机 → 只出不进，" + _why)
        print(f"[{s['name']}][HALT] ⚠️ {_why}", flush=True)
        if not s.get("_halt_logged"):
            s["_halt_logged"] = True
            _popup("MA13 模拟盘：⚠️ 对账偏离 → 只出不进",
                   f"{s['name']}：{_why}\n\n"
                   "已自动执行：停止一切新开仓（已有持仓照常按止损/时间止损出场）。\n"
                   "程序不会自动恢复。请核对快期 App 的真实持仓后人工处理。\n\n"
                   "确认复位方法：\n"
                   "  1. 关闭本程序窗口\n"
                   "  2. 删除文件 策略\\06_MA13超短线\\模拟盘\\台账\\熔断状态.json\n"
                   "  3. 重新启动")
        # 把云端持仓认回本地，让止损与出场判定继续生效（只出不进 ≠ 脱管）
        _c = float(q.last_price or 0) if q is not None else 0.0
        _a = s.get("last_atr") or 0.0
        if not (_a == _a and _a > 0):
            try:
                _a = (sig or _SIG).atr_at(s["k"])
            except Exception:
                _a = 0.0
        if cp != 0:
            _attach_stop_on_boot(s, cp, _c, _a, sig=sig)
        s["pos"] = cp
        s["recovered"] = False
        s["_recon_base"] = True
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
    s["_recon_base"] = True                # ★ P0-2：本会话对账基线已建立（下一拍起偏离即判）


def _bar_gate(s, bar_id):
    """这根 bar 该不该处理。返回 True=处理 / False=跳过（跳过时已更新 last_id）。

    ★ #19（2026-10-21 19:00，D1 直接改，用户授权「工程量小的决策员直接做」）：
      把这段从 `_process` 里**原样抽出**，唯一目的是**可单测**（规则 F：疏漏必须固化为断言）。
      **逻辑一字未改**：
        · 同一根 bar（id 相同）或未成形 bar（id<0）⇒ 跳过；
        · `skip_first` 为真 ⇒ 首根只认账不处理，随后清标记。
      为什么必须抽：`last_id` 只存内存、重启后为 -1，原来 `skip_first` 只在**重连**分支
      （本文件 :953）置位、**重启**分支不置位 ⇒ 重启后会把最后一根**已收盘**的 bar 当新 bar 重放。
      实测 2026-10-21 18:03:41：BU+8 / EG-7 依据 14:59:59.8 的 bar，在收盘后 3 小时成交。
    """
    try:
        bid = int(bar_id)
    except Exception:
        return False
    if bid == s.get("last_id") or bid < 0:
        return False                # 本品种没有新 bar（或 bar 尚未成形）
    s["last_id"] = bid
    if s.get("skip_first"):
        s["skip_first"] = False     # 启动/重连后的首根 bar：记 id 防重复，不处理
        return False
    return True


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
            # ---- ★★ P1-2 真实保证金占用率（2026-10-20）----
            #   `NOTIONAL_CAP` 是「名义÷权益」静态估算，**不是**交易所实际占用；
            #   这里读 Account.margin/balance 算真实占用率（强平线≈100%，
            #   60% 已是拒开线）。可见 + 逼近预警，不改任何仓位。
            #   ⚠️ 自动降仓**未实现**：那会动未平持仓，与红线1（硬停机不得自动平仓）
            #      冲突，须 D 单独授权并给出触发阶梯，见交接说明。
            _mg = float("nan")
            try:
                _mg = float(acc["margin"])
            except Exception:
                pass
            _mr = (_mg / bal) if (bal > 0 and _mg == _mg) else None
            # 权益快照（60秒一次，独立于熔断）：首日对账要靠它核对「台账盈亏 vs 账户权益」
            _now = time.time()
            if _now - s.get("eq_ts", 0) >= 60:
                s["eq_ts"] = _now
                _extra = f" 占用率={_mr:.1%}" if _mr is not None else " 占用率=na"
                s["rec"].event("EQUITY", f"权益={bal:.2f} 持仓={s['pos']}{_extra}")
            if _mr is not None and _mr >= MARGIN_WARN_RATIO:
                if _now - s.get("_mg_warn_ts", 0) >= MARGIN_WARN_REPEAT_SEC:
                    s["_mg_warn_ts"] = _now
                    _lvl = "已拒开仓" if _mr >= OPS_MAX_MARGIN_RATIO else "逼近拒开线"
                    s["rec"].event("MARGINWARN",
                                   f"保证金占用 {_mr:.1%}（{_lvl}，上限 "
                                   f"{OPS_MAX_MARGIN_RATIO:.0%}） 保证金={_mg:.0f} "
                                   f"权益={bal:.0f}")
                    print(f"[{s['name']}][MARGINWARN] 保证金占用 {_mr:.1%} {_lvl}",
                          flush=True)
            # ---- ★★ P1-1 单日亏损阀（账户级，节流调用）----
            #   只更新闩锁状态；**真正的拦截在下方开仓闸门**（`_open_ok`），
            #   平仓/止损/撤单三条通道完全不经过这里 ⇒ 不可能被本阀挡住。
            _dlg = acc_box.get("dloss")
            if _dlg is not None and (_now - float(acc_box.get("_dl_ts") or 0.0)
                                     >= DLOSS_UPDATE_SEC):
                acc_box["_dl_ts"] = _now
                if _dlg.update(bal) and not s.get("_dl_logged"):
                    s["_dl_logged"] = True
                    s["rec"].event("DAILYSTOP",
                                   "⚠️ 单日亏损阀触发 → 只停加仓（不改止损/不提前平仓）| "
                                   + _dlg.reason())
                    print(f"[{s['name']}][DAILYSTOP] {_dlg.reason()}", flush=True)
    # 每个事件都刷新心跳缓存（价格实时；tgt 在信号节拍里更新）
    _lp = q.last_price
    sh["dt"] = q.datetime
    sh["price"] = float(_lp) if _lp and not math.isnan(_lp) else sh["price"]

    # ---- 云端持仓对账：必须在「无新bar 就return」之前 ----
    # 每次 wait_update 都问一次云端，不依赖新 bar（休市/ 重启后首个 bar 到来前
    # 也必须生效，否则新仓无人接管）。只在不一致时才动手，见函数内注释。
    # ---- ★ P1-3 封板字段可读性自证（每腿仅一次）----
    #   任务书要求「先确认实盘行情模式能否读 upper_limit/lower_limit」。
    #   源码级依据：tqsdk 3.10.2 `objs.py:81` 定义该字段、`api.py:3203` 明写
    #   「回测时 upper_limit/lower_limit 为 nan」⇒ 实盘行情可读。
    #   但不能只靠文档：这里在真跑环境里打印一次实际读到的值（可读=True/False），
    #   重启后看日志即可确认，不需要额外登录一次账号（单账号单会话，额外登录会踢掉本进程）。
    if s.get("_lb_probe") is None:
        _u_l, _d_l = getattr(q, "upper_limit", None), getattr(q, "lower_limit", None)
        _rok = _lb.readable(_u_l, _d_l)
        s["_lb_probe"] = bool(_rok)
        _msg = (f"封板检查：upper={_u_l} lower={_d_l} ⇒ "
                + ("可读，封板方向将不报单" if _rok else "不可读 → 不拦（放行，等字段就绪）"))
        rec.event("LIMITCHK", _msg)
        print(f"[{s['name']}][LIMITCHK] {_msg}", flush=True)

    _sync_cloud_pos(api, s, allow_trade, sig=sig, fuse=fuse, acc=acc)
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
    if not _bar_gate(s, bar["id"]):
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
                    float(_lp), q.datetime, pos,
                    # ★ #20：收盘出场有明确的"信号根" ⇒ 用它的收盘价作滑点基准
                    sig_close=c)
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
    # ★★ P1-1 单日亏损阀（2026-10-20）：触阈后**只拒绝新开仓**。
    #   本判断只出现在"开仓"这条路上；平仓/止损/撤单三条通道在别处，不经过它。
    _dlg2 = acc_box.get("dloss")
    if _dlg2 is not None and _dlg2.blocked:
        _open_ok = False
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
    if pos == 0 and dir_trend != 0 and _dlg2 is not None and _dlg2.blocked:
        # 让复盘看得见「有信号、但被停加仓阀拦住」（不写 SIGNAL，避免被当成已开仓）
        _dcn = "多" if dir_trend > 0 else "空"
        rec.event("RISKBLOCK", f"停加仓阀拒开{_dcn}（当日不再新开仓）| {_dlg2.reason()}")
        print(f"[{s['name']}][RISKBLOCK] 停加仓阀拒开{_dcn}：{_dlg2.reason()}", flush=True)
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
            # ★★ #18 P1-A（2026-10-21 18:20，D1 审计）：**真相源回填**。
            #   `sh["pos"]` 只在 `_process` 末尾（约 2413 行）才写成 s["pos"]，
            #   而同一拍里「止损平仓 → 反手开仓」时 s["pos"] 已变 0、sh 还是旧持仓 ⇒
            #   check_limit 里 sym_before(旧 7 手) + new_notional(新 8 手) **双计**：
            #     实测 BU 748950 > 0.45×权益 443264 ⇒ 误拒（真实 399440 应放行）。
            #   今天 BU×3 + EG×3 = 6/6 全是这条造成的假拦截。
            #   规则：凡在开仓判定前读 shared 的地方，先把本品种写成真相源。
            _sync_shared_pos(shared, s)
            # ★ 运维闸门（2026-10-17）：只挡**开仓**，平仓永不被挡。异常一律放行。
            _ok, _why = _risk_check(
                s, acc, hand, shared=shared,
                side_sign=(1 if dir_trend > 0 else -1),
                price=(float(_lp) if _lp and not math.isnan(_lp) else None),
                day=str(_rs.trading_day()))       # #22-A：长假前降级判定要用交易日
            if not _ok:
                rec.event("RISKBLOCK", f"风控拒开 {dir_cn} {hand}手 | {_why}")
                print(f"[{s['name']}][RISKBLOCK] {_why}", flush=True)
                # ★★ #18 P1-A 第 2 件（必须与上面回填一起做，否则只修双计、没修锁死）：
                #   本条 return 会跳过 `_process` 末尾的 sh.update ⇒ sh["pos"] 永远停在
                #   上一根 bar 的值。一旦「止损平仓 → 反手被拒」，该品种的鬼腿就再也
                #   清不掉，从此**永久被判超限**（今天 BU/EG 14:44 之后就是这样锁死的）。
                #   所以拒开前必须把真相源写回 shared。
                try:
                    _p = int(s.get("pos") or 0)
                    sh.update({"dt": q.datetime, "price": c, "pos": _p,
                               "tgt": (1 if _p > 0 else -1 if _p < 0 else 0)})
                except Exception:
                    pass
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
                 # ★ #20（2026-10-21）：**信号根收盘价**单独带下去。
                 #   `ref_price` 是下单瞬间的 last_price（报价，不是基准），
                 #   D2 20.1 定滑点基准 = 信号那根已收盘 K 线的收盘价 ⇒ 必须显式传 `c`。
                 "sig_close": float(c),
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
            # ★★ #15（2026-10-21）：开仓即把**这条止损线本身**落盘。
            #   重启/接管时原样读回 —— 不再用「现价 ∓ 1.15×ATR」重算（那会放宽止损）。
            _ok = _ps.save(s.get("record_dir"), s.get("key"), q.underlying_symbol,
                           stop_new, c, 1 if dir_trend > 0 else -1, hand, q.datetime)
            # ★ 2026-10-21 D1（#15 体检 P2-5）：这是最关键的一次落盘 —— 失败即意味着
            #   本笔仓在下次重启时会走"重算兜底"（止损可能被放宽）。原先返回值被丢弃，
            #   失败无任何痕迹。这里只告警 + 记台账，不改交易行为。
            if not _ok:
                print(f"[{s['name']}]⚠️ PLANSTOP_FAIL **开仓落盘失败** key={s.get('key')} "
                      f"sym={q.underlying_symbol} stop={stop_new:.1f} hand={hand} "
                      f"⇒ 下次重启将按现价重算（止损可能被放宽）", flush=True)
                try:
                    s["rec"].event(
                        "PLANSTOP_FAIL",
                        f"⚠️ 开仓计划止损落盘失败 key={s.get('key')} "
                        f"sym={q.underlying_symbol} stop≈{stop_new:.1f} hand={hand}；"
                        f"下次重启将按现价重算（止损可能被放宽）")
                except Exception:
                    pass
            s["_ps_mark"] = (1 if dir_trend > 0 else -1, q.underlying_symbol)
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
    ap.add_argument("--drill-dailyloss", action="store_true",
                    help="单日亏损阀演练：强制置为「当日已触阀」（纯内存、不落盘、重启即恢复），"
                         "用于验证只拒新开仓/止损不动/平仓放行")
    ap.add_argument("--drill-holidayclose", action="store_true",
                    help="#22-B 长假强平演练：只打印「将平哪些品种手数」，不发任何单、"
                         "不写已执行标记（D2 §8-③ 安全约束①）")
    args = ap.parse_args()

    # ★★ A9（2026-10-20 派单 T3）：核心 stdout/stderr 落 台账/核心输出_YYYYMMDD.log（按日切）。
    #   看门狗用 DETACHED_PROCESS + stdout=DEVNULL 拉起 ⇒ 原生输出被丢弃；
    #   此处**在 Python 层**把 sys.stdout/stderr 重定向到带日期的日志文件，
    #   直接覆盖继承来的 DEVNULL（fd 无关，只换对象）。新增、try/except 包裹，
    #   任何异常都退回原 stdout，绝不影响交易。下次（非交易时段）重启生效。
    def _install_core_log():
        try:
            _logdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "台账")
            os.makedirs(_logdir, exist_ok=True)

            class _DayLog:
                def __init__(self):
                    self._date = None
                    self._f = None
                    self._tee = sys.__stdout__          # 原始 stdout（DEVNULL，仅保底）
                    self._open()
                def _open(self):
                    d = _dt.date.today().strftime("%Y%m%d")
                    if d == self._date and self._f is not None:
                        return
                    if self._f is not None:
                        try:
                            self._f.close()
                        except Exception:
                            pass
                    self._date = d
                    self._f = open(os.path.join(_logdir, f"核心输出_{d}.log"),
                                   "a", encoding="utf-8", buffering=1)
                def write(self, s):
                    self._open()
                    try:
                        self._f.write(s)
                    except Exception:
                        pass
                    try:
                        self._tee.write(s)
                    except Exception:
                        pass
                def flush(self):
                    try:
                        self._f.flush()
                    except Exception:
                        pass
                    try:
                        self._tee.flush()
                    except Exception:
                        pass
            _dl = _DayLog()
            sys.stdout = _dl
            sys.stderr = _dl
            print(f"[核心日志] stdout/stderr 已重定向到 台账/核心输出_{_dt.date.today().strftime('%Y%m%d')}.log")
        except Exception as _e:
            try:
                print(f"[核心日志] 重定向失败（不影响交易）：{type(_e).__name__}: {_e}", flush=True)
            except Exception:
                pass
    _install_core_log()

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
                       drill_fuse=args.drill_fuse, drill_dloss=args.drill_dailyloss,
                       drill_holidayclose=args.drill_holidayclose)
    finally:
        _释放核心锁()
