# -*- coding: utf-8 -*-
"""
exec4/apply_pool.py —— 把 S2 选出的池子写回 workbench.json（带持仓守门）

为什么要一个专门的脚本，而不是手改 JSON：

  四期红线写得很清楚 —— **有持仓的品种必须留到自然出场，不许为换池平仓**
  （那等于第 6 次「提前出场」）。而换池一旦写进 workbench.json，下次重启核心
  就会按新池订阅；旧池里还有持仓的品种会被 `_adopt_carried_legs` 接管成
  「只出不进」——虽然不会强行平仓，但**新池的腿会跟你已有的旧仓方向叠加**，
  净敞口可能瞬间顶到 RiskGate 的 2× 上限。

  ⇒ 所以把「现在到底能不能切」做成一道**可执行的守门**，而不是靠人记得：
      · `--dry-run`（默认）：只算 S2 池并打印差异，不写盘，不联网
      · `--check`：联网读真实持仓，报告"能不能切"（不写盘）
      · `--write`：只有持仓为空才写盘；非空直接拒绝并列出还在持仓的品种

用法
----
    # 1) 看 S2 选什么（离线，用工作台日线缓存）
    python exec4/apply_pool.py

    # 2) 看现在能不能切（会连一次 TqApi，只读持仓）
    python exec4/apply_pool.py --check

    # 3) 真写回（持仓必须为空，否则拒绝）
    python exec4/apply_pool.py --write

⚠️ 本脚本**不会**重启模拟盘。写完需要用户自己重启（或等 watchdog 下次拉起）。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPLAY = os.path.dirname(_HERE)          # 工具/复盘


def _root() -> str:
    p = _HERE
    for _ in range(8):
        if os.path.isfile(os.path.join(p, "config_symbols.py")):
            return p
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    raise RuntimeError("找不到工程根（config_symbols.py）")


ROOT = _root()
WORKBENCH_DIR = os.path.join(ROOT, "策略", "06_MA13超短线", "工作台")
WORKBENCH_JSON = os.path.join(WORKBENCH_DIR, "workbench.json")
SIM_DIR = os.path.join(ROOT, "策略", "06_MA13超短线", "模拟盘")

for _p in (_REPLAY, _HERE, WORKBENCH_DIR, ROOT, SIM_DIR):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)


# ---------------------------------------------------------------------------
def compute_s2_pool(n_legs: int = 5, exclude=None, seed=None,
                    use_cache: bool = True, daily=None,
                    corr_window: int = None, max_tick_frac: float = None) -> dict:
    """用工作台本地日线缓存算 S2 池（离线）。

    exclude：人工剔除名单（品种 KEY）。剔除后从剩余池里按原规则补足腿数。
             ★ 2026-10-19 新增：用户主观剔除焦煤 JM（不涉及窗口/参数，属标的选择，
               与铁律 2「不遍历参数择优」无关）。
    seed：固定腿（workbench.json 的 selection.force_include）。★ 2026-10-31 新增：
          此前**没有**这个参数，导致本脚本选出的池会把固定腿 JD 静默丢掉
          （同一份日线实测：无 seed → ['RU','MA','EG','P','TA']；带 seed →
          ['JD','RU','MA','EG','P']）。凡绕过 `pool.select_pool(..., seed=)` 的
          入口都会重现这个问题，与 JM「绕过统一入口」同类。
    use_cache：True=读工作台日线缓存（快，但**不校验新鲜度**）；
               False=真拉全池日线（慢约 1-2 分钟，周六任务必须用这个）。
               ★ 2026-10-31 实测：缓存末根曾停在 6 个交易日前而无人察觉。
    daily：已经拉好的 {key: {...}}，传了就不再拉（周六任务先拉一次做新鲜度
           校验，避免重复拉两遍 57 个品种）。
    """
    import data_feed as df
    import universe as u
    import pool
    from signal_bridge import spec, has_spec

    if daily is None:
        daily = df.build_universe_daily(u.all_keys(), use_cache=use_cache,
                                        fetch=not use_cache)
    if not daily:
        raise RuntimeError(
            "无日线缓存。请先跑：python 工作台/data_feed.py --fetch")
    ex = {str(k).strip().upper() for k in (exclude or []) if str(k).strip()}
    if ex:
        daily = {k: v for k, v in daily.items() if str(k).upper() not in ex}
        print(f"[exclude] 人工剔除 {len(ex)} 个：{','.join(sorted(ex))}（不参与排序）")
    specs = {k: spec(k) for k in daily if has_spec(k)}
    _seed = tuple(str(k).strip().upper() for k in (seed or ()) if str(k).strip())
    if _seed:
        print(f"[seed] 固定腿：{','.join(_seed)}（先进座，占掉对应名额；不参与 ρ 检查）")
    # ★ 2026-11-07 结构闸门：读工作台 tick_atr.json（与 watchdog 同源同口径）。
    _tf = None
    _tf_path = os.path.join(os.path.dirname(WORKBENCH_JSON), "data", "tick_atr.json")
    try:
        with io.open(_tf_path, "r", encoding="utf-8") as _f:
            _tfj = json.load(_f)
        _tf = {k: v["tick_frac"] for k, v in (_tfj.get("data") or {}).items()
               if v.get("tick_frac") is not None}
    except Exception:
        _tf = None
        print("[tick_atr] ⚠️ 结构表缺失/损坏 → 结构闸门本次关闭")
    return pool.select_pool(daily, specs, n_legs=n_legs, seed=_seed,
                            corr_window=corr_window or pool.CORR_WINDOW,
                            tick_frac=_tf,
                            max_tick_frac=(pool.MAX_TICK_FRAC if max_tick_frac is None
                                           else float(max_tick_frac)))


def _load_wb() -> dict:
    with open(WORKBENCH_JSON, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_wb(cfg: dict) -> None:
    """先写临时文件再替换，避免写一半断电把配置写坏。"""
    tmp = WORKBENCH_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, WORKBENCH_JSON)


def read_positions() -> dict:
    """只读云端持仓 → {品种KEY: 净仓}。需要 tqsdk + 账号 + 连网。"""
    from tqsdk import TqApi, TqAuth, TqKq
    import config_account
    import config_symbols
    import time

    user, pwd = config_account.check_live()
    api = TqApi(TqKq(), auth=TqAuth(user, pwd))
    try:
        api.wait_update(deadline=time.time() + 15)
        out = {}
        for sym, p in api.get_position().items():
            try:
                net = int(getattr(p, "pos", 0) or 0)
            except Exception:
                continue
            if not net:
                continue
            code = str(sym).rstrip("0123456789").split(".")[-1].upper()
            if code not in config_symbols.SYMBOLS:
                continue
            out[code] = out.get(code, 0) + net
        return out
    finally:
        try:
            api.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="把 S2 池写回 workbench.json（带持仓守门）")
    ap.add_argument("--write", action="store_true", help="真写回（要求持仓为空）")
    ap.add_argument("--check", action="store_true", help="联网检查现在能不能切（不写盘）")
    ap.add_argument("--n-legs", type=int, default=5, help="腿数（拍板③=5）")
    ap.add_argument("--force", action="store_true",
                    help="★ 跳过持仓守门（危险：会在旧仓未平时换池）")
    ap.add_argument("--exclude", nargs="*", default=None,
                    help="人工剔除的品种 KEY（如 JM）；不传则用 workbench.json "
                         "selection.exclude")
    ap.add_argument("--seed", nargs="*", default=None,
                    help="★ 固定腿（如 JD）；不传则用 workbench.json "
                         "selection.force_include。不传且配置文件也为空时才真的没有固定腿")
    ap.add_argument("--fresh", action="store_true",
                    help="★ 真拉全池日线（不走缓存）。缓存不校验新鲜度，"
                         "手动换池建议带上；周六任务默认就是真拉")
    a = ap.parse_args()

    cfg = _load_wb()
    old = [str(k).upper() for k in (cfg.get("selected") or [])]
    # 剔除名单：CLI 优先，其次配置文件持久化值（每周自动重选也读它）
    exclude = a.exclude if a.exclude is not None else (cfg.get("selection", {}) or {}).get("exclude")
    # 固定腿：CLI 优先，其次配置文件（watchdog 走的就是这个字段）
    seed = a.seed if a.seed is not None else (cfg.get("selection", {}) or {}).get("force_include")

    # ★ 2026-11-07 修：本入口此前**没读** selection.corr_window_days / max_tick_frac，
    #   compute_s2_pool 回退到 pool.CORR_WINDOW=20 ⇒ 与守护 _select_by_method（读配置 60）
    #   口径不一致，dry-run 会算出 JD/MA/RU/EG/FU 而生产是 JD/MA/RU/EG/P。
    #   凡是「--write 写盘」的入口，必须与守护同参数，否则一次手工换池就把规则悄悄改回去。
    _sc = cfg.get("selection", {}) or {}
    _mtf = _sc.get("max_tick_frac")
    r = compute_s2_pool(n_legs=a.n_legs, exclude=exclude, seed=seed,
                        use_cache=not a.fresh,
                        corr_window=int(_sc.get("corr_window_days", 20) or 20),
                        max_tick_frac=float(_mtf) if _mtf is not None else None)
    new = list(r["legs"])

    print("=" * 74)
    print("S2 池 vs 当前 workbench.json")
    print("=" * 74)
    print(f"当前（线上）: {', '.join(old) or '（空）'}")
    print(f"S2 选出     : {', '.join(new)}")
    pa, pb, pr = r["max_rho_pair"]
    if pr is not None:
        print(f"最大 |ρ|    : {abs(pr):.3f}  ← {pa}↔{pb}（阈值 0.5）")
    if r["dropped_toolarge"]:
        print("粒度太粗排除: " + "；".join(
            f"{k} 1手={ml:,.0f}({ratio:.2f}×cap)"
            for k, ml, ratio in r["dropped_toolarge"]))
    print(f"\n新增: {sorted(set(new) - set(old)) or '无'}")
    print(f"剔除: {sorted(set(old) - set(new)) or '无'}")
    print(f"保留: {sorted(set(old) & set(new)) or '无'}")

    if new == old:
        print("\n⇒ 与当前一致，无需改动。")
        return 0

    # ---- 持仓守门 ----
    if a.write or a.check:
        try:
            pos = read_positions()
        except Exception as e:
            print(f"\n⚠️ 读持仓失败（{type(e).__name__}: {e}）")
            print("   ⇒ 无法确认是否能安全换池，**拒绝写盘**。")
            print("   修好连接后重试，或确认空仓后用 --force（不推荐）。")
            return 2
        dropped_held = {k: v for k, v in pos.items() if k in (set(old) - set(new))}
        print(f"\n当前持仓 {len(pos)} 个："
              + "；".join(f"{k} {v:+d}" for k, v in sorted(pos.items())))
        if dropped_held:
            print(f"★ 将被剔除且**仍有持仓**："
                  + "；".join(f"{k} {v:+d}" for k, v in sorted(dropped_held.items())))
            print("   四期红线：有持仓必须留到自然出场，不许为换池平仓。")
            print("   ⇒ 现在不能切，等这些腿自然出场后再跑本脚本。")
            if a.write and not a.force:
                print("\n❌ 拒绝写盘（如确认要强行换池，加 --force，但那是违反红线的）")
                return 3
        elif pos:
            print("   将要剔除的品种都没有持仓 ⇒ 换池不会强行平掉任何仓位 ✅")
        else:
            print("   账户空仓 ⇒ 任何时刻切换都安全 ✅")

    if not a.write:
        print("\n（未写盘。确认可切后加 --write）")
        return 0

    cfg["selected"] = new
    # ★ 必须同时把 method 切成 pool_s2，否则**下个周一重选又会用手数口径覆盖回来**
    #   （watchdog 的 _maybe_refresh_selection 按 selection.method 分派）。
    #   这是"切换会自己失效"的唯一根因。
    cfg.setdefault("selection", {})["method"] = "pool_s2"
    cfg["selection"]["ret_top_n"] = int(a.n_legs)
    # ★ 剔除名单必须落盘：watchdog 每周自动重选会读它，否则 JM 下周一自己回来
    cfg["selection"]["exclude"] = sorted({str(x).strip().upper()
                                          for x in (exclude or []) if str(x).strip()})
    _save_wb(cfg)

    # 同步 selection_state.json：换池也是一次「重选」，不重置计时会导致
    # 下次启动因为"未满 6 天"而沿用旧的 selected，把这次写入覆盖掉。
    import datetime
    state_path = os.path.join(WORKBENCH_DIR, "data", "selection_state.json")
    try:
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump({"last_reselect": datetime.date.today().isoformat(),
                       "selected": new,
                       "stage1": list(r.get("ranked") or [])[:20],
                       "source": "apply_pool_s2"},
                      f, ensure_ascii=False, indent=2)
        print(f"\n✅ 已写回 {os.path.basename(WORKBENCH_JSON)}：")
        print(f"   selected = {new}")
        print(f"   selection.method = pool_s2（下个周一重选也走 S2，不会被手数口径覆盖）")
        print(f"   并同步 selection_state.json（重选日期 = 今天，6 天后才再选）")
    except Exception as e:
        print(f"\n⚠️ workbench.json 已写入，但 selection_state.json 同步失败：{e!r}")
        print("   下次启动可能因未满 6 天而沿用旧池 —— 请手动确认。")
        return 4

    print("\n⚠️ 本脚本没有重启模拟盘。要让新池生效：")
    print("   1) 关闭当前模拟盘（守护窗口 Ctrl+C 或结束进程）")
    print("   2) 重新跑 每日0855自动启动.bat（会用 workbench.json 的 auto 池）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
