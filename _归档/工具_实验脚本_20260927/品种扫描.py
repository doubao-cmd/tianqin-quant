# -*- coding: utf-8 -*-
"""
==========================================================
 全市场期货品种扫描：流动性排名 + 相关性矩阵
==========================================================

用法：
    python 工具/品种扫描.py                     # 全市场成交额排名
    python 工具/品种扫描.py --top 40             # 只显示前 40 名
    python 工具/品种扫描.py --corr 20            # 对成交额前 20 名算相关性矩阵
    python 工具/品种扫描.py --corr 20 --pick 10  # 再挑出一组低相关品种

数据口径：
    流动性 —— 交易所行情的【成交额】（元），不是成交量。
              各品种一手大小差很多（螺纹 10 吨/手 vs 纯碱 20 吨/手），
              比「手数」没意义，比「钱」才有意义。
    相关性 —— 指数合约（KQ.i@）的日线收益率。
              用指数不用主力：主力换月那天会跳空，会把相关性算歪。

⚠️ 两个坑（都踩过）：
    1. `api.wait_update()` 不传 deadline 会**无限阻塞** —— 凌晨没有行情推送时
       它就一直等。本脚本一律用 `deadline=` 轮询 + 总超时兜底。
    2. 必须 `python -u` 跑（或本脚本自身冲掉缓冲），否则卡住了也看不到日志。

注意：
    - 相关性只代表【历史相关】，不代表将来；它随行情结构变化。
    - 低相关 ≠ 能赚钱。它只降低「押注单一板块」的风险，不提高预期收益。
"""

import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
while ROOT != os.path.dirname(ROOT) and not os.path.isdir(os.path.join(ROOT, "lib")):
    ROOT = os.path.dirname(ROOT)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config_account as CA


# ---------------------------------------------------------------
# 两套分类：板块（粗，用来看分布） / 族（细，用来卡「不能是同一个大宗品种」）
# 划分依据是「价格驱动因素」（产业链），不是交易所分类。
# 族比板块细：比如「农产品」里，油脂油料 / 软商品 / 养殖 其实是三件独立的事，
# 相关性差得远，不该被当成一个东西。
# ---------------------------------------------------------------
分类表 = {
    # --- 黑色 ---
    "rb": ("黑色", "黑色-钢材"), "hc": ("黑色", "黑色-钢材"),
    "i": ("黑色", "黑色-钢材"), "ss": ("黑色", "黑色-钢材"),
    "j": ("黑色", "黑色-煤焦"), "jm": ("黑色", "黑色-煤焦"),
    "sf": ("黑色", "铁合金"), "sm": ("黑色", "铁合金"),
    "FG": ("黑色", "建材-玻璃"), "wr": ("黑色", "黑色-钢材"),
    # --- 有色 ---
    "cu": ("有色", "有色-基本金属"), "al": ("有色", "有色-基本金属"),
    "zn": ("有色", "有色-基本金属"), "pb": ("有色", "有色-基本金属"),
    "sn": ("有色", "有色-基本金属"), "ni": ("有色", "有色-基本金属"),
    "ao": ("有色", "有色-基本金属"), "bc": ("有色", "有色-基本金属"),
    "ad": ("有色", "有色-基本金属"),
    "SI": ("有色", "新能源金属"), "LC": ("有色", "新能源金属"),
    "PS": ("有色", "新能源金属"),
    # --- 化工（煤化工 + 油化工；实测两者相关性 0.7~0.9，故同族） ---
    "MA": ("化工", "化工"), "UR": ("化工", "化工"), "TA": ("化工", "化工"),
    "PX": ("化工", "化工"), "PF": ("化工", "化工"), "PR": ("化工", "化工"),
    "BZ": ("化工", "化工"), "PL": ("化工", "化工"),
    "l": ("化工", "化工"), "v": ("化工", "化工"), "pp": ("化工", "化工"),
    "eg": ("化工", "化工"), "eb": ("化工", "化工"),
    "SA": ("化工", "盐化工-纯碱"), "SH": ("化工", "盐化工-烧碱"),
    # --- 橡胶（独立：与化工、能源相关性都低） ---
    "ru": ("橡胶", "橡胶"), "nr": ("橡胶", "橡胶"), "br": ("橡胶", "橡胶"),
    # --- 农产品（按产业链分三族 + 谷物 + 鲜果 + 林产品） ---
    "m": ("农产品", "油脂油料"), "y": ("农产品", "油脂油料"),
    "a": ("农产品", "油脂油料"), "b": ("农产品", "油脂油料"),
    "p": ("农产品", "油脂油料"), "OI": ("农产品", "油脂油料"),
    "RM": ("农产品", "油脂油料"),
    "SR": ("农产品", "软商品-糖棉"), "CF": ("农产品", "软商品-糖棉"),
    "CY": ("农产品", "软商品-糖棉"),
    "lh": ("农产品", "养殖-生猪"), "jd": ("农产品", "养殖-鸡蛋"),
    "c": ("农产品", "谷物"), "cs": ("农产品", "谷物"),
    "WH": ("农产品", "谷物"), "PM": ("农产品", "谷物"),
    "RI": ("农产品", "谷物"), "JR": ("农产品", "谷物"),
    "LR": ("农产品", "谷物"), "rr": ("农产品", "谷物"),
    "AP": ("农产品", "鲜果"), "CJ": ("农产品", "鲜果"),
    "PK": ("农产品", "油脂油料"),
    "sp": ("农产品", "林产品-纸浆"), "fb": ("农产品", "林产品-纸浆"),
    "bb": ("农产品", "林产品-纸浆"),
    # --- 能源（原油系） ---
    "sc": ("能源", "能源-原油系"), "fu": ("能源", "能源-原油系"),
    "bu": ("能源", "能源-原油系"), "lu": ("能源", "能源-原油系"),
    "pg": ("能源", "能源-原油系"), "ZC": ("能源", "能源-煤炭"),
    # --- 贵金属 ---
    "au": ("贵金属", "贵金属"), "ag": ("贵金属", "贵金属"),
    # --- 航运 ---
    "ec": ("航运", "航运"),
}

金融交易所 = {"CFFEX"}          # 股指/国债，不算大宗商品

# 用户明确排除的品种（2026-09-25）
排除品种 = {
    "SHFE.au": "沪金 —— 用户排除",
    "SHFE.ag": "沪银 —— 用户排除",
    "INE.sc": "原油 —— 用户排除",
}


def 拆代码(合约):
    """'SHFE.rb2610' -> ('SHFE', 'rb')"""
    exch, _, rest = 合约.partition(".")
    prod = "".join(ch for ch in rest if not ch.isdigit())
    return exch, prod


def 认分类(exch, prod):
    """返回 (板块, 族)。大小写不敏感（郑商所大写，其余小写，GFEX 也是小写）。"""
    for k in (prod, prod.upper(), prod.lower()):
        if k in 分类表:
            return 分类表[k]
    return ("未分类", "未分类")


def 轮询(api, 条件, 超时=40, 说明=""):
    """带 deadline 的等待：绝不允许无限阻塞"""
    end = time.time() + 超时
    while time.time() < end:
        api.wait_update(deadline=time.time() + 0.5)
        try:
            if 条件():
                return True
        except Exception:
            pass
    print(f"  [警告] 等待超时（{超时}s）：{说明}", flush=True)
    return False


def 拉行情():
    from tqsdk import TqApi, TqAuth

    user, pwd = CA.check()
    api = TqApi(auth=TqAuth(user, pwd))
    print("已登录。", flush=True)

    conts = sorted(api.query_cont_quotes())
    print(f"交易所返回主力合约 {len(conts)} 个，正在订阅行情…", flush=True)

    qs = {}
    for c in conts:
        try:
            qs[c] = api.get_quote(c)
        except Exception:
            pass
    轮询(api, lambda: all(bool(getattr(q, "datetime", "")) for q in qs.values()),
        超时=45, 说明="订阅全部主力合约行情")

    rows = []
    for c, q in qs.items():
        if not getattr(q, "datetime", ""):
            continue
        exch, prod = 拆代码(c)
        if exch in 金融交易所:
            continue
        板块, 族 = 认分类(exch, prod)
        rows.append({
            "合约": c,
            "主力合约": c,
            "交易所": exch,
            "品种": prod,
            "板块": 板块,
            "族": 族,
            "成交额": float(getattr(q, "amount", 0) or 0),
            "成交量": float(getattr(q, "volume", 0) or 0),
            "持仓量": float(getattr(q, "open_interest", 0) or 0),
            "收盘": float(getattr(q, "close", 0) or 0),
            "乘数": float(getattr(q, "volume_multiple", 0) or 0),
            "最小变动": float(getattr(q, "price_tick", 0) or 0),
            "一环": float(getattr(q, "volume_multiple", 0) or 0)
                    * float(getattr(q, "price_tick", 0) or 0),
            "行情时间": str(getattr(q, "datetime", ""))[:19],
        })
    return api, rows


def 打印排名(rows, top=None):
    rows = sorted(rows, key=lambda r: -r["成交额"])
    if top:
        rows = rows[:top]
    print()
    print("=" * 84)
    print(f"  流动性排名（成交额降序；行情时间 {rows[0]['行情时间'] if rows else '—'}）")
    print("=" * 84)
    print(f"{'排名':<5}{'品种':<8}{'板块':<7}{'族':<13}{'成交额(亿)':>11}{'持仓量(手)':>13}  主力合约")
    print("-" * 90)
    for n, r in enumerate(rows, 1):
        print(f"{n:<5}{r['品种']:<8}{r['板块']:<7}{r['族']:<13}{r['成交额']/1e8:>11.1f}"
              f"{r['持仓量']:>13,.0f}  {r['主力合约']}")
    return rows


def 算相关性(api, 选中):
    """对给定的一组品种，用【指数合约】日线收益率算相关性矩阵"""
    import pandas as pd

    ks = {}
    for r in 选中:
        idx = f"KQ.i@{r['交易所']}.{r['品种']}"
        try:
            ks[r["品种"]] = (r, api.get_kline_serial(idx, 86400, data_length=260))
        except Exception as e:
            print(f"  {r['品种']} 订阅指数日线失败：{e}", flush=True)

    轮询(api, lambda: all(float(k["datetime"].iloc[0]) > 0 and
                          float(k["close"].iloc[0]) > 0
                          for _, k in ks.values()),
        超时=90, 说明="取指数日线（必须等到最早一根也有数据，否则会被当成缺失）")

    data = {}
    有效行 = []
    for name, (r, k) in ks.items():
        s = pd.Series(k["close"].to_numpy(), index=k["datetime"].to_numpy())
        s = s[s > 0]
        if len(s) > 30:
            data[name] = s.pct_change()
            有效行.append(r)
    if not data:
        return None, None
    df = pd.DataFrame(data)
    有效 = df.dropna()
    if 有效.empty:
        return None, None
    t0 = pd.to_datetime(有效.index[0], unit="ns")
    t1 = pd.to_datetime(有效.index[-1], unit="ns")
    print(f"\n相关性样本：{len(有效)} 个交易日（{t0:%Y-%m-%d} ~ {t1:%Y-%m-%d}）", flush=True)
    return 有效.corr(), 有效行


def 池体检(corr, 元数据, 名字=""):
    """报一个池子的分散程度：平均|相关|、最大|相关|、最像的一对"""
    import itertools

    cols = list(corr.columns)
    if len(cols) < 2:
        return
    pairs = [(abs(corr.loc[a, b]), a, b) for a, b in itertools.combinations(cols, 2)]
    avg = sum(p[0] for p in pairs) / len(pairs)
    最像 = max(pairs)
    print(f"\n  【{名字}】{len(cols)} 个品种，{len(pairs)} 对")
    print(f"    平均|相关| = {avg:.2f}      最大|相关| = {最像[0]:.2f}  "
          f"（{最像[1]} - {最像[2]}）")
    族数 = {}
    for c in cols:
        f = 元数据[c]["族"]
        族数[f] = 族数.get(f, 0) + 1
    print(f"    族分布：{'  '.join(f'{k}×{v}' for k, v in sorted(族数.items()))}")
    return avg, 最像[0]


def 打印矩阵(corr):
    cols = list(corr.columns)
    print()
    print("相关性矩阵（1.00 = 完全同涨同跌，0 = 无关）")
    print(" " * 8 + "".join(f"{c:>7}" for c in cols))
    for a in cols:
        print(f"{a:<8}" + "".join(f"{corr.loc[a, b]:>7.2f}" for b in cols))


def 挑低相关(corr, 元数据, k, 最大相关=0.55, 每族最多=2):
    """按【流动性优先】挑一组「互相不重叠」的品种。

    为什么不是「先挑最不相关的」：
        那样会把烧碱、不锈钢这类 130 亿的小品种选进来 —— 它们确实独立，
        但流动性不够，滑点会吃掉一切。正确顺序是：先要够大，再要不重叠。

    约束（硬）：
      · 与已选集合里任意一个的 |相关| 不得超过 `最大相关`
        —— 光看「平均相关」会漏掉成对的高相关（例如 燃料油 vs 甲醇 0.79）
      · 同一个「族」最多 `每族最多` 个 —— 保证不扎堆同一个大宗品种

    顺序（软）：沿着成交额从大到小扫，能过约束就收。
    """
    已选 = []
    被挡 = []
    for c in corr.columns:                 # 已按成交额降序
        if len(已选) >= k:
            break
        同族 = 元数据[c]["族"]
        if sum(1 for s in 已选 if 元数据[s]["族"] == 同族) >= 每族最多:
            被挡.append((c, f"同族已达 {每族最多} 个（{同族}）"))
            continue
        最差 = max((abs(corr.loc[c, s]) for s in 已选), default=0.0)
        if 最差 > 最大相关:
            被挡.append((c, f"与已选 {最差:.2f} > {最大相关}"))
            continue
        已选.append(c)
    return 已选, 被挡


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=None, help="只显示成交额前 N 名")
    ap.add_argument("--corr", type=int, default=0, help="对前 N 名算相关性矩阵")
    ap.add_argument("--pick", type=int, default=0, help="挑 N 个低相关品种")
    ap.add_argument("--pool", type=str, default="",
                    help='指定品种看相关性，逗号分隔；多组用分号隔开。'
                         '例：--pool "V,MA,SA,RB,M;ru,eb,jm,p,m"')
    ap.add_argument("--max-corr", type=float, default=0.55, help="两两相关的上限（默认 0.55）")
    ap.add_argument("--max-per-family", type=int, default=2, help="同一族最多几个（默认 2）")
    args = ap.parse_args()

    api, rows = 拉行情()
    try:
        排名 = 打印排名(rows, args.top)

        保留 = []
        for r in 排名:
            key = f"{r['交易所']}.{r['品种']}"
            if key in 排除品种:
                print(f"\n  [已排除] {r['品种']:<8} {排除品种[key]}")
            else:
                保留.append(r)

        if args.corr or args.pick:
            n = max(args.corr, args.pick)
            corr, 选中 = 算相关性(api, 保留[:n])
            if corr is None:
                print("  没取到相关性数据")
                return

            元数据 = {r["品种"]: r for r in 选中}

            if args.corr:
                print()
                print("=" * 84)
                print(f"  相关性矩阵（成交额前 {n} 名）")
                print("=" * 84)
                打印矩阵(corr)

            if args.pick:
                picks, 被挡 = 挑低相关(corr, 元数据, args.pick,
                                       最大相关=args.max_corr, 每族最多=args.max_per_family)
                print()
                print("=" * 96)
                print(f"  推荐品种池（{len(picks)} 个；两两|相关|≤{args.max_corr}，同族≤{args.max_per_family}，按流动性优先）")
                print("=" * 96)
                print(f"{'品种':<8}{'板块':<7}{'族':<13}{'成交额(亿)':>10}{'持仓(手)':>11}"
                      f"{'乘数':>6}{'跳值':>6}{'最差相关':>9}  主力合约")
                print("-" * 96)
                全对 = []
                for c in picks:
                    r = 元数据[c]
                    others = [abs(corr.loc[c, s]) for s in picks if s != c]
                    最差 = max(others) if others else 0.0
                    全对.append((c, 最差))
                    print(f"{c:<8}{r['板块']:<7}{r['族']:<13}{r['成交额']/1e8:>10.1f}"
                          f"{r['持仓量']:>11,.0f}{r['乘数']:>6.0f}{r['一环']:>6.0f}"
                          f"{最差:>9.2f}  {r['主力合约']}")
                最差整体 = max(v for _, v in 全对) if 全对 else 0
                print(f"\n  池内两两|相关|最大值 = {最差整体:.2f}"
                      f"（越低越分散；>0.8 基本等于同一个东西）")
                print()
                print("  未入选的（按流动性与被挡原因看，可当替补）：")
                seen = set()
                for c, why in 被挡:
                    if c in seen:
                        continue
                    seen.add(c)
                    if len(seen) > 10:
                        break
                    r = 元数据.get(c)
                    if r:
                        print(f"    {c:<8}{r['板块']:<7}{r['族']:<13}"
                              f"{r['成交额']/1e8:>8.1f} 亿   被挡：{why}")

        if args.pool:
            全元数据 = {}
            for r in rows:
                全元数据[r["品种"]] = r
                全元数据[r["品种"].upper()] = r      # 大小写都认，免得手打 RB/V 找不到
                全元数据[r["品种"].lower()] = r
            组 = [g.strip() for g in args.pool.split(";") if g.strip()]
            for gi, g in enumerate(组, 1):
                codes = [c.strip() for c in g.split(",") if c.strip()]
                缺失 = [c for c in codes if c not in 全元数据]
                if 缺失:
                    print(f"\n  [注意] 不认识的品种：{缺失}")
                rs = [全元数据[c] for c in codes if c in 全元数据]
                if len(rs) < 2:
                    continue
                corr2, 选中2 = 算相关性(api, rs)
                if corr2 is None:
                    continue
                print()
                print("=" * 84)
                print(f"  指定池 {gi}：{', '.join(r['品种'] for r in 选中2)}")
                print("=" * 84)
                打印矩阵(corr2)
                池体检(corr2, {r["品种"]: r for r in 选中2}, f"池 {gi}")
    finally:
        api.close()


if __name__ == "__main__":
    main()
