import os, re, glob, json

ROOT = "回测结果/01_双均线"
KEYS = ["收益率","年化收益","最大回撤","胜率","盈亏比","夏普率","结束权益",
        "初始资金","总手续费","开仓次数","平仓次数","最大连盈天数","最大连亏天数",
        "卡玛比率","索提诺比率","交易天数"]
KEYMAP = {k: k for k in KEYS}

def parse_html(path):
    txt = open(path, encoding="utf-8", errors="ignore").read()
    out = {}
    for k in KEYS:
        m = re.search(r'<div class="k">'+re.escape(k)+r'</div><div class="v[^"]*">([^<]*)</div>', txt)
        if m:
            out[k] = m.group(1).strip()
    # 评估区间
    m = re.search(r'信号评估区间：([0-9:~ ]+)', txt)
    if m:
        out["评估区间"] = m.group(1).strip()
    # 周期
    m = re.search(r'周期：(\S+)', txt)
    if m:
        out["回测周期"] = m.group(1).strip()
    return out

def to_float(s):
    if s is None: return None
    s = s.replace(",", "").replace("%", "")
    try: return float(s)
    except: return None

rows = []
for d in sorted(glob.glob(os.path.join(ROOT, "*"))):
    v = os.path.basename(d)
    hs = sorted(glob.glob(os.path.join(d, "*.html")), key=os.path.getmtime, reverse=True)
    # 取 in 报告（文件名含 _in）
    target = [h for h in hs if "_in" in h]
    if not target:
        target = hs[:1]
    if not target:
        continue
    p = target[0]
    rec = parse_html(p)
    rec["品种"] = v
    rec["_file"] = os.path.basename(p)
    rows.append(rec)

# 打印表
cols = ["品种","评估区间"] + KEYS
hdr = " | ".join(cols)
print(hdr)
print("-"*len(hdr))
for r in rows:
    line = " | ".join(str(r.get(c,"")) for c in cols)
    print(line)

# 保存 json
with open("工具/_输出/_补齐摆动线/_01_metrics.json","w",encoding="utf-8") as f:
    json.dump(rows, f, ensure_ascii=False, indent=2)
print("\n[已存] 工具/_输出/_补齐摆动线/_01_metrics.json")

# 简要统计
print("\n=== 关键指标（数值）===")
for r in rows:
    rn = to_float(r.get("收益率")); sh = to_float(r.get("夏普率")); mdd = to_float(r.get("最大回撤"))
    wr = to_float(r.get("胜率")); kc = to_float(r.get("卡玛比率")); oc = to_float(r.get("开仓次数"))
    print(f"{r['品种']:<6} 收益={rn:>8}  夏普={sh:>6}  回撤={mdd:>7}  胜率={wr:>6}  卡玛={kc:>6}  开仓={oc:>4}")
