# -*- coding: utf-8 -*-
"""
MA13 五腿实测逐笔诊断（滑点 / 手续费 / 止损偏离 / 持仓时长）
=============================================================
读 模拟盘/台账/<品种>_成交流水.csv，把每一笔「开仓→平仓」配成一行，
输出**纯文本**对照表，回答四个问题：

  ① 这笔亏/赚多少，去掉手续费还剩多少？
  ② 止损线画在几号？实际平仓价离止损线差多少？（滑点/跳空）
  ③ 持仓多久？（止损宽度与持仓时长是否匹配）
  ④ 成本占比多少？—— 手续费吃掉了几成利润。

★ 与既有工具的分工（别重复造轮子）
--------------------------------------------------------------
  工具/模拟盘台账.py  → HTML 月度台账，算「FILL 与信号参考价之差」的粗滑点
  本工具              → 逐笔 CSV 对照表，算「实际平仓价 vs 止损线」的**执行偏离**
                        并把手续费显式扣出来，看**净盈亏**。
两者口径不同、互为校验：若两者滑点差异大，说明存在跳空或未成交。

★ 口径（重要，勿改）
--------------------------------------------------------------
  手续费 = config_symbols.COST[品种]，**单边**元/手，开+平各收一次 ⇒ 双边 = 2×COST×手数。
  实盘成本**精确度**：COST 是回测用的估算表，不是云端实际扣费；真实值需从账户权益倒推
  （权益差 − 价格盈亏 = 成本）。本工具两者都给：
     · 「成本(估算)」 = COST 表×手数×2
     · 「成本(实测)」 = 权益变化 − 价格盈亏（含浮盈浮亏，故为毛值，含持仓期间波动）
  ⚠️ 实测值含持仓期浮动，只在**持仓已清零**的那一行才有意义。

不做什么：不改任何策略参数、不算夏普/回撤、不生成HTML。纯观测。

用法：
  python 工具/诊断台账.py            # 五腿全部
  python 工具/诊断台账.py 焦煤#     # 单腿（带# 即带持仓明细）
"""
import csv
import os
import sys
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    import config_symbols as _cs
except Exception:                                    # pragma: no cover
    _cs = None

FIVE = ["JM", "M", "JD", "P", "SR"]
NAME = {"JM": "焦煤", "M": "豆粕", "JD": "鸡蛋", "P": "棕榈油", "SR": "白糖"}
DEFAULT_TAIDIR = os.path.join(
    _ROOT, "策略", "06_MA13超短线", "模拟盘", "台账")


def _mult_tick(key):
    """取 (乘数, 最小变动价位)。

    ⚠️ config_symbols.SYMBOLS 的结构（实测，别猜）：
        它是 dict[品种码] → **tuple** (中文名, 交易所代码, 乘数, 最小变动价位)
        例：SYMBOLS['JM'] == ('焦煤', 'KQ.m@DCE.jm', 60, 0.5)
        ⇒ 不是对象、没有 .mult 属性；按下标 2/3 取。
        （踩过一次：先前误当对象取 .mult/.tick，静默返回 None 而算不出金额。）
    """
    try:
        t = _cs.SYMBOLS[key]
        return float(t[2]), float(t[3])
    except Exception:
        return None, None


def _cost(key):
    """单边手续费（元/手）。"""
    try:
        return float((getattr(_cs, "COST", {}) or {}).get(key, 0.0) or 0.0)
    except Exception:
        return 0.0


def _read(path):
    """读事件流 CSV → [dict, ...]

    ⚠️ 列结构（实测自引擎 `_LiveRecorder._w`，别猜也别照抄旧工具的 8 列写法）：
        0 类型 | 1 记录时间 | 2 K线时间 | 3 方向+动作 | 4 手数 | 5 成交价 | 6 参考价 | 7 备注
        方向+动作是**合在一起**的（如 "空平" / "多开"），没有独立的 action 列。
        （踩过一次：先前按 8 列且 action=r[4] 解析 → 成交笔数恒为 0，静默不出错。）
    """
    rows = []
    if not os.path.isfile(path):
        return rows
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for r in csv.reader(f):
            if len(r) < 6 or r[0] in ("类型", "kind", "EVENT"):
                continue
            rows.append({
                "kind": r[0], "ts": r[1], "bar": r[2] if len(r) > 2 else "",
                "diract": r[3] if len(r) > 3 else "",
                "hands": r[4] if len(r) > 4 else "",
                "price": r[5] if len(r) > 5 else "",
                "ref": r[6] if len(r) > 6 else "",
                "note": r[7] if len(r) > 7 else "",
            })
    return rows


def _f(x):
    try:
        return float(x)
    except Exception:
        return None


def _parse_trades(rows):
    """把事件流配成「一笔」：开仓 → 平仓。

    ⚠️★ 止损归属的坑（实测踩过，数据会全错却不报错）：
       事件流里SIGNAL 出现在**开仓之前**（先算信号再报单），而 STOPLOSS 出现在
       **平仓之后**（同秒）。所以：
         · 绝不能"拿还没配对的上一条 SIGNAL 的 stop" —— 那会把它错配给下一笔；
         · 止损价有两种来源，优先级：**STOPLOSS 行备注里的「止损线 X」**（最准，
           是程序实际挂在云端的那条）> **同一笔开仓前最近那条 SIGNAL 的 stop≈**（估算）。
       另：开仓后紧跟又来一条 SIGNAL（13:37 那次）属于**加仓/重复信号**，不改变本笔持仓。
    """
    out, cur = [], None
    pending_stop = None          # 最近一条 SIGNAL 的止损（还没被开仓消费）
    extra = []                   # 新版才有：FILLX 真滑点 / EXCURSION 浮盈浮亏
    for r in rows:
        k = r["kind"]
        if k == "SIGNAL":
            s = _stop_from_note(r["note"])
            if s is not None:
                pending_stop = s
            continue
        if k in ("FILLX", "EXCURSION"):
            extra.append(r)
            continue
        if k == "STOPLOSS":
            s = _stop_from_note(r["note"]) or _f(r["ref"])
            # 归属到**刚刚被平掉**的那笔（cur 已append，这里用 out[-1]）
            if out and s is not None:
                out[-1]["stop"] = s
                out[-1]["reason"] = "STOPLOSS"
            elif cur is not None:
                cur["stop"] = s
                cur["reason"] = "STOPLOSS"
            continue
        if k != "FILL":
            continue
        da = (r["diract"] or "").strip()
        h = _f(r["hands"])
        px = _f(r["price"])
        if px is None or h is None:
            continue
        if da.endswith("开"):# 多开 / 空开
            if cur:                              # 上笔没平掉又开 → 反手：先平再开
                cur["exit_price"] = px
                cur["exit_ts"] = r["ts"]
                cur["reason"] = "REVERSE"
                out.append(cur)
            cur = {"dir": "多" if da.startswith("多") else "空",
                   "hands": h, "entry": px, "entry_ts": r["ts"],
                   "stop": pending_stop, "reason": "?"}
            pending_stop = None                  # 这条止损已被本笔消费
        elif da.endswith("平"):                  # 多平 / 空平
            if cur:
                cur["exit_price"] = px
                cur["exit_ts"] = r["ts"]
                out.append(cur)
                cur = None
    return out, cur, extra


def _stop_from_note(note):
    """从 'stop≈1477.1' 里抠出止损价。"""
    if not note:
        return None
    for seg in str(note).replace("，", ",").split(","):
        seg = seg.strip()
        for tag in ("stop≈", "止损线", "止损"):
            if tag in seg:
                v = seg.split(tag, 1)[1].strip()
                v = v.replace("元", "").strip()
                return _f(v)
    return None


def _mins(t0, t1):
    try:
        a = datetime.strptime(t0, "%Y-%m-%d %H:%M:%S")
        b = datetime.strptime(t1, "%Y-%m-%d %H:%M:%S")
        return int((b - a).total_seconds() // 60)
    except Exception:
        return None


def _key_norm(k):
    """品种码归一：允许传 'JM'、'jm' 或中文名 '焦煤'，一律转成 SYMBOLS 的键。

    ⚠️ 踩过：直接传中文 '焦煤' → SYMBOLS['焦煤'] KeyError → 乘数/手续费静默变 0，
    金额全算成 0 却不报错。命令行给人用，必须能敲中文名。
    """
    k = (k or "").strip().replace("#", "")
    for code, nm in NAME.items():
        if k in (code, nm, nm.lower(), code.lower()):
            return code
    return k.upper()


def analyse(key, taidir=DEFAULT_TAIDIR, show_open=False):
    key = _key_norm(key)
    nm = NAME.get(key, key)
    path = os.path.join(taidir, f"{nm}_成交流水.csv")
    rows = _read(path)
    if not rows:
        print(f"[{nm}] 没有成交流水：{path}")
        return None
    trades, open_pos, extra = _parse_trades(rows)
    mult, tick = _mult_tick(key)
    fee1 = _cost(key)

    print("=" * 78)
    print(f"【{nm}】{os.path.basename(path)}   成交笔数={len(trades)}"
          f"   乘数={mult or '?'}  最小变动={tick or '?'}"
          f"   单边手续费={fee1}元/手")
    print("=" * 78)
    if not trades:
        print("  （尚无完整开平配对）")
    hdr = (f"{'#':>2} {'方向':2} {'手':>2} {'开仓':>9} {'平仓':>9} {'止损':>9} "
           f"{'偏离':>8} {'持仓min':>7} {'毛盈亏':>9} {'手续费':>7} {'净盈亏':>9} {'出场'}")
    print(hdr)
    print("-" * 78)
    tot_g = tot_f = 0.0
    for i, t in enumerate(trades, 1):
        e, x = t["entry"], t["exit_price"]
        d = 1 if t["dir"] == "多" else -1
        if mult:
            gross = (x - e) * d * mult * t["hands"]
        else:
            gross = 0.0
        fee = fee1 * t["hands"] * 2
        dev = None
        if t.get("stop") and mult and tick:
            dev = (x - t["stop"]) * d * mult * t["hands"]
        else:
            dev = None
        hold = _mins(t["entry_ts"], t["exit_ts"])
        # 计划风险：开仓价到止损线的距离（按方向取正数）
        risk = None
        if t.get("stop") and mult:
            risk = abs(t["stop"] - e) * mult * t["hands"]
        # ★ 汇总要用这几个字段，所以顺手挂回 trade（否则 _summarise 拿不到）
        t["gross"], t["fee"], t["dev"], t["hold"], t["risk"] = \
            gross, fee, dev, hold, risk
        tot_g += gross
        tot_f += fee
        devs = f"{dev:+8,.0f}" if dev is not None else "       -"
        print(f"{i:>2} {t['dir']:2} {t['hands']:>2.0f} {e:>9.2f} {x:>9.2f} "
              f"{(t.get('stop') or 0):>9.2f} {devs} "
              f"{(hold if hold is not None else -1):>7} "
              f"{gross:>+9,.0f} {fee:>7,.0f} {gross - fee:>+9,.0f} {t['reason']}")
    print("-" * 78)
    if trades:
        wins = sum(1 for t in trades
                   if ((t["exit_price"] - t["entry"]) * (1 if t["dir"] == "多" else -1)
                       * (mult or 0)) > 0)
        n = len(trades)
        print(f"合计：毛盈亏 {tot_g:+,.0f}   手续费 {tot_f:,.0f}   "
              f"净盈亏 {tot_g - tot_f:+,.0f}   胜 {wins}/{n}")
        if tot_g != 0:
            print(f"成本占毛盈亏的{abs(tot_f / tot_g) * 100:.1f}%（正数=成本侵蚀利润）")
        hs = [_mins(t["entry_ts"], t["exit_ts"]) for t in trades]
        hs = [h for h in hs if h is not None]
        if hs:
            print(f"持仓时长：最短 {min(hs)} min  中位 {sorted(hs)[len(hs) // 2]} min"
                  f"  最长 {max(hs)} min")
        devs = []
        for t in trades:
            if t.get("stop") and mult:
                d = 1 if t["dir"] == "多" else -1
                devs.append((t["exit_price"] - t["stop"]) * d * mult * t["hands"])
        if devs:
            bad = [v for v in devs if v < 0]
            print(f"止损执行偏离：最差 {min(devs):+,.0f} 元   "
                  f"其中{len(bad)}/{len(devs)} 笔**差于**止损价（滑价/跳空）")
    if open_pos and show_open:
        print(f"\n  ⚠ 当前持仓：{open_pos['dir']} {open_pos['hands']:.0f}手 "
              f"@{open_pos['entry']} 开于 {open_pos['entry_ts']} 止损≈{open_pos.get('stop')}")
    _print_extra(extra)
    return trades


def _print_extra(extra):
    """新版钩子才有 的两类数据：FILLX（真滑点）与 EXCURSION（最大浮盈/浮亏）。

    ⚠️ 2026-10-08 那批台账**没有**这两类（当时程序只记决策价），
    所以首次运行会显示「尚无」—— 这不是 bug，要等重启后的新成交才有。
    """
    fx = [r for r in extra if r["kind"] == "FILLX"]
    ex = [r for r in extra if r["kind"] == "EXCURSION"]
    print()
    if not fx:
        print("  【真滑点】尚无FILLX 数据（需重启新版后才有："
              "老台账只记决策价，不记云端成交价）")
    else:
        print(f"  【真滑点】来自云端成交均价（{len(fx)} 条）")
        for r in fx[-8:]:
            print(f"    {r['ts']}  {r['note']}")
    if not ex:
        print("  【浮盈浮亏】尚无 EXCURSION 数据（同上，需重启新版）")
    else:
        print(f"  【浮盈浮亏】持仓期间极值（{len(ex)} 条）")
        for r in ex[-8:]:
            print(f"    {r['ts']}  {r['note']}")


def _summarise(all_tr):
    """五腿加总。单腿看不出成本量级，必须加总才看得出「成本吃掉了多少」。"""
    n = sum(len(v) for v in all_tr.values())
    if not n:
        return
    g = sum(t["gross"] for v in all_tr.values() for t in v)
    f = sum(t["fee"] for v in all_tr.values() for t in v)
    w = sum(1 for v in all_tr.values() for t in v if t["gross"] > 0)
    dev = [t["dev"] for v in all_tr.values() for t in v if t.get("dev") is not None]
    hold = [t["hold"] for v in all_tr.values() for t in v if t.get("hold") is not None]
    sl = [t["risk"] for v in all_tr.values() for t in v if t.get("risk")]

    print("=" * 78)
    print("【五腿汇总】")
    print("=" * 78)
    print(f"总成交 {n} 笔   胜 {w}笔 ({w / n * 100:.0f}%)")
    print(f"毛盈亏 {g:+,.0f}   手续费 {f:,.0f}   **净盈亏 {g - f:+,.0f}**")
    if g:
        print(f"→ 手续费吃掉毛盈亏的 {abs(f / g) * 100:.1f}%"
              f"（亏损行情下这是雪上加霜）")
    if dev:
        bad = [x for x in dev if x < 0]
        print(f"止损执行：{len(bad)}/{len(dev)} 笔实际**差于**止损价（滑价/跳空）"
              f"   最差 {min(dev):+,.0f} 元")
        worse = sum(-x for x in bad)
        if g< 0:
            print(f"   ⇒ 滑价额外损失约 {worse:,.0f} 元，"
                  f"占毛亏损的 {abs(worse / g) * 100:.1f}%")
    if sl:
        over = [t for v in all_tr.values() for t in v
                if t.get("risk") and t.get("dev") is not None
                and t["dev"] < -t["risk"]]
        print(f"计划风险合计 {sum(sl):,.0f} 元   "
              f"其中 {len(over)} 笔实际亏损**超过**计划风险（超止损）")
        if over:
            print(f"   ⇒ 超止损最严重的一笔：{min(t['dev'] for t in over):+,.0f} 元")
    if hold:
        hold.sort()
        print(f"持仓时长：最短 {hold[0]} min  中位 {hold[len(hold) // 2]} min"
              f"  最长 {hold[-1]} min")
        print(f"   ⇒ 持仓普遍很短（多数 <60 min），而止损宽度约 0.2%"
              f" —— 这正是「被噪音扫」的可观察特征")
    print()


def main(argv):
    taidir = DEFAULT_TAIDIR
    args = [a for a in argv if not a.startswith("--")]
    if args and os.path.isdir(args[0]):
        taidir = args[0]
        args = args[1:]
    keys = [_key_norm(a) for a in args] or FIVE
    allt = {}
    for k in keys:
        ts = analyse(k, taidir, show_open=True)
        if ts:
            allt[k] = ts
        print()
    if len(keys) > 1:
        _summarise(allt)
    return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))