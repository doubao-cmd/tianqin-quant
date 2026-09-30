import os
import sys
# -*- coding: utf-8 -*-
"""
补齐 05_摆动线 —— 只跑「缺的」品种（跳过已完整出报告的）
================================================================
已完成（HTML 含「夏普率」）：生猪 / 纯碱 / 苯乙烯
需补：PVC（被手动终止，不完整）+ 8 个空目录
  = 焦煤 / 甲醇 / 白糖 / 螺纹钢 / 豆粕 / 鸡蛋 / 天然橡胶 / 棕榈油

设计：串行、每品种独立进程（禁并发）；跑完校验 HTML 是否含指标，
      写 _SUMMARY_missing.txt。
"""
import subprocess, os, time, datetime, glob, re

ROOT = os.path.dirname(os.path.abspath(__file__))
while not os.path.exists(os.path.join(ROOT, "config_symbols.py")):
    _up = os.path.dirname(ROOT)
    if _up == ROOT:
        raise RuntimeError("未找到项目根（config_symbols.py）")
    ROOT = _up
PY = sys.executable
LOGDIR = os.path.join(ROOT, "工具/_输出/_补齐摆动线")
os.makedirs(LOGDIR, exist_ok=True)

# 只跑缺的 9 个（焦煤最先跑，作为早期健康探针）
RUN_THESE = ["焦煤", "甲醇", "白糖", "螺纹钢", "豆粕", "鸡蛋",
             "天然橡胶", "棕榈油", "PVC"]
PER_TIMEOUT = 1500


def _report_ok(variety):
    """已完整 = 存在 .html 且含『夏普率』或『收益率』且不含中断标记。"""
    d = os.path.join(ROOT, f"回测结果/05_摆动线/{variety}")
    if not os.path.isdir(d):
        return False
    hs = glob.glob(os.path.join(d, "*.html"))
    if not hs:
        return False
    try:
        txt = open(max(hs, key=os.path.getmtime), "r", encoding="utf-8", errors="ignore").read()
    except Exception:
        return False
    if "被柜台拒绝" in txt or "提前结束" in txt or "未采集" in txt:
        return False
    return ("夏普率" in txt) or ("收益率" in txt)


def _parse_metrics(html_path):
    out = {}
    try:
        t = open(html_path, "r", encoding="utf-8", errors="ignore").read()
    except Exception:
        return out
    pats = {
        "收益率": r"收益率[^\d\-]*([\-\d.]+)%",
        "年化": r"年化[^\d\-]*([\-\d.]+)%",
        "夏普": r"夏普率?[^\d\-]*([\-\d.]+)",
        "卡玛": r"卡玛[^\d\-]*([\-\d.]+)",
        "最大回撤": r"最大回撤[^\d\-]*([\-\d.]+)%",
        "胜率": r"胜率[^\d\-]*([\-\d.]+)%",
        "笔数": r"成交笔数[^\d]*(\d+)",
    }
    for k, p in pats.items():
        m = re.search(p, t)
        if m:
            out[k] = m.group(1)
    return out


def main():
    log_path = os.path.join(LOGDIR, "_runner_missing.log")
    summary_path = os.path.join(LOGDIR, "_SUMMARY_missing.txt")
    summary = []
    with open(log_path, "w", encoding="utf-8") as log:
        for v in RUN_THESE:
            script = os.path.join(ROOT, f"策略/05_摆动线/回测/{v}/摆动线_回测.py")
            if not os.path.isfile(script):
                log.write(f"[跳过] {v}: 入口脚本不存在\n"); summary.append((v, "NO_SCRIPT", {})); continue
            if _report_ok(v):
                log.write(f"[已完成-跳过] {v}\n"); summary.append((v, "SKIP_DONE", {})); continue
            t0 = time.time()
            log.write(f"\n===== [{datetime.datetime.now():%H:%M:%S}] 开始 {v} =====\n"); log.flush()
            rc = None
            try:
                p = subprocess.run([PY, script], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, timeout=PER_TIMEOUT)
                rc = p.returncode
            except subprocess.TimeoutExpired:
                rc = "TIMEOUT"; log.write(f"[超时] {v}\n")
            except Exception as e:
                rc = f"ERR:{e}"; log.write(f"[异常] {v}: {e}\n")
            dt = time.time() - t0
            # 取最新 html
            d = os.path.join(ROOT, f"回测结果/05_摆动线/{v}")
            hs = glob.glob(os.path.join(d, "*.html"))
            html = max(hs, key=os.path.getmtime) if hs else None
            m = _parse_metrics(html) if html else {}
            ok = bool(m)
            summary.append((v, rc, m))
            log.write(f"===== 结束 {v}: rc={rc} 用时 {dt/60:.1f}min 指标={'有' if ok else '无'}\n"); log.flush()
            print(f"[{datetime.datetime.now():%H:%M:%S}] {v}: rc={rc} {dt/60:.1f}min "
                  f"收益率={m.get('收益率','?')} 夏普={m.get('夏普','?')} 回撤={m.get('最大回撤','?')}", flush=True)

    with open(summary_path, "w", encoding="utf-8") as s:
        s.write("05_摆动线 补齐回测汇总（只跑缺的 9 个，样本内 in）\n")
        s.write(f"生成时间：{datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n")
        s.write("=" * 70 + "\n")
        for v, rc, m in summary:
            s.write(f"\n● {v}  rc={rc}\n")
            if m:
                for k in ("收益率", "年化", "夏普", "卡玛", "最大回撤", "胜率", "笔数"):
                    if k in m:
                        s.write(f"  {k}：{m[k]}\n")
            else:
                s.write("  未抓到指标（中断/超时/空数据）\n")


if __name__ == "__main__":
    main()
