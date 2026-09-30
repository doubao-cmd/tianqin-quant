# -*- coding: utf-8 -*-
"""
回滚 —— 代码 + 汇总报告，一起退回上一版
==========================================================
场景：改了一处策略逻辑 -> 跑回测 -> 收益率为负 -> 判定这次改动不成立，
      想退回改动之前的样子。手改代码容易漏，汇总页里那条记录也还挂着。

本工具一条命令做三件事：
   1. **代码还原**：策略脚本 + lib/engine.py 还原成上一版
      （用回测开跑前自动存好的快照，见 lib/code_snapshot.py）
      ⚠ lib/engine.py 是所有策略共用的 —— 若旧快照比当前版本还缺功能
        （别的策略后加的），会跳过它、只还原策略脚本，避免误伤；
  2. **删掉记录**：这次修改在 工具/_账本/<品种>_<策略>.json 里的那一条，
     汇总页随之重渲染，那一条就消失了
  3. **删掉报告**：这次修改产出的回测报告 / 成交明细，丢进回收站（可还原）

用法（在项目根目录跑）：
    python 工具/回滚.py                           # 不带参数 = 引导式（问品种/策略，再确认）
    python 工具/回滚.py --list                    # 看所有账本的版本、收益率、正负
    python 工具/回滚.py V 双均线                  # 干跑：只列"将做什么"，不动任何文件
    python 工具/回滚.py V 双均线 --go             # 真执行（默认撤最后一条）
    python 工具/回滚.py V 双均线 --序号 1 --go    # 撤指定那一条（序号见 --list）
    python 工具/回滚.py V 双均线 --keep-code --go # 只清记录 + 报告，代码自己改回去
双击 工具/回滚.bat = 走第一条（引导式）。那个 bat 里刻意只有 ASCII，
中文引导语全部由本脚本打印 —— bat 里成段中文 echo 会被 cmd 随机截断。

安全闸门：
  1. 默认干跑，加 --go 才动手；
  2. 账本至少保留 1 条（要清空必须显式加 --allow-empty）；
  3. 动手前先把**当前代码**再存一份快照（`_回滚前_...`），还原错了还能再回来；
  4. 删文件一律走回收站，未成功的会报出来；
  5. 快照若被别的版本共用，只还原、不删除 —— 绝不删掉还要用的代码。
  6. 撤"中间某条"（不是最后一条）时不动报告文件 —— 磁盘上那份其实属于更新的版本。

退出码（工具/回滚.bat 靠它区分提示语）：
  0 = 成功（干跑也算）    1 = 报错（用错参数 / 找不到账本）
  2 = 执行完但复核没过    3 = 安全拒绝（没有可撤的条目，什么都没动）
"""
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))          # .../天勤量化/工具
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from lib import code_snapshot
from lib.recycle import 移入回收站
from lib.strategy_dirs import 短名们, 映射 as _策略映射
import 汇总报告 as 汇总


def 规整策略(名):
    """策略名归一：'双均线' / '01_双均线' 都认。"""
    if 名 in _策略映射:
        return 名
    反查 = 短名们(名)
    return 反查[0] if 反查 else 名


def 解析品种(值):
    """把用户写的品种解析成账本里用的品种名（中文名）。

    支持 V（账号键）和 PVC（中文名）两种写法。**故意不走 config_symbols.get()**，
    因为它写错时抛的是 SystemExit（BaseException），会直接掀掉整个进程、
    只留一行"品种代号写错了"，用户看不到"账本里其实有哪些品种"。
    这里识别不出就原样返回，交给后面"找不到账本"那一步去报清楚。
    """
    a = str(值).strip()
    try:
        from config_symbols import SYMBOLS
        if a.upper() in SYMBOLS:
            return SYMBOLS[a.upper()][0]
        for _, v in SYMBOLS.items():
            if a == v[0]:
                return v[0]
    except Exception:
        pass
    return a


def 现有账本():
    d = 汇总.LEDGER_DIR
    if not os.path.isdir(d):
        return []
    return sorted(f[:-5] for f in os.listdir(d) if f.endswith(".json"))


def _条目标记(v):
    """该条目属于哪个样本标记（in / out）；老条目没有字段就从报告文件名推。"""
    if v.get("标记"):
        return v["标记"]
    m = re.search(r"_(in|out)\.html$", str(v.get("报告", "")))
    return m.group(1) if m else ""


def 该条报告文件(品种名, 策略, v):
    """这次修改产出的报告文件（html + 成交明细 txt）。

    关键背景：报告文件名是**按 品种+策略+周期+标记 固定的、不带日期**
    （PVC_双均线_5min_in.html 每次回测都覆盖同一个文件）。所以磁盘上那个
    文件装的永远是"最后一次跑出来的内容" —— 也就是正要撤掉的这一版。
    因此这里不去看"有没有被别的版本引用"（那样会永远删不掉），而是：

      · 只在 回测结果/<策略目录>/<品种>/ 里找；
      · 只认 <品种>_<策略>_* 且标记相同（撤 _in 不碰 _out）；
      · _汇总 汇总页永不删（它的内容来自账本，不是回测产物）。

    撤"中间某条"（不是最后一条）的情况由调用方拦下 —— 那时磁盘上的文件
    其实属于更新的那一版，不能删。
    """
    rel = str(v.get("报告", "") or "")
    if not rel:
        return []
    d = os.path.dirname(os.path.join(_ROOT, rel))
    tag = _条目标记(v)
    out = []
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            p = os.path.join(d, fn)
            if not os.path.isfile(p) or not fn.lower().endswith((".html", ".txt")):
                continue
            if "_汇总" in fn:
                continue
            if not fn.startswith(f"{品种名}_{策略}_"):
                continue
            stem = os.path.splitext(fn)[0]
            if tag and not stem.endswith(f"_{tag}"):
                continue
            out.append(p)
    if not out:                                  # 目录不存在/没匹配到 -> 退回"记录的那一个"
        p = os.path.join(_ROOT, rel)
        if os.path.isfile(p):
            out.append(p)
    return out


def 选还原快照(vers, idx, 策略, 品种名):
    """要还原成哪一份快照 —— 也就是"上一版代码"。返回 (快照名或None, 为什么)。"""
    自己的 = vers[idx].get("代码快照")
    if idx > 0:
        cand = vers[idx - 1].get("代码快照")
        if cand and cand != 自己的:
            return cand, f"上一版记录的快照（{vers[idx-1].get('版本', '')}）"
    snaps = [s for s in code_snapshot.列(策略, 品种名) if s != 自己的]
    if snaps:
        return snaps[-1], "上一版没有记录快照，取最近一份非本次的快照"
    return None, "没有可用的历史快照（这一版可能是第一次跑）"


def 列清单():
    d = 汇总.LEDGER_DIR
    files = sorted(f for f in os.listdir(d) if f.endswith(".json")) if os.path.isdir(d) else []
    if not files:
        print("还没有任何账本（工具/_账本/ 是空的）。")
        return 0
    for fn in files:
        品种名, _, 策略 = fn[:-5].partition("_")
        L = 汇总.读账本(品种名, 策略)
        vers = L.get("版本", [])
        print("=" * 74)
        print(f"  {品种名} · {策略}     账本：工具/_账本/{fn}")
        print("=" * 74)
        for i, v in enumerate(vers):
            收益率 = str((v.get("指标") or {}).get("收益率", "—"))
            负 = 收益率.startswith("-")
            print(f"  [{v.get('序号', i + 1)}] {v.get('版本', '')}  {v.get('日期', '')}"
                  f"  {v.get('标题', '')}")
            print(f"        收益率 {收益率}{'   <-  负收益，可考虑回滚' if 负 else ''}"
                  f"    代码快照 {v.get('代码快照') or '（无）'}")
        snaps = code_snapshot.列(策略, 品种名)
        print(f"  代码快照 {len(snaps)} 份：{', '.join(snaps) if snaps else '（暂无）'}")
        print()
    print("回滚某一条：python 工具/回滚.py <品种键> <策略> [--序号 N] --go")
    return 0


def 执行(品种键, 策略, 序号=None, go=False, keep_code=False, allow_empty=False,
         允许旧共享库=False):
    品种名 = 解析品种(品种键)
    try:
        L = 汇总.读账本(品种名, 策略)
    except FileNotFoundError:
        print(f"!! 找不到账本：工具/_账本/{品种名}_{策略}.json")
        有 = 现有账本()
        print(f"   现有账本（{len(有)} 个）：{', '.join(有) if 有 else '（一个都没有）'}")
        print("   品种可写 V 或 PVC；策略可写 双均线 或 01_双均线。")
        return 1

    vers = L.get("版本", [])
    if not vers:
        print("!! 账本里没有版本记录，无事可做。")
        return 3

    # ---- 找到要撤的那一条 ----
    if 序号 is None:
        idx = len(vers) - 1
    else:
        idx = next((i for i, v in enumerate(vers) if str(v.get("序号", i + 1)) == str(序号)), None)
        if idx is None:
            print(f"!! 账本里没有序号 {序号} 的条目（现有序号："
                  f"{[v.get('序号', i + 1) for i, v in enumerate(vers)]}）。")
            return 3
    v = vers[idx]

    if len(vers) == 1 and not allow_empty:
        print("!! 账本里只剩这 1 条了，删掉汇总页就没有基准可对照。")
        print("   确实要清空请显式加 --allow-empty。已中止，什么都没动。")
        return 3                     # 3 = 安全拒绝（不是报错，什么都没动）

    # ---- 快照防误删：别的版本还在用这一份就不删 ----
    待删快照 = v.get("代码快照")
    for i, x in enumerate(vers):
        if i != idx and x.get("代码快照") == 待删快照:
            待删快照 = None

    还原目标, 还原理由 = 选还原快照(vers, idx, 策略, 品种名)

    # ---- 报告：只有"撤最后一条"时磁盘上那份才属于本次；撤中间某条要保 ----
    是最后一条 = (idx == len(vers) - 1)
    if 是最后一条:
        报告文件, 报告说明 = 该条报告文件(品种名, 策略, v), ""
    else:
        报告文件 = []
        报告说明 = (f"撤的不是最后一条 —— 磁盘上那份报告其实属于更新的 "
                    f"{vers[-1].get('版本', '')}（{vers[-1].get('标题', '')}），故保留不删。")

    # ---- 打印计划 ----
    print("=" * 74)
    print(f"  回滚：{品种名} · {策略}    {'干跑（不动任何文件）' if not go else '★ 正式执行'}")
    print("=" * 74)
    收益率 = str((v.get("指标") or {}).get("收益率", "—"))
    print(f"  要撤掉的这一条：[{v.get('序号', idx + 1)}] {v.get('版本', '')} "
          f"{v.get('日期', '')}  {v.get('标题', '')}")
    print(f"      收益率 {收益率}"
          f"{'（负收益）' if 收益率.startswith('-') else ''}")
    print(f"      原因 {v.get('原因', '')}")

    if keep_code:
        print("\n  ① 代码：--keep-code，跳过（代码维持现状，你自己改回去）")
    elif 还原目标:
        明细 = code_snapshot.还原(策略, 品种名, 还原目标, dry=True,
                                  允许旧共享库=允许旧共享库)
        print(f"\n  ① 代码：还原成快照 {还原目标}")
        print(f"      （{还原理由}）")
        for rel, 状态 in 明细:
            print(f"      · {rel}   {状态}")
        if any(状态.startswith("跳过还原") for _, 状态 in 明细):
            print("\n      ⚠ 共享库保护：上面标「跳过还原」的文件是所有策略共用的，")
            print("        这份快照比当前版本旧（缺了后来给别的策略加的功能），")
            print("        直接覆盖会把别的策略一起抹掉，故保留现状。")
            print("        确实要退回旧引擎：加 --engine 重跑（慎用）。")
    else:
        print(f"\n  ① 代码：跳过 —— {还原理由}")

    print(f"\n  ② 账本：删掉第 [{v.get('序号', idx + 1)}] 条"
          f"（删后剩 {len(vers) - 1} 条）-> 汇总页重渲染")

    if 报告说明:
        print(f"\n  ③ 报告：保留 —— {报告说明}")
    else:
        print(f"\n  ③ 报告：{len(报告文件)} 个文件丢进回收站"
              f"（报告名按 品种+策略+周期+标记 固定，每次回测覆盖同一个文件）")
        for p in 报告文件:
            print(f"      · {os.path.relpath(p, _ROOT)}")
        if not 报告文件:
            print("      （没有匹配到报告文件，可能还没跑过回测）")

    if 待删快照:
        print(f"\n  ④ 快照：删掉本次自己的那份 {待删快照}（丢回收站）")
    else:
        print("\n  ④ 快照：本次那份不删（没有 / 或还被别的版本共用）")

    if not go:
        print("\n" + "-" * 74)
        print("  [干跑] 上面就是将要做的全部动作，未改动任何文件。")
        print(f"  确认无误后加 --go 重跑：python 工具/回滚.py {品种键} {策略} --go")
        print("-" * 74)
        return 0

    # ---- 正式执行 ----
    print("\n" + "-" * 74)
    备份名 = code_snapshot.存(策略, 品种名, 前缀="回滚前", 去重=False)
    if 备份名:
        print(f"  [安全] 已把当前代码另存一份：{备份名}")
    else:
        print("  [安全] 没识别到策略脚本，未做代码备份（不影响后续步骤）")

    if not keep_code and 还原目标:
        明细 = code_snapshot.还原(策略, 品种名, 还原目标, dry=False,
                                  允许旧共享库=允许旧共享库)
        print(f"  [代码] 已还原成 {还原目标}")
        for rel, 状态 in 明细:
            print(f"         · {rel}   {状态}")
        if any(状态.startswith("跳过还原") for _, 状态 in 明细):
            print("  [代码] 注：共享库(lib/engine.py)因防倒退已保留现状，")
            print("         策略脚本本身已正常还原到上一版。")

    if 报告文件:
        残留 = 移入回收站(报告文件)
        if 残留:
            print(f"  [报告] !! {len(残留)} 个文件没能进回收站：")
            for p in 残留:
                print(f"         · {os.path.relpath(p, _ROOT)}")
        else:
            print(f"  [报告] {len(报告文件)} 个文件已进回收站（可右键还原）")

    if 待删快照:
        残留 = 移入回收站([code_snapshot.快照路径(策略, 品种名, 待删快照)])
        if 残留:
            print(f"  [快照] !! 未能移入回收站：{待删快照}")
        else:
            print(f"  [快照] {待删快照} 已进回收站")

    vers.pop(idx)
    L["版本"] = vers
    汇总.写账本(品种名, 策略, L)
    print(f"  [账本] 已删除第 [{v.get('序号', idx + 1)}] 条，剩 {len(vers)} 条")

    out = 汇总.渲染(品种名, 策略)
    print(f"  [汇总] 已重渲染：{os.path.relpath(out, _ROOT)}")

    # ---- 复核 ----
    坏 = [p for p in 报告文件 if os.path.exists(p)]
    if 坏 or not os.path.exists(out):
        print("\n!! 复核未通过，请检查上面的报错。")
        return 2
    print(f"\n  复核通过：该条记录已消失"
          + (f"、{len(报告文件)} 个报告文件已进回收站。" if 报告文件 else "。"))
    if not keep_code and 还原目标:
        print("  下一步：代码已还原成上一版，想确认就跑一次回测 ——")
        print("          报告会按原文件名重新生成，汇总页数字也会跟着刷新。")
    if 报告文件:
        print("  注：报告文件在回收站里，想留档可以还原。")
    print("-" * 74)
    return 0


引导语 = """============================================================
  回滚 —— 代码 + 汇总报告，一起退回上一版
============================================================

  什么时候用：改了一处策略逻辑，回测跑出来是负收益，
  判定这次改动不成立，想退回改动之前的样子。

  它会做三件事：
    1. 策略代码还原成上一版（回测开跑前自动存的代码快照）
    2. 删掉这次修改在账本里的那一条 —— 汇总页随之刷新，那条就没了
    3. 这次修改产出的回测报告 / 成交明细 丢进回收站（可还原）

  节奏：先干跑一遍给你看，你确认了它才真动手。
  只想看看不改东西：python 工具/回滚.py --list
"""


def _问(提示, 默认):
    try:
        s = input(f"{提示}（直接回车 = {默认}）：").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    return s or 默认


def 交互():
    """不带参数时走这里 —— 也是双击 工具/回滚.bat 的路径。

    引导文字全部由本脚本打印（不写在 bat 里）：bat 中成段的中文 echo
    在 cmd 切换代码页时会被随机截断，交给 Python 才稳定。
    """
    print(引导语)
    品种键 = _问("请输入品种：V / MA / SA / RB / M，或直接写中文名",
                 "V")
    if 品种键 is None:
        print("已取消，什么都没动。")
        return 0
    策略 = _问("请输入策略：双均线 / 均线排列", "双均线")
    if 策略 is None:
        print("已取消，什么都没动。")
        return 0
    策略 = 规整策略(策略)

    rc = 执行(品种键, 策略, go=False)
    if rc == 3:
        print("\n没有可撤的东西，什么都没动。")
        print("常见原因：账本里只剩 1 条了（那是最初的基准，删了就没得对照），")
        print("          或者品种 / 策略名写错了 —— 可以跑 python 工具/回滚.py --list 看看。")
        return 3
    if rc != 0:
        return rc

    ans = _问("确认按上面清单执行吗？输入 y 回车 = 执行，其它 = 取消", "取消")
    if ans is None or ans.strip().lower() not in ("y", "yes", "是"):
        print("\n已取消，什么都没动。")
        return 0
    print("\n开始执行 ...")
    return 执行(品种键, 策略, go=True)


def main():
    原始 = sys.argv[1:]
    if "--list" in 原始:
        return 列清单()
    if not 原始:
        return 交互()

    go = "--go" in 原始
    keep_code = "--keep-code" in 原始
    allow_empty = "--allow-empty" in 原始
    允许旧共享库 = "--engine" in 原始
    序号 = None
    if "--序号" in 原始:
        i = 原始.index("--序号")
        序号 = 原始[i + 1] if i + 1 < len(原始) else None
        原始 = 原始[:i] + 原始[i + 2:]

    args = [a for a in 原始 if not a.startswith("--")]
    if len(args) < 2:
        print(__doc__)
        return 1
    return 执行(args[0], 规整策略(args[1]), 序号=序号, go=go,
                keep_code=keep_code, allow_empty=allow_empty,
                允许旧共享库=允许旧共享库)


if __name__ == "__main__":
    sys.exit(main())
