import os
import sys
# -*- coding: utf-8 -*-
"""
补齐 05_摆动线 全部 12 品种「样本内(in)」回测 —— 串行执行器
====================================================================
设计要点（严守项目铁律）：
  · 禁并发：每个品种用独立 subprocess 启动，一次只跑一个 tqsdk 会话；
    进程内 subprocess.run 阻塞，天然串行，绝不在同一会话里并发取数。
  · 每品种独立进程 = 独立会话，规避「慢请求污染整会话」与「每20个重开会话」问题。
  · 某品种失败不影响其余（try/except + 继续）。
  · 跑完自动从生成的报告里抓关键指标，写进 SUMMARY。

区间来源：各品种入口脚本里已经配好的 SAMPLE="in"（与 01 补齐口径一致：
  9 个品种 2016-2019；生猪/纯碱/苯乙烯 2020-2022 例外样本内；焦煤脚本仍 2016-2019）。
"""
import subprocess, os, time, datetime, glob, re, sys

ROOT = os.path.dirname(os.path.abspath(__file__))
while not os.path.exists(os.path.join(ROOT, "config_symbols.py")):
    _up = os.path.dirname(ROOT)
    if _up == ROOT:
        raise RuntimeError("未找到项目根（config_symbols.py）")
    ROOT = _up
PY = sys.executable
LOGDIR = os.path.join(ROOT, "工具/_输出/_补齐摆动线")
os.makedirs(LOGDIR, exist_ok=True)

VARIETIES = ["PVC", "天然橡胶", "棕榈油", "焦煤", "生猪", "甲醇",
             "白糖", "纯碱", "苯乙烯", "螺纹钢", "豆粕", "鸡蛋"]

PER_TIMEOUT = 1500  # 单品种超时（秒）


def _parse_metrics(txt_path):
    """从生成的 .txt 报告里抓关键指标，抓不到就返回空字典。"""
    out = {}
    try:
        with open(txt_path, "r", encoding="utf-8", errors="ignore") as f:
            txt = f.read()
    except Exception:
        return out
    pats = {
        "收益率": r"收益率[^\d\-]*([\-\d.]+)%",
        "年化": r"年化[^\d\-]*([\-\d.]+)%",
        "夏普": r"夏普[^\d\-]*([\-\d.]+)",
        "卡玛": r"卡玛[^\d\-]*([\-\d.]+)",
        "最大回撤": r"最大回撤[^\d\-]*([\-\d.]+)%",
        "胜率": r"胜率[^\d\-]*([\-\d.]+)%",
        "笔数": r"成交笔数[^\d]*(\d+)",
        "结束权益": r"结束资金[^\d\-]*([\d,]+)",
    }
    for k, p in pats.items():
        m = re.search(p, txt)
        if m:
            out[k] = m.group(1)
    # 诊断信号
    if "被柜台拒绝" in txt or "提前结束" in txt or "未采集" in txt:
        out["⚠️诊断"] = "中断/拒单"
    if "一根行情都没有" in txt or "空数据" in txt or "无数据" in txt:
        out["⚠️诊断"] = "空数据"
    return out


def _newest_report(variety):
    d = os.path.join(ROOT, f"回测结果/05_摆动线/{variety}")
    if not os.path.isdir(d):
        return None
    files = glob.glob(os.path.join(d, "*.txt"))
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def main():
    log_path = os.path.join(LOGDIR, "_runner.log")
    summary_path = os.path.join(LOGDIR, "_SUMMARY.txt")
    summary = []
    with open(log_path, "w", encoding="utf-8") as log:
        for v in VARIETIES:
            script = os.path.join(ROOT, f"策略/05_摆动线/回测/{v}/摆动线_回测.py")
            if not os.path.isfile(script):
                log.write(f"[跳过] {v}: 入口脚本不存在 {script}\n")
                summary.append((v, "NO_SCRIPT", {}, {}))
                continue
            t0 = time.time()
            log.write(f"\n===== [{datetime.datetime.now():%H:%M:%S}] 开始 {v} =====\n")
            log.flush()
            rc = None
            try:
                p = subprocess.run([PY, script], cwd=ROOT,
                                   stdout=log, stderr=subprocess.STDOUT,
                                   timeout=PER_TIMEOUT)
                rc = p.returncode
            except subprocess.TimeoutExpired:
                rc = "TIMEOUT"
                log.write(f"[超时] {v} 超过 {PER_TIMEOUT}s\n")
            except Exception as e:
                rc = f"ERR:{e}"
                log.write(f"[异常] {v}: {e}\n")
            dt = time.time() - t0
            rpt = _newest_report(v)
            metrics = _parse_metrics(rpt) if rpt else {}
            summary.append((v, rc, rpt, metrics))
            log.write(f"===== 结束 {v}: rc={rc} 用时 {dt/60:.1f}min 报告={os.path.basename(rpt) if rpt else '无'}\n")
            log.flush()
            # 终端也打印进度
            print(f"[{datetime.datetime.now():%H:%M:%S}] {v}: rc={rc} {dt/60:.1f}min "
                  f"收益率={metrics.get('收益率','?')} 夏普={metrics.get('夏普','?')} "
                  f"回撤={metrics.get('最大回撤','?')}", flush=True)

    # 写汇总
    with open(summary_path, "w", encoding="utf-8") as s:
        s.write("05_摆动线 补齐回测汇总（样本内 in）\n")
        s.write(f"生成时间：{datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n")
        s.write("=" * 70 + "\n")
        for v, rc, rpt, m in summary:
            s.write(f"\n● {v}  rc={rc}\n")
            s.write(f"  报告：{os.path.basename(rpt) if rpt else '无'}\n")
            if m:
                for k in ("收益率", "年化", "夏普", "卡玛", "最大回撤", "胜率", "笔数", "结束权益", "⚠️诊断"):
                    if k in m:
                        s.write(f"  {k}：{m[k]}\n")
            else:
                s.write("  未抓到指标（可能中断/空数据）\n")


if __name__ == "__main__":
    main()
