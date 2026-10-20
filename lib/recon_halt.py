# -*- coding: utf-8 -*-
"""
对账偏离硬停机判定（P0-2）—— **纯逻辑，不 import tqsdk**
================================================================================
问题（任务书 §二 P0-2）
--------------------------------------------------------------
云端对账发现「本地以为平了、云端还有仓」（或反之）时，原实现只写一条 FILLX
日志、**不停交易** ⇒ 后续会在这个错误的账本上继续开仓（仓位翻倍/裸奔）。
2026-10-19 FU 那次就是反面教材：废单 + 本地误判平，靠 RECOVER 兜底。

本模块只回答一个问题：**这次对账偏离，该不该锁「只出不进」？**
（真正的锁动作、弹窗、落盘都在主文件里做，这里只做判定，便于离线复算。）

四种结论
--------------------------------------------------------------
  · `ok`     —— 账实一致，什么都不做
  · `skip`   —— 云端读不到（未订阅/连接未就绪），不下结论
  · `wait`   —— **过渡态**，本拍不下结论：本进程有在途委托 / 换月移交进行中
                 （任务书明令排除，否则会把自己刚开的仓误判成外部持仓而误锁）
  · `adopt`  —— 本会话**首次**对账（`sync_base=False`）：按历史行为接管/清零
  · `clear`  —— 同上，云端已无仓而本地以为有仓
  · `halt`   —— 非首次对账、无在途委托、无换月 ⇒ 账实不符，锁「只出不进」

阈值（任务书「建议净仓差 ≥1 手 或 金额超权益 1%」）
--------------------------------------------------------------
净仓是整数手，故任何非零差额都 ≥1 手 ⇒ 实际上**任何无法解释的偏离都会锁**。
`HALT_MIN_RATIO` 保留为「非整数手差额」的备用门槛，也让口径能复算成金额。
判平从严：宁锁一次要人工删文件，也不在错账上继续开仓。

跑法（离线单测，纯标准库）：
    python -c "from lib import recon_halt; recon_halt.selftest()"
"""

# ===================== 常量（任务书给出，改动即改风控口径）=====================
HALT_MIN_LOTS = 1        # 净仓差 ≥ 1 手 ⇒ 锁
HALT_MIN_RATIO = 0.01    # 或 偏离金额 ≥ 权益 1% ⇒ 锁


def plan(local_pos, cloud_pos, sync_base=False, pending_orders=0,
         rollover_active=False, equity=None, price=None, mult=None,
         min_lots=HALT_MIN_LOTS, min_ratio=HALT_MIN_RATIO):
    """判定本次对账偏离的处置。返回 (action, reason)。

    参数
    ----
    local_pos        : 本地记录的净仓（多正空负）
    cloud_pos        : 云端净仓；None/NaN = 读不到
    sync_base        : 本会话是否**已经**完成过一次对账（False = 首次，按接管处理）
    pending_orders   : 本进程在途委托数（`len(s["pending_fill"])`）
    rollover_active  : 换月移交进行中（`rollover_pending` 或 held_sym≠主连）
    equity/price/mult: 仅用于把差额折算成金额占比（缺任一项则只按手数判）
    """
    # 1) 云端读不到 ⇒ 不下结论（未订阅/连接未就绪）
    try:
        if cloud_pos is None:
            return "skip", "云端持仓读不到"
        cp = int(cloud_pos)
    except Exception:
        return "skip", f"云端持仓无法解析：{cloud_pos!r}"
    try:
        lp = int(local_pos or 0)
    except Exception:
        lp = 0

    # 2) 一致
    if lp == cp:
        return "ok", f"账实一致：{lp}"

    # 3) 过渡态：本进程有在途委托 / 换月移交中 ⇒ 本拍不下结论
    #    ⚠️ 这两条是任务书明令的排除项，去掉会把自己刚开的仓/换月平仓误判成偏离
    if pending_orders > 0:
        return "wait", f"本进程有 {int(pending_orders)} 张在途委托未确认，暂不判定"
    if rollover_active:
        return "wait", "换月移交进行中（换月平仓单可能仍在途），暂不判定"

    # 4) 本会话首次对账 ⇒ 历史行为（boot 接管 / 云端清零）
    if not sync_base:
        return ("adopt" if cp != 0 else "clear",
                f"本会话首次对账：本地={lp} 云端={cp} ⇒ 按接管处理（不锁）")

    # 5) 非首次 + 无在途 + 无换月 ⇒ 账实不符
    diff = cp - lp
    lots = abs(diff)
    amt_ratio = None
    try:
        if equity and price and mult:
            amt_ratio = (lots * float(price) * float(mult)) / float(equity)
    except Exception:
        amt_ratio = None
    hit = (lots >= int(min_lots)) or (amt_ratio is not None and amt_ratio >= float(min_ratio))
    if not hit:
        return "ok", f"偏离未达阈值：{lots} 手 / {amt_ratio}"
    extra = f"，偏离金额≈权益的 {amt_ratio:.2%}" if amt_ratio is not None else "，偏离金额未知"
    return "halt", (f"账实不符：本地={lp} 云端={cp}（差 {diff:+d} 手{extra}）"
                    f" ⇒ 锁「只出不进」，需人工核对后删 熔断状态.json 复位")


# ---------------------------------------------------------------- 自检
def selftest():
    """离线自检：六种结论各来一发。纯标准库。"""
    print("[selftest] recon_halt.py（纯逻辑，无网络）")

    # 1) 读不到
    a, why = plan(0, None)
    assert a == "skip", (a, why)
    print("  1 云端读不到 ⇒ skip OK")

    # 2) 一致
    assert plan(8, 8, sync_base=True)[0] == "ok"
    assert plan(0, 0)[0] == "ok"
    print("  2 账实一致 ⇒ ok OK")

    # 3) 在途委托 ⇒ wait（绝不锁）
    a, why = plan(0, 8, sync_base=True, pending_orders=1)
    assert a == "wait", (a, why)
    print("  3 在途委托 ⇒ wait（不误锁）OK")

    # 4) 换月过渡 ⇒ wait
    a, why = plan(0, 8, sync_base=True, rollover_active=True)
    assert a == "wait", (a, why)
    print("  4 换月过渡 ⇒ wait（不误锁）OK")

    # 5) 首次对账 ⇒ adopt/clear（历史行为，不锁）
    assert plan(0, 8)[0] == "adopt"
    assert plan(8, 0)[0] == "clear"
    print("  5 首次对账 ⇒ adopt/clear（重启接管不误锁）OK")

    # 6) 非首次、无在途、无换月 ⇒ halt（两个方向都锁）
    a, why = plan(0, 8, sync_base=True, equity=1_000_000.0, price=3000.0, mult=10.0)
    assert a == "halt", (a, why)
    assert "只出不进" in why and "差 +8 手" in why, why
    a2, why2 = plan(8, 0, sync_base=True, equity=1_000_000.0, price=3000.0, mult=10.0)
    assert a2 == "halt", (a2, why2)
    print("  6 账实不符（双向）⇒ halt OK")

    # 7) 金额占比算得出来（8 手 × 3000 × 10 ÷ 100 万 = 24%）
    a, why = plan(0, 8, sync_base=True, equity=1_000_000.0, price=3000.0, mult=10.0)
    assert "24.00%" in why, why
    print("  7 偏离金额占比可复算 OK")

    # 8) 手数门槛可配（用它来验证「≥1 手」这一条不是写死的巧合）
    assert plan(0, 1, sync_base=True, min_lots=2)[0] == "ok"
    assert plan(0, 2, sync_base=True, min_lots=2)[0] == "halt"
    print("  8 门槛可配（≥1 手语义正确）OK")

    print("[selftest] 全部通过")
    return True


if __name__ == "__main__":
    selftest()
