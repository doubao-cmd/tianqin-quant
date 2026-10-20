# -*- coding: utf-8 -*-
"""
工具/_np_data/check_pit_live.py —— 在**真实数据**上验 PIT 层（不是夹具）

为什么需要
===============================================================================
`_t_pit.py` 用的是 13 条假夹具，证明的是"代码逻辑对"。它证明不了
"库里这 2000 多天真数据按 available_at 取，取出来的是当时能看见的那一份"。
本文件只做三件事，全部是对着 `np.db` 里的真数据：

  A. **时刻截断**：同一数据日，`as_of` 早于 available_at 时取不到，晚于时取得到。
  B. **未来不可见**：把 as_of 设成"某一期公布前一刻"，该期必须不出现。
  C. **快照单调**：`latest_snapshot(as_of)` 的期别随 as_of 前进只增不减，且不受未来行影响。

这三条一起构成"策略层拿数据时看不见未来"的实证。

用法
===============================================================================
    $PY check_pit_live.py            # 用库里最大数据日做判据
    $PY check_pit_live.py --date 20200102
退出码：0 全通过 / 1 有断言失败 / 2 库里没有可用数据
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import pit  # noqa: E402


def _ok(cond: bool, msg: str) -> bool:
    print(("  ✅ " if cond else "  ❌ ") + msg)
    return bool(cond)


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description="真实数据上的 PIT 断言")
    ap.add_argument("--date", default=None, help="指定数据日 YYYYMMDD（默认取库内最大 receipt 日）")
    a = ap.parse_args(argv[1:])

    print("=" * 78)
    print(f"PIT 真实数据断言 | 库={pit.db_path_default()}")
    print("=" * 78)

    allrows = pit.read_all_for_audit()
    if not allrows:
        print("★ 库是空的，先跑 run_daily.py / backfill.py")
        return 2

    receipts = [r for r in allrows if r["source"].endswith("_receipt")]
    if not receipts:
        print("★ 库里没有 receipt 数据")
        return 2

    d = a.date or max(r["period_end"] for r in receipts).replace("-", "")
    d_iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    day = [r for r in receipts if r["period_end"] == d_iso]
    if not day:
        print(f"★ 库里没有 {d_iso} 的 receipt")
        return 2

    pub = day[0]["published_at"]
    avail = day[0]["available_at"]
    src = day[0]["published_src"]
    print(f"判据数据日 {d_iso}：{len(day)} 行 | published_at={pub} ({src}) | available_at={avail}")

    ok = True
    # A. 时刻截断：公布前一小时必须取不到，公布后必须取得到
    before = f"{d_iso}T00:00:00"
    after = avail
    n_before = len([r for r in pit.query_available(before, source="shfe_receipt")
                    if r["period_end"] == d_iso])
    n_after = len([r for r in pit.query_available(after, source="shfe_receipt")
                   if r["period_end"] == d_iso])
    ok &= _ok(n_before == 0, f"A1 as_of={before} 看得见 {d_iso} 的 SHFE 仓单：{n_before} 行（必须 0）")
    ok &= _ok(n_after > 0, f"A2 as_of={avail} 看得见 {d_iso} 的 SHFE 仓单：{n_after} 行（必须 >0）")

    # B. 未来不可见：把 as_of 卡在 available_at 前一秒，该期必须消失
    import datetime as _dt
    av_dt = _dt.datetime.strptime(avail, "%Y-%m-%dT%H:%M:%S")
    one_sec_before = (av_dt - _dt.timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S")
    n_edge = len([r for r in pit.query_available(one_sec_before, source="shfe_receipt")
                  if r["period_end"] == d_iso])
    ok &= _ok(n_edge == 0, f"B1 as_of=available_at-1s（{one_sec_before}）仍看不见该期：{n_edge} 行（必须 0）")

    # C. 快照单调：把数据日按时间切开，快照里的最新期别只能前进
    span = sorted({r["period_end"] for r in receipts})
    probes = [span[0], span[len(span) // 2], span[-1]]
    seq = []
    for pd_ in probes:
        as_of = f"{pd_}T23:59:59"
        snap = pit.latest_snapshot(as_of, source="shfe_receipt")
        latest = max((r["period_end"] for r in snap), default="")
        n = len(snap)
        seq.append((as_of, latest, n))
        print(f"  · as_of={as_of} ⇒ 快照 {n} 个品种，最新一期 {latest}")
    ok &= _ok(seq[0][1] <= seq[1][1] <= seq[2][1],
              "C1 快照最新期别随 as_of 单调不减")
    mid_as_of = seq[1][0]
    rows_mid = pit.query_available(mid_as_of, source="shfe_receipt")
    ok &= _ok(all(r["available_at"] <= mid_as_of for r in rows_mid),
              f"C2 as_of={mid_as_of} 返回的 {len(rows_mid)} 行全部满足 available_at <= as_of")

    # D. 全库没有一条 available_at < published_at（写入口的 CHECK 之外再确认一遍）
    bad = [r for r in allrows if r["available_at"] < r["published_at"]]
    ok &= _ok(not bad, f"D1 全库 {len(allrows)} 行中 available_at<published_at 的行数：{len(bad)}（必须 0）")

    # E. published_src 的分布必须打得出来（不许把 rule 粉饰成 measured）
    from collections import Counter
    cnt = Counter(r["published_src"] for r in allrows)
    print(f"  · published_src 分布：{dict(cnt)}（rule=按规则假设，未实测；这是事实，不是缺陷）")

    print("=" * 78)
    print("PIT 真实数据断言：" + ("通过 ✅" if ok else "失败 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
