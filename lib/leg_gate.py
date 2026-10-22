# -*- coding: utf-8 -*-
"""
组合腿数闸门（#18，D2 2026-10-21 拍板）
==========================================================
【为什么需要】
回测器 `lib/portfolio.py` 里「最大同时持仓腿数 K」是硬闸门（§7.7 规格），
但**实盘 `ma13_live_multi.py` 从来没实现过**（grep「腿数/MAX_LEGS」零命中）。
⇒ 回测里成立的组合约束，实盘里不存在 = 口径裂缝。

【D2 已拍死的三个口径（不得自行改动）】
  18.1 K 沿用 **6**（与回测 §7.7 一致，便于口径统一）。
       ⚠️ **条件**：当前池恰好 6 品种 ⇒ K=6 与池上限**数值等价**，今日**永远不触发**。
          这不是"低风险"，是"零作用"。它的价值只在**扩池/换池**时显现。
          一旦扩池，K 必须重新定值，**不得沿用 6**。（`warn_if_inert()` 会在
          启动时把这句话打到日志里，防止后人误以为闸门在保护什么。）
  18.2 口径 = **账户真实持仓品种数**（不是委托数、不含在途单）。
       避开在途单的瞬时态导致误挡正常开仓。
  18.3 换池双份算 **1 腿**（按品种聚合）。
       换月/换池时新旧合约各持一份是**同一标的的时间分布**，不是两个独立风险源。
       按品种聚合计 1 腿，避免换池时闸门误挡正常移仓。

【数据来源】
`shared`（`run_live_multi` 里的各腿共享标量字典，`shared[key] = {pos, price, tgt, dt}`）。
`_process` 末尾会 `sh.update({"pos": s["pos"], ...})` **立即回写**，
所以同一轮 specs 循环里前一腿开仓后，后一腿查询时已能看到 ⇒ 不会少算一腿。

【只挡开仓，绝不挡平仓】
与全线惯例一致：`_risk_check` 只在开仓路径调用；平仓/止损/撤单永不经过这里。

【自检】
    python -c "from lib import leg_gate as g; g.selftest()"
"""

# ===================== 常量（D2 拍板，改动即改变组合风控行为）=====================
MAX_LEGS = 6            # 18.1：沿用回测 §7.7。扩池必须重定值，不得沿用。


def count_legs(shared, exclude=None):
    """账户真实持仓**品种数**（18.2）。exclude 用于排除查询者自身。"""
    n = 0
    keys = []
    for key, sh in (shared or {}).items():
        try:
            if exclude is not None and str(key).upper() == str(exclude).upper():
                continue
            if int(sh.get("pos") or 0) != 0:
                n += 1
                keys.append(str(key))
        except Exception:
            continue
    return n, keys


def check_open(shared, key=None, max_legs=None):
    """开仓前查腿数。返回 (ok: bool, reason: str)。

    异常一律放行 —— 与 `_risk_check` 的既有约定一致：
    宁可漏挡一次，也不能因为一个 getattr 异常把正常开仓全掐死。
    """
    try:
        K = int(max_legs if max_legs is not None else MAX_LEGS)
        if K <= 0:
            return True, ""
        n, keys = count_legs(shared, exclude=key)
        # ★ exclude=key：本品种自己已有仓时不算"新增一腿"（反手/加仓不是新风险源）。
        #   实盘开仓路径只在 s["pos"]==0 时进入，这里是防御性处理。
        if n >= K:
            return False, (f"组合腿数 {n} ≥ 上限 {K}（在场：{','.join(keys)}）"
                           f" ⇒ 拒绝新开 {key}")
        return True, ""
    except Exception:
        return True, ""


def warn_if_inert(pool_keys, max_legs=None):
    """18.1 的条件：池规模 ≤ K 时明确写一行日志，防止后人误判闸门在起作用。

    返回提示文本（调用方负责打印）；池 > K 时返回空串。
    """
    try:
        K = int(max_legs if max_legs is not None else MAX_LEGS)
        n = len({str(k).upper() for k in (pool_keys or [])})
        if n <= 0:
            return ""
        if n <= K:
            return (f"[腿数闸门] 池 {n} 品种 ≤ K={K} ⇒ **今日零触发属预期**，"
                    f"闸门只对扩池/换池生效。⚠️ 一旦扩池，K 必须重新定值，不得沿用 {K}。")
        return f"[腿数闸门] 池 {n} 品种 > K={K} ⇒ 闸门**实际生效**，超限将拒绝新开仓。"
    except Exception:
        return ""


def selftest():
    # 1 空仓 ⇒ 0 腿、放行
    sh = {"M": {"pos": 0}, "SA": {"pos": 0}}
    n, _ = count_legs(sh)
    assert n == 0, n
    assert check_open(sh, "M")[0] is True
    print("  1 空仓放行 OK")

    # 2 达 K 后拒开
    sh = {k: {"pos": 8} for k in ("M", "SA", "MA", "FU", "BU", "EG")}
    n, keys = count_legs(sh)
    assert n == 6 and len(keys) == 6, (n, keys)
    ok, why = check_open(sh, "RB")
    assert ok is False and "6" in why, (ok, why)
    print("  2 达 K 拒开 OK")

    # 3 未达 K 放行（5 腿）
    sh.pop("EG")
    assert check_open(sh, "RB")[0] is True
    print("  3 未达 K 放行 OK")

    # 4 多空都计（腿数是风险源数，不分方向）
    sh = {"M": {"pos": 8}, "SA": {"pos": -8}}
    assert count_legs(sh)[0] == 2
    print("  4 多空都计 OK")

    # 5 ★18.3 换池双份按品种聚合 —— 同一 key 只算 1 腿
    sh = {"M": {"pos": 8}}          # key 是品种（M），新旧合约都归到 M
    assert count_legs(sh)[0] == 1
    print("  5 换池双份=1腿 OK")

    # 6 exclude：自己已有仓不算新增腿
    sh = {k: {"pos": 8} for k in ("M", "SA", "MA", "FU", "BU", "EG")}
    assert check_open(sh, "M")[0] is True, "自己已有仓时不该被自己挡掉"
    print("  6 exclude 自身 OK")

    # 7 异常数据不炸、且放行
    sh = {"M": {"pos": "坏数据"}, "SA": None}
    ok, why = check_open(sh, "RB")
    assert ok is True, (ok, why)
    print("  7 坏数据放行不炸 OK")

    # 8 warn_if_inert：池=K 必须提示零触发
    msg = warn_if_inert(["MA", "JM", "EB", "FU", "M", "EG"])
    assert "零触发" in msg, msg
    print("  8 池=K 提示零触发 OK")

    # 9 池 > K 提示实际生效
    msg = warn_if_inert(["MA", "JM", "EB", "FU", "M", "EG", "RB"])
    assert "实际生效" in msg, msg
    print("  9 扩池提示生效 OK")

    # 10 大小写不敏感（symbol key 来源不统一）
    sh = {"m": {"pos": 8}}
    assert count_legs(sh)[0] == 1
    assert check_open(sh, "M")[0] is True     # exclude 命中 m
    print("  10 大小写不敏感 OK")

    print("[selftest] 全部通过")


if __name__ == "__main__":
    selftest()
