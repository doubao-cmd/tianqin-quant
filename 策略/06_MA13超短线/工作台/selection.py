# -*- coding: utf-8 -*-
"""
selection.py —— 选股/选品种逻辑（纯函数，无网络，可离线单测）

两种模式（对应预注册卡 A / 卡 B）：

  卡 A 时序（timeseries）：
      - 全池（或 workbench.json 里 selected 指定的）各自跑 MA13，方向由信号决定。
      - force_dir(key) 恒返回 0 ⇒ 多空都允许。
      - "选"只体现在「进池的 50 个里挑哪几个跑」，一次性 master list。

  卡 B 截面（cross_sectional）：
      - 每日早盘对全 50 品种算截面分数 score = 近 N 日收益 / 近 N 日波动（风险调整动量）。
      - 取分数最高的 Q 个做多、最低的 Q 个做空（Q 受 MAX_LEGS 约束，最多 3）。
      - force_dir(key) 对头部返回 +1、尾部返回 -1、其余返回 0。
      - 选中后持有到次日重排；中间按 5min MA13 进出，但**只在 force_dir 方向上开**。

无未来函数：排名用「已收盘日线」算，下一交易日的开仓才用结论。

卡 C 成交量活跃度筛选（用户 2026-10-17 新思路，工程脚手架）：
  - 两阶段选择 select_two_stage()：
      阶段1（流动性闸门）：全池按「近 20 交易日平均成交量」降序取前 M=20；
      阶段2（活跃度）    ：在阶段1内按「近 K=3 日累计 |涨跌幅|」降序取前 N=6。
  - 逻辑只解决「在哪交易」（流动性 + 近期有行情），不解决「何时有边缘」；
    底层信号仍是 MA13 占位（负期望），找到正期望因子后换入 Signal 接口即可。
  - 否决隐含波动率(IV)作 v1 主筛（覆盖不全 + 语义错位 + 工程不划算）。
"""
from __future__ import annotations
import datetime
import math
import statistics as _st
import universe as _u
from sectors import cap_fill


import os as _os
import sys as _sys

# ★ 定位项目根并导入 config_symbols（找不到直接 ImportError，不静默兜底）：
#   本模块就在项目内，规格表是硬依赖；曾用「import 失败就放行」，结果守卫静默失效。
_ROOT = _os.path.dirname(_os.path.abspath(__file__))
while _ROOT != _os.path.dirname(_ROOT) and not _os.path.isfile(
        _os.path.join(_ROOT, "config_symbols.py")):
    _ROOT = _os.path.dirname(_ROOT)
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
import config_symbols as _cs


def tradable(key: str) -> bool:
    """该品种是否具备「可下单」的合约规格（乘数 / 一跳 / 成本 / 保证金）。

    ★ 2026-10-18 新增（选品种落实的安全阀）：
      55 品种池里有一部分（CS / B / PG / RR / SC / SI / LC / UR / PF / PK / AP / CJ /
      SS / AO / LU / NR / BC / PS）在 config_symbols 里**没有核验过的规格**。
      若被选中，实盘核心 `config_symbols.get()` 会直接 SystemExit；即便兜底，
      乘数错 = 手数与盈亏全错。⇒ 选择层直接不许它们进池，宁可少选，不可错选。
    """
    return str(key).upper() in _cs.SYMBOLS


def filter_tradable(daily: dict) -> tuple[dict, list[str]]:
    """把 {key: {...}} 过滤成「规格齐备」的子集，并返回被排除的 key 列表。"""
    kept, skipped = {}, []
    for k, v in (daily or {}).items():
        if tradable(k):
            kept[k] = v
        else:
            skipped.append(k)
    return kept, skipped


def _daily_score(closes: list[float], n: int) -> float:
    """风险调整动量：近 n 日收益率 / 近 n 日日收益波动率。"""
    if closes is None or len(closes) < n + 2:
        return 0.0
    window = closes[-(n + 1):]
    rets = [(window[i + 1] / window[i] - 1.0) for i in range(len(window) - 1)]
    if not rets:
        return 0.0
    mean_r = sum(rets) / len(rets)
    total_ret = window[-1] / window[0] - 1.0
    var = sum((r - mean_r) ** 2 for r in rets) / max(1, len(rets) - 1)
    vol = math.sqrt(var)
    if vol <= 1e-12:
        return 0.0
    return total_ret / vol


def rank_daily(closes_by_key: dict[str, list[float]], n: int = 20, q: int = 3
               ) -> dict[str, int]:
    """
    纯函数：给定 {key: 日线收盘序列(最新在末)}，返回 {key: dir}，
    dir ∈ {+1 做多, -1 做空, 0 不持仓}。
    头部 Q 个 → +1；尾部 Q 个 → -1；其余 → 0。
    """
    scores = {}
    for key, closes in closes_by_key.items():
        if _u.get(key) is None:
            continue
        try:
            scores[key] = _daily_score(closes, n)
        except Exception:
            scores[key] = 0.0
    ranked = sorted(scores.items(), key=lambda kv: kv[1])
    m = len(ranked)
    q = max(0, min(q, m // 2))  # 不能超出一半
    out = {k: 0 for k in scores}
    for i in range(q):
        out[ranked[i][0]] = -1          # 尾部做空
        out[ranked[m - 1 - i][0]] = +1  # 头部做多
    return out


def select_two_stage_core(daily: dict[str, dict], vol_window: int = 20,
                          vol_top_m: int = 20, ret_window: int = 3,
                          ret_top_n: int = 6, sector_cap: int | None = None,
                          exclude=None
                          ) -> tuple[list[str], list[str], bool]:
    """
    卡 C 两阶段选择（纯函数，无网络，无未来函数）。核心实现，返回 (selected, stage1, overflow)。

    输入 daily: {key: {"close": [..], "volume": [..]}}，序列「最新一根在末」，
         且必须是**已收盘**日线（调用方负责只喂已收盘数据）。
    阶段1（流动性闸门）：各品种近 vol_window 日平均成交量（手）降序，取前 vol_top_m。
    阶段2（活跃度）    ：在阶段1结果内，近 ret_window 日累计 |涨跌幅|
                        = |close[-1] / close[-(ret_window+1)] - 1| 降序，取前 ret_top_n。
    sector_cap（可选） ：阶段2 填充时每个板块最多取 sector_cap 个（贪心，见 cap_fill）。
                        为 None 时**逐字沿用原逻辑**（取前 ret_top_n，无板块约束）。
    exclude（可选）    ：人工永久剔除名单（workbench.json 的 selection.exclude）。
                        ★ 2026-10-19：在**阶段1 之前**剔除 —— 不参与流动性排序、
                        不占 vol_top_m / ret_top_n 名额，等于「不在池里」。这是唯一
                        正确的位置：若放在阶段2 之后剔，名额会被它白占一个。

    返回 (selected, stage1, overflow)：
      selected = 最终选中的品种（按排名，最活跃在前）；sector_cap 下可能 < ret_top_n；
      stage1   = 阶段1 流动性池（诊断 / 看“差一点没选上”的品种）；
      overflow= True 表示 sector_cap 下贪心被卡住 / ranked 不足 而未能取满 ret_top_n。

    无未来函数证明：只用 close[-1]（已收盘），不引用任何 i+1 数据；
    涨跌幅窗口用「末端已收盘 K 线」，下一交易日开仓才用结论。
    数据不足 vol_window / (ret_window+1) 的品种被跳过（不在结果中）。
    """
    # ---- 阶段1 之前：人工永久剔除（不参与排序、不占名额）----
    if exclude:
        _ex = {str(x).strip().upper() for x in exclude if str(x).strip()}
        if _ex:
            daily = {k: v for k, v in daily.items()
                     if str(k).upper() not in _ex}

    # ---- 阶段1：流动性闸门（成交量）----（sector_cap 下也完全不动）
    vol_avg = {}
    for key, d in daily.items():
        if _u.get(key) is None:
            continue
        vol = d.get("volume")
        if not vol or len(vol) < vol_window:
            continue
        try:
            vol_avg[key] = sum(vol[-vol_window:]) / float(vol_window)
        except Exception:
            continue
    stage1 = [k for k, _ in sorted(vol_avg.items(),
                                   key=lambda kv: kv[1], reverse=True)[:vol_top_m]]

    # ---- 阶段2：活跃度（近期 |涨跌幅|）----
    ret_abs = {}
    need = ret_window + 1
    for key in stage1:
        closes = daily[key].get("close")
        if not closes or len(closes) < need:
            continue
        try:
            base = closes[-need]
            last = closes[-1]
            if base is None or base <= 0 or last is None:
                continue
            ret_abs[key] = abs(float(last) / float(base) - 1.0)
        except Exception:
            continue
    ranked = sorted(ret_abs.items(), key=lambda kv: kv[1], reverse=True)
    overflow = False
    if sector_cap is not None:
        # ★ 仅此一处改动：阶段2 填充加板块腿数上限（任务书 §2.2）。
        selected, overflow = cap_fill(ranked, ret_top_n, int(sector_cap))
    else:
        # 原逻辑逐字不变：直接取排名前 ret_top_n。
        selected = [k for k, _ in ranked[:ret_top_n]]
    return selected, stage1, overflow


def select_two_stage(daily: dict[str, dict], vol_window: int = 20,
                     vol_top_m: int = 20, ret_window: int = 3,
                     ret_top_n: int = 6, sector_cap: int | None = None,
                     exclude=None
                     ) -> tuple[list[str], list[str]]:
    """公开接口：与旧签名/返回值（selected, stage1）完全兼容；overflow 被丢弃。

    见 select_two_stage_core 的文档（参数含义相同）。
    """
    selected, stage1, _ = select_two_stage_core(
        daily, vol_window=vol_window, vol_top_m=vol_top_m,
        ret_window=ret_window, ret_top_n=ret_top_n, sector_cap=sector_cap,
        exclude=exclude)
    return selected, stage1


class Selection:
    """选股策略上下文。被 run_workbench / ma13_live_multi 持有。"""

    def __init__(self, mode: str, universe_keys: list[str],
                 selected: list[str] | None = None,
                 cross_n: int = 20, cross_q: int = 3,
                 max_legs: int = 6, selection_cfg: dict | None = None):
        # 规范化：config 可能是 "ma13_timeseries"/"cross_sectional"/"liquidity"
        mode = (mode or "timeseries").lower()
        self.is_cross = ("cross" in mode)
        self.is_liquidity = (mode == "liquidity")
        self.mode = ("cross_sectional" if self.is_cross else
                     "liquidity" if self.is_liquidity else "timeseries")
        self.universe_keys = list(universe_keys)
        self.selected = [k.upper() for k in (selected or [])]
        self.cross_n = int(cross_n)
        self.cross_q = min(int(cross_q), max_legs // 2)  # 多 Q + 空 Q = 2Q ≤ MAX_LEGS
        # 卡 C 选择参数（来自 workbench.json 的 "selection" 段，冻结不可回测调）
        sc = selection_cfg or {}
        self.vol_window = int(sc.get("vol_window_days", 20))
        self.vol_top_m = int(sc.get("vol_top_m", 20))
        self.ret_window = int(sc.get("ret_window_days", 3))
        self.ret_top_n = int(sc.get("ret_top_n", 6))
        self.reselect_min_days = int(sc.get("reselect_min_days", 7))  # 约=5交易日
        # 板块配额上限（任务书 §2.2）：读 selection.sector_cap；workbench.json 本次不设
        # ⇒ 默认 None = 逐字沿用原两阶段逻辑（不卡板块）。仅在 workbench.json 显式写入
        # 该字段后才生效（CLI --sector-cap 走 backtest_year 透传，不经此字段）。
        self.sector_cap = sc.get("sector_cap")
        # ★ 人工永久剔除（2026-10-19 用户拍板：焦煤 JM 不再进池）。
        #   语义 = 不参与阶段1 流动性排序、不占名额；不是"选完之后再删"。
        self.exclude = [str(x).strip().upper() for x in (sc.get("exclude") or [])
                        if str(x).strip()]
        self._force: dict[str, int] = {}
        self._stage1: list[str] = []            # 阶段1 流动性池（诊断用）
        self._skipped: list[str] = []           # 因缺合约规格被排除的品种（诊断用）
        self._excluded: list[str] = []          # 因人工剔除名单不参与排序的品种（诊断用）
        self._last_reselect: str | None = None  # "YYYY-MM-DD"

    # ---- 时序 / 卡C 模式：selected 指定跑哪些（卡C 由 update_selection 写入）----
    def active_keys(self) -> list[str]:
        if self.is_cross:
            keys = self.universe_keys   # 截面先跑全池，排名后收敛
        elif self.is_liquidity:
            # ★ 2026-10-18 修：卡C 的 selected 为空时**绝不回退到全池**。
            #   旧写法 `self.selected or self.universe_keys` 意味着"没选出来就跑 57 个"
            #   —— 这正是"没选上的品种也开仓"的最短路径。空 = 没选出来 = 不许开仓。
            keys = self.selected
        else:
            keys = self.selected or self.universe_keys   # 卡A 时序：显式列表优先，否则全池（研究用）
        return [k for k in keys if _u.get(k)]

    def force_dir(self, key: str) -> int:
        """返回该品种被允许的开仓方向：0=多空皆可，+1=仅多，-1=仅空。"""
        if self.is_cross:
            return int(self._force.get(key.upper(), 0))
        return 0  # 时序 / 卡C 不约束方向（方向交给 MA13 信号多空都做）

    # ---- 截面模式：用日线序列更新排名 ----
    def update_ranking(self, closes_by_key: dict[str, list[float]]) -> dict[str, int]:
        """传入全池日线序列，重算 force_dir。返回 {key: dir}。"""
        if not self.is_cross:
            return {}
        self._force = rank_daily(closes_by_key, n=self.cross_n, q=self.cross_q)
        return dict(self._force)

    # ---- 卡 C 模式：两阶段选择 ----
    def update_selection(self, daily: dict[str, dict], today=None) -> list[str]:
        """跑两阶段选择，把结果写入 self.selected / self._stage1，记录重选日期。

        daily: {key: {"close":[..], "volume":[..]}}（已收盘日线）。
        返回最终选中的品种列表（最活跃在前）。
        """
        if not self.is_liquidity:
            return self.selected
        # ★ 只让「规格齐备」的品种参与选择（见 tradable 的说明）
        daily, skipped = filter_tradable(daily)
        self._skipped = skipped
        # 人工剔除命中的品种单独记录（不要并进 _skipped，否则会被误报成"缺规格"）
        _ex = set(self.exclude)
        self._excluded = [k for k in daily if str(k).upper() in _ex] if _ex else []
        sel, stage1, _overflow = select_two_stage_core(
            daily, vol_window=self.vol_window, vol_top_m=self.vol_top_m,
            ret_window=self.ret_window, ret_top_n=self.ret_top_n,
            sector_cap=self.sector_cap, exclude=self.exclude)
        self.selected = [k.upper() for k in sel]
        self._stage1 = [k.upper() for k in stage1]
        if today is not None:
            self._last_reselect = str(today)[:10]
        return self.selected

    def needs_reselect(self, today) -> bool:
        """是否到了周频重选窗口（无数据 / 超期 → True）。"""
        if not self.is_liquidity:
            return False
        if not self._last_reselect:
            return True
        try:
            last = datetime.date.fromisoformat(self._last_reselect)
            t = datetime.date.fromisoformat(str(today)[:10])
            return (t - last).days >= self.reselect_min_days
        except Exception:
            return True

    def selection_report(self) -> dict:
        """卡 C 诊断信息。"""
        return {"stage1": list(self._stage1),
                "selected": list(self.selected),
                "excluded": list(self._excluded),
                "last_reselect": self._last_reselect}

    def chosen_for_report(self) -> dict[str, int]:
        """截面模式下，被选中（dir≠0）的品种及其方向，用于日志。"""
        return {k: d for k, d in self._force.items() if d != 0}


if __name__ == "__main__":
    # 时序：selected 指定
    ts = Selection("timeseries", _u.all_keys(), selected=["RB", "MA", "EG"])
    print("时序 active:", ts.active_keys(), "force(RB)=", ts.force_dir("RB"))

    # 截面：造 6 个品种的合成日线，long 涨、short 跌
    import random
    closes = {}
    for i, k in enumerate(["RB", "MA", "EG", "CU", "AU", "SR"]):
        base = 100 + i
        seq = [base * (1 + 0.002 * j + (0.01 if i < 3 else -0.01) * j) for j in range(25)]
        closes[k] = seq
    cs = Selection("cross_sectional", list(closes.keys()), cross_n=20, cross_q=3)
    force = cs.update_ranking(closes)
    print("截面 force:", force)
    print("选中:", cs.chosen_for_report())
