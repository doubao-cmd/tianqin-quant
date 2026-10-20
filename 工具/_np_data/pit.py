# -*- coding: utf-8 -*-
"""
工具/_np_data/pit.py —— 非价格数据的 PIT（point-in-time）层

===============================================================================
为什么需要这一层
===============================================================================
价格数据天然没有 PIT 问题：bar 的时间戳**就是**可用时刻。
非价格数据全都有时间差，而用错**不会报错、不会跑崩，只会让回测虚高**：

| 数据           | 真实可用时刻                     |
|----------------|----------------------------------|
| 仓单日报       | 当日收盘后（约 15:30~16:00）      |
| 会员持仓排名   | T+1 公布（保守假设，待核实）      |
| 库存周报       | 周三公布上周数据                  |
| 现货报价       | 时刻不明 ⇒ 直接排除               |

本模块的唯一职责：**让"用错时间"变成一件写不出来的事**，
而不是靠人记得写 `available_at <= t`。

===============================================================================
三时刻定义（写死，不许中途改）
===============================================================================
  period_start / period_end : 数据**描述**的时段（单日数据 start=end；周报是周区间）
  published_at              : 交易所**公布**时刻，必须含时分（'YYYY-MM-DDTHH:MM:SS'）
  available_at              : published_at + margin_sec（默认 3600s），写入时算好存下

全部时间为**北京时间（UTC+8）的 naive 字符串**，统一 `isoformat(sep='T', timespec='seconds')`。
因此字符串比较 == 时间比较（sqlite 用 BINARY collation）。
注意：这意味着**全库只允许一个时区**。引入 UTC 会让字典序比较静默出错。

===============================================================================
四条硬约束
===============================================================================
1. **只用 `available_at <= as_of`** —— 唯一合法的对齐方式。
   `query_available()` 的 `as_of` 是**必填位置参数**：不传就 TypeError，
   想"先取全量再自己 join"必须先调用名字很难看的 `read_all_for_audit()`。
2. **禁止按 period 日期 join** —— 由 PIT-4 静态扫描兜底（见 `_t_pit.py`）。
3. **只追加不覆盖** —— 同主键再次写入且 value 不同 ⇒ 抛异常；
   要改值必须显式抬 `revision`（`next_revision()`）。这是 store.py 的同一条原则。
4. **结构断言在写入口** —— `published_at >= period_end` 等三条 CHECK + Python 侧预校验。
   数据不可能在时段结束**之前**公布；违反就是字段填错了，当场炸掉比事后查便宜。

===============================================================================
表结构（相对清单原稿改了 4 处，理由见行尾注释）
===============================================================================
  (source, symbol, contract, metric, period_start, period_end, revision) 为主键
  + contract          : 会员持仓排名是**按合约**公布的（rb2601），不是按品种
  + period_start/end  : 库存周报是区间，单个 DATE 装不下
  + revision          : 口径会修订（清单 §1.4），必须能存两版且不覆盖旧版
  + margin_sec        : 安全边际是**逐源**的，硬编码 1h 会给出假的安全感；存下来可审计

主键为什么不含 fetched_at：同一次抓取的幂等重跑不该产生新行（会污染"只增"判据）。
抓取时刻存在列里，不参与去重。
"""
from __future__ import annotations

import math
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent            # 工具/_np_data
DB_PATH = _HERE / "np.db"                          # 默认库；可用参数/环境变量覆盖

ENV_DB = "NP_DB"


def db_path_default() -> Path:
    """默认库路径（允许用环境变量 NP_DB 指向别处，便于多环境切换）。"""
    v = os.environ.get(ENV_DB)
    return Path(v) if v else DB_PATH


# ---------------------------------------------------------------------------
# 安全边际：逐源配置
# ---------------------------------------------------------------------------
DEFAULT_MARGIN_SEC = 3600      # 未登记的数据源一律 +1h（保守）

# ★ 这张表的语义是"**在已核实公布时刻之上**再加多少"，不是"公布时刻"本身。
#   published_at 必须填核实到的真实时刻（精确到时分），margin 只是防抖缓冲。
#   阶段 1 核实完每个源的真实公布时刻后，在这里逐源收紧 margin。
MARGIN_SEC = {
    "shfe_receipt":         3600,   # 仓单日报：约 15:30 后公布
    "dce_receipt":          3600,   # 大商所仓单（若打得通）
    "czce_receipt":         3600,
    "ine_receipt":          3600,
    "gfex_receipt":         3600,
    "shfe_rank":            3600,   # 会员持仓排名
    "dce_rank":             3600,
    "czce_rank":            3600,
    "czce_weekly_stock":    3600,   # 库存周报：区间数据，跨日已是硬边界
    "spot_basis":           3600,
}


def margin_for(source: str) -> int:
    return int(MARGIN_SEC.get(str(source), DEFAULT_MARGIN_SEC))


# ---------------------------------------------------------------------------
# 时间规范化（全库唯一入口，别在别处手搓字符串）
# ---------------------------------------------------------------------------
def norm_dt(v, field: str = "dt") -> str:
    """规范化成 'YYYY-MM-DDTHH:MM:SS'。

    ★ date-only 直接拒绝。清单 §1.2 要求"公布时刻精确到时分"；
      允许 '2026-10-18' 会被悄悄补成 00:00，等于凭空多给 15 小时可用性。
    """
    if v is None:
        raise ValueError(f"{field} 为空")
    if isinstance(v, datetime):
        return v.replace(microsecond=0).isoformat(sep="T", timespec="seconds")
    s = str(v).strip()
    if len(s) <= 10 and ("T" not in s and " " not in s):
        raise ValueError(
            f"{field} 只给了日期没有时分：{s!r}\n"
            f"  清单 §1.2 要求公布时刻精确到时分；不知道时分就去核实，不许猜。")
    dt = datetime.fromisoformat(s.replace(" ", "T"))
    return dt.replace(microsecond=0).isoformat(sep="T", timespec="seconds")


def norm_date(v, field: str = "date") -> str:
    """规范化成 'YYYY-MM-DD'。"""
    if v is None:
        raise ValueError(f"{field} 为空")
    if isinstance(v, datetime):
        return v.date().isoformat()
    s = str(v).strip()
    if "T" in s or " " in s:
        s = s.replace(" ", "T").split("T")[0]
    return datetime.fromisoformat(s).date().isoformat()


def available_at_for(published_at, source: str, margin_sec: int | None = None) -> str:
    """available_at = published_at + margin。margin 未给则查 MARGIN_SEC。"""
    pub = norm_dt(published_at, "published_at")
    m = margin_for(source) if margin_sec is None else int(margin_sec)
    if m < 0:
        raise ValueError(f"margin_sec 不能为负：{m}")
    return (datetime.fromisoformat(pub) + timedelta(seconds=m)).isoformat(
        sep="T", timespec="seconds")


# ---------------------------------------------------------------------------
# 建表
# ---------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS np_data (
    source       TEXT    NOT NULL,
    symbol       TEXT    NOT NULL,
    contract     TEXT    NOT NULL DEFAULT '',
    metric       TEXT    NOT NULL,
    period_start TEXT    NOT NULL,
    period_end   TEXT    NOT NULL,
    published_at TEXT    NOT NULL,
    available_at TEXT    NOT NULL,
    margin_sec   INTEGER NOT NULL,
    -- 这个 published_at 是「实测出来的」还是「按规则假设的」——审计时必须能查。
    -- 'measured' = probe_sources.py --watch 打出的 first_seen_at（有证据）
    -- 'rule'     = 按交易所惯例填的保守值（无实测证据，允许但必须可见）
    -- 'manual'   = 人工指定（需在 note/文档里写理由）
    published_src TEXT   NOT NULL DEFAULT 'rule',
    value        REAL,
    revision     INTEGER NOT NULL DEFAULT 0,
    raw_url      TEXT    NOT NULL DEFAULT '',
    fetched_at   TEXT    NOT NULL,

    PRIMARY KEY (source, symbol, contract, metric, period_start, period_end, revision),

    -- 数据不可能在时段结束"之前"公布（字典序比较，前提是格式统一）
    CHECK (published_at >= period_end),
    CHECK (available_at >= published_at),
    CHECK (period_start <= period_end),
    CHECK (revision >= 0)
);

CREATE INDEX IF NOT EXISTS ix_np_avail  ON np_data (available_at);
CREATE INDEX IF NOT EXISTS ix_np_key    ON np_data (source, symbol, metric, period_end);
CREATE INDEX IF NOT EXISTS ix_np_contract ON np_data (contract);

-- 数据源登记表：阶段 1 的核实结果直接落这里（而不是一张散落的 md）
CREATE TABLE IF NOT EXISTS np_source (
    source        TEXT PRIMARY KEY,
    category      TEXT NOT NULL,          -- 仓单日报 / 会员持仓排名 / 库存周报 / 现货基差
    exchange      TEXT NOT NULL,          -- SHFE / DCE / CZCE / INE / GFEX / 3rd
    published_rule TEXT NOT NULL,         -- 'T日15:30' / 'T+1 15:40' / '周三16:00报上周'
    history_depth TEXT NOT NULL DEFAULT '',
    fetch_method  TEXT NOT NULL DEFAULT '',
    anti_crawl    TEXT NOT NULL DEFAULT '',
    need_login    INTEGER NOT NULL DEFAULT 0,
    verified      TEXT NOT NULL DEFAULT '',   -- '✅' / '❌' / '🟡' / ''（空=未核实）
    note          TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL
);
"""


def connect(db_path=None) -> sqlite3.Connection:
    """打开（并按需初始化）库。check_same_thread=False 仅为方便脚本内复用。"""
    p = Path(db_path) if db_path else db_path_default()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn) -> None:
    """轻量迁移：老库缺列就补。只加列不改列，保证已写入的数据不动。"""
    have = {r[1] for r in conn.execute("PRAGMA table_info(np_data)")}
    if "published_src" not in have:
        conn.execute("ALTER TABLE np_data ADD COLUMN published_src TEXT NOT NULL DEFAULT 'rule'")
        conn.commit()


# ---------------------------------------------------------------------------
# 行校验（写入口的结构断言）
# ---------------------------------------------------------------------------
_REQUIRED = ("source", "symbol", "metric", "period_start", "period_end", "published_at")
_PUBLISHED_SRC = {"measured", "rule", "manual"}


def make_row(source, symbol, metric, period_start, period_end, published_at, value,
             contract="", revision=0, raw_url="", margin_sec=None,
             available_at=None, fetched_at=None, published_src="rule") -> dict:
    """构造一条规范化的记录（时间和 available_at 都算好）。

    `available_at` 一般不要传；传了也必须 >= published_at，否则报错。
    `published_src` 必须坦白公布时刻的来源：measure / rule / manual（见 _SCHEMA 注释）。
    """
    if published_src not in _PUBLISHED_SRC:
        raise ValueError(
            f"published_src 只能是 {sorted(_PUBLISHED_SRC)}：{published_src!r}\n"
            f"  不知道这个时刻是怎么来的，就没法判断这条数据能不能用于回测。")
    pub = norm_dt(published_at, "published_at")
    ps = norm_date(period_start, "period_start")
    pe = norm_date(period_end, "period_end")
    if pe < ps:
        raise ValueError(f"period_end({pe}) < period_start({ps})")
    if pub[:10] < pe:
        raise ValueError(
            f"published_at({pub}) 早于 period_end({pe})："
            f"数据不可能在时段结束前公布 —— 字段填错了（多半是把 period 填成了公布日）。")
    padded = available_at_for(pub, source, margin_sec)
    if available_at is not None:
        avail = norm_dt(available_at, "available_at")
        if avail < padded:
            raise ValueError(
                f"显式 available_at({avail}) 小于 published_at+margin({padded})："
                f"不要在写入口偷减安全边际。要收紧请改 MARGIN_SEC 并写清理由。")
    else:
        avail = padded
    return {
        "source": str(source).strip(),
        "symbol": str(symbol).strip().upper(),
        "contract": str(contract or "").strip().lower(),
        "metric": str(metric).strip(),
        "period_start": ps,
        "period_end": pe,
        "published_at": pub,
        "available_at": avail,
        "margin_sec": margin_for(source) if margin_sec is None else int(margin_sec),
        "published_src": published_src,
        "value": value,
        "revision": int(revision),
        "raw_url": str(raw_url or ""),
        "fetched_at": norm_dt(fetched_at, "fetched_at") if fetched_at
                      else datetime.now().replace(microsecond=0).isoformat(
                          sep="T", timespec="seconds"),
    }


PK_COLS = ("source", "symbol", "contract", "metric",
           "period_start", "period_end", "revision")


def validate(row: dict) -> dict:
    """把 dict 校验并补全成可写入的行。缺字段/时序矛盾 ⇒ 直接抛。"""
    r = dict(row)
    for k in _REQUIRED:
        if k not in r or r[k] in (None, ""):
            raise ValueError(f"缺必填字段 {k!r}：{ {x: r.get(x) for x in _REQUIRED} }")

    # 统一重新算一遍（不信任调用方算的 available_at/margin 组合）
    fixed = make_row(
        source=r["source"], symbol=r["symbol"], metric=r["metric"],
        period_start=r["period_start"], period_end=r["period_end"],
        published_at=r["published_at"], value=r.get("value"),
        contract=r.get("contract", ""), revision=r.get("revision", 0),
        raw_url=r.get("raw_url", ""), margin_sec=r.get("margin_sec"),
        available_at=r.get("available_at"), fetched_at=r.get("fetched_at"),
        published_src=r.get("published_src", "rule"),
    )

    if fixed["period_end"] < fixed["period_start"]:
        raise ValueError(f"period_end < period_start：{fixed}")
    if fixed["published_at"][:10] < fixed["period_end"]:
        raise ValueError(
            f"published_at({fixed['published_at']}) 早于 period_end({fixed['period_end']})："
            f"数据不可能在时段结束前公布 —— 字段填错了。")
    v = fixed["value"]
    if v is not None:
        try:
            fv = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"value 非数值：{v!r}")
        if math.isnan(fv) or math.isinf(fv):
            raise ValueError(f"value 非有限数：{v!r}")
        fixed["value"] = fv
    return fixed


# ---------------------------------------------------------------------------
# 写入：只追加不覆盖
# ---------------------------------------------------------------------------
def next_revision(conn, row: dict) -> int:
    """同一 (source,symbol,contract,metric,period_start,period_end) 的下一个版本号。"""
    q = """SELECT MAX(revision) AS m FROM np_data
           WHERE source=? AND symbol=? AND contract=? AND metric=?
             AND period_start=? AND period_end=?"""
    cur = conn.execute(q, (row["source"], row["symbol"], row["contract"],
                           row["metric"], row["period_start"], row["period_end"]))
    m = cur.fetchone()["m"]
    return 0 if m is None else int(m) + 1


def write_records(rows, db_path=None, on_conflict: str = "raise") -> dict:
    """写入一批记录。

    幂等：同主键 + 同 value/available_at/raw_url ⇒ 跳过（不算新增）。
    冲突：同主键 + 不同 value ⇒ 默认**抛异常**，必须显式抬 revision。

    on_conflict:
      "raise"（默认）—— 冲突即抛，防止静默改历史
      "skip"          —— 冲突跳过（补历史时用，不推荐日更用）
    """
    if on_conflict not in ("raise", "skip"):
        raise ValueError(f"on_conflict 只能是 raise/skip：{on_conflict!r}")

    conn = connect(db_path)
    inserted = unchanged = skipped = 0
    conflicts = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        for raw in rows:
            r = validate(raw)
            pk = tuple(r[k] for k in PK_COLS)
            cur = conn.execute(
                "SELECT value, available_at, raw_url, published_at FROM np_data "
                "WHERE " + " AND ".join(f"{k}=?" for k in PK_COLS), pk)
            old = cur.fetchone()
            if old is not None:
                same = (old["value"] == r["value"]
                        and old["available_at"] == r["available_at"]
                        and old["raw_url"] == r["raw_url"]
                        and old["published_at"] == r["published_at"])
                if same:
                    unchanged += 1
                    continue
                if on_conflict == "skip":
                    skipped += 1
                    continue
                conflicts.append((pk, old["value"], r["value"]))
                continue
            cols = list(r.keys())
            conn.execute(
                f"INSERT INTO np_data ({','.join(cols)}) "
                f"VALUES ({','.join('?' * len(cols))})",
                [r[c] for c in cols])
            inserted += 1

        if conflicts:
            conn.rollback()
            head = "\n".join(f"  {pk}  旧={ov!r}  新={nv!r}" for pk, ov, nv in conflicts[:5])
            raise ValueError(
                "★ 同主键已存在且 value 不同 —— 拒绝覆盖（这会悄悄改掉历史）。\n"
                f"{head}\n"
                "  这是「修订」不是「重跑」：请用 next_revision() 抬版本号后重写，"
                "或确认旧值错了再手工处理。")
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

    return {"inserted": inserted, "unchanged": unchanged, "skipped": skipped}


# ---------------------------------------------------------------------------
# 查询：★ as_of 必填
# ---------------------------------------------------------------------------
_SELECT_COLS = ("source, symbol, contract, metric, period_start, period_end, "
                "published_at, available_at, margin_sec, published_src, value, revision, raw_url")


def query_available(as_of, source=None, symbol=None, contract=None, metric=None,
                    db_path=None, as_df: bool = False):
    """取 `available_at <= as_of` 的记录。**as_of 是必填位置参数，没有默认值。**

    ★ 为什么强制传 as_of：不传就等于"取全量"，而取全量之后的每一次 join
      都是一个未来函数入口。把接口做成"拿不到全量"，比写十遍注释有用。

    返回 list[dict]（as_df=True 时返回 DataFrame），按 available_at 排序。
    """
    t = norm_dt(as_of, "as_of")
    sql = f"SELECT {_SELECT_COLS} FROM np_data WHERE available_at <= ?"
    args = [t]
    for col, val in (("source", source), ("symbol", symbol),
                     ("contract", contract), ("metric", metric)):
        if val is not None:
            sql += f" AND {col} = ?"
            args.append(str(val).upper() if col == "symbol" else str(val))
    sql += " ORDER BY available_at, source, symbol, contract, metric, period_end"
    conn = connect(db_path)
    try:
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()
    if as_df:
        import pandas as pd
        return pd.DataFrame(rows)
    return rows


def latest_snapshot(as_of, db_path=None, **filters):
    """每个 (source,symbol,contract,metric) 在 as_of 时点的**最新一版**取值。

    这就是策略层该拿的东西：一个时点一张快照，不需要自己排序去重。
    """
    rows = query_available(as_of, db_path=db_path, **filters)
    best = {}
    for r in rows:
        k = (r["source"], r["symbol"], r["contract"], r["metric"])
        if k not in best or (r["period_end"], r["revision"]) > (
                best[k]["period_end"], best[k]["revision"]):
            best[k] = r
    return [best[k] for k in sorted(best)]


def read_all_for_audit(db_path=None) -> list:
    """⚠ 取全量（不按 as_of 过滤）—— **只给核对/审计用，禁止在策略代码里调用。**

    唯一合法用途：抽样核对交易所官网、写清单表、跑 PIT-1 的对照侧。
    """
    conn = connect(db_path)
    try:
        return [dict(r) for r in conn.execute(
            f"SELECT {_SELECT_COLS} FROM np_data ORDER BY available_at")]
    finally:
        conn.close()


def stats(db_path=None) -> dict:
    """库概况：总行数、源数、时间跨度、每源行数。"""
    conn = connect(db_path)
    try:
        n = conn.execute("SELECT COUNT(*) c FROM np_data").fetchone()["c"]
        srcs = [dict(r) for r in conn.execute(
            "SELECT source, COUNT(*) n, MIN(period_end) p0, MAX(period_end) p1, "
            "MIN(published_at) pub0, MAX(published_at) pub1 "
            "FROM np_data GROUP BY source ORDER BY source")]
        return {"rows": n, "sources": srcs}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 自检（只碰临时库）
# ---------------------------------------------------------------------------
def _selftest() -> int:
    import shutil
    import tempfile

    print("=" * 72)
    print("PIT 层自检（写入幂等 / 冲突拒绝 / as_of 强制 / 未来不可见）")
    print("=" * 72)
    ok = True
    tmp = Path(tempfile.mkdtemp(prefix="pit_"))
    db = tmp / "t.db"
    try:
        r = make_row("shfe_receipt", "rb", "receipt", "2026-10-12", "2026-10-12",
                     "2026-10-12 15:30", 120000, raw_url="u1")
        s1 = write_records([r], db_path=db)
        print(f"  ① 首次写入：{s1}")
        ok &= s1["inserted"] == 1

        s2 = write_records([r], db_path=db)
        print(f"  ② 重跑同一批（幂等）：{s2}")
        ok &= s2["inserted"] == 0 and s2["unchanged"] == 1

        r2 = dict(r, value=999)
        try:
            write_records([r2], db_path=db)
            print("  ③ ★ 改值未报错")
            ok = False
        except ValueError:
            print("  ③ 改值被拒绝 ✅（必须显式抬 revision）")
        ok &= len(read_all_for_audit(db)) == 1

        conn = connect(db)
        try:
            rev = next_revision(conn, r)
        finally:
            conn.close()
        write_records([dict(r2, revision=rev)], db_path=db)
        print(f"  ④ 抬 revision={rev} 后写入：共 {len(read_all_for_audit(db))} 行")
        ok &= len(read_all_for_audit(db)) == 2

        try:
            query_available()
            print("  ⑤ ★ 不传 as_of 竟能查")
            ok = False
        except TypeError:
            print("  ⑤ 不传 as_of ⇒ TypeError ✅")

        vis = query_available("2026-10-12T16:29:59", db_path=db)
        print(f"  ⑥ 15:30 公布、16:30 可用；t=16:29:59 可见 {len(vis)} 条（应 0）")
        ok &= len(vis) == 0
        vis = query_available("2026-10-12T16:30:00", db_path=db)
        print(f"      t=16:30:00 可见 {len(vis)} 条（应 2：原版 + 修订版，边界含等号）")
        ok &= len(vis) == 2
        snap = latest_snapshot("2026-10-12T16:30:00", db_path=db)
        print(f"      latest_snapshot 去重后 {len(snap)} 条、值 {snap[0]['value'] if snap else None}"
              f"（应 1 条、取修订版 999.0）")
        ok &= len(snap) == 1 and snap[0]["value"] == 999.0

        try:
            make_row("x", "rb", "m", "2026-10-12", "2026-10-12", "2026-10-12", 1)
            print("  ⑦ ★ 只给日期没给时分的 published_at 被接受")
            ok = False
        except ValueError:
            print("  ⑦ date-only 的 published_at 被拒绝 ✅")

        try:
            make_row("x", "rb", "m", "2026-10-12", "2026-10-12",
                     "2026-10-10T10:00", 1)
            print("  ⑧ ★ published_at 早于 period_end 被接受")
            ok = False
        except ValueError:
            print("  ⑧ published_at 早于 period_end 被拒绝 ✅")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("=" * 72)
    print("PIT 自检：" + ("通过 ✅" if ok else "失败 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
