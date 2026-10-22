# -*- coding: utf-8 -*-
"""
仓单变化率因子 —— 信号频率预算（冻结前必做，解析计算，不建引擎）。

口径（与预注册卡一致）：
  rc = receipt_t / receipt_{t-K} - 1        K=20 交易日
  触发：|rc| > THRESH（±10%）
  有效日：receipt > 0（注册仓单为 0 时 rc 无定义 ⇒ 置 NaN，不参与）
  IS 窗口：2019-01-02 ~ 2021-12-31

输出：每品种 有效仓单占比 / rc 分布 / 触发天数 / 触发段数(episodes) / 多空拆分，
并给「覆盖品种数」判定。
"""
import os

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
CSV = os.path.join(HERE, "数据", "仓单_SHFE_CZCE_2018_2022.csv")

K = 20
TH = 0.10
IS_START = pd.Timestamp("2019-01-02")
IS_END = pd.Timestamp("2021-12-31")


def episodes(flags):
    """连续 True 段数（= 触发段数，段内多日算一次）。"""
    f = np.asarray(flags).astype(int)
    if len(f) == 0:
        return 0
    return int(((f[1:] == 1) & (f[:-1] == 0)).sum() + (1 if f[0] == 1 else 0))


def main():
    df = pd.read_csv(CSV)
    df["receipt"] = pd.to_numeric(df["receipt"], errors="coerce")
    df["date"] = pd.to_datetime(df["date"])

    print("=" * 78)
    print("仓单变化率因子 · 信号频率预算")
    print(f"数据 {CSV}")
    print(f"rc = receipt_t / receipt_(t-{K}) - 1   |  触发 |rc| > {TH:.0%}  |  IS {IS_START.date()}~{IS_END.date()}")
    print("=" * 78)

    rows = []
    for var, g in df.groupby("var"):
        g = g.sort_values("date").set_index("date")
        s = g["receipt"].astype(float)
        prev = s.shift(K)
        rc = s / prev - 1
        rc = rc.where((prev > 0) & s.notna())          # 注册为 0/缺失 ⇒ NaN

        is_mask = (s.index >= IS_START) & (s.index <= IS_END)
        s_is = s[is_mask]
        rc_is = rc[is_mask]

        valid_days = int((s_is > 0).sum())
        total_days = int(is_mask.sum())
        trig = rc_is.abs() > TH
        n_trig_days = int(trig.sum())
        n_epi = episodes(trig.fillna(False))
        n_long = episodes((rc_is < -TH).fillna(False))
        n_short = episodes((rc_is > TH).fillna(False))

        rows.append({
            "品种": var,
            "总日数": total_days,
            "有效仓单日": valid_days,
            "有效占比%": round(100 * valid_days / total_days, 1) if total_days else 0,
            "仓单均值": int(s_is[s_is > 0].mean()) if valid_days else 0,
            "rc_std%": round(100 * rc_is.std(), 1) if rc_is.notna().any() else np.nan,
            "触发天数": n_trig_days,
            "触发段数": n_epi,
            "多段": n_long,
            "空段": n_short,
        })

    out = pd.DataFrame(rows)
    print(out.to_string(index=False))

    # 覆盖判定：有效仓单日占比 ≥ 50% 且 触发段数 ≥ 2 才算「该品种可用」
    usable = out[(out["有效占比%"] >= 50) & (out["触发段数"] >= 2)]
    print("-" * 78)
    print(f"可用品种（有效仓单日≥50% 且 触发段数≥2）：{list(usable['品种'])}  共 {len(usable)} 个")
    print(f"全池触发段数合计：{int(out['触发段数'].sum())}")
    print()
    gate_n = 5
    if len(usable) >= gate_n and int(out["触发段数"].sum()) >= 10:
        print(f"✅ 频率预算通过：可用品种 {len(usable)} ≥ {gate_n}，触发段数合计 ≥ 10 ⇒ 可冻结进引擎")
    else:
        print(f"❌ 频率预算不通过：可用品种 {len(usable)}（需≥{gate_n}）或触发段数不足 ⇒ 按卡规定收档，不调参")
    print("=" * 78)


if __name__ == "__main__":
    main()
