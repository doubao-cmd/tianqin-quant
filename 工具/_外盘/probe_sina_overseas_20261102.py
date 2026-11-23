# -*- coding: utf-8 -*-
"""
探测新浪外盘日线接口：哪些符号能取、历史多长、字段质量
接口：GlobalFuturesService.getGlobalFuturesDailyKLine?symbol=XX
"""
import urllib.request, json, re, ssl, time
ssl._create_default_https_context = ssl._create_unverified_context

SYMS = {
    # 能源
    "CL": "NYMEX原油", "OIL": "布伦特原油", "NG": "NYMEX天然气", "HO": "取暖油", "RB": "RBOB汽油",
    # 金属
    "GC": "COMEX黄金", "SI": "COMEX白银", "HG": "COMEX铜", "PL": "铂", "PA": "钯",
    "CAD": "LME铜", "AHD": "LME铝", "ZSD": "LME锌", "NID": "LME镍", "PBD": "LME铅",
    # 农产品
    "S": "CBOT大豆", "SM": "CBOT豆粕", "BO": "CBOT豆油", "C": "CBOT玉米", "W": "CBOT小麦",
    "CT": "ICE棉花", "SB": "ICE原糖", "KC": "ICE咖啡", "CC": "ICE可可", "OJ": "橙汁",
    "LH": "CME瘦肉猪", "FC": "CME活牛",
    # 亚洲
    "JRU": "TOCOM橡胶", "FCPO": "马棕油", "NK": "日经225",
}

def fetch(sym):
    u = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/"
         f"var%20_{sym}=/GlobalFuturesService.getGlobalFuturesDailyKLine?symbol={sym}")
    req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0",
                                            "Referer": "https://finance.sina.com.cn"})
    raw = urllib.request.urlopen(req, timeout=15).read().decode("gbk", errors="ignore")
    m = re.search(r"=\s*\((\[.*\])\)", raw, re.S)
    if not m:
        return None, raw[:80]
    return json.loads(m.group(1)), None

print("=" * 86)
print(f"{'代码':6s} {'名称':12s} {'根数':>6s} {'起':>12s} {'止':>12s} {'末收':>10s} {'备注'}")
print("=" * 86)
ok = []
for sym, name in SYMS.items():
    try:
        rows, err = fetch(sym)
        if rows is None:
            print(f"{sym:6s} {name:12s} {'--':>6s} {'--':>12s} {'--':>12s} {'--':>10s} 无数据")
            continue
        rows = [r for r in rows if r.get("close") not in (None, "", "0", 0)]
        if not rows:
            print(f"{sym:6s} {name:12s} {'0':>6s} {'--':>12s} {'--':>12s} {'--':>10s} 全为0")
            continue
        n = len(rows)
        t0, t1 = rows[0]["date"], rows[-1]["date"]
        last = float(rows[-1]["close"])
        vol0 = sum(1 for r in rows if str(r.get("volume")) in ("0", "0.0", ""))
        note = f"零成交{vol0}天" if vol0 else ""
        print(f"{sym:6s} {name:12s} {n:>6d} {t0:>12s} {t1:>12s} {last:>10.2f} {note}")
        ok.append((sym, name, n, t0, t1))
    except Exception as e:
        print(f"{sym:6s} {name:12s} {'ERR':>6s} {type(e).__name__}: {str(e)[:40]}")
    time.sleep(0.15)

print("=" * 86)
print(f"可用 {len(ok)} / {len(SYMS)}")
