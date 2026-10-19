# -*- coding: utf-8 -*-
"""
工具/_np_data/probe_sources.py —— 数据源核实（阶段 1 的工具，不写信号）

清单 §1.3 的机械化：**akshare 搜函数 → 真调用一次 → 看列名/行数/最早日期 → 记录**。
判据只有三个：OK（有数据）/ EMPTY（返回空）/ FAIL（抛异常）。没有"应该能拿到"。

三个模式
--------
  python probe_sources.py                     # 快照：对最近交易日跑一遍，结果写核实结果.json + np_source
  python probe_sources.py --depth             # 深度：对候选日期二分，找每个源最早可用日
  python probe_sources.py --watch 20261019    # 实测：收盘后轮询到拿到当日数据，记录**首次可得时刻**
                                              #   ★ 公布时刻不许抄文档，只能实测。这条是 PIT 的唯一硬证据。

--watch 为什么重要
------------------
  "T 日 15:30 公布"这种说法无法证伪。能证伪的只有：15:31 去抓，抓到了 ⇒ 上界 15:31。
  所以 published_at 的正确来源是 --watch 打出来的 first_seen_at，不是任何网页的形容词。

写 np_source 的口径
-------------------
  verified 只由**本次真调用**的结果决定（OK⇒✅ / EMPTY⇒🟡 / FAIL⇒❌），不接受手填。
  这样"已验证可得"这一列永远有证据链，不会退化成自我安慰。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import pit  # noqa: E402

warnings.filterwarnings("ignore")

RESULT_JSON = _HERE / "核实结果.json"


# ---------------------------------------------------------------------------
# 源登记（curated：只有这里能写"公布规则"，因为它是需要取证的说法）
# ---------------------------------------------------------------------------
def _gfex_receipt(date: str):
    """GFEX 的公共包装函数 futures_gfex_warehouse_receipt 在 1.19.1 里坏了
    （KeyError 'whType'），必须先走内部函数并**传对 vars_list**，否则 KeyError。
    """
    from akshare.futures import receipt as R
    return R.get_gfex_receipt(date=date, vars_list=["SI", "LC", "PS"])


def _em_inventory(date=None):
    import akshare as ak
    return ak.futures_inventory_em(symbol="m")


def _inv_99qh(date=None):
    """99 期货网库存页（周频），直读 SSR 的 __NEXT_DATA__。

    为什么不用 akshare 的 futures_inventory_99：
      ① 它请求时**不带 User-Agent** ⇒ 拿到不含 __NEXT_DATA__ 的页面 ⇒ JSONDecodeError；
      ② 它的数据接口 centerapi.fx168api.com 实测返回 **412 且 body 为空**（与 DCE 同款的
         反爬拦截），所以即便修了 UA，深度也拿不到。
    本函数只读页面里已经渲染好的那 20 期（翻页参数实测全部无效：page/pageNo/pageNum/
    pageSize/currentPage 都改不动 pageNum），所以**深度固定 ≈ 20 周**。
    """
    import json
    import requests
    from bs4 import BeautifulSoup
    h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 Chrome/120.0 Safari/537.36"}
    r = requests.get("https://www.99qh.com/data/stockIn", headers=h,
                     timeout=25, verify=False)
    raw = BeautifulSoup(r.text, features="lxml").find(attrs={"id": "__NEXT_DATA__"})
    if raw is None:                       # 无 UA 时正是这样静默失败
        raise RuntimeError("页面无 __NEXT_DATA__（疑似被拦），拒绝当作空数据")
    d = json.loads(raw.text)["props"]["pageProps"]["data"]
    t = d["positionTrendTableListData"]
    import pandas as pd
    df = pd.DataFrame(t["list"])[["date", "name", "productId", "stock", "stockChange", "close"]]
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df.sort_values("date", ignore_index=True)


def _spot_basis(date):
    """100ppi 现货价 + 基差（日频，54 个商品品种，覆盖 5 所）。

    ★ 两个必须在 note 里写死的风险：
      ① **现货报价的采集时刻不明** —— 100ppi 只说"某日现货价"，不说几点采的。
         PIT 层只能证明"这个数在 available_at 之后可见"，证明不了它反映的是几点前的现货。
         所以它的 published_at 必须由 --watch 实测首见，不许按"收盘后"假设。
      ② 返回里的 dominant_contract 是**第三方定义的主力合约**。直接拿它 join 等于
         使用了当时不可知的主力判定（第二类未来函数），必须走自己的换月序列（L3）。
    """
    import akshare as ak
    return ak.futures_spot_price(date=date)


def _shfe_receipt(date):
    import akshare as ak
    return ak.futures_shfe_warehouse_receipt(date=date)


def _czce_receipt(date):
    import akshare as ak
    return ak.futures_warehouse_receipt_czce(date=date)


def _dce_receipt(date):
    import akshare as ak
    return ak.futures_warehouse_receipt_dce(date=date)


def _shfe_rank(date):
    import akshare as ak
    return ak.get_shfe_rank_table(date=date)


def _czce_rank(date):
    import akshare as ak
    return ak.get_rank_table_czce(date=date)


def _gfex_rank(date):
    import akshare as ak
    return ak.futures_gfex_position_rank(date=date)


def _dce_rank(date):
    import akshare as ak
    return ak.futures_dce_position_rank(date=date)


# source / 类别 / 交易所 / 调用 / 公布规则（curated）/ 说明
#   method      ← 8 字段里的"获取方式"，手写（因为不是每个源都能用 fn.__name__ 说清）
#   depth_fixed ← 深度不能靠 --depth 探测的源（周频/忽略 date），直接写实测结论
SOURCES = [
    dict(source="shfe_receipt", category="仓单日报", exchange="SHFE+INE", fn=_shfe_receipt,
         method="akshare.futures_shfe_warehouse_receipt(date)",
         published_rule="T日结算后(15:00后); 实测首见见 first_seen",
         anti_crawl="无(静态 html)", need_login=0,
         note="返回 dict[品种名->DataFrame]，含 INE 5 项(原油/低硫/20号胶/BC铜)"),
    dict(source="czce_receipt", category="仓单日报", exchange="CZCE", fn=_czce_receipt,
         method="akshare.futures_warehouse_receipt_czce(date)",
         published_rule="T日结算后(15:00后); 实测首见见 first_seen",
         anti_crawl="无(xls/xlsx)", need_login=0,
         note="2025-11-01 起格式 xls→xlsx，函数内已按日期分支"),
    dict(source="gfex_receipt", category="仓单日报", exchange="GFEX", fn=_gfex_receipt,
         method="akshare.futures.receipt.get_gfex_receipt(date, vars_list=[SI,LC,PS])",
         published_rule="T日结算后(15:00后); 实测首见见 first_seen",
         anti_crawl="无", need_login=0,
         note="公共包装函数已坏，须走 get_gfex_receipt(vars_list=SI/LC/PS)"),
    dict(source="dce_receipt", category="仓单日报", exchange="DCE", fn=_dce_receipt,
         method="akshare.futures_warehouse_receipt_dce(date)",
         published_rule="未知(取不到)",
         anti_crawl="瑞数 412（全域名）", need_login=0,
         note="实测 www.dce.com.cn 主页也 412 ⇒ 非接口级，是 WAF 全站拦非浏览器会话"),
    dict(source="shfe_rank", category="会员持仓排名", exchange="SHFE", fn=_shfe_rank,
         method="akshare.get_shfe_rank_table(date)",
         published_rule="T日结算后; 当日 vs T+1 待实测",
         anti_crawl="无", need_login=0,
         note="dict[合约->DataFrame]，含 成交量/持买单量/持卖单量 前20名"),
    dict(source="czce_rank", category="会员持仓排名", exchange="CZCE", fn=_czce_rank,
         method="akshare.get_rank_table_czce(date)",
         published_rule="T日结算后; 待实测",
         anti_crawl="无", need_login=0,
         note="akshare 自报数据源起始 20151008"),
    dict(source="gfex_rank", category="会员持仓排名", exchange="GFEX", fn=_gfex_rank,
         method="akshare.futures_gfex_position_rank(date)",
         published_rule="T日结算后; 待实测",
         anti_crawl="无", need_login=0,
         note=""),
    dict(source="dce_rank", category="会员持仓排名", exchange="DCE", fn=_dce_rank,
         method="akshare.futures_dce_position_rank(date)",
         published_rule="未知(取不到)",
         anti_crawl="瑞数 412（全域名）", need_login=0,
         note="batchDownload 返回 412 HTML ⇒ BadZipFile；与仓单同一个 WAF"),
    dict(source="inv_99qh", category="库存周报", exchange="SHFE+CZCE+DCE+GFEX", fn=_inv_99qh,
         method="requests 直读 99qh 页面 __NEXT_DATA__（akshare 版不带 UA 已死）",
         published_rule="周频(每周五); 当日 15:00 后待周五实测",
         anti_crawl="页面可读；其 centerapi 翻页接口 412", need_login=0,
         depth_fixed="仅最近20期(约5个月); 翻页参数实测全部无效",
         note="口径=交易所库存(吨)≠仓单；stockChange 自带周环比；⚠ 勿与仓单混用(R6)"),
    dict(source="spot_basis", category="现货基差", exchange="SHFE+CZCE+DCE+GFEX+INE",
         fn=_spot_basis,
         method="akshare.futures_spot_price(date)  [100ppi]",
         published_rule="★采集时刻不明，须 --watch 实测；未测前不得假设收盘后",
         anti_crawl="无", need_login=0,
         note="54 品种含现货价/近月基差/主力基差；⚠ dominant_contract 是第三方主力定义,"
              "直接 join = 第二类未来函数, 必须走自己的换月序列"),
    dict(source="em_inventory", category="库存(第三方)", exchange="DCE+SHFE", fn=_em_inventory,
         method="akshare.futures_inventory_em(symbol)",
         published_rule="时刻不明 ⇒ 不可用于 PIT",
         anti_crawl="无", need_login=0, mark_cap="🟡",
         note="东财口径=库存≠仓单(R6)；深度仅约 3 个月；时刻不明 ⇒ 只记录不采用"),
]

# 深探候选日（二分用的锚点）
DEPTH_DATES = ["20100104", "20130104", "20150105", "20160104",
               "20180102", "20200102", "20220104", "20240102", "20260601"]


# ---------------------------------------------------------------------------
# 调用 + 判定
# ---------------------------------------------------------------------------
def _measure(obj) -> dict:
    """把返回值度量成 (状态, 品种数, 行数)。"""
    if obj is None:
        return {"status": "EMPTY", "n_key": 0, "n_rows": 0}
    if isinstance(obj, dict):
        n_key = len(obj)
        n_rows = sum(len(v) for v in obj.values() if hasattr(v, "__len__"))
    else:
        n_key = 1
        n_rows = len(obj)
    return {"status": "OK" if (n_key > 0 and n_rows > 0) else "EMPTY",
            "n_key": int(n_key), "n_rows": int(n_rows)}


def probe_one(src: dict, date: str, retries: int = 3) -> dict:
    """调一次。失败重试 retries-1 次再定罪 —— 单次 SSL 抖动不该被记成"源不可用"。"""
    t0 = time.time()
    rec = {"source": src["source"], "date": date, "attempts": 0}
    last = None
    for i in range(max(1, retries)):
        rec["attempts"] = i + 1
        try:
            obj = src["fn"](date)                          # 统一位置参数
            rec.update(_measure(obj))
            last = None
            break
        except Exception as e:
            last = e
            if i + 1 < retries:
                time.sleep(1.5)
    if last is not None:
        rec.update({"status": "FAIL", "n_key": 0, "n_rows": 0,
                    "error": f"{type(last).__name__}: {str(last)[:160]}"})
    rec["elapsed_s"] = round(time.time() - t0, 2)
    rec["probed_at"] = datetime.now().replace(microsecond=0).isoformat(sep="T")
    return rec


# ---------------------------------------------------------------------------
# 写 np_source（verified 由探测结果决定，不许手填）
# ---------------------------------------------------------------------------
_STATUS2MARK = {"OK": "✅", "EMPTY": "🟡", "FAIL": "❌"}


def write_sources(recs: list, db_path=None) -> int:
    conn = pit.connect(db_path)
    now = datetime.now().replace(microsecond=0).isoformat(sep="T")
    by = {r["source"]: r for r in recs}
    n = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        for src in SOURCES:
            r = by.get(src["source"], {})
            mark = _STATUS2MARK.get(r.get("status", ""), "")
            # 口径不同/深度不足的源，即使抓得到也不许标 ✅（否则等于自我安慰）
            if src.get("mark_cap") and mark == "✅":
                mark = src["mark_cap"]
            note = src["note"]
            if r.get("status") == "FAIL":
                note = (note + " | " if note else "") + r.get("error", "")[:120]
            depth = r.get("depth") or src.get("depth_fixed") or ""
            conn.execute(
                """INSERT INTO np_source
                   (source,category,exchange,published_rule,history_depth,fetch_method,
                    anti_crawl,need_login,verified,note,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source) DO UPDATE SET
                     category=excluded.category, exchange=excluded.exchange,
                     published_rule=excluded.published_rule,
                     history_depth=excluded.history_depth,
                     fetch_method=excluded.fetch_method,
                     anti_crawl=excluded.anti_crawl, need_login=excluded.need_login,
                     verified=excluded.verified, note=excluded.note,
                     updated_at=excluded.updated_at""",
                (src["source"], src["category"], src["exchange"], src["published_rule"],
                 depth, src.get("method") or src["fn"].__name__,
                 src["anti_crawl"], src["need_login"], mark, note, now))
            n += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return n


# ---------------------------------------------------------------------------
# 三个模式
# ---------------------------------------------------------------------------
def latest_trade_day() -> str:
    """最近一个已收盘的交易日（YYYYMMDD）。15:00 前用上一交易日。"""
    import akshare as ak
    try:
        df = ak.tool_trade_date_hist_sina()
        days = [d.strftime("%Y%m%d") for d in df["trade_date"]]
    except Exception:
        return datetime.now().strftime("%Y%m%d")
    now = datetime.now()
    today = now.strftime("%Y%m%d")
    past = [d for d in days if d < today]
    if today in days and now.hour >= 16:
        return today
    return past[-1] if past else days[0]


def mode_snapshot(date: str | None) -> int:
    d = date or latest_trade_day()
    print(f"[快照] 交易日 {d}")
    recs = []
    for src in SOURCES:
        r = probe_one(src, d)
        recs.append(r)
        flag = {"OK": "OK  ", "EMPTY": "EMPTY", "FAIL": "FAIL"}[r["status"]]
        extra = f"{r['n_key']} 键 / {r['n_rows']} 行" if r["status"] != "FAIL" else r.get("error", "")
        print(f"  {src['source']:<14} {src['exchange']:<9} {flag}  {extra}")
    meta = {"probed_at": datetime.now().replace(microsecond=0).isoformat(sep="T"),
            "date": d, "akshare": _ak_ver(), "results": recs}
    RESULT_JSON.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    n = write_sources(recs)
    print(f"  → {RESULT_JSON.name}；np_source 写入 {n} 行")
    return 0


def mode_depth() -> int:
    print("[深度] 每个源最早可用日（候选锚点，不是精确二分）")
    out = {}
    for src in SOURCES:
        if src["source"] in ("em_inventory", "dce_receipt", "dce_rank"):
            continue                       # 快照已判 ❌ / 口径不可用，深探没意义
        if src.get("depth_fixed"):
            out[src["source"]] = src["depth_fixed"]   # 周频/忽略 date 的源，探 date 是自欺
            print(f"  {src['source']:<14} 深度固定（不探）: {src['depth_fixed']}")
            continue
        first = ""
        for d in DEPTH_DATES:
            r = probe_one(src, d)
            if r["status"] == "OK":
                first = d
                break
        out[src["source"]] = first or "不可得"
        print(f"  {src['source']:<14} 最早可用（锚点内）: {first or '全部失败'}")
    meta = json.loads(RESULT_JSON.read_text(encoding="utf-8")) if RESULT_JSON.exists() else {}
    meta.setdefault("results", [])
    for r in meta["results"]:
        r["depth"] = out.get(r["source"], "")
    meta["depth_probed_at"] = datetime.now().replace(microsecond=0).isoformat(sep="T")
    RESULT_JSON.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    write_sources(meta["results"])
    return 0


def mode_watch(date: str, deadline: str = "16:40", interval: int = 60,
               start: str = "") -> int:
    """轮询到拿到目标交易日数据为止，记录 first_seen_at（= published_at 的上界证据）。

    两条务实约束：
      ① **不轮询已知 ❌ 的源**（DCE 全站 412）。每 60 秒打一次 WAF 毫无意义，
         还会让"我们被封了"变成"我们主动惹它"。
      ② **每拿到一个源就立刻落盘**。这个进程要活 1 小时以上，
         中途被杀时已拿到的首见时刻不能丢。
    """
    skip = {"dce_receipt", "dce_rank", "em_inventory",
            "inv_99qh"}      # 库存是周频，只有周五会出新周期；日频 watch 必然空转
    targets = [s for s in SOURCES if s["source"] not in skip]
    out = _HERE / f"实测首见_{date}.json"
    first = {}
    if out.exists():                                  # 续跑：已记录的不再重测
        try:
            first = json.loads(out.read_text(encoding="utf-8"))
        except Exception:
            first = {}

    print(f"[实测] 目标 {date}  源 {len(targets)} 个（跳过 DCE 全站+第三方+周频库存）")
    print(f"       窗口 {start or 'now'} → {deadline}，间隔 {interval}s，落盘 {out.name}")
    print("       注：inv_99qh 是周频，须在**周五**单独跑本命令才能测到它的公布时刻")

    while start and datetime.now().strftime("%H:%M") < start:
        time.sleep(min(30, interval))

    while True:
        now = datetime.now()
        for src in targets:
            if src["source"] in first:
                continue
            r = probe_one(src, date, retries=1)        # 轮询模式不重试，下一轮再说
            if r["status"] == "OK":
                first[src["source"]] = {
                    "first_seen": now.replace(microsecond=0).isoformat(sep="T"),
                    "n_key": r["n_key"], "n_rows": r["n_rows"]}
                out.write_text(json.dumps(first, ensure_ascii=False, indent=2),
                               encoding="utf-8")
                print(f"  {now.strftime('%H:%M:%S')}  {src['source']:<14} 首见 ✅ "
                      f"({r['n_key']} 键/{r['n_rows']} 行)")
        missing = [s["source"] for s in targets if s["source"] not in first]
        if not missing:
            print("  全部拿到")
            break
        if now.strftime("%H:%M") > deadline:
            print(f"  到 {deadline} 仍未拿到：{', '.join(missing)}")
            break
        time.sleep(interval)

    print(f"  → {out}")
    return 0


def _ak_ver() -> str:
    try:
        import akshare as ak
        return ak.__version__
    except Exception:
        return "?"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--depth", action="store_true")
    ap.add_argument("--watch", metavar="YYYYMMDD")
    ap.add_argument("--date", metavar="YYYYMMDD")
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--deadline", default="16:40")
    ap.add_argument("--start", default="", help="等到 HH:MM 再开始轮询，例如 15:20")
    a = ap.parse_args()
    if a.depth:
        return mode_depth()
    if a.watch:
        return mode_watch(a.watch, a.deadline, a.interval, a.start)
    return mode_snapshot(a.date)


if __name__ == "__main__":
    raise SystemExit(main())
