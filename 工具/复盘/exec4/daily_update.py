# -*- coding: utf-8 -*-
"""
exec4/daily_update.py —— 四期 S4：每日数据流水线（带显式失败）

## 用法

```bash
# 常规（建议由计划任务在 11:35 / 15:05 / 23:05 各跑一次）
python 工具/复盘/exec4/daily_update.py

# 只更新指定周期 / 品种（排查用）
python 工具/复盘/exec4/daily_update.py --periods 5,15 --keys M,P,SR,JM,JD,EB

# 只刷新池层筛选表（不联网，用已有 archive / 工作台日线缓存）
python 工具/复盘/exec4/daily_update.py --pool-only
```

⚠️ **必须用装了 tqsdk 的解释器**（`.venv_viz` 没有 tqsdk）：
   `%USERPROFILE%\\.workbuddy\\binaries\\python\\envs\\tqsdk\\Scripts\\python.exe`
   或用 `工具\\复盘\\刷新数据.bat`（已自动选解释器）。

## 它做三件事

1. **拉数据**：用**回测账号**（`config_account.check()`，方案 §2.6 的"账号 2"）
   拉 6 品种 × {1,5,15,1440}min，`TqSim` 本地账本（**绝不用 TqKq**，会踢掉模拟盘）。
   落 `_缓存/kline2026/`（`data._save_recent` 的老路径，保留兼容）。

2. **并入 archive**（`exec4/store.upsert_archive`）：只追加、不覆盖。
   这一步修掉了 `_save_recent` 的老 bug —— 整文件覆盖 + 只回 8800 根 ⇒ 每天少一天历史。

3. **写池层筛选表** + **新鲜度** + **失败标记**：
   `_缓存/pool/pool_screen_{date}.csv`（50 品种指标，不进 HTML）
   `_缓存/pool/_last_update.json`（工具角标读它）
   `_缓存/pool/_FAILED_{date}.txt`（存在 = 那天没更新成功，必须让人看见）

## 为什么"显式失败"是硬要求（方案 §2.6 原话）

> 账号被占用（比如你恰好在跑回测）时 tqsdk 会登录失败。如果脚本静默退出，
> **当天数据就没更新，而你不会知道** —— 直到某天发现复盘工具显示的是三天前的数据。

所以：重试 3 次 × 间隔 60s；3 次都失败 ⇒ 写 `_FAILED_{date}.txt` +
终端红字 + **退出码非 0**（计划任务据此标红）。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys
import time
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPLAY = os.path.dirname(_HERE)


def _bootstrap_path():
    root = _REPLAY
    for _ in range(8):
        if os.path.isfile(os.path.join(root, "config_symbols.py")):
            break
        parent = os.path.dirname(root)
        if parent == root:
            break
        root = parent
    for p in (root, _REPLAY, _HERE,
              os.path.join(root, "策略", "06_MA13超短线", "工作台")):
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)
    return root


ROOT = _bootstrap_path()

import store as S

# 终端红字（Windows 10+ 支持 ANSI；不支持时退化为纯文本，不报错）
_RED = "\033[91m"
_YEL = "\033[93m"
_GRN = "\033[92m"
_RST = "\033[0m"


def _c(txt: str, color: str) -> str:
    try:
        if os.name == "nt" and not os.environ.get("WT_SESSION"):
            os.system("")            # 打开 Windows 控制台 ANSI 支持
    except Exception:
        pass
    return f"{color}{txt}{_RST}"


def _log(msg: str) -> None:
    print(f"[{_dt.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# 1) 拉数据（带回重试）
# ---------------------------------------------------------------------------
def fetch_with_retry(periods, retries: int = 3, wait_sec: int = 60):
    """拉数据，登录/网络失败时重试。

    返回 (ok: bool, payload: dict)

    ★ `data.fetch_recent_all` 内部已经用 `TqSim` + 主账号（= 回测账号），
      并且一次连接批量订阅所有品种 ⇒ 这里只需包一层重试。
    """
    import data

    last_err = ""
    for attempt in range(1, int(retries) + 1):
        try:
            _log(f"拉数据（第 {attempt}/{retries} 次）：周期 {periods}")
            rows = data.fetch_recent_all(periods=tuple(periods), data_length=8800)
            return True, {"rows": rows, "attempts": attempt}
        except SystemExit:
            # config_account.check() 没填账号 ⇒ 重试无意义
            raise
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            _log(_c(f"  失败：{last_err}", _YEL))
            if attempt < retries:
                _log(f"  {wait_sec}s 后重试…（若回测正占着账号，请等它结束）")
                time.sleep(wait_sec)
    return False, {"error": last_err, "attempts": retries}


# ---------------------------------------------------------------------------
# 2) 并入 archive
# ---------------------------------------------------------------------------
def archive_all(keys, periods) -> dict:
    """把 kline2026 的本次结果并入 archive。返回 {key: {period: result}}。"""
    import data

    out = {}
    for key in keys:
        per_map = {}
        for period in periods:
            try:
                df, _meta = data.load_recent(key, period)
            except FileNotFoundError:
                per_map[period] = {"error": "本次未拉到（kline2026 无此文件）"}
                continue
            try:
                r = S.upsert_archive(key, period, df)
                per_map[period] = r
            except AssertionError as e:
                # 单调性被破坏 ⇒ 必须显式记录，绝不吞掉
                per_map[period] = {"error": f"archive 断言失败：{e}"}
            except Exception as e:
                per_map[period] = {"error": f"{type(e).__name__}: {e}"}
        out[key] = per_map
    return out


def seed_archive_from_cache(keys, periods) -> dict:
    """★ 把现有 `kline2026/` 缓存**离线播种**进 archive（不联网）。

    为什么必须先做这一步：
      `archive` 初始是空的，而 `fetch_recent_all` 会**覆盖** `kline2026/*.csv`
      （`_save_recent` 的老行为）。如果第一次日更中途失败（网络/账号），
      `kline2026` 可能被截断，而 archive 里什么都没有 ⇒ **没有备份，数据真丢了**。
      ⇒ 上线 S4 的第一步必须是"先把现有缓存复制进 archive"，之后才允许联网更新。
    """
    import data

    out = {}
    for key in keys:
        per_map = {}
        for period in periods:
            src = S.recent_path(key, period)
            if not src.exists():
                per_map[period] = {"skipped": "kline2026 无此文件"}
                continue
            try:
                df, _m = data.load_recent(key, period)
                r = S.upsert_archive(key, period, df)
                per_map[period] = r
            except Exception as e:
                per_map[period] = {"error": f"{type(e).__name__}: {e}"}
        out[key] = per_map
    return out


# ---------------------------------------------------------------------------
# 3) 池层筛选表
# ---------------------------------------------------------------------------
def build_pool_screen() -> list:
    """算 50 品种的筛选指标（成交额 / 振幅 / 成本÷ATR / 粒度 / 规格齐备）。

    数据源优先级：archive 1440（自维护）→ 工作台 daily_*.csv（data_feed 维护）。
    **都不联网** —— 池层只需要"最近 20 日"的粗略量级。
    """
    import config_symbols as cs
    import universe as u
    from signal_bridge import has_spec

    # 1) 优先用工作台日线缓存（字段最全：datetime/close/volume）
    daily = {}
    try:
        import data_feed as df
        daily = df.build_universe_daily(u.all_keys(), use_cache=True) or {}
    except Exception as e:
        _log(_c(f"  池层：读工作台日线失败（{type(e).__name__}: {e}），改用 archive", _YEL))

    # 2) archive 1440 作为补充/替代
    arch_daily = {}
    try:
        for st in S.archive_stats(periods=(1440,)):
            d = S.read_archive(st["key"], 1440)
            if d is not None and len(d) >= 5:
                arch_daily[st["key"]] = {
                    "close": [float(x) for x in d["close"].tolist()],
                    "volume": [float(x) for x in d["volume"].tolist()]
                    if "volume" in d.columns else [0.0] * len(d),
                }
    except Exception:
        pass

    NOTIONAL_CAP = 400_000.0        # = equity_base 200k × notional_cap 2.0（与 pool.py 一致）
    rows = []
    for key in u.all_keys():
        meta = u.get(key) or {}
        src = daily.get(key) or arch_daily.get(key)
        spec_ok = bool(has_spec(key))
        row = {"key": key, "name": meta.get("name", ""),
               "exch": meta.get("exch", ""), "night": bool(meta.get("night")),
               "tradable": spec_ok, "note": ""}
        if not spec_ok:
            row["note"] = "无核验合约规格"
        if not src or len(src.get("close") or []) < 5:
            row["note"] = (row["note"] + "；无日线数据").strip("；")
            rows.append(row)
            continue
        close = [float(x) for x in src["close"]]
        vol = [float(x) for x in (src.get("volume") or [])]
        n = min(len(close), len(vol)) if vol else 0
        if n == 0:
            row["note"] = (row["note"] + "；无成交量").strip("；")
            rows.append(row)
            continue
        w = min(20, n)
        px = close[-1]
        row["last_close"] = round(px, 4)
        mult = float(cs.SYMBOLS[key][2]) if spec_ok else 0.0
        tick = float(cs.SYMBOLS[key][3]) if spec_ok else 0.0

        if mult:
            turn = sum(v * c for v, c in zip(vol[-w:], close[-w:])) / w * mult
            row["turnover_20d"] = round(turn, 2)
            row["min_lot_notional"] = round(px * mult, 2)
        # 振幅：近 20 日 |日收益| 均值（没有 high/low 时用收盘近似）
        rets = [abs(close[i] / close[i - 1] - 1.0) for i in range(len(close) - w + 1, len(close))
                if close[i - 1] > 0]
        if rets:
            row["amp_20d"] = round(sum(rets) / len(rets), 6)
        # 成本 ÷ ATR（ATR 用振幅代理：amp × price）
        if spec_ok and rets:
            atr_proxy = (sum(rets) / len(rets)) * px
            comm = float(cs.COST.get(key, 2.0))
            one_side = comm + 1.0 * tick * mult       # 手续费 + 1 跳滑点
            if atr_proxy > 0:
                row["cost_over_atr"] = round(one_side / atr_proxy, 6)
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="四期 S4 每日数据流水线")
    ap.add_argument("--periods", default="1,5,15,1440",
                    help="要更新的周期（逗号分隔），默认全部")
    ap.add_argument("--keys", default=None,
                    help="品种 KEY（逗号分隔），缺省=复盘工具那 6 个")
    ap.add_argument("--retries", type=int, default=3, help="登录失败重试次数")
    ap.add_argument("--wait", type=int, default=60, help="重试间隔秒数")
    ap.add_argument("--pool-only", action="store_true",
                    help="只刷新池层筛选表，不联网")
    ap.add_argument("--no-archive", action="store_true",
                    help="只拉数据不写 archive（不建议）")
    ap.add_argument("--seed-archive", action="store_true",
                    help="★ 只把现有 kline2026 缓存播种进 archive（离线，不联网）。"
                         "首次启用 S4 必须先跑这个，否则 archive 空、更新失败就没备份")
    a = ap.parse_args()

    started = _dt.datetime.now()
    today = started.date().isoformat()
    errors: list = []
    payload = {"ok": False, "started_at": started.isoformat(timespec="seconds"),
               "account": "backtest", "n_symbols": 0, "symbols": {}, "errors": [],
               "data_updated": False}      # ★ 只有真拉了行情才置 True（见 store.freshness_hours）

    # ---- 品种范围 ----
    import data
    if a.keys:
        keys = [x.strip().upper() for x in a.keys.split(",") if x.strip()]
    else:
        keys = list(data.CN2KEY.values())
    periods = [int(x) for x in str(a.periods).split(",") if str(x).strip()]

    # ---- 播种模式（离线，最先跑一次）----
    if a.seed_archive:
        print("=" * 78)
        print("S4 archive 播种（离线：kline2026 → kline_archive，不联网）")
        print("=" * 78)
        res = seed_archive_from_cache(keys, periods)
        n_new = n_tot = 0
        for key, per_map in res.items():
            parts = []
            for period, r in per_map.items():
                if "error" in r:
                    parts.append(f"{period}min 失败({r['error']})")
                elif "skipped" in r:
                    parts.append(f"{period}min 跳过")
                else:
                    parts.append(f"{period}min {r['n_before']}→{r['n_total']}"
                                 f"(+{r['n_new_rows']})")
                    n_new += r["n_new_rows"]
                    n_tot += r["n_total"]
            print(f"  {key:<4} " + " | ".join(parts))
        print("-" * 78)
        print(f"  合计：新增 {n_new} 行 / 总 {n_tot} 行 → {S.ARCHIVE_DIR}")
        print("  ★ 现在 archive 有备份了，可以安全联网更新（失败也不会丢历史）")
        return 0

    print("=" * 78)
    print("四期 S4 每日数据流水线")
    print(f"  品种 {len(keys)}：{', '.join(keys)}")
    print(f"  周期 {periods}")
    print(f"  archive 目录：{S.ARCHIVE_DIR}")
    print("=" * 78)

    try:
        # ---- ① 拉数据（可跳过）----
        if a.pool_only:
            _log("--pool-only：跳过联网拉数据")
            payload["note"] = "pool-only，未更新行情"
        else:
            ok, info = fetch_with_retry(periods, retries=a.retries, wait_sec=a.wait)
            # ★ 无论成败都记 attempts，否则失败标记里会写"重试次数：0"，误导排查
            payload["attempts"] = info.get("attempts")
            if not ok:
                errors.append(f"拉数据失败（{info.get('attempts')} 次）：{info.get('error')}")
                raise RuntimeError(errors[-1])
            payload["data_updated"] = True        # ★ 只有走到这里才算"行情已更新"
            _log(_c(f"  拉数据成功（第 {info.get('attempts')} 次）", _GRN))

        # ---- ② 并入 archive ----
        if not a.pool_only and not a.no_archive:
            _log("并入 archive（只追加不覆盖）…")
            arch = archive_all(keys, periods)
            for key, per_map in arch.items():
                entry = payload["symbols"].setdefault(key, {})
                for period, r in per_map.items():
                    if "error" in r:
                        errors.append(f"{key}/{period} archive：{r['error']}")
                        entry[f"archive_{period}"] = {"error": r["error"]}
                        continue
                    entry[f"archive_{period}"] = {
                        "n_before": r["n_before"], "n_new": r["n_new_rows"],
                        "n_total": r["n_total"],
                        "dedup_fixed": r.get("n_dedup_fixed", 0),
                    }
                    if r["n_new_rows"] < 0:
                        errors.append(f"{key}/{period} archive 行数减少！")
                    if r.get("n_dedup_fixed"):
                        _log(_c(f"    {key}/{period} 修掉 {r['n_dedup_fixed']} 行历史重复", _YEL))
            # 汇总一行
            n_new = sum(v.get("n_new", 0) for e in payload["symbols"].values()
                        for k2, v in e.items() if isinstance(v, dict) and "n_new" in v)
            n_tot = sum(v.get("n_total", 0) for e in payload["symbols"].values()
                        for k2, v in e.items() if isinstance(v, dict) and "n_total" in v)
            _log(f"  archive 合计：新增 {n_new} 行 / 总 {n_tot} 行")
            payload["archive_new_rows"] = n_new
            payload["archive_total_rows"] = n_tot

        # ---- ③ 池层筛选表 ----
        _log("生成池层筛选表…")
        rows = build_pool_screen()
        p = S.write_pool_screen(rows, today)
        _log(f"  {p.name}：{len(rows)} 品种，池层总体积 {S.pool_layer_size_kb()} KB")
        payload["pool_screen"] = {"n": len(rows), "path": str(p)}
        payload["n_symbols"] = len(keys)

        payload["ok"] = (len(errors) == 0)
        payload["errors"] = errors
        payload["finished_at"] = _dt.datetime.now().isoformat(timespec="seconds")
        S.write_last_update(payload)

        if errors:
            # 部分失败：仍写 last_update（ok=False），并落失败标记
            S.write_failure(today, "\n".join(errors), attempts=payload.get("attempts", 0))
            print()
            print(_c("⚠️ 部分品种/周期失败：", _YEL))
            for e in errors:
                print(_c("   - " + e, _YEL))
            print(_c(f"   已写失败标记：{S.failure_path(today)}", _YEL))
            return 2

        # 成功 ⇒ 顺手清掉当天的旧失败标记（否则角标一直红）
        if S.clear_failure(today):
            _log("已清除当天的失败标记（本次成功）")
        badge = S.freshness_badge()
        _log(_c(f"完成 ✅ {badge['text']}", _GRN))
        return 0

    except Exception:
        detail = traceback.format_exc()
        print()
        print(_c("=" * 78, _RED))
        print(_c("数据更新失败 —— 当天数据没有更新成功", _RED))
        print(_c(f"  {detail.strip().splitlines()[-1]}", _RED))
        print(_c("=" * 78, _RED))
        fp = S.write_failure(today, detail, attempts=payload.get("attempts", 0))
        payload["ok"] = False
        payload["errors"] = errors + [detail.strip().splitlines()[-1]]
        payload["finished_at"] = _dt.datetime.now().isoformat(timespec="seconds")
        try:
            S.write_last_update(payload)
        except Exception:
            pass
        print(f"  失败标记已写入：{fp}")
        print("  处置：确认回测账号未被占用 → 重跑本脚本 → 成功后脚本会自动清除标记")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
