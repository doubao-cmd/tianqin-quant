# -*- coding: utf-8 -*-
r"""A23 · `工具\风控自检.py` 的场景断言（#25-B）

【为什么用临时台账目录，不碰生产】
自检脚本的红线是「只读」。「只读」这件事只能在**能被写坏也不心疼**的副本上验：
本测试把 13 项的证据全部现造成夹具（6 份心跳 / 6 份滑点 / 告警队列 / 核心日志 /
状态 json / 计划止损 …），然后改其中一两处，断言判定与退出码跟着变。
生产 `台账\` 全程只被读（`--ledger` 指到临时目录，脚本无从写到别处）。

【4 种场景（D2 任务书 §五 B6）】
  S1 全新鲜        ⇒ 无 ❌、退出码 0、#21 必须是 ❔
  S2 快照过期      ⇒ #22C 判 ❌、退出码 1
  S3 日志缺失      ⇒ #21/#23 判 ❔（**不许判 ✅**）、退出码 0
  S4 日历临期      ⇒ #24 判 ⚠️ 且提示补表、退出码 0

运行：python 工具\test_风控自检.py
"""
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import datetime as _dt

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "风控自检.py")
PY = sys.executable
RESULTS = []

NAMES = ["燃料油", "石油沥青", "甲醇", "豆粕", "纯碱", "乙二醇"]
DAY = _dt.date.today().strftime("%Y%m%d")


def ok(cond, msg):
    RESULTS.append((bool(cond), msg))
    print(("  PASS " if cond else "  FAIL ") + msg, flush=True)
    return bool(cond)


def _w(path, text, newline=""):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8-sig" if path.endswith(".csv") else "utf-8",
              newline=newline) as f:
        f.write(text)


def make_ledger(hold="燃料油", pos=8, with_log=True, snap_days_old=0):
    """造一份「全新鲜」的台账副本；按参数改坏某一处。"""
    d = tempfile.mkdtemp(prefix="riskchk_")

    # ---- 告警队列（2 条，含末行时间 + 当日计数）----
    rows = [{"ts": f"{_dt.date.today()} 09:05:00", "trade_day": DAY, "level": "INFO",
             "source": "核心", "text": "[PARAM] 参数自证", "dedup_key": "INFO:参数|1"},
            {"ts": f"{_dt.date.today()} 09:06:00", "trade_day": DAY, "level": "CRITICAL",
             "source": "守护进程", "text": "核心退出 rc=2，1.0s 后重启", "dedup_key": "C:1"}]
    _w(os.path.join(d, "告警队列.jsonl"),
       "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")

    # ---- 核心日志（#18 腿数闸门 / #23 PARAM；#21 故意不给 AVAILPRE）----
    if with_log:
        _w(os.path.join(d, f"核心输出_{DAY}.log"),
           "[腿数闸门] 池 6 品种 ≤ K=6 ⇒ **今日零触发属预期**，闸门只对扩池/换池生效。\r\n"
           '[PARAM] {"LEG_MAX": 6, "STALE_MULT": 2, "DAILY_LOSS_RATIO": 0.015, '
           '"KSEC": 300}\r\n')

    # ---- 6 份心跳（只有一只持仓；#15 用它数腿数）----
    for nm in NAMES:
        v = pos if nm == hold else 0
        _w(os.path.join(d, f"{nm}_运行心跳.csv"),
           f"记录时间,K线时间,现价,持仓手数,目标手数\r\n"
           f"{_dt.datetime.now():%Y-%m-%d %H:%M:%S},{_dt.datetime.now():%Y-%m-%d %H:%M:%S}.500000,"
           f"3000.00,{v},{1 if v else 0}\r\n")

    # ---- 6 份滑点（mtime 现在 ⇒ 全新鲜）----
    for nm in NAMES:
        _w(os.path.join(d, f"{nm}_滑点.csv"),
           "记录时间,trade_day,品种,合约,方向,开平,手数,信号价,成交价,滑点,tick数,成本,备注,溯源\r\n"
           f"{_dt.datetime.now():%Y-%m-%d %H:%M:%S},{DAY},{nm},X,多,开,8,3000,3000,0,0,0,,x\r\n")

    # ---- 隔夜快照（#22C）----
    _w(os.path.join(d, "隔夜敞口_快照.json"), json.dumps({"positions": {hold: pos}},
                                                        ensure_ascii=False))
    _w(os.path.join(d, "隔夜敞口_历史.csv"),
       f"日期,{hold},合计敞口\r\n{_dt.date.today()},{pos},1.0\r\n")
    if snap_days_old:
        old = (_dt.datetime.now() - _dt.timedelta(days=snap_days_old)).timestamp()
        for fn in ("隔夜敞口_快照.json", "隔夜敞口_历史.csv"):
            os.utime(os.path.join(d, fn), (old, old))

    # ---- #19 连亏分布（一次性快照）----
    _w(os.path.join(d, f"连亏分布_{DAY}.md"), "# 连亏分布\n")

    # ---- A2 守护：锁里放**本测试进程**的 PID（保证 alive）----
    _w(os.path.join(d, "守护.lock"), f"{os.getpid()}\r\n{HERE}\r\n")
    _w(os.path.join(d, "守护日志.txt"), f"{_dt.datetime.now():%Y-%m-%d %H:%M:%S}  测试\n")

    # ---- 单日亏损阀：update 写「现在」⇒ 新鲜 ----
    _w(os.path.join(d, "单日亏损状态.json"), json.dumps({
        "trade_day": DAY, "base_day": DAY, "base_equity": 1000000.0,
        "cur_equity": 999000.0, "loss": 1000.0, "threshold": 15000.0,
        "triggered": False, "triggered_at": None, "base_src": "state",
        "update": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False))

    # ---- #15 计划止损：文件名是**品种 key**，与持仓腿对齐才判 ✅ ----
    KEY = {"燃料油": "FU", "石油沥青": "BU", "甲醇": "MA",
           "豆粕": "M", "纯碱": "SA", "乙二醇": "EG"}
    if pos:
        _w(os.path.join(d, f"计划止损_{KEY[hold]}.json"), json.dumps({"stop": 1.0}))
    return d


def run(ledger, date=None, json_out=True):
    cmd = [PY, SCRIPT, "--ledger", ledger]
    if date:
        cmd += ["--date", date]
    if json_out:
        cmd += ["--json"]
    r = subprocess.run(cmd, capture_output=True, timeout=120)
    out = (r.stdout or b"").decode("utf-8", "replace")
    try:
        data = json.loads(out)
    except Exception:
        data = None
        print("    stdout:", out[:400], flush=True)
        print("    stderr:", (r.stderr or b"").decode("utf-8", "replace")[:400], flush=True)
    return r.returncode, data


def by_id(data, rid):
    for r in (data or {}).get("rows", []):
        if r["id"] == rid:
            return r
    return {}


print("=" * 72)
print("A23 · 风控自检脚本（4 场景，全部在临时台账副本上跑）")
print("=" * 72, flush=True)

# ---------------------------------------------------------------- S1 全新鲜
d = make_ledger()
try:
    rc, data = run(d)
    st = {r["id"]: r["status"] for r in (data or {}).get("rows", [])}
    print(f"    S1 退出码 {rc} / 判定 {st}", flush=True)
    ok(data is not None, "S1 `--json` 输出可被 json.loads 解析")
    ok(len((data or {}).get("rows", [])) == 13,
       f"S1 输出 13 行（9 项 + 3 附加 + 总判定用的行），实测 {len((data or {}).get('rows', []))} 行")
    ok("❌" not in st.values(), f"S1 全新鲜时无 ❌（实测 {[k for k, v in st.items() if v == '❌']}）")
    ok(st.get("#21") == "❔",
       f"S1 #21 必须是 ❔ 而不是 ✅（实测 {st.get('#21')}）—— 0 条无法区分「全通过」与「未接线」")
    ok(rc == 0, f"S1 全 OK ⇒ 退出码 0（实测 {rc}）")
finally:
    shutil.rmtree(d, ignore_errors=True)

# ---------------------------------------------------------------- S2 快照过期
d = make_ledger(snap_days_old=3)
try:
    rc, data = run(d)
    r22c = by_id(data, "#22C")
    print(f"    S2 #22C = {r22c.get('status')} / {r22c.get('verdict')}", flush=True)
    ok(r22c.get("status") == "❌",
       f"S2 快照 3 天前 ⇒ #22C 判 ❌（实测 {r22c.get('status')}）")
    ok(rc == 1, f"S2 有 ❌ ⇒ 退出码 1（实测 {rc}）")
finally:
    shutil.rmtree(d, ignore_errors=True)

# ---------------------------------------------------------------- S3 日志缺失
d = make_ledger(with_log=False)
try:
    rc, data = run(d)
    st = {r["id"]: r["status"] for r in (data or {}).get("rows", [])}
    print(f"    S3 判定 {st}", flush=True)
    ok(st.get("#21") == "❔" and st.get("#23") == "❔",
       f"S3 无核心日志 ⇒ #21/#23 判 ❔（实测 #21={st.get('#21')} / #23={st.get('#23')}）"
       f"—— 不许把「拿不到证据」写成 ✅")
    ok(rc == 0, f"S3 仅 ❔ ⇒ 退出码 0（不把「无法验证」当故障，实测 {rc}）")
finally:
    shutil.rmtree(d, ignore_errors=True)

# ---------------------------------------------------------------- S4 日历临期
d = make_ledger()
try:
    rc, data = run(d, date="20261210")      # 距覆盖末日 2026-12-31 仅 21 天 < 30
    r24 = by_id(data, "#24")
    print(f"    S4 #24 = {r24.get('status')} / {r24.get('verdict')}", flush=True)
    ok(r24.get("status") == "⚠️" and "补" in r24.get("verdict", ""),
       f"S4 剩 21 天 < 阈值 30 ⇒ #24 判 ⚠️ 且提示补表（实测 {r24.get('status')}："
       f"{r24.get('verdict')}）")
    ok(rc == 0, f"S4 ⚠️ 不单独导致退出码 1（实测 {rc}）")
finally:
    shutil.rmtree(d, ignore_errors=True)

print("=" * 72)
bad = [m for c, m in RESULTS if not c]
print(f" 合计 {len(RESULTS)} 项：通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
if bad:
    for m in bad:
        print("  FAIL " + m)
    sys.exit(1)
print(" 【全部通过】")
