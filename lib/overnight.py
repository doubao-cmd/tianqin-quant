# -*- coding: utf-8 -*-
"""
隔夜敞口：快照（#22-C）+ 上限闸门（#22-A）
==========================================================
【为什么需要】
06 名义上是日内短线（`MAX_HOLD=15` 根 5min ≈ 75 分钟），但**事实上持仓过夜**：
2026-10-20 收的就是豆粕 +8、纯碱 +8。**目前没有任何隔夜敞口上限、没有长假减仓规则。**
过夜是允许的风险承担还是该消除 —— 这是**策略定义的空缺**，D2 已定性（见下）。

【D2 拍死的口径（不得自行改动）】
  22.1 = **A**：设隔夜上限、**只拒新开、不改已有仓**；同时落地 C（快照）供数据。
  22.1-B（收盘强平）：**仅限 ≥5 天长假**，且仅由 `holiday_close.py` 实现，
        本模块**绝不**做任何平仓动作（红线：非长假场景一律禁止自动平仓）。
  22-A 初始阈值（first-pass，须用 C 的两周快照回检后微调，**不遍历参数择优**）：
        · 隔夜**总**名义敞口 ≤ **1.0 × 权益**（日间 `NOTIONAL_CAP=2.0` 之半，隔夜风险折价）
        · **单品种**隔夜名义 ≤ **0.3 × 权益**（与"单品种保证金 ≤ 权益 30%"同口径）
        · **长假前**（>3 天休市）自动降级为总名义 ≤ **0.5 × 权益**

【★ 两个阈值不一样，别"统一"掉（这是 D2 原文，不是笔误）】
  · 隔夜**降级**用 `gap > 3`（即 ≥4 天：元旦 4 天会降级）
  · 长假**强平**用 `gap >= 5`（3 天清明/端午/中秋都不算，元旦也不算）
  两个用途不同：降级是"少留点"，强平是"全清掉"，后者门槛必须更高。

【口径统一】
  名义敞口 = |手数| × 价格 × 合约乘数，与既有 `NOTIONAL_CAP` 共用同一分母（权益），
  不另立第二套口径。

【自检】
    python -c "from lib import overnight as o; o.selftest()"
"""
import csv
import json
import os
import time as _time

try:
    from . import trade_calendar as _tc
except ImportError:
    import trade_calendar as _tc         # noqa: F401

# ===================== 常量（D2 拍板，改动即改变隔夜风控行为）=====================
# ★★ 2026-10-21 13:29 D2 拍板（用户「改A」）：1.0/0.30 → 2.0/0.45
#   起因：六品种满腿名义 1.733× 权益，日内新开仓被隔夜总闸门全拒（1.733 已 > 1.0）。
#   注意：本闸门**不区分日内/隔夜**，一旦持仓名义超限，连日内开仓也会被挡。
#   下次若出现「策略只能平不能开」，先看这条日志而不是怀疑信号。
TOTAL_RATIO = 2.0            # 隔夜总名义 ≤ 2.0×权益（D2 2026-10-21 由 1.0 上调）
SYM_RATIO = 0.45             # 单品种隔夜名义 ≤ 0.45×权益（D2 2026-10-21 由 0.30 上调）
HOLIDAY_TOTAL_RATIO = 0.5    # 长假前降级到 0.5×权益
DOWNGRADE_MIN_GAP = 4        # 「>3 天休市」⇒ gap ≥ 4 触发降级（★不是 5）

SNAP_JSON = "隔夜敞口_快照.json"
SNAP_CSV = "隔夜敞口_历史.csv"
CSV_HEADER = ["时间", "交易日", "品种", "方向", "手数", "价格", "合约乘数",
              "名义敞口", "占比", "权益", "是否长假前", "长假间隔天", "总名义", "总占比"]


# ---------------------------------------------------------------- 工具
def leg_notional(hands, price, mult):
    try:
        return abs(float(hands)) * float(price) * float(mult)
    except Exception:
        return 0.0


def build_legs(shared, symbols_conf):
    """从 `shared`（各腿共享标量）+ `config_symbols.SYMBOLS` 构造敞口腿。

    返回 [{"key","name","dir","hands","price","mult","notional"}]，只含有仓的腿。
    `pos` 为 0 的腿不进来（那不算隔夜敞口）。
    """
    out = []
    for key, sh in (shared or {}).items():
        try:
            pos = int(sh.get("pos") or 0)
            if pos == 0:
                continue
            px = float(sh.get("price") or 0)
            conf = symbols_conf.get(str(key).upper()) if symbols_conf else None
            if not conf:
                continue
            name_cn, _cont, mult = conf[0], conf[1], float(conf[2])
            out.append({
                "key": str(key).upper(), "name": name_cn,
                "dir": "多" if pos > 0 else "空",
                "hands": abs(pos), "price": px, "mult": mult,
                "notional": leg_notional(abs(pos), px, mult),
            })
        except Exception:
            continue
    out.sort(key=lambda x: -x["notional"])
    return out


def holiday_gap(day):
    """该交易日的长假间隔天数（供降级判定）。非交易日/无法判定返回 -1。"""
    try:
        gap, _ = _tc.gap_days(day)
        return gap
    except Exception:
        return -1


def is_downgrade_day(day):
    """是否已进入"长假前降级"窗口（gap > 3）。"""
    g = holiday_gap(day)
    return g >= DOWNGRADE_MIN_GAP


# ---------------------------------------------------------------- #22-C 快照
def _snap_ts_valid_for_day(ts, day):
    """这份快照的 ts 是否属于「交易日 day 当天」（用于判定它是不是提前消费的）。

    ★★ 为什么需要（2026-10-22 实盘事故）：
      `trading_day()` 在 `hour >= 20` 时就 +1 天（夜盘属次日）。而 `#22-C` 的调用方
      原本只判 `hour >= 15`。于是**夜盘一启动（20:00）就写了一份「次日」的快照**，
      把次日的额度提前消费掉。实测 `2026-10-21 20:00:37` 写了 `trade_day=20261022`
      的份；到 `2026-10-22` 15:00 后，`trading_day()` 仍是 `20261022` ⇒ 幂等命中
      ⇒ **10-22 的真收盘快照整天没写**，历史里留着的是「10-21 夜盘的持仓」却标成
      10-22（FU/SA 方向与实际相反、缺 M、BU 手数不同）。

    判据：`ts` 的**自然日**必须等于 `day`。
    （只校验日期、不校验小时：真实调用方只在 15:00–20:00 窗口内写，小时本就 >= 15；
      而"提前消费"事故的特征恰恰是**日期对不上**——10-21 20:00 写的 ts 落在 10-21，
      却被标成 trade_day=20261022。多校验小时只会误伤早上跑的自检/补记。）

    解析失败一律返回 False（= 需要重写）。方向是刻意的：快照是纯记录，多写一份只会
    多几行日志；漏写会让 `#22-C` 的阈值回检数据源直接缺一天，无法补救。
    """
    try:
        t = _time.strptime(str(ts).strip(), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return False
    return _time.strftime("%Y%m%d", t) == str(day)


def snapshot(record_dir, day, legs, equity, margin=None, force=False):
    """落一份隔夜敞口快照（#22-C）。**永不抛**，返回 (ok, note)。

    幂等：同一交易日默认只写一次（`force=True` 可覆盖，供人工补记）。
    """
    try:
        if not record_dir:
            return False, "无 record_dir"
        td = str(day)
        pj = os.path.join(record_dir, SNAP_JSON)
        pc = os.path.join(record_dir, SNAP_CSV)
        os.makedirs(record_dir, exist_ok=True)

        # 幂等：读 JSON 里的 trade_day
        if not force and os.path.isfile(pj):
            try:
                with open(pj, "r", encoding="utf-8") as f:
                    old = json.load(f)
                if str(old.get("trade_day")) == td and \
                        _snap_ts_valid_for_day(old.get("ts"), td):
                    return False, "本交易日已快照"
            except Exception:
                pass

        try:
            eq = float(equity or 0)
        except Exception:
            eq = 0.0
        # ★ nan 归一成 0：否则 json.dump 会写出非法的 `NaN` 字面量（严格 JSON 解析器会拒收）
        if not (eq == eq):
            eq = 0.0
        gap = holiday_gap(td)
        down = gap >= DOWNGRADE_MIN_GAP
        rows = []
        total = 0.0
        for L in (legs or []):
            n = float(L.get("notional") or 0)
            total += n
            rows.append({
                "key": L.get("key"), "name": L.get("name"), "dir": L.get("dir"),
                "hands": L.get("hands"), "price": L.get("price"),
                "mult": L.get("mult"), "notional": round(n, 2),
                "ratio": round(n / eq, 4) if eq > 0 else None,
            })
        doc = {
            "ts": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "trade_day": td,
            "equity": round(eq, 2),
            "margin": (round(float(margin), 2) if margin is not None else None),
            "margin_ratio": (round(float(margin) / eq, 4)
                             if (margin is not None and eq > 0) else None),
            "holiday_gap": gap,
            "holiday_downgrade": down,
            "total_notional": round(total, 2),
            "total_ratio": (round(total / eq, 4) if eq > 0 else None),
            "legs": rows,
        }
        tmp = pj + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        os.replace(tmp, pj)

        new = not os.path.isfile(pc)
        with open(pc, "a", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(CSV_HEADER)
            if not rows:
                w.writerow([doc["ts"], td, "（空仓）", "", 0, "", "", 0.0, 0.0,
                            doc["equity"], down, gap, 0.0, 0.0])
            for r in rows:
                w.writerow([doc["ts"], td, r["key"], r["dir"], r["hands"],
                            r["price"], r["mult"], r["notional"], r["ratio"],
                            doc["equity"], down, gap, doc["total_notional"],
                            doc["total_ratio"]])
        return True, f"ok legs={len(rows)} total={total:.0f} ratio={doc['total_ratio']}"
    except Exception as e:                          # noqa: BLE001
        try:
            print(f"[隔夜快照] 写入失败（已忽略，不影响交易）：{type(e).__name__}: {e}",
                  flush=True)
        except Exception:
            pass
        return False, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------- #22-A 上限
def check_limit(legs, equity, key=None, new_notional=0.0, day=None):
    """开仓前查隔夜敞口上限。返回 (ok, reason)。

    · **只挡开仓**：由开仓路径调用；平仓/止损永不经过这里。
    · `key` + `new_notional`：本次拟开仓的品种与名义，用于"加进去之后"是否超限。
      若该品种已有仓则不重复计（只在开新腿时用得上）。
    · 无法判定权益（≤0）⇒ 放行（不误杀；与既有 `_risk_check` 约定一致）。
    """
    try:
        eq = float(equity or 0)
        # ★★ tqsdk 的 Account 字段默认是 float("nan") 不是 None（objs.py:322-326 源码确认）。
        #   `bool(nan)` 为 True 且 `nan <= 0` 为 False ⇒ 只写 `if eq <= 0` **挡不住 nan**，
        #   后续 nan 参与比较全是 False，等于静默放行。必须显式判 nan。
        if not (eq == eq) or eq <= 0:
            return True, ""
        total = sum(float(L.get("notional") or 0) for L in (legs or []))
        exist_key = None
        sym_before = 0.0
        for L in (legs or []):
            if key is not None and str(L.get("key")).upper() == str(key).upper():
                exist_key = L.get("key")
                sym_before += float(L.get("notional") or 0)

        # 同一品种已有仓 ⇒ 是加仓，不是新增隔夜来源，本闸门只管"隔夜总敞口"
        add = 0.0 if exist_key else float(new_notional or 0)
        total_after = total + add
        sym_after = sym_before + (float(new_notional or 0) if new_notional else 0.0)

        down = is_downgrade_day(day) if day else False
        tr = HOLIDAY_TOTAL_RATIO if down else TOTAL_RATIO
        tag = f"（长假前降级 {tr:.1f}×）" if down else ""

        # ① 单品种上限（降级时同步收紧到同比例：0.3 × 0.5/1.0 = 0.15）
        sr = SYM_RATIO * (HOLIDAY_TOTAL_RATIO / TOTAL_RATIO) if down else SYM_RATIO
        if sym_after > sr * eq + 1e-6:
            return False, (f"单品种隔夜名义 {sym_after:.0f} > {sr:.2f}×权益 "
                           f"({sr * eq:.0f}){tag} ⇒ 拒绝新开 {key}")
        # ② 总上限
        if total_after > tr * eq + 1e-6:
            return False, (f"组合隔夜名义 {total_after:.0f} > {tr:.1f}×权益 "
                           f"({tr * eq:.0f}){tag} ⇒ 拒绝新开 {key}")
        return True, ""
    except Exception:
        return True, ""


def selftest():
    import tempfile
    conf = {"M": ("豆粕", "DCE.m2701", 10, 1),
            "SA": ("纯碱", "CZCE.SA701", 20, 1)}
    sh = {"M": {"pos": 8, "price": 3200.0},
          "SA": {"pos": -8, "price": 950.0}}
    legs = build_legs(sh, conf)
    assert len(legs) == 2, legs
    assert abs(legs[0]["notional"] - 8 * 3200 * 10) < 1e-6 or \
           abs(legs[0]["notional"] - 8 * 950 * 20) < 1e-6, legs
    print("  1 build_legs OK")

    # 2 空仓不产腿
    assert build_legs({"M": {"pos": 0}}, conf) == []
    print("  2 空仓无腿 OK")

    # 3 正常范围内放行（权益 100 万，两腿共 40.8 万 = 0.41×）
    eq = 1_000_000.0
    ok, why = check_limit(legs, eq, key="RB", new_notional=100_000, day="2026-10-21")
    assert ok is True, why
    print("  3 正常放行 OK")

    # 4 总敞口超 TOTAL_RATIO ⇒ 拒（须构造「单品种不超但组合超」）
    #   ★ 按常量推算腿数，避免把 1.0/0.3 写死（2026-10-21 上调到 2.0/0.45 后原断言失效）
    _per = eq * SYM_RATIO * 0.9                  # 每腿占单品种上限的 90% ⇒ 单品种必不超
    _n = int(TOTAL_RATIO / (SYM_RATIO * 0.9)) + 2  # 腿数 × 每腿 > 总上限
    _many = [{"key": f"K{i}", "notional": _per} for i in range(_n)]
    assert _n * _per > eq * TOTAL_RATIO, "用例构造失败：腿数不足以超总上限"
    ok, why = check_limit(_many, eq, key="RB", new_notional=1_000, day="2026-10-21")
    assert ok is False and "组合隔夜名义" in why, (ok, why)
    print(f"  4 总上限拒开 OK（{_n} 腿 × {_per/eq:.3f}× > {TOTAL_RATIO}×）")

    # 5 单品种超 SYM_RATIO ⇒ 拒
    ok, why = check_limit([], eq, key="RB", new_notional=eq * SYM_RATIO * 1.2,
                          day="2026-10-21")
    assert ok is False and "单品种" in why, (ok, why)
    print(f"  5 单品种上限拒开 OK（{SYM_RATIO}×）")

    # 6 ★长假前降级：两腿共 40.8 万，再加 10 万 = 50.8 万
    #   普通日上限 1.0×（100 万）⇒ 放行；国庆前(9/30 gap=7)上限降到 0.5×（50 万）⇒ 拒
    ok_normal, _ = check_limit(legs, eq, key="RB", new_notional=100_000, day="2026-10-21")
    ok_holi, why_holi = check_limit(legs, eq, key="RB", new_notional=100_000, day="2026-09-30")
    assert ok_normal is True, "普通日应放行"
    assert ok_holi is False and "降级" in why_holi, (ok_holi, why_holi)
    print("  6 长假前降级收紧 OK")

    # 7 ★降级阈值 gap>3：元旦(4天)要降级、清明(3天)不降级 —— 与强平阈值(5)不同
    assert is_downgrade_day("2025-12-31") is True, "元旦 4 天应触发降级"
    assert is_downgrade_day("2026-04-03") is False, "清明 3 天不应降级"
    assert is_downgrade_day("2026-02-13") is True
    print("  7 降级阈值 gap≥4 OK")

    # 8 权益缺失放行（不误杀）
    assert check_limit(legs, 0, key="RB", new_notional=9e9)[0] is True
    assert check_limit(legs, None, key="RB", new_notional=9e9)[0] is True
    print("  8 权益缺失放行 OK")

    # 9 快照落盘 + 幂等
    #   ★ 2026-10-22 修：这里原本固定传 "20261021"，而 snapshot 内部的 ts 取**当前**
    #   日期 ⇒ 新增的「ts 自然日必须等于 day」校验会判定不合法并重写，第二次就不是
    #   幂等命中了。改用真实当天日期，让 ts 与 day 天然一致。
    d = tempfile.mkdtemp(prefix="ovn_")
    _today = _time.strftime("%Y%m%d")
    ok, note = snapshot(d, _today, legs, eq, margin=300_000)
    assert ok, note
    ok2, note2 = snapshot(d, _today, legs, eq, margin=300_000)
    assert ok2 is False and "已快照" in note2, (ok2, note2)
    with open(os.path.join(d, SNAP_JSON), encoding="utf-8") as f:
        doc = json.load(f)
    assert doc["trade_day"] == _today and len(doc["legs"]) == 2, doc
    assert abs(doc["total_ratio"] - 0.408) < 0.01, doc["total_ratio"]
    print("  9 快照落盘与幂等 OK")

    # 9b ★ 提前消费必须重写：ts 落在前一自然日、却标成今天（2026-10-21 20:00:37 事故）
    d0 = tempfile.mkdtemp(prefix="ovn_")
    with open(os.path.join(d0, SNAP_JSON), "w", encoding="utf-8") as f:
        json.dump({"ts": "2000-01-01 20:00:37", "trade_day": _today,
                   "legs": [], "total_notional": 0.0}, f)
    ok3, note3 = snapshot(d0, _today, legs, eq)
    assert ok3 is True, f"ts 与 day 日期不符时必须重写，却返回 {ok3}/{note3}"
    print("  9b 提前消费的快照被重写 OK")

    # 10 空仓也能快照（写一行"空仓"，供连续性统计）
    d2 = tempfile.mkdtemp(prefix="ovn_")
    ok, note = snapshot(d2, _today, [], eq)
    assert ok, note
    print("  10 空仓快照 OK")

    # 11 异常不抛
    assert snapshot(None, "20261021", legs, eq)[0] is False
    assert check_limit("坏数据", eq, key="M")[0] is True
    print("  11 异常不抛 OK")

    # 12 ★nan 权益：tqsdk 账户字段默认是 nan（objs.py 源码），不能当 0，也不能算
    #    出 NaN 限额后静默放行；更要防止 json.dump 写出非法的 NaN 字面量。
    nan = float("nan")
    assert check_limit(legs, nan, key="RB", new_notional=9e9)[0] is True, \
        "nan 权益必须按「未知 ⇒ 放行」处理，不能误拦"
    d2 = tempfile.mkdtemp(prefix="ovn_nan_")
    ok, note = snapshot(d2, "20261021", legs, nan)
    assert ok, note
    with open(os.path.join(d2, SNAP_JSON), encoding="utf-8") as f:
        doc2 = json.load(f)
    assert doc2["equity"] == 0.0, f"nan 必须归一成 0，实际 {doc2['equity']}"
    # 严格 JSON（allow_nan=False）必须能序列化 —— 否则下游解析器会炸
    json.dumps(doc2, ensure_ascii=False, allow_nan=False)
    print("  12 nan 权益归一 OK")

    print("[selftest] 全部通过")


if __name__ == "__main__":
    selftest()
