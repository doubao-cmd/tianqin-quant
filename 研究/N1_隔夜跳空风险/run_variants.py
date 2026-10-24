"""N1 k 变体全报表（预注册 §11：k=6，主+5 变体，全部结果必须落盘）"""
import os, json
import numpy as np
import pandas as pd
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("m", os.path.join(HERE, "run_model.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

PANEL = os.path.join(HERE, "data", "gap_panel.csv")
OUT = os.path.join(HERE, "out")
IS_END, OOS_START = "2021-12-31", "2022-01-04"


def gt(y, s, k=0.10):
    thr = np.quantile(s, 1 - k)
    f = s >= thr
    if not f.any() or y.mean() == 0:
        return float("nan")
    return float(y[f].mean() / y.mean())


def run(df, base_f, full_f, label="label"):
    sym = sorted(df["symbol"].unique())
    isd = df[df["date"] <= IS_END].reset_index(drop=True)
    oos = df[df["date"] >= OOS_START].reset_index(drop=True)
    y_is, y_oos = isd[label].to_numpy(float), oos[label].to_numpy(float)
    Xb, sc = m.build_design(isd, base_f, sym); wb = m.fit_irls(Xb, y_is)
    Xbo, _ = m.build_design(oos, base_f, sym, sc)
    Xf, sc2 = m.build_design(isd, full_f, sym); wf = m.fit_irls(Xf, y_is)
    Xfo, _ = m.build_design(oos, full_f, sym, sc2)
    pb, pf = Xbo @ wb, m.sigmoid(Xfo @ wf)
    return dict(n_is=len(isd), n_oos=len(oos),
                auc_b_is=m.auc(y_is, Xb @ wb), auc_f_is=m.auc(y_is, Xf @ wf),
                auc_b_oos=m.auc(y_oos, pb), auc_f_oos=m.auc(y_oos, pf),
                d_is=m.auc(y_is, Xf @ wf) - m.auc(y_is, Xb @ wb),
                d_oos=m.auc(y_oos, pf) - m.auc(y_oos, pb),
                lift_full=gt(y_oos, pf), lift_base=gt(y_oos, pb))


def main():
    df = pd.read_csv(PANEL, encoding="utf-8")
    df["date"] = pd.to_datetime(df["date"])
    base, full = m.BASE_F, m.FULL_F
    rows = []

    d0 = df.dropna(subset=full + ["label"]).reset_index(drop=True)
    r = run(d0, base, full); r.update(k="k0 主定义", note="|g|≥1.0，5 特征"); rows.append(r)

    d1 = d0.copy(); d1["lab15"] = (d1["g"].abs() >= 1.5).astype(int)
    r = run(d1, base, full, label="lab15"); r.update(k="k1", note="标签阈值 |g|≥1.5"); rows.append(r)

    d2 = d0.dropna(subset=["abs_ret_ratio_lag1"]).reset_index(drop=True)
    r = run(d2, base, full + ["abs_ret_ratio_lag1"]); r.update(k="k2", note="加 r_{t-2}"); rows.append(r)

    # k3 逐品种单独拟合（无哑变量），报 OOS ΔAUC 中位
    sym = sorted(d0["symbol"].unique()); ds = []
    for s in sym:
        sub = d0[d0.symbol == s].reset_index(drop=True)
        isd = sub[sub.date <= IS_END]; oos = sub[sub.date >= OOS_START]
        if len(isd) < 300 or len(oos) < 100 or oos["label"].nunique() < 2:
            continue
        def z(frame, fit_df=None):
            X = np.array(frame[full].to_numpy(float), dtype=float, copy=True)
            mu = np.array((fit_df[full].mean() if fit_df is not None else frame[full].mean()).to_numpy(float), copy=True)
            sd = np.array((fit_df[full].std(ddof=0) if fit_df is not None else frame[full].std(ddof=0)).to_numpy(float), copy=True)
            sd[sd == 0] = 1
            return (X - mu) / sd
        Xi, Xo = z(isd), z(oos, isd)
        Xib = np.column_stack([np.ones(len(isd)), Xi[:, :2]])
        Xib_o = np.column_stack([np.ones(len(oos)), Xo[:, :2]])
        Xif = np.column_stack([np.ones(len(isd)), Xi])
        Xif_o = np.column_stack([np.ones(len(oos)), Xo])
        yi, yo = isd["label"].to_numpy(float), oos["label"].to_numpy(float)
        wb = m.fit_irls(Xib, yi); wf = m.fit_irls(Xif, yi)
        ds.append(m.auc(yo, Xif_o @ wf) - m.auc(yo, Xib_o @ wb))
    rows.append(dict(k="k3", note=f"逐品种拟合({len(ds)} 品种)，报中位ΔAUC", d_oos=float(np.median(ds)),
                     d_is=np.nan, n_oos=int(len(d0[d0.date >= OOS_START])), lift_full=np.nan, lift_base=np.nan))

    d4 = d0[d0.symbol != "LH"].reset_index(drop=True)
    r = run(d4, base, full); r.update(k="k4", note="剔 LH"); rows.append(r)

    d5 = d0.copy(); b5 = ["sd20_norm", "ewma97"]; f5 = b5 + full[2:]
    r = run(d5, b5, f5); r.update(k="k5", note="EWMA λ=0.97"); rows.append(r)

    out = pd.DataFrame(rows)[["k", "note", "n_is", "n_oos", "auc_b_is", "auc_f_is", "d_is",
                              "auc_b_oos", "auc_f_oos", "d_oos", "lift_base", "lift_full"]]
    out.to_csv(os.path.join(OUT, "k_variants.csv"), index=False, encoding="utf-8")
    print(out.round(4).to_string(index=False))
    print("\nk =", len(rows), "（主 + 5 变体）；看结果后新增 = 0")


if __name__ == "__main__":
    main()
