# -*- coding: utf-8 -*-
"""
工具/_np_data/fetch_receipt.py —— 仓单日报 → np_data（D6/D7）

覆盖：SHFE（含 INE 5 项）/ CZCE / GFEX。DCE 不可得（见 `DCE可行性结论.md`）。

===============================================================================
三个会「算错但不报错」的坑（全部实测确认，不是猜的）
===============================================================================
1. **SHFE 的 ROWSTATUS 是字符串** '0'/'1'/'2'，不是整数。
   用 `df["ROWSTATUS"] == 2` 比对会全部落空、**静默返回 0**（实测：总计算成 0）。
   必须 `astype(str) == "0"`。

2. **不能用「总计」行，要对明细行求和。** 实测 2026-10-16：
   - 铜：ROWSTATUS=='2' 有 3 行（保税商品总计/完税商品总计/总计），**求和 = 72772 = 2×36386**
   - 螺纹钢厂库：**根本没有 ROWSTATUS=='2' 的行**，总计算出来是 0，而明细合计是 34500
   ⇒ 唯一对两种情形都成立的是 `sum(值 | ROWSTATUS=='0')`。

3. **同一品种被拆成「仓库」和「厂库」两个 key**（氧化铝/螺纹钢/纸浆/沥青/热卷/不锈钢/
   低硫燃料油/20号胶/丁二烯橡胶/胶版印刷纸）。必须**按品种合并**，否则厂库的量整个丢掉。

CZCE 另有独立坑：每个品种的 DataFrame 里混着 `小计`（24 个/品种）与 `总计`（1 个）行，
直接 sum 会重复数倍。按 `仓库编号` 剔除这两类再求和。

===============================================================================
单位（跨所不可加总，红线 R6）
===============================================================================
  SHFE / GFEX : WRTWGHTS / receipt = **吨**
  CZCE        : 仓单数量 = **张**（且商品不同每张对应的实物量不同）
⇒ `metric='receipt'` 的 value 只在本所内可比。跨所截面必须先做单位归一，
  未归一前的任何跨所排序都是「量纲噪音」而非信号。
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

METRIC = "receipt"

# ---------------------------------------------------------------------------
# 品种名 → 交易所品种代码（**未知 key 必须报错，不许静默跳过**）
# ---------------------------------------------------------------------------
SHFE_NAME2CODE = {
    "铜": "CU", "铝": "AL", "锌": "ZN", "铅": "PB", "镍": "NI", "锡": "SN",
    "铸造铝合金": "AD", "天然橡胶": "RU", "燃料油": "FU", "黄金": "AU", "白银": "AG",
    "线材": "WR", "中质含硫原油": "SC", "铜(BC)": "BC",
    # ★ 2026-10-20 补：**历史年份用无后缀的裸品种名**，与今天的"仓库/厂库"两 key 形态不同。
    #   实测 SHFE 2018-01-02 的 key 是 '热轧卷板'（裸）/'沥青仓库'+'沥青厂库'（旧名），
    #   2026-01-05 才变成 '热轧卷板仓库'+'热轧卷板厂库'。回填第一刀就撞在这上面
    #   （KeyError 是好事：它挡住了"该品种仓单凭空消失"，但必须显式登记别名才能继续）。
    "热轧卷板": "HC", "不锈钢": "SS", "氧化铝": "AO", "纸浆": "SP",
    "螺纹钢": "RB", "石油沥青": "BU", "沥青": "BU", "低硫燃料油": "LU",
    "20号胶": "NR", "丁二烯橡胶": "BR", "胶版印刷纸": "OP",
}
# 带仓库/厂库后缀的品种（key 形如 "螺纹钢仓库" / "螺纹钢厂库"）
# ★ 2026-10-20 补 '沥青'：2018~2020 的 key 是 '沥青仓库'/'沥青厂库'（旧名），
#   2022 起才改叫 '石油沥青仓库'/'石油沥青厂库'。两个名字都必须在表里。
SHFE_SUFFIXED = {
    "氧化铝": "AO", "丁二烯橡胶": "BR", "纸浆": "SP", "胶版印刷纸": "OP",
    "石油沥青": "BU", "沥青": "BU", "螺纹钢": "RB", "热轧卷板": "HC",
    "不锈钢": "SS", "低硫燃料油": "LU", "20号胶": "NR",
}
CZCE_NAME2CODE = {"PTA": "TA"}      # 其余 key 本身就是代码


def _shfe_code(key: str) -> str:
    if key in SHFE_NAME2CODE:
        return SHFE_NAME2CODE[key]
    for base, code in SHFE_SUFFIXED.items():
        if key.startswith(base) and key[len(base):] in ("仓库", "厂库"):
            return code
    raise KeyError(
        f"SHFE 仓单出现未登记的品种 key：{key!r}\n"
        f"  交易所新增品种时必须显式加进 SHFE_NAME2CODE / SHFE_SUFFIXED。\n"
        f"  静默跳过会让该品种的仓单凭空消失，而且没有任何报错。")


def _num(v) -> float:
    """把 '63,289' / '1 234' / '' 之类转成 float。空值 → NaN（不静默变 0）。"""
    import math
    if v is None:
        return float("nan")
    s = str(v).replace(",", "").replace(" ", "").strip()
    if s in ("", "-", "--", "nan", "None"):
        return float("nan")
    try:
        return float(s)
    except ValueError:
        return float("nan")


def _rows_for(source: str, date: str, symbol: str, value, url: str = "") -> dict | None:
    pub, src_tag = store.resolve_published(source, date)
    if value is None:
        return None
    import math
    if math.isnan(value):
        return None                       # 只跳过「该品种当日确实无数」，不跳过解析失败
    iso = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
    return pit.make_row(source, symbol, METRIC, iso, iso, pub, float(value),
                        raw_url=url or store.RULE_NOTE.get(source, ""),
                        published_src=src_tag)


# ---------------------------------------------------------------------------
# 各所
# ---------------------------------------------------------------------------
def _fetch_shfe(date: str) -> list:
    import akshare as ak
    obj = ak.futures_shfe_warehouse_receipt(date=date)
    store.save_raw(date, "shfe_receipt", store.df_to_records(obj))

    acc: dict[str, float] = {}
    keys_by_code: dict[str, list] = {}
    detail_rows = 0
    for key, df in obj.items():
        code = _shfe_code(key)
        keys_by_code.setdefault(code, []).append(key)
        # ★ ROWSTATUS 是字符串；'0' 才是明细行（见模块头注释坑 1、2）
        mask = df["ROWSTATUS"].astype(str).str.strip() == "0"
        sub = df[mask]
        detail_rows += len(sub)
        tot = sum(_num(x) for x in sub["WRTWGHTS"].tolist())
        acc[code] = acc.get(code, 0.0) + (0.0 if tot != tot else tot)   # NaN 归 0

    # ★ 2026-10-20 新增：同一品种**同时**出现裸名与「仓库/厂库」名 ⇒ 会把量算两遍。
    #   历史年份是裸名（2018 '热轧卷板'），近年才拆仓库/厂库（2026 '热轧卷板仓库'+'厂库'），
    #   两者从未在同一天共存 —— 一旦共存就是交易所改了结构，必须人看，不许照样相加。
    for code, ks in keys_by_code.items():
        plain = [k for k in ks if not k.endswith(("仓库", "厂库"))]
        suffixed = [k for k in ks if k.endswith(("仓库", "厂库"))]
        if plain and suffixed:
            raise RuntimeError(
                f"SHFE 品种 {code} 同一天既有无后缀 key {plain} 又有仓库/厂库 key {suffixed} —— "
                f"照原样相加会让仓单翻倍。请先确认该日的正确口径再改映射。")

    if detail_rows == 0:
        raise RuntimeError(
            "SHFE 仓单没有任何 ROWSTATUS=='0' 的明细行 —— "
            "要么交易所改了口径，要么 ROWSTATUS 类型变了。拒绝当成「仓单为 0」写入。")

    rows = []
    for code, v in sorted(acc.items()):
        r = _rows_for("shfe_receipt", date, code, v)
        if r:
            rows.append(r)
    return rows


def _pick_col(df, candidates):
    """返回第一个存在的列名；都不存在返回 None。"""
    for c in candidates:
        if c in df.columns:
            return c
    return None


# CZCE 的编号列有三种叫法；数量列的口径差别更大（见 _fetch_czce 注释）
_ID_COLS = ("仓库编号", "机构编号", "厂库编号")
# ★ 只有这些才是「仓单数量」口径。`确认书数量` / `预报数量` 是**别的指标**，
#   拿它们当仓单等于偷换（红线 R6）。
_QTY_COLS = ("仓单数量",)
_QTY_SPLIT = ("仓单数量(完税)", "仓单数量(保税)")
# 不是仓单、但确实存在的其他口径 —— 记录但不写 receipt
_OTHER_QTY = ("确认书数量", "预报数量")
_SKIP_IDS = {"小计", "总计", "合计", "nan", "None", ""}


def _sum_cols(df, mask, cols) -> tuple:
    """按 mask 取行、把 cols 的数量相加。返回 (合计, 是否至少命中一个非 NaN 值)。"""
    total, hit = 0.0, False
    for col, sign in cols:
        for x in df[mask][col].tolist():
            v = _num(x)
            if v == v:                       # 跳过 NaN，不当 0
                total += sign * v
                hit = True
    return total, hit


def _czce_total(df, id_col, cols, code: str) -> tuple:
    """CZCE 单品种仓单量：**优先用交易所自己的「总计」行**，退化为 Σ小计，再退化为 Σ明细。

    ★★ 2026-10-20 修一个 2× 静默错误（与独立历史 CSV 交叉核对时发现）：
       原实现是「剔除 小计/总计 后对明细求和」。对 **MA（甲醇）** 这不成立 ——
       MA 的 DataFrame 末尾除了 `总计` 行，**还有一行 仓库编号=NaN、数量=总计值**，
       原实现把它当明细加进去 ⇒ MA 仓单**恒为真值 2 倍**（实测 2026-10-16：
       写到库里 13166，交易所总计 6583；2021-07-01：3334 vs 1667；2018-11-01：28 vs 14）。
       而 SR/CF 等品种的 NaN 编号行是**合法的品牌明细行**（一个仓库下多个品牌），
       所以「凡是 NaN 就丢掉」又会少算（SR 会从 24840 掉到 9771）。
       ⇒ 唯一两头都不错的判据是：**用 总计 行**（交易所自己算好的），并断言 总计 == Σ小计。
       实测 6 个年份（2018/2019/2020/2022/2024/2026）× 全品种：总计 == Σ小计 无一例外。

    返回 (value, 口径名, 是否命中)。
    """
    ids = df[id_col].astype(str).str.strip()
    is_total = ids == "总计"
    is_sub = ids == "小计"

    v_total, hit_total = _sum_cols(df, is_total, cols)
    v_sub, hit_sub = _sum_cols(df, is_sub, cols)

    if bool(is_total.any()):
        # 自洽断言：交易所的总计必须等于小计之和。不等 ⇒ 结构变了，必须人看。
        if bool(is_sub.any()) and abs(v_total - v_sub) > 1e-6:
            raise RuntimeError(
                f"CZCE {code} 总计({v_total}) ≠ 小计之和({v_sub}) —— "
                f"交易所表结构或口径变了，拒绝二选一，先核实再改。")
        return v_total, "总计", hit_total
    if bool(is_sub.any()):
        # 极少数品种/年份没有总计行（实测 SM 2020-01-02）
        return v_sub, "小计", hit_sub
    v_all, hit_all = _sum_cols(df, ~ids.isin(_SKIP_IDS), cols)
    return v_all, "明细", hit_all


def _fetch_czce(date: str) -> list:
    """CZCE 仓单。

    ★ 实测 2026-10-16：27 个品种有 **26 种不同的列组合**，不能一套聚合走到底。
      编号列：仓库编号 / 机构编号 / 厂库编号
      数量列：仓单数量 / 仓单数量(完税)+仓单数量(保税) / 确认书数量 / 预报数量
      其中 **AP（苹果）、ZC 只有「预报数量」**，那是预报量不是仓单 ⇒ 不能当 receipt（R6）。
    """
    import akshare as ak
    obj = ak.futures_warehouse_receipt_czce(date=date)
    store.save_raw(date, "czce_receipt", store.df_to_records(obj))

    rows, zero_codes, non_receipt, basis = [], [], [], {}
    for key, df in obj.items():
        code = CZCE_NAME2CODE.get(key, key)
        id_col = _pick_col(df, _ID_COLS)
        qty_col = _pick_col(df, _QTY_COLS)

        if id_col is None:
            raise KeyError(
                f"CZCE 品种 {key} 找不到编号列（试过 {_ID_COLS}）：{list(df.columns)}\n"
                f"  拒绝继续——盲猜编号列会让「小计」行混进合计。")

        if qty_col is not None:
            cols = [(qty_col, 1.0)]
        elif all(c in df.columns for c in _QTY_SPLIT):
            cols = [(_QTY_SPLIT[0], 1.0), (_QTY_SPLIT[1], 1.0)]   # 完税 + 保税
        else:
            # 只有 确认书数量 / 预报数量 ⇒ 不是仓单，显式记下来，不写 receipt
            other = _pick_col(df, _OTHER_QTY)
            non_receipt.append(f"{code}({other})" if other else f"{code}(?col)")
            continue

        total, how, hit = _czce_total(df, id_col, cols, code)
        basis[how] = basis.get(how, 0) + 1
        if not hit:
            zero_codes.append(code)
            total = 0.0
        rows.append(_rows_for("czce_receipt", date, code, total))

    # 口径必须打得出来：这一行就是「哪些品种用了总计 / 小计 / 明细」的证据
    print(f"    CZCE 聚合口径：{basis}")
    if zero_codes:
        print(f"    CZCE 当日 0 仓单 {len(zero_codes)} 个：{','.join(zero_codes)}（记 0，不跳过）")
    if non_receipt:
        print(f"    CZCE 非仓单口径 {len(non_receipt)} 个（预报/确认书，按 R6 不写 receipt）："
              f"{','.join(non_receipt)}")
    return [r for r in rows if r]


def _fetch_gfex(date: str) -> list:
    from akshare.futures import receipt as R
    obj = R.get_gfex_receipt(date=date, vars_list=["SI", "LC", "PS"])
    store.save_raw(date, "gfex_receipt", store.df_to_records(obj))
    rows = []
    for _, r in obj.iterrows():
        v = _num(r["receipt"])
        if v != v:
            continue
        rows.append(_rows_for("gfex_receipt", date, str(r["var"]).upper(), v))
    return [r for r in rows if r]


def fetch(date: str) -> list:
    rows = []
    rows += _fetch_shfe(date)
    rows += _fetch_czce(date)
    rows += _fetch_gfex(date)
    return rows


# ---------------------------------------------------------------------------
# 自检：在真实数据日上验证聚合口径
# ---------------------------------------------------------------------------
def _selftest() -> int:
    ok = True
    print("=" * 72)
    print("fetch_receipt 自检（真实数据 20261016，验证聚合口径而非连接性）")
    print("=" * 72)

    import akshare as ak
    obj = ak.futures_shfe_warehouse_receipt(date="20261016")
    df = obj["铜"]
    detail = df[df["ROWSTATUS"].astype(str) == "0"]["WRTWGHTS"].sum()
    wrong_int = df[df["ROWSTATUS"] == 2]["WRTWGHTS"].sum()
    print(f"  ① 铜 明细合计={detail} | 用整数 2 比对总计={wrong_int}（应为 0 ⇒ 证明坑 1 存在）")
    ok &= wrong_int == 0 and detail > 0

    rows = fetch("20261016")
    by_src: dict[str, int] = {}
    for r in rows:
        by_src[r["source"]] = by_src.get(r["source"], 0) + 1
    print(f"  ② 抓取 {len(rows)} 行：{by_src}")

    # 螺纹钢 = 仓库 82947 + 厂库 34500 = 117447（实测值，写死用于回归）
    rb = [r for r in rows if r["source"] == "shfe_receipt" and r["symbol"] == "RB"]
    print(f"  ③ RB 仓单={rb[0]['value'] if rb else None}（应 117447 = 仓库82947+厂库34500）")
    ok &= bool(rb) and abs(rb[0]["value"] - 117447) < 1e-6

    cu = [r for r in rows if r["source"] == "shfe_receipt" and r["symbol"] == "CU"]
    print(f"  ④ CU 仓单={cu[0]['value'] if cu else None}（应 36386，不是翻倍的 72772）")
    ok &= bool(cu) and abs(cu[0]["value"] - 36386) < 1e-6

    # ★★ CZCE 的 2× 坑（2026-10-20 用独立历史 CSV 交叉核对时发现）：
    #   MA 的 df 末尾有一行 仓库编号=NaN、数量=总计值 ⇒ 用「剔除小计/总计后求明细」
    #   会把它加进去，恒为真值 2 倍。而 SR 的 NaN 编号行是合法品牌明细 ⇒ 又不能一律丢。
    #   正确判据 = 用「总计」行。下面三条断言把这件事钉住，改回去就会红。
    cz = ak.futures_warehouse_receipt_czce(date="20261016")
    for code, want in (("MA", 6583.0), ("SR", 24840.0)):
        df = cz[code]
        cols = [("仓单数量", 1.0)] if "仓单数量" in df.columns else \
               [(c, 1.0) for c in _QTY_SPLIT if c in df.columns]
        got, how, _ = _czce_total(df, _pick_col(df, _ID_COLS), cols, code)
        print(f"  ⑤ CZCE {code} 按「{how}」= {got:.0f}（应 {want:.0f}）")
        ok &= abs(got - want) < 1e-6 and how == "总计"

    # ⑤-b 反证：旧的「剔除小计/总计后求明细」对 MA 恰好是 2 倍
    df = cz["MA"]
    _mc = [("仓单数量", 1.0)] if "仓单数量" in df.columns else \
          [(c, 1.0) for c in _QTY_SPLIT if c in df.columns]
    ids = df[_pick_col(df, _ID_COLS)].astype(str).str.strip()
    old, _ = _sum_cols(df, ~ids.isin(_SKIP_IDS), _mc)
    print(f"  ⑤-b MA 旧口径(明细求和)={old:.0f} = 2×{old/2:.0f}（旧口径必错，这不是巧合）")
    ok &= abs(old - 13166.0) < 1e-6 and abs(old - 2 * 6583.0) < 1e-6

    # ⑤-c 另一种错法：只取「编号是纯数字」的行 ⇒ SR 会少算（品牌行编号为 NaN）
    df = cz["SR"]
    ids = df[_pick_col(df, _ID_COLS)].astype(str).str.strip()
    digit_only, _ = _sum_cols(df, ids.str.fullmatch(r"\d+"), [("仓单数量", 1.0)])
    print(f"  ⑤-c SR 只取数字编号={digit_only:.0f}（应 < 24840 ⇒ 证明不能靠丢 NaN 行来修）")
    ok &= digit_only < 24840.0

    # ⑤-d 自洽断言真的会炸：人为构造 总计≠小计 必须抛错
    import pandas as pd
    bad = pd.DataFrame({"仓库编号": ["0001", "小计", "总计"], "仓单数量": [1.0, 1.0, 999.0]})
    try:
        _czce_total(bad, "仓库编号", [("仓单数量", 1.0)], "TEST")
        print("  ⑤-d ★ 总计≠小计 竟然没报错")
        ok = False
    except RuntimeError:
        print("  ⑤-d 总计≠小计 ⇒ RuntimeError ✅（拒绝二选一）")

    print("=" * 72)
    print("fetch_receipt 自检：" + ("通过 ✅" if ok else "失败 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    d = sys.argv[1] if len(sys.argv) > 1 else None
    if not d:
        raise SystemExit("用法: fetch_receipt.py YYYYMMDD | --selftest")
    raise SystemExit(store.run({"receipt": fetch}, d))
