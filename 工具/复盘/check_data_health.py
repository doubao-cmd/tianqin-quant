"""复盘终端数据体检（可复用）—— 每次刷新 K 线后跑一次。

用法：
    <策略venv>/python.exe 工具/复盘/check_data_health.py

查四件事（全部只读，不写任何数据）：
  1. 覆盖区间：每品种 1/5/15/1440min 的根数与起止（确认「从 10-08 到现在」没缺口）
  2. 交易日历：最后一个交易日、夜盘是否存在、10:15 小休是否存在（校验 RECENT_META 口径）
  3. 异常跳变：窗口内相邻 5min 收盘 |涨跌|>2% 的事件，逐个标注是否落在「开/收盘边界」
     —— 边界处的大跳变多半是①节假日跳空（真）②主连换月（假，价格不可比）
  4. 数据质量：0 值 / NaN / high<low

★ 主连换月判定（2026-10-23 实证）：KQ.m@ 是**不复权**拼接（data.py is_adjusted=False），
  换月处会出现巨大的假跳空。判定方法：拿跳变时刻去比对该品种各具体合约的价格，
  KQ.m@ 与哪张合约逐值相同 ⇒ 当时的主力就是它。
  已证实例：FU 2026-10-20 21:00 -15.32% = SHFE.fu2611(5854) → SHFE.fu2701(4981)，纯换月；
            MA 2026-10-12 09:00 -8.19% = 真实行情（1min 连续、开盘放量下跌，非换月）。
"""
import sys
sys.path.insert(0, ".")

import data


def pct(a, b):
    return (b / a - 1.0) * 100.0


def main() -> int:
    syms = list(data.RECENT_SPECS)
    print("=" * 78)
    print("复盘终端数据体检 · %d 个品种" % len(syms))
    print("=" * 78)

    print("\n[1] 覆盖区间")
    for k in syms:
        parts = []
        for p in (1, 5, 15, 1440):
            try:
                df, _ = data.load_recent(k, p)
                bj = data.to_beijing(df["datetime"])
                parts.append(f"{p}min n={len(df):>5} {bj.min():%m-%d %H:%M}→{bj.max():%m-%d %H:%M}")
            except Exception as e:                                  # noqa: BLE001
                parts.append(f"{p}min 缺({type(e).__name__})")
        print(f"  {k:<3}")
        for s in parts:
            print(f"      {s}")

    print("\n[2] 时段口径校验（末个完整交易日）")
    for k in syms:
        df, _ = data.load_recent(k, 5)
        bj = data.to_beijing(df["datetime"])
        days = sorted(set(bj.dt.normalize()))
        d = days[-2]
        sub = df[bj.dt.normalize() == d]
        t = data.to_beijing(sub["datetime"])
        has_night = bool((t.dt.hour >= 21).any())
        slots = set(t.dt.strftime("%H:%M"))
        brk = not bool({"10:15", "10:20", "10:25"} & slots)
        meta = data.RECENT_META.get(k, {})
        ok = (has_night == (meta.get("night_end", 0) > 0)) and (brk == bool(meta.get("has_1015_break")))
        print(f"  {k:<3} {str(d)[:10]} 夜盘={has_night} 10:15小休={brk} "
              f"│ 表声明 night_end={meta.get('night_end')} break={meta.get('has_1015_break')} "
              f"{'OK' if ok else '✗ 与表不符'}")

    print("\n[3] 窗口内异常跳变（>2%，标注是否落在开/收盘边界）")
    hot = 0
    for k in syms:
        df, _ = data.load_recent(k, 5)
        bj = data.to_beijing(df["datetime"]).reset_index(drop=True)
        c = df["close"].astype(float).reset_index(drop=True)
        p = c.pct_change() * 100
        mask = (bj >= "2026-10-08") & (p.abs() > 2.0)
        idx = list(p[mask].index)
        if not idx:
            continue
        print(f"  {k:<3}")
        for i in idx:
            hm = bj[i].strftime("%H:%M")
            edge = "开盘边界" if hm in ("09:00", "21:00") else ("收盘边界" if hm in ("14:55", "22:55") else "盘中")
            flag = "★需查换月" if (edge != "盘中" and abs(p[i]) > 3) else ""
            hot += 1
            print(f"      {bj[i]:%m-%d %H:%M} ({edge}) {c[i-1]:.1f}→{c[i]:.1f} {p[i]:+.2f}% {flag}")
    if not hot:
        print("  无")

    print("\n[4] 数据质量")
    bad = 0
    for k in syms:
        for per in (5, 15, 1440):
            df, _ = data.load_recent(k, per)
            z = int((df["close"] <= 0).sum())
            n = int(df[["open", "high", "low", "close"]].isna().any(axis=1).sum())
            h = int((df["high"] < df["low"]).sum())
            if z or n or h:
                print(f"  ⚠ {k:<3} {per:>4}min zero={z} nan={n} high<low={h}")
                bad += z + n + h
    print(f"  异常合计 = {bad}")

    print("\n说明：★标记 = 边界处 >3% 的跳变，必须人工确认是「节假日跳空」还是「主连换月」。")
    print("     换月跳空不是行情，用来看形态可以，用来估 ATR/止损幅度会失真。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
