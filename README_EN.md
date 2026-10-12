# tianqin-quant

**English | [中文](README.md)**

A personal futures-quant research and execution system, built end-to-end in Python:
research methodology → backtest engine → live-simulation execution → self-audit tooling.

The differentiating part of this repo is **not** the amount of code. It is the
**process discipline**, made auditable: every strategy is *pre-registered before it is run*,
every result carries hard-check verdicts, and **all strategy/factor experiments to date have been publicly falsified or frozen** — six pre-registration cards were judged invalid and archived, one of them at the frequency-budget stage before any engine code was written.

---

## 1. Why this repo exists

Most quant repos show winning backtests. This one shows the opposite, on purpose:

| | |
|---|---|
| **Pre-registration** | Signal definition, parameters, sample windows, pass criteria, hard checks and a change log are **written down and frozen before the first run**. After a run, parameters do not change. |
| **Hard checks** | A result is void unless: stop-loss exits never show a win; per-reason trade counts reconcile; initial capital equals the sum of per-trade P&L; every stop fill sits on the adverse side of entry. |
| **Honest archiving** | Strategies that fail are archived **with the failure analysis**, not deleted. §4 lists all of them. |
| **Reproducible verdicts** | 23 self-audit records document bugs that were found and fixed — including bugs that produced *plausible but wrong* numbers without raising any error. |

## 2. Repository map

```
策略/                     strategies 01–08 (all falsified/frozen); un-frozen engine strategy: 07_Bollinger
  06_MA13超短线/
    回测/                  per-symbol self-contained backtesters + 回测.bat
    模拟盘/                cloud simulation execution + 台账/ (ledgers)
lib/                      shared engine (lib/engine.py) + portfolio layer
工具/                     tooling: health checks, attribution, calibration, pre-registration cards
  _截面因子_持仓量/         cross-sectional open-interest factor (archived: falsified)
  _tsmom因子/              time-series momentum (archived: falsified)
  _出水芙蓉/                "lotus breakout" pattern (archived: untestable)
  _仓单因子/                warehouse-receipt change factor (archived: frequency-budget)
  _中轨趋势持有/            mid-band trend-hold signal (archived: falsified)
  _保本止损/                breakeven-stop experiment on 06 (archived: no improvement)
  _截面因子_基差/            cross-sectional basis factor (frequency budget passed; not yet run)
  _进场体检/                 entry-signal screener (L0 forward return / L1 with 2×ATR stop / gross-to-cost bar)
  _CTP接入/                  CTP / SimNow connectivity scaffold (market-data only by default)
回测结果/                  backtest artefacts (HTML detached; parameter JSON kept as evidence)
```

## 3. Results and what they do **not** mean

⚠️ **2026-10-11 correction — the previously published MA13 returns are void.**
Those numbers (+398.8% / Sharpe 3.667 and every per-symbol figure) came from a **bookkeeping bug**
in the equity curve: while holding, it accumulated `close[i] − close[i−1]` **without excluding the
entry/exit bars**, so every trade double-counted two boundary price gaps. Independently re-verified
on methanol 2021: **the error accounted for 139% of the reported return.** After the fix and a full
re-run, the strategy **has no edge**:

| Window | Positive-Sharpe symbols | Equal-weight return |
|---|---:|---:|
| Development 2021-2023 | 0/12 | ≈ −101.2% |
| Forward 2024-2025 | 2/12 | ≈ −95.7% |
| Probe 2026 | 0/12 | ≈ −69.4% |

The old "+399% against a 7.7% drawdown" figure is **retracted — it was never real.** The retired
numbers are preserved only as evidence in `回测结果/06_MA13超短线/失真清单_20261011.md`.

**None of the three windows qualifies as out-of-sample** — all were inspected while developing
(see §5); this data source simply offers no clean window.

## 4. Strategies that were falsified (kept on purpose)

| Strategy | Verdict | Evidence |
|---|---|---|
| Cross-sectional open interest ("volume up, price stalls") | **Invalid** | −141.1% in-sample; adding an ATR stop made it −112.2%. Two independent signals: still deeply negative *after* stops ⇒ wrong direction, and win rate *fell* (47%→34%) when stops truncated trades ⇒ the stopped-out trades mostly came back. Economically: in a 2016-2018 uptrend, "volume up + price stalls" is usually a **continuation**, not a top. |
| TSMOM (12-month time-series momentum) | **Invalid** | −74.5%, Sharpe −0.298, drawdown −80% — textbook momentum-crash regime (post-2016 supply-side rally). Three implementation bugs were fixed first: nanosecond dates sliced as strings (477 rebalances instead of 23), weights normalised by pool size instead of Σ(1/σ) (~10× hidden leverage), and a direction check comparing final state against historical signals. |
| "Lotus breakout" (volume spike piercing an MA cluster) | **Untestable** | With the frozen parameter, **0 signals** in 5 years × 9 symbols. Signal frequency: ≥3× avg volume → 0; ≥2× → 19; ≥1.5× → 57; no volume filter → 110. Daily commodity bars simply do not produce "multi-fold volume spikes". Loosening the threshold to exactly the pass mark was **refused** — that would be gate-driven cherry-picking. |

Two more have since been added to the list:

- **08 mid-band trend-hold** (daily MA20, the trend-following mirror of 06): in-sample equal-weight
  Sharpe **−0.69, 0/3 symbols positive** ⇒ falsified. Its signal layer carries almost no edge
  (soybean-meal gross P&L ≈ 0 over six years); adding stops only reshuffled the loss distribution.
- **Warehouse-receipt change factor**: rejected at the **frequency-budget stage**, before any engine
  code was written — the ratio `receipt_t / receipt_{t−20} − 1` is scale-sensitive and degenerates on
  small-base varieties (4 of 5 usable contracts). Methodological lesson: for any ratio/threshold
  signal, check "will it be on *almost always*?", not just "is there a signal?".
- **Breakeven stop on 06** (exit-side change, single variable): once open profit reaches 1×ATR, move
  the stop to the entry fill and leave it there. Five symbols (JM/M/JD/P/SR):
  equal-weight Sharpe **−1.650 → −1.926 (worse)**, only **2/5** improved ⇒ rejected on all three
  pre-registered criteria. Hard checks passed (books reconcile; the breakeven-exit bucket never shows
  a non-negative net P&L), and the five baseline runs reproduce the archived artefacts **trade-for-trade**.
  This is the **4th** same-family failure for "exit earlier" changes ⇒ 06's payoff structure is
  "small stop + let winners run"; any earlier exit cuts the winners first.

**Why exit-side tweaks cannot rescue 06 — an order-of-magnitude result.** Decomposing the baseline
trades (gross P&L = net P&L + both-side costs) over 5 symbols / 2021-2023:

| | Gross (signal layer) | Cost | Net | Gross/Cost |
|---|---:|---:|---:|---:|
| Total | **+47,962** | **1,074,942** | −1,026,980 | **0.045** |

Gross P&L is only **4.5% of cost** — 96% of the loss *is* cost. 06 is not "almost profitable", it is
**two orders of magnitude off**. Exit-logic changes move a variable worth <5% of the total, so they
**cannot** flip the sign (you would need 22× the gross, or 1/22 the cost). Also: ~62% of trades are
stopped out directly ⇒ the problem is **entry quality**, not exit. This is the same finding as the
08 experiment ("soybean-meal gross ≈ 0; cost turns ≈0 into negative").

**A whole *class* of price-based entry signals, falsified in one batch.** Following the above, a
screener (`工具/_进场体检/`) was built to judge a candidate entry *before* any strategy code exists:
**L0** = fixed-horizon forward gross return with **no stop** (does the signal carry directional
information?); **L1** = entry + fixed **2×ATR** stop (can the small system survive *after* cost?);
and a magnitude bar of **gross ÷ round-trip cost ≥ 2.0**. Six canonical, externally-fixed entries
(MA13 retrace — the anchor, matching 06's own entry condition — MA13 cross, 20-bar Donchian breakout,
20-bar time-series momentum, 2-bar reversal, volatility expansion) were run over **12 symbols × 5min ×
2021-2023, HOLD = 12 bars, 1 lot, costs included**:

- **0 of 72 combinations passed.** Best cell: `vol_break` on palm oil, **−0.41 points/trade** net.
- **Gross/cost ratio < 1 everywhere** — best values 0.83 / 0.77 / 0.72 / 0.64 vs a 2.0 bar.
- **L0 (pure entry, no stop) ≈ 0** ⇒ the signal layer itself carries almost no directional information;
  the stop is not what kills it.
- **44–57% of trades are stopped out directly** (same shape as 06's 62%).
- **Highest cost-per-point symbols are worst** (rubber 10.6 / hogs 14.35 / eggs 3.16), again matching
  the cost-to-ATR ordering above.

★ The most informative detail: the holding-period curve (1/2/3/6/12/24/48 bars) **does not rise
consistently across symbols** — coking coal and hogs rise monotonically, white sugar and eggs fall
monotonically, the rest is noise. A merely *slow* signal would rise for all 12; an inconsistent sign
is the signature of **noise, not a slow edge**.

This now cross-validates 06, 07 and 08: it is no longer "one strategy that didn't work out" but
**price-pattern entry signals as a class having no gross edge under this cost structure**. Further
research should therefore shift to **data dimensionality**, not another pattern.

Caveats stated up front: the 2021-2023 window is **contaminated**, so the screener can only **kill**
candidates, never confirm them; and this is a 72-comparison batch — though with a best cell of 0.83
the multiple-comparison question is moot. The screener's own future-function truncation test passed
**14,400/14,400 probes**.

## 5. Data-source limits (verified empirically, not assumed)

This matters more than any single backtest number:

- **Free-tier continuous-contract daily history starts at 2016-01-04.** `data_length=3000`
  (≈12 years) still returns nothing earlier, for all 9 symbols.
  ⇒ The retrievable window **coincides with** the already-inspected window, so
  "find a clean out-of-sample period" is **impossible with this data source** — it is a
  data limitation, not a discipline problem.
- **No historical klines for individual (non-continuous) contracts** — 4/4 probes returned
  empty, including the *current* front month. ⇒ Cross-commodity **roll yield / carry**
  cannot be computed *cleanly* with this feed. (A carry attempt — route 2 — was still run and
  archived as negative; see §4.)
- **Backtest mode returns `volume = 0` for continuous daily bars** (live mode has the real
  values). Daily-frequency research should pull history in live mode instead.
- **Historical tick data is professional-tier only** (verified: order-flow research is a
  paid wall).

## 6. Engineering highlights

- **Backtest engine** (`lib/engine.py`): bar-level matching, stop-loss monitoring, margin and
  notional caps, risk-parity sizing, slippage folded into commission, portfolio layer.
- **Gap-conservative fills**: if the next open already crosses the stop line, the fill is the
  open (worse), not the stop line. Plus a clamp guaranteeing stops sit on the adverse side.
- **Execution system**: cloud simulation account, stop-first risk control, two-layer watchdog
  (reconnect + restart), full ledger (equity every 60s, fill+30s, heartbeat 300s) and
  **cloud reconciliation on startup** (adopt existing positions, re-attach stops).
- **Self-audit tooling** (all re-runnable): 9-item code health check, stop-fill assertion,
  4-cell attribution (gap × compounding), sensitive-info double-layer scan.
- **Cost-label integrity**: the calibration tag stored in every result JSON is **generated
  from the code path**, never hand-written — because a hard-coded label silently lied about
  the run it described (see self-audit record 13).
- 48 groups of synthetic-data unit tests across three engines.

## 7. How to run

```bash
# Backtest one symbol (opens an HTML report)
策略/06_MA13超短线/回测/<品种>/回测.bat

# Engine self-audit (9 checks: stop clamp, future function, parameter alignment, ...)
python 工具/_输出/_MA13全面体检_20261009.py

# Synthetic unit tests (no network)
python 工具/_截面因子_持仓量/单元测试_截面引擎.py
python 工具/_tsmom因子/单元测试_TSMOM.py
python 工具/_出水芙蓉/单元测试_出水芙蓉.py
```

Requires Python 3.12 and `tqsdk` (free tier is enough for everything in this repo).

## 8. Honest statement

- No live-money P&L is claimed anywhere. The only real-money experience is manual futures
  trading that led to this risk system; everything measurable here is backtest or cloud simulation.
- The strategy in §3 is **not** demonstrated to be profitable out-of-sample.
- Six pre-registration cards were judged invalid and archived (§4). Those are the most transferable
  part of this repo: they show how a result is *rejected* with evidence, not tuned until it looks good.
- The cloud-simulation ledger (5 legs, live since 2026-10-08) has **41 paired round-trips** as of
  2026-10-12: win rate 29.3% (95% CI ≈ [15.3%, 43.2%]), exits 70.7% price-stop / 29.3% time-stop,
  gross P&L −18,060. It is **not** evidence of anything yet (<100 trades) — it is logged for
  execution-path validation. Its equity figures use a different account base (~1M, cloud) and are
  **not** comparable to the 200k backtests.
- Windows in use are contaminated; §5 explains why no clean window exists in this data source.

Licence: personal research project (repository is public). Code comments are in Chinese; Windows batch files require
CRLF line endings (enforced via `.gitattributes`).