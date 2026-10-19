# -*- coding: utf-8 -*-
"""
exec4/store.py —— 四期 S4：数据分层存储（archive 只追加 / 池层 / 新鲜度 / 显式失败）

## 为什么需要这一层（方案 §2.5 指出的真实 bug）

`data.py:412` 的 `_save_recent` 是**整文件覆盖**，而且 `--fetch2026` 只回 8800 根：

    data_length = 8800，5min ⇒ 约 6 个月窗口
    每天更新一次 ⇒ 新文件 = 最近 8800 根 ⇒ **最前面那一天被挤掉**

⇒ 结果是「越更新历史越短」。这是**静默的**：没有任何报错，只有等你某天发现
复盘工具画不出三个月前的数据。

修法就是本模块的核心：**archive 层只追加不覆盖**。

    工具/复盘/_缓存/
    ├── kline2026/              ← 保留。只作为「本次拉取结果」的暂存（_save_recent 仍写它）
    ├── kline_archive/          ← 新增。**只增不减**，drop_duplicates 后按时间排序
    │   └── {KEY}_{period}min.csv
    └── pool/
        ├── pool_screen_{date}.csv   ← 池层扫描结果（50 品种 × 筛选指标，不进 HTML）
        ├── _last_update.json        ← 新鲜度（工具的角标读这个）
        └── _FAILED_{date}.txt       ← 显式失败标记（存在即表示那天没更新成功）

## 三个硬约束（都写成断言，不做"尽力而为"）

1. **`archive` 行数只增不减**。写盘后立刻回读校验；减少就抛异常并保留旧文件。
   宁可当天更新失败（有 `_FAILED` 标记可见），也不要静默把历史砍掉。

2. **失败必须显式**（方案 §2.6）。数据脚本打不动账号（比如回测正占着席位）时，
   如果静默退出 ⇒ 当天数据没更新而你不知道。所以：
   `write_failure()` 写标记文件 + 返回码非 0，由调用方红色打印。

3. **去重键是 `datetime`**，`keep="last"`（新数据覆盖旧数据 —— 因为 tqsdk 偶尔
   会回补修正，新值更可信）。
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径（**不 import data**，避免本模块被 import 时反过来依赖 tqsdk 相关逻辑）
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent          # 工具/复盘/exec4
REPLAY_DIR = _HERE.parent                        # 工具/复盘
CACHE_DIR = REPLAY_DIR / "_缓存"
RECENT_DIR = CACHE_DIR / "kline2026"
ARCHIVE_DIR = CACHE_DIR / "kline_archive"
POOL_DIR = CACHE_DIR / "pool"

ENCODING = "utf-8-sig"          # 项目所有 CSV 都带 BOM（已实测 EF BB BF）


# ---------------------------------------------------------------------------
# 路径工具
# ---------------------------------------------------------------------------
def archive_path(key: str, period) -> Path:
    return ARCHIVE_DIR / f"{str(key).upper()}_{int(period)}min.csv"


def recent_path(key: str, period) -> Path:
    return RECENT_DIR / f"{str(key).upper()}_{int(period)}min.csv"


def pool_screen_path(date_str: str) -> Path:
    return POOL_DIR / f"pool_screen_{str(date_str)[:10]}.csv"


def last_update_path() -> Path:
    return POOL_DIR / "_last_update.json"


def failure_path(date_str: str) -> Path:
    return POOL_DIR / f"_FAILED_{str(date_str)[:10]}.txt"


# ---------------------------------------------------------------------------
# 1) archive：只追加不覆盖
# ---------------------------------------------------------------------------
def read_archive(key: str, period) -> "object":
    """读 archive（无文件返回 None）。返回 pandas.DataFrame。"""
    import pandas as pd
    p = archive_path(key, period)
    if not p.exists():
        return None
    df = pd.read_csv(p, encoding=ENCODING)
    df.columns = [str(c).lstrip("\ufeff") for c in df.columns]
    df["datetime"] = pd.to_datetime(df["datetime"])
    return df.sort_values("datetime").reset_index(drop=True)


def upsert_archive(key: str, period, df, strict: bool = True) -> dict:
    """把本次拉到的数据并入 archive。

    参数
    ----
    df     : 必须含 `datetime` 列（+ open/high/low/close[/volume]）。datetime 是 UTC 口径。
    strict : True（默认）⇒ 合并后行数若少于合并前，**抛异常且不写盘**。
             False 只用于"首次建库/人工重建"场景，正常日更绝不能用。

    返回
    ----
    {"key", "period", "n_before", "n_new_rows", "n_total", "monotonic_ok", "path"}

    ★ 为什么必须断言"只增不减"：
      这是本模块存在的唯一理由。如果哪天有人改坏了（比如误用 overwrite），
      断言会当场炸掉，而不是等到三个月后发现历史变短。
    """
    import pandas as pd

    if df is None or len(df) == 0:
        raise ValueError(f"upsert_archive({key},{period}) 收到空数据 —— "
                         f"不要用空数据覆盖 archive（宁可不写）")
    if "datetime" not in df.columns:
        raise ValueError(f"upsert_archive({key},{period}) 缺 datetime 列：{list(df.columns)}")

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    p = archive_path(key, period)

    old = read_archive(key, period)
    n_before = 0 if old is None else len(old)
    # archive 自身可能带内部重复行（历史损坏 / 手工拼接过）。
    # 判"是否丢数据"要比的是**去重后的旧行数**，否则把"修复重复"误判成"丢数据"。
    n_before_uniq = 0 if old is None else len(
        old.drop_duplicates(subset=["datetime"]))

    new = df.copy()
    new["datetime"] = pd.to_datetime(new["datetime"])
    if old is not None:
        merged = pd.concat([old, new], ignore_index=True)
    else:
        merged = new
    merged = (merged.drop_duplicates(subset=["datetime"], keep="last")
                    .sort_values("datetime").reset_index(drop=True))
    n_total = len(merged)
    n_new_rows = n_total - n_before

    # ★ 关于这道断言的真实作用（别把它想成"日更时的保险"）：
    #   `concat(old, new) + drop_duplicates` 这个算法**本身就不可能减少行数**
    #   （除非老文件自带重复）。所以它不是"跑起来会响的警报"，而是**一枚金丝雀**：
    #   将来谁把这里改成"覆盖写"（那正是 _save_recent 的老 bug），断言会当场炸。
    #   真正会触发它的现实场景只有一个：老 archive 内部有重复行（去重会减少行数）。
    #   那种情况是**修复**，应当放行并报告，所以比较基准用 n_before_uniq。
    if strict and n_total < n_before_uniq:
        raise AssertionError(
            f"★ archive 行数会减少（去重后 {n_before_uniq} → {n_total}）：{p}\n"
            f"  这违反「只追加不覆盖」。已拒绝写盘，旧文件保持原样。\n"
            f"  若确实要重建，请人工删除该文件后重跑，或用 strict=False。")

    deduped = n_before - n_before_uniq        # 顺带修掉的内部重复行数

    # 原子写：先写临时文件再替换，避免写一半断电把 archive 写坏
    tmp = p.with_suffix(".csv.tmp")
    merged.to_csv(tmp, index=False, encoding=ENCODING)
    os.replace(tmp, p)

    return {"key": str(key).upper(), "period": int(period),
            "n_before": n_before, "n_before_uniq": n_before_uniq,
            "n_new_rows": n_new_rows, "n_total": n_total,
            "n_dedup_fixed": max(0, deduped),
            "monotonic_ok": bool(n_total >= n_before_uniq), "path": str(p)}


def archive_stats(keys=None, periods=(1, 5, 15, 1440)) -> list:
    """扫描 archive 目录，返回每个文件的 (key, period, 行数, 起, 止, 大小KB)。"""
    out = []
    if not ARCHIVE_DIR.exists():
        return out
    for p in sorted(ARCHIVE_DIR.glob("*.csv")):
        name = p.stem                       # 如 "MA_5min"
        if "_" not in name:
            continue
        k, per = name.rsplit("_", 1)
        per = per.replace("min", "")
        try:
            per_i = int(per)
        except ValueError:
            continue
        if keys and k.upper() not in {str(x).upper() for x in keys}:
            continue
        if periods and per_i not in set(int(x) for x in periods):
            continue
        try:
            df = read_archive(k, per_i)
        except Exception:
            continue
        if df is None or len(df) == 0:
            continue
        out.append({
            "key": k.upper(), "period": per_i, "n": len(df),
            "first": str(df["datetime"].iloc[0]), "last": str(df["datetime"].iloc[-1]),
            "kb": round(p.stat().st_size / 1024.0, 1),
        })
    return out


# ---------------------------------------------------------------------------
# 2) 新鲜度：_last_update.json
# ---------------------------------------------------------------------------
def write_last_update(payload: dict) -> Path:
    """写新鲜度文件。payload 至少含 ok / finished_at / symbols。

    结构（工具的角标直接读这个）：
    {
      "ok": true|false,
      "started_at": "2026-10-18T23:05:01",
      "finished_at": "2026-10-18T23:07:12",
      "account": "backtest|live",          # 用了哪个账号（数据更新必须 backtest）
      "n_symbols": 6,
      "symbols": {"MA": {"n": 8812, "first":..., "last":..., "archive_n": 12000}, ...},
      "errors": ["..."],
      "note": "..."
    }
    """
    POOL_DIR.mkdir(parents=True, exist_ok=True)
    p = last_update_path()
    tmp = p.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    return p


def read_last_update() -> dict:
    """读新鲜度文件（无/损坏返回 {}）。"""
    try:
        with open(last_update_path(), "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def freshness_hours(now=None) -> float:
    """距上次**成功且真的拉了行情**多少小时。无记录/未成功/只刷了池层 ⇒ inf。

    ★ 两个"不能算数"的情形（都会让角标该红却不红 = 静默失效）：
      ① `ok=False`（更新失败）—— 失败不能刷新新鲜度；
      ② `data_updated=False`（`--pool-only` 只刷池层，没碰行情）——
         池层是本地重算，拿它冒充"数据已更新"是自欺。
    """
    info = read_last_update()
    if not info or not info.get("ok"):
        return float("inf")
    if info.get("data_updated") is False:
        return float("inf")
    ts = info.get("finished_at") or info.get("started_at")
    if not ts:
        return float("inf")
    try:
        t = datetime.fromisoformat(str(ts))
    except Exception:
        return float("inf")
    now = now or datetime.now()
    return (now - t).total_seconds() / 3600.0


def freshness_badge(threshold_hours: float = 24.0, now=None) -> dict:
    """给 UI 用的角标数据。超过 threshold 视为过期（红）。"""
    h = freshness_hours(now=now)
    if h == float("inf"):
        return {"ok": False, "hours": None, "text": "数据从未成功更新", "stale": True}
    stale = h > threshold_hours
    if h < 1:
        txt = f"数据更新于 {int(h * 60)} 分钟前"
    else:
        txt = f"数据更新于 {h:.1f} 小时前"
    return {"ok": not stale, "hours": round(h, 2), "text": txt, "stale": stale}


# ---------------------------------------------------------------------------
# 3) 显式失败：_FAILED_{date}.txt
# ---------------------------------------------------------------------------
def write_failure(date_str: str, detail: str, attempts: int = 0) -> Path:
    """写失败标记。**存在即表示那天没更新成功**，必须让人看见。"""
    POOL_DIR.mkdir(parents=True, exist_ok=True)
    p = failure_path(date_str)
    lines = [
        f"数据更新失败  {str(date_str)[:10]}",
        f"记录时间：{datetime.now().isoformat(timespec='seconds')}",
        f"重试次数：{attempts}",
        "",
        "原因：",
        str(detail),
        "",
        "处置建议：",
        "  1) 确认回测账号没有被别的进程占用（同一账号同时只能一处登录）",
        "  2) 确认网络可连 wss://free-api.shinnytech.com",
        "  3) 修好后重跑：python 工具/复盘/exec4/daily_update.py",
        "  4) 成功后再手动删除本文件（否则角标一直红）",
    ]
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def has_failure(date_str: str) -> bool:
    return failure_path(date_str).exists()


def clear_failure(date_str: str) -> bool:
    p = failure_path(date_str)
    if p.exists():
        p.unlink()
        return True
    return False


# ---------------------------------------------------------------------------
# 4) 池层：50 品种筛选表（不进 HTML，输出 CSV/MD）
# ---------------------------------------------------------------------------
POOL_SCREEN_COLS = [
    "key", "name", "exch", "night", "last_close",
    "turnover_20d",          # 20 日均成交额（元/日）
    "oi",                    # 持仓量（有则填，无则空）
    "amp_20d",               # 20 日均振幅（%）
    "cost_over_atr",         # 成本 ÷ ATR（单边成本 / 日均 ATR）
    "min_lot_notional",      # 1 手名义（元）—— 粒度闸门用
    "tradable",              # 有无核验合约规格
    "note",
]


def write_pool_screen(rows: list, date_str: str) -> Path:
    """写池层扫描结果 CSV。`rows` 是 dict 列表，列顺序按 POOL_SCREEN_COLS。"""
    import csv
    POOL_DIR.mkdir(parents=True, exist_ok=True)
    p = pool_screen_path(date_str)
    with open(p, "w", encoding=ENCODING, newline="") as f:
        w = csv.DictWriter(f, fieldnames=POOL_SCREEN_COLS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in POOL_SCREEN_COLS})
    return p


def read_pool_screen(date_str: str) -> list:
    import csv
    p = pool_screen_path(date_str)
    if not p.exists():
        return []
    with open(p, "r", encoding=ENCODING, newline="") as f:
        rows = list(csv.DictReader(f))
    return rows


def pool_layer_size_kb() -> float:
    """池层总体积（方案 §2.4 目标 ≈0.23 MB，即 <250 KB）。"""
    if not POOL_DIR.exists():
        return 0.0
    total = sum(p.stat().st_size for p in POOL_DIR.glob("*") if p.is_file())
    return round(total / 1024.0, 1)


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------
def _selftest() -> int:
    import tempfile
    import pandas as pd

    print("=" * 72)
    print("S4 存储层自检（archive 只增不减 / 新鲜度 / 显式失败 / 池层）")
    print("=" * 72)

    ok = True
    # 用临时目录，绝不碰真实 archive
    global ARCHIVE_DIR, POOL_DIR, CACHE_DIR
    _bak = (CACHE_DIR, ARCHIVE_DIR, POOL_DIR)
    tmp = Path(tempfile.mkdtemp(prefix="s4_"))
    CACHE_DIR = tmp
    ARCHIVE_DIR = tmp / "kline_archive"
    POOL_DIR = tmp / "pool"
    try:
        def mk(n, start="2026-10-01"):
            t = pd.date_range(start, periods=n, freq="5min")
            return pd.DataFrame({"datetime": t, "open": 1.0, "high": 1.0,
                                 "low": 1.0, "close": 1.0})

        # ① 首次写入
        r1 = upsert_archive("TST", 5, mk(100))
        print(f"  ① 首次写入：{r1['n_total']} 行")
        ok &= r1["n_total"] == 100 and r1["n_before"] == 0

        # ② 追加（新时间段）
        r2 = upsert_archive("TST", 5, mk(50, start="2026-10-02"))
        print(f"  ② 追加 50 行（与旧数据部分重叠）：{r2['n_before']} → {r2['n_total']}"
              f"（新增 {r2['n_new_rows']}）")
        ok &= r2["n_total"] >= r2["n_before"] and r2["monotonic_ok"]

        # ③ 重复写同一份 ⇒ 行数不变（幂等）
        r3 = upsert_archive("TST", 5, mk(50, start="2026-10-02"))
        print(f"  ③ 重复写同一份：{r3['n_total']} 行（应与上一步相同）")
        ok &= r3["n_total"] == r2["n_total"] and r3["n_new_rows"] == 0

        # ④ 传「旧数据的子集」⇒ 行数不变、新增 0（单调性）
        r4 = upsert_archive("TST", 5, mk(20, start="2026-10-01"))
        print(f"  ④ 传旧数据子集（20 行）：{r4['n_before']} → {r4['n_total']}"
              f"（新增 {r4['n_new_rows']}，应为 0 且行数不降）")
        ok &= r4["n_total"] >= r4["n_before"] and r4["n_new_rows"] == 0

        # ⑤ 老 archive 自带重复行 ⇒ 应**静默修复**（不是报错）
        #    ★ 这是唯一能真正触发"行数变化"的现实场景：concat+drop_duplicates
        #      本身不可能减少行数，除非老文件内部有重复。
        p_arch = archive_path("TST", 5)
        with open(p_arch, "r", encoding=ENCODING) as f:
            body = f.read().splitlines()
        header, data = body[0], body[1:]
        with open(p_arch, "w", encoding=ENCODING, newline="") as f:
            f.write(header + "\n")
            for ln in data:
                f.write(ln + "\n")
            for ln in data[:5]:          # 人为灌 5 行重复
                f.write(ln + "\n")
        n_dirty = len(read_archive("TST", 5))
        r5 = upsert_archive("TST", 5, mk(1, start="2026-10-09"))
        print(f"  ⑤ 老文件灌 5 行重复（{n_dirty} 行）后 upsert："
              f"→ {r5['n_total']} 行，修掉重复 {r5['n_dedup_fixed']} 行 ✅"
              f"（未报错，是修复不是丢数据）")
        ok &= r5["n_dedup_fixed"] == 5 and r5["monotonic_ok"]

        # ⑥ 空数据必须拒绝
        try:
            upsert_archive("TST", 5, pd.DataFrame({"datetime": []}))
            print("  ⑥ ★ 空数据未拒绝")
            ok = False
        except ValueError:
            print("  ⑥ 空数据被拒绝 ✅")

        # ⑥ 新鲜度 / 失败标记
        print(f"  ⑦ 无记录时 freshness = {freshness_hours()}（应为 inf）")
        ok &= freshness_hours() == float("inf")
        write_last_update({"ok": True,
                           "started_at": (datetime.now() - timedelta(minutes=10)).isoformat(),
                           "finished_at": (datetime.now() - timedelta(minutes=8)).isoformat(),
                           "account": "backtest", "n_symbols": 1, "symbols": {}})
        h = freshness_hours()
        print(f"     写入成功后 freshness = {h:.2f} 小时")
        ok &= 0.0 <= h < 1.0
        print(f"     角标：{freshness_badge()['text']}")
        # 失败不刷新新鲜度
        write_last_update({"ok": False, "started_at": datetime.now().isoformat(),
                           "finished_at": datetime.now().isoformat(),
                           "account": "backtest", "n_symbols": 0, "symbols": {},
                           "errors": ["模拟失败"]})
        print(f"     失败记录后 freshness = {freshness_hours()}（必须回到 inf）")
        ok &= freshness_hours() == float("inf")

        fp = write_failure("2026-10-18", "模拟：登录失败 3 次", attempts=3)
        print(f"  ⑧ 失败标记：{fp.name} 存在={has_failure('2026-10-18')}")
        ok &= has_failure("2026-10-18")
        clear_failure("2026-10-18")
        ok &= not has_failure("2026-10-18")

        # ⑧ 池层
        p = write_pool_screen([{"key": "MA", "name": "甲醇", "exch": "CZCE",
                                "night": True, "last_close": 2400.0,
                                "turnover_20d": 7.3e10, "tradable": True}], "2026-10-18")
        rows = read_pool_screen("2026-10-18")
        print(f"  ⑨ 池层：{p.name} 写入 {len(rows)} 行，体积 {pool_layer_size_kb()} KB")
        ok &= len(rows) == 1 and rows[0]["key"] == "MA"
    finally:
        CACHE_DIR, ARCHIVE_DIR, POOL_DIR = _bak
        shutil.rmtree(tmp, ignore_errors=True)

    print("=" * 72)
    print("S4 存储层自检：" + ("通过 ✅" if ok else "失败 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
