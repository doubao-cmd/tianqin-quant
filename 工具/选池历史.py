# -*- coding: utf-8 -*-
"""选池历史 —— 记下「每周换过一次什么池」，一条命令同步、一条命令查看。

【为什么需要】
`workbench.json` 的 `selected` 与 `data/selection_state.json` 都**只保留最新一份**
（每次重选原地覆盖）。换池这个动作一旦发生，旧池就永久消失：
  · 想知道「上周是哪些品种」「这个品种是哪周进来的」「换池前后权益怎么变的」——
    翻 git log 也只能看到提交过 workbench.json，看不到「为什么换、换了哪几个」。
本脚本把每次**品种池变化**追加成一行，CSV 永不覆盖。

【记录什么、不记录什么】
  记    ：品种池**发生实际变化**的那一次（首行建档也算一次）。
  不记  ：到期重选但结果与上期完全相同 —— 因为「品种没变」不携带信息，
          记了只会把 52 周稀释成一片「不变」。`--show` 会标注「沿用 N 周」补上连续性。

【幂等性（重要）】
  `--sync` 可以任意重复跑、可以每天跑：判重键 = (重选日, 品种池)。
  与 CSV 最后一行完全相同则**什么都不写**。所以挂定时器不会产生重复行。

【用法】
  python 工具\\选池历史.py --sync                          # 幂等同步（读 workbench.json + selection_state.json）
  python 工具\\选池历史.py --show                          # 打印全部记录 + 当前池沿用了多久
  python 工具\\选池历史.py --show 8                        # 只看最近 8 行
  python 工具\\选池历史.py --manual "RU,MA,AG,P,BU,EG" --note "人工调池"   # 手工登记一次变更
  python 工具\\选池历史.py --manual "..." --date 2026-10-26 --source manual

退出码：0 = 正常（含「无变化、未写入」）；1 = 出错（读不到配置 / CSV 结构损坏）。
"""
import argparse
import csv
import datetime
import io
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

# ---- 路径（全部集中在此，禁止在函数内散落拼字符串）----
WORKBENCH = os.path.join(_ROOT, "策略", "06_MA13超短线", "工作台", "workbench.json")
SEL_STATE = os.path.join(_ROOT, "策略", "06_MA13超短线", "工作台", "data", "selection_state.json")
HISTORY = os.path.join(_ROOT, "策略", "06_MA13超短线", "工作台", "选池历史.csv")

# ---- CSV 列（顺序即表头顺序；改名会让旧数据错位，禁止重排）----
COLS = ["记录时间", "重选日", "来源", "品种池", "数量", "较上期", "新增", "移除", "备注"]
SEP = "/"          # 品种池内部分隔符（用 / 而不用 , 避免 Excel 把它当列）
LOG = os.path.join(_ROOT, "策略", "06_MA13超短线", "工作台", "data", "选池历史_sync.log")


class _Tee(object):
    """同时写控制台和日志文件。日志用于证明「定时任务真的跑到了脚本」。"""
    def __init__(self, *streams):
        self.streams = streams

    def write(self, s):
        for f in self.streams:
            try:
                f.write(s)
                f.flush()
            except Exception:
                pass

    def flush(self):
        for f in self.streams:
            try:
                f.flush()
            except Exception:
                pass


# ---------------------------------------------------------------- 基础读写
def _read_history():
    """读历史 CSV → list[dict]。文件不存在返回 []。"""
    if not os.path.exists(HISTORY):
        return []
    with io.open(HISTORY, "r", encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.DictReader(f)]
    bad = [c for c in COLS if rows and c not in rows[0]]
    if bad:
        raise SystemExit(f"[ERR] {HISTORY} 表头缺列 {bad}（可能被手工改坏）")
    return rows


def _append_row(row):
    """追加一行。首次创建时写 UTF-8 BOM（Excel 双击不乱码），后续追加不再重复 BOM。"""
    os.makedirs(os.path.dirname(HISTORY), exist_ok=True)
    new = not os.path.exists(HISTORY) or os.path.getsize(HISTORY) == 0
    enc = "utf-8-sig" if new else "utf-8"
    with io.open(HISTORY, "a", encoding=enc, newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS, lineterminator="\n")  # 项目行尾规范：csv → LF
        if new:
            w.writeheader()
        w.writerow({c: row.get(c, "") for c in COLS})


# ---------------------------------------------------------------- 读现网状态
def _read_json(path, what):
    try:
        with io.open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except FileNotFoundError:
        raise SystemExit(f"[ERR] 找不到{what}：{path}")
    except Exception as e:
        raise SystemExit(f"[ERR] 读{what}失败：{type(e).__name__}: {e}")


def _norm(seq):
    """品种列表归一：大写、去空、去重、保持顺序。"""
    out, seen = [], set()
    for x in seq or []:
        k = str(x).strip().upper()
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _diff(old, new):
    """返回 (新增, 移除)。old/new 都是归一后的 list。"""
    so, sn = set(old), set(new)
    return [k for k in new if k not in so], [k for k in old if k not in sn]


def _fmt(seq):
    return SEP.join(seq) if seq else "—"


# ---------------------------------------------------------------- sync
def cmd_sync(args):
    cfg = _read_json(WORKBENCH, "workbench.json")
    st = _read_json(SEL_STATE, "selection_state.json")

    pool = _norm(cfg.get("selected"))
    if not pool:
        print("[SKIP] workbench.json 的 selected 为空 → 不记录（疑似配置未就绪）")
        return 0

    # 重选日：优先 state（真重选时写入），退化到今天
    reselect = str(st.get("last_reselect") or "")[:10]
    if not reselect:
        reselect = datetime.date.today().isoformat()

    rows = _read_history()
    last = rows[-1] if rows else None

    if last and last.get("重选日") == reselect and (last.get("品种池") or "") == _fmt(pool):
        print(f"[OK] 无变化（重选日 {reselect}、池 {_fmt(pool)} 已记录）→ 未写入")
        return 0

    old = _norm((last.get("品种池") or "").split(SEP)) if last else []
    add, drop = _diff(old, pool)

    # 归因：重选日变了 = 正常周频重选；重选日没变但池变了 = 有人手改了 workbench.json
    if not last:
        source, delta, note = "建档", "首次", "首次建档（此前的池变更无法回溯）"
    # 「出」= 移出池（drop），「入」= 移入池（add）。别把 add 写成出。
    elif reselect != last.get("重选日"):
        source = "auto"
        delta = "不变" if not add and not drop else f"{len(drop)}出{len(add)}入"
        note = (st.get("_note") or "") if isinstance(st, dict) else ""
        note = args.note or note
    else:
        source = "manual"
        delta = "不变" if not add and not drop else f"{len(drop)}出{len(add)}入"
        note = args.note or "品种池变化但重选日未变（疑人工改 workbench.json，非周频重选）"

    row = {
        "记录时间": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "重选日": reselect,
        "来源": source,
        "品种池": _fmt(pool),
        "数量": str(len(pool)),
        "较上期": delta,
        "新增": _fmt(add),
        "移除": _fmt(drop),
        "备注": note or "",
    }
    _append_row(row)
    print(f"[写入] {reselect} {source} | {row['品种池']} ({row['数量']}) | {delta}"
          f" | +{row['新增']} -{row['移除']}")
    return 0


# ---------------------------------------------------------------- manual
def cmd_manual(args):
    pool = _norm(str(args.manual).replace(",", " ").replace(SEP, " ").split())
    if not pool:
        print("[ERR] --manual 里没有解析出品种")
        return 1
    day = args.date or datetime.date.today().isoformat()
    rows = _read_history()
    last = rows[-1] if rows else None
    old = _norm((last.get("品种池") or "").split(SEP)) if last else []
    add, drop = _diff(old, pool)
    row = {
        "记录时间": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "重选日": day,
        "来源": args.source or "manual",
        "品种池": _fmt(pool),
        "数量": str(len(pool)),
        "较上期": "首次" if not last else ("不变" if not add and not drop else f"{len(drop)}出{len(add)}入"),
        "新增": _fmt(add),
        "移除": _fmt(drop),
        "备注": args.note or "手工登记",
    }
    _append_row(row)
    print(f"[写入] {day} {row['来源']} | {row['品种池']} ({row['数量']}) | {row['较上期']}")
    return 0


# ---------------------------------------------------------------- show
def cmd_show(args):
    rows = _read_history()
    if not rows:
        print(f"[空] 还没有记录：{HISTORY}")
        print("      先跑一次：python 工具\\选池历史.py --sync")
        return 0
    show = rows[-args.show:] if args.show else rows
    print("=" * 96)
    print(f"  选池历史（共 {len(rows)} 条，显示最近 {len(show)} 条）")
    print("=" * 96)
    for r in show:
        print(f"  {r.get('重选日','?'):<12} {r.get('来源',''):<7} {r.get('品种池','')}")
        extra = []
        if r.get("较上期") and r.get("较上期") not in ("不变",):
            extra.append(r["较上期"])
        if r.get("新增") and r["新增"] != "—":
            extra.append(f"新增 {r['新增']}")
        if r.get("移除") and r["移除"] != "—":
            extra.append(f"移除 {r['移除']}")
        if extra:
            print(f"  {'':<12} {'':<7} └ {' / '.join(extra)}")
        if r.get("备注"):
            print(f"  {'':<12} {'':<7}   {r['备注']}")
    print("-" * 96)

    # 当前池沿用了多少天（给「这周是不是该换了」一个直观数）
    cur = rows[-1]
    print(f"  当前池：{cur.get('品种池','')}（{cur.get('数量','?')} 个，自 {cur.get('重选日','?')}）")
    try:
        base = datetime.date.fromisoformat(str(cur.get("重选日"))[:10])
        age = (datetime.date.today() - base).days
        print(f"  已沿用：{age} 天（重选周期 6 天、到期待周一 ⇒ 下一个可换池日取周一）")
    except Exception:
        pass
    print("=" * 96)
    print(f"  文件：{HISTORY}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="选池历史：记录每周品种池变化")
    ap.add_argument("--sync", action="store_true", help="从 workbench.json / selection_state.json 幂等同步一次")
    ap.add_argument("--show", nargs="?", type=int, const=0, default=None, help="打印历史，可带行数")
    ap.add_argument("--manual", help="手工登记一个池（逗号或空格分隔），例如 \"FU,BU,MA\"")
    ap.add_argument("--date", help="配合 --manual：重选日 YYYY-MM-DD（默认今天）")
    ap.add_argument("--source", help="配合 --manual：来源标签（默认 manual）")
    ap.add_argument("--note", help="备注")
    ap.add_argument("--log", action="store_true", help="把本次输出追加到 工作台/data/选池历史_sync.log（定时任务用）")
    args = ap.parse_args()

    _fh = None
    if args.log:
        try:
            os.makedirs(os.path.dirname(LOG), exist_ok=True)
            _fh = io.open(LOG, "a", encoding="utf-8")
            _fh.write(f"\n[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] ---- 选池历史 sync ----\n")
            _fh.flush()
            sys.stdout = _Tee(sys.stdout, _fh)
        except Exception:
            _fh = None

    try:
        if args.manual:
            return cmd_manual(args)
        if args.show is not None:
            return cmd_show(args)
        if args.sync:
            return cmd_sync(args)
        ap.print_help()
        return 0
    finally:
        if _fh is not None:
            try:
                _fh.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
