# -*- coding: utf-8 -*-
"""
工具/_np_data/backfill.py —— 历史回填（执行任务书 D6/D7 要求③：`--backfill 一次拉满历史深度`）

为什么需要它
===============================================================================
`run_daily.py` 一次只抓**一个数据日**。到 2026-10-20 为止 `np_data` 里
receipt / rank / spot 三类**只有 2026-10-16 一天**（inventory 有 20 期）。
一天的数据不能做任何时间序列比较，地基等于只有地基线、没有楼。
本文件把已有的 fetch_* 按交易日逐日跑一遍，把历史一次性灌进去。

六个必须遵守的边界（每条都对应一个「会算错但不报错」的坑）
===============================================================================
1. **只按交易日历发请求**。非交易日（周末/节假日）交易所接口返回的不是空表而是
   JSONDecodeError —— 若把它当失败，回填会写几千行假失败；若把它当成功，又会
   静默跳过真实缺失。所以先用 `ak.tool_trade_date_hist_sina()` 取日历，
   非交易日根本不发请求，**交易日返回 0 行才算失败**。
2. **逐日幂等 + 可断点续跑**。库里该日已有「主源」行 ⇒ 跳过（`--force` 才重抓）。
   主源 = 覆盖最完整的那个（receipt→shfe_receipt / rank→shfe_rank / spot→spot_basis）；
   GFEX 只从 2024 年才有数据，不能拿它当"这天做没做过"的判据。
3. **冲突即失败，不覆盖**。写库走 `pit.write_records(on_conflict="raise")`：
   同主键不同值 = 交易所回头修订 ⇒ 记进失败清单，等人抬 revision，绝不静默改历史。
4. **回填默认不留原始响应**。单日 `_raw` ≈ 2.3MB × 2000 交易日 ≈ 5GB；
   任务书只要求「至少存一次」（日更那一路每天都在存）。`--raw` 可开。
5. **不写 `_last_update.json`**。那是「今天真跑过日更」的新鲜度凭据，
   回填写它 = 谎报今天抓过。回填的进度落 `_backfill_progress_{group}.json`。
6. **不吃掉单日失败**。单日重试 3 次仍失败 ⇒ 累计进 `_BACKFILL_FAILED_{group}.txt`
   并继续跑后面的日子（一天坏不该让 2000 天全停），但只要有一条失败，退出码就是 1。

用法
===============================================================================
    PY=C:\\Users\\cc\\.workbuddy\\binaries\\python\\envs\\tqsdk\\Scripts\\python.exe

    $PY backfill.py receipt --start 20180101                 # 仓单，默认到今天
    $PY backfill.py rank    --start 20180101                 # 会员持仓排名（慢，约 5s/日）
    $PY backfill.py spot    --start 20180101                 # 现货价 + 基差
    $PY backfill.py receipt --start 20180101 --limit 5       # 冒烟（只跑前 5 个交易日）
    $PY backfill.py receipt --start 20180101 --dry-run       # 只打印要跑多少天，不发请求
    $PY backfill.py receipt --start 20180101 --force         # 已有也重抓（幂等，用于补漏）
    $PY backfill.py receipt --start 20180101 --raw           # 保留原始响应（很占盘）

退出码：0 全部成功 / 1 至少一个交易日失败（清单见 `_BACKFILL_FAILED_{group}.txt`）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import date as _date
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import pit          # noqa: E402
import store        # noqa: E402

# ---------------------------------------------------------------------------
# 三个回填组：源清单 / 主源 / 抓取函数
# ---------------------------------------------------------------------------
def _groups() -> dict:
    """延迟 import：akshare 的 import 本身要 1~2s，--help / --dry-run 不该付这个钱。"""
    import fetch_receipt
    import fetch_rank
    import fetch_spot
    return {
        "receipt": {
            "sources": ("shfe_receipt", "czce_receipt", "gfex_receipt"),
            "primary": "shfe_receipt",          # ★ 覆盖最完整（GFEX 2024 才有）
            "fetch": fetch_receipt.fetch,
        },
        "rank": {
            "sources": ("shfe_rank", "czce_rank", "gfex_rank"),
            "primary": "shfe_rank",
            "fetch": fetch_rank.fetch,
        },
        "spot": {
            "sources": ("spot_basis",),
            "primary": "spot_basis",
            "fetch": fetch_spot.fetch,
        },
    }


# ---------------------------------------------------------------------------
# 交易日历（★ 边界 1）
# ---------------------------------------------------------------------------
def trading_days(start: str, end: str) -> list:
    """返回 [start, end] 内的交易日（YYYYMMDD，升序）。

    日历来自 akshare（1990-12-19 起，含已公布的年假安排）。
    **不自己判断周末**：调休（周末上班）与法定假日补班都会让"周一~周五=交易日"错。
    """
    import warnings
    warnings.filterwarnings("ignore")
    import akshare as ak
    cal = ak.tool_trade_date_hist_sina()
    col = "trade_date" if "trade_date" in cal.columns else cal.columns[0]
    all_days = sorted(str(x)[:10].replace("-", "") for x in cal[col].tolist())
    return [d for d in all_days if start <= d <= end]


def _today_str() -> str:
    return _date.today().strftime("%Y%m%d")


def _norm_date_arg(s: str) -> str:
    d = str(s).replace("-", "").strip()
    if len(d) != 8 or not d.isdigit():
        raise SystemExit(f"日期参数必须是 YYYYMMDD 或 YYYY-MM-DD：{s!r}")
    return d


# ---------------------------------------------------------------------------
# 库里已有的日子（★ 边界 2）
# ---------------------------------------------------------------------------
def existing_dates(source: str, db_path=None) -> set:
    """库里该源已有的数据日，**统一成 YYYYMMDD**。

    ★ 必须归一：库里 `period_end` 是 '2026-10-16'，而交易日历给的是 '20261016'。
      不归一的话一个都匹配不上 ⇒ 每个交易日都以为没跑过 ⇒ 2132 天全部重抓
      （不报错、只是白跑 1.5 小时）。这类"格式不一致导致全不命中"是静默故障。
    """
    conn = pit.connect(db_path)
    try:
        return {str(r[0]).replace("-", "")[:8] for r in conn.execute(
            "SELECT DISTINCT period_end FROM np_data WHERE source=?", (source,))}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 进度 / 失败落盘
# ---------------------------------------------------------------------------
def _progress_path(group: str) -> Path:
    return _HERE / f"_backfill_progress_{group}.json"


def _failed_path(group: str) -> Path:
    return _HERE / f"_BACKFILL_FAILED_{group}.txt"


def _atomic_write(p: Path, text: str) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


def _write_failed_list(group: str, failed: dict) -> None:
    if not failed:
        p = _failed_path(group)
        if p.exists():
            p.unlink()
        return
    lines = [f"# backfill group={group}  失败交易日 {len(failed)} 个",
             f"# 生成于 {time.strftime('%Y-%m-%d %H:%M:%S')}",
             "# 重跑同一条命令即可重试（成功的日子会自动跳过）：",
             f"#   python backfill.py {group} --start <最早失败日>", ""]
    for d in sorted(failed):
        lines.append(f"{d}\t{failed[d]}")
    _atomic_write(_failed_path(group), "\n".join(lines) + "\n")


def _write_progress(group: str, payload: dict) -> None:
    _atomic_write(_progress_path(group),
                  json.dumps(payload, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# 单日抓取（★ 边界 3 / 6）
# ---------------------------------------------------------------------------
def _fetch_one_day(fn, d: str, retries: int = 3, verbose: bool = True) -> tuple:
    """返回 (rows, err)。重试用尽仍失败 ⇒ rows=None, err='...'。"""
    last = ""
    for attempt in range(1, retries + 1):
        try:
            rows = fn(d)
            if not rows:
                # 交易日返回 0 行 = 解析/结构变了，不许当成「今天没数据」
                return None, f"0 行（交易日却无数据，多半是 schema 变了）"
            return rows, ""
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:300]}"
            if verbose and attempt < retries:
                print(f"    [{d}] 第 {attempt}/{retries} 次失败：{last[:160]}  → 退避重试")
                time.sleep(2.0 * attempt)
    return None, last


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def backfill(group: str, start: str, end: str, limit: int = 0, force: bool = False,
             save_raw: bool = False, sleep_s: float = 0.3, db_path=None,
             max_consec_fail: int = 20) -> int:
    g = _groups()[group]
    fn, primary = g["fetch"], g["primary"]

    days = trading_days(start, end)
    if not days:
        print(f"★ [{start}, {end}] 内没有交易日 —— 日期参数错了？")
        return 1

    have_all = existing_dates(primary, db_path)
    have = set() if force else have_all
    todo = [d for d in days if d not in have]
    skipped = len(days) - len(todo)
    if limit:
        todo = todo[:limit]

    store.SAVE_RAW_ENABLED = save_raw

    print("=" * 78)
    print(f"回填 group={group}  源={','.join(g['sources'])}  主源={primary}")
    print(f"区间 {start}~{end}：交易日 {len(days)} 个 | 已有 {skipped} 个（跳过）| 待跑 {len(todo)} 个")
    print(f"原始响应留存={'开（很占盘）' if save_raw else '关（日更那一路照常存）'}"
          f" | 每晚间隔 {sleep_s}s")
    if not todo:
        print("没有要跑的日子 —— 已是最新（用 --force 可强制重抓）")
        print("=" * 78)
        return 0
    print("=" * 78)

    t0 = time.time()
    inserted = unchanged = 0
    failed: dict = {}
    done = 0
    consec_fail = 0
    # 之前几轮留下的失败清单要先读进来，别让本轮成功把别的日子一起抹掉
    old_failed: dict = {}
    fp = _failed_path(group)
    if fp.exists():
        for line in fp.read_text(encoding="utf-8", errors="replace").splitlines():
            if line and not line.startswith("#") and "\t" in line:
                k, v = line.split("\t", 1)
                old_failed[k] = v
    # ★ 历史失败清单里"其实已经成功"的条目要清掉：这些日子后来跑通了（或本轮之前补上了），
    #   否则它们永远留在清单里、让退出码恒为 1，真失败就被噪音淹没了。
    stale = [k for k in old_failed if k in have_all]
    for k in stale:
        old_failed.pop(k, None)

    for i, d in enumerate(todo, 1):
        rows, err = _fetch_one_day(fn, d)
        if rows is None:
            failed[d] = err
            consec_fail += 1
            print(f"  [{i}/{len(todo)}] {d}  ★ 失败：{err[:120]}")
            # ★ 熔断：连续失败说明不是"某一天数据坏了"，而是被封/断网/接口整体变了。
            #   不熔断的后果是继续打 1 小时、把 2000 天全记成失败，还把 IP 打黑。
            if max_consec_fail and consec_fail >= max_consec_fail:
                print(f"  ★★ 连续失败 {consec_fail} 天 ⇒ 熔断退出（像是被反爬/断网/接口变更）。")
                print(f"     已跑 {i}/{len(todo)}，进度已落盘；网络恢复后重跑同一条命令即可续。")
                _write_failed_list(group, {**old_failed, **failed})
                _write_progress(group, {
                    "group": group, "start": start, "end": end, "aborted": "consecutive_fail",
                    "todo": len(todo), "processed": i, "ok_days": done,
                    "inserted": inserted, "unchanged": unchanged,
                    "failed": len(failed), "elapsed_s": round(time.time() - t0, 1),
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                })
                return 2
        else:
            consec_fail = 0
            old_failed.pop(d, None)   # ★ 本轮跑成功 ⇒ 撤销历史失败清单里的这一条（否则永远清不掉）
            try:
                stat = pit.write_records(rows, db_path=db_path, on_conflict="raise")
                inserted += stat["inserted"]
                unchanged += stat["unchanged"]
                done += 1
                if i % 20 == 0 or i <= 3:
                    el = time.time() - t0
                    rate = el / i
                    eta = rate * (len(todo) - i) / 60.0
                    print(f"  [{i}/{len(todo)}] {d}  {len(rows):>4} 行  "
                          f"累计新增 {inserted}  幂等 {unchanged}  "
                          f"{rate:.1f}s/日  ETA {eta:.0f}min")
            except Exception as e:
                # 冲突（同主键不同值）＝ 交易所回头修订，必须人看，不许静默覆盖
                failed[d] = f"写入失败 {type(e).__name__}: {str(e)[:240]}"
                print(f"  [{i}/{len(todo)}] {d}  ★ 写入失败：{str(e)[:160]}")

        # 每 20 天落一次进度（被沙箱回收也不至于全丢）
        if i % 20 == 0:
            _write_progress(group, {
                "group": group, "start": start, "end": end,
                "todo": len(todo), "processed": i, "ok_days": done,
                "inserted": inserted, "unchanged": unchanged,
                "failed": len(failed), "elapsed_s": round(time.time() - t0, 1),
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            })
            _write_failed_list(group, {**old_failed, **failed})
        if sleep_s:
            time.sleep(sleep_s)

    elapsed = time.time() - t0
    all_failed = {**old_failed, **failed}
    _write_failed_list(group, all_failed)
    _write_progress(group, {
        "group": group, "start": start, "end": end,
        "todo": len(todo), "processed": len(todo), "ok_days": done,
        "inserted": inserted, "unchanged": unchanged,
        "failed": len(all_failed), "elapsed_s": round(elapsed, 1),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })

    print("-" * 78)
    print(f"完成：成功 {done}/{len(todo)} 日 | 新增 {inserted} 行 / 幂等 {unchanged} 行 "
          f"| 失败 {len(failed)} 日 | 耗时 {elapsed/60:.1f} min")
    if all_failed:
        print(f"★ 失败清单：{_failed_path(group)}（退出码 1）")
        print("=" * 78)
        return 1
    print("=" * 78)
    return 0


# ---------------------------------------------------------------------------
# 自检：不联网，只验「交易日历 + 跳过逻辑 + 失败清单」三个判据
# ---------------------------------------------------------------------------
def _selftest() -> int:
    ok = True
    print("=" * 78)
    print("backfill 自检（交易日历 / 跳过逻辑 / 失败清单；不联网部分）")
    print("=" * 78)

    # ① 交易日历：周六周日绝不在列表里（用 2026-10-17/18 周末做判据）
    days = trading_days("20261016", "20261019")
    print(f"  ① 20261016~20261019 交易日={days}（应只含 16、19，不含 17/18 周末）")
    ok &= "20261017" not in days and "20261018" not in days
    ok &= "20261016" in days and "20261019" in days

    # ② 日历必须覆盖 2018 起（回填起点）
    d18 = trading_days("20180101", "20180110")
    print(f"  ② 2018 首批交易日={d18[:3]}... 共 {len(d18)} 个（应非空）")
    ok &= len(d18) > 0 and d18[0] >= "20180101"

    # ③ 跳过逻辑：已有日期的源，todo 必须排除它（用临时库）
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="bf_"))
    try:
        db = tmp / "t.db"
        row = pit.make_row("shfe_receipt", "RB", "receipt", "2026-10-16", "2026-10-16",
                           "2026-10-16T15:30", 1.0, published_src="rule")
        pit.write_records([row], db_path=db)
        have = existing_dates("shfe_receipt", db_path=db)
        print(f"  ③ 已有日期={sorted(have)}（应 {{'20261016'}}，★ 必须是 YYYYMMDD 无横线），"
              f"故该日会被跳过={('20261016' in have)}")
        ok &= have == {"20261016"}
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    # ④ 失败清单：空 ⇒ 文件不该存在（不能留一个空文件冒充"有失败"）
    p = _failed_path("__selftest__")
    _write_failed_list("__selftest__", {})
    print(f"  ④ 空失败清单 ⇒ 文件存在={p.exists()}（应 False）")
    ok &= not p.exists()
    _write_failed_list("__selftest__", {"20261016": "TestError: x"})
    body = p.read_text(encoding="utf-8") if p.exists() else ""
    print(f"    一条失败 ⇒ 文件存在={p.exists()} 且含日期={'20261016' in body}（应 True/True）")
    ok &= p.exists() and "20261016" in body and "TestError" in body
    p.unlink()

    # ⑤ 日期参数规范化
    print(f"  ⑤ 2026-10-16 → {_norm_date_arg('2026-10-16')}（应 20261016）")
    ok &= _norm_date_arg("2026-10-16") == "20261016"

    print("=" * 78)
    print("backfill 自检：" + ("通过 ✅" if ok else "失败 ❌"))
    return 0 if ok else 1


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(
        description="非价格数据历史回填（交易日逐日跑 fetch_*，幂等可续跑）")
    ap.add_argument("group", nargs="?", choices=["receipt", "rank", "spot"],
                    help="回填哪一组")
    ap.add_argument("--start", help="起始日 YYYYMMDD（含）")
    ap.add_argument("--end", help="截止日 YYYYMMDD（含），默认今天")
    ap.add_argument("--limit", type=int, default=0, help="最多跑几个交易日（冒烟用）")
    ap.add_argument("--force", action="store_true", help="已有也重抓（默认跳过）")
    ap.add_argument("--raw", action="store_true", help="保留原始响应（很占盘，默认关）")
    ap.add_argument("--sleep", type=float, default=0.3, help="每晚之间的间隔秒，默认 0.3")
    ap.add_argument("--db", default=None, help="库路径（默认 np.db / 环境变量 NP_DB）")
    ap.add_argument("--dry-run", action="store_true", help="只打印要跑多少天，不发请求")
    ap.add_argument("--selftest", action="store_true", help="自检（不联网）")
    a = ap.parse_args(argv[1:])

    if a.selftest:
        return _selftest()
    if not a.group or not a.start:
        ap.print_help()
        return 2

    start = _norm_date_arg(a.start)
    end = _norm_date_arg(a.end) if a.end else _today_str()
    if end < start:
        raise SystemExit(f"--end({end}) 早于 --start({start})")

    if a.dry_run:
        days = trading_days(start, end)
        have = existing_dates(_groups()[a.group]["primary"], a.db)
        todo = [d for d in days if d not in have]
        print(f"group={a.group} 区间 {start}~{end}：交易日 {len(days)} | "
              f"已有 {len(days) - len(todo)} | 待跑 {len(todo)}")
        if todo:
            print(f"  首个 {todo[0]}  末个 {todo[-1]}")
        return 0

    try:
        return backfill(a.group, start, end, limit=a.limit, force=a.force,
                        save_raw=a.raw, sleep_s=a.sleep, db_path=a.db)
    except KeyboardInterrupt:
        print("\n★ 被中断：进度已落 _backfill_progress_*.json，重跑同一命令即可续跑")
        return 130
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
