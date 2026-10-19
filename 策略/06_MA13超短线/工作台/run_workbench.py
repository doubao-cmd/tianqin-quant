# -*- coding: utf-8 -*-
"""
run_workbench.py —— 单启动器（工作台入口）

职责：
  - 读 workbench.json（或 --config 指定）；
  - 支持命令行快速切品种 / 切策略：
      --strategy liquidity          （卡C 成交量活跃度筛选，默认）
      --strategy cross_sectional    （卡B 截面）
      --strategy ma13_timeseries    （卡A 时序，仅练手）
      --select RB,MA,EG             （手动覆盖品种）
  - --auto-select：跑一次卡C两阶段选择并打印；--write 写回 workbench.json 的 selected；
    --fetch 配合 --auto-select 可真拉日线（需 tqsdk+账号）。
  - --dry-run：只打印「要跑哪些品种 / 各自允许方向 / 闸门状态」，不连网、不交易；
    卡C 模式下会先用本地日线缓存跑一次选择（无缓存则沿用配置兜底）。
  - 正式运行：启动时卡C 自动注入最新选择，再调 ma13_live_multi.run_live_multi。

切换品种 = 改 workbench.json 的 selected，或 --select，全程一个启动器（run_workbench.bat）。
不弹产物卡片，落盘+路径，需看再要。
"""
from __future__ import annotations
import argparse
import datetime
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from config import load_workbench, build_context


def _merge_cli(cfg: dict, args) -> dict:
    if args.strategy:
        cfg["active_strategy"] = args.strategy
    if args.select:
        cfg["selected"] = [s.strip().upper() for s in args.select.split(",") if s.strip()]
    if args.gate_k is not None:
        cfg.setdefault("session_gate", {})["k"] = args.gate_k
        cfg["session_gate"]["enabled"] = True
    if args.no_gate:
        cfg.setdefault("session_gate", {})["enabled"] = False
    return cfg


def _today() -> str:
    return datetime.date.today().isoformat()


# ---------------------------------------------------------------------------
# ★ 卡C 重选节奏的持久化（2026-10-18 修，P0）
#
# 原状：`Selection.needs_reselect()` 写好了，但**生产代码从未调用它**，
#       `_last_reselect` 也只是进程内存变量 ⇒ 每次启动 `_maybe_reselect` 都无条件重选。
#       后果：「六天选一次」实际是「每次重启都重选」，池子在周中被重启打断就整批换腿，
#       而换腿 = 换掉正在持有的腿 ⇒ 触发裸仓风险（见 ma13_live_multi 的 carried 处理）。
# 修法：把 last_reselect / selected / stage1 落盘到 data/selection_state.json，
#       启动时先读回来喂给 needs_reselect()，未到期就沿用上次结果。
# ---------------------------------------------------------------------------
_STATE_PATH = os.path.join(_HERE, "data", "selection_state.json")


def _load_sel_state() -> dict:
    try:
        with open(_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_sel_state(selected: list, stage1: list, today: str) -> None:
    try:
        os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
        with open(_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"last_reselect": today,
                       "selected": [str(k).upper() for k in selected],
                       "stage1": [str(k).upper() for k in stage1]},
                      f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[卡C] ⚠️ 选品种状态写盘失败（下次启动会再重选一次）：{e!r}")


def _cache_staleness_note(daily: dict) -> None:
    """提示日线缓存有多旧（不自动联网，避免与正在跑的模拟盘抢 TqApi 连接）。"""
    try:
        import data_feed as _df
        import pandas as _pd
        labels = [v["datetime"][-1] for v in daily.values() if v.get("datetime")]
        if not labels:
            return
        newest = max(labels)
        bj = _df._to_bj(newest).normalize()
        age = (_pd.Timestamp.now().normalize() - bj).days
        if age > 7:
            print(f"[卡C] ⚠️ 日线缓存最新交易日 {bj.date()} 距今 {age} 天，"
                  f"建议先 `python data_feed.py --fetch` 再重选。")
    except Exception:
        pass


def _maybe_reselect(ctx: dict, args) -> None:
    """卡C：按「重选周期」决定是沿用上次结果、还是重跑两阶段选择并注入 ctx。"""
    strat = ctx.get("strategy")
    if strat is None or getattr(strat, "mode", "") != "liquidity":
        return
    sel = strat.sel
    today = _today()
    st = _load_sel_state()
    last = st.get("last_reselect")
    sel._last_reselect = last          # ★ 关键：把历史喂回去，needs_reselect 才有意义

    force = bool(getattr(args, "force_reselect", False))
    if not force and not sel.needs_reselect(today):
        prev = [str(k).upper() for k in (st.get("selected") or [])]
        if prev:
            sel.selected = prev
            sel._stage1 = [str(k).upper() for k in (st.get("stage1") or [])]
            print(f"[卡C] 距上次重选（{last}）未满 {sel.reselect_min_days} 天 "
                  f"→ 沿用 {len(prev)} 个: {', '.join(prev)}")
            return
        print("[卡C] 状态文件里没有 selected ⇒ 立即重选")

    import data_feed as _df
    import universe as _u
    sc = ctx["cfg"].get("selection", {})
    days = int(sc.get("vol_window_days", 20)) + 10
    # ★ use_cache=not args.fetch：--fetch 就必须真拉（旧缓存存在时旧写法永远不会真拉）
    daily = _df.build_universe_daily(_u.all_keys(), use_cache=not args.fetch,
                                     fetch=args.fetch, days=days)
    if not daily:
        print(f"[卡C] 无日线缓存（需先 `python data_feed.py --fetch`），"
              f"沿用配置 selected={sel.selected}")
        return
    _cache_staleness_note(daily)
    strat.update_selection(daily, today=today)
    _save_sel_state(sel.selected, sel._stage1, today)
    print(f"[卡C] 两阶段已选 {len(sel.selected)} 个: {', '.join(sel.selected)}"
          f"（已记录本次重选日期 {today}，{sel.reselect_min_days} 天后才会再选）")
    _sk = getattr(sel, "_skipped", None) or []
    if _sk:
        print(f"[卡C] 已排除 {len(_sk)} 个缺合约规格的品种: {', '.join(_sk)}")


def _do_auto_select(cfg: dict, args) -> None:
    """独立动作：跑一次卡C选择并打印；--write 写回 workbench.json 的 selected。"""
    import data_feed as _df
    import universe as _u
    from selection import select_two_stage, filter_tradable
    sc = cfg.get("selection", {})
    days = int(sc.get("vol_window_days", 20)) + 10
    # ★ use_cache=not args.fetch：--fetch 就必须真拉（旧缓存存在时旧写法永远不会真拉）
    daily = _df.build_universe_daily(_u.all_keys(), use_cache=not args.fetch,
                                     fetch=args.fetch, days=days)
    if not daily:
        print("[auto-select] 无可用日线缓存；加 --fetch 真拉，"
              "或先 `python data_feed.py --fetch`")
        return
    # ★ 只让「有核验合约规格」的品种参与（缺乘数/成本的品种实盘无法下单）
    daily, skipped = filter_tradable(daily)
    selected, stage1 = select_two_stage(
        daily,
        vol_window=int(sc.get("vol_window_days", 20)),
        vol_top_m=int(sc.get("vol_top_m", 20)),
        ret_window=int(sc.get("ret_window_days", 3)),
        ret_top_n=int(sc.get("ret_top_n", 6)),
        exclude=sc.get("exclude"),   # ★ 人工永久剔除（JM），阶段1 前生效
    )
    print("=" * 60)
    print(f"  卡C 两阶段选择（{len(daily)} 个品种有数据且规格齐备）")
    print(f"  阶段1 流动性池(前{sc.get('vol_top_m', 20)}): {', '.join(stage1)}")
    print(f"  阶段2 选中(前{sc.get('ret_top_n', 6)}): {', '.join(selected)}")
    if skipped:
        print(f"  已排除 {len(skipped)} 个缺合约规格: {', '.join(skipped)}")
    print("=" * 60)
    if args.write:
        cfg["selected"] = selected
        path = args.config or os.path.join(_HERE, "workbench.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        # ★ 手动 --auto-select 也是一次「重选」，必须同步重置周期计时，
        #   否则下次启动会因 needs_reselect 未到期而沿用旧结果、覆盖掉这次写入。
        _save_sel_state(selected, stage1, _today())
        print(f"  [已写回] {path} 的 selected，并记录重选日期 {_today()}")


def _print_plan(ctx: dict) -> None:
    strat = ctx["strategy"]
    gate = ctx["gate"]
    rep = strat.report()
    print("=" * 60)
    print(f"  策略 = {rep['strategy']}   闸门 = {'开' if gate else '关'}")
    print(f"  将运行的品种（{len(rep['active'])}）：{', '.join(rep['active'])}")
    if rep.get("selected"):
        print(f"  卡C 两阶段选中：{', '.join(rep['selected'])}（多空皆可）")
    if rep["chosen"]:
        print("  截面选中（方向约束）：")
        for k, d in rep["chosen"].items():
            print(f"    {k:<5} {'多' if d > 0 else '空'}")
    elif rep["strategy"] == "cross_sectional":
        print("  方向约束：运行时用日线排名算（dry-run 无数据，暂显多空皆可）")
    else:
        print("  方向约束：多空皆可（时序/卡C 模式）")
    if gate:
        print(f"  闸门参数：k={gate.k}  N={gate.n}（|session收益|>k×sessionATR 则封下一session）")
    print("=" * 60)


def main():
    ap = argparse.ArgumentParser(description="MA13 工作台单启动器")
    ap.add_argument("--config", default=None, help="workbench.json 路径")
    ap.add_argument("--strategy", default=None,
                    choices=["ma13_timeseries", "cross_sectional", "liquidity"])
    ap.add_argument("--select", default=None, help="逗号分隔品种短码，如 RB,MA,EG（多选/单选）")
    ap.add_argument("--gate-k", type=float, default=None, help="覆盖强趋势阈值 k")
    ap.add_argument("--no-gate", action="store_true", help="关闭时段闸门")
    ap.add_argument("--auto-select", action="store_true",
                    help="跑一次卡C两阶段选择并打印（--write 写回json）")
    ap.add_argument("--fetch", action="store_true",
                    help="--auto-select/运行时真拉日线（需tqsdk+账号+连网）")
    ap.add_argument("--write", action="store_true", help="--auto-select 时写回 workbench.json")
    ap.add_argument("--force-reselect", action="store_true",
                    help="忽略重选周期，强制重跑两阶段选择（排查/应急用）")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不连网不交易")
    ap.add_argument("--allow-trade", action="store_true", help="真实下单（默认仅信号/观察）")
    args = ap.parse_args()

    cfg = load_workbench(args.config)
    cfg = _merge_cli(cfg, args)

    if args.auto_select:
        _do_auto_select(cfg, args)
        return

    ctx = build_context(cfg)
    _maybe_reselect(ctx, args)

    if args.dry_run:
        _print_plan(ctx)
        return

    # 正式运行：延迟导入 ma13_live_multi（需要 tqsdk + 账号，连网）
    sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "模拟盘"))
    try:
        import ma13_live_multi
    except Exception as e:
        print("无法导入 ma13_live_multi（需要 tqsdk/账号/连网）：", repr(e))
        print("可加 --dry-run 仅查看计划，或 --auto-select 跑选择。")
        return

    keys = ctx["strategy"].active_keys()
    if not keys:
        # ★ 卡C 没选出品种时必须拒绝启动（绝不回退全池；空列表=不下单）
        print("[工作台] ✗ 调度列表为空（卡C 没选出品种 / selected 为空）→ 拒绝启动。")
        print("         先跑：python run_workbench.py --auto-select --write")
        return
    print(f"[工作台] 启动策略={cfg['active_strategy']}，品种={keys}，"
          f"交易={'是' if args.allow_trade else '否(观察)'}")
    started = ma13_live_multi.run_live_multi(
        symbol_keys=keys,
        allow_trade=args.allow_trade,
        ctx=ctx,
    )
    if started is False:
        # 核心单实例锁没抢到（已有另一个核心在跑）⇒ 明确失败，不假装启动成功
        print("[工作台] ✗ 未启动：另一个核心实例正在运行（核心.lock 被占用）。"
              "请先关闭它，或改用 watchdog/计划任务统一入口。")


if __name__ == "__main__":
    main()
