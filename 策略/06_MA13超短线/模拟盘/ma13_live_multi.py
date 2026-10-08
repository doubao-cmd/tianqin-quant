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
import sys, os, time, math, json, threading
import numpy as np

_ROOT = os.path.dirname(os.path.abspath(__file__))
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tqsdk import TqApi, TqSim, TqKq, TqAuth
from lib.engine import _LiveRecorder
import config_account
import config_symbols

MA_N, SLOPE_N, ATR_N = 13, 5, 14
RETRACE_TOL, STOP_BUFFER, STOP_CAP, MAX_HOLD = 0.5, 0.5, 2.0, 15
RISK_PCT, SLIP, HAND_MIN, HAND_MAX = 0.01, 1, 1, 8
NOTIONAL_CAP = 2.0   # 名义敞口上限（倍权益）：手数 ≤ 权益×2÷每手名义价值（与回测同步 2026-10-07）
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

DEFAULT_SYMBOLS = ["JM", "M", "JD", "P", "SR"]   # ★2026-10-08 晚从 ["P","EB"] 改来
# ★★ 这是品种列表的**第 5 处同步点**，之前漏了（bugfix 19:55）。
#   为什么它危险：`__main__` 的 `--symbols` 用 `",".join(DEFAULT_SYMBOLS)` 当
#   **argparse 的 default**，即命令行最终默认值。
#   ⇒ 谁只要不传 --symbols（手敲 python 调试 / 将来任何新写入口忘了传），
#     就按这份列表下单。原先留的["P","EB"] 是**已淘汰的旧双候选**，
#     P 和 EB 都不在当前五腿里 ⇒ 会静默去下两条**不在计划内**的真单。
#   ⚠️ 我上一轮自称「四个同步点全改齐」是漏数了：bat 有 3 处 + watchdog 1 处，
#      这里的 DEFAULT_SYMBOLS 是被 `__main__` 引用的**第 5 处**。
#      同款隐患另见 工具/_查bug_20261008.py（已加成自动检查项）。


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


def run_live_multi(symbol_keys=None, allow_trade=False, web_gui=False,
                   record_dir=None, heartbeat_sec=300, drill_fuse=False):
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
        # ★ recovered：标记「这个持仓是从云端接管来的，不是本进程开的」。
        #   接管时必须补挂止损（云端不存我们的止损线），且退出时要打醒目事件。
        s.update({"pos": 0, "entry": 0.0, "stop": 0.0, "bars_in": 0,
                  "last_id": -1, "skip_first": False, "recovered": False,
                  "last_order_ts": 0.0})
        def _hand_of(atr, equity, price=None, _s=s):
            if not atr or math.isnan(atr) or atr <= 0:
                return HAND_MIN
            # ★ 2026-10-08：一律用 RISK_EQUITY(20万)，**忽略传入的账户权益**。
            #   切 TqKq 后账户余额约 100 万，直接用它算手数会让名义敞口放大 5 倍
            #   且与回测脱钩。调用方仍会传账户权益（供台账展示），但这里不采信。
            equity = RISK_EQUITY
            h = int(round((equity * RISK_PCT) / (atr * _s["tick"] * _s["mult"])))
            h = max(HAND_MIN, min(HAND_MAX, h))
            # 名义敞口夹（与回测 hand_plan 同口径）：price = 当前最新价（决策时点，非未来数据）
            if price and price > 0 and not math.isnan(price):
                名义手数 = int((equity * NOTIONAL_CAP) / (price * _s["mult"]))
                h = min(h, max(HAND_MIN, 名义手数))
            return h
        s["hand_of"] = _hand_of
        s["rec"] = _LiveRecorder(record_dir, s["name"], heartbeat_sec=heartbeat_sec)

    names = " / ".join(s["name"] for s in specs)
    for s in specs:
        s["rec"].start(f"MA13 多品种实时盘启动 [{names}] allow_trade={allow_trade} "
                       f"手数基数=锁定20万(与回测对齐) x1%夹[1,8]+名义敞口≤2倍(20261008切TqKq云端) "
                       f"账户=TqKq云端快期模拟(手机可同步)")

    # ---- 心跳独立线程：休市/无行情时照写，防台账断层误报 ----
    # 铁律：线程内绝不触碰 tqsdk 对象（q/k/api），只读 shared 里的纯标量缓存。
    # tqsdk 对象跨线程使用会返回全零帧（项目既有教训）。
    shared = {s["key"]: {"dt": "", "price": 0.0, "pos": 0, "tgt": 0} for s in specs}
    import threading

    def _hb_loop():
        while True:
            time.sleep(5)
            for s in specs:
                sh = shared[s["key"]]
                s["rec"].heartbeat(sh["dt"], sh["price"], sh["pos"], sh["tgt"])

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
                        _process(api, s, allow_trade, shared, acc_box, fuse)
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


def _cloud_pos(api, s):
    """读云端真实持仓（正=多/ 负=空）。读不到返回 None（调用方跳过同步）。

    ⚠️ 云端合约代码 = q.underlying_symbol（已由引擎在_quote_handler 里展开为
       具体月份，如 DCE.p2701）。用主连代码查会返回空持仓 —— 必须在订阅
       underlying_symbol 之后才有意义。
    """
    q = s.get("q")
    if q is None:
        return None
    sym = q.underlying_symbol
    if not sym:
        return None
    try:
        p = api.get_position(sym)
        return int(p["pos"])          # tqsdk 的pos 本身就是带符号净持仓
    except Exception:
        return None


def _stop_price(dir_trend, entry, a, swing):
    """★ 唯一止损价计算入口（开仓 / 恢复补挂 都必须走这里，逐字一致）。

    原式：多 = max(entry − 2ATR, swing_low − 0.5ATR) / 空 = min(entry + 2ATR, swing_high + 0.5ATR)
    ⚠️【2026-10-08 bugfix】原式有缺陷：swing 取「近 SLOPE_N 根」的极值，而 entry 是**当根开盘价**。
       跳空低开（开多）时 open[i] 可能低于 swing_low ⇒ max() 选中 swing_low 那一支
       ⇒ stop > entry ⇒ 下一根 low<=stop 立刻成立 ⇒ 以一个**从未存在过的价格**平仓、凭空盈利。
       实测 2016-01~02 焦煤：184 笔开仓里 69 笔开仓即矛盾，占全部止损单的 45.6%。
    ⇒ 止损价必须夹紧在入场价的**不利侧**（至少留一跳），否则退回兜底 c∓2ATR。
    """
    tick = 1.0
    if dir_trend > 0:
        stop = max(entry - STOP_CAP * a, swing - STOP_BUFFER * a)
        if stop >= entry - tick:
            stop = entry - STOP_CAP * a
    else:
        stop = min(entry + STOP_CAP * a, swing + STOP_BUFFER * a)
        if stop <= entry + tick:
            stop = entry + STOP_CAP * a
    return stop


def _attach_stop_on_boot(s, pos, c, a):
    """启动/重连后补挂止损（对齐 engine.run_live_boll 的 attach_stop_on_start）。

    云端**不存我们的止损线**（那是本策略自算的），所以恢复出的持仓必须重算一条。
    ⚠️ 算法必须与开仓处**逐字一致** ⇒ 统一走 _stop_price()，禁止在这里另写一份。
       swing 取近 SLOPE_N 根的极值。

    这不是新增过滤条件，纯粹是「止损线丢了必须补回来」，否则持仓会裸奔。
    """
    if pos == 0 or a != a or a <= 0:
        return
    c = float(s["q"].last_price or c)
    # 近SLOPE_N 根的高低极值（与开仓处同一口径；取不到就用现价兜底）
    try:
        k = s["k"]
        lo = float(np.min(np.array(k["low"][max(0, len(k) - SLOPE_N):], dtype=float)))
        hi = float(np.max(np.array(k["high"][max(0, len(k) - SLOPE_N):], dtype=float)))
        if lo != lo or lo <= 0:
            lo = c
        if hi != hi or hi <= 0:
            hi = c
    except Exception:
        lo = hi = c
    stop = _stop_price(1 if pos > 0 else -1, c, a,
                       lo if pos > 0 else hi)
    s["stop"] = stop
    s["entry"] = c
    s["bars_in"] = 0
    s["recovered"] = True
    print(f"[{s['name']}][恢复] 检测到云端已有持仓 {pos} 手 → "
          f"以云端为准接管，止损补挂至≈{stop:.1f}(ATR={a:.2f})", flush=True)
    s["rec"].event("RECOVER",
                   f"⚠️ 云端存在持仓 {pos} 手，本进程原以为空仓 → 已接管并补挂止损"
                   f"≈{stop:.1f}（ATR={a:.2f}，算法与开仓一致）。若这是意外持仓请人工核对。")


def _now_atr(k, at=None):
    """就地算当前 ATR，口径与 _process 主循环**完全一致**（Wilder 式 TR 均值）。

    为什么要单独算：接管的止损线必须在「还没走到新 bar」的当下就能挂上，
    而主循环的 a 只在有 bar 时才更新。at 指定第几根（默认最后一根的前一根，
    与主循环 `a = atr_arr[i-1]` 的取值一致）；给不出有效值返回 nan。
    """
    try:
        high = k["high"].to_numpy(float); low = k["low"].to_numpy(float)
        close = k["close"].to_numpy(float)
    except Exception:
        return float("nan")
    n = len(close)
    if n < 3:
        return float("nan")
    prev_c = np.roll(close, 1); prev_c[0] = close[0]
    tr = np.maximum(high - low,
                    np.maximum(np.abs(high - prev_c), np.abs(low - prev_c)))
    i = (n - 1) if at is None else at
    if i < 2 or i >= n:
        return float("nan")
    return float(np.mean(tr[max(1, i - ATR_N):i]))


def _sync_cloud_pos(api, s):
    """★ 云端持仓对账（切 TqKq 后的核心保护，见常量区SYNC_CLOUD 注释）。

    以**云端为唯一真相源**，纠正进程内存里的 s["pos"]。只在不一致时动手：
      ① 云端有仓、本进程以为空仓 ⇒ 接管 + 补挂止损（否则持仓裸奔 / 可能重复开仓）
      ② 云端空仓、本进程以为有仓 ⇒ 清零（止损线是本进程算的，云端已平就不该再管）

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
    cp = _cloud_pos(api, s)
    if cp is None:
        return# 读不到云端持仓（尚未订阅 / 连接未就绪）⇒ 本次不动，静默跳过
    if cp == s["pos"]:
        # 已一致。若是上一轮接管的持仓，本根起按正常持仓维护（止损已挂好）。
        if cp != 0 and s.get("recovered"):
            s["recovered"] = False
        return
    rec = s["rec"]
    if cp != 0:
        # 接管：云端有仓而本进程不知道 ⇒ 补挂止损（云端不存我们的止损线）
        c_now = float(s["q"].last_price or 0)
        a_now = s.get("last_atr") or 0.0
        # ★ 若last_atr 还是 0（进程刚起、ATR 尚未算出），就地算一次。
        #   否则 _attach_stop_on_boot 会在 `a <= 0` 处早退⇒ **接管了持仓却没挂
        #   止损**，等于白接管（实测踩到：云端 1 手已接管，止损仍为 0）。
        if not (a_now == a_now and a_now > 0):
            a_now = _now_atr(s["k"])
            if a_now == a_now and a_now > 0:
                s["last_atr"] = a_now
        _attach_stop_on_boot(s, cp, c_now, a_now)
    else:
        # 退出：被恢复出来的持仓已在云端消失（可能人工平仓/被其他程序平掉）
        rec.event("SYNC", "云端持仓已清零（可能人工平仓）→ 本进程同步退出该持仓，"
                          "并撤销其止损监控")
        print(f"[{s['name']}][同步] 云端持仓已清零 → 同步退出，撤销止损监控", flush=True)
        s["entry"] = 0.0; s["stop"] = 0.0; s["bars_in"] = 0
    s["pos"] = cp
    s["recovered"] = False


def _process(api, s, allow_trade, shared, acc_box, fuse):
    """单品种的一个处理节拍：新 bar 出现（=前一根收盘）才算信号。"""
    q, k, rec = s["q"], s["k"], s["rec"]
    sh = shared[s["key"]]
    if len(k) == 0:
        return

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

    # ---- 云端持仓对账：必须在「无新 bar 就return」之前 ----
    # 每次 wait_update 都问一次云端，不依赖新 bar（休市/ 重启后首个 bar 到来前
    # 也必须生效，否则新仓无人接管）。只在不一致时才动手，见函数内注释。
    _sync_cloud_pos(api, s)

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
    prev_c = np.roll(close, 1); prev_c[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_c), np.abs(low - prev_c)))
    atr_arr = np.array([np.mean(tr[max(1, i - ATR_N):i]) if i >= 2 else tr[i] for i in range(n)])
    ma = np.array([np.mean(close[i - MA_N:i]) for i in range(MA_N, n)])
    ma = np.concatenate([np.full(MA_N, np.nan), ma])
    i = n - 1
    a = atr_arr[i - 1]
    s["last_atr"] = float(a) if a == a and a > 0 else s.get("last_atr", 0.0)
    slope = ma[i - 1] - ma[i - 1 - SLOPE_N]
    dir_trend = 1.0 if slope > 0 else (-1.0 if slope < 0 else 0.0)
    touch = abs(close[i - 1] - ma[i - 1]) <= RETRACE_TOL * a

    pos = s["pos"]
    if pos > 0 and (l <= s["stop"] or s["bars_in"] >= MAX_HOLD):
        if allow_trade:
            api.insert_order(q.underlying_symbol, "SELL", "CLOSE", abs(pos))
            s["last_order_ts"] = time.time()      # 等撮合窗口，防自开仓被误接管
            rec.fill("多", "平", abs(pos), float(_lp), q.underlying_symbol)
        rec.stoploss("多", s["stop"], c,
                     f"止损触发={l <= s['stop']} 时间止损={s['bars_in'] >= MAX_HOLD}")
        print(f"[{s['name']}][EXIT] 多 {abs(pos)}手 止损={l <= s['stop']} "
              f"时间={s['bars_in'] >= MAX_HOLD} @≈{c:.1f}", flush=True)
        s["pos"] = 0; s["bars_in"] = 0
    elif pos < 0 and (h >= s["stop"] or s["bars_in"] >= MAX_HOLD):
        if allow_trade:
            api.insert_order(q.underlying_symbol, "BUY", "CLOSE", abs(pos))
            s["last_order_ts"] = time.time()      # 等撮合窗口，防自开仓被误接管
            rec.fill("空", "平", abs(pos), float(_lp), q.underlying_symbol)
        rec.stoploss("空", s["stop"], c,
                     f"止损触发={h >= s['stop']} 时间止损={s['bars_in'] >= MAX_HOLD}")
        print(f"[{s['name']}][EXIT] 空 {abs(pos)}手 止损={h >= s['stop']} "
              f"时间={s['bars_in'] >= MAX_HOLD} @≈{c:.1f}", flush=True)
        s["pos"] = 0; s["bars_in"] = 0

    # ---- 开仓（对齐回测：趋势 + 回踩；熔断闩死期间禁止）----
    pos = s["pos"]
    if pos == 0 and dir_trend != 0 and touch and not fuse["halted"]:
        try:
            _eq = float(acc["balance"]) if acc is not None else float("nan")
        except Exception:
            _eq = float("nan")                # 权益读不到 → _hand_of 内兜底 20 万
        hand = s["hand_of"](a, _eq, float(_lp) if _lp and not math.isnan(_lp) else None)
        dir_cn = "多" if dir_trend > 0 else "空"
        if dir_trend > 0:
            swing_low = float(np.min(low[max(0, i - SLOPE_N):i]))
        else:
            swing_high = float(np.max(high[max(0, i - SLOPE_N):i]))
        stop_new = _stop_price(dir_trend, c, a,
                               swing_low if dir_trend > 0 else swing_high)
        rec.signal(dir_cn, hand, c, q.datetime, c, a,
                   f"MA13回踩顺势 stop≈{stop_new:.1f}" + ("" if allow_trade else " 【观察模式不报单】"))
        if allow_trade and hand > 0:
            api.insert_order(q.underlying_symbol,
                             "BUY" if dir_trend > 0 else "SELL", "OPEN", hand)
            s["last_order_ts"] = time.time()      # 等撮合窗口，防自开仓被误接管
            rec.fill(dir_cn, "开", hand, float(_lp), q.underlying_symbol)
            # 已修复单品种版 led 未定义的 NameError；账本对接见 运营SOP 欠账#6
        print(f"[{s['name']}][{'OPEN' if allow_trade else 'SIGNAL'}] {dir_cn} {hand}手 "
              f"@≈{c:.1f} 止损≈{stop_new:.1f} ATR={a:.2f}", flush=True)
        if allow_trade:
            s["pos"] = hand if dir_trend > 0 else -hand
            s["entry"] = c; s["stop"] = stop_new; s["bars_in"] = 0
    elif pos != 0:
        s["bars_in"] += 1

    tgt = int(dir_trend) if (s["pos"] == 0 and dir_trend != 0 and touch) else \
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
    run_live_multi(symbol_keys=[x.strip().upper() for x in args.symbols.split(",") if x.strip()],
                   allow_trade=args.trade, web_gui=args.gui,
                   drill_fuse=args.drill_fuse)
