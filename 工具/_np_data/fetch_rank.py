# -*- coding: utf-8 -*-
"""
工具/_np_data/fetch_rank.py —— 会员持仓排名 → np_data（D6/D7）

覆盖：SHFE / CZCE / GFEX。DCE 不可得（见 `DCE可行性结论.md`）。

数据是**合约级**（rb2701），不是品种级。所以 `contract` 列必须写实：
这正是清单里没识别出来的「第二类未来函数」的入口——把合约级数据当品种级用，
就要做「今天的主力是谁」这个映射，而那一步本身必须 PIT。

===============================================================================
三个实测坑
===============================================================================
1. **SHFE 每张表多一行 `rank=999` 的合计行**，且它的值**恰好等于前 20 名之和**
   （实测 rb2701：前20 long 合计 1,173,747 = rank999 行的 1,173,747）。
   若同时取 rank<=20 与 rank==999，结果翻倍。本模块取 rank<=20 自己求和，
   并在合计行存在时**断言两者相等**（这是免费的交叉校验，能抓到交易所口径变化）。

2. **CZCE 的数值是带千分位逗号的字符串**（'63,289'）。
   直接 `pd.to_numeric` 依赖 pandas 版本行为，这里显式去逗号。

3. **CZCE 的 key 混着品种级与合约级**：'AP'（symbol=AP，品种级）与 'UR705'（合约级）。
   品种级排名说明该品种当日只有品种级数据可得，此时 contract 写品种代码本身，
   下游必须能从 `contract` 是否含数字判断它是合约还是品种。

===============================================================================
只存「前 20 名合计」，不存会员明细
===============================================================================
理由：非价格因子的常见用法是「持仓集中度/多空比」，这些只需要合计；
而会员明细会引入**会员身份**这一维度，涉及数据口径与体量问题，当前阶段不需要。
需要时从 `_raw/` 的原始快照里重建即可——原始响应已留存，不会丢。
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import pit    # noqa: E402
import store  # noqa: E402

warnings.filterwarnings("ignore")

M_LONG, M_SHORT, M_VOL = "top20_long", "top20_short", "top20_vol"
# SHFE 用 'rank'，其余同理；数值列名三所一致
N_LONG, N_SHORT, N_VOL = "long_open_interest", "short_open_interest", "vol"
RANK_TOTAL = 999          # SHFE 的合计行标记


def _num(v) -> float:
    """'63,289' → 63289.0；空/异常 → nan（不静默变 0）。"""
    if v is None:
        return float("nan")
    s = str(v).replace(",", "").replace(" ", "").strip()
    if s in ("", "-", "--", "nan", "None"):
        return float("nan")
    try:
        return float(s)
    except ValueError:
        return float("nan")


def _variety(sym: str) -> str:
    """合约代码 → 品种代码：'au2702'→'AU'，'MA610'→'MA'，'AP'→'AP'。"""
    import re
    s = str(sym).strip().upper()
    m = re.match(r"^([A-Z]+)", s)
    return m.group(1) if m else s


def _mk(source, date, symbol, contract, metric, value, url=""):
    import math
    if value != value or math.isinf(value):
        return None
    pub, tag = store.resolve_published(source, date)
    iso = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
    return pit.make_row(source, symbol, metric, iso, iso, pub, float(value),
                        contract=str(contract).lower(),
                        raw_url=url or store.RULE_NOTE.get(source, ""),
                        published_src=tag)


def _agg_one(df, source, date, symbol_hint, url="", check_total=False) -> list:
    """一张排名表 → 3 条记录（long/short/vol 的前 20 合计）。"""
    if "rank" not in df.columns:
        raise KeyError(f"{source} {symbol_hint} 无 rank 列：{list(df.columns)}")

    ranks = df["rank"]
    d20 = df[ranks.apply(lambda x: str(x).strip().isdigit() and int(str(x).strip()) <= 20)]
    if len(d20) == 0:
        raise RuntimeError(f"{source} {symbol_hint} 没有任何 rank<=20 的行 —— 拒绝写 0")

    sym = str(symbol_hint)
    if "symbol" in df.columns and len(df):
        s0 = str(df["symbol"].iloc[0]).strip()
        if s0 and s0.lower() not in ("nan", "none"):
            sym = s0

    # ★ symbol 必须是**品种**（AU），contract 才是合约（au2702）。
    #   原始表里有 variety 列就直接用；没有就从合约代码去掉尾部数字。
    #   写错会让下游每次都要自己截字符串，且「按品种聚合」变成不可能。
    if "variety" in df.columns and len(df):
        var = str(df["variety"].iloc[0]).strip()
        var = var.upper() if var and var.lower() not in ("nan", "none") else _variety(sym)
    else:
        var = _variety(sym)

    out = []
    for metric, col in ((M_LONG, N_LONG), (M_SHORT, N_SHORT), (M_VOL, N_VOL)):
        vals = [_num(x) for x in d20[col].tolist()]
        vals = [v for v in vals if v == v]
        if not vals:
            continue
        mine = sum(vals)

        if check_total:
            # ★ 免费交叉校验：rank==999 的合计行必须与「前20之和」一致。
            #   不一致 = 交易所改了口径（比如从 20 名改成 30 名），必须炸掉而不是照抄。
            tot_row = df[ranks.apply(lambda x: str(x).strip() == str(RANK_TOTAL))]
            if len(tot_row):
                official = _num(tot_row[col].iloc[0])
                if official == official and abs(official - mine) > 1e-6:
                    raise ValueError(
                        f"{source} {sym} {col}: 前20合计={mine} ≠ 官方合计行={official}\n"
                        f"  两者本应相等 ⇒ 交易所名次口径可能变了（20→N 名），"
                        f"不能当作「数据正常」继续写。")
        out.append(_mk(source, date, var, sym, metric, mine, url))
    return [r for r in out if r]


# ---------------------------------------------------------------------------
# 各所
# ---------------------------------------------------------------------------
def _fetch_shfe(date: str) -> list:
    import akshare as ak
    obj = ak.get_shfe_rank_table(date=date)
    store.save_raw(date, "shfe_rank", store.df_to_records(obj))
    rows = []
    for contract, df in obj.items():
        rows += _agg_one(df, "shfe_rank", date, contract, check_total=True)
    return rows


def _fetch_czce(date: str) -> list:
    import akshare as ak
    obj = ak.get_rank_table_czce(date=date)
    store.save_raw(date, "czce_rank", store.df_to_records(obj))
    rows = []
    for key, df in obj.items():
        rows += _agg_one(df, "czce_rank", date, key, check_total=False)   # 无 999 行
    return rows


def _fetch_gfex(date: str) -> list:
    import akshare as ak
    obj = ak.futures_gfex_position_rank(date=date)
    store.save_raw(date, "gfex_rank", store.df_to_records(obj))
    rows = []
    for contract, df in obj.items():
        rows += _agg_one(df, "gfex_rank", date, contract, check_total=False)
    return rows


def fetch(date: str) -> list:
    rows = []
    rows += _fetch_shfe(date)
    rows += _fetch_czce(date)
    rows += _fetch_gfex(date)
    return rows


# ---------------------------------------------------------------------------
def _selftest() -> int:
    ok = True
    print("=" * 72)
    print("fetch_rank 自检（真实数据 20261016，验证合计口径与逗号解析）")
    print("=" * 72)

    import akshare as ak
    r = ak.get_shfe_rank_table(date="20261016")
    df = r["rb2701"]
    mine = df[df["rank"] <= 20]["long_open_interest"].sum()
    official = df[df["rank"] == RANK_TOTAL]["long_open_interest"].iloc[0]
    print(f"  ① SHFE rb2701 前20长单={mine} vs 官方合计行={official}（应相等）")
    ok &= abs(float(mine) - float(official)) < 1e-6

    if len(df["rank"]) != 21:
        print(f"  ①b ★ rank 行数={len(df['rank'])}，已不是 21（口径可能变了）")
        ok = False

    cz = ak.get_rank_table_czce(date="20261016")
    print(f"  ② CZCE 数值 dtype={cz['SR']['vol'].dtype} 样例={cz['SR']['vol'].head(2).tolist()}"
          f" → 解析后={[_num(x) for x in cz['SR']['vol'].head(2).tolist()]}")
    ok &= _num(cz["SR"]["vol"].iloc[0]) > 0

    rows = fetch("20261016")
    by = {}
    for x in rows:
        by[x["source"]] = by.get(x["source"], 0) + 1
    print(f"  ③ 抓取 {len(rows)} 行：{by}")

    # 合约级校验：rb2701 必须存在，且 symbol=RB、contract=rb2701（两级分离）
    hit = [x for x in rows if x["source"] == "shfe_rank" and x["contract"] == "rb2701"]
    print(f"  ④ rb2701 记录数={len(hit)}（应 3），symbol={hit[0]['symbol'] if hit else None}"
          f"（应 RB，不是 RB2701）")
    ok &= len(hit) == 3 and hit[0]["symbol"] == "RB"

    # 品种级校验：CZCE 的品种级 key 必须能落成 contract=品种代码
    ap = [x for x in rows if x["source"] == "czce_rank" and x["contract"] == "ap"]
    print(f"  ⑤ CZCE 品种级 AP 记录数={len(ap)}（应为 3），symbol={ap[0]['symbol'] if ap else None}")
    ok &= len(ap) == 3 and ap[0]["symbol"] == "AP"

    # ★ 两级必须真的分离：库里不能出现 symbol 带数字的行
    bad = [x for x in rows if any(ch.isdigit() for ch in x["symbol"])]
    print(f"  ⑥ symbol 含数字的行数={len(bad)}（应 0 —— symbol 是品种，不是合约）")
    ok &= len(bad) == 0

    print("=" * 72)
    print("fetch_rank 自检：" + ("通过 ✅" if ok else "失败 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    d = sys.argv[1] if len(sys.argv) > 1 else None
    if not d:
        raise SystemExit("用法: fetch_rank.py YYYYMMDD | --selftest")
    raise SystemExit(store.run({"rank": fetch}, d))
