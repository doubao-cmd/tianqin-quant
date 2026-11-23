# -*- coding: utf-8 -*-
"""
⚠️ 2026-11-23 18:30 起本脚本的方案已作废，**不要用它得出的结论**。

   生产实现已从 `api.query_his_cont_quotes` 改为 `urllib.request` + `timeout=5`
   直读 `files.shinnytech.com/continuous_table.json`。原因：前者内部
   `requests.get(...)` **没有 timeout**（tqsdk/calendar.py），而校准函数跑在
   wait_update 主循环里，一次网络抖动会让主循环永久阻塞 ⇒ 止损不再被监控。

   保留本文件仅为「旧方案行为留档」。现行实现见 ma13_live_multi.py 的
   `_cont_table()` / `_calib_cont()`，其正确性由隔离测试覆盖（6 个分支）。

--------------------------------------------------------------------------------
（以下为作废前的原始说明）

验证 query_his_cont_quotes 能否作为「主连→实际合约」的独立校准源。

⚠️ 必须在**模拟盘停机后**跑：tqsdk 同一快期账户多会话会互踢，
   盘中跑会把 ma13_live_multi 踢下线。

验证项：
  1) 接口能否返回「主连 → 标的合约」，返回几条、字段长什么样
  2) 与 q.underlying_symbol（可能已冻结）是否一致
  3) 单次调用耗时（决定放主循环里会不会卡）
  4) Entity 字段可写性：改写 q.underlying_symbol 后能否读回（C 方案的前提）
  5) n=1 与 n=3 的差异（判断"当天"的语义，夜盘归属哪个交易日）
"""
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, _ROOT)

from tqsdk import TqApi, TqSim, TqAuth  # noqa: E402
from config_account import check_live  # noqa: E402

CONTS = ["KQ.m@DCE.jd", "KQ.m@CZCE.MA", "KQ.m@SHFE.ru",
         "KQ.m@DCE.eg", "KQ.m@DCE.p", "KQ.m@SHFE.fu", "KQ.m@DCE.v"]


def main():
    user, pwd = check_live()
    print(f"用 TqSim（不登录 TqKq 交易账户）连接，账号 {user}")
    api = TqApi(TqSim(), auth=TqAuth(user, pwd))
    try:
        api.wait_update(deadline=time.time() + 10)

        print("\n=== 1) quote 侧现状 ===")
        qs = {}
        for c in CONTS:
            q = api.get_quote(c)
            qs[c] = q
            print(f"  {c:<16} underlying={getattr(q, 'underlying_symbol', None)!r} "
                  f"last={q.last_price}")

        print("\n=== 2) query_his_cont_quotes(n=1) ===")
        t0 = time.time()
        try:
            df1 = api.query_his_cont_quotes(symbol=CONTS, n=1)
            cost = time.time() - t0
            print(f"  耗时 {cost * 1000:.0f} ms")
            print(df1.to_string(index=False))
        except Exception as e:
            print(f"  ✗ 失败：{type(e).__name__}: {e}")
            df1 = None

        print("\n=== 3) query_his_cont_quotes(n=3)（看 '当天' 语义）===")
        try:
            df3 = api.query_his_cont_quotes(symbol=CONTS, n=3)
            print(df3.to_string(index=False))
        except Exception as e:
            print(f"  ✗ 失败：{type(e).__name__}: {e}")
            df3 = None

        print("\n=== 4) 一致性 + 字段可写性 ===")
        if df1 is not None and len(df1):
            for c in CONTS:
                if c not in df1.columns:
                    continue
                need = str(df1[c].iloc[-1] or "").strip()
                q = qs[c]
                cur = str(getattr(q, "underlying_symbol", "") or "")
                flag = "一致" if need == cur else "★不一致"
                print(f"  {c:<16} 日历={need:<14} quote={cur:<14} {flag}")
            # 写回测试：拿第一个不一致的（全一致就拿第一个）试写
            c0 = CONTS[0]
            old = str(getattr(qs[c0], "underlying_symbol", "") or "")
            probe = f"{old}_PROBE" if old else "PROBE"
            qs[c0].underlying_symbol = probe
            back = getattr(qs[c0], "underlying_symbol", None)
            print(f"  写回测试：{old!r} → {probe!r} → 读回 {back!r} "
                  f"{'✓ 可写' if back == probe else '✗ 不可写'}")
            qs[c0].underlying_symbol = old      # 复原

            # ---- 5) 模拟「quote 冻结」→ 用日历值纠正 ----
            print("\n=== 5) 模拟冻结修复（C 方案的核心前提）===")
            c1 = next((c for c in CONTS
                       if c in df1.columns
                       and str(df1[c].iloc[-1]).strip()
                       and str(df1[c].iloc[-1]).strip()
                       != str(getattr(qs[c], "underlying_symbol", "") or "")),
                      CONTS[0])
            need1 = str(df1[c1].iloc[-1]).strip()
            frozen = "DCE.jd2612" if "jd" in c1.lower() else "FROZEN.OLD0001"
            qs[c1].underlying_symbol = frozen
            print(f"  {c1}：人为冻结为 {frozen!r}，日历正确值是 {need1!r}")
            # 复刻 _calib_cont 的判定（不导入实盘模块，避免副作用）
            cur = str(getattr(qs[c1], "underlying_symbol", "") or "")
            ok = False
            if need1 and need1 != cur:
                a = "".join(ch for ch in cur if ch.isdigit())
                b = "".join(ch for ch in need1 if ch.isdigit())
                if not (a.isdigit() and b.isdigit() and int(b) <= int(a)):
                    qs[c1].underlying_symbol = need1
                    ok = True
            back = str(getattr(qs[c1], "underlying_symbol", "") or "")
            print(f"  校准后读回 {back!r} → "
                  f"{'✓ 已纠正' if ok and back == need1 else '✗ 未纠正'}")
        else:
            print("  跳过（无查询结果）")
    finally:
        api.close()
        print("\n已断开。")


if __name__ == "__main__":
    main()
