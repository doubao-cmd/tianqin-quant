# -*- coding: utf-8 -*-
"""A24 —— `工具\\选池历史.py` 的 4 个场景断言（全部在临时目录跑，绝不碰生产 CSV）。

覆盖：
  T1 首次建档           → 写 1 行，来源=建档，较上期=首次
  T2 幂等               → 同 (重选日, 品种池) 再 sync 不新增
  T3 正常周频换池 3出3入 → 写 1 行，来源=auto，新增/移除各 3 个，顺序不乱
  T4 池变了但重选日没变 → 来源=manual，备注含「疑人工改」

跑法：python 工具\\test_选池历史.py
"""
import argparse
import csv
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(_HERE, "选池历史.py")


def _load_mod():
    spec = importlib.util.spec_from_file_location("selhist", _SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _rows(path):
    with io.open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _setup(tmp, pool, reselect):
    """写 workbench.json / selection_state.json 到临时目录，并把模块路径指过去。"""
    wb = os.path.join(tmp, "workbench.json")
    st = os.path.join(tmp, "selection_state.json")
    with io.open(wb, "w", encoding="utf-8") as f:
        json.dump({"selected": pool}, f, ensure_ascii=False)
    with io.open(st, "w", encoding="utf-8") as f:
        json.dump({"last_reselect": reselect, "selected": pool}, f, ensure_ascii=False)
    return wb, st


def _sync(m, wb, st, hist, note=None):
    m.WORKBENCH, m.SEL_STATE, m.HISTORY = wb, st, hist
    return m.cmd_sync(argparse.Namespace(note=note))


def main():
    tmp = tempfile.mkdtemp(prefix="selhist_")
    ok = fail = 0

    def chk(cond, label, extra=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  PASS  {label}")
        else:
            fail += 1
            print(f"  FAIL  {label}  {extra}")

    try:
        hist = os.path.join(tmp, "选池历史.csv")
        m = _load_mod()

        # ---------- T1 首次建档 ----------
        wb, st = _setup(tmp, ["FU", "BU", "MA", "M", "SA", "EG"], "2026-10-19")
        rc = _sync(m, wb, st, hist)
        r = _rows(hist)
        chk(rc == 0 and len(r) == 1, "T1 首次建档写 1 行", f"rc={rc} n={len(r)}")
        chk(r and r[0]["来源"] == "建档" and r[0]["较上期"] == "首次", "T1 来源=建档 / 较上期=首次")
        chk(r and r[0]["品种池"] == "FU/BU/MA/M/SA/EG" and r[0]["数量"] == "6", "T1 品种池与数量正确")

        # ---------- T2 幂等 ----------
        _sync(m, wb, st, hist)
        _sync(m, wb, st, hist)
        chk(len(_rows(hist)) == 1, "T2 重复 sync 不新增（幂等）", f"n={len(_rows(hist))}")

        # ---------- T3 正常周频换池 3 出 3 入 ----------
        wb, st = _setup(tmp, ["EG", "RU", "MA", "AG", "P", "BU"], "2026-10-26")
        _sync(m, wb, st, hist)
        r = _rows(hist)
        last = r[-1] if r else {}
        chk(len(r) == 2, "T3 换池写第 2 行", f"n={len(r)}")
        chk(last.get("来源") == "auto", "T3 来源=auto", last.get("来源"))
        chk(last.get("较上期") == "3出3入", "T3 较上期=3出3入", last.get("较上期"))
        chk(last.get("新增") == "RU/AG/P", "T3 新增=RU/AG/P（保持池内顺序）", last.get("新增"))
        chk(last.get("移除") == "FU/M/SA", "T3 移除=FU/M/SA（保持旧池顺序）", last.get("移除"))
        chk(last.get("品种池") == "EG/RU/MA/AG/P/BU", "T3 新池顺序不被排序打乱", last.get("品种池"))

        # ---------- T4 池变了但重选日没变 → 判为人工改 ----------
        wb, st = _setup(tmp, ["EG", "RU", "MA", "AG", "P", "BU", "SA"], "2026-10-26")
        _sync(m, wb, st, hist)
        r = _rows(hist)
        last = r[-1] if r else {}
        chk(len(r) == 3 and last.get("来源") == "manual", "T4 重选日同、池不同 ⇒ 来源=manual",
            f"n={len(r)} src={last.get('来源')}")
        chk("疑人工改" in (last.get("备注") or ""), "T4 备注含「疑人工改」", last.get("备注"))
        chk(last.get("新增") == "SA" and last.get("移除") == "—", "T4 新增=SA / 移除=—",
            f"+{last.get('新增')} -{last.get('移除')}")
        chk(last.get("较上期") == "0出1入", "T4 较上期=0出1入（出=移出、入=移入，不许写反）",
            last.get("较上期"))

        # ---------- T5 空池不记录 ----------
        before = len(_rows(hist))
        wb, st = _setup(tmp, [], "2026-10-27")
        _sync(m, wb, st, hist)
        chk(len(_rows(hist)) == before, "T5 selected 为空不写行", f"n={len(_rows(hist))}")

        # ---------- T6 CSV 编码：带 BOM 且可被 Excel 识别 ----------
        raw = open(hist, "rb").read(3)
        chk(raw == b"\xef\xbb\xbf", "T6 文件首 3 字节为 UTF-8 BOM", repr(raw))

        print(f"\n结果：{ok} PASS / {fail} FAIL")
        return 0 if fail == 0 else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
