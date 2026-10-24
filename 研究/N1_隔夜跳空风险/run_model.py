"""N1 模型与判据（预注册 v1.1 §8~§9）

- 品种内 z-score（scaler 只用 IS 统计）→ 池化
- numpy 手写 IRLS 逻辑回归（L2，λ=1.0），品种哑变量
- 基线 = sd20_norm + ewma_sigma20；全模型 = +abs_ret_ratio +doi +vol_ratio
- L1：OOS ΔAUC ≥ 0.02 且 按交易日块 bootstrap 95% CI 下界 > 0 且 top-10% lift ≥ 1.25
- L2 仅在 L1 通过后执行
"""
import os, sys, json, math, datetime
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
PANEL = os.path.join(HERE, "data", "gap_panel.csv")
OUT = os.path.join(HERE, "out")
os.makedirs(OUT, exist_ok=True)

BASE_F = ["sd20_norm", "ewma_sigma20"]
FULL_F = BASE_F + ["abs_ret_ratio", "doi", "vol_ratio"]
IS_END = "2021-12-31"
FLAG = None
OOS_START = "2022-01-04"
SEED = 20261023


def sigmoid(x):
    x = np.clip(x, -30, 30)
    return 1.0 / (1.0 + np.exp(-x))


def fit_irls(X, y, lam=1.0, iters=60, tol=1e-8):
    n, p = X.shape
    w = np.zeros(p)
    for _ in range(iters):
        eta = X @ w
        pr = sigmoid(eta)
        Wd = np.clip(pr * (1 - pr), 1e-6, None)
        z = eta + (y - pr) / Wd
        XtW = X.T * Wd
        A = XtW @ X + lam * np.eye(p)
        A[0, 0] -= lam                      # 截距不惩罚
        b = XtW @ z
        w_new = np.linalg.solve(A, b)
        if np.max(np.abs(w_new - w)) < tol:
            w = w_new
            break
        w = w_new
    return w


def auc(y, s):
    y = np.asarray(y); s = np.asarray(s)
    pos = s[y == 1]; neg = s[y == 0]
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    # Mann-Whitney with ties
    allv = np.concatenate([pos, neg])
    order = np.argsort(allv, kind="mergesort")
    ranks = np.empty(len(allv), float)
    ranks[order] = np.arange(1, len(allv) + 1)
    # average ranks for ties
    srt = allv[order]
    i = 0
    while i < len(srt):
        j = i
        while j + 1 < len(srt) and srt[j + 1] == srt[i]:
            j += 1
        if j > i:
            avg = (i + j + 2) / 2.0
            ranks[order[i:j + 1]] = avg
        i = j + 1
    rp = ranks[:len(pos)].sum()
    return float((rp - len(pos) * (len(pos) + 1) / 2.0) / (len(pos) * len(neg)))


def build_design(df, feats, sym_levels, scaler=None):
    X = np.array(df[feats].to_numpy(float), dtype=float, copy=True)
    if scaler is None:
        scaler = {}
        for j, f in enumerate(feats):
            mu = X[:, j].mean(); sd = X[:, j].std(ddof=0)
            scaler[f] = (float(mu), float(sd) if sd > 0 else 1.0)
    for j, f in enumerate(feats):
        mu, sd = scaler[f]
        X[:, j] = (X[:, j] - mu) / sd
    dums = np.zeros((len(df), len(sym_levels) - 1))
    sv = df["symbol"].to_numpy()
    for k, s in enumerate(sym_levels[1:], start=0):
        dums[:, k] = (sv == s).astype(float)
    D = np.column_stack([np.ones(len(df)), X, dums])
    return D, scaler


def main():
    global FLAG
    FLAG = os.path.join(OUT, "OOS_DONE.flag")
    IS_ONLY = "--is-only" in sys.argv
    if os.path.exists(FLAG) and not IS_ONLY:
        raise SystemExit("OOS_DONE.flag 已存在：预注册 §10 禁止重跑 OOS")
    df = pd.read_csv(PANEL, encoding="utf-8")
    df = df.dropna(subset=FULL_F + ["label"]).reset_index(drop=True)
    df["date"] = pd.to_datetime(df["date"])
    sym_levels = sorted(df["symbol"].unique())

    is_df = df[df["date"] <= IS_END].reset_index(drop=True)
    oos_df = df[df["date"] >= OOS_START].reset_index(drop=True)
    if IS_ONLY:
        oos_df = oos_df.iloc[0:0].copy()
    res = {"n_all": int(len(df)), "n_is": int(len(is_df)), "n_oos": int(len(oos_df)),
           "symbols": sym_levels, "is_range": [str(is_df.date.min().date()), str(is_df.date.max().date())],
           "oos_range": [str(oos_df.date.min().date()), str(oos_df.date.max().date())],
           "label_rate_is": float(is_df.label.mean()), "label_rate_oos": float(oos_df.label.mean()),
           "features": FULL_F, "base_features": BASE_F}

    y_is = is_df["label"].to_numpy(float); y_oos = oos_df["label"].to_numpy(float)

    Xb_is, sc_b = build_design(is_df, BASE_F, sym_levels)
    Xb_oos, _ = build_design(oos_df, BASE_F, sym_levels, sc_b)
    wb = fit_irls(Xb_is, y_is)
    auc_b_is, auc_b_oos = auc(y_is, Xb_is @ wb), auc(y_oos, Xb_oos @ wb)

    Xf_is, sc_f = build_design(is_df, FULL_F, sym_levels)
    Xf_oos, _ = build_design(oos_df, FULL_F, sym_levels, sc_f)
    wf = fit_irls(Xf_is, y_is)
    pr_f_oos = sigmoid(Xf_oos @ wf)
    auc_f_is, auc_f_oos = auc(y_is, Xf_is @ wf), auc(y_oos, pr_f_oos)

    res["auc_base"] = {"is": auc_b_is, "oos": auc_b_oos}
    res["auc_full"] = {"is": auc_f_is, "oos": auc_f_oos}
    d_is = auc_f_is - auc_b_is
    d_oos = auc_f_oos - auc_b_oos
    res["delta_auc"] = {"is": d_is, "oos": d_oos}

    # ---- 按交易日块 bootstrap（OOS）----
    rng = np.random.default_rng(SEED)
    oos_dates = oos_df["date"].to_numpy()
    uniq = np.unique(oos_dates)
    idx_by_date = {d: np.where(oos_dates == d)[0] for d in uniq}
    boots = []
    for _ in range(1000):
        pick = rng.choice(uniq, size=len(uniq), replace=True)
        idx = np.concatenate([idx_by_date[d] for d in pick])
        a_b = auc(y_oos[idx], (Xb_oos @ wb)[idx])
        a_f = auc(y_oos[idx], pr_f_oos[idx])
        boots.append(a_f - a_b)
    boots = np.array(boots)
    ci_lo, ci_hi = np.percentile(boots, [2.5, 97.5])
    res["bootstrap"] = {"n": 1000, "mean": float(boots.mean()), "ci95": [float(ci_lo), float(ci_hi)],
                        "p_le0": float((boots <= 0).mean())}

    # ---- top-10% lift ----
    thr = np.quantile(pr_f_oos, 0.90)
    flag = pr_f_oos >= thr
    base_rate = float(y_oos.mean())
    prec = float(y_oos[flag].mean()) if flag.any() else float("nan")
    res["lift"] = {"threshold_p90": float(thr), "flagged_share": float(flag.mean()),
                   "precision_flagged": prec, "base_rate": base_rate,
                   "lift": float(prec / base_rate) if base_rate > 0 else float("nan")}

    # ---- 逐品种 OOS AUC（参考，不作判据）----
    by_sym = {}
    for s in sym_levels:
        m = oos_df["symbol"].to_numpy() == s
        if m.sum() > 30 and len(np.unique(y_oos[m])) == 2:
            by_sym[s] = {"auc_base": auc(y_oos[m], (Xb_oos @ wb)[m]),
                         "auc_full": auc(y_oos[m], pr_f_oos[m]), "n": int(m.sum())}
    res["by_symbol_oos"] = by_sym

    # ---- L1 判定 ----
    l1 = (d_oos >= 0.02) and (ci_lo > 0) and (res["lift"]["lift"] >= 1.25)
    res["L1"] = {"delta_auc_ge_0.02": bool(d_oos >= 0.02), "ci_lo_gt_0": bool(ci_lo > 0),
                 "lift_ge_1.25": bool(res["lift"]["lift"] >= 1.25), "pass": bool(l1)}

    # ---- 系数（全模型，标准化尺度）----
    res["coef_full_std"] = {name: float(w) for name, w in
                            zip(["intercept"] + FULL_F + [f"dum_{s}" for s in sym_levels[1:]], wf)}
    res["coef_base_std"] = {name: float(w) for name, w in
                            zip(["intercept"] + BASE_F + [f"dum_{s}" for s in sym_levels[1:]], wb)}

    # ---- L2（仅 L1 通过后）----
    if l1:
        res["L2"] = run_l2(oos_df, pr_f_oos)
    else:
        res["L2"] = {"skipped": True, "reason": "L1 未通过（预注册 §9：不许建 L2）"}
        if os.path.isdir(os.path.join(OUT, "L2")):
            res["L2"]["warn"] = "out/L2 目录已存在，与预注册冲突"

    with open(os.path.join(OUT, "results.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1, default=float)
    if not IS_ONLY:
        open(FLAG, "w", encoding="utf-8").write("OOS consumed at " + datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "\n")

    print(json.dumps({k: res[k] for k in ["n_is", "n_oos", "auc_base", "auc_full", "delta_auc",
                                          "bootstrap", "lift", "L1"]}, ensure_ascii=False, indent=1, default=float))
    print("coef_full_std:", {k: round(v, 4) for k, v in res["coef_full_std"].items()})


def run_l2(oos_df, pr):
    """预注册 §9 L2：9 品种各 1 手；映射 ×1/0.6/0.3；敞口匹配后比尾部暴露。"""
    mult = {"RU": 10, "EB": 5, "JM": 60, "P": 10, "M": 10, "V": 5, "LH": 16, "JD": 10, "SR": 10}
    d = oos_df.copy()
    d["notional"] = d["close_prev"] * d["symbol"].map(mult)
    d["pr"] = pr
    q = d.groupby("date")["pr"].rank(pct=True)
    d["mult"] = np.where(q < 0.33, 1.0, np.where(q < 0.90, 0.6, 0.3))
    d["tail"] = np.maximum(0.0, d["g"].abs() - 1.0)
    d["expo_base"] = d["notional"]
    d["expo_strat"] = d["notional"] * d["mult"]
    d["tail_base"] = d["expo_base"] * d["tail"]
    d["tail_strat"] = d["expo_strat"] * d["tail"]
    g = d.groupby("date")[["expo_base", "expo_strat", "tail_base", "tail_strat"]].sum()
    k = g["expo_base"].mean() / g["expo_strat"].mean()
    g["expo_strat_m"] = g["expo_strat"] * k
    g["tail_strat_m"] = g["tail_strat"] * k
    drop = 1 - g["tail_strat_m"].sum() / g["tail_base"].sum()
    expo_diff = g["expo_strat_m"].mean() / g["expo_base"].mean() - 1
    ok = (drop >= 0.20) and (abs(expo_diff) <= 0.02)
    return {"k": float(k), "tail_base_sum": float(g["tail_base"].sum()),
            "tail_strat_m_sum": float(g["tail_strat_m"].sum()),
            "tail_drop_pct": float(drop), "expo_diff_pct": float(expo_diff),
            "max_day_tail_base": float(g["tail_base"].max()),
            "max_day_tail_strat_m": float(g["tail_strat_m"].max()),
            "pass": bool(ok)}


if __name__ == "__main__":
    main()
