# -*- coding: utf-8 -*-
"""
工具/_np_data/store.py —— 非价格数据的抓取底座（D6/D7）

职责**只有四件**，不含业务聚合（聚合在各 fetch_*.py 里）：
  1. **公布时刻解析**：实测优先，无实测则按规则假设，并**如实标记来源**
  2. **原始响应留存**：每个源每天的第一份原始数据落 _raw/，事后可复核
  3. **新鲜度**：_last_update.json（「没真拉就不刷新」）
  4. **显式失败**：_FAILED_{date}.txt + 退出码 1（不许静默成功）

写入本身复用 `pit.py` 的 `write_records()`（只追加 / 冲突拒绝 / 结构断言），
本模块不重写那一套——重复实现两套写入逻辑，等于给未来留一个分叉。

===============================================================================
为什么要区分 published_src（measured / rule）
===============================================================================
交易所"15:30 公布"这种说法**无法证伪**，只有 `--watch` 打出的 first_seen 才是证据。
但 --watch 需要进程在收盘后存活数小时，本环境（沙箱回收长驻进程）跑不满窗口，
所以现阶段绝大多数源只能是 `rule`。

把这件事写进列里，而不是写进文档里，理由是：
  - 文档会过期，列不会；
  - 将来做因子时，`WHERE published_src='measured'` 一句就能筛出"有证据"的子集；
  - 汇报里说"11 个源都没实测"是事实，说"已按交易所规则处理"就是粉饰。

===============================================================================
规则时刻的取法（保守方向）
===============================================================================
对**日线** bar（15:00 收盘）：仓单 15:30 公布 ⇒ 当日数据当日就**已经**不可用，
必须 T+1。所以规则值只要 > 15:00 就是安全方向；再叠加 pit.py 的 margin（+1h）
属于冗余保守，不是精确。

对分钟线/夜盘（21:00 起），精确时刻才有意义 —— 那时必须换成 measured。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import traceback
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import pit  # noqa: E402

# ---------------------------------------------------------------------------
# 目录
# ---------------------------------------------------------------------------
DB_DIR = _HERE
RAW_DIR = _HERE / "_raw"
LAST_UPDATE = _HERE / "_last_update.json"
# ★ 失败标记必须**按源分开**（2026-10-20 修）：
#   四个抓取器串行跑时，若共用一个 _FAILED_{date}.txt，后一个源成功会把它清掉
#   ⇒ 前一个源的失败被静默抹掉。这种"失败标记互相清除"和"空表当成功"是同一类病。
FAILED_FMT = "_FAILED_{date}_{source}.txt"
MEASURED_FMT = "实测首见_{date}.json"


def raw_path(date: str, source: str) -> Path:
    return RAW_DIR / str(date)[:10].replace("-", "") / f"{source}.json"


def failed_path(date: str, source: str) -> Path:
    return DB_DIR / FAILED_FMT.format(date=str(date)[:10], source=source)


def measured_path(date: str) -> Path:
    return _HERE / MEASURED_FMT.format(date=str(date)[:10])


# ---------------------------------------------------------------------------
# 规则公布时刻（无实测时的兜底；每一条都是**假设**，不是证据）
# ---------------------------------------------------------------------------
# 值 = 'HH:MM'，表示"该交易日的这个时刻之后才可能拿到当日数据"。
RULE_PUBLISHED = {
    "shfe_receipt": "15:30",
    "czce_receipt": "15:30",
    "gfex_receipt": "15:30",
    "shfe_rank":    "17:00",   # 持仓排名通常晚于仓单
    "czce_rank":    "17:00",
    "gfex_rank":    "17:00",
    "spot_basis":   "18:00",   # 实测 14:28 仍无当日数据，只能给更晚的保守值
    "inv_99qh":     "16:00",
}

# 规则值本身的可信度提示，随每行一起写进 np_data 的 note 位（raw_url 不占位）
RULE_NOTE = {
    "shfe_receipt": "T日15:30(假设,未实测)",
    "czce_receipt": "T日15:30(假设,未实测)",
    "gfex_receipt": "T日15:30(假设,未实测)",
    "shfe_rank":    "T日17:00(假设,未实测;当日vsT+1未定)",
    "czce_rank":    "T日17:00(假设,未实测)",
    "gfex_rank":    "T日17:00(假设,未实测)",
    "spot_basis":   "T日18:00(假设,未实测;实测14:28仍无当日数据)",
    "inv_99qh":     "T日16:00(假设,未实测;该源周频+日频混排,不能写死周五)",
}


# ---------------------------------------------------------------------------
# 1) 公布时刻解析
# ---------------------------------------------------------------------------
def resolve_published(source: str, date: str) -> tuple[str, str]:
    """返回 (published_at_iso, published_src)。

    实测优先：若 `实测首见_{date}.json` 里有该源的 first_seen，用它（measured）。
    注意 first_seen 是**上界**（我们观察到它已经在了），真实公布时刻 <= first_seen，
    所以拿它当 published_at 是保守方向，安全。
    """
    d = str(date).replace("-", "")[:8]
    mp = measured_path(d)
    if mp.exists():
        try:
            m = json.loads(mp.read_text(encoding="utf-8"))
            hit = m.get(source)
            if isinstance(hit, dict) and hit.get("first_seen"):
                return pit.norm_dt(hit["first_seen"], "first_seen"), "measured"
            if isinstance(hit, str) and hit:
                return pit.norm_dt(hit, "first_seen"), "measured"
        except Exception:
            # 实测文件坏了不能当"没实测"处理——那是把异常吞成默认值
            raise RuntimeError(
                f"实测首见文件存在但解析失败：{mp}\n"
                f"  拒绝回退到规则值（会把「有证据」静默降级成「假设」）。请先修文件。")

    hhmm = RULE_PUBLISHED.get(source)
    if not hhmm:
        raise KeyError(
            f"源 {source!r} 既无实测首见，也没有在 RULE_PUBLISHED 里登记规则时刻。\n"
            f"  不许猜：要么跑 --watch 实测，要么显式登记一个保守值并写清理由。")
    iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}T{hhmm}:00"
    return pit.norm_dt(iso, "published_at"), "rule"


# ---------------------------------------------------------------------------
# 2) 原始响应留存（第一份即冻结，不覆盖）
# ---------------------------------------------------------------------------
# ★ 2026-10-20 新增：回填开关。
#   日更单日 _raw ≈ 2.3MB；历史回填 2000 多个交易日 ≈ 5GB。
#   任务书只要求「原始响应至少存一次」——日更那一路每天都在存，回填可以关掉。
#   默认 True：日更行为一个字没变；只有 `backfill.py` 会把这里置 False。
SAVE_RAW_ENABLED = True


def save_raw(date: str, source: str, payload, overwrite: bool = False) -> Path:
    """把原始数据存成 json。默认**不覆盖**：同一源同一天的首份原始响应才是证据。"""
    p = raw_path(date, source)
    if not SAVE_RAW_ENABLED:
        return p
    if p.exists() and not overwrite:
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    os.replace(tmp, p)
    return p


def df_to_records(obj):
    """把 akshare 的 dict[str, DataFrame] / DataFrame 转成可 json 化的结构。"""
    import pandas as pd
    if obj is None:
        return None
    if isinstance(obj, dict):
        return {str(k): (v.to_dict("records") if hasattr(v, "to_dict") else v)
                for k, v in obj.items()}
    if isinstance(obj, pd.DataFrame):
        return obj.to_dict("records")
    return obj


# ---------------------------------------------------------------------------
# 3) 新鲜度（没真拉就不刷新）
# ---------------------------------------------------------------------------
def write_last_update(payload: dict) -> Path:
    tmp = LAST_UPDATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, LAST_UPDATE)
    return LAST_UPDATE


def read_last_update() -> dict:
    if not LAST_UPDATE.exists():
        return {}
    try:
        return json.loads(LAST_UPDATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# 4) 显式失败
# ---------------------------------------------------------------------------
def write_failure(date: str, source: str, detail: str) -> Path:
    p = failed_path(date, source)
    p.parent.mkdir(parents=True, exist_ok=True)
    body = (f"date={date}\nsource={source}\n"
            f"time={datetime.now().replace(microsecond=0).isoformat()}\n\n{detail}\n")
    p.write_text(body, encoding="utf-8")
    return p


def clear_failure(date: str, source: str) -> bool:
    """只清**本源**的标记。传 source=None 才清该日全部（仅供人工/测试使用）。"""
    if source is None:
        n = 0
        for p in DB_DIR.glob(f"_FAILED_{str(date)[:10]}_*.txt"):
            p.unlink()
            n += 1
        return n > 0
    p = failed_path(date, source)
    if p.exists():
        p.unlink()
        return True
    return False


def has_failure(date: str, source: str = None) -> bool:
    if source is None:
        return any(DB_DIR.glob(f"_FAILED_{str(date)[:10]}_*.txt"))
    return failed_path(date, source).exists()


def failed_sources(date: str) -> list:
    pre = f"_FAILED_{str(date)[:10]}_"
    return sorted(p.name[len(pre):-4] for p in DB_DIR.glob(pre + "*.txt"))


# ---------------------------------------------------------------------------
# 主流程：一个日期、多个源的抓取 → 写库
# ---------------------------------------------------------------------------
def run(sources: dict, date: str, out=print) -> int:
    """sources: {source_name: callable(date_str) -> list[np_data row dict]}

    每个 fetch 函数负责自己的**聚合语义**（这是我们最容易出错的地方：
    SHFE 的 ROWSTATUS 合计行、CZCE 的"小计/总计"行、rank 的 rank=999 合计行）。

    返回 0 = 全部成功；1 = 至少一个源失败（已写 _FAILED，不许静默）。
    """
    d = str(date).replace("-", "")[:8]
    t0 = datetime.now().replace(microsecond=0)
    results = {}
    failures = []

    for name, fn in sources.items():
        try:
            rows = fn(d)
            if not rows:
                # 空批次也是失败：抓取成功但 0 行，几乎一定是解析/结构变了。
                # 「静默返回空表」在 akshare 上已踩过 4 次（get_shfe_receipt_2 / GFEX / 99qh / shfe_receipt_2）。
                raise RuntimeError(f"{name} 返回 0 行 —— 视为失败，请检查交易所 schema 是否变化")
            stat = pit.write_records(rows)
            results[name] = {"ok": True, "rows": len(rows), **stat,
                             "published_src": rows[0].get("published_src", "?")}
            clear_failure(d, name)          # 只清自己那一个标记
            out(f"  {name:<15} OK   {len(rows):>4} 行  "
                f"新增 {stat['inserted']} / 幂等 {stat['unchanged']}"
                f"  公布时刻来源={rows[0].get('published_src','?')}")
        except Exception as e:
            failures.append((name, f"{type(e).__name__}: {e}"))
            results[name] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
            write_failure(d, name, f"{type(e).__name__}: {e}")   # 每源一个文件，互不覆盖
            out(f"  {name:<15} FAIL {type(e).__name__}: {str(e)[:120]}")

    elapsed = (datetime.now().replace(microsecond=0) - t0).total_seconds()

    if failures:
        names = [n for n, _ in failures]
        out(f"  ★ 失败 {len(failures)} 个源（{','.join(names)}）→ "
            f"{FAILED_FMT.format(date=d, source='<源名>')}（退出码 1）")
        write_last_update({
            "ok": False, "date": d, "started_at": t0.isoformat(),
            "elapsed_s": elapsed, "results": results,
            "failed": names,
        })
        return 1

    write_last_update({
        "ok": True, "date": d, "started_at": t0.isoformat(),
        "elapsed_s": elapsed, "results": results, "failed": [],
    })
    out(f"  全部成功；{LAST_UPDATE.name} 已刷新")
    return 0


def _selftest() -> int:
    """只碰临时库与临时目录。"""
    import tempfile
    ok = True
    tmp = Path(tempfile.mkdtemp(prefix="nps_"))
    try:
        global DB_DIR, RAW_DIR, LAST_UPDATE, _HERE
        old = (DB_DIR, RAW_DIR, LAST_UPDATE)
        _HERE = tmp
        DB_DIR = tmp
        RAW_DIR = tmp / "_raw"
        LAST_UPDATE = tmp / "_last_update.json"

        print("=" * 72)
        print("store 自检（公布时刻解析 / 空批次判失败 / 失败标记 / 原子写）")
        print("=" * 72)

        # ① 规则值
        pub, src = resolve_published("shfe_receipt", "20261016")
        print(f"  ① 无实测 ⇒ {pub} src={src}（应 15:30 / rule）")
        ok &= pub == "2026-10-16T15:30:00" and src == "rule"

        # ② 实测值优先
        measured_path("20261016").write_text(
            json.dumps({"shfe_receipt": {"first_seen": "2026-10-16T15:41:00"}}),
            encoding="utf-8")
        pub, src = resolve_published("shfe_receipt", "20261016")
        print(f"  ② 有实测 ⇒ {pub} src={src}（应 15:41 / measured）")
        ok &= pub == "2026-10-16T15:41:00" and src == "measured"

        # ③ 未登记源不许猜
        try:
            resolve_published("不存在的源", "20261016")
            print("  ③ ★ 未登记源竟能解析")
            ok = False
        except KeyError:
            print("  ③ 未登记源 ⇒ KeyError ✅（不许猜）")

        # ④ 空批次必须判失败（静默空表是最危险的失败形态）
        db = tmp / "t.db"
        rc = run({"empty_src": lambda d: []}, "20261016", out=lambda *a: None)
        print(f"  ④ 空批次 ⇒ 退出码 {rc}（应 1）+ 本源失败标记={has_failure('20261016', 'empty_src')}")
        ok &= rc == 1 and has_failure("20261016", "empty_src")

        # ⑤ 正常批次只清自己，**不许清别的源**（串行跑四个抓取器时的真实事故）
        def good(d):
            return [pit.make_row("x_test", "RB", "m", "2026-10-16", "2026-10-16",
                                 "2026-10-16T15:30", 1.0, published_src="rule")]
        os.environ["NP_DB"] = str(db)
        rc = run({"good_src": good}, "20261016", out=lambda *a: None)
        survived = has_failure("20261016", "empty_src")
        print(f"  ⑤ 正常批次 ⇒ 退出码 {rc}；good_src 自己已清={not has_failure('20261016', 'good_src')}"
              f"；empty_src 标记仍在={survived}（应 True）")
        ok &= rc == 0 and not has_failure("20261016", "good_src") and survived

        # ⑥ 人工 clear(source=None) 才清该日全部
        clear_failure("20261016", None)
        print(f"  ⑥ 显式清空该日 ⇒ 仍有失败标记={has_failure('20261016')}（应 False）；"
              f"失败源清单={failed_sources('20261016')}")
        ok &= not has_failure("20261016")

        # ⑦ 新鲜度落盘 + 重启可读
        lu = read_last_update()
        print(f"  ⑦ _last_update.json ok={lu.get('ok')} date={lu.get('date')}")
        ok &= lu.get("ok") is True and lu.get("date") == "20261016"

        DB_DIR, RAW_DIR, LAST_UPDATE = old
    finally:
        os.environ.pop("NP_DB", None)
        shutil.rmtree(tmp, ignore_errors=True)

    print("=" * 72)
    print("store 自检：" + ("通过 ✅" if ok else "失败 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
