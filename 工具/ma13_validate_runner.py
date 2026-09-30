# -*- coding: utf-8 -*-
"""MA13 验证 runner：单品种跑 4 个窗口 + 1 个成本压测，输出 JSON 一行。
供 ma13_validate_2345.py 以子进程方式调用（每品种一个进程，隔离 tqsdk 会话）。
"""
import sys, os, json, importlib.util
import pandas as pd

ROOT = r"C:/Users/cc/Desktop/天勤量化"

# 窗口：(标签, 起始, 结束)
WINDOWS = [
    ("dev",     "2021-01-01", "2023-12-31"),   # ③ 开发窗（样本内基线）
    ("oos",     "2024-01-01", "2025-12-31"),   # ② 样本外纯净验证（从未调参）
    ("trend20", "2020-01-01", "2020-12-31"),   # ③ 趋势年体制
    ("death",   "2016-01-01", "2019-12-31"),   # ③ 死亡窗体制
]

KEYS = ["total_return", "max_drawdown", "win_rate", "sharpe",
        "sharpe_11500", "final_equity", "trades", "blown", "commission", "SLIP"]


def load_module(path):
    spec = importlib.util.spec_from_file_location("ma13mod", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def run(m, s, e):
    out, *_ = m.run_backtest(pd.Timestamp(s), pd.Timestamp(e))
    return {k: out.get(k) for k in KEYS}


def main():
    script = sys.argv[1]
    var = os.path.basename(os.path.dirname(script))
    m = load_module(script)
    results = {}
    for tag, s, e in WINDOWS:
        try:
            results[tag] = run(m, s, e)
        except Exception as ex:
            results[tag] = {"error": str(ex)[:240]}
    # ④ 成本压测：开发窗抬滑点到 3 跳
    try:
        m.SLIP = 3
        r = run(m, "2021-01-01", "2023-12-31")
        m.SLIP = 1
        results["dev_slip3"] = r
    except Exception as ex:
        results["dev_slip3"] = {"error": str(ex)[:240]}
    print(json.dumps({"variety": var, "script": script, "results": results},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
