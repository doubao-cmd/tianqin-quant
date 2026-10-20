# -*- coding: utf-8 -*-
"""
工具/_np_data/fetch_spot.py —— 现货价 / 基差 → np_data（D6/D7）

源：100ppi（经 `akshare.futures_spot_price(date)`），54 个商品品种，深度 ≤2013-01-04。

===============================================================================
这个源的两条硬风险（必须写进库，不能只写在文档里）
===============================================================================
1. **采集时刻不明。** 100ppi 只说"某日现货价"，不说几点采的。
   实测证据：2026-10-19 14:28:36 调 `futures_spot_price("20261019")` → **0 行**，
   而同一时刻查 20261016 → 54 行。
   ⇒ 当日现货价不是盘中更新，`published_at` 落在收盘之后 ⇒ **当日基差对当日 bar 不可用**。
   规则值取 18:00（比任何可能的实际时刻都晚，保守方向）。

2. **`dominant_contract` 是第三方判定的主力合约。**
   `dom_basis` = 现货价 − 100ppi 认定的主力合约价。
   直接拿它 join 自己的 bar，等于用了「当时不可知的主力判定」= **第二类未来函数**。
   所以本模块拆成两个 metric：
     - `spot_price` （contract=''）  ← **最干净，推荐下游只用这个**
     - `dom_basis`  （contract=主力）← 第三方口径，用它就必须承担主力定义的前视风险
   下游若要严格 PIT，应当：用 `spot_price` 减去**自己**的换月序列对应的期货价。
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import pit    # noqa: E402
import store  # noqa: E402

warnings.filterwarnings("ignore")

M_SPOT = "spot_price"
M_BASIS = "dom_basis"


def _num(v) -> float:
    if v is None:
        return float("nan")
    s = str(v).replace(",", "").replace(" ", "").strip()
    if s in ("", "-", "--", "nan", "None"):
        return float("nan")
    try:
        return float(s)
    except ValueError:
        return float("nan")


def _mk(date, symbol, metric, value, contract=""):
    import math
    if value != value or math.isinf(value):
        return None
    pub, tag = store.resolve_published("spot_basis", date)
    iso = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
    return pit.make_row("spot_basis", symbol, metric, iso, iso, pub, float(value),
                        contract=contract,
                        raw_url=store.RULE_NOTE.get("spot_basis", ""),
                        published_src=tag)


def fetch(date: str) -> list:
    import akshare as ak
    df = ak.futures_spot_price(date=date)
    if df is None or len(df) == 0:
        raise RuntimeError(
            f"spot_basis {date} 返回 0 行。\n"
            f"  已知该源当日数据要收盘后才有（实测 14:28 查当日仍为 0 行）；\n"
            f"  如果是收盘后仍为 0 行，则可能是节假日或源站结构变化，不能当「当日无数据」静默通过。")
    store.save_raw(date, "spot_basis", store.df_to_records(df))

    rows = []
    for _, r in df.iterrows():
        sym = str(r["symbol"]).strip().upper()
        v = _num(r.get("spot_price"))
        rows.append(_mk(date, sym, M_SPOT, v))
        dom = str(r.get("dominant_contract") or "").strip().lower()
        b = _num(r.get("dom_basis"))
        if dom and dom not in ("nan", "none"):
            rows.append(_mk(date, sym, M_BASIS, b, contract=dom))
    return [x for x in rows if x]


def _selftest() -> int:
    ok = True
    print("=" * 72)
    print("fetch_spot 自检（真实数据 20261016）")
    print("=" * 72)

    rows = fetch("20261016")
    n_spot = sum(1 for r in rows if r["metric"] == M_SPOT)
    n_basis = sum(1 for r in rows if r["metric"] == M_BASIS)
    print(f"  ① 抓取 {len(rows)} 行：spot_price={n_spot} / dom_basis={n_basis}"
          f"（54 品种 ⇒ 应 54/54）")
    ok &= n_spot == 54 and n_basis == 54

    rb = [r for r in rows if r["symbol"] == "RB" and r["metric"] == M_SPOT]
    print(f"  ② RB 现货价={rb[0]['value'] if rb else None}（20261016 实测 3164.84）")
    ok &= bool(rb) and abs(rb[0]["value"] - 3164.84) < 1e-6

    # ★ 关键：基差的 contract 必须是主力合约（带月份），不能是空
    rb_b = [r for r in rows if r["symbol"] == "RB" and r["metric"] == M_BASIS]
    print(f"  ③ RB dom_basis contract={rb_b[0]['contract'] if rb_b else None}"
          f"（应含月份，如 rb2701）")
    ok &= bool(rb_b) and any(ch.isdigit() for ch in rb_b[0]["contract"])

    print(f"  ④ published_src 全为 rule（未实测）: "
          f"{all(r['published_src'] == 'rule' for r in rows)}")
    ok &= all(r["published_src"] == "rule" for r in rows)

    print("=" * 72)
    print("fetch_spot 自检：" + ("通过 ✅" if ok else "失败 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    d = sys.argv[1] if len(sys.argv) > 1 else None
    if not d:
        raise SystemExit("用法: fetch_spot.py YYYYMMDD | --selftest")
    raise SystemExit(store.run({"spot": fetch}, d))
