# -*- coding: utf-8 -*-
"""select_pool_sat.py —— 周六独立换池任务（方案 A，2026-10-31 用户拍板）

为什么单独一个脚本，而不是直接跑 apply_pool.py：

  1. **apply_pool 用缓存**。`build_universe_daily(use_cache=True)` 不校验新鲜度
     （只判 len>=5）。2026-10-31 实测缓存末根停在 2026-10-22（落后 6 个交易日）
     而无人察觉 ⇒ 周六任务必须**真拉**。
  2. **apply_pool 的守门是硬拒绝**。「将被剔除且仍有持仓」直接 return 3 不写盘。
     但周六一定带着周五夜盘留的仓 ⇒ 任务永远跑不成。核心侧
     `ma13_live_multi.py:691-747` 的 `_carried` 换池保护已经覆盖「有仓但不在池内
     → 补订阅接管、只出不进」，所以这里把守门**降级为警告**，照常写盘。
  3. **apply_pool 在 new==old 时提前 return，不写 state**。周六任务必须无论变没变
     都写 `last_reselect`，否则周一 watchdog 会因 gap>=6 再真拉一次。

与 watchdog 的关系（**这是本方案的核心设计**）：

  · 不改星期闸门。2026-11-07 起 `ma13_watchdog_main.RESELECT_WEEKDAYS=(5,6,0)`
    （周六/周日/周一都可换池，用户要求周末提前换好、免掉周一 08:55 手忙脚乱）；
    本脚本仍是**唯一带「日线新鲜度硬校验」和 `--dry-run` 预演**的入口。
  · 周六写完 `last_reselect = 今天` 之后，周一启动时 gap≈2 天 < RESELECT_DAYS(6)
    ⇒ `_maybe_refresh_selection` 自动跳过重选，直接沿用今天写好的池。
  · 周六若失败，state 没写 ⇒ 周一仍按原路径（gap>=6 且周一）重选 = 兜底。
  · 主流程一行不用改。

用法
----
    # 预演（不写盘）
    python 工具/复盘/exec4/select_pool_sat.py --dry-run

    # 真跑（写 workbench.json + selection_state.json）
    python 工具/复盘/exec4/select_pool_sat.py

    # 跳过持仓检查（不连 TqApi，纯离线写盘）
    python 工具/复盘/exec4/select_pool_sat.py --no-position-check

退出码：0 正常 / 2 数据不新鲜 / 3 写盘失败 / 4 选池失败（缺腿等）
"""
from __future__ import annotations

import argparse
import datetime
import io
import json
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import apply_pool as ap  # noqa: E402  （复用 compute_s2_pool / _load_wb / _save_wb）

WORKBENCH_DIR = ap.WORKBENCH_DIR
WORKBENCH_JSON = ap.WORKBENCH_JSON
STATE_JSON = os.path.join(WORKBENCH_DIR, "data", "selection_state.json")
LOG_PATH = os.path.join(WORKBENCH_DIR, "data", "选池_周六.log")


# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    """同时打到控制台和日志文件（计划任务下 stdout 看不到）。"""
    print(msg, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with io.open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}\n")
    except Exception:
        pass


def _last_bar_trading_day(daily: dict) -> tuple[str, datetime.date] | tuple[None, None]:
    """取全池日线的末根时间，换算成它所属的**交易日**。

    口径：tqsdk 日线时间戳 16:00 = 该交易日的起始（夜盘开盘）。
    所以 `2026-10-29 16:00:00` 这根 bar 属于交易日 **2026-10-30**（+1 天）。
    这与 `工具/复盘/data.py` 的「normalize()+1天」显示口径一致。
    """
    from collections import Counter

    cnt = Counter()
    for v in daily.values():
        dts = v.get("datetime") or []
        if dts:
            cnt[dts[-1]] += 1
    if not cnt:
        return None, None
    last_dt, _n = cnt.most_common(1)[0]
    try:
        t = datetime.datetime.strptime(str(last_dt)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return str(last_dt), None
    return str(last_dt), (t + datetime.timedelta(days=1)).date()


def _check_fresh(n: int) -> int:
    """末根交易日必须 >= 上一个交易日。返回 0=OK / 2=不新鲜。"""
    root = ap.ROOT
    for p in (root, os.path.join(root, "lib")):
        if p not in sys.path:
            sys.path.insert(0, p)
    try:
        import trade_calendar as tc
        prev = tc.prev_trading_day(datetime.date.today())
    except Exception as e:
        log(f"[warn] 交易日历不可用（{type(e).__name__}: {e}）→ 跳过新鲜度校验")
        return 0
    if n is None or n < prev:
        log(f"❌ 数据不新鲜：末根交易日 {n} < 上一交易日 {prev} ⇒ 拒绝写盘")
        return 2
    log(f"[fresh] 末根交易日 {n} >= 上一交易日 {prev} ✅")
    return 0


def _read_state() -> dict:
    try:
        with io.open(STATE_JSON, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_state(new: list, ranked: list, source: str) -> None:
    st = _read_state()           # 保留原有其它字段，只更新这几项
    st["last_reselect"] = datetime.date.today().isoformat()
    st["selected"] = list(new)
    st["stage1"] = list(ranked or [])[:20]
    st["source"] = source
    os.makedirs(os.path.dirname(STATE_JSON), exist_ok=True)
    tmp = STATE_JSON + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_JSON)


def _position_warning(old: list, new: list) -> None:
    """守门降级为**警告**：只报告，不阻断。

    依据：核心的 `_carried` 接管已保证「有仓但不在本次池内 → 只出场不开新仓」。
    """
    dropped = [k for k in old if k not in new]
    if not dropped:
        log("[gate] 本次没有剔除任何在池腿 ⇒ 无需检查持仓")
        return
    try:
        pos = ap.read_positions()
    except Exception as e:
        log(f"[gate][warn] 读持仓失败（{type(e).__name__}: {e}）"
            f" → 跳过检查（不影响写盘）")
        return
    hit = {k: v for k, v in pos.items() if k in dropped}
    if not pos:
        log("[gate] 云端空仓 ⇒ 换池不会触碰任何持仓 ✅")
    elif hit:
        log("★ [gate][warn] 以下被剔除的腿**仍有持仓**，周一启动后会被 `_carried` "
            "接管为「只出不进」，不会强平（四期红线不受影响）：")
        for k, v in sorted(hit.items()):
            log(f"      {k} {v:+d} 手")
    else:
        log(f"[gate] 被剔除的腿当前无持仓（账户共 {len(pos)} 个品种有仓）✅")


# ---------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(description="周六独立换池（真拉日线 → S2 → 写配置）")
    p.add_argument("--dry-run", action="store_true", help="只算不写")
    p.add_argument("--no-position-check", action="store_true", help="不连 TqApi 查持仓")
    p.add_argument("--n-legs", type=int, default=None,
                   help="缺省读 workbench.json 的 selection.ret_top_n")
    a = p.parse_args()

    log("=" * 70)
    log("[start] 周六独立换池任务")

    cfg = ap._load_wb()
    sc = cfg.get("selection", {}) or {}
    n_legs = int(a.n_legs if a.n_legs is not None else sc.get("ret_top_n", 5))
    exclude = sc.get("exclude") or []
    seed = sc.get("force_include") or []
    old = [str(k).upper() for k in (cfg.get("selected") or [])]

    log(f"[cfg] n_legs={n_legs}  exclude={len(exclude)} 个  seed={seed}  现池={old}")

    # ---- 1. 真拉 + 选池 ----
    try:
        import data_feed as df
        import universe as u
        daily = df.build_universe_daily(u.all_keys(), use_cache=False, fetch=True)
    except Exception as e:
        log(f"❌ 拉日线失败：{type(e).__name__}: {e}")
        traceback.print_exc()
        return 4
    if not daily:
        log("❌ 日线为空")
        return 4

    last_dt, last_td = _last_bar_trading_day(daily)
    log(f"[data] 品种 {len(daily)} 个，末根 {last_dt} → 交易日 {last_td}")
    rc = _check_fresh(last_td)
    if rc:
        return rc

    # ★ 2026-11-07 修：此前**没传** corr_window / max_tick_frac，compute_s2_pool
    #   回退 pool.CORR_WINDOW=20 ⇒ 本任务（周六 10:00 真跑写盘）算出 20 日窗的池
    #   JD/MA/RU/EG/FU，与守护 _select_by_method（读配置 60 日）和 workbench.json
    #   的 JD/MA/RU/EG/P 不一致。今早 10:00 那次真跑就是这么把 FU 选进来的。
    #   ⇒ 凡写盘入口一律读配置，不再依赖函数默认值。
    _cw = int(sc.get("corr_window_days", 20) or 20)
    _mtf = sc.get("max_tick_frac")
    log(f"[cfg] corr_window={_cw} 日  max_tick_frac={_mtf}")
    try:
        r = ap.compute_s2_pool(n_legs=n_legs, exclude=exclude, seed=seed,
                               use_cache=False, daily=daily,
                               corr_window=_cw,
                               max_tick_frac=float(_mtf) if _mtf is not None else None)
    except Exception as e:
        log(f"❌ 选池失败：{type(e).__name__}: {e}")
        traceback.print_exc()
        return 4

    new = [str(k).upper() for k in (r.get("legs") or [])]
    ranked = list(r.get("ranked") or [])
    if not new:
        log("❌ 选池返回空 legs")
        return 4
    for k in seed:
        if str(k).upper() not in new:
            log(f"❌ 固定腿 {k} 未进池（可能被 exclude / 缺规格 / 粒度闸门挡掉）"
                f" → 拒绝写盘。rejected={r.get('rejected')}")
            return 4

    # ---- 2. 打印差异 ----
    log(f"[pool] 旧池 = {old}")
    log(f"[pool] 新池 = {new}")
    log(f"[pool] 新增 = {sorted(set(new) - set(old)) or '无'}")
    log(f"[pool] 剔除 = {sorted(set(old) - set(new)) or '无'}")
    log(f"[pool] 保留 = {sorted(set(old) & set(new)) or '无'}")
    pa, pb, pr = r.get("max_rho_pair") or (None, None, None)
    if pr is not None:
        import pool as _pool
        log(f"[pool] 入选腿两两最大 |ρ| = {abs(pr):.3f}  {pa}↔{pb}"
            f"（阈值 {_pool.MAX_RHO}，必须 ≤ 阈值）")
    if r.get("dropped_toolarge"):
        log("[pool] 粒度太粗排除：" + "；".join(
            f"{k} 1手={ml:,.0f}({ratio:.2f}×cap)"
            for k, ml, ratio in r["dropped_toolarge"]))

    # ---- 3. 持仓警告（不阻断）----
    if a.no_position_check:
        log("[gate] 已跳过持仓检查（--no-position-check）")
    else:
        _position_warning(old, new)

    # ---- 4. 写盘 ----
    if a.dry_run:
        log("[dry-run] 不写盘。确认无误后去掉 --dry-run 再跑一次。")
        return 0

    if new == old:
        log("[write] 池未变化")
    else:
        cfg["selected"] = new
        cfg.setdefault("selection", {})["method"] = "pool_s2"
        # ★ 不写 ret_top_n / force_include / exclude：它们是**输入参数**，
        #   本任务只输出 selected，避免参数在无人察觉时漂移。
        try:
            ap._save_wb(cfg)
        except Exception as e:
            log(f"❌ 写 workbench.json 失败：{type(e).__name__}: {e}")
            return 3
        log("[write] workbench.json 已更新（selected）")

    # ★ 无论池变没变都写 state：这是让周一 watchdog 跳过的唯一开关
    try:
        _write_state(new, ranked, "select_pool_sat")
    except Exception as e:
        log(f"❌ 写 selection_state.json 失败：{type(e).__name__}: {e}")
        return 3
    log(f"[write] selection_state.json 已更新：last_reselect="
        f"{datetime.date.today().isoformat()} ⇒ 周一 gap<6 将跳过重选")

    log("[done] 周六换池完成。本任务**未启动**任何下单进程。")
    log("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
