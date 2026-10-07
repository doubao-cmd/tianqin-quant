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

from tqsdk import TqApi, TqSim, TqAuth
from lib.engine import _LiveRecorder
import config_account
import config_symbols

MA_N, SLOPE_N, ATR_N = 13, 5, 14
RETRACE_TOL, STOP_BUFFER, STOP_CAP, MAX_HOLD = 0.5, 0.5, 2.0, 15
RISK_PCT, SLIP, HAND_MIN, HAND_MAX = 0.01, 1, 1, 8
NOTIONAL_CAP = 2.0   # 名义敞口上限（倍权益）：手数 ≤ 权益×2÷每手名义价值（与回测同步 2026-10-07）
INIT = 200000.0
RISK_EQUITY = 200000.0     # 兜底：账户权益取不到时回落到 20 万（2026-10-07 起
                           # 正常路径用 TqSim 实时权益，与回测复利口径对齐）
KSEC = 300
DD_FUSE = 0.20             # 账户级回撤熔断线（自峰值回撤 20%）。纯灾难保护阀，
                           # 非策略参数；数值为占位初值，SOP 欠账#1 用真数据定稿。
RECONNECT_WAIT = 10   # 断线后等待秒数（重连前歇一口气，避免撞上限流）

DEFAULT_SYMBOLS = ["P", "EB"]   # 任务A 判定的双候选


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
    """建立会话并完成全部品种订阅；失败时关闭半开会话再抛出。"""
    api = TqApi(TqSim(INIT), auth=TqAuth(user, pwd), web_gui=web_gui)
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
        s.update({"pos": 0, "entry": 0.0, "stop": 0.0, "bars_in": 0,
                  "last_id": -1, "skip_first": False})
        def _hand_of(atr, equity, price=None, _s=s):
            if not atr or math.isnan(atr) or atr <= 0:
                return HAND_MIN
            if equity is None or math.isnan(equity) or equity <= 0:
                equity = RISK_EQUITY          # 兜底：权益取不到才走（正常不会）
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
                       f"手数口径=当前权益x1%夹[1,8]+名义敞口≤2倍权益(20261007) 账号=模拟盘专用")

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
    # 直到人工删除 熔断状态.json（=确认复位，峰值从 20 万重新计）再重启。
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
    acc_box = {"acc": None}               # 当前会话的 TqSim 账户对象（随重连刷新）
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
        # 熔断线 = peak×(1−DD_FUSE)=16万；演练取其 90%（=14.4万）⇒ 必定触发。
        # ⚠️ 首版这里写成 peak×(1−DD_FUSE/2)=18万，落在熔断线之上 ⇒ 演练没触发，
        #    是演练数学错了、不是熔断逻辑错。演练的意义就在这类错误。
        fuse["peak"] = INIT
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
                    api.wait_update()
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


def _process(api, s, allow_trade, shared, acc_box, fuse):
    """单品种的一个处理节拍：新 bar 出现（=前一根收盘）才算信号。"""
    q, k, rec = s["q"], s["k"], s["rec"]
    sh = shared[s["key"]]
    if len(k) == 0:
        return

    # ---- 账户级回撤熔断（纯保护结构，非策略参数）----
    # 观察/交易模式都检查：观察模式 TqSim 权益恒为初值，永远不会触发；
    # 交易模式下权益=真实 TqSim 撮合账本。触发后闩死：停新开仓、持仓照常
    # 按止损/时间止损出场，不自动恢复，必须人工重启进程。
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
    slope = ma[i - 1] - ma[i - 1 - SLOPE_N]
    dir_trend = 1.0 if slope > 0 else (-1.0 if slope < 0 else 0.0)
    touch = abs(close[i - 1] - ma[i - 1]) <= RETRACE_TOL * a

    # ---- 出场（对齐回测：止损/时间止损）----
    if pos > 0 and (l <= s["stop"] or s["bars_in"] >= MAX_HOLD):
        if allow_trade:
            api.insert_order(q.underlying_symbol, "SELL", "CLOSE", abs(pos))
            rec.fill("多", "平", abs(pos), float(_lp), q.underlying_symbol)
        rec.stoploss("多", s["stop"], c,
                     f"止损触发={l <= s['stop']} 时间止损={s['bars_in'] >= MAX_HOLD}")
        print(f"[{s['name']}][EXIT] 多 {abs(pos)}手 止损={l <= s['stop']} "
              f"时间={s['bars_in'] >= MAX_HOLD} @≈{c:.1f}", flush=True)
        s["pos"] = 0; s["bars_in"] = 0
    elif pos < 0 and (h >= s["stop"] or s["bars_in"] >= MAX_HOLD):
        if allow_trade:
            api.insert_order(q.underlying_symbol, "BUY", "CLOSE", abs(pos))
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
            stop_new = max(c - STOP_CAP * a, swing_low - STOP_BUFFER * a)
        else:
            swing_high = float(np.max(high[max(0, i - SLOPE_N):i]))
            stop_new = min(c + STOP_CAP * a, swing_high + STOP_BUFFER * a)
        rec.signal(dir_cn, hand, c, q.datetime, c, a,
                   f"MA13回踩顺势 stop≈{stop_new:.1f}" + ("" if allow_trade else " 【观察模式不报单】"))
        if allow_trade and hand > 0:
            api.insert_order(q.underlying_symbol,
                             "BUY" if dir_trend > 0 else "SELL", "OPEN", hand)
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
                    help="品种key，逗号分隔，如 P,EB")
    ap.add_argument("--trade", action="store_true",
                    help="开真实下单（TqSim 撮合）；缺省为观察模式")
    ap.add_argument("--gui", action="store_true", help="开 web_gui 图形界面")
    ap.add_argument("--drill-fuse", action="store_true",
                    help="熔断演练：启动后强制触发一次回撤熔断（会闩死，需人工复位）")
    args = ap.parse_args()
    run_live_multi(symbol_keys=[x.strip().upper() for x in args.symbols.split(",") if x.strip()],
                   allow_trade=args.trade, web_gui=args.gui,
                   drill_fuse=args.drill_fuse)
