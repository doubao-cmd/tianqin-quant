# PIT 口径说明（point-in-time 层）

> 生成：2026-10-19 19:44 ｜ 对应 D1 缺口（清单 0.4 交付物，此前只存在于代码注释里）
> 本文**以源码为准**（`pit.py` 23,659 B / `_t_pit.py` 14,184 B），不是凭设计意图复述。
> 配套：`非价格数据源清单.md`（哪些源能接）、`方向B_非价格数据_上下文交接_20261019.md`（红线 R1-R7）

---

## 一、这个层在解决什么

外部数据（仓单、持仓排名、库存、基差）有一个价格数据没有的坑：**它描述的是某段时间的状态，但你在另一个时刻才看得到它。**

如果不区分这两件事，回测就会**用 10 月 20 日才知道的仓单，去 10 月 18 日下单**。这种前视不会报错，只会让回测好看得莫名其妙——价格类策略当年就是这么攒出"假显著"的。

PIT 层的唯一职责：**让「什么时候能看到」变成一条可以查询的硬约束，而不是靠人记得。**

---

## 二、三个时刻（核心，背下来）

| 字段 | 含义 | 格式 | 例 |
|---|---|---|---|
| `period_start` / `period_end` | 这条数据**描述的时段**（自然日） | `YYYY-MM-DD` | `2026-10-16` |
| `published_at` | 交易所**实际公布**的时刻（含时分） | `YYYY-MM-DDTHH:MM:SS` | `2026-10-16T15:35:00` |
| `available_at` | **你在系统里最早能用它**的时刻 = `published_at + margin` | 同上 | `2026-10-16T16:35:00` |

**唯一合法的对齐口径**：查询/回测只用 `available_at <= as_of`。
**禁止**：拿 `period_end` 或 `period_start` 去和 K 线日期 join。这一条由 PIT-4 静态扫描兜底。

---

## 三、五条硬规则（代码强制的，不是约定）

### 规则1 · `published_at` 不许只给日期

`norm_dt()` 遇到 `"2026-10-16"` 这种 date-only **直接抛 ValueError**：

```
published_at 只给了日期没有时分：'2026-10-16'
  清单 §1.2 要求公布时刻精确到时分；不知道时分就去核实，不许猜。
```

理由：`"2026-10-16"` 会被悄悄补成 `00:00`，等于凭空多给 15 小时可用性。

### 规则2 · `published_at >= period_end`

`make_row()` / `validate()` 双重校验，DB 层还有 `CHECK (published_at >= period_end)` 兜底。

> 数据不可能在时段结束**之前**公布。触发这个报错，十次有九次是**把 period 填成了公布日**。

### 规则3 · `available_at` 不许偷减安全边际

`available_at` 一般**不要传**，由 `available_at_for()` 自动算：

```
available_at = published_at + margin_for(source)
margin_for() 查 MARGIN_SEC 表，未登记源一律 DEFAULT_MARGIN_SEC = 3600 秒（+1h，保守）
```

显式传入比 `published_at + margin` 更早的值会抛：

```
显式 available_at(...) 小于 published_at+margin(...)：不要在写入口偷减安全边际。
要收紧请改 MARGIN_SEC 并写清理由。
```

`MARGIN_SEC` 的语义是「**在已核实公布时刻之上**再加多少」的防抖缓冲，**不是公布时刻本身**。目前 10 个源全是 3600，属未收紧状态。

### 规则4 · `as_of` 必填，没有默认值

```python
query_available(as_of, ...)      # as_of 是必填位置参数 → 不传 TypeError
latest_snapshot(as_of, ...)      # 同上
```

设计意图写在 docstring 里：*不传就等于"取全量"，而取全量之后的每一次 join 都是一个未来函数入口。把接口做成"拿不到全量"，比写十遍注释有用。*

唯一能拿全量的口子是 `read_all_for_audit()` —— **只给核对/审计用，策略代码里禁止调用**。

### 规则5 · 只追加，不许静默改历史

`write_records(on_conflict="raise")`（默认）：

| 情况 | 行为 |
|---|---|
| 主键不存在 | 插入 |
| 主键存在且 `value / available_at / raw_url / published_at` 全同 | 跳过（幂等，算 `unchanged`） |
| 主键存在但 `value` 不同 | **整批回滚 + 抛异常** |

```
★ 同主键已存在且 value 不同 —— 拒绝覆盖（这会悄悄改掉历史）。
  这是「修订」不是「重跑」：请用 next_revision() 抬版本号后重写
```

主键 = `(source, symbol, contract, metric, period_start, period_end, revision)`
⇒ 同一时段数据被交易所修正，正确做法是 `revision += 1` 新增一行，旧版本永久留档。

---

## 四、表结构（np_data）

```sql
source, symbol, contract, metric,          -- 谁 / 哪个品种 / 哪个合约 / 哪个指标
period_start, period_end,                  -- 描述时段（自然日）
published_at, available_at, margin_sec,    -- 三时刻 + 用了多少缓冲
value, revision, raw_url, fetched_at       -- 值 / 版本 / 原始地址 / 抓取时刻

PRIMARY KEY (source, symbol, contract, metric, period_start, period_end, revision)
CHECK (published_at >= period_end)
CHECK (available_at >= published_at)
CHECK (period_start <= period_end)
CHECK (revision >= 0)
INDEX ix_np_avail ON np_data(available_at)   -- 查询走这个
```

- `contract` 空串 `''` 表示「品种级」指标（没区分合约）。
- `symbol` 入库时统一 `.upper()`。
- 时间字符串统一由 `norm_dt()` / `norm_date()` 规范化，**别在别处手搓字符串**。

---

## 五、正确用法（两行代码）

```python
from pit import latest_snapshot, query_available

# 策略层要的：某时点每个 (source,symbol,contract,metric) 的最新一版
snap = latest_snapshot(as_of="2026-10-16T16:40:00", source="shfe_receipt")

# 要历史序列：也是在 as_of 截止的前提下取
rows = query_available(as_of="2026-10-16T16:40:00",
                       source="shfe_receipt", symbol="CU", metric="receipt")
```

### 三种必须避免的错法

| ❌ 错法 | 为什么错 | ✅ 正确 |
|---|---|---|
| `df.merge(kline, on="date")`，`date` 是 `period_end` | 公布在收盘后，收盘价却已用上当天数据 = 前视 1 个 bar | 按 `available_at` 对齐：信号在 `available_at` 之后的第一根 bar 才生效 |
| `SELECT * FROM np_data` 全量拉出再自己筛日期 | 全量出口一旦存在，筛条件迟早写漏 | `query_available(as_of=...)` |
| 发现数据错了直接 `UPDATE np_data SET value=...` | 历史被悄悄改写，之前跑过的结论全部失去可复现性 | `next_revision()` 抬版本，新增一行 |

---

## 六、`published_at` 从哪来（最容易糊弄过去的一步）

**不许抄文档。** 交易所官网写「15:00 后」「结算后」是规则描述，不是实测上界，也不是 `available_at` 的依据。

唯一合法来源：

```bash
python probe_sources.py --watch 2026-10-20
```

它打出每个源的 `first_seen_at`（轮询观测到的**首次可见时刻**），这才是 `published_at` 的实测上界。

现状：**11 个源没有一个完成 `--watch` 实测**（见 `非价格数据源清单.md` §四）。所以当前 `np_data` 表 0 行是**正确状态**，不是欠账——在没有实测公布时刻之前往里灌数据，等于把前视固化进库。

---

## 七、回归测试（改任何东西前后都跑）

```bash
C:\Users\cc\.workbuddy\binaries\python\envs\tqsdk\Scripts\python.exe _t_pit.py
```

| 用例 | 检验内容 | 当前 |
|---|---|:-:|
| **PIT-1 截断检验** | 把 `available_at > t` 的行**物理删除**后重算，结果必须与 `query_available(as_of=t)` 完全一致 | PASS（10 个时点逐字段一致） |
| **PIT-2 时序单调** | 每个 key 的 period 链条有序、无重叠冲突 | PASS（6 项检查，含 1 条修订版） |
| **PIT-3 未来不可见** | 造一条 `published_at=明天` 的记录，`as_of=今天` 必须查不到 | PASS |
| **PIT-4 接口不可误用** | ① `as_of` 不传必须 `TypeError`；② 静态扫描源码禁止 period 等值 join | PASS |

**4/4 PASS**（2026-10-19 19:4x 实跑复核，非引用旧记录）。

★ PIT-1 顺带给出了本层最硬的量化证据：**阴性对照**——同样的数据，按 `period` 日期 join 在 **6/10 个时点**会得到与正确结果**不同**的答案。
也就是说「按 period join」不是一个洁癖问题，它在六成时点会实际改变你的持仓判断。

注意：测试用的是 13 条**假夹具**，不是真实数据——它验证的是**机制**，不验证数据正确性。

---

## 八、已知缺口（诚实清单）

| # | 缺口 | 影响 | 何时补 |
|:-:|---|---|---|
| 1 | `np_data` 表 **0 行** | PIT 层目前是空转，没有任何真实数据流过 | D6/D7 管道 |
| 2 | 11 源**全部未做 `--watch` 实测**公布时刻 | 即使接数据，`published_at` 也只能先用规则描述占位 ⇒ 前视风险仍在 | D6/D7 前置 |
| 3 | `MARGIN_SEC` 10 个源全是默认 3600 | 安全边际未按源收紧，偏保守（不会造成前视，只会少用一天数据） | 实测后收紧 |
| 4 | PIT-4 的静态扫描只覆盖 period 等值 join | 用 `<`、`BETWEEN`、字符串切片拼日期的写法扫不到 | 有真实查询代码后再补 |

---

## 九、一句话总结

> **`available_at` 是这个层的全部。** 写的每一行都要回答「我什么时候才知道这件事」，
> 查的每一条都要回答「站在 `as_of` 那一刻，我看到的和当时真实看到的完全一样吗」。
