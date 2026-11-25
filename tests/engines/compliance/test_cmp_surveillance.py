import inspect

import numpy as np
import pytest
from sqlalchemy import func, select

from app.engines.compliance import surv_service as SV
from app.engines.compliance import surveillance as S
from app.engines.compliance.models import (
    CmpCase,
    CmpEvaluation,
    CmpModelVersion,
    CmpScore,
)

pytestmark = pytest.mark.gold


@pytest.fixture(scope="module")
def exp():
    return S.run_experiment(seed=42)


def test_S_A08_split_is_chronological_and_labels_never_reach_features(exp):
    ds, m = exp["dataset"], exp["masks"]
    assert ds.day[m["train"]].max() < ds.day[m["validation"]].min() <= ds.day[m["validation"]].max() < ds.day[m["holdout"]].min()
    # feature/normalisation functions cannot even receive labels
    for fn in (S.fit_robust, S.robust_z):
        assert not any("label" in p or p == "y" for p in inspect.signature(fn).parameters)
    # normalisation parameters depend ONLY on train rows: corrupting validation/holdout features changes nothing
    ds2 = S.generate_dataset(42)
    X2 = ds2.X.copy()
    later = ~m["train"]
    X2[later] += 1e6
    p1 = S.fit_robust(ds2.X, ds2.account_idx, ds2.regime, m["train"])
    p2 = S.fit_robust(X2, ds2.account_idx, ds2.regime, m["train"])
    assert all(np.array_equal(p1[k][0], p2[k][0]) and np.array_equal(p1[k][1], p2[k][1]) for k in p1)


def test_scores_do_not_depend_on_labels(exp):
    ds = S.generate_dataset(42)
    ds.labels[:] = np.random.default_rng(0).integers(0, 2, len(ds.labels))   # scramble the truth
    sp = S.split_days(ds.n_days)
    tr = (ds.day >= sp["train"][0]) & (ds.day < sp["train"][1])
    Z = S.robust_z(ds.X, ds.account_idx, ds.regime, S.fit_robust(ds.X, ds.account_idx, ds.regime, tr))
    assert np.array_equal(np.abs(Z).max(axis=1), exp["score"])


def test_threshold_chosen_on_validation_only(exp):
    ds, m = exp["dataset"], exp["masks"]
    assert exp["threshold"] == S.choose_threshold(ds.labels[m["validation"]], exp["score"][m["validation"]])


def test_metrics_reported_with_confusion_matrix_prevalence_and_sample_size(exp):
    h = exp["primary"]["holdout"]
    for k in ("precision", "recall", "pr_auc", "false_alerts_per_1000_obs", "confusion_matrix", "prevalence", "sample_size", "positives",
              "mean_detection_lag_days"):
        assert k in h
    cm = h["confusion_matrix"]
    assert cm["tp"] + cm["fp"] + cm["fn"] + cm["tn"] == h["sample_size"] and cm["tp"] + cm["fn"] == h["positives"]
    assert h["positives"] > 0 and 0 < h["prevalence"] < 0.2
    # sanity: ranking beats chance on synthetic injected anomalies (PR-AUC > prevalence). Not a real-world claim.
    assert h["pr_auc"] > h["prevalence"]
    assert exp["challenger"]["holdout"]["pr_auc"] is not None           # IsolationForest challenger evaluated on same holdout


def test_deterministic_for_fixed_seed():
    a, b = S.run_experiment(42), S.run_experiment(42)
    assert a["training_data_hash"] == b["training_data_hash"] and a["primary"]["holdout"] == b["primary"]["holdout"]
    assert S.run_experiment(7)["training_data_hash"] != a["training_data_hash"]


def test_model_card_contains_required_sections(exp):
    card = S.model_card(exp)
    assert {"intended_use", "not_for", "method", "features", "data", "evaluation", "limitations", "governance"} <= set(card)
    assert "misconduct" in " ".join(card["not_for"]) and card["data"]["kind"] == "SYNTHETIC" and len(card["data"]["training_data_hash"]) == 64
    assert any("INJECTED" in x for x in card["limitations"])


def test_S_A09_anomaly_is_investigation_case_not_a_finding_and_controls_untouched(db, tenant):
    before = db.execute(select(func.count()).select_from(CmpEvaluation).where(CmpEvaluation.tenant_id == tenant)).scalar_one()
    out = SV.train_and_persist(db, tenant, {"id": "u-co", "type": "USER", "role": "compliance_officer"})
    db.commit()
    assert out["cases_opened"] > 0 and "not a finding of misconduct" in out["disclaimer"]
    cases = db.execute(select(CmpCase).where(CmpCase.tenant_id == tenant)).scalars().all()
    assert cases and all(c.kind == "ANOMALY_INVESTIGATION" and c.last_status == "ANOMALY_SIGNAL" and "not a finding" in c.title for c in cases)
    assert all("misconduct" not in c.title.lower() and "manipulat" not in c.title.lower() for c in cases)
    after = db.execute(select(func.count()).select_from(CmpEvaluation).where(CmpEvaluation.tenant_id == tenant)).scalar_one()
    assert before == after == 0                                           # G22: raw control status/evaluations unaffected
    assert db.execute(select(func.count()).select_from(CmpScore).where(CmpScore.tenant_id == tenant)).scalar_one() > 0
    # no label column is persisted with scores
    assert not any("label" in c.name for c in CmpScore.__table__.columns)
    m = db.execute(select(CmpModelVersion).where(CmpModelVersion.tenant_id == tenant)).scalar_one()
    assert m.model_card["limitations"] and len(m.training_data_hash) == 64 and m.split["scheme"].startswith("chronological")


def test_flagged_scores_explain_with_transparent_feature_deviations(db, tenant):
    SV.train_and_persist(db, tenant, {"id": "u-co", "type": "USER", "role": "x"})
    db.commit()
    s = db.execute(select(CmpScore).where(CmpScore.tenant_id == tenant, CmpScore.flagged.is_(True)).limit(1)).scalar_one()
    f = s.top_features[0]
    assert set(f) == {"feature", "value", "baseline_median", "robust_z"} and f["feature"] in S.FNAMES


def test_disabled_model_is_reported_and_versions_increment(db, tenant):
    a = SV.train_and_persist(db, tenant, {"id": "u", "type": "USER", "role": None})
    SV.set_model_status(db, tenant, a["model_id"], "DISABLED", {"id": "u", "type": "USER", "role": None})
    db.commit()
    assert "DISABLED" in SV.surveillance_view(db, tenant)["model"]["note"]
    b = SV.train_and_persist(db, tenant, {"id": "u", "type": "USER", "role": None})
    assert b["version"] == a["version"] + 1
