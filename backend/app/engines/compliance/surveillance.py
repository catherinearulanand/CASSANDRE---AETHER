"""Statistical surveillance on SYNTHETIC events. Scores RANK investigation cases; they are never findings.

Anti-leakage design:
  * the generator returns (raw_features, injected_labels) as SEPARATE objects; feature code never receives labels;
  * normalisation parameters (median/MAD per account x time regime) are fitted on the TRAIN interval only;
  * the decision threshold is chosen on VALIDATION only; metrics are reported on the later HOLDOUT only;
  * split is chronological (train < validation < holdout by day).
Synthetic labels are not proof of real-world detection performance. Floats are used for analytics only
(spec section 4); nothing here produces an authoritative financial amount.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.metrics import average_precision_score

from app.core.canonical import content_hash

FEATURES = [
    ("exposure_change", "Signed change in net exposure vs previous day (lots-equivalent)"),
    ("gross_exposure", "Gross exposure (lots-equivalent)"),
    ("turnover", "Traded quantity per day"),
    ("concentration", "Herfindahl concentration of positions across instruments (0..1)"),
    ("cancel_ratio", "Cancelled / submitted orders (only because synthetic order events exist)"),
    ("price_volume_dev", "Price*volume deviation vs the synthetic baseline (dimensionless)"),
    ("margin_utilization", "Margin used / margin available (0..1+)"),
    ("missing_ratio", "Fraction of expected fields missing in the day's records"),
]
FNAMES = [f for f, _ in FEATURES]
EXPIRY_CYCLE, NEAR_EXPIRY_DAYS = 20, 3
BASE_DATE = datetime(2026, 1, 1, tzinfo=UTC)
MODEL_NAME = "robust-mad-ranker"


@dataclass
class Dataset:
    accounts: list[str]
    day: np.ndarray            # (N,)
    account_idx: np.ndarray    # (N,)
    regime: np.ndarray         # (N,) 0 normal, 1 near-expiry
    X: np.ndarray              # (N, F) RAW features - no labels in here
    labels: np.ndarray         # (N,) injected truth, kept OUT of features
    episode: np.ndarray        # (N,) episode id or -1
    n_days: int


def generate_dataset(seed: int = 42, n_accounts: int = 20, n_days: int = 240, anomaly_rate: float = 0.025) -> Dataset:
    rng = np.random.default_rng(seed)
    acc = [f"ACC-SYN-{i:03d}" for i in range(n_accounts)]
    rows_x, rows_day, rows_acc, rows_reg = [], [], [], []
    for a in range(n_accounts):
        g0 = rng.lognormal(5.0, 0.4)
        base = np.array([0.0, g0, g0 * rng.uniform(0.15, 0.3), rng.uniform(0.25, 0.5), rng.uniform(0.05, 0.2),
                         0.0, rng.uniform(0.3, 0.6), 0.01])
        sd = np.array([g0 * 0.03, g0 * 0.05, g0 * 0.04, 0.03, 0.03, 0.5, 0.05, 0.005])
        gross = g0
        for d in range(n_days):
            near = 1 if (d % EXPIRY_CYCLE) >= EXPIRY_CYCLE - NEAR_EXPIRY_DAYS else 0
            x = base + sd * rng.standard_normal(8)
            if near:
                x[2] *= 1.5  # expiry-week turnover regime (normalised away per regime)
                x[1] += g0 * 0.05
            gross = max(1.0, 0.9 * gross + 0.1 * x[1])
            x[1] = gross
            x[3] = float(np.clip(x[3], 0.01, 1.0)); x[4] = float(np.clip(x[4], 0, 1))
            x[6] = float(np.clip(x[6], 0, 1.5)); x[7] = float(np.clip(x[7], 0, 1))
            rows_x.append(x); rows_day.append(d); rows_acc.append(a); rows_reg.append(near)
    X = np.array(rows_x); day = np.array(rows_day); ai = np.array(rows_acc); reg = np.array(rows_reg)
    n = len(X)
    labels = np.zeros(n, dtype=int); episode = -np.ones(n, dtype=int)
    # inject labelled episodes (1-3 days) across the whole timeline; each touches 2-3 features by +6..+12 robust sd
    ep = 0
    n_ep = int(n * anomaly_rate / 2)
    starts = rng.choice(n, size=n_ep, replace=False)
    sds = X.std(axis=0)
    kinds = [[2, 4], [0, 3, 1], [6], [7, 5]]
    for s in starts:
        length = int(rng.integers(1, 4))
        k = kinds[int(rng.integers(0, len(kinds)))]
        for off in range(length):
            i = s + off
            if i >= n or ai[i] != ai[s] or labels[i]:
                break
            for j in k:
                X[i, j] += float(rng.uniform(6, 12)) * (sds[j] * 0.35) * (1 if j != 0 or rng.random() < 0.5 else -1)
            X[i, 3] = min(X[i, 3], 1.0); X[i, 4] = min(X[i, 4], 1.0); X[i, 7] = min(X[i, 7], 1.0)
            labels[i], episode[i] = 1, ep
        ep += 1
    return Dataset(acc, day, ai, reg, X, labels, episode, n_days)


def split_days(n_days: int) -> dict:
    a, b = int(n_days * 0.6), int(n_days * 0.8)
    return {"train": [0, a], "validation": [a, b], "holdout": [b, n_days], "scheme": "chronological by day, 60/20/20"}


def _mask(ds: Dataset, part: str, sp: dict):
    lo, hi = sp[part]
    return (ds.day >= lo) & (ds.day < hi)


def fit_robust(X: np.ndarray, account_idx: np.ndarray, regime: np.ndarray, train: np.ndarray) -> dict:
    """median/MAD per (account, regime) from TRAIN rows only. Falls back to the account median if a regime is thin."""
    params: dict = {}
    for a in np.unique(account_idx):
        for r in (0, 1):
            m = train & (account_idx == a) & (regime == r)
            if m.sum() < 8:
                m = train & (account_idx == a)
            med = np.median(X[m], axis=0)
            mad = np.median(np.abs(X[m] - med), axis=0) * 1.4826
            params[(int(a), r)] = (med, np.maximum(mad, 1e-6 * (np.abs(med) + 1.0)))
    return params


def robust_z(X, account_idx, regime, params) -> np.ndarray:
    Z = np.empty_like(X)
    for i in range(len(X)):
        med, mad = params[(int(account_idx[i]), int(regime[i]))]
        Z[i] = (X[i] - med) / mad
    return Z


def pr_metrics(y: np.ndarray, flagged: np.ndarray, score: np.ndarray, episode: np.ndarray, day: np.ndarray, acct: np.ndarray) -> dict:
    tp = int(((y == 1) & flagged).sum()); fp = int(((y == 0) & flagged).sum())
    fn = int(((y == 1) & ~flagged).sum()); tn = int(((y == 0) & ~flagged).sum())
    n = len(y)
    prec = tp / (tp + fp) if tp + fp else None
    rec = tp / (tp + fn) if tp + fn else None
    eps = sorted(set(episode[y == 1].tolist()))
    lags, detected = [], 0
    for e in eps:
        m = episode == e
        if (flagged & m).any():
            detected += 1
            lags.append(int(day[flagged & m].min() - day[m].min()))
    return {
        "sample_size": n, "positives": int(y.sum()), "prevalence": float(y.mean()) if n else None,
        "confusion_matrix": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "precision": prec, "recall": rec,
        "f1": (2 * prec * rec / (prec + rec)) if prec and rec else (0.0 if prec is not None and rec is not None else None),
        "pr_auc": float(average_precision_score(y, score)) if y.sum() > 0 else None,
        "false_alerts_per_1000_obs": fp / n * 1000 if n else None,
        "episodes": len(eps), "episodes_detected": detected,
        "mean_detection_lag_days": float(np.mean(lags)) if lags else None,
    }


def choose_threshold(y: np.ndarray, score: np.ndarray) -> float:
    """Maximise F1 on VALIDATION only (never on holdout)."""
    cands = np.unique(np.quantile(score, np.linspace(0.80, 0.999, 120)))
    best, bt = -1.0, float(cands[-1])
    for t in cands:
        f = score >= t
        tp = int(((y == 1) & f).sum()); fp = int(((y == 0) & f).sum()); fn = int(((y == 1) & ~f).sum())
        p = tp / (tp + fp) if tp + fp else 0.0; r = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * p * r / (p + r) if p + r else 0.0
        if f1 > best:
            best, bt = f1, float(t)
    return bt


def top_features(z_row: np.ndarray, x_row: np.ndarray, med: np.ndarray, k: int = 3) -> list[dict]:
    idx = np.argsort(-np.abs(z_row))[:k]
    return [{"feature": FNAMES[j], "value": round(float(x_row[j]), 4), "baseline_median": round(float(med[j]), 4),
             "robust_z": round(float(z_row[j]), 2)} for j in idx]


def run_experiment(seed: int = 42, n_accounts: int = 20, n_days: int = 240) -> dict:
    ds = generate_dataset(seed, n_accounts, n_days)
    sp = split_days(n_days)
    tr, va, ho = (_mask(ds, p, sp) for p in ("train", "validation", "holdout"))
    assert ds.day[tr].max() < ds.day[va].min() and ds.day[va].max() < ds.day[ho].min()  # chronological
    params = fit_robust(ds.X, ds.account_idx, ds.regime, tr)  # features only; no labels passed
    Z = robust_z(ds.X, ds.account_idx, ds.regime, params)
    s_mad = np.abs(Z).max(axis=1)
    th = choose_threshold(ds.labels[va], s_mad[va])
    iso = IsolationForest(n_estimators=200, random_state=seed, contamination="auto").fit(np.abs(Z[tr]))
    s_iso = -iso.score_samples(np.abs(Z))
    th_iso = choose_threshold(ds.labels[va], s_iso[va])

    def m(score, t, mask):
        return pr_metrics(ds.labels[mask], score[mask] >= t, score[mask], ds.episode[mask], ds.day[mask], ds.account_idx[mask])
    primary = {"validation": m(s_mad, th, va), "holdout": m(s_mad, th, ho)}
    challenger = {"validation": m(s_iso, th_iso, va), "holdout": m(s_iso, th_iso, ho)}
    sens = []
    for mult in (0.5, 0.75, 1.0, 1.25, 1.5, 2.0):
        mm = m(s_mad, th * mult, ho)
        sens.append({"threshold": round(th * mult, 3), "multiple_of_selected": mult, "precision": mm["precision"], "recall": mm["recall"],
                     "false_alerts_per_1000_obs": mm["false_alerts_per_1000_obs"]})
    train_hash = content_hash({"rows": [[int(ds.account_idx[i]), int(ds.day[i])] + [format(float(v), ".6f") for v in ds.X[i]]
                                         for i in np.where(tr)[0]]})
    drift = float(np.median(s_mad[ho]) / max(np.median(s_mad[tr]), 1e-9))
    return {"seed": seed, "dataset": ds, "split": sp, "params": params, "Z": Z, "score": s_mad, "threshold": th, "primary": primary,
            "challenger": {"name": "IsolationForest(n_estimators=200, random_state=seed) on |robust z|", "threshold": th_iso, **challenger},
            "sensitivity": sens, "training_data_hash": train_hash, "drift_ratio": drift, "masks": {"validation": va, "holdout": ho, "train": tr}}


def model_card(res: dict) -> dict:
    p = res["primary"]["holdout"]
    return {
        "name": MODEL_NAME, "intended_use": "Rank synthetic account-days for HUMAN investigation. Not a misconduct detector.",
        "not_for": ["misconduct findings", "manipulation verdicts", "trade blocking", "automatic regulatory reporting", "real-world accuracy claims"],
        "method": "Per (account x expiry-regime) median/MAD z-scores fitted on the train interval; score = max |z| over features; "
                  "threshold chosen on validation to maximise F1; reported on a later holdout.",
        "features": [{"name": n, "definition": d} for n, d in FEATURES],
        "regime_note": "Expiry proximity is used as a normalisation regime (last 3 days of a 20-day synthetic cycle), not as a scored feature.",
        "data": {"kind": "SYNTHETIC", "seed": res["seed"], "accounts": len(res["dataset"].accounts), "days": res["dataset"].n_days,
                 "training_data_hash": res["training_data_hash"], "split": res["split"]},
        "evaluation": {"holdout": p, "validation": res["primary"]["validation"], "challenger": res["challenger"]["holdout"]},
        "limitations": [
            "Labels are INJECTED synthetic anomalies; performance here says nothing about real markets.",
            "Prevalence is set by the generator, so precision depends on it; compare false alerts per 1,000 observations.",
            "Small sample: confidence intervals are wide; do not over-read differences between models.",
            "Explanations are transparent feature deviations (robust z), not causal claims.",
            "Drift monitor is a simple score-median ratio, not a validated drift test."],
        "governance": "Model can be DISABLED; disabled models create no cases. No score affects any deterministic control status.",
    }


def obs_date(day: int) -> datetime:
    return BASE_DATE + timedelta(days=int(day))
