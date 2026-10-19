# -*- coding: utf-8 -*-
"""把台账 + 行情换算成前端 bundle.json（P0-A）。

只负责"造数据"，不碰 HTML。输出：dist/data/bundle.json
复用原 build_static.py 的数据逻辑（指标全量算完再裁窗口，僵尸腿/吸附/体检口径不变）。
"""
from __future__ import annotations
import json, math, re, sys
from pathlib import Path
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                      # 工具/复盘
sys.path.insert(0, str(ROOT))
import analyze, data, features, sessions, trades as T

# ★ P2-1 模拟下单成本/一跳的权威来源 = 项目根 config_symbols.py（06 回测同口径）。
#   导入失败时用本地留档值兜底（2026-10-18 与 config_symbols 逐一核对过，勿单改此处）。
try:
    _PROJ_ROOT = HERE.parent.parent.parent          # 工具/复盘/build → 项目根
    sys.path.insert(0, str(_PROJ_ROOT))
    import config_symbols as _cs
    _SYMBOLS = {k: v for k, v in _cs.SYMBOLS.items()}
    _COST = {k: float(v) for k, v in _cs.COST.items()}
except Exception:
    _SYMBOLS = {"M": ("豆粕", None, 10, 1), "P": ("棕榈油", None, 10, 1), "SR": ("白糖", None, 10, 1),
                "JM": ("焦煤", None, 60, 0.5), "JD": ("鸡蛋", None, 10, 1), "EB": ("苯乙烯", None, 5, 1)}
    _COST = {"M": 1.5, "P": 2.5, "SR": 2.0, "JM": 9.1, "JD": 5.802, "EB": 1.0}

OUT = ROOT / "dist" / "data" / "bundle.json"
PERIODS = (5, 15, 1440)

# ★★ A21（2026-10-18）：工具里不留 EB。
#   四期拍板③：EB 是僵尸腿（成交流水停 2026-10-09、心跳再没更新），已从策略池删掉。
#   数据层留着它只会造成「下拉列表里有个点进去什么都没有」的空按钮。
#   ★ 这是**唯一**一处黑名单，与 `exec4/pool.py:BLACKLIST` 必须一致。
#     （测试里有一条断言比对两处，避免哪天只改一边。）
EXCLUDED_SYMBOLS = ("EB",)


# ★★ S5 / 板块构成与集中度（2026-10-19 用户要求）
#
# 为什么需要这个：用户看到"化工亏 78%"就想取消化工板块。
# 但那个 78% 是**粗标签**算出来的——它把建材(FG/SA)、能源(FU/BU)、轻工(SP)
# 全塞进了"化工"，而实测（2026-10-19）：
#     建材 每笔 −455、风险调整 −1.0558（最差）
#     真化工 每笔 −229、风险调整 −0.2722（比整体还好）
# ⇒ **用粗标签做决策会误伤**。所以必须把"板块"拆成可核对的细类，且把
#   「每周单板块最多几只」这个**确定性事实**直接显示出来（它不需要统计检验）。
#
# ⚠️ 这份表是**冻结**的：一旦按它做了决策（比如配额），就不许因为"回测更好"再改。
#    改表 = 换假设 = 必须重新预注册。
SECTOR_FINE = {
    # 真化工（烯烃/芳烃/聚酯链）
    "MA": "化工", "EG": "化工", "EB": "化工", "PP": "化工",
    "L": "化工", "V": "化工", "TA": "化工",
    # 能源（原油下游，与化工同源但驱动不同）
    "FU": "能源", "BU": "能源", "SC": "能源", "LU": "能源", "PG": "能源",
    # 建材
    "FG": "建材", "SA": "建材",
    # 黑色
    "JM": "黑色", "J": "黑色", "I": "黑色", "SF": "黑色", "SM": "黑色",
    "RB": "黑色", "HC": "黑色", "SS": "黑色",
    # 贵金属
    "AU": "贵金属", "AG": "贵金属",
    # 有色
    "CU": "有色", "AL": "有色", "ZN": "有色", "PB": "有色",
    "NI": "有色", "SN": "有色", "AO": "有色", "BC": "有色", "SI": "有色",
    # 农产品
    "M": "农产品", "P": "农产品", "Y": "农产品", "RM": "农产品", "OI": "农产品",
    "JD": "农产品", "SR": "农产品", "C": "农产品", "CS": "农产品", "A": "农产品",
    "B": "农产品", "CF": "农产品", "AP": "农产品", "CJ": "农产品", "PK": "农产品",
    "LH": "农产品", "RR": "农产品",
    # 其他
    "SP": "轻工", "RU": "轻工", "NR": "轻工", "UR": "轻工", "PF": "轻工", "PS": "轻工",
    "LC": "新能源",
}


def sector_of(key: str) -> str:
    """品种 → 细板块。未登记返回 '未分类'（前端会显示出来，方便补表）。"""
    return SECTOR_FINE.get(str(key).upper(), "未分类")


def _sector_panel(out: dict) -> dict:
    """板块构成 + 集中度面板（纯展示，不参与选品种/下单）。

    两件事：
      1. **当前池子的板块构成** —— 一眼看出有没有扎堆
      2. **HHI / 有效腿数** —— HHI = Σ(share²)，有效腿数 = 1/HHI
         例：6 腿全同板块 ⇒ HHI=1、有效腿数=1（假分散）
             6 腿 6 板块 ⇒ HHI=1/6、有效腿数=6（真分散）

    ★ 这里**只统计"当前池"**，不做事后 grouping 的"哪个板块赚得多"。
      理由（2026-10-19 实测）：按板块分组比较收益 = 典型数据窥探。
      实测证据链：
        · 粗标签"化工"占 78% 亏损 ⇒ 但按每笔算，黑色(−486)比化工(−306)更差
        · 拆细后建材(−1.056)最差、真化工(−0.272)反而好于整体(−0.334)
        · 按 tick/价 分组看着有 ρ=−0.738 的强相关，但**去掉贵金属后
          低 tick/价 组回到 −228.5 ≈ 全样本 −229.3** ⇒ 相关性是假的，
          真实驱动力只是"贵金属这一年赚了"（且后段已从 +149 衰减到 −3）
      ⇒ 所以本面板**只给结构事实，不给收益排名**。
    """
    pool_keys, pool_src = [], ""
    try:
        wb = ROOT.parent.parent / "策略" / "06_MA13超短线" / "工作台" / "workbench.json"
        import json as _json
        with open(wb, "r", encoding="utf-8") as f:
            pool_keys = [str(k).upper() for k in (_json.load(f).get("selected") or [])]
        pool_src = "workbench.json"
    except Exception as e:
        pool_src = f"读 workbench.json 失败（{type(e).__name__}）"
    if not pool_keys:
        pool_keys = [k for k in out.keys() if k != "_meta"]

    by_sec: dict = {}
    legs = []
    for k in pool_keys:
        sec = sector_of(k)
        by_sec.setdefault(sec, []).append(k)
        legs.append({"key": k, "name": (out.get(k) or {}).get("name", k),
                     "sector": sec, "in_bundle": k in out})

    n = len(pool_keys)
    counts = {s: len(v) for s, v in by_sec.items()}
    shares = {s: c / n for s, c in counts.items()} if n else {}
    hhi = sum(x * x for x in shares.values()) if shares else None
    eff = (1.0 / hhi) if hhi else None
    worst = max(counts.items(), key=lambda kv: kv[1]) if counts else (None, 0)

    return {
        "pool": pool_keys,
        "pool_source": pool_src,
        "legs": legs,
        "by_sector": counts,
        "shares": {s: round(v, 4) for s, v in shares.items()},
        "n_sectors": len(counts),
        "max_per_sector": int(worst[1]) if counts else 0,
        "max_sector": worst[0],
        "hhi": round(hhi, 4) if hhi is not None else None,
        "effective_legs": round(eff, 2) if eff is not None else None,
        "warn_集中": (int(worst[1]) >= 3 if counts else False),
        "note": ("结构性事实，不需统计检验。HHI=Σ(份额²)，有效腿数=1/HHI。"
                 "★ 本面板只给结构，不做任何按板块的绩效比较 —— 那属于事后分组，"
                 "实测拆细后真化工好于整体、tick/价相关去掉贵金属即消失。"),
    }


def _risk_panel(out: dict) -> dict:
    """A20 风险贡献面板：按 RiskParSizer 公式算每腿「参考手数」，给出名义/风险占比。

    ★★ 三条必须守住的纪律（否则这个面板会变成"偷偷改仓位"的工具）：
      1. **只展示、不参与下单**。本函数纯计算，结果只写进 `_meta.risk`，前端只读。
      2. **手数公式必须与实盘一致**（`sizer.RiskParSizer`）： 
         h = round(equity_base × risk_pct / (atr × tick × mult))，夹 [hand_min, hand_max]，
         再 min(h, equity_base × notional_cap / (price × mult))。
         equity_base 锁死 200,000（不是账户余额，理由见 sizer.py 注释）。
      3. **ATR 取 bundle 里 5min 的最后一根**（与实盘"用已收盘 bar 算 ATR"同口径）。

    池子来源：`workbench.json` 的 `selected`（= 线上正在跑的腿）。
    读不到就退化为 bundle 里全部品种，并在 note 里说明。

    返回 {legs:[{key,name,hand,notional,risk,notional_share,risk_share}], ...,
          totals:{...}, params:{...}, note:str}
    """
    # ---- 参数（与 sizer.py 默认值逐字一致）----
    EQ_BASE = 200_000.0
    RISK_PCT = 0.01
    HAND_MIN, HAND_MAX = 1, 8
    NOTIONAL_CAP = 2.0

    # ---- 池子 ----
    pool_keys, pool_src = [], ""
    try:
        wb = ROOT.parent.parent / "策略" / "06_MA13超短线" / "工作台" / "workbench.json"
        import json as _json
        with open(wb, "r", encoding="utf-8") as f:
            pool_keys = [str(k).upper() for k in (_json.load(f).get("selected") or [])]
        pool_src = "workbench.json"
    except Exception as e:
        pool_src = f"读 workbench.json 失败（{type(e).__name__}）→ 退化为 bundle 全部品种"
    if not pool_keys:
        pool_keys = [k for k in out.keys() if k != "_meta"]
    missing = [k for k in pool_keys if k not in out]

    legs = []
    for k in pool_keys:
        d = out.get(k)
        if not d:
            continue
        ind = (d.get("periods") or {}).get("5") or {}
        atr_s, ma_s = ind.get("atr"), ind.get("ma")
        price = None
        try:
            price = float(d["last"])
        except Exception:
            pass
        atr = float(atr_s[-1]) if (atr_s and atr_s[-1] is not None) else None
        mult = float(d.get("mult") or 0)
        tick = float(d.get("tick") or 0)
        if not (price and price > 0 and mult > 0 and tick > 0 and atr and atr > 0):
            legs.append({"key": k, "name": d.get("name", k), "error": "缺 ATR/价格/乘数",
                         "hand": None})
            continue
        # RiskParSizer 逐字复刻
        h = int(round((EQ_BASE * RISK_PCT) / (atr * tick * mult)))
        h = max(HAND_MIN, min(HAND_MAX, h))
        cap_hand = int((EQ_BASE * NOTIONAL_CAP) / (price * mult))
        h = min(h, max(HAND_MIN, cap_hand))
        notional = h * price * mult
        risk = h * atr * mult          # ATR 口径的风险额（1 个 ATR 的波动）
        legs.append({
            "key": k, "name": d.get("name", k), "price": round(price, 3),
            "atr": round(atr, 4), "mult": mult, "tick": tick,
            "hand": h, "notional": round(notional, 0), "risk": round(risk, 0),
            "capped_by": ("notional_cap" if cap_hand < HAND_MAX else
                          ("hand_max" if h >= HAND_MAX else
                           ("hand_min" if h <= HAND_MIN else "risk_par"))),
        })

    ok_legs = [x for x in legs if x.get("hand")]
    tot_n = sum(x["notional"] for x in ok_legs) or 1.0
    tot_r = sum(x["risk"] for x in ok_legs) or 1.0
    for x in ok_legs:
        x["notional_share"] = round(x["notional"] / tot_n, 4)
        x["risk_share"] = round(x["risk"] / tot_r, 4)

    eq = sum(x["notional"] for x in ok_legs)
    # 集中度：单腿风险占比 > 35% 提示（不是规则，只是让人看见）
    conc = [x["key"] for x in ok_legs if x["risk_share"] > 0.35]
    return {
        "legs": legs,
        "totals": {
            "n_legs": len(ok_legs),
            # ★ 完整性标记：池子有 6 腿但 bundle 只含 2 腿时，面板是**残缺**的，
            #   不标出来会被误读成"整个组合只有 2 腿、风险可控"。
            "pool_n": len(pool_keys),
            "complete": (len(missing) == 0),
            "notional_sum": round(tot_n, 0),
            "risk_sum": round(tot_r, 0),
            "notional_x_equity": round(eq / EQ_BASE, 3) if EQ_BASE else None,
            "concentrated": conc,
        },
        "params": {"equity_base": EQ_BASE, "risk_pct": RISK_PCT,
                   "hand_min": HAND_MIN, "hand_max": HAND_MAX,
                   "notional_cap": NOTIONAL_CAP},
        "pool_source": pool_src,
        "missing_from_bundle": missing,
        "note": ("★ 参考值，不参与下单。手数按 RiskParSizer 公式复刻；"
                 "ATR 取 5min 最后一根。equity_base 锁死 20 万（非账户余额）⇒ "
                 "notional_x_equity 是「相对手数基数」的倍数，"
                 "不是「占真实账户权益」的比例（同向敞口上限按真实权益算）。"),
    }


def _pool_epochs() -> dict:
    """A19 池子周期：从模拟盘成交流水的 **START 行**解析真实池子变更。

    ★ 为什么必须从台账读，不能读 workbench.json：
      `workbench.json` 只有**当前** `selected`，改一次就覆盖，历史不可复原。
      台账 START 行每次启动都记一遍池子名单（`[... / ...]`）⇒ **去重连续相同名单**
      就是「池子纪元」。这是目前唯一的池子变更留档。

    ★ 周期口径（方案 §4.8(a)）：**池子资格 = 5 个交易日**，不是持仓期限。
      一个纪元若活过 5 个交易日，前端按 5 日切段（C1 / C2…），尾段不足 5 日标出来。

    注意：START 行里的品种名不全是 bundle 里的品种（FU/BU/MA/SA 没 K 线缓存），
      所以这里只负责「当时池子里有谁」，不负责「有没有成交」。

    返回 {"source","cycle_days","restarts","epochs":[{t,t_last,keys,names,restarts}]}
    """
    import csv as _csv
    import re as _re

    led = ROOT.parent.parent / "策略" / "06_MA13超短线" / "模拟盘" / "台账"
    name2key = {}
    try:
        for k, v in _cs.SYMBOLS.items():          # config_symbols 里 FU/BU 由 setdefault 并入
            name2key[str(v[0])] = str(k).upper()
    except Exception:
        pass
    for k, (cn, _m) in data.RECENT_SPECS.items():
        name2key.setdefault(cn, k)

    events = []
    for f in sorted(led.glob("*_成交流水.csv")) if led.exists() else []:
        try:
            with open(f, "r", encoding="utf-8-sig", newline="") as fh:
                for row in _csv.DictReader(fh):
                    if str(row.get("类型") or "").strip() != "START":
                        continue
                    memo = str(row.get("备注") or "")
                    m = _re.search(r"\[([^\]]+)\]", memo)
                    if not m:
                        continue
                    names = [x.strip() for x in m.group(1).split("/") if x.strip()]
                    keys = [name2key.get(n, n) for n in names]
                    ts = str(row.get("记录时间") or "").strip()[:16]
                    if len(ts) < 16:
                        continue
                    events.append({"t": ts, "names": names, "keys": keys})
        except Exception as e:
            print(f"    [A19] 读 {f.name} 失败（{type(e).__name__}）→ 跳过", flush=True)

    events.sort(key=lambda e: e["t"])
    epochs = []
    for e in events:
        if epochs and epochs[-1]["keys"] == e["keys"]:
            epochs[-1]["restarts"] += 1          # 同池子重启 ≠ 换池
            epochs[-1]["t_last"] = e["t"]
            continue
        epochs.append({"t": e["t"], "t_last": e["t"], "keys": e["keys"],
                       "names": e["names"], "restarts": 1})

    # ── 噪声清洗：台账里有一批"不是换池"的 START ────────────────────────
    #   实测两类（2026-10-18 核对）：
    #   ① 单腿重启 —— 只启一个品种，池子名单只有一个（≠ 换池）
    #   ② 短命重启 —— 旧脚本/僵尸腿用**旧池子**起了几分钟就被正确进程覆盖
    #      （苯乙烯 2026-10-09 23:45 起僵尸，23:54 被覆盖 ⇒ 只活 9 分钟）
    #   两条都是"操作噪声"，不是池子变更；不剔会把 3 个真实纪元切成 7 段。
    #   ★ 判据用「活不过 60 分钟」而不是"名单再现"：后者会误杀 A→B→A 的真实换回。
    from datetime import datetime as _dt
    MIN_LIVE_MIN = 60
    noise = []

    def _mins(a, b):
        fa = "%Y-%m-%d %H:%M"
        return (_dt.strptime(b, fa) - _dt.strptime(a, fa)).total_seconds() / 60.0

    changed = True
    while changed:
        changed = False
        for i, ep in enumerate(epochs):
            nxt = epochs[i + 1]["t"] if i + 1 < len(epochs) else None
            why = None
            if len(ep["keys"]) < 2:
                why = "单腿重启（名单 <2）"
            elif nxt and _mins(ep["t"], nxt) < MIN_LIVE_MIN:
                why = f"短命重启（{int(_mins(ep['t'], nxt))} 分钟 < {MIN_LIVE_MIN}）"
            if why:
                noise.append({"t": ep["t"], "keys": ep["keys"], "why": why})
                epochs.pop(i)
                changed = True
                # 剔除后相邻同名 → 并回上一个（同一池子的重启）
                for j in range(len(epochs) - 1, 0, -1):
                    if epochs[j]["keys"] == epochs[j - 1]["keys"]:
                        epochs[j - 1]["restarts"] += epochs[j]["restarts"]
                        epochs[j - 1]["t_last"] = epochs[j]["t_last"]
                        epochs.pop(j)
                break

    for i, ep in enumerate(epochs):
        ep["i"] = i + 1
    for nz in noise:
        print(f"    [A19] 剔除噪声 START {nz['t']} {'/'.join(nz['keys'])}：{nz['why']}", flush=True)
    print(f"[A19] 池子纪元 {len(epochs)} 个（START {len(events)} 条 → 剔噪声 {len(noise)}）: "
          + " → ".join(f"#{e['i']} {e['t']} {'/'.join(e['keys'])}" for e in epochs), flush=True)
    return {"source": "台账 成交流水 START 行（连续同名单去重）",
            "cycle_days": 5, "restarts": len(events),
            "dropped_noise": noise, "epochs": epochs}


def _intrabar_stop_in_code():
    """从策略源码读当前 INTRABAR_STOP 值（★ 不许硬编码时间点/取值）。

    为什么必须读代码：台账里的切换点是"历史生效时刻"，而**当前口径**只有代码知道。
    两者不一致恰恰是最重要的信号——说明"代码改了但进程还没重启"，
    这时若拿代码值当分段依据，会把旧口径的成交误判成新口径。
    """
    p = ROOT.parent.parent / "策略" / "06_MA13超短线" / "模拟盘" / "ma13_live_multi.py"
    try:
        txt = p.read_text(encoding="utf-8")
        m = re.search(r"^\s*INTRABAR_STOP\s*=\s*(True|False)\b", txt, re.M)
        if m:
            return (m.group(1) == "True"), p.name
    except Exception:
        pass
    return None, ""


def _version_switch() -> dict:
    """A11 版本分段：从台账「版本切换记录.csv」解析 INTRABAR_STOP 切换点。

    ★ 为什么用独立全局文件而不是 workbench.json：workbench 只有当前值，会覆盖历史。
      切换点是**事实**（代码落地时刻），写在台账留档，复盘按开仓时刻 t 切，不构成未来函数。

    ★ 口径（2026-10-18 核证）：
      ma13_live_multi.py:143 `INTRABAR_STOP = True`，文件 mtime 2026-10-18 22:06:20。
      模拟盘 10-17 20:03 停，唯一重启 10-18 22:01:48（早于 22:06:20 编辑）
      ⇒ 当前进程跑的是旧码，True 版已落地但未真正交易。
      ⇒ 切换点取代码落地时刻 22:06:20：t < 该时刻全部旧版，t >= 该时刻为新版。
      全部 90 笔已结算 t 均 < 22:06:20 ⇒ 现全归旧版；新版成交 = 0。

    返回 {"source","switches":[{t,on}],"note":...}；无标记 ⇒ switches=[]
    """
    import csv as _csv
    led = ROOT.parent.parent / "策略" / "06_MA13超短线" / "模拟盘" / "台账"
    switches = []
    src_file = led / "版本切换记录.csv"
    if src_file.exists():
        try:
            with open(src_file, "r", encoding="utf-8-sig", newline="") as fh:
                for row in _csv.DictReader(fh):
                    ts = str(row.get("记录时间") or "").strip()[:16]
                    if len(ts) < 16:
                        continue
                    on = str(row.get("INTRABAR_STOP") or "").strip().lower() == "true"
                    # 来源：启动自记录（口径真正生效的时刻）/ 手工补录（只能代表代码落地）
                    src = str(row.get("来源") or "").strip() or "手工补录"
                    switches.append({"t": ts, "on": on, "src": src})
        except Exception as e:
            print(f"    [A11] 读 {src_file.name} 失败（{type(e).__name__}）→ 无版本分段", flush=True)
    # 兜底：也扫成交流水里的「版本切换」类型行（若将来策略直接写进成交流水）
    for f in sorted(led.glob("*_成交流水.csv")) if led.exists() else []:
        try:
            with open(f, "r", encoding="utf-8-sig", newline="") as fh:
                for row in _csv.DictReader(fh):
                    if str(row.get("类型") or "").strip() != "版本切换":
                        continue
                    ts = str(row.get("记录时间") or "").strip()[:16]
                    if len(ts) < 16:
                        continue
                    memo = str(row.get("备注") or "")
                    on = "false" not in memo.lower()      # 标记即新版本，除非备注显式含 false
                    switches.append({"t": ts, "on": on, "src": "成交流水"})
        except Exception:
            pass
    switches.sort(key=lambda s: s["t"])
    # 合并相邻同 on（一次重启写多份 / 全局+兜底重复 / A→B→A 真实换回不在此列）
    merged = []
    for s in switches:
        if merged and merged[-1]["on"] == s["on"]:
            continue
        merged.append(s)
    if not merged:
        return {"source": "", "switches": [],
                "note": "台账无版本切换标记（方案 §1.4 后果 2）→ 全部按同一版本，跨版本混算风险待标记后消除"}
    print(f"[A11] 版本切换 {len(merged)} 个："
          + " → ".join(f"{'新' if s['on'] else '旧'}@{s['t']}({s.get('src', '')})"
                       for s in merged), flush=True)
    code_val, code_src = _intrabar_stop_in_code()
    note = ""
    if code_val is not None and merged:
        last_on = merged[-1]["on"]
        if last_on != code_val:
            note = (f"台账最后记录（{'True' if last_on else 'False'} @{merged[-1]['t']}）与代码当前值"
                    f"（INTRABAR_STOP={code_val}）不一致 ⇒ 代码改完还没重启，进程仍在跑旧口径；"
                    f"分段按台账（真正生效的那个）切，不按代码。")
    if code_val is not None:
        print(f"    [A11] 代码当前 INTRABAR_STOP={code_val}（{code_src}）"
              f"｜台账最后={merged[-1]['on'] if merged else '无'}"
              f"{'｜⚠ 不一致（未重启）' if note else ''}", flush=True)
    return {"source": "台账 版本切换记录.csv（生效时刻：自记录=进程启动加载、手工=代码落地；按开仓时刻 t 切分）",
            "switches": merged, "note": note,
            "code_value": code_val, "code_src": code_src}


def _calendar(out) -> list:
    """A12/A13 交易日历：archive 日线 ∪ bundle 5min 交易日（只认日期，不涉及行情）。

    ★ 为什么需要完整历史：bundle 的 periods.day 只从 2026-10-08 起，看不到之前的
      国庆长假，也就判断不了「长假前最后一个交易日」。archive 日线 2016-2026 覆盖完整历史。
    ★ 为什么要并 bundle：archive 日线末端有洞（实测缺 2026-10-09），
      单用 archive 会把「周五 + 周末」误判成 3 天小长假。5min 序列末端更密，用它补洞。

    返回排序去重的 'YYYY-MM-DD' 列表。纯日期，不含任何价格。
    """
    days = set()
    arch = ROOT / "_缓存" / "kline_archive"
    if arch.exists():
        for f in sorted(arch.glob("*_1440min.csv")):
            try:
                df = pd.read_csv(f, usecols=["datetime"])
                for v in pd.to_datetime(df["datetime"]).dt.strftime("%Y-%m-%d").unique():
                    days.add(str(v))
            except Exception:
                pass
    for k in out:
        p5 = ((out[k] or {}).get("periods") or {}).get("5") or {}
        for v in (p5.get("day") or []):
            if v:
                days.add(str(v))
    if not days:
        return []
    return sorted(days)


def _freshness_payload():
    """读 S4 的 `_缓存/pool/_last_update.json`，转成前端角标要的结构。

    ★ 绝不在这里"猜"新鲜度：只认 daily_update.py 写的文件。
      文件不存在 / 解析失败 ⇒ 返回 {"stale": True, "text": "数据从未成功更新"}
      —— 让失败可见，而不是显示一个漂亮的"刚刚更新"。
    """
    import json
    from datetime import datetime
    p = ROOT / "_缓存" / "pool" / "_last_update.json"
    try:
        with open(p, "r", encoding="utf-8") as f:
            info = json.load(f) or {}
    except Exception:
        return {"stale": True, "hours": None, "text": "数据从未成功更新（无更新记录）"}
    # 只有"真的拉了行情"的成功记录才算数（与 store.freshness_hours 同口径）
    if not info.get("ok") or info.get("data_updated") is False:
        return {"stale": True, "hours": None,
                "text": "上次数据更新未成功（见 _缓存/pool/_FAILED_*.txt）"}
    ts = info.get("finished_at") or info.get("started_at")
    try:
        t = datetime.fromisoformat(str(ts))
        h = (datetime.now() - t).total_seconds() / 3600.0
    except Exception:
        return {"stale": True, "hours": None, "text": "数据更新时间无法解析"}
    if h < 1:
        txt = f"数据更新于 {int(h * 60)} 分钟前"
    else:
        txt = f"数据更新于 {h:.1f} 小时前"
    return {"stale": bool(h > 24), "hours": round(h, 2), "text": txt,
            "archive_total": info.get("archive_total_rows"),
            "archive_new": info.get("archive_new_rows")}

# ★ 复盘窗口（用户 2026-10-18：不需要 3 年，只要这一个月）
WIN_DAYS = 30          # 所有周期统一保留最近 30 天（"这一个月"）
LEAD_IN_DAYS = 1       # 窗口起点再往前 1 天（夜盘归属次日，保证首根完整）


def _x(s, period):
    s = pd.Timestamp(s)
    return s.strftime("%Y-%m-%d" if period == 1440 else "%Y-%m-%d %H:%M")

def _arr(vals, nd=2):
    return [None if pd.isna(v) else round(float(v), nd) for v in vals]

def _holiday_dates(x_list):
    dates = sorted({pd.Timestamp(s).normalize() for s in x_list})
    out = []
    for a, b in zip(dates[:-1], dates[1:]):
        gap = (b - a).days
        if gap <= 1:
            continue
        for k in range(1, gap):
            d = a + pd.Timedelta(days=k)
            if d.weekday() < 5:
                out.append(d.strftime("%Y-%m-%d"))
    return out

def _clean_nan(o):
    if isinstance(o, dict):
        return {k: _clean_nan(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_clean_nan(v) for v in o]
    if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
        return None
    return o

def build_stats(df, exc, n_closes, order=None, with_by_symbol=False):
    pnl = pd.to_numeric(df["盈亏"], errors="coerce")
    settled = pnl.notna()
    p_null = df["平仓价"].isna()
    e_null = df["开仓价"].isna()
    n_trades = int(len(df))
    n_settled = int(settled.sum())
    n_takeover = int((e_null & ~p_null).sum())
    n_open = int(p_null.sum())
    p = pnl[settled]
    win = int((p > 0).sum()); loss = int((p < 0).sum()); flat = int((p == 0).sum())
    avg_win = float(p[p > 0].mean()) if win else None
    avg_loss = float(p[p < 0].mean()) if loss else None
    payoff = (abs(avg_win / avg_loss) if (avg_win is not None and avg_loss not in (None, 0)) else None)
    h = pd.to_numeric(df.loc[settled, "持仓分钟"], errors="coerce").dropna()
    st = df[settled]
    by_reason = []
    for r, g in st.groupby("原因"):
        gp = pd.to_numeric(g["盈亏"], errors="coerce")
        by_reason.append({"r": str(r), "n": int(len(g)), "sum": float(gp.sum()),
                          "mean": float(gp.mean()), "win": int((gp > 0).sum())})
    by_reason.sort(key=lambda x: x["sum"])
    ps = df[df["原因"] == "价格止损"].copy()
    sign = ps["方向"].map({"多": 1, "空": -1})
    slip = (pd.to_numeric(ps["平仓价"], errors="coerce")
            - pd.to_numeric(ps["止损"], errors="coerce")) * sign
    mult = ps["key"].map(data.MULT).fillna(1) if "key" in ps.columns else 1
    amount = slip * pd.to_numeric(ps["手数"], errors="coerce") * mult
    ok = slip.notna()
    good = pd.DataFrame({"滑点": slip[ok]})
    bad = ps[~ok]
    sl = analyze.slippage_stats(good, bad)
    sd = slip[ok]
    slippage = {"n_ok": int(sl["n_ok"]), "n_excluded": int(sl["n_excluded"]),
        "median": sl["median"], "mean": sl["mean"], "p10": float(sd.quantile(0.10)) if len(sd) else None,
        "p25": sl["p25"], "p75": sl["p75"], "min": float(sd.min()) if len(sd) else None,
        "worse_n": int((sd < 0).sum()), "worse_share": float((sd < 0).mean()) if len(sd) else None,
        "sum_amount": float(amount[ok].sum()) if len(sd) else None}
    ex = analyze.excursion_stats(exc, n_closes) if exc is not None else {}
    excursion = {k: ex.get(k) for k in ("n","n_closes","coverage","mfe_median","mae_median",
        "mfe_p25","mfe_p75","mae_p25","mae_p75","mfe_mean","mae_mean")}
    by_symbol = []
    if with_by_symbol and order:
        for k in order:
            sub = df[df["key"] == k] if "key" in df.columns else df.iloc[0:0]
            sp = pd.to_numeric(sub["盈亏"], errors="coerce")
            by_symbol.append({"key": k, "name": data.RECENT_SPECS[k][0],
                "n": int(len(sub)), "pnl_n": int(sp.notna().sum()),
                "sum": float(sp.sum()) if sp.notna().any() else 0.0})
    return _clean_nan({"n_trades": n_trades, "n_settled": n_settled, "n_takeover": n_takeover,
        "n_open": n_open, "win": win, "loss": loss, "flat": flat,
        "pnl_sum": float(p.sum()) if len(p) else 0.0, "pnl_mean": float(p.mean()) if len(p) else None,
        "win_rate": (win / n_settled) if n_settled else None, "avg_win": avg_win, "avg_loss": avg_loss,
        "payoff": payoff, "hold_median": float(h.median()) if len(h) else None,
        "hold_mean": float(h.mean()) if len(h) else None, "by_reason": by_reason,
        "by_symbol": by_symbol, "slippage": slippage, "excursion": excursion})


def build_data():
    out = {}
    ledger = analyze.load_ledger()
    exc_all = analyze.excursion_rows(ledger)
    n_closes_all = analyze.closes_count(ledger)
    fill_t = pd.to_datetime(ledger.loc[ledger["类型"] == "FILL", "记录时间"], errors="coerce").dropna()
    if len(fill_t):
        sim_start = fill_t.min().normalize()
        sim_end = fill_t.max().normalize()
    else:
        sim_end = pd.Timestamp.now().normalize()
        sim_start = sim_end - pd.Timedelta(days=WIN_DAYS)
    # ★ 窗口 = 最近 WIN_DAYS 天（"这一个月"），锚定在模拟盘最新成交日
    END = sim_end
    win_start = END - pd.Timedelta(days=WIN_DAYS)
    start_i = win_start - pd.Timedelta(days=LEAD_IN_DAYS)
    start_d = win_start - pd.Timedelta(days=LEAD_IN_DAYS)
    print(f"[window] 数据窗口 = 最近 {WIN_DAYS} 天：{start_i.date()} ~ {END.date()} "
          f"（模拟盘 {sim_start.date()} ~ {sim_end.date()}）", flush=True)

    all_tr = []
    for key in data.RECENT_SPECS:
        # ★ A21：僵尸腿/黑名单不进 bundle（否则前端会留一个点进去空白的按钮）
        if key in EXCLUDED_SYMBOLS:
            print(f"  ⏭  {key}：已列入 EXCLUDED_SYMBOLS（僵尸腿/四期拍板删除）→ 不进 bundle",
                  flush=True)
            continue
        cn = data.RECENT_SPECS[key][0]
        # ★ 容错：新品种加进 RECENT_SPECS 后若还没拉过 K 线，跳过并提示，不中断整轮构建
        miss = [p for p in PERIODS if not (data.RECENT_DIR / f"{key}_{p}min.csv").exists()]
        if miss:
            print(f"  ⚠ {cn} {key}：缺 K 线缓存（{miss}）→ 跳过（先跑 data.py --fetch2026 拉取后再构建）", flush=True)
            continue
        print(f"  {cn} {key} …", flush=True)
        df5, _ = data.load_recent(key, 5)
        last_price = float(df5["close"].iloc[-1])
        periods = {}
        for p in PERIODS:
            df, _ = data.load_recent(key, p)
            if p == 1440:
                df = df.assign(x=df["datetime"].dt.normalize() + pd.Timedelta(days=1))
            else:
                df = df.assign(x=data.to_beijing(df["datetime"]))
            F = features.build_custom(df)
            cf = F["close"].astype(float)
            ema12 = cf.ewm(span=12, adjust=False).mean()
            ema26 = cf.ewm(span=26, adjust=False).mean()
            dif = ema12 - ema26
            dea = dif.ewm(span=9, adjust=False).mean()
            hist = (dif - dea) * 2
            delta = cf.diff()
            up = delta.clip(lower=0).rolling(14).mean()
            dn = (-delta.clip(upper=0)).rolling(14).mean()
            rs = up / dn.replace(0, np.nan)
            F["dif"], F["dea"], F["hist"] = dif, dea, hist
            F["rsi"] = 100 - 100 / (1 + rs)
            low9 = F["low"].astype(float).rolling(9).min()
            high9 = F["high"].astype(float).rolling(9).max()
            rsv = (cf - low9) / (high9 - low9).replace(0, np.nan) * 100
            F["k"] = rsv.ewm(com=2, adjust=False).mean()
            F["d"] = F["k"].ewm(com=2, adjust=False).mean()
            F["j"] = 3 * F["k"] - 2 * F["d"]
            f = (F[F["x"] >= start_d] if p == 1440
                 else F[F["x"].dt.normalize() >= start_i]).reset_index(drop=True)
            periods[str(p)] = {
                "x": [_x(v, p) for v in f["x"]],
                "o": _arr(f["open"], 1), "h": _arr(f["high"], 1), "l": _arr(f["low"], 1),
                "c": _arr(f["close"], 1),
                "v": [int(v) if pd.notna(v) else 0 for v in f["volume"]],
                "ma": _arr(f["ma"], 2), "atr": _arr(f["atr"], 2),
                "slope": _arr(f["slope"], 3), "atr_dist": _arr(f["atr_dist"], 3),
                "boll_u": _arr(f["boll_upper"], 2), "boll_m": _arr(f["boll_mid"], 2), "boll_l": _arr(f["boll_lower"], 2),
                "day": [pd.Timestamp(sessions.trading_day_of_bj(v)).strftime("%Y-%m-%d") for v in f["x"]],
                "dif": _arr(f["dif"], 2), "dea": _arr(f["dea"], 2), "hist": _arr(f["hist"], 2),
                "rsi": _arr(f["rsi"], 2), "k": _arr(f["k"], 2), "d": _arr(f["d"], 2), "j": _arr(f["j"], 2),
            }
        grid5 = pd.DatetimeIndex(pd.to_datetime(periods["5"]["x"]))
        xs5_set = set(periods["5"]["x"])
        def _snap5(ts):
            ts = pd.Timestamp(ts)
            i = int(grid5.searchsorted(ts, side="right")) - 1
            if i < 0: i = 0
            if i >= len(grid5): i = len(grid5) - 1
            return grid5[i].strftime("%Y-%m-%d %H:%M")
        ev = T.load_events(cn)
        events = []
        for _, r in ev.iterrows():
            if pd.isna(r["价格"]): continue
            events.append({"t": _snap5(r["时间"]),
                "day": pd.Timestamp(sessions.trading_day_of_bj(r["时间"])).strftime("%Y-%m-%d"),
                "y": r["类型"], "d": r["方向"], "q": int(r["手数"]), "p": float(r["价格"])})
        tr = T.load_trades(cn)
        trades = []
        for i, r in tr.iterrows():
            en = r["开仓时间"]; ex = r["平仓时间"]
            trades.append({"i": int(i),
                "t": _snap5(en) if pd.notna(en) else None,
                "x": _snap5(ex) if pd.notna(ex) else None,
                "day": pd.Timestamp(sessions.trading_day_of_bj(en)).strftime("%Y-%m-%d") if pd.notna(en) else None,
                "xday": pd.Timestamp(sessions.trading_day_of_bj(ex)).strftime("%Y-%m-%d") if pd.notna(ex) else None,
                "d": r["方向"], "q": int(r["手数"]),
                "e": None if pd.isna(r["开仓价"]) else round(float(r["开仓价"]), 1),
                "p": None if pd.isna(r["平仓价"]) else round(float(r["平仓价"]), 1),
                "s": None if pd.isna(r["止损"]) else round(float(r["止损"]), 2),
                "ps": None if pd.isna(r["计划止损"]) else round(float(r["计划止损"]), 2),
                "mfe": None if pd.isna(r["MFE"]) else round(float(r["MFE"]), 0),
                "mae": None if pd.isna(r["MAE"]) else round(float(r["MAE"]), 0),
                "pnl": None if pd.isna(r["盈亏"]) else round(float(r["盈亏"]), 0),
                "r": None if pd.isna(r["原因"]) else str(r["原因"]),
                "k": None if pd.isna(r["配对"]) else str(r["配对"]),
                "h": None if pd.isna(r["持仓分钟"]) else round(float(r["持仓分钟"]), 0)})
        # ══ ★ P3-2：MAE/MFE 补全到 100%（2026-10-18）══════════════════════════
        # 台账 EXCURSION 是逐 tick/逐分钟算的，靠「3 分钟窗口」匹配到成交时刻
        # ⇒ 覆盖 82/105 = 78%，匹配不上就是 NaN（analyze.excursion_stats 的 coverage）。
        # 5min 的 o/h/l/c 是全的 ⇒ 从开仓 bar 到平仓 bar 之间用 bar 高低价补算。
        # ⚠️ 口径差异必须标注：K 线口径**系统性低估**（bar 内极值取不到盘中瞬时极值）
        #    ⇒ 打 mfe_src / mae_src = 'ledger' | 'kline'，两者**不混用统计**（二期红线 6）。
        # ⚠️ 金额口径与台账一致：台账记的是「元」，已含手数与乘数（ma13_live_multi.py:716
        #    的 peak_favorable 是浮盈金额）⇒ 这里必须 × qty × mult，只乘乘数会小 qty 倍。
        _xs5 = periods["5"]["x"]
        def _idx_le5(ts):
            """包含该时刻的 bar 下标（最后一个 x <= t）；与前端 _scAbsOf/_cmpAbsOf 同口径。"""
            k5 = _snap5(ts)
            lo, hi, ans = 0, len(_xs5) - 1, -1
            while lo <= hi:
                mid = (lo + hi) // 2
                if _xs5[mid] <= k5:
                    ans, lo = mid, mid + 1
                else:
                    hi = mid - 1
            return ans
        _mult5 = float(data.MULT.get(key, 1))
        n_kline = 0
        for t in trades:
            t["mfe_src"] = "ledger" if t["mfe"] is not None else None
            t["mae_src"] = "ledger" if t["mae"] is not None else None
            if t["mfe"] is not None and t["mae"] is not None:
                continue
            # 接管笔（开仓价未知）与未平仓无法补算 —— 这两类不进分母，不是"补不上"
            if t["e"] is None or t["t"] is None or t["x"] is None:
                continue
            a, b = _idx_le5(t["t"]), _idx_le5(t["x"])
            if a is None or b is None or b < a:
                continue
            seg_h = periods["5"]["h"][a:b + 1]
            seg_l = periods["5"]["l"][a:b + 1]
            if not seg_h or not seg_l:
                continue
            hi_px, lo_px = max(seg_h), min(seg_l)
            q, ep = float(t["q"]), float(t["e"])
            if t["d"] == "多":
                mfe, mae = (hi_px - ep) * q * _mult5, (lo_px - ep) * q * _mult5
            else:
                mfe, mae = (ep - lo_px) * q * _mult5, (ep - hi_px) * q * _mult5
            if t["mfe"] is None:
                t["mfe"], t["mfe_src"] = round(mfe, 0), "kline"
            if t["mae"] is None:
                t["mae"], t["mae_src"] = round(mae, 0), "kline"
            n_kline += 1
        if n_kline:
            print(f"    [P3-2] MFE/MAE K 线补算 {n_kline} 笔"
                  f"（src=kline；台账口径 src=ledger，两者不混用）", flush=True)
        df1, _ = data.load_recent(key, 1)
        df1 = df1.assign(x=data.to_beijing(df1["datetime"]))
        lab_all = sessions.label_sessions(df1)
        g = lab_all.groupby("交易日")["close"].last()
        g.index = pd.to_datetime(g.index)
        day_prev_all = {}
        for j, d in enumerate(list(g.index)):
            day_prev_all[d.strftime("%Y-%m-%d")] = round(float(g.iloc[j - 1] if j > 0 else g.iloc[j]), 1)
        keep_from = (start_i - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        day_prev = {k: v for k, v in day_prev_all.items() if k >= keep_from}
        lab = lab_all[lab_all["x"].dt.normalize() >= start_i].copy()
        m1 = {"x": [_x(v, 1) for v in lab["x"]],
              "o": _arr(lab["open"], 1), "c": _arr(lab["close"], 1),
              "v": [int(v) if pd.notna(v) else 0 for v in lab["volume"]],
              "day": [pd.Timestamp(v).strftime("%Y-%m-%d") for v in lab["交易日"]]}
        day_rows = lab[lab["时段"].isin(["早盘", "午盘"])]
        last_day = (pd.Timestamp(day_rows["交易日"].iloc[-1]).strftime("%Y-%m-%d")
                    if len(day_rows) else m1["day"][-1])
        zombie = [t for t in trades if t["k"] == "SYNC清零" and t["p"] is None]
        removed_events = []
        if zombie:
            trades = [t for t in trades if not (t["k"] == "SYNC清零" and t["p"] is None)]
            def _is_zombie_event(e):
                if e["y"] not in ("开", "平"): return False
                for z in zombie:
                    if e["d"] != z["d"]: continue
                    t0, t1 = z["t"], z["x"]
                    if t0 is None: continue
                    if e["t"] == t0: return True
                    if t1 is not None and t0 <= e["t"] <= t1: return True
                return False
            removed_events = [e for e in events if _is_zombie_event(e)]
            events = [e for e in events if not _is_zombie_event(e)]
        note = (f"{'策略已停：' if key == 'EB' else ''}"
                f"{len(zombie)} 笔僵尸腿（对应 {len(removed_events)} 条开/平 FILL）已隐藏"
                if zombie else "")
        for j, t in enumerate(trades):
            t["i"] = j
        holidays = _holiday_dates(periods["5"]["x"])
        holidays_d = _holiday_dates(periods["1440"]["x"])
        meta = dict(data.RECENT_META.get(key, {"has_1015_break": True, "night_end": 23.0}))
        bad5 = sum(1 for e in events if e["t"] not in xs5_set)
        print(f"    [P0-1] zombie trades={len(zombie)} events_removed={len(removed_events)}"
              f" | [P0-2] events={len(events)} not_on_5m={bad5}"
              f" | holidays={len(holidays)}/daily={len(holidays_d)} | meta={meta}", flush=True)
        zmask = (tr["配对"] == "SYNC清零") & tr["平仓价"].isna()
        tr_clean = tr[~zmask].copy()
        tr_clean["key"] = key
        all_tr.append(tr_clean)
        exc_sym = exc_all[exc_all["品种"] == cn] if len(exc_all) else exc_all
        led_sym = ledger[ledger["品种"] == cn]
        stats = build_stats(tr_clean, exc_sym, analyze.closes_count(led_sym))
        out[key] = {"name": cn, "last": round(last_price, 1), "mult": float(data.MULT.get(key, 1)),
            # ★ P2-1 模拟下单：tick=最小变动价位、comm=每手手续费(元/手,单边)——权威来源=项目根 config_symbols.py
            "tick": _SYMBOLS.get(key, (None, None, 1, 1))[3], "comm": float(_COST.get(key, 0.0)),
            "note": note,
            "periods": periods, "events": events, "trades": trades,
            "m1": m1, "day_prev": day_prev, "last_day": last_day,
            "holidays": holidays, "holidays_d": holidays_d, "meta": meta, "stats": stats}
    all_df = pd.concat(all_tr, ignore_index=True)
    stats_all = build_stats(all_df, exc_all, n_closes_all,
                            order=[k for k in data.RECENT_SPECS
                                   if k not in EXCLUDED_SYMBOLS], with_by_symbol=True)
    for k in out:
        out[k]["stats_all"] = stats_all
    sl = stats_all["slippage"]
    print(f"[P1] n={stats_all['n_trades']} settled={stats_all['n_settled']} "
          f"takeover={stats_all['n_takeover']} open={stats_all['n_open']} "
          f"win/loss/flat={stats_all['win']}/{stats_all['loss']}/{stats_all['flat']} "
          f"pnl={stats_all['pnl_sum']:.0f}", flush=True)
    print(f"[P1] slip n={sl['n_ok']} worse={sl['worse_n']}/{sl['n_ok']} "
          f"median={sl['median']:.2f} sum={sl['sum_amount']:.0f}", flush=True)
    print("[P1] by_symbol: " + " | ".join(
        f"{x['key']} {x['n']}/{x['pnl_n']}/{x['sum']:.0f}" for x in stats_all["by_symbol"]), flush=True)
    out["_meta"] = {
        "data_end": sim_end.strftime("%Y-%m-%d"),
        "sim_start": sim_start.strftime("%Y-%m-%d"),
        "window_days": WIN_DAYS,
        "built": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
        "n_symbols": len(out),
        # ★ S4/A-freshness：把「数据更新于 X 小时前」的角标数据带进 bundle。
        #   数据源是 exec4/daily_update.py 写的 _缓存/pool/_last_update.json。
        #   超 24 小时 ⇒ stale=True，UI 显示成红字（方案 §2.6 第 4 条）。
        "freshness": _freshness_payload(),
        # ★ S5/A21：明确记录被排除的品种，前端可据此做断言（不该有人再问"EB 呢"）
        "excluded_symbols": list(EXCLUDED_SYMBOLS),
        # ★ S5/A20：风险贡献面板（只展示，不参与下单）
        "risk": _risk_panel(out),
        # ★ S5：板块构成 + 集中度（只给结构事实，不给板块收益排名）
        "sector": _sector_panel(out),
        # ★ A19：池子周期（真实池子纪元，来自台账 START 行）
        "pool_epochs": _pool_epochs(),
        # ★ A11：版本分段（INTRABAR_STOP 切换点，来自台账 版本切换记录.csv）
        "version_switch": _version_switch(),
        # ★ A12/A13：交易日历（archive 日线 ∪ 5min 交易日；只认日期，不涉及行情）
        "calendar": _calendar(out),
    }
    return out


def main():
    payload = build_data()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"完成：{OUT}  ({OUT.stat().st_size/1024/1024:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
