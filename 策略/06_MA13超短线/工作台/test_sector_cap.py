# -*- coding: utf-8 -*-
"""
test_sector_cap.py —— 板块配额单元测试（离线、无网络、不依赖 tqsdk）

覆盖任务书 §4.5：
  1. cap_fill 四边界：正常 / cap 卡住（每板块≤2 仍补满）/ 板块不足（overflow）/ ranked 为空
  2. sector_cap=None → 离线重选 51 周与 bt_selections.json 逐周一致（回归）
  3. sector_of 未知 key 必须抛 KeyError；已知 key 正常归类（含补全的 RU）
"""
from __future__ import annotations
import sys, os, json, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sectors import sector_of, cap_fill, SECTOR
from backtest_year import generate_selections, load_bt_daily, load_workbench


def test_cap_fill_normal():
    """正常：6 个不同板块、cap=2 → 取满 6，overflow=False。"""
    ranked = [("MA", 9), ("RB", 8), ("CU", 7), ("AU", 6), ("M", 5), ("SC", 4)]
    sel, over = cap_fill(ranked, 6, 2)
    assert sel == ["MA", "RB", "CU", "AU", "M", "SC"], sel
    assert over is False
    print("test_cap_fill_normal OK")


def test_cap_fill_cap_blocks():
    """cap 卡住：同板块多只时只取前 2，但可从其他板块补满 6（overflow=False）。"""
    ranked = [("MA", 20), ("TA", 19), ("EG", 18),    # 化工前 3
              ("RB", 17), ("HC", 16), ("I", 15),     # 黑色前 3
              ("M", 14), ("Y", 13), ("P", 12)]       # 农产品前 3
    sel, over = cap_fill(ranked, 6, 2)
    assert sel == ["MA", "TA", "RB", "HC", "M", "Y"], sel
    assert over is False
    c = collections.Counter(sector_of(k) for k in sel)
    assert all(v <= 2 for v in c.values()), "每板块必须 ≤2"
    print("test_cap_fill_cap_blocks OK")


def test_cap_fill_sector_insufficient():
    """板块不足：只有 2 个板块有货、cap=2、n=6 → 最多 4，overflow=True。"""
    ranked = [("MA", 20), ("TA", 19), ("RB", 18), ("HC", 17), ("EG", 16)]
    sel, over = cap_fill(ranked, 6, 2)
    assert over is True, "板块不足应 overflow"
    assert len(sel) == 4, sel
    print("test_cap_fill_sector_insufficient OK")


def test_cap_fill_empty():
    """ranked 为空 → selected=[]，overflow=True。"""
    sel, over = cap_fill([], 6, 2)
    assert sel == [] and over is True
    print("test_cap_fill_empty OK")


def test_sector_of_unknown_raises():
    """未知 key 必须抛 KeyError；已知 key 正常归类。"""
    for bad in ("ZZ", "XYZ", "NOPE", ""):
        try:
            sector_of(bad)
            raise AssertionError(f"{bad} 应抛 KeyError")
        except KeyError:
            pass
    assert sector_of("MA") == "化工"
    assert sector_of("RU") == "化工"   # 补全项：天然橡胶，原 §3 表漏列
    assert sector_of("RB") == "黑色"
    assert sector_of("AU") == "贵金属"
    # 表必须覆盖全部 57 个 universe（去重后 key 数）
    assert len(set(k.upper() for v in SECTOR.values() for k in v)) >= 57
    print("test_sector_of_unknown_raises OK")


def test_sector_cap_none_matches_baseline():
    """sector_cap=None 时，离线重选必须与基线 bt_selections.json 逐周一致（回归）。"""
    cfg = load_workbench()
    sc = cfg.get("selection", {}) or {}
    params = {"vol_window_days": int(sc.get("vol_window_days", 20)),
              "vol_top_m": int(sc.get("vol_top_m", 20)),
              "ret_window_days": int(sc.get("ret_window_days", 3)),
              "ret_top_n": int(sc.get("ret_top_n", 6)),
              "reselect_min_days": int(sc.get("reselect_min_days", 7))}
    dailies = load_bt_daily()
    axis = sorted({d for v in dailies.values() for d in v["td"]})
    base_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "data", "bt_selections.json")
    with open(base_path, encoding="utf-8") as f:
        base = json.load(f)
    base_recs = {r["reselect_date"]: r["selected"] for r in base["records"]}
    recs, _union, _gaps = generate_selections(dailies, axis, params, sector_cap=None)
    assert len(recs) == len(base["records"]), \
        f"重选次数 {len(recs)} vs 基线 {len(base['records'])}"
    mism = 0
    for r in recs:
        if base_recs.get(r["reselect_date"]) != r["selected"]:
            mism += 1
            print(f"  差异 {r['reselect_date']}: 基线 {base_recs.get(r['reselect_date'])} "
                  f"重算 {r['selected']}")
    assert mism == 0, f"有 {mism} 周不一致"
    print(f"test_sector_cap_none_matches_baseline OK（{len(recs)} 周逐周一致）")


if __name__ == "__main__":
    test_cap_fill_normal()
    test_cap_fill_cap_blocks()
    test_cap_fill_sector_insufficient()
    test_cap_fill_empty()
    test_sector_of_unknown_raises()
    test_sector_cap_none_matches_baseline()
    print("\n板块配额单元测试通过 ✅")
