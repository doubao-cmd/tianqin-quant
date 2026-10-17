# -*- coding: utf-8 -*-
"""ops.py — 运维可靠性内核（订单生命周期 / 幂等 / 原子状态 / 风控闸门 / 对账）

为什么要单独一层：
  现有 `_LiveRecorder` 记的是「程序做了什么」（START/SIGNAL/FILL/STOPLOSS），
  但 FILL 是**下单那一刻**的记账 —— `insert_order` 之后无条件写，拒单也照写，
  于是产生幻影仓（EB 9 笔、10-15 四条串价行）。本层把「意图」与「成交确认」拆开。

设计原则（三条硬约束）：
  1) **不改信号、不改手数**（`RiskGate` 是显式拒绝开仓，属保护而非策略）。
  2) **绝不打断交易**：本模块所有方法内部吞异常；调用方仍应包 try/except。
  3) **只增不删**：订单生命周期落 append-only CSV，供事后复盘。

对外接口：
  · atomic_write_json / read_json      —— 状态文件原子写（断电不留半个 JSON）
  · BarGate                            —— 同一根 bar 只触发一次（跨重启有效）
  · OrderLedger                        —— ORDER_SENT / FILL_CONFIRMED / ORDER_REJECTED
  · RiskGate                           —— 开仓前闸门（手数 / 保证金占用 / 日亏）
  · reconcile                          —— 本地持仓 vs 云端持仓差异清单

用法（策略侧）：
  from lib.ops import OrderLedger, BarGate, atomic_write_json
"""
import os
import csv
import json
import time
from datetime import datetime


# ===================== 原子状态 =====================
def atomic_write_json(path, obj, indent=2):
    """写 tmp -> fsync -> os.replace。断电/强杀不会留下半个 JSON。

    返回 True/False（失败不抛，调用方不必处理）。"""
    try:
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ===================== 幂等：同一根 bar 只触发一次 =====================
class BarGate:
    """每腿一个文件，记录「最后处理过的 bar 时间」。

    解决两个真实故障：
      · 进程重启后把**历史 bar** 当成实时 bar 再执行一遍；
      · 同一根 bar 因 `wait_update` 多次返回而被重复触发。
    """

    def __init__(self, path):
        self.path = path
        self.seen = read_json(path, {}) or {}
        if not isinstance(self.seen, dict):
            self.seen = {}

    def first(self, key, bar_time):
        """True = 这根 bar 还没处理过（可以动作）；False = 重复，跳过。"""
        if not bar_time:
            return True
        bt = str(bar_time)
        if str(self.seen.get(key) or "") == bt:
            return False
        self.seen[key] = bt
        atomic_write_json(self.path, self.seen)
        return True

    def get(self, key):
        return self.seen.get(key)


def leg_prefix(sym):
    """品种级前缀（孤儿扫描用）："DCE.p2701" -> "DCE.p"、"CZCE.SR701" -> "CZCE.SR"。

    ★ 不能用 `sym.rsplit('.', 1)[0]` —— 那会得到**交易所级**前缀（"DCE"），
      多腿并存时同交易所的其它品种持仓会被误判成孤儿、互相平仓（2026-10-15/17 事故根因）。
    """
    if not sym or "." not in str(sym):
        return ""
    return str(sym).rstrip("0123456789")


def find_orphan(pos_map, cur_sym, exclude=None):
    """从账户持仓里挑出「本品种旧月份」的孤儿仓；没有返回 None。

    pos_map: {symbol: net_pos}（多正空负）。纯函数，便于单测/CI。
    """
    prefix = leg_prefix(cur_sym)
    if not prefix:
        return None
    for sym, net in (pos_map or {}).items():
        sym = str(sym)
        if sym == cur_sym or sym == exclude or not sym.startswith(prefix):
            continue
        try:
            if int(net or 0) != 0:
                return sym
        except Exception:
            continue
    return None


# ===================== 订单生命周期 =====================
class OrderLedger:
    """三层记账：ORDER_SENT（意图）/ FILL_CONFIRMED（成交）/ ORDER_REJECTED（拒单）。

    ★ 统计与对账**只认 FILL_CONFIRMED**；FILL（既有台账行）保留为「意图时刻」，
      但不能再被当成成交。
    ★ 僵尸单 = 有 ORDER_SENT、超时后既无 CONFIRMED 也无 REJECTED 的单。
    """

    COLS = ["时间", "事件", "键", "合约", "方向", "开平", "手数", "价格", "原因"]
    OK_EVENTS = ("FILL_CONFIRMED", "ORDER_REJECTED", "ORDER_UNCONFIRMED")

    def __init__(self, path):
        self.path = path
        self._pend = {}          # key -> sent_ts
        self._load()

    def _load(self):
        """从既有 CSV 恢复未决单（进程重启后僵尸单仍可被发现）。"""
        try:
            if not os.path.exists(self.path):
                return
            with open(self.path, encoding="utf-8-sig", newline="") as f:
                for r in csv.DictReader(f):
                    ev, k = (r.get("事件") or ""), (r.get("键") or "")
                    if not k:
                        continue
                    if ev == "ORDER_SENT":
                        self._pend[k] = 0.0        # 时间未知 -> 视为很久以前
                    elif ev in self.OK_EVENTS:
                        self._pend.pop(k, None)
        except Exception:
            pass

    def _w(self, row):
        try:
            new = (not os.path.exists(self.path)) or os.path.getsize(self.path) == 0
            with open(self.path, "a", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(self.COLS)
                w.writerow(row)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass
            return True
        except Exception:
            return False

    def sent(self, key, symbol, dir_cn, offset_cn, hands, ref_price=0.0):
        self._pend[str(key)] = time.time()
        return self._w([_now(), "ORDER_SENT", key, symbol, dir_cn, offset_cn,
                        int(hands), f"{float(ref_price):.2f}", ""])

    def confirmed(self, key, price, hands=None, note=""):
        self._pend.pop(str(key), None)
        return self._w([_now(), "FILL_CONFIRMED", key, "", "", "",
                        "" if hands is None else int(hands),
                        "" if price is None else f"{float(price):.2f}", note])

    def rejected(self, key, reason, hands=None):
        self._pend.pop(str(key), None)
        return self._w([_now(), "ORDER_REJECTED", key, "", "", "",
                        "" if hands is None else int(hands), "", str(reason)])

    def unconfirmed(self, key, reason):
        """既没确认也没被拒（如云端均价未就绪）—— 单独一类，供人工核对，不冒充拒单。"""
        self._pend.pop(str(key), None)
        return self._w([_now(), "ORDER_UNCONFIRMED", key, "", "", "", "", "", str(reason)])

    def pending(self):
        return dict(self._pend)

    def zombies(self, timeout_sec=300.0):
        """超时未确认的 (key, 未决秒数)，按未决时长降序。"""
        now = time.time()
        out = [(k, now - t) for k, t in self._pend.items() if now - t >= timeout_sec]
        out.sort(key=lambda x: -x[1])
        return out


# ===================== 风控闸门 =====================
class RiskGate:
    """开仓前的硬闸门。**只在开仓路径调用**，平仓永不被挡（否则会卡住裸仓）。

    检查项（任一不过 -> 拒绝开仓）：
      1. 单笔手数上限        max_hands
      2. 账户保证金占用率    margin / balance <= max_margin_ratio
      3. 当日亏损比例        -daily_pnl / balance <= max_daily_loss
    """

    def __init__(self, max_hands=20, max_margin_ratio=0.60, max_daily_loss=0.03,
                 max_same_side_ratio=2.0):
        self.max_hands = int(max_hands)
        self.max_margin_ratio = float(max_margin_ratio)
        self.max_daily_loss = float(max_daily_loss)
        self.max_same_side_ratio = float(max_same_side_ratio)

    def check_open(self, hands, balance=None, margin=None, daily_pnl=None,
                   same_side_notional=None, order_notional=None):
        """返回 (ok: bool, reason: str)。参数缺失时对应检查跳过（不误杀）。"""
        if hands is None or hands <= 0:
            return False, "手数为 0"
        if hands > self.max_hands:
            return False, f"手数 {hands} > 上限 {self.max_hands}"
        try:
            if balance and margin is not None and balance > 0:
                r = float(margin) / float(balance)
                if r > self.max_margin_ratio:
                    return False, f"保证金占用 {r:.1%} > 上限 {self.max_margin_ratio:.0%}"
        except Exception:
            pass
        try:
            if balance and daily_pnl is not None and balance > 0:
                loss = -float(daily_pnl)
                if loss > 0 and loss / float(balance) >= self.max_daily_loss:
                    return False, f"当日亏损 {-loss / float(balance):.2%} ≥ 熔断线 {self.max_daily_loss:.1%}"
        except Exception:
            pass
        # 4. 同向敞口（多腿同向叠加）：已有同向名义 + 本笔 <= 上限 × 权益
        try:
            if balance and same_side_notional is not None and order_notional:
                r = (float(same_side_notional) + float(order_notional)) / float(balance)
                if r > self.max_same_side_ratio:
                    return False, (f"同向敞口 {r:.2f}× 权益 > 上限 "
                                   f"{self.max_same_side_ratio:.2f}×")
        except Exception:
            pass
        return True, ""


# ===================== 对账 =====================
def reconcile(local, cloud, tol=0):
    """本地持仓 vs 云端持仓，返回差异列表。

    local / cloud: {symbol: net_position}（多正空负）。
    返回 [{"symbol", "local", "cloud", "diff", "kind"}]，kind ∈ {missing_cloud, missing_local, diff}。
    """
    def norm(d):
        out = {}
        for k, v in (d or {}).items():
            try:
                iv = int(v)
            except Exception:
                continue
            if iv != 0:
                out[str(k)] = iv
        return out

    L, C = norm(local), norm(cloud)
    rows = []
    for sym in sorted(set(L) | set(C)):
        a, b = L.get(sym, 0), C.get(sym, 0)
        if abs(a - b) > max(0, int(tol)):
            kind = "missing_cloud" if (a and not b) else ("missing_local" if (b and not a) else "diff")
            rows.append({"symbol": sym, "local": a, "cloud": b, "diff": a - b, "kind": kind})
    return rows


# ===================== 自检 =====================
def selftest():
    import tempfile
    print("[selftest] ops.py（纯逻辑，无网络）")
    d = tempfile.mkdtemp(prefix="ops_")

    # 1) 原子写 / 读
    p = os.path.join(d, "state.json")
    assert atomic_write_json(p, {"a": 1})
    assert read_json(p) == {"a": 1}
    assert read_json(os.path.join(d, "nope.json"), {"x": 0}) == {"x": 0}
    assert not os.path.exists(p + ".tmp"), "tmp 未清理"

    # 2) BarGate：同一根 bar 只放行一次，且跨实例（重启）有效
    bg = BarGate(os.path.join(d, "bar.json"))
    assert bg.first("P", "2026-10-17 10:00:00") is True
    assert bg.first("P", "2026-10-17 10:00:00") is False
    assert bg.first("P", "2026-10-17 10:05:00") is True
    bg2 = BarGate(os.path.join(d, "bar.json"))        # 模拟重启
    assert bg2.first("P", "2026-10-17 10:05:00") is False
    assert bg2.first("P", "2026-10-17 10:10:00") is True

    # 3) OrderLedger：三层记账 + 僵尸识别 + 跨重启恢复
    lp = os.path.join(d, "orders.csv")
    L = OrderLedger(lp)
    L.sent("k1", "DCE.p2701", "多", "开", 4, 9477.0)
    L.sent("k2", "DCE.p2701", "空", "平", 4, 9471.0)
    L.confirmed("k1", 9478.0, 4)
    L.rejected("k2", "涨跌停拒单")
    L.sent("k3", "DCE.jm2701", "空", "开", 2, 1500.0)
    assert L.zombies(timeout_sec=0), "应识别出未决单"
    assert [k for k, _ in L.zombies(0)] == ["k3"], L.zombies(0)
    L2 = OrderLedger(lp)                              # 模拟重启后恢复
    assert list(L2.pending().keys()) == ["k3"], L2.pending()

    # 4) RiskGate
    g = RiskGate(max_hands=10, max_margin_ratio=0.5, max_daily_loss=0.02)
    assert g.check_open(4, 1_000_000, 200_000, 0)[0] is True
    assert g.check_open(20)[0] is False                     # 手数超限
    assert g.check_open(4, 1_000_000, 600_000, 0)[0] is False   # 保证金 60%
    assert g.check_open(4, 1_000_000, 100_000, -30_000)[0] is False  # 日亏 3%
    assert g.check_open(0)[0] is False

    # 5) leg_prefix / find_orphan（L449 回归）
    assert leg_prefix("DCE.p2701") == "DCE.p", leg_prefix("DCE.p2701")
    assert leg_prefix("CZCE.SR701") == "CZCE.SR", leg_prefix("CZCE.SR701")
    assert leg_prefix("SHFE.rb2410") == "SHFE.rb"
    assert leg_prefix("") == "" and leg_prefix("BAD") == ""
    pos = {"DCE.p2701": -4, "DCE.jm2701": -4, "DCE.m2701": -8,
           "DCE.jd2612": -8, "CZCE.SR701": 7}
    for cur in pos:                                    # 多腿并存：谁也不许命中别人
        assert find_orphan(pos, cur, exclude=cur) is None, (cur, find_orphan(pos, cur, exclude=cur))
    assert find_orphan(pos, "CZCE.SR701", exclude="CZCE.SF701") is None   # CZCE 同字母族不误伤
    pos["DCE.jm2609"] = -2                             # 真孤儿：只有焦煤腿能命中
    assert find_orphan(pos, "DCE.jm2701", exclude="DCE.jm2701") == "DCE.jm2609"
    assert find_orphan(pos, "DCE.p2701", exclude="DCE.p2701") is None

    # 6) reconcile
    r = reconcile({"DCE.p2701": 4, "DCE.jm2701": -2},
                  {"DCE.p2701": 4, "DCE.jm2701": 0, "DCE.m2701": 3})
    kinds = {x["symbol"]: x["kind"] for x in r}
    assert kinds == {"DCE.jm2701": "missing_cloud", "DCE.m2701": "missing_local"}, kinds

    print("  OK: 原子写 / BarGate 幂等 / 订单三层账+僵尸+重启恢复 / RiskGate / "
          "leg_prefix+孤儿扫描(L449 回归) / reconcile 全部通过")
    return True


if __name__ == "__main__":
    selftest()
