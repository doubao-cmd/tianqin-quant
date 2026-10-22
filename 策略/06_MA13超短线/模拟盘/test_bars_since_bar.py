"""E1 离线单测：`_bars_since_bar` 的 8 小时时区错配（清单 A15）。

为什么必须离线测：
  15:00 重启时若已空仓，`_attach_stop_on_boot` 在 `pos == 0` 处直接 return，
  这个函数根本没机会被调用 ⇒ 等实盘重启验证等于永远验不到。

口径（已实证，勿猜）：
  · tqsdk `k["datetime"]` = **UTC 纳秒整数**；`pd.to_datetime()` 出来是 UTC naive。
  · `quote.datetime`（= 传进来的 bar_time）= **北京时间**字符串。
  · 实证：`pd.Timestamp(1792541400000000000)` → `2026-10-21 00:10:00`（UTC，非 08:10）。
  · 实证：13:32 核心日志 `K末行=05:30:00` 正是北京 13:30 ⇒ kd 是 UTC。

★ 关于"50 根"：豆粕单交易时段最长 24 根（夜盘 21:00-23:00），csv 里**不存在**
  时间连续的 50 根（中间必跨午休/隔夜）。跨时段会使旧逻辑的 UTC 跨度 > 8h，
  从而让"复现 bug"失真。故分两个用例：
    t1/t2 用**真实连续段 24 根**（期望 14，旧逻辑必 0）—— 最诚实；
    t5 用 50 根**真实 OHLC + 重建连续时间轴**（期望 40）—— 对齐清单数字，
       只重建 datetime，OHLC 原样，且该函数只用 datetime 列。

跑法：
  cd 策略/06_MA13超短线/模拟盘 && python test_bars_since_bar.py
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.abspath(os.path.join(_HERE, "..", "..", "..")))
sys.path.insert(0, os.path.abspath(os.path.join(_HERE, "..", "工作台")))

import pandas as pd                      # noqa: E402

import ma13_live_multi as m              # noqa: E402

CSV = os.path.abspath(os.path.join(
    _HERE, "..", "..", "..", "工具", "复盘", "_缓存", "kline2026", "M_5min.csv"))

IDX = 9              # 第 10 根（0-based）


def _read():
    assert os.path.exists(CSV), f"数据文件不存在：{CSV}"
    df = pd.read_csv(CSV, encoding="utf-8-sig")
    return df, pd.to_datetime(df["datetime"])       # csv 是 UTC naive


def _find_contiguous(dt, n):
    """找一段时间连续（每根相隔 5min）的 n 根，返回起始索引。"""
    for i in range(n, len(dt) + 1):
        seg = dt.iloc[i - n:i]
        if (seg.diff().dropna() == pd.Timedelta(minutes=5)).all():
            return i - n
    return None


def _build_k(times_utc, ohlc):
    """模拟 tqsdk：datetime 列 = 纳秒整数。"""
    return pd.DataFrame({
        "datetime": [int(t.value) for t in times_utc],
        "open": ohlc["open"].values, "high": ohlc["high"].values,
        "low": ohlc["low"].values, "close": ohlc["close"].values,
    })


def _bt_bj(utc_start):
    """bar_time 的语义：该根 bar 结束前 0.02s 的**北京时间**（quote.datetime）。"""
    return utc_start + pd.Timedelta(hours=8, minutes=5) - pd.Timedelta(milliseconds=20)


def _load_real(n):
    df, dt = _read()
    s = _find_contiguous(dt, n)
    assert s is not None, f"数据里找不到 {n} 根连续段"
    seg = df.iloc[s:s + n].reset_index(drop=True)
    return seg, dt.iloc[s:s + n].reset_index(drop=True)


def _check(got, want, tag):
    assert got is not None, f"{tag}：返回 None"
    assert abs(got - want) <= 1, f"{tag}：期望 {want}±1，实际 {got}"


def t1_真实连续段_24根():
    seg, utc = _load_real(24)
    got = m._bars_since_bar(_build_k(utc, seg), f"{_bt_bj(utc[IDX]):%Y-%m-%d %H:%M:%S.%f}")
    _check(got, 24 - 1 - IDX, "t1 真实连续段")
    print(f"  PASS  t1 真实连续段 24 根 → bars_in={got}（期望 {24 - 1 - IDX}）")


def t2_旧逻辑_必为0_复现bug():
    """内联旧逻辑：kd 不加 8h（bt 仍为北京）⇒ 连续段内计数恒 0。"""
    seg, utc = _load_real(24)
    k = _build_k(utc, seg)
    bt_old = pd.Timestamp(f"{_bt_bj(utc[IDX]):%Y-%m-%d %H:%M:%S.%f}"[:26])
    kd_old = pd.to_datetime(k["datetime"])          # bug 版本：不 +8h
    old = max(0, int((kd_old > bt_old).sum()))
    assert old == 0, f"未能复现 bug：旧逻辑算出 {old}（期望 0）"
    print(f"  PASS  t2 旧逻辑复现 bug：bars_in={old}"
          f"（K末行 {kd_old.iloc[-1]} vs bt {bt_old}）")


def t3_纳秒整数分支():
    """bar_time 也可能是纳秒整数（此时原值是 UTC）⇒ 两边都要抬，不能只加一边。"""
    seg, utc = _load_real(24)
    ns = int((utc[IDX] + pd.Timedelta(minutes=5) - pd.Timedelta(milliseconds=20)).value)
    got = m._bars_since_bar(_build_k(utc, seg), ns)
    _check(got, 24 - 1 - IDX, "t3 纳秒分支")
    print(f"  PASS  t3 纳秒 bar_time → bars_in={got}")


def t5_五十根_重建时间轴():
    """对齐清单：50 根、第 10 根的 quote 值、期望 40。

    OHLC 用真实数据；datetime 重建为从段起点起连续 5min（本函数只用 datetime 列）。
    """
    n_src = 50
    df, dt = _read()
    s = _find_contiguous(dt, 24)                    # 借一段真实起点
    seg = df.iloc[s:s + n_src].reset_index(drop=True)
    assert len(seg) == n_src, f"数据不足 {n_src} 根"
    base = dt.iloc[s]                               # UTC
    fake_utc = pd.Series([base + pd.Timedelta(minutes=5 * i) for i in range(n_src)])
    k = _build_k(fake_utc, seg)

    got = m._bars_since_bar(k, f"{_bt_bj(fake_utc[IDX]):%Y-%m-%d %H:%M:%S.%f}")
    _check(got, 40, "t5 50 根重建")
    assert got == 40, f"清单要求 40，实际 {got}"

    # 同一构造下旧逻辑必须为 0
    bt_old = pd.Timestamp(f"{_bt_bj(fake_utc[IDX]):%Y-%m-%d %H:%M:%S.%f}"[:26])
    kd_old = pd.to_datetime(k["datetime"])
    old = max(0, int((kd_old > bt_old).sum()))
    assert old == 0, f"50 根构造下旧逻辑应 0，实际 {old}"
    print(f"  PASS  t5 50 根重建时间轴 → bars_in=40（旧逻辑 {old}）")


def t6_边界():
    seg, utc = _load_real(24)
    k = _build_k(utc, seg)
    assert m._bars_since_bar(k, None) is None
    assert m._bars_since_bar(k, "not-a-time") is None
    early = f"{utc[0] + pd.Timedelta(hours=8) - pd.Timedelta(minutes=5):%Y-%m-%d %H:%M:%S.%f}"
    got = m._bars_since_bar(k, early)
    assert got == 24, f"早于首根应得 24，实际 {got}"
    print(f"  PASS  t6 None/乱码/早于首根（{got}）")


def main():
    print("=" * 70)
    print("E1 离线单测：_bars_since_bar 时区（kd=UTC，bt=北京，差 8h）")
    print(f"数据：{CSV}")
    print("=" * 70)
    tests = [t1_真实连续段_24根, t2_旧逻辑_必为0_复现bug, t3_纳秒整数分支,
             t5_五十根_重建时间轴, t6_边界]
    bad = 0
    for t in tests:
        try:
            t()
        except Exception as e:                       # noqa: BLE001
            bad += 1
            print(f"  FAIL  {t.__name__}: {type(e).__name__}: {e}")
    print("=" * 70)
    print(f"{len(tests) - bad}/{len(tests)} 通过" + ("" if not bad else f"，{bad} 失败"))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
