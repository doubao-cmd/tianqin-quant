# -*- coding: utf-8 -*-
"""
【只读探测 · 一次性】12 个品种的「具体合约保证金」是否全部可得？
==========================================================
上一轮已确认：主连 KQ.m@ 的 quote.margin = nan，
但主连背后的真实月份合约（quote.underlying_symbol）的 margin 有值。
本轮要回答两件事：
  Q1  12 个品种（含郑商所 CZCE / 上期所 SHFE / 大商所 DCE）是否**全部**取得到？
  Q2  margin 在回测推进过程中**会不会变**？（若恒定，则「动态」的意义只在换月时）

不写盘、不下单、不改任何配置。
"""
import sys
import os

_ROOT = os.path.dirname(os.path.abspath(__file__))
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from datetime import date
import config_account
import config_symbols
from tqsdk import TqApi, TqAuth, TqBacktest

USER, PWD = config_account.check()

START = date(2026, 3, 2)
END = date(2026, 9, 3)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def _ok(x):
    return x == x  # 非 NaN


def main():
    print("=" * 104)
    print(f"  12 品种「具体合约保证金」可得性 + 时变性 | {START} ~ {END}")
    print("=" * 104)

    api = TqApi(backtest=TqBacktest(start_dt=START, end_dt=END),
                auth=TqAuth(USER, PWD), web_gui=False)
    try:
        mains = {}
        for key, (cn, main, mult, tick) in sorted(config_symbols.SYMBOLS.items()):
            mains[key] = api.get_quote(main)
        kl = api.get_kline_serial("KQ.m@DCE.v", 1800, 40)  # 只用来判断回测推进到哪天

        for _ in range(6):
            api.wait_update()

        # ---- 第一组：起点 ----
        snap1 = {}
        for key, q in mains.items():
            und = getattr(q, "underlying_symbol", None)
            row = {"und": und, "mg": None, "vm": None, "px": None, "err": ""}
            try:
                q2 = api.get_quote(und)
                api.wait_update()
                row["mg"] = _f(getattr(q2, "margin", float("nan")))
                row["vm"] = _f(getattr(q2, "volume_multiple", float("nan")))
                row["px"] = _f(getattr(q2, "last_price", float("nan")))
            except Exception as e:
                row["err"] = f"{type(e).__name__}: {e}"
            snap1[key] = row

        t1 = str(kl.iloc[-1]["datetime"])[:10]

        # ---- 推进到接近回测中点 ----
        target = date(2026, 6, 1)
        n = 0
        while n < 6000:
            api.wait_update()
            n += 1
            try:
                if str(kl.iloc[-1]["datetime"])[:10] >= str(target):
                    break
            except Exception:
                pass

        snap2 = {}
        for key, q in mains.items():
            und = getattr(q, "underlying_symbol", None)
            row = {"und": und, "mg": None, "vm": None, "px": None, "err": ""}
            try:
                q2 = api.get_quote(und)
                api.wait_update()
                row["mg"] = _f(getattr(q2, "margin", float("nan")))
                row["vm"] = _f(getattr(q2, "volume_multiple", float("nan")))
                row["px"] = _f(getattr(q2, "last_price", float("nan")))
            except Exception as e:
                row["err"] = f"{type(e).__name__}: {e}"
            snap2[key] = row

        t2 = str(kl.iloc[-1]["datetime"])[:10]

        # ---- 汇报 ----
        print(f"\n【第一组】{t1}")
        hdr = (f"{'品种':<8}{'交易所':<7}{'具体合约':<14}{'一手名义':>11}"
               f"{'每手保证金':>12}{'保证金率':>10}{'乘数核对':>10}")
        print(hdr)
        print("-" * 104)
        ok_cnt = 0
        for key, (cn, main, mult_cfg, tick_cfg) in sorted(config_symbols.SYMBOLS.items(),
                                                          key=lambda kv: kv[0]):
            r = snap1[key]
            exch = main.split(".")[0].replace("KQ.m@", "")
            notional = r["px"] * r["vm"] if (_ok(r["px"]) and _ok(r["vm"])) else float("nan")
            ratio = (r["mg"] / notional * 100.0) if (_ok(notional) and notional > 0
                                                     and _ok(r["mg"])) else float("nan")
            vm_chk = "OK" if (_ok(r["vm"]) and abs(r["vm"] - mult_cfg) < 1e-6) else "⚠不一致"
            if _ok(r["mg"]) and r["mg"] > 0:
                ok_cnt += 1
            else:
                vm_chk = r["err"][:20] if r["err"] else vm_chk

            def _s(x, w, p=2):
                return f"{x:>{w}.{p}f}" if _ok(x) else f"{'nan':>{w}}"

            print(f"{cn:<8}{exch:<7}{str(r['und']):<14}{_s(notional,11,0)}"
                  f"{_s(r['mg'],12,1)}{_s(ratio,10,2)}%{vm_chk:>10}")

        print(f"\n  可得：{ok_cnt}/12")

        print(f"\n【第二组】{t2}（推进 {n} 拍后）—— 看 margin 是否变化")
        print(f"{'品种':<8}{'具体合约':<14}{'保证金1':>12}{'保证金2':>12}"
              f"{'变化':>11}{'名义价1':>11}{'名义价2':>11}")
        print("-" * 104)
        changed = 0
        for key, (cn, main, mult_cfg, tick_cfg) in sorted(config_symbols.SYMBOLS.items(),
                                                          key=lambda kv: kv[0]):
            a, b = snap1[key], snap2[key]
            same_und = (a["und"] == b["und"])
            d = (b["mg"] - a["mg"]) if (_ok(a["mg"]) and _ok(b["mg"])) else float("nan")
            if _ok(d) and abs(d) > 1e-6:
                changed += 1
            tag = "" if same_und else f" [换月{a['und']}→{b['und']}]"

            def _s(x, w, p=2):
                return f"{x:>{w}.{p}f}" if _ok(x) else f"{'nan':>{w}}"

            print(f"{cn:<8}{str(a['und']):<14}{_s(a['mg'],12,1)}{_s(b['mg'],12,1)}"
                  f"{_s(d,11,1)}{_s(a['px'],11,0)}{_s(b['px'],11,0)}{tag}")

        print(f"\n  margin 发生变化的品种：{changed}/12")

        print("\n" + "=" * 104)
        print("判读：")
        print("  · 12/12 都拿得到 ⇒ 『动态保证金』可覆盖全品种，无需逐品种配保证金率")
        print("  · 若少数品种 nan  ⇒ 那些品种退化到『价格 × 乘数 × 备用保证金率』")
        print("  · margin 会变      ⇒ 动态名副其实（随价格/交易所调整走）")
        print("  · margin 恒定      ⇒ 仍优于静态配值（至少换月/不同品种是真实值）")
        print("=" * 104)
    finally:
        api.close()


if __name__ == "__main__":
    main()
