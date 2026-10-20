# -*- coding: utf-8 -*-
"""
工具/_np_data/fetch_inventory.py —— 库存（周报/日报）→ np_data（D6/D7 收尾）

源：99qh「库存」页（https://www.99qh.com/data/stockIn）。
akshare 的 `futures_inventory_99` **已死**（请求不带 UA ⇒ 拿不到 `__NEXT_DATA__` ⇒ json 解析炸），
本模块自建两层请求：

  ① 页面 SSR：`/data/stockIn` 的 `__NEXT_DATA__` → 拿 `buildId` + 全部品种目录（82 条）
  ② 数据路由：`/_next/data/{buildId}/data/stockIn.json?productId=N` → 该品种最近 20 期
     ★ 这一步是本次实测新发现：页面的 `?productId=` 参数**不生效**（SSR 永远渲染默认品种
       螺纹钢），但 Next.js 的 data 路由**生效**。所以 82 个品种是能拿到的，代价是 83 次请求。

===============================================================================
这个源的四个真实缺陷（都写进库/日志，不写进"应该能拿到"）
===============================================================================
1. **深度锁死 20 期**（`pageNum` 等 6 种分页参数实测全部无效；翻页走 centerapi，返回 412）。
   20 期对周频品种 ≈ 5 个月，对日频品种 ≈ 1 个月。
   ⇒ 只能做**当日状态观测 + 近端序列**，做不了长历史因子。这一点比仓单/排名差一个量级。

2. **覆盖不含 GFEX。** 广期所 5 个品种（LC/SI/PS/PT/PD）实测 `total=0`（真无数据，不是解析失败）。
   清单里 `inv_99qh` 的交易所写成 "SHFE+CZCE+DCE+GFEX" 是**错的**，已按实测改为三所。

3. **频率不统一，且是源侧不统一。** 同为品种：
   - 周频（间隔 6~9 天）：SHFE 系为主，如 RB/CU
   - 日频（间隔 1~3 天）：DCE 系为主，如 C
   `period_start = period_end = 源给的那个日期`——源只给一个日期，不替它编区间。

4. **口径是"库存"，不是"仓单"**（红线 R6）。SHFE 的库存数值恰好等于仓单总计
   （实测 2026-10-16 铜 36386 = 仓单总计 36386），但 DCE/CZCE 两者不同。
   ⇒ metric 一律叫 `inventory`，**绝不并入 `receipt`**，下游要替代必须先做口径论证。

===============================================================================
单位：不统一 ⇒ 编进 metric 名，物理上禁止误加总
===============================================================================
82 个品种的单位：吨 76 / 千克 2（au,ag）/ 立方米 2（lg,fb）/ 千桶 1（sc）/ 张 1（bb）。
np_data 没有 unit 列（表结构已定死），如果把 千克 和 吨 混在同一个 metric 里，
下游一句 `groupby(metric).sum()` 就会把黄金（千克）和铜（吨）加起来 —— 算错且不报错。
所以：吨 → `inventory`；其余 → `inventory_kg` / `inventory_m3` / `inventory_kbbl` / `inventory_sheet`。
遇到未知单位直接抛错，不许猜一个后缀糊过去。

===============================================================================
回填的诚实边界
===============================================================================
每次抓取会把该品种**当前可见的 20 期全部写入**（不是只写最新一期），好处是首次运行就有序列。
但这 20 期是「**今天看到的**历史值」，不等于「**当时公布的**值」——第三方源可能已回头修订。
`published_at` 按各期自身日期 + 16:00 规则给（不是按抓取日），所以不会引入前视；
`raw_url` 里写明"回填=当前可见值非当时公布值"，需要严格回放时按 `fetched_at` 筛。
"""
from __future__ import annotations

import sys
import json
import time
import warnings
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import pit    # noqa: E402
import store  # noqa: E402

warnings.filterwarnings("ignore")

SOURCE = "inv_99qh"
PAGE_URL = "https://www.99qh.com/data/stockIn"
DATA_URL = "https://www.99qh.com/_next/data/{build_id}/data/stockIn.json?productId={pid}"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 单位 → metric 后缀。吨是基准，不加后缀。
UNIT_SUFFIX = {"吨": "", "千克": "_kg", "立方米": "_m3", "千桶": "_kbbl", "张": "_sheet"}

# 伪品种：不是库存序列，不能当库存写
PSEUDO_NAME_KEYS = ("均价", "指数")

# 频率随品种不同（周频/日频都有），所以只能说"T日"，不能说"周五"
NOTE = "T日16:00(假设,未实测); 回填=当前可见值非当时公布值; 深度仅20期"


class _StaleBuild(Exception):
    """buildId 过期（站点重新部署）。data 路由会返回 404 + HTML，不是 JSON。"""


# ---------------------------------------------------------------------------
def _session():
    import requests
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": PAGE_URL})
    return s


def _catalog(sess):
    """返回 (build_id, products)。products = [{productId,name,code,exchange,unit}, ...]

    ★ 不带 UA 时页面照常 200，但 `__NEXT_DATA__` 会缺失 —— akshare 就是死在这一步。
    所以这里必须**显式报错**，不能回退成"今天没数据"。
    """
    from bs4 import BeautifulSoup
    r = sess.get(PAGE_URL, timeout=25)
    r.raise_for_status()
    tag = BeautifulSoup(r.text, features="lxml").find(attrs={"id": "__NEXT_DATA__"})
    if tag is None:
        raise RuntimeError(
            f"{PAGE_URL} 返回 {r.status_code} 但页面里没有 __NEXT_DATA__（len={len(r.content)}）。\n"
            f"  已知原因：请求不带浏览器 UA 时站点会返回一个空壳页面（akshare 的 "
            f"futures_inventory_99 就是这么静默死掉的）。\n"
            f"  不要把它当成「当日无数据」放过。")
    j = json.loads(tag.text)
    build_id = j.get("buildId")
    data = j.get("props", {}).get("pageProps", {}).get("data")
    if not build_id or not data:
        raise RuntimeError(f"{PAGE_URL} 的 __NEXT_DATA__ 结构变了：buildId={build_id!r} data={'有' if data else '无'}")
    prods = []
    for g in data.get("varietyListData", []):
        for p in g.get("productList", []):
            prods.append({
                "productId": p["productId"],
                "name": p["name"],
                "code": str(p["code"]).upper(),
                "exchange": p.get("qhExchangeName", ""),
                "unit": p.get("stockUnit", ""),
            })
    if not prods:
        raise RuntimeError(f"{PAGE_URL} 品种目录为空 —— 结构变化，不是「今天没有品种」")
    return build_id, prods


def _series(sess, build_id, pid):
    """返回该品种的 list[{date,stock,stockChange,close,...}]；404 ⇒ _StaleBuild。"""
    r = sess.get(DATA_URL.format(build_id=build_id, pid=pid), timeout=25)
    if r.status_code == 404:
        raise _StaleBuild(r.url)
    r.raise_for_status()
    ctype = r.headers.get("Content-Type", "")
    if "json" not in ctype:
        raise _StaleBuild(f"{r.url} Content-Type={ctype}")
    o = r.json()
    t = o.get("pageProps", {}).get("data", {}).get("positionTrendTableListData")
    if t is None:
        raise RuntimeError(f"pid={pid} 的响应里没有 positionTrendTableListData —— 结构变化")
    return t


def _metric(unit: str) -> str:
    if unit not in UNIT_SUFFIX:
        raise ValueError(
            f"未知单位 {unit!r}：UNIT_SUFFIX 里没登记。\n"
            f"  不许猜后缀（把千克当吨混进同一 metric，下游 sum 会算错且不报错）。"
            f"  先在 UNIT_SUFFIX 里显式登记。")
    return "inventory" + UNIT_SUFFIX[unit]


# ---------------------------------------------------------------------------
def fetch(date: str) -> list:
    d = str(date).replace("-", "")[:8]
    as_of = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    sess = _session()

    build_id, prods = _catalog(sess)
    store.save_raw(d, "inv_99qh_catalog", prods)

    raw_all, rows = {}, []
    empty, pseudo, null_cells, errors = [], [], [], []
    refreshed = False

    for p in prods:
        if any(k in p["name"] for k in PSEUDO_NAME_KEYS):
            pseudo.append(p["code"])
            continue
        try:
            try:
                t = _series(sess, build_id, p["productId"])
            except _StaleBuild:
                if refreshed:
                    raise RuntimeError(
                        f"pid={p['productId']} buildId 刷新后仍 404 —— 数据路由路径可能变了")
                build_id, prods2 = _catalog(sess)      # 重新部署 ⇒ buildId 变了
                refreshed = True
                store.save_raw(d, "inv_99qh_catalog", prods2, overwrite=True)
                t = _series(sess, build_id, p["productId"])
        except Exception as e:
            errors.append(f"{p['code']}(pid={p['productId']}): {type(e).__name__}: {str(e)[:80]}")
            time.sleep(0.1)
            continue

        lst = t.get("list") or []
        if not lst:
            # total=0 才是"真无数据"；list 空但 total>0 = 解析失败，必须炸
            if t.get("total", 0):
                errors.append(f"{p['code']}: list 为空但 total={t['total']}（解析失败）")
            else:
                empty.append(p["code"])
            time.sleep(0.1)
            continue

        raw_all[p["code"]] = {"productId": p["productId"], "unit": p["unit"],
                              "exchange": p["exchange"], "total": t.get("total"),
                              "list": lst}
        unit_m = _metric(p["unit"])
        for it in lst:
            iso = str(it.get("date", ""))[:10]
            if not iso or iso > as_of:          # 目标日之后的不写（保证"as of"语义）
                continue
            v = it.get("stock")
            if v is None:
                null_cells.append(f"{p['code']}@{iso}")
                continue
            pub, tag = store.resolve_published(SOURCE, iso)
            rows.append(pit.make_row(
                SOURCE, p["code"], unit_m, iso, iso, pub, float(v),
                raw_url=NOTE, published_src=tag))
        time.sleep(0.1)

    store.save_raw(d, "inv_99qh", raw_all)

    print(f"    品种 {len(prods)}：有数据 {len(raw_all)} / 空 {len(empty)} / 伪品种剔除 {len(pseudo)}")
    if empty:
        print(f"    空品种（源侧 total=0，非解析失败）：{','.join(empty)}")
    if pseudo:
        print(f"    伪品种（月均价，非库存序列）：{','.join(pseudo)}")

    # 三条"不许静默"的检查
    if errors:
        raise RuntimeError("以下品种抓取/解析失败（不许只写成功的部分，否则样本被系统性挑剩）：\n  "
                           + "\n  ".join(errors))
    if null_cells:
        raise RuntimeError(f"{len(null_cells)} 个期次 stock 为空：{null_cells[:10]}\n"
                           f"  跳过等于制造看不见的缺口，先确认是不是源侧改了字段。")
    if not rows:
        raise RuntimeError(f"{d} 未取到任何库存记录 —— 视为失败")
    return rows


# ---------------------------------------------------------------------------
def _selftest() -> int:
    import math
    ok = True
    print("=" * 72)
    print("fetch_inventory 自检（真实数据 20261016）")
    print("=" * 72)

    rows = fetch("20261016")

    # ① RB 最新一期数值对得上（实测 2026-10-16 螺纹钢 82947 吨）
    rb = [r for r in rows if r["symbol"] == "RB"]
    rb_latest = max(rb, key=lambda r: r["period_end"]) if rb else None
    print(f"  ① RB 行数={len(rb)} 最新期={rb_latest['period_end'] if rb else None} "
          f"值={rb_latest['value'] if rb_latest else None}（应 2026-10-16 / 82947.0）")
    ok &= bool(rb) and rb_latest["period_end"] == "2026-10-16" \
        and abs(rb_latest["value"] - 82947) < 1e-6

    # ② 单位不许混：黄金 千克 / 原木 立方米 / 原油 千桶 / 胶合板 张 必须各自成 metric
    au = {r["metric"] for r in rows if r["symbol"] == "AU"}
    lg = {r["metric"] for r in rows if r["symbol"] == "LG"}
    sc = {r["metric"] for r in rows if r["symbol"] == "SC"}
    bb = {r["metric"] for r in rows if r["symbol"] == "BB"}
    print(f"  ② AU={au} LG={lg} SC={sc} BB={bb}（应 inventory_kg/_m3/_kbbl/_sheet）")
    ok &= au == {"inventory_kg"} and lg == {"inventory_m3"} \
        and sc == {"inventory_kbbl"} and bb == {"inventory_sheet"}

    # ③ 吨品种必须是裸 inventory，且不能和 kg 混
    cu = {r["metric"] for r in rows if r["symbol"] == "CU"}
    n_metric = len({r["metric"] for r in rows})
    print(f"  ③ CU={cu}  全库 metric 种类={n_metric}（应 5）")
    ok &= cu == {"inventory"} and n_metric == 5

    # ④ GFEX 品种必须缺席（实测 total=0），且缺席不能是"被静默跳过"
    gfex = [r for r in rows if r["symbol"] in ("LC", "SI", "PS", "PT", "PD")]
    print(f"  ④ GFEX 品种行数={len(gfex)}（应 0：源侧无数据）")
    ok &= len(gfex) == 0

    # ⑤ 不许有前视：所有期次 <= 目标日
    maxp = max(r["period_end"] for r in rows)
    print(f"  ⑤ 最新期={maxp} ≤ 2026-10-16: {maxp <= '2026-10-16'}")
    ok &= maxp <= "2026-10-16"

    # ⑥ 公布时刻 = 各期自身日期 16:00（不是抓取日），且全 rule
    bad_pub = [r for r in rows
               if r["published_at"] != r["period_end"] + "T16:00:00"]
    print(f"  ⑥ published_at 与期次日不符的行={len(bad_pub)}（应 0）；"
          f"published_src 全 rule={all(r['published_src'] == 'rule' for r in rows)}")
    ok &= not bad_pub and all(r["published_src"] == "rule" for r in rows)

    # ⑦ 值域：不许出现 NaN/inf/负库存
    bad_val = [r for r in rows
               if r["value"] != r["value"] or math.isinf(r["value"]) or r["value"] < 0]
    print(f"  ⑦ 非法值行数={len(bad_val)}（应 0）  总行数={len(rows)}")
    ok &= not bad_val

    print("=" * 72)
    print("fetch_inventory 自检：" + ("通过 ✅" if ok else "失败 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    d = sys.argv[1] if len(sys.argv) > 1 else None
    if not d:
        raise SystemExit("用法: fetch_inventory.py YYYYMMDD | --selftest")
    raise SystemExit(store.run({"inventory": fetch}, d))
