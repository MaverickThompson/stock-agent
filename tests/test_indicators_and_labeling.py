"""Tests for indicator correctness, causality, and regime naming.

The causality tests are the ones that matter most: an indicator that peeks at
the future produces a backtest that cannot be traded, and the leak is invisible
in the output.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stockagent.features import FeatureScaler, build_features
from stockagent.indicators import (add_all_indicators, atr, bollinger, ema,
                                   macd, realized_volatility, rolling_zscore, rsi)
from stockagent.regime.labeling import (ARCHETYPES, LABEL_SETS,
                                        UNRECOGNISED_DISTANCE, ArchetypeScale,
                                        RegimeLabel, _assign_labels,
                                        _mean_run_length)


@pytest.fixture(scope="module")
def prices() -> pd.DataFrame:
    """A synthetic OHLCV series with drift, noise and a crash."""
    rng = np.random.default_rng(5)
    n = 900
    steps = rng.normal(0.0004, 0.011, n)
    steps[500:530] = rng.normal(-0.03, 0.04, 30)      # crash
    close = 100.0 * np.exp(np.cumsum(steps))
    spread = np.abs(rng.normal(0, 0.006, n)) * close
    index = pd.bdate_range("2020-01-01", periods=n)
    return pd.DataFrame(
        {
            "open": close * (1 + rng.normal(0, 0.002, n)),
            "high": close + spread,
            "low": close - spread,
            "close": close,
            "volume": rng.lognormal(15, 0.4, n),
        },
        index=index,
    )


# --------------------------------------------------------------- indicators


def test_rsi_stays_in_bounds(prices: pd.DataFrame) -> None:
    values = rsi(prices["close"], 14).dropna()
    assert not values.empty
    assert values.between(0, 100).all()


def test_rsi_of_a_pure_uptrend_is_one_hundred() -> None:
    """No down-closes means an undefined RS, pinned to 100 by convention."""
    rising = pd.Series(np.arange(1, 60, dtype=float))
    assert rsi(rising, 14).dropna().iloc[-1] == pytest.approx(100.0)


def test_atr_is_positive_and_respects_gaps(prices: pd.DataFrame) -> None:
    values = atr(prices["high"], prices["low"], prices["close"], 14).dropna()
    assert (values > 0).all()
    # ATR must be at least as large as the mean high-low range would suggest
    # only when gaps exist; here we just assert it reacts to the crash.
    assert values.loc["2021-11-01":].max() > values.iloc[:100].mean()


def test_macd_histogram_is_line_minus_signal(prices: pd.DataFrame) -> None:
    frame = macd(prices["close"]).dropna()
    assert np.allclose(frame["macd_hist"], frame["macd"] - frame["macd_signal"])


def test_macd_rejects_bad_periods(prices: pd.DataFrame) -> None:
    with pytest.raises(ValueError, match="fast"):
        macd(prices["close"], fast=26, slow=12)


def test_bollinger_bands_are_ordered(prices: pd.DataFrame) -> None:
    frame = bollinger(prices["close"]).dropna()
    assert (frame["bb_upper"] >= frame["bb_mid"]).all()
    assert (frame["bb_mid"] >= frame["bb_lower"]).all()


def test_ema_reacts_faster_than_a_longer_ema(prices: pd.DataFrame) -> None:
    fast, slow = ema(prices["close"], 20), ema(prices["close"], 200)
    assert fast.dropna().std() > slow.dropna().std()


@pytest.mark.parametrize("func,args", [
    (lambda s: rsi(s, 14), None),
    (lambda s: ema(s, 20), None),
    (lambda s: realized_volatility(s.pct_change(), 21), None),
    (lambda s: rolling_zscore(s, 63), None),
])
def test_indicators_are_causal(prices: pd.DataFrame, func, args) -> None:
    """Truncating the future must not change any past value.

    This is the test that catches an accidental full-sample mean, a centred
    rolling window, or a stray ``shift(-1)``.
    """
    close = prices["close"]
    full = func(close)
    cut = 600
    prefix = func(close.iloc[:cut])
    pd.testing.assert_series_equal(prefix, full.iloc[:cut], check_names=False)


def test_add_all_indicators_is_causal(prices: pd.DataFrame) -> None:
    cut = 700
    full = add_all_indicators(prices)
    prefix = add_all_indicators(prices.iloc[:cut])
    for column in ("rsi", "macd", "atr", "ema_200", "bb_pct_b", "obv",
                   "realized_vol", "volume_z", "drawdown", "mom_63"):
        pd.testing.assert_series_equal(
            prefix[column], full[column].iloc[:cut], check_names=False,
            obj=f"{column} leaked future information")


def test_add_all_indicators_requires_columns() -> None:
    with pytest.raises(KeyError, match="missing columns"):
        add_all_indicators(pd.DataFrame({"close": [1.0, 2.0]}))


# ----------------------------------------------------------------- features


def test_build_features_produces_three_channels(prices: pd.DataFrame) -> None:
    feats = build_features(prices)
    assert list(feats.columns) == ["ret", "log_vol", "volume_z"]
    assert feats.notna().all().all()
    assert np.isfinite(feats.to_numpy()).all()


def test_volume_channel_is_neutral_without_volume(prices: pd.DataFrame) -> None:
    """An index such as ^VIX has no volume; the channel must not become NaN."""
    no_volume = prices.copy()
    no_volume["volume"] = 0.0
    feats = build_features(no_volume)
    assert (feats["volume_z"] == 0.0).all()


def test_scaler_uses_training_statistics_only(prices: pd.DataFrame) -> None:
    """Transforming later data must not shift the scaler's own parameters."""
    feats = build_features(prices).to_numpy()
    scaler = FeatureScaler(clip_sigma=5.0).fit(feats[:400])
    mean_before = scaler.mean_.copy()
    scaler.transform(feats)
    assert np.array_equal(scaler.mean_, mean_before)


def test_scaler_clips_outliers(prices: pd.DataFrame) -> None:
    feats = build_features(prices).to_numpy()
    scaled = FeatureScaler(clip_sigma=3.0).fit_transform(feats)
    assert np.abs(scaled).max() <= 3.0 + 1e-9


# ----------------------------------------------------------------- labeling


def _raw(mean: float, vol: float) -> dict[str, float]:
    return {"mean": mean, "vol": vol, "freq": 0.2, "dur": 20.0, "n": 400}


def test_bear_requires_actually_losing_money() -> None:
    """The regression this module exists for.

    A state returning +0.4%/yr must never be labelled Bear just because it is
    the least bullish of the five.
    """
    raw = {
        0: _raw(-0.049, 0.433),   # crisis: high vol, mildly negative
        1: _raw(0.004, 0.198),    # chop: flat, moderate vol
        2: _raw(0.095, 0.146),
        3: _raw(0.110, 0.104),
        4: _raw(0.224, 0.070),
    }
    labels = {s: lab for s, (lab, _) in _assign_labels(raw, 5).items()}
    assert labels[1] is not RegimeLabel.BEAR, "a +0.4%/yr state is not a bear market"
    assert labels[1] is RegimeLabel.SIDEWAYS
    assert labels[0] is RegimeLabel.HIGH_VOLATILITY
    assert all(labels[s] is RegimeLabel.BULL for s in (2, 3, 4))


def test_a_real_bear_state_is_labelled_bear() -> None:
    raw = {0: _raw(-0.22, 0.26), 1: _raw(0.0, 0.12), 2: _raw(0.16, 0.11)}
    labels = {s: lab for s, (lab, _) in _assign_labels(raw, 3).items()}
    assert labels[0] is RegimeLabel.BEAR
    assert labels[1] is RegimeLabel.SIDEWAYS
    assert labels[2] is RegimeLabel.BULL


def test_labels_may_repeat() -> None:
    """No bijection: three bull-ish states are allowed to all be Bull."""
    raw = {0: _raw(0.14, 0.10), 1: _raw(0.16, 0.11), 2: _raw(0.18, 0.12)}
    labels = [lab for lab, _ in _assign_labels(raw, 3).values()]
    assert labels.count(RegimeLabel.BULL) == 3


def test_unvisited_state_is_flagged_unrecognised() -> None:
    raw = {0: _raw(0.1, 0.12), 1: {"mean": 0.0, "vol": float("nan"),
                                   "freq": 0.0, "dur": 0.0, "n": 0}}
    assignments = _assign_labels(raw, 2)
    assert assignments[1][1] == float("inf")


def test_recovery_is_separated_from_bull_by_volatility() -> None:
    """Same positive drift, different volatility, different regime.

    The two states are deliberately far apart on the volatility axis. Now that
    archetypes are scaled to the instrument's own baseline, a state only
    slightly above average volatility is genuinely ambiguous between Bull and
    Recovery, and pinning a test to that coin-flip would be testing noise.
    """
    raw = {
        0: _raw(0.30, 0.38),   # bouncing hard, still violent -> Recovery
        1: _raw(0.15, 0.11),   # grinding up, calm            -> Bull
        2: _raw(-0.21, 0.25),
        3: _raw(0.00, 0.13),
        4: _raw(-0.05, 0.45),
    }
    labels = {s: lab for s, (lab, _) in _assign_labels(raw, 5).items()}
    assert labels[0] is RegimeLabel.RECOVERY
    assert labels[1] is RegimeLabel.BULL


def test_archetypes_scale_to_a_volatile_instrument() -> None:
    """The ERIC regression: a wild single stock must still be readable.

    Under fixed index anchors this state sat 5.2 units from every archetype and
    was reported as an unrecognised HighVolatility. Scaled to the instrument it
    is plainly a bear regime.
    """
    raw = {
        0: _raw(-0.595, 0.712),   # the state that broke the old anchors
        1: _raw(0.180, 0.290),
        2: _raw(0.020, 0.240),
        3: _raw(0.350, 0.330),
        4: _raw(-0.120, 0.450),
    }
    assignments = _assign_labels(raw, 5)
    label, distance = assignments[0]
    assert label is RegimeLabel.BEAR
    assert distance < UNRECOGNISED_DISTANCE, (
        f"state sits {distance:.1f} units from Bear; still unrecognised")


def test_high_volatility_requires_absolute_high_volatility() -> None:
    """A calm instrument's choppiest state is not a crash regime."""
    raw = {
        0: _raw(0.14, 0.10),
        1: _raw(0.16, 0.11),
        2: _raw(0.18, 0.12),   # highest vol here, but 12% is objectively calm
        3: _raw(0.10, 0.09),
        4: _raw(0.05, 0.14),
    }
    labels = [lab for lab, _ in _assign_labels(raw, 5).values()]
    assert RegimeLabel.HIGH_VOLATILITY not in labels
    assert RegimeLabel.RECOVERY not in labels


def test_bear_guard_survives_rescaling() -> None:
    """No rescaling may label a profitable state Bear."""
    raw = {
        0: _raw(0.05, 0.80),   # profitable but violent
        1: _raw(0.30, 0.10),
        2: _raw(0.12, 0.35),
        3: _raw(0.20, 0.25),
        4: _raw(-0.30, 0.40),
    }
    labels = {s: lab for s, (lab, _) in _assign_labels(raw, 5).items()}
    assert labels[0] is not RegimeLabel.BEAR
    assert labels[4] is RegimeLabel.BEAR   # this one actually lost money


def test_scale_calibrates_to_baseline_volatility() -> None:
    calm = ArchetypeScale.from_states({0: _raw(0.10, 0.12), 1: _raw(0.05, 0.16)})
    wild = ArchetypeScale.from_states({0: _raw(0.10, 0.55), 1: _raw(0.05, 0.65)})
    assert wild.base_vol > calm.base_vol
    assert wild.vol_center > calm.vol_center
    assert wild.return_scale > calm.return_scale
    # An index-like instrument must reproduce the original hand-set anchors.
    index = ArchetypeScale.from_states({0: _raw(0.10, 0.167)})
    assert index.return_scale == pytest.approx(0.15, abs=0.01)
    assert index.vol_scale == pytest.approx(0.10, abs=0.01)


def test_exposure_ordering_is_sane() -> None:
    assert RegimeLabel.BEAR.target_exposure == 0.0
    assert (RegimeLabel.BULL.target_exposure
            > RegimeLabel.RECOVERY.target_exposure
            > RegimeLabel.SIDEWAYS.target_exposure
            > RegimeLabel.HIGH_VOLATILITY.target_exposure)


def test_label_sets_cover_supported_state_counts() -> None:
    for n in (2, 3, 4, 5, 6):
        assert LABEL_SETS[n], f"no archetypes registered for {n} states"
        assert all(label in ARCHETYPES for label in LABEL_SETS[n])


def test_mean_run_length() -> None:
    path = np.array([0, 0, 0, 1, 1, 0, 0, 1])
    assert _mean_run_length(path, 0) == pytest.approx(5 / 2)
    assert _mean_run_length(path, 1) == pytest.approx(3 / 2)
    assert _mean_run_length(path, 2) == 0.0
