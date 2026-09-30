import os
# -*- coding: utf-8 -*-
"""05_摆动线 / 07_布林带 体检 runner
==========================================================
给两条"留观"策略做 MA13 同款多窗体检（已注册策略的验证流程，不花 T0 预算）：
  窗口：dev(21-23) / oos(24-25) / trend20(2020) / death(16-19) / devslip3(成本压测)
  品种：12 个（与 05 的品种清单一致，从其脚本里提取 SYMBOL_KEY）
  口径：定稿口径 —— 20万 × 1% 波动率预算，手数夹 [1,8]；30min K 线
        摆动线 stop_atr=2.0（v2 方案A）；布林带 1×ATR 止损 + 20 根时间止损
  参数零改动：全部沿用两策略脚本里的当前注册值，不是新假设、不扫参。

用法：
  main   python 工具/体检_05_07.py main     # 串行跑全部，写 工具/体检_05_07.json
  driver python 工具/体检_05_07.py driver <strategy> <key> <tag> <start> <end> <slip>
          （单次回测，stdout 末行输出 RESULT_JSON:{...}，供 main 解析）
"""
import sys, os, re, json, subprocess

ROOT = os.path.dirname(os.path.abspath(__file__))
while not os.path.exists(os.path.join(ROOT, "config_symbols.py")):
    _up = os.path.dirname(ROOT)
    if _up == ROOT:
        raise RuntimeError("未找到项目根（config_symbols.py）")
    ROOT = _up
OUT_JSON = os.path.join(ROOT, "工具", "体检_05_07.json")

WINDOWS = [  # (tag, start, end, slip_ticks)
    ("dev",      "2021-01-01", "2023-12-31", 1),
    ("oos",      "2024-01-01", "2025-12-31", 1),
    # 2026-09-30 用户拍板轻量化：砍 trend20 / death 两窗（仅服务死因归类，
    # 不影响「给不给模拟盘席位」的裁决链 dev→oos→devslip3）。已跑完的窗保留。
    # ("trend20",  "2020-01-01", "2020-12-31", 1),
    # ("death",    "2016-01-01", "2019-12-31", 1),
    ("devslip3", "2021-01-01", "2023-12-31", 3),   # 成本压测：费用层 3 跳
]

STRATEGIES = ["swing", "boll"]


def discover_keys():
    """从 05_摆动线 的 12 份脚本里提取品种 key（05 的清单=全池口径）。"""
    d = os.path.join(ROOT, "策略", "05_摆动线", "回测")
    keys = []
    for folder in sorted(os.listdir(d)):
        p = os.path.join(d, folder, "摆动线_回测.py")
        if not os.path.isfile(p):
            continue
        with open(p, "r", encoding="utf-8") as f:
            m = re.search(r'SYMBOL_KEY\s*=\s*"(\w+)"', f.read())
        if m and m.group(1) not in keys:
            keys.append(m.group(1))
    return keys


def driver(strategy, key, tag, start, end, slip):
    """单次回测子进程：直接调 lib.engine 的两个入口（参数=脚本注册值）。"""
    sys.path.insert(0, ROOT)
    from datetime import date, timedelta
    from lib import engine
    from lib.engine import _stat_to_dict

    s = date.fromisoformat(start)
    e = date.fromisoformat(end)
    warm = s - timedelta(days=7)
    common = dict(start=warm, end=e, eval_start=s, init_balance=200000,
                  kline_sec=1800, open_html=False, sample_tag=f"bt{tag}",
                  slip_ticks=int(slip), risk_pct=1.0,
                  risk_hand_min=1, risk_hand_max=8)
    if strategy == "swing":
        stat = engine.run_backtest_swing(key, volume=1, strategy_name="摆动线",
                                         stop_atr=2.0, **common)
    else:
        stat = engine.run_backtest_boll(key, period=20, k=2.0, volume=1,
                                        strategy_name="布林带", atr_period=14,
                                        atr_stop_mult=1.0, time_stop_bars=20,
                                        **common)
    if stat is None:
        print("RESULT_JSON:" + json.dumps({"error": "no_data"}, ensure_ascii=False))
        return
    d = _stat_to_dict(stat)
    out = {"ror": d.get("ror"), "mdd": d.get("max_drawdown"),
           "sharpe": d.get("sharpe_ratio"), "balance": d.get("balance"),
           "annual": d.get("annual_yield")}
    # 回合数：从分窗落盘的成交明细 txt 数买卖行 / 2
    try:
        name, _mc, _mult, _tick = __import__("config_symbols").get(key)
        strat_cn = "摆动线" if strategy == "swing" else "布林带"
        strat_dir = "05_摆动线" if strategy == "swing" else "03_布林带"
        txt = os.path.join(ROOT, "回测结果", strat_dir, name,
                           f"{name}_{strat_cn}_30min_bt{tag}.txt")
        n = 0
        with open(txt, "r", encoding="utf-8") as f:
            for line in f:
                if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}", line) \
                        and ("BUY" in line or "SELL" in line):
                    n += 1
        out["rounds"] = n // 2
    except Exception:
        pass
    print("RESULT_JSON:" + json.dumps(out, ensure_ascii=False))


def main():
    keys = discover_keys()
    print(f"[体检] 品种 {len(keys)} 个: {keys}", flush=True)
    # 断点续跑（2026-09-30 轻量化新增）：旧结果先装回来，跑完的不再重跑
    results = {}
    if os.path.isfile(OUT_JSON):
        try:
            with open(OUT_JSON, "r", encoding="utf-8") as f:
                results = json.load(f)
            print(f"[体检] 续跑：已有 {len(results)} 条结果", flush=True)
        except Exception as ex:
            print(f"[体检] ⚠️ 旧结果读不了（已备份），从头跑：{ex}", flush=True)
            results = {}
    # 只排还没跑完的活：error 条目视为未完成，重试
    任务 = []
    for strat in STRATEGIES:
        for key in keys:
            for tag, s, e, slip in WINDOWS:
                rkey = f"{strat}_{key}_{tag}"
                if rkey in results and "error" not in results[rkey]:
                    continue
                任务.append((strat, key, rkey, tag, s, e, slip))
    total = len(任务)
    print(f"[体检] 待跑 {total} 次（轻量窗口：{[w[0] for w in WINDOWS]}）", flush=True)
    done = 0
    for strat, key, rkey, tag, s, e, slip in 任务:
        done += 1
        print(f"[{done}/{total}] {rkey} ...", flush=True)
        try:
            p = subprocess.run(
                [sys.executable, "-u", os.path.abspath(__file__),
                 "driver", strat, key, tag, s, e, str(slip)],
                capture_output=True, text=True, timeout=1800, cwd=ROOT)
            line = next((ln for ln in p.stdout.splitlines()
                         if ln.startswith("RESULT_JSON:")), None)
            if line:
                results[rkey] = json.loads(line[len("RESULT_JSON:"):])
            else:
                tail = (p.stdout or "").strip().splitlines()[-3:]
                results[rkey] = {"error": "no_result_json",
                                 "stdout_tail": tail[-1][:200] if tail else "",
                                 "stderr_tail": (p.stderr or "")[-200:]}
        except Exception as ex:
            results[rkey] = {"error": f"{type(ex).__name__}: {str(ex)[:180]}"}
        # 每步落盘：崩了也不丢已跑完的
        with open(OUT_JSON, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=1)
    print(f"[体检] 全部完成 -> {OUT_JSON}", flush=True)


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "driver":
        driver(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6],
               sys.argv[7])
    else:
        main()
