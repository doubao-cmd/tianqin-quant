# tianqin-quant

**English | [中文](README.md)**

A one-person futures quant project. Started September 2026, Python + TqSdk.

The goal was never to find a profitable strategy. It was to make one path work end to end —
idea → backtest → live order — and to be able to prove the thing is not looking into the future.

> Personal research project. Not investment advice. All backtests are historical simulations.

## What I built

**A path from an idea to a real order.**
Research script → backtest → simulation → cloud execution → ledger reconciliation, no gaps in between.
Five symbols run in parallel intraday today, and orders really do go out to the cloud
(simulated matching account, no real money).

**"No look-ahead" turned into four machine-checkable layers.**
Not a promise: static scan for references to future data; truncate the tail of the dataset and see
if results move; overwrite future data with noise and see if signals follow; meta-tests for the checks
themselves. If a layer fails, that backtest does not get to produce a conclusion.

**Risk controls written from incidents, not from textbooks.**
The drawdown breaker is latched and persisted to disk — a restart does not reset it, a human has to
release it. When the main loop dies, the whole session restarts instead of reconnecting in place, so no
subscription can silently go dark. A single-instance lock means two processes can never both send orders.
Lost stop lines are restored by formula after a restart. A code snapshot is archived at every start, so
"which version was running" is answerable later. Each one maps to a real incident.

**Reconciling the ledger against actual equity.**
This is where the expensive problem surfaced: the ledger said the account was up, actual equity said it
was down — a 70k gap. Three independent bookkeeping errors: the "fill price" field held the signal price,
the account switch point was never segmented, and the roll window paired contracts from different months.
Reconciliation now trusts equity only; the ledger number is a reference.

**Every idea gets tested, and the ones that fail stay written down.**
Price signals, exit modifications, cross-market transmission, non-price factors — each one kept a record:
what the criterion was, what the result was, why changing a parameter would not save it. Nothing is
filtered by whether the result looked good, and conclusions my own data overturned stay in the text.

**A review tool for my own use.**
One HTML file, double-click to open, no environment, no server. Click a trade and the chart jumps to it —
candles, moving averages, intraday chart together.

## Where it stands

37 trading days on the simulated account. Equity went from 1,000k to 994k.

**No strategy that makes money reliably.** That line goes here, not at the bottom.

The 06 MA13 strategy still running was already shown to have no edge. It keeps running for one reason:
to verify that the engineering pipeline works, not the strategy.

## Repository map

```
lib/          backtest and live engine, drawdown breaker, ledger, code snapshots
策略/         strategies and their history (06 still running, the rest frozen or archived)
工具/         entry screener, alignment scan, non-price data, data ledger, review terminal
回测结果/      parameters and notes
实测记录/      reconciliation records
```

## Stack

Python 3.12 · TqSdk · SQLite · pandas · single-file HTML
