# -*- coding: utf-8 -*-
"""
期权 IV 曲面摸底（单日快照）
目的：在动手做任何策略前，先量清楚每个品种的期权链有多少合约、
      有多少是真有成交的、平值附近（|Delta| 0.4~0.6）是否可算 IV。

只读，不产生交易信号。
用法：python probe_iv_surface.py 20261030
"""
import sys, os, re, warnings, json
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed

warnings.filterwarnings("ignore")
import akshare as ak

OUT = os.path.dirname(os.path.abspath(__file__))

CZCE = ["白糖期权", "棉花期权", "甲醇期权", "PTA期权", "菜籽粕期权", "菜籽油期权",
        "花生期权", "对二甲苯期权", "烧碱期权", "纯碱期权", "短纤期权", "锰硅期权",
        "硅铁期权", "尿素期权", "苹果期权", "红枣期权", "玻璃期权", "瓶片期权"]

SHFE = ["原油期权", "铜期权", "铝期权", "锌期权", "铅期权", "螺纹钢期权", "镍期权",
        "锡期权", "氧化铝期权", "黄金期权", "白银期权", "丁二烯橡胶期权", "天胶期权"]


def parse_opt(code):
    """从合约代码解析 品种/到期月/看涨看跌/行权价。
    cu2612C78000 -> (cu, 2612, C, 78000.0)
    SR701C4700   -> (SR, 701, C, 4700.0)
    MA612C2125   -> (MA, 612, C, 2125.0)
    """
    m = re.match(r"^([A-Za-z]+)(\d{3,4})([CP])(\d+(?:\.\d+)?)$", str(code).strip())
    if not m:
        return None
    return m.group(1).upper(), m.group(2), m.group(3), float(m.group(4))


def norm(df, ex):
    """统一成同一套列名。"""
    if df is None or len(df) == 0:
        return None
    d = df.copy()
    if ex == "CZCE":
        ren = {"合约代码": "code", "今收盘": "close", "今结算": "settle",
               "成交量(手)": "vol", "持仓量": "oi", "DELTA": "delta",
               "隐含波动率": "iv"}
    else:
        ren = {"合约代码": "code", "收盘价": "close", "结算价": "settle",
               "成交量": "vol", "持仓量": "oi", "德尔塔": "delta"}
    d = d.rename(columns=ren)
    for c in ["close", "settle", "vol", "oi", "delta", "iv"]:
        if c not in d.columns:
            d[c] = pd.NA
        d[c] = pd.to_numeric(d[c], errors="coerce")
    return d[["code", "close", "settle", "vol", "oi", "delta", "iv"]]


def one(ex, sym, day):
    try:
        raw = (ak.option_hist_czce(symbol=sym, trade_date=day) if ex == "CZCE"
               else ak.option_hist_shfe(symbol=sym, trade_date=day))
    except Exception as e:
        return {"ex": ex, "sym": sym, "err": f"{type(e).__name__}:{str(e)[:60]}"}
    d = norm(raw, ex)
    if d is None or len(d) == 0:
        return {"ex": ex, "sym": sym, "err": "EMPTY"}

    pars = d["code"].map(parse_opt)
    ok = pars.notna()
    d, pars = d[ok].copy(), pars[ok]
    d["prod"] = [p[0] for p in pars]
    d["month"] = [p[1] for p in pars]
    d["cp"] = [p[2] for p in pars]
    d["strike"] = [p[3] for p in pars]

    traded = d["vol"].fillna(0) > 0
    atm = d["delta"].abs().between(0.35, 0.65)
    atm_tr = atm & traded
    exp_iv = d["iv"].notna() if ex == "CZCE" else pd.Series(False, index=d.index)

    return {
        "ex": ex, "sym": sym, "err": "",
        "n_contract": len(d),
        "n_traded": int(traded.sum()),
        "pct_traded": round(100 * traded.mean(), 1),
        "vol_sum": int(d["vol"].fillna(0).sum()),
        "oi_sum": int(d["oi"].fillna(0).sum()),
        "n_atm": int(atm.sum()),
        "n_atm_traded": int(atm_tr.sum()),
        "pct_atm_traded": round(100 * atm_tr.sum() / max(atm.sum(), 1), 1),
        "has_iv_col": bool(exp_iv.any()),
        "n_months": int(d["month"].nunique()),
        "n_strikes": int(d["strike"].nunique()),
    }


def main(day):
    jobs = [("CZCE", s) for s in CZCE] + [("SHFE", s) for s in SHFE]
    rows = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(one, e, s, day): (e, s) for e, s in jobs}
        for f in as_completed(futs):
            rows.append(f.result())
    df = pd.DataFrame(rows).sort_values(["ex", "vol_sum"], ascending=[True, False])
    p = os.path.join(OUT, f"iv_surface_probe_{day}.csv")
    df.to_csv(p, index=False, encoding="utf-8-sig")
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 40)
    print(f"date={day}  saved -> {p}\n")
    print(df.to_string(index=False))
    ok = df[df.err == ""]
    print(f"\n可用 {len(ok)}/{len(df)}；总合约 {ok.n_contract.sum()}，"
          f"有成交 {ok.n_traded.sum()} ({100*ok.n_traded.sum()/max(ok.n_contract.sum(),1):.1f}%)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "20261030")
