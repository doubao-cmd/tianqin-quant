# -*- coding: utf-8 -*-
"""
A 方案 · 后处理（不联网，直接读 趋势指标.json）
把"净方向"与"路径弯曲度"分开，纠正初版把二者混为一谈的"震荡年"标签。
"""
import os, json

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "趋势指标.json"), encoding="utf-8") as f:
    data = json.load(f)

YEARS = [2016, 2017, 2018, 2019]

def classify(ret, tr):
    """ret=区间/年度净收益, tr=方向性比率。返回 (标签, 方向)"""
    a = abs(ret)
    if a < 0.12:
        return "横盘震荡(无净方向)", "平"
    direction = "上行" if ret > 0 else "下行"
    if tr >= 0.30:
        return f"{direction}趋势(流畅单向)", direction
    return f"{direction}趋势但高波动(缠绕)", direction

print("=" * 86)
print("A 方案 · 修正判读（净方向 ≠ 路径弯曲度）")
print("=" * 86)

whole_rows = []
annual_flat = annual_up = annual_dn = 0
for key, rec in data.items():
    if "whole" not in rec:
        print(f"{key:4s} 取数失败: {rec.get('error')}")
        continue
    w = rec["whole"]
    label, _ = classify(w["total_return"], w["trend_ratio"])
    whole_rows.append((key, rec["name"], w["total_return"], w["ann_vol"], w["trend_ratio"], label))
    # 年度
    for y in YEARS:
        yy = rec.get("yearly", {}).get(str(y) if str(y) in rec.get("yearly", {}) else y)
        if not yy:
            continue
        if abs(yy["total_return"]) < 0.12:
            annual_flat += 1
        elif yy["total_return"] > 0:
            annual_up += 1
        else:
            annual_dn += 1

print("\n【整段 2016-2019 · 9 品种净方向】")
print(f"{'品种':6s}{'净收益':>10s}{'年化波动':>10s}{'方向性':>9s}   判读")
for key, name, ret, vol, tr, label in sorted(whole_rows, key=lambda r: -r[2]):
    print(f"{name:6s}{ret*100:+9.1f}%{vol*100:9.1f}%{tr:9.3f}   {label}")

n_up = sum(1 for r in whole_rows if r[2] > 0.12)
n_dn = sum(1 for r in whole_rows if r[2] < -0.12)
n_flat = sum(1 for r in whole_rows if abs(r[2]) <= 0.12)
print(f"\n整段净方向统计：上行 {n_up} / 下行 {n_dn} / 横盘 {n_flat}（共 {len(whole_rows)}）")
print(f"年度格子(36)净方向：上行 {annual_up} / 下行 {annual_dn} / 横盘 {annual_flat}")

# 写结论
md = []
md.append("# A 方案结论 · 2016-2019 是不是“震荡年”\n")
md.append("## 一、原始方向性指标（净位移 ÷ 总路程，越接近 0 越弯曲）\n")
md.append("| 品种 | 整段净收益 | 年化波动 | 方向性比率 | 判读 |")
md.append("|---|---|---|---|---|")
for key, name, ret, vol, tr, label in sorted(whole_rows, key=lambda r: -r[2]):
    md.append(f"| {name} | {ret*100:+.1f}% | {vol*100:.1f}% | {tr:.3f} | {label} |")
md.append("")
md.append("## 二、判读（纠正初版误标）\n")
md.append(f"- 整段 2016-2019：**{n_up} 个上行 / {n_dn} 个下行 / {n_flat} 个横盘**。")
md.append("  7 个净上涨、0 个净大跌 —— 这**不是横盘震荡市**，而是**波动巨大、路径弯曲的上行市**")
md.append("  （焦煤 +108%、螺纹 +100% 是 2016 商品大牛市的残留，只是 2017-2019 回吐切碎了路径）。\n")
md.append(f"- 年度 36 格：上行 {annual_up} / 下行 {annual_dn} / 横盘 {annual_flat}。")
md.append("  年与年之间确实反复（涨一年跌一年），在**年尺度**上是缠绕的；但多数年份仍有 ±10%~30% 的净方向，")
md.append("  不是纯随机横盘。\n")
md.append("## 三、对用户原话'震荡年→双均线亏正常'的修正\n")
md.append("1. **'年尺度缠绕'这部分对**：价格路径弯曲、趋势被频繁打断，对长周期趋势跟随不友好。")
md.append("2. **'震荡=横盘没趋势'这个理解要改**：这 4 年有很强中期趋势（尤其 2016-17），只是被高波动切碎。")
md.append("   焦煤 2016 单年 +110%，双均线整段却 -47% —— 连这种年度大趋势都做亏，")
md.append("   说明问题更在**策略本身/参数/成本**，不在'宏观没趋势'。")
md.append("3. **尺度错配**：双均线是 30 分钟、MA5/20（数日尺度），年线方向性低不代表短周期没波段可捕。")
md.append("   A 在年/四年尺度**不能**给'双均线亏损是因为震荡'背书。\n")
md.append("## 四、下一步（A'）\n")
md.append("要在双均线**实际交易周期（30 分钟/日线）**重测短周期方向性，")
md.append("看哪些年份/品种短周期也没趋势 —— 那才是在正确尺度上验证'没趋势→亏'。\n")
md.append(f"> 原始数据：趋势指标.json ｜ 取数脚本：verify_trend.py ｜ 运行日志：_run.log")

with open(os.path.join(HERE, "结论.md"), "w", encoding="utf-8") as f:
    f.write("\n".join(md))
print("\n已写出：结论.md")
