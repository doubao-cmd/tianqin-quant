# -*- coding: utf-8 -*-
"""R3 前置检查 1：截面期限结构 carry vs 已死截面基差 的秩相关。

依据 `创新/R3_截面期限结构carry_20261019.md` §五：
    carry 因子值与 spot_basis 的 Spearman 秩相关 > 0.8 ⇒ 同一因子，直接停。

因子口径（对齐 R3 §4.1 / 截面基差预注册卡）：
    spot_basis = dom_basis_rate = (现货 − 主力合约价) / 现货价        # 已死
    carry      = ln(F_near / F_far) × 365 / Δdays                     # R3
      F_near/F_far = 同一 (品种,日期) 下 OI 最大的两个合约，按到期月排序取首末

只读，不写因子库、不跑回测。前置检查用「当日 OI 选合约」近似即可——
相关性对 OI 口径（当日/前一日）不敏感。
"""
import csv
import math
import os
import re

from scipy.stats import spearmanr

ROOT = r"C:\Users\cc\Desktop\天勤量化"
BASIS_CSV = os.path.join(ROOT, "工具", "_截面因子_基差", "数据", "基差_20190101_20211231.csv")
ROLL_CSV = os.path.join(ROOT, "工具", "_数据", "展期_合约日线.csv")


def norm_date(s):
    s = str(s).strip()
    if "-" in s:
        return s[:10]
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


def exp_month(sym):
    m = re.search(r"(\d{3,4})$", str(sym))
    if not m:
        return None
    d = m.group(1)
    if len(d) < 4:
        return None
    yy = int(d[:2]); mm = int(d[2:])
    year = 2000 + yy if yy < 90 else 1900 + yy
    return (year, mm)


def main():
    # 1) 已死基差因子
    basis = {}
    with open(BASIS_CSV, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            b = r.get("dom_basis_rate")
            if b in ("", None, "nan", "NaN"):
                continue
            try:
                basis[(norm_date(r["date"]), r["symbol"])] = float(b)
            except (ValueError, KeyError):
                continue
    basis_syms = sorted({s for _, s in basis})
    print(f"[基差] {len(basis)} 个 (日期,品种) 观测，品种 {basis_syms}")

    # 2) carry 因子（展期表，OI 最大两合约）
    from collections import defaultdict
    g = defaultdict(list)
    with open(ROLL_CSV, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            em = exp_month(r.get("sym"))
            if em is None:
                continue
            try:
                g[(norm_date(r["日期"]), r["key"])].append(
                    (em, float(r["close"]), float(r["oi"])))
            except (ValueError, KeyError):
                continue
    carry = {}
    for (date, key), lst in g.items():
        if len(lst) < 2:
            continue
        lst.sort(key=lambda x: -x[2])          # oi 降序
        two = lst[:2]
        two.sort(key=lambda x: x[0])           # 按到期月升序：near, far
        (em1, p1, _), (em2, p2, _) = two[0], two[1]
        if em1 >= em2 or p1 <= 0 or p2 <= 0:
            continue
        dm = (em2[0] - em1[0]) * 12 + (em2[1] - em1[1])
        if dm <= 0:
            continue
        carry[(date, key)] = math.log(p1 / p2) * 365.0 / (dm * 30.44)
    carry_syms = sorted({s for _, s in carry})
    print(f"[carry] {len(carry)} 个 (日期,品种) 观测，品种 {carry_syms}")

    inter = sorted(set(basis_syms) & set(carry_syms))
    print(f"[交集] {inter}")

    xs, ys = [], []
    for k in basis:
        if k in carry:
            xs.append(basis[k]); ys.append(carry[k])
    print(f"[对齐] {len(xs)} 个共同 (日期,品种) 观测")
    if len(xs) < 8:
        print("观测对太少（<8），秩相关不可靠，需扩品种后才能做前置检查")
        return

    rho, p = spearmanr(xs, ys)
    print(f"[结果] Spearman rho = {rho:+.3f}  p = {p:.3e}")
    print(f"[裁定] {'> 0.8 ⇒ 同一因子，直接停' if abs(rho) > 0.8 else '≤ 0.8 ⇒ 不视为同一因子，R3 可继续（仍需扩池 + 其他前置检查）'}")


if __name__ == "__main__":
    main()
