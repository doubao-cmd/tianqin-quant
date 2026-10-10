# tianqin-quant

**English | [中文](README.md) | [中文简历](求职/简历.md)**

A personal futures-quant research and execution system, built end-to-end in Python:
research methodology → backtest engine → live-simulation execution → self-audit tooling.

The differentiating part of this repo is **not** the amount of code. It is the
**process discipline**, made auditable: every strategy is *pre-registered before it is run*,
every result carries hard-check verdicts, and **three strategies have been publicly falsified and archived**.

---

## 1. Why this repo exists

Most quant repos show winning backtests. This one shows the opposite, on purpose:

| | |
|---|---|
| **Pre-registration** | Signal definition, parameters, sample windows, pass criteria, hard checks and a change log are **written down and frozen before the first run**. After a run, parameters do not change. |
| **Hard checks** | A result is void unless: stop-loss exits never show a win; per-reason trade counts reconcile; initial capital equals the sum of per-trade P&L; every stop fill sits on the adverse side of entry. |
| **Honest archiving** | Strategies that fail are archived **with the failure analysis**, not deleted. §4 lists all three. |
| **Reproducible verdicts** | 17 self-audit records document bugs that were found and fixed — including bugs that produced *plausible but wrong* numbers without raising any error. |

## 2. Repository map

```
策略/                     strategies (01–07); active: 06_MA13, 07_Bollinger
  06_MA13超短线/
    回测/                  per-symbol self-contained backtesters + 回测.bat
    模拟盘/                cloud simulation execution + 台账/ (ledgers)
lib/                      shared engine (lib/engine.py) + portfolio layer
工具/                     tooling: health checks, attribution, calibration, pre-registration cards
  _截面因子_持仓量/         cross-sectional open-interest factor (archived: falsified)
  _tsmom因子/              time-series momentum (archived: falsified)
  _出水芙蓉/                "lotus breakout" pattern (archived: untestable)
回测结果/                  backtest artefacts (HTML detached; parameter JSON kept as evidence)
```

## 3. Results and what they do **not** mean

**Backtest, development window 2021-01 ~ 2023-12, 12 symbols, fixed 200k sizing, gap-conservative fills.**

| Symbol | Return | Sharpe | Max DD | Trades | Win rate |
|---|---:|---:|---:|---:|---:|
| Benzene (EB) | +655.7% | 5.336 | 6.9% | 2730 | 30.1% |
| Coking coal (JM) | +899.9% | 5.291 | 4.7% | 2801 | 31.9% |
| Palm oil (P) | +740.8% | 4.825 | 7.1% | 2717 | 31.7% |
| PVC (V) | +384.4% | 4.165 | 5.7% | 2667 | 31.5% |
| Soybean meal (M) | +250.3% | 3.920 | 4.1% | 2671 | 31.6% |
| Rebar (RB) | +347.4% | 3.855 | 9.7% | 2681 | 32.0% |
| Live hog (LH) | +413.3% | 3.730 | 5.2% | 1805 | 33.0% |
| Soda ash (SA) | +518.4% | 3.718 | 10.3% | 2766 | 33.0% |
| Sugar (SR) | +144.0% | 2.779 | 5.3% | 2709 | 30.7% |
| Methanol (MA) | +184.4% | 2.696 | 12.3% | 2733 | 31.2% |
| Rubber (RU) | +117.3% | 1.913 | 13.0% | 2790 | 30.9% |
| Egg (JD) | +129.8% | 1.773 | 7.7% | 1873 | 33.0% |
| **Equal-weight** | **+398.8%** | **3.667** | **7.7%** | **30,943** | **31.6%** |

**Read this table with care — it is a development window, not tradable evidence:**

1. A **~32% win rate** means the edge lives entirely in the payoff ratio.
2. A **+399% return against a 7.7% drawdown** points at position sizing, not alpha.
3. **This window is contaminated**: it was inspected while developing the strategy.
4. On a *different* window (2025-03 ~ 2026-03) the same configuration degrades to
   **equal-weight Sharpe 1.59** — roughly half. Decay is expected; the number to trust is
   the decay, not the peak.

## 4. Strategies that were falsified (kept on purpose)

| Strategy | Verdict | Evidence |
|---|---|---|
| Cross-sectional open interest ("volume up, price stalls") | **Invalid** | −141.1% in-sample; adding an ATR stop made it −112.2%. Two independent signals: still deeply negative *after* stops ⇒ wrong direction, and win rate *fell* (47%→34%) when stops truncated trades ⇒ the stopped-out trades mostly came back. Economically: in a 2016-2018 uptrend, "volume up + price stalls" is usually a **continuation**, not a top. |
| TSMOM (12-month time-series momentum) | **Invalid** | −74.5%, Sharpe −0.298, drawdown −80% — textbook momentum-crash regime (post-2016 supply-side rally). Three implementation bugs were fixed first: nanosecond dates sliced as strings (477 rebalances instead of 23), weights normalised by pool size instead of Σ(1/σ) (~10× hidden leverage), and a direction check comparing final state against historical signals. |
| "Lotus breakout" (volume spike piercing an MA cluster) | **Untestable** | With the frozen parameter, **0 signals** in 5 years × 9 symbols. Signal frequency: ≥3× avg volume → 0; ≥2× → 19; ≥1.5× → 57; no volume filter → 110. Daily commodity bars simply do not produce "multi-fold volume spikes". Loosening the threshold to exactly the pass mark was **refused** — that would be gate-driven cherry-picking. |

## 5. Data-source limits (verified empirically, not assumed)

This matters more than any single backtest number:

- **Free-tier continuous-contract daily history starts at 2016-01-04.** `data_length=3000`
  (≈12 years) still returns nothing earlier, for all 9 symbols.
  ⇒ The retrievable window **coincides with** the already-inspected window, so
  "find a clean out-of-sample period" is **impossible with this data source** — it is a
  data limitation, not a discipline problem.
- **No historical klines for individual (non-continuous) contracts** — 4/4 probes returned
  empty, including the *current* front month. ⇒ Cross-commodity **roll yield / carry**
  cannot be computed with this feed.
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
- Three strategies were falsified and archived (§4). Those are the most transferable part
  of this repo: they show how a result is *rejected* with evidence, not tuned until it looks good.
- Windows in use are contaminated; §5 explains why no clean window exists in this data source.

Licence: private research project. Code comments are in Chinese; Windows batch files require
CRLF line endings (enforced via `.gitattributes`).