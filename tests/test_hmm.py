"""Tests for the HMM implementation.

The important ones verify the two properties the whole system rests on:

1. Our Forward and Viterbi recursions agree with ``hmmlearn`` to numerical
   precision -- so the textbook mapping is real, not decorative.
2. Filtered posteriors differ from smoothed ones, and filtering a prefix gives
   the same answer as filtering the whole series. That is what "no lookahead"
   means operationally, and it is worth a test rather than a comment.
"""

from __future__ import annotations

import numpy as np
import pytest

from stockagent.config import HMMConfig
from stockagent.regime.hmm_model import NotFittedError, RegimeHMM


@pytest.fixture(scope="module")
def synthetic() -> np.ndarray:
    """Two well-separated regimes: calm drift, then violent chop, alternating."""
    rng = np.random.default_rng(11)
    blocks = []
    for i in range(12):
        if i % 2 == 0:
            blocks.append(rng.normal([0.001, -2.4, 0.0], [0.004, 0.20, 0.9], (120, 3)))
        else:
            blocks.append(rng.normal([-0.002, -1.4, 0.6], [0.020, 0.35, 1.3], (80, 3)))
    return np.vstack(blocks)


@pytest.fixture(scope="module")
def fitted(synthetic: np.ndarray) -> RegimeHMM:
    cfg = HMMConfig(n_states=2, n_restarts=3, random_state=7)
    return RegimeHMM(cfg).fit(synthetic, ["ret", "log_vol", "volume_z"])


def test_requires_fit_before_inference() -> None:
    with pytest.raises(NotFittedError):
        RegimeHMM(HMMConfig(n_states=2)).viterbi(np.zeros((10, 3)))


def test_rejects_non_finite_input() -> None:
    X = np.zeros((100, 2))
    X[5, 1] = np.nan
    with pytest.raises(ValueError, match="NaN/inf"):
        RegimeHMM(HMMConfig(n_states=2)).fit(X)


def test_rejects_too_few_samples() -> None:
    with pytest.raises(ValueError, match="too few"):
        RegimeHMM(HMMConfig(n_states=5)).fit(np.random.default_rng(0).normal(size=(40, 2)))


def test_forward_matches_hmmlearn(fitted: RegimeHMM, synthetic: np.ndarray) -> None:
    """Our log P(O|lambda) equals hmmlearn's score()."""
    assert fitted.log_likelihood(synthetic) == pytest.approx(
        float(fitted.model.score(synthetic)), rel=1e-9)


def test_viterbi_matches_hmmlearn(fitted: RegimeHMM, synthetic: np.ndarray) -> None:
    """Our decoded path is identical to hmmlearn's."""
    ours = fitted.viterbi(synthetic)
    theirs = fitted.model.decode(synthetic, algorithm="viterbi")[1]
    assert np.array_equal(ours, theirs)


def test_smoothing_matches_hmmlearn(fitted: RegimeHMM, synthetic: np.ndarray) -> None:
    """Forward-backward equals hmmlearn's predict_proba."""
    assert np.allclose(fitted.smooth(synthetic),
                       fitted.model.predict_proba(synthetic), atol=1e-9)


def test_filtered_probs_are_a_distribution(fitted: RegimeHMM, synthetic: np.ndarray) -> None:
    probs = fitted.filter(synthetic)
    assert probs.shape == (len(synthetic), fitted.n_states)
    assert np.allclose(probs.sum(axis=1), 1.0)
    assert (probs >= 0).all() and (probs <= 1).all()


def test_filtering_is_causal(fitted: RegimeHMM, synthetic: np.ndarray) -> None:
    """Row t of the filtered posterior does not depend on bars after t.

    This is the property that makes the backtest meaningful: filtering a prefix
    must give the same answer as filtering the full series and slicing.
    """
    full = fitted.filter(synthetic)
    for cut in (300, 700, 1100):
        prefix = fitted.filter(synthetic[:cut])
        assert np.allclose(prefix, full[:cut], atol=1e-10), f"prefix {cut} diverged"


def test_smoothing_is_not_causal(fitted: RegimeHMM, synthetic: np.ndarray) -> None:
    """Smoothed posteriors *do* use the future -- which is why we never trade them."""
    full = fitted.smooth(synthetic)
    prefix = fitted.smooth(synthetic[:700])
    assert not np.allclose(prefix, full[:700], atol=1e-6), (
        "smoothing should differ from its own prefix; if it does not, the test "
        "data has no information flowing backwards and the test is vacuous"
    )


def test_filter_and_smooth_differ(fitted: RegimeHMM, synthetic: np.ndarray) -> None:
    assert np.abs(fitted.filter(synthetic) - fitted.smooth(synthetic)).max() > 0.01


def test_recovers_two_regimes(fitted: RegimeHMM, synthetic: np.ndarray) -> None:
    """The fit separates the calm and violent blocks rather than splitting noise."""
    path = fitted.viterbi(synthetic)
    assert len(np.unique(path)) == 2
    means = fitted.means
    # States are sorted by the return channel, so state 0 is the lower-return one
    # and should carry the higher volatility.
    assert means[0, 1] > means[1, 1], "expected the low-return state to be high-vol"


def test_transition_matrix_rows_sum_to_one(fitted: RegimeHMM) -> None:
    assert np.allclose(fitted.transition_matrix.sum(axis=1), 1.0)


def test_expected_duration_is_positive(fitted: RegimeHMM) -> None:
    durations = fitted.expected_duration()
    assert (durations > 0).all() and np.isfinite(durations).all()


def test_stationary_distribution_is_a_distribution(fitted: RegimeHMM) -> None:
    stationary = fitted.stationary_distribution()
    assert stationary.sum() == pytest.approx(1.0, abs=1e-6)
    assert (stationary >= 0).all()


def test_save_and_load_roundtrip(fitted: RegimeHMM, synthetic: np.ndarray, tmp_path) -> None:
    path = fitted.save(tmp_path / "model.pkl")
    reloaded = RegimeHMM.load(path)
    assert np.array_equal(reloaded.viterbi(synthetic), fitted.viterbi(synthetic))
    assert reloaded.log_likelihood(synthetic) == pytest.approx(
        fitted.log_likelihood(synthetic))


def test_restarts_pick_the_best_optimum(synthetic: np.ndarray) -> None:
    """The kept model is the best-scoring restart, not the last one."""
    model = RegimeHMM(HMMConfig(n_states=3, n_restarts=5, random_state=3)).fit(synthetic)
    report = model.fit_report
    assert report is not None
    assert report.log_likelihood == pytest.approx(max(report.restart_scores))
    assert report.restart_spread >= 0.0
