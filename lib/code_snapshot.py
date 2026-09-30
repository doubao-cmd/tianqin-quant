# -*- coding: utf-8 -*-
"""
代码快照（回滚用）
==========================================================
要解决的问题
------------
"改一处策略逻辑 -> 跑回测 -> 结果是负收益 -> 想退回改动前的样子"，
但代码已经被改掉了、手头没有旧版 —— 只能靠记忆手改回去，容易漏。

做法
----
**每次回测刚开跑时**（engine.run_backtest / run_backtest_align 进去的位置）
自动把「正在跑的那个策略脚本 + 同品种的对侧脚本 + lib/engine.py」原样存一份：

    工具/_归档/代码快照/<策略目录>/<品种>/<日期_时分秒>/
        双均线_回测.py      <- 策略脚本（按当前在跑的那个自动识别）
        双均线_模拟盘.py    <- 同品种的对侧脚本（回测 <-> 模拟盘），成对改就必须成对退
        engine.py           <- 引擎
        _来源.json          <- 每个副本原本在哪儿 + 内容指纹

约定
----
· 代码一个字都没动（指纹相同）就不重复存，反复回测不会堆垃圾。
· 存的是**原始字节**，行尾（engine.py=LF / 策略脚本=CRLF）原样保留，
  还原时按 _来源.json 的相对路径覆盖回去即可，不需要再跑「行尾归一」。
· 目录名以 `_` 开头的（如回滚前自动备份的 `_回滚前_...`）不算"正式快照"，
  列/最新 都会跳过它们。

配套：工具/回滚.py —— 还原代码 + 删掉这次修改的汇总记录与报告文件。
"""
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib.strategy_dirs import 策略目录名

SNAP_ROOT = os.path.join(_ROOT, "工具", "_归档", "代码快照")

# 每次快照固定一起存的文件（相对项目根）；策略脚本由 sys.argv[0] 自动识别
固定文件 = ["lib/engine.py"]

# 「共享库」= 被所有策略共用的文件。它在快照里存的是**拍快照那一刻**的版本，
# 而别的策略可能在那之后又往同一个文件里加了东西（如 05 摆动线整块 500+ 行）。
# 旧快照直接覆盖 = 把别的策略的代码一起抹掉 —— 2026-09-23 回滚 02 的时候就这么
# 丢过一次 05 摆动线。所以共享库还原前先比对顶层定义，会「倒退」的就跳过。
共享库 = list(固定文件)

# 「回测」与「模拟盘」是同一套逻辑的两个入口，成对修改、也必须成对回滚。
# 只存「当前在跑的那一个」会有漏：跑回测时改了模拟盘脚本 -> 快照里没有它 ->
# 回滚后回测（旧）与模拟盘（新）静默不一致。故把对侧目录的同品种 .py 一并纳入。
_阶段对侧 = {"回测": "模拟盘", "模拟盘": "回测"}

元数据名 = "_来源.json"


def _指纹(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def _结构parts(rel):
    """把相对路径拆成 (前导前缀, 结构parts)。

    旧结构及其归档后的形态都归一到同一套 parts，下游只按 parts 判断结构：

        策略/01_双均线/回测/PVC/x.py            -> ("策略", ['01_双均线','回测','PVC','x.py'])
        _归档/策略_旧结构/01_双均线/回测/PVC/x.py -> ("_归档/策略_旧结构", [同上])

    （2026-09-24 旧结构整体归档到 _归档/策略_旧结构/，2026-09-27 又搬回根目录 `策略/`。
    两种形态都仍然认 —— 万一有脚本被留在归档区或被搬回去，快照机制照旧工作。）
    """
    p = (rel or "").replace("\\", "/").split("/")
    if len(p) >= 3 and p[0] == "_归档" and p[1] == "策略_旧结构":
        return "_归档/策略_旧结构", p[2:]
    if p and p[0] == "策略":
        return "策略", p[1:]
    return None, p


def 运行脚本():
    """当前正在跑的是哪个策略脚本（相对项目根的路径）；识别不出就返回 None。

    兼容三代形态（2026-09-24 池化重构 + 旧结构归档）：
      旧：策略/<策略目录>/<回测|模拟盘>/<品种>/<文件>.py
      新：组合/<策略>__<品种key>.py       （回测 / 模拟盘都从这里跑）
      归档：_归档/策略_旧结构/<策略目录>/<回测|模拟盘>/<品种>/<文件>.py
    """
    try:
        p = os.path.abspath(sys.argv[0])
    except Exception:
        return None
    if not (os.path.isfile(p) and p.lower().endswith(".py")):
        return None
    rel = os.path.relpath(p, _ROOT).replace("\\", "/")
    if (rel.startswith("策略/") or rel.startswith("组合/")
            or rel.startswith("_归档/策略_旧结构/")):
        return rel
    return None


def 对侧目录(rel):
    """旧结构：<策略目录>/<回测|模拟盘>/<品种>/ -> 同品种的对侧目录（含归档形态）。

    识别不出结构就返回 None —— 宁可少存，也不瞎猜路径。

    ⚠️ 池化结构不适用（组合层一个文件同时是回测与模拟盘的入口，没有"对侧目录"），
    那种情况按设计返回 None：组合文件本身会被存入，不需要再挂一个对侧。
    """
    pre, parts = _结构parts(rel)
    if pre is None or len(parts) != 4:
        return None
    if parts[1] not in _阶段对侧:
        return None
    对面 = _阶段对侧.get(parts[1])
    if not 对面:
        return None
    return "/".join([pre, parts[0], 对面, parts[2]])


def 策略标识(rel):
    """从脚本相对路径里认出「(策略名, 品种名)」，三代形态都认。

    旧：策略/03_布林带/回测/PVC/布林带_PVC.py -> ("布林带", "PVC")
    归档：_归档/策略_旧结构/03_布林带/回测/PVC/... -> 同上（按归档后的编号目录名）
    新：组合/布林带__V.py                      -> ("布林带", "V")
      策略池旧写法：策略池/布林带.py            -> ("布林带", None)
      策略池新写法：策略池/通道突破/通道突破.py  -> ("通道突破", None)  ← 一个策略一个文件夹
    认不出返回 (None, None)。
    """
    pre, parts = _结构parts(rel)
    if pre is not None:
        # <策略目录>/<回测|模拟盘>/<品种>/<文件>
        return (parts[0], parts[2]) if len(parts) == 4 else (None, None)
    if len(parts) == 2 and parts[0] == "组合":
        stem = os.path.splitext(parts[1])[0]
        if "__" in stem:
            策略, 品 = stem.split("__", 1)
            return (策略, 品)
        return (stem, None)
    if parts and parts[0] == "策略池":
        # 旧：策略池/布林带.py                      -> ("布林带", None)
        # 新：策略池/通道突破/通道突破.py            -> ("通道突破", None)
        if len(parts) == 2 and parts[1].endswith(".py"):
            return (os.path.splitext(parts[1])[0], None)
        if len(parts) == 3:
            return (parts[1], None)
        return (None, None)
    return (None, None)


def 待存清单():
    """本次要存哪些文件（相对项目根）。

    ★ 旧结构（策略/<策略>/<回测|模拟盘>/<品种>/）：
      当前在跑的策略脚本 + 同品种的「对侧」脚本 + `lib/engine.py`。

    ★ 新池化结构（组合/<策略>__<品种>.py）：
      组合文件 + 它引用的策略池文件 + `lib/engine.py`。
      组合文件是显式配对，本身就是"策略×品种"的完整声明，
      所以不需要（也不存在）对侧脚本。
      ⚠️ 按设计**不自动推断**策略池依赖 —— 依赖靠组合文件里的 import，
      这里只做静态扫描（见 _组合引用的策略池）。
    """
    rels = []
    s = 运行脚本()
    if s:
        rels.append(s)
        if s.startswith("组合/"):
            for r in _组合引用的策略池(s):
                if r not in rels:
                    rels.append(r)
        else:
            对 = 对侧目录(s)
            if 对:
                d = os.path.join(_ROOT, 对.replace("/", os.sep))
                if os.path.isdir(d):
                    for fn in sorted(os.listdir(d)):
                        if fn.endswith(".py"):
                            r = f"{对}/{fn}"
                            if r not in rels:
                                rels.append(r)
    for r in 固定文件:
        if r not in rels and os.path.isfile(os.path.join(_ROOT, r)):
            rels.append(r)
    return rels


def _组合引用的策略池(rel):
    """静态扫描组合文件引用了哪些策略池文件。

    认两种写法：
      ① `from 策略池.布林带 import ...`        -> 策略池/布林带.py
      ② `_读模块("策略池/通道突破/通道突破.py")` -> 策略池/通道突破/通道突破.py  ← 当前写法
    返回相对路径列表（只保留磁盘上真实存在的）。
    """
    import re
    p = os.path.join(_ROOT, rel.replace("/", os.sep))
    try:
        with open(p, encoding="utf-8", errors="ignore") as f:
            t = f.read()
    except Exception:
        return []
    候选 = [f"策略池/{名}.py" for 名 in re.findall(r"from\s+策略池\.(\w+)\s+import", t)]
    # 任何引号里出现的 策略池/xxx.py 或 策略池/xxx/xxx.py
    候选 += re.findall(r"""["'](策略池/[\w/]+\.py)["']""", t)
    out = []
    for r in 候选:
        if r not in out and os.path.isfile(os.path.join(_ROOT, r.replace("/", os.sep))):
            out.append(r)
    return out


def 目录(策略, 品种名):
    return os.path.join(SNAP_ROOT, 策略目录名(策略), 品种名)


def 列(策略, 品种名):
    """该品种已有的正式快照目录名，按时间升序（最新在最后）。`_` 开头的不算。"""
    d = 目录(策略, 品种名)
    if not os.path.isdir(d):
        return []
    return sorted(e for e in os.listdir(d)
                  if not e.startswith("_") and os.path.isdir(os.path.join(d, e)))


def 最新(策略, 品种名):
    ls = 列(策略, 品种名)
    return ls[-1] if ls else None


def 读元数据(策略, 品种名, 快照名):
    p = os.path.join(目录(策略, 品种名), 快照名, 元数据名)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def 存(策略, 品种名, 前缀="", 去重=True, 静默=False):
    """把当前代码存一份快照。

    返回新建的快照目录名；"代码没变"或"识别不出策略脚本"时返回 None。
      前缀：非空时目录名变成 `_<前缀>_<时间>`（回滚前的自动备份走这条路，不进正式序列）
      去重：True 时若与最近一份正式快照指纹完全相同则不新建
    """
    rels = 待存清单()
    if not rels:
        return None

    指纹 = {r: _指纹(os.path.join(_ROOT, r)) for r in rels}

    if 去重 and not 前缀:
        last = 最新(策略, 品种名)
        if last:
            m = 读元数据(策略, 品种名, last)
            if m and m.get("指纹") == 指纹:
                return None                      # 代码一个字都没动

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    名称 = f"_{前缀}_{stamp}" if 前缀 else stamp
    目标 = os.path.join(目录(策略, 品种名), 名称)
    os.makedirs(目标, exist_ok=True)

    文件映射, 已用 = {}, set()
    for r in rels:
        base = os.path.basename(r)
        if base in 已用:                          # 同名不同目录（目前不会发生）
            base = r.replace("/", "__")
        已用.add(base)
        shutil.copy2(os.path.join(_ROOT, r), os.path.join(目标, base))
        文件映射[base] = r

    with open(os.path.join(目标, 元数据名), "w", encoding="utf-8") as f:
        json.dump({"时间": f"{datetime.now():%Y-%m-%d %H:%M:%S}",
                   "策略": 策略, "品种": 品种名,
                   "指纹": 指纹, "文件": 文件映射,
                   "说明": "回测开跑前的代码存档；由 工具/回滚.py 还原。"},
                  f, ensure_ascii=False, indent=2)

    if not 静默:
        print(f"[快照] 已存档当前代码 -> 工具/_归档/代码快照/"
              f"{策略目录名(策略)}/{品种名}/{名称}/")
    return 名称


def _顶层定义(路径):
    """文件里的顶层 def / class 名（只用来判断「谁更新」，不解析语法）。"""
    import re
    try:
        with open(路径, encoding="utf-8", errors="ignore") as f:
            t = f.read()
    except Exception:
        return set()
    return set(re.findall(r"^(?:def|class)\s+(\w+)", t, re.M))


def 共享库会误伤(源, 目标):
    """快照里的共享库会不会「倒退」：返回**当前有、快照里没有**的顶层定义名。

    空集 = 快照是当前的超集，覆盖安全。
    非空 = 快照更旧，覆盖会抹掉别的策略后加的功能 —— 该跳过还原。
    """
    旧, 新 = _顶层定义(源), _顶层定义(目标)
    return (新 - 旧) if 新 else set()


def 还原(策略, 品种名, 快照名, dry=True, 允许旧共享库=False):
    """按 _来源.json 里的相对路径，把快照里的文件覆写回项目。

    返回 [(相对路径, 状态文案)]，状态含「将还原 / 已还原 / 一致，跳过 /
    缺文件 / 跳过还原（…）」。最后一种是共享库防倒退保护拦下的
    （见文件头「共享库」注释）；确定要退回旧引擎再加 允许旧共享库=True。
    """
    m = 读元数据(策略, 品种名, 快照名)
    if not m:
        raise FileNotFoundError(
            f"快照缺少 {元数据名}，无法还原：{os.path.join(目录(策略, 品种名), 快照名)}")
    源 = os.path.join(目录(策略, 品种名), 快照名)
    out = []
    for base, rel in m.get("文件", {}).items():
        sp = os.path.join(源, base)
        tp = os.path.join(_ROOT, rel)
        if not os.path.isfile(sp):
            out.append((rel, "缺文件"))
            continue
        if rel in 共享库 and not 允许旧共享库:
            缺 = 共享库会误伤(sp, tp)
            if 缺:
                名 = sorted(缺)
                样例 = "、".join(名[:3]) + (" …" if len(名) > 3 else "")
                out.append((rel, f"跳过还原（快照更旧，缺 {len(名)} 个定义：{样例}）"))
                continue
        if os.path.isfile(tp) and _指纹(sp) == _指纹(tp):
            out.append((rel, "一致，跳过"))
            continue
        if not dry:
            os.makedirs(os.path.dirname(tp), exist_ok=True)
            shutil.copy2(sp, tp)
        out.append((rel, "将还原" if dry else "已还原"))
    return out


def 快照路径(策略, 品种名, 快照名):
    return os.path.join(目录(策略, 品种名), 快照名)
