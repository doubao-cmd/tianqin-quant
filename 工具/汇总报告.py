# -*- coding: utf-8 -*-
"""
PVC 双均线 · 修改记录汇总报告
==========================================================
作用：把「每次改了什么 + 改完之后的回测数据」攒成一份会自己更新的汇总页。

  · 每条修改的文字说明（改了什么 / 为什么 / 对照）存在账本里，由人写：
        工具/_账本/<品种>_<策略>.json
  · 每次回测跑完，自动把最新报告的指标抓进账本里「最新那一条」，然后重渲染：
        回测结果/<策略目录>/<品种>/<品种>_<策略>_汇总.html
    （<策略目录> 带编号：01_双均线 / 02_均线排列 / 03_布林带，见 lib/strategy_dirs.py）

所以：文字是手写的（不会变），数字是自动的（每次回测后刷新）。

用法：
    python 工具/汇总报告.py V 双均线      # 抓最新报告 + 更新账本 + 重渲染
    python 工具/汇总报告.py V 双均线 --render   # 只按账本重渲染，不抓报告
（被 策略/01_双均线/回测/PVC/双均线_回测.py 在回测结束后自动调用）

⚠️ 缺账本时会**优雅跳过**（打一行「[汇总] 跳过…」并返回 None），不抛异常 ——
   那只说明该品种还没建账本，**与回测结果无关**（报告已正常落盘）。
   2026-09-27 之前这里抛 FileNotFoundError，调用方会打「[汇总] 刷新失败」。

某次修改跑出来是负收益、要退回上一版怎么办？
    python 工具/回滚.py V 双均线 --go
    一条命令做完三件事：还原上一版代码 + 删掉本条记录（本页随之消失）+
    本次报告进回收站。代码快照由 lib/code_snapshot.py 在每次回测开跑前自动存。
"""
import json
import os
import re
import sys
from datetime import datetime

# ---- 定位项目根目录（含 lib 的那一层）----
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = _HERE
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# 策略短名 -> 落盘目录名（01_双均线 / 02_均线排列 / 03_布林带）
from lib.strategy_dirs import 策略目录名
# 顶层目录名统一从 lib/paths.py 取（唯一出处，改名只改那一处）
from lib.paths import 回测结果 as _DIR_回测结果

LEDGER_DIR = os.path.join(_HERE, "_账本")

# 精简版式：只把 6 项关键指标摆在一行指标条上，其余收进「更多指标」折叠区
BAR_KEYS = ["收益率", "最大回撤", "胜率", "盈亏比", "结束权益", "开仓次数"]
MORE_KEYS = ["年化收益", "夏普率", "总手续费", "平仓次数", "卡玛比率",
             "索提诺比率", "交易天数", "最大连盈天数", "最大连亏天数", "初始资金"]

_CARD_RE = re.compile(r'<div class="k">([^<]*)</div><div class="v[^>]*>([^<]*)</div>')

# 各策略的成本口径文案 —— 口径由各策略**回测脚本**决定（02/03 目前没传 slip_ticks），
# 这里只是把事实写清楚，不做任何换算。新增策略时在这里补一行即可。
成本口径文案 = {
    "双均线": (
        "成本口径（全部版本一致）：TqSim <b>对价成交</b>（价格层 1 跳：买 +1 / 卖 −1）"
        "＋ 脚本 <b>slip_ticks = 1</b>（费用层 1 跳）→ <b>等效实盘滑点假设 2 跳/次</b>。"
        "即：若实盘实际只吃 1 跳，下表收益率应上修约 0.16pp。"),
    "均线排列": (
        "成本口径（全部版本一致）：TqSim <b>对价成交</b>（价格层 1 跳：买 +1 / 卖 −1）"
        "＋ 脚本 <b>slip_ticks = 0</b>（未加费用层滑点）→ <b>等效实盘滑点假设 1 跳/次</b>。"
        "⚠️ 本策略<b>不含费用层滑点</b>，与「01 双均线（等效 2 跳）」<b>口径不一致</b>，"
        "两张汇总页的收益率<b>不可直接横向比较</b>。"),
    "通道突破": (
        "成本口径（全部版本一致）：TqSim <b>对价成交</b>（价格层 1 跳：买 +1 / 卖 −1）"
        "＋ 脚本 <b>slip_ticks = 1</b>（费用层 1 跳）→ <b>等效实盘滑点假设 2 跳/次</b>。"
        "与「01 双均线」<b>口径一致</b> —— 周期 30 分钟、区间 2026-03-13~09-13、"
        "波动率预算手数、ATR 止损、成本模型五项全部相同，"
        "因此两页收益率<b>可直接横向比较，差异即「信号源」的贡献</b>。"),
    "摆动线": (
        "成本口径（全部版本一致）：TqSim <b>对价成交</b>（价格层 1 跳：买 +1 / 卖 −1）"
        "＋ 脚本 <b>slip_ticks = 1</b>（费用层 1 跳）→ <b>等效实盘滑点假设 2 跳/次</b>。"
        "与「01 双均线 / 04 通道突破」<b>口径一致</b>，收益率可横向比较。"
        "手数口径：v0 固定 1 手；<b>v1 起改为波动率预算 1~8 手</b>（与 01/03 同一把尺子），"
        "故 v0 与 v1 之后的收益率<b>不可直接比大小</b>（读数变大先看手数，不是信号变好）。"
        "周期口径：全程 <b>30 分钟</b>（曾试 15 分钟，结论明确更差、已回滚作废，未留档；"
        "曾试「只平不反+等结构重建」，机制性失效、已回滚作废，未留档）。"
        "止损：v2 起叠 2.0×ATR 固定止损（仅灾难保险，出场仍归结构）。"
        "出场口径：全部在档版本均为<b>穿越即反手</b>（结构反向穿越 = 平仓+反手，永远在场）。"),
}
_成本口径兜底 = ("成本口径：TqSim <b>对价成交</b>（价格层 1 跳：买 +1 / 卖 −1）。"
              "本策略未登记成本口径文案，请核对对应回测脚本的 slip_ticks 后再解读收益率。")


def 账本路径(品种, 策略):
    return os.path.join(LEDGER_DIR, f"{品种}_{策略}.json")


def 读账本(品种, 策略):
    p = 账本路径(品种, 策略)
    if not os.path.exists(p):
        raise FileNotFoundError(f"账本不存在：{p}")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def 写账本(品种, 策略, data):
    os.makedirs(LEDGER_DIR, exist_ok=True)
    p = 账本路径(品种, 策略)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return p


def 抓指标(html_path):
    """从回测报告 HTML 里抓指标卡 + 注脚。返回 (指标dict, 周期文本, 区间文本, 生成时间)。"""
    with open(html_path, encoding="utf-8") as f:
        h = f.read()
    cards = {}
    for k, v in _CARD_RE.findall(h):
        cards[k.strip()] = v.strip()

    per = ""
    m = re.search(r'<div class="sub">(.*?)</div>', h, re.S)
    if m:
        sub = re.sub(r"<[^>]+>", "", m.group(1))
        parts = [x.strip() for x in sub.split("｜")]
        if len(parts) >= 3:
            per = parts[1]
    note = ""
    m = re.search(r'信号评估区间：([^<｜|]+)', h)
    if m:
        note = m.group(1).strip()
        # 「2026-03-13 ~ 2026-09-13（2026-03-06 起仅预热不交易）」→ 只留前面的日期段
        note = re.split(r'[（(]', note)[0].strip()
    m = re.search(r'生成于 ([\d\-: ]+)', h)
    gen = m.group(1).strip() if m else ""
    return cards, per, note, gen


def 找最新报告(品种, 策略, tag):
    d = os.path.join(_ROOT, _DIR_回测结果, 策略目录名(策略), 品种)
    if not os.path.isdir(d):
        return None
    cand = [os.path.join(d, f) for f in os.listdir(d)
            if f.startswith(f"{品种}_{策略}_") and f.endswith(f"_{tag}.html")
            and "汇总" not in f]
    if not cand:
        return None
    return max(cand, key=os.path.getmtime)


def _品种键(品种名):
    """中文名 -> 账号键（PVC -> V），只为了在提示里写一条能直接抄的命令。"""
    try:
        from config_symbols import SYMBOLS
        for k, v in SYMBOLS.items():
            if v[0] == 品种名:
                return k
    except Exception:
        pass
    return 品种名


def _上色(k, v):
    if k in ("收益率", "年化收益"):
        return "neg" if str(v).startswith("-") else "pos"
    if k == "最大回撤":
        return "neg"
    return ""


def _指标条(指标):
    """返回 (一行指标条 HTML, 更多指标文本)。"""
    cells = []
    for k in BAR_KEYS:
        v = 指标.get(k, "—")
        cells.append(f'<div class="cell"><div class="k">{k}</div>'
                     f'<div class="v {_上色(k, v)}">{v}</div></div>')
    bar = f'<div class="bar">{"".join(cells)}</div>'
    more = " ｜ ".join(f"{k} {指标.get(k)}" for k in MORE_KEYS if k in 指标)
    return bar, more


CSS = """
  * { box-sizing: border-box; }
  body { font-family: -apple-system, "Microsoft YaHei", sans-serif;
         background:#f5f7fa; color:#222; margin:0; padding:24px; }
  .wrap { max-width:880px; margin:0 auto; background:#fff;
          border:1px solid #e3e8ef; border-radius:12px; padding:22px 26px; }
  h1 { font-size:19px; margin:0 0 3px; }
  .sub { color:#788; font-size:12.5px; margin-bottom:18px; }
  .ver { border:1px solid #eef2f7; border-left:4px solid #cdd6e3; border-radius:9px;
         padding:12px 15px; margin-bottom:12px; background:#fcfdff; }
  .ver.now { border-left-color:#4c8bf5; background:#f8fbff; }
  .vhead { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
  .badge { background:#eef2f7; color:#667; font-size:11.5px; font-weight:700;
           padding:2px 8px; border-radius:20px; }
  .badge.now { background:#4c8bf5; color:#fff; }
  .vtitle { font-size:14.5px; font-weight:700; }
  .vdate { color:#99a; font-size:11.5px; margin-left:auto; }
  .chg { margin:7px 0 0; font-size:13px; line-height:1.7; color:#334; }
  .chg span { color:#8a94a6; margin-right:5px; }
  .why { margin:5px 0 0; font-size:13px; line-height:1.7; color:#556; }
  .why span { color:#8a94a6; margin-right:5px; }
  .bar { display:flex; border:1px solid #eef2f7; border-radius:8px;
         background:#f8fafc; overflow:hidden; margin:10px 0 6px; }
  .bar .cell { flex:1; text-align:center; padding:7px 2px; border-right:1px solid #eef2f7; }
  .bar .cell:last-child { border-right:0; }
  .bar .k { font-size:11px; color:#8a94a6; margin-bottom:2px; }
  .bar .v { font-size:15px; font-weight:700; }
  .pos { color:#d4380d; } .neg { color:#198755; }
  .ret { font-size:13px; font-weight:700; }
  .warnbox { font-size:12.5px; line-height:1.7; color:#8a5a00; background:#fffbf0;
             border:1px solid #f0d9a8; border-radius:8px; padding:8px 11px; margin:8px 0 4px; }
  .warnbox code { background:#fff; border:1px solid #f0e2c4; border-radius:4px;
                  padding:0 4px; font-size:12px; }
  /* 历史版本：默认收起成一行，点开才展开 */
  details.hist { border:1px solid #eef2f7; border-left:4px solid #cdd6e3; border-radius:9px;
                 background:#fcfdff; margin-bottom:8px; }
  details.hist > summary { padding:9px 14px; cursor:pointer; font-size:13px; color:#334;
                           display:flex; align-items:center; gap:8px; list-style:none; }
  details.hist > summary::-webkit-details-marker { display:none; }
  details.hist > summary::after { content:"▾"; color:#99a; font-size:11px; }
  details.hist[open] > summary::after { content:"▴"; }
  details.hist[open] > summary { border-bottom:1px solid #eef2f7; }
  details.hist .inner { padding:10px 15px 12px; }
  .vs { font-size:12px; color:#667; background:#f8fafc; border-radius:7px;
        padding:6px 10px; margin:0 0 2px; }
  .vs b { color:#222; }
  details { margin-top:7px; }
  summary { cursor:pointer; color:#4c8bf5; font-size:12px; outline:none; }
  details .dt { font-size:12.5px; color:#667; line-height:1.75; margin-top:6px;
                padding-left:10px; border-left:2px solid #eef2f7; }
  .wait { font-size:13px; color:#c2790a; background:#fffbf0; border:1px dashed #f0d9a8;
          border-radius:8px; padding:9px 11px; margin-top:9px; }
  .foot { margin-top:20px; font-size:11.5px; color:#aab; border-top:1px solid #eef2f7;
          padding-top:10px; }
  .cost { font-size:12px; color:#b45309; background:#fffbf0; border:1px solid #f5e3c0;
          border-radius:8px; padding:8px 11px; margin:10px 0 2px; line-height:1.7; }
"""


def 渲染(品种, 策略):
    L = 读账本(品种, 策略)
    vers = L.get("版本", [])
    # 倒序：最新在最上面
    ordered = list(reversed(vers))
    blocks = []
    for i, v in enumerate(ordered):
        now = (i == 0)
        指标 = v.get("指标") or {}

        # 改动：一行一条短句
        改动 = v.get("改动", [])
        chg = "<br>".join(f"· {x}" for x in 改动)

        # 原因：一句话
        why = v.get("原因", "")

        # 指标条（有数据才渲染）
        if 指标:
            bar, more = _指标条(指标)
        else:
            bar = ('<div class="wait">还没有数据 —— 跑一次回测（双击同目录的回测.bat），'
                   '本页会自动刷新。</div>')
            more = ""

        # 对照行：压缩成一行
        vs_html = ""
        vs = v.get("对照")
        if vs:
            items = " ｜ ".join(f"{k} <b>{val}</b>" for k, val in vs.items())
            vs_html = f'<div class="vs">上次（{v.get("对照标题", "对照")}）：{items}</div>'

        # 折叠区：更多指标 + 详细说明 + 出处
        细节 = []
        if more:
            细节.append(f"更多指标：{more}")
        if v.get("详细"):
            细节.append(v["详细"])
        if 指标:
            细节.append(f'数据来源：{v.get("报告", "")} ｜ 抓取于 {v.get("快照时间", "")}')
        if v.get("区间"):
            细节.append(f'区间：{v["区间"]}')
        detail_html = ""
        if 细节:
            body_txt = "<br><br>".join(细节)
            detail_html = (f'<details><summary>详细说明</summary>'
                           f'<div class="dt">{body_txt}</div></details>')

        # 负收益提示：只挂在「当前」这一条上，告诉用户怎么一键回退
        warn_html = ""
        if now and str(指标.get("收益率", "")).startswith("-"):
            warn_html = (
                f'<div class="warnbox">本次修改为<b>负收益</b>（{指标.get("收益率")}）。'
                f'若判定这次改动不成立，可一键回退：还原上一版代码 + 删掉本条记录 + '
                f'本次报告进回收站。双击 <code>工具\\回滚.bat</code>，或跑 '
                f'<code>python 工具/回滚.py {_品种键(品种)} {策略} --go</code>。</div>')

        # 标题行（当前版本完整显示，历史版本只留这一行）
        收益率 = 指标.get("收益率", "")
        head = (f'<span class="badge{" now" if now else ""}">'
                f'{v.get("版本", "")}{" · 当前" if now else ""}</span>'
                f'<span class="vtitle">{v.get("标题", "")}</span>'
                + (f'<span class="ret {_上色("收益率", 收益率)}">{收益率}</span>'
                   if 收益率 and not now else "")
                + f'<span class="vdate">{v.get("日期", "")}</span>')

        body = f"""
    <div class="chg"><span>改动</span>{chg}</div>
    <div class="why"><span>原因</span>{why}</div>
    {bar}
    {warn_html}
    {vs_html}
    {detail_html}"""

        if now:
            blocks.append(f'<div class="ver now"><div class="vhead">{head}</div>{body}</div>')
        else:
            blocks.append(f'<details class="hist"><summary>{head}</summary>'
                          f'<div class="inner">{body}</div></details>')

    sub = (f"{L.get('周期', '5分钟')} ｜ 练习区间 {L.get('区间', '')} ｜ "
           f"共 {len(vers)} 次修改 ｜ 更新于 {datetime.now():%Y-%m-%d %H:%M}")
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{品种} {策略} 修改记录汇总</title>
<style>{CSS}</style>
</head>
<body>
  <div class="wrap">
    <h1>{品种} · {策略} 修改记录汇总</h1>
    <div class="sub">{sub}</div>
    <div class="cost">{成本口径文案.get(策略, _成本口径兜底)}</div>
{''.join(blocks)}

    <div class="foot">本页由「每次回测结束后自动刷新」，数字取自最新一次回测报告；
    修改说明为人工记录。仅用于策略历史回测展示，不构成任何投资建议。</div>
  </div>
</body>
</html>"""
    out = os.path.join(_ROOT, _DIR_回测结果, 策略目录名(策略), 品种, f"{品种}_{策略}_汇总.html")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    return out


def 更新(品种键, 策略, tag="in", 品种名=None):
    """抓最新回测报告 -> 写入账本最新一条 -> 重渲染汇总页。返回汇总页路径。

    账本不存在时**优雅跳过**（返回 None）：
        这只是「该品种还没有账本 ⇒ 本次没东西可汇总」，**不是回测出错**。
        报告照常落盘，只是不生成汇总页。
        （2026-09-27 前这里直接抛 FileNotFoundError，调用方只能打一行
         「[汇总] 刷新失败」，容易被误读成「回测坏了」。）
    """
    if 品种名 is None:
        from config_symbols import get
        品种名 = get(品种键)[0]
    _lp = 账本路径(品种名, 策略)
    if not os.path.exists(_lp):
        print(f"[汇总] 跳过：{品种名} {策略} 还没有账本"
              f"（{os.path.relpath(_lp, _ROOT)}），本次不生成汇总页。"
              f"回测结果不受影响。")
        return None
    L = 读账本(品种名, 策略)
    vers = L.get("版本", [])
    if not vers:
        raise RuntimeError(f"账本里还没有任何修改条目：{账本路径(品种名, 策略)}")
    rpt = 找最新报告(品种名, 策略, tag)
    if not rpt:
        raise FileNotFoundError(
            f"没找到回测报告：{_DIR_回测结果}/{策略目录名(策略)}/{品种名}/"
            f"{品种名}_{策略}_*_{tag}.html")
    指标, 周期, 区间, _gen = 抓指标(rpt)
    if not 指标:
        raise RuntimeError(f"报告里没抓到指标卡：{rpt}")
    vers[-1]["指标"] = 指标
    vers[-1]["报告"] = f"{_DIR_回测结果}/{策略目录名(策略)}/{品种名}/" + os.path.basename(rpt)
    vers[-1]["快照时间"] = f"{datetime.now():%Y-%m-%d %H:%M}"
    vers[-1]["标记"] = tag
    # 本次回测开跑前自动存的代码快照（engine._snap_code 落地）——
    # 记录它是为了让「工具/回滚.py」知道该还原成哪一版代码
    try:
        from lib import code_snapshot
        _snap = code_snapshot.最新(策略, 品种名)
        if _snap:
            vers[-1]["代码快照"] = _snap
    except Exception:
        pass
    if 周期:
        L["周期"] = 周期
    if 区间:
        L["区间"] = 区间
    写账本(品种名, 策略, L)
    out = 渲染(品种名, 策略)
    print(f"[汇总] 已刷新：{out}")

    # 负收益时给一条"怎么退回去"的提示（默认干跑，要真删得加 --go）
    收益率 = str(指标.get("收益率", ""))
    if 收益率.startswith("-"):
        print(f"[提示] 本次修改收益率为 {收益率}（负收益）。")
        print("       若判定这次改动不成立、要退回上一版：")
        print(f"       python 工具/回滚.py {品种键} {策略} --go")
        print("       （或双击 工具\\回滚.bat）它会：还原上一版代码 + 删掉本条记录 + "
              "本次报告进回收站。")
    return out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    only_render = "--render" in sys.argv
    if len(args) < 2:
        print(__doc__)
        return 1
    品种键, 策略 = args[0], args[1]
    品种名 = args[2] if len(args) > 2 else None
    if only_render:
        if 品种名 is None:
            from config_symbols import get
            品种名 = get(品种键)[0]
        print(渲染(品种名, 策略))
        return 0
    更新(品种键, 策略, 品种名=品种名)
    return 0


if __name__ == "__main__":
    sys.exit(main())
