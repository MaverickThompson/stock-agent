"""The observation vector fed to the HMM.

Three channels, as specified: **daily return, volume, volatility**.

    ret        log(close_t / close_{t-1})
    log_vol    log of 21-day realised volatility, annualised
    volume_z   63-day trailing z-score of log volume

Two deliberate transformations:

*Volatility is logged.* Realised volatility is bounded below by zero and has a
long right tail. A Gaussian emission fitted to raw volatility spends its density
budget on impossible negative values and gets dragged around by the tail. Logged,
it is close to symmetric.

*Volume is z-scored against a trailing window, not the whole sample.* Share
volume trends with liquidity over 20 years; an absolute level means nothing
across decades. The trailing window makes "unusually heavy for this stock,
lately" the actual signal.

Scaling policy is the part that matters for honesty. :class:`FeatureScaler`
computes its mean and standard deviation on the **training window only** and
applies them unchanged to later data. Standardising over the full sample would
leak the future into every historical bar -- the backtest would know the size of
the 2020 crash before it happened.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .config import FeatureConfig
from .indicators import realized_volatility, rolling_zscore
from .logging_setup import get_logger

LOG = get_logger("features")

FEATURE_NAMES: tuple[str, ...] = ("ret", "log_vol", "volume_z")


@dataclass
class FeatureScaler:
    """Standardise features using training-window statistics only."""

    clip_sigma: float = 5.0
    mean_: np.ndarray | None = field(default=None, repr=False)
    std_: np.ndarray | None = field(default=None, repr=False)
    names_: list[str] = field(default_factory=list)

    def fit(self, X: np.ndarray, names: list[str] | None = None) -> "FeatureScaler":
        X = np.asarray(X, dtype=float)
        self.mean_ = np.nanmean(X, axis=0)
        std = np.nanstd(X, axis=0, ddof=0)
        # A constant column would divide by zero; leave it at unit scale so it
        # becomes a harmless all-zero feature rather than NaN or inf.
        degenerate = std < 1e-12
        if degenerate.any():
            LOG.warning("features %s are constant in the training window",
                        [names[i] if names else i for i in np.flatnonzero(degenerate)])
        self.std_ = np.where(degenerate, 1.0, std)
        self.names_ = list(names or [])
        return self

    def transform(self, X: np.ndarray) -> np.ndarray:
        if self.mean_ is None or self.std_ is None:
            raise RuntimeError("FeatureScaler.transform called before fit")
        Z = (np.asarray(X, dtype=float) - self.mean_) / self.std_
        # Winsorise. Gaussian emissions are not fat-tailed, and without this a
        # single -12 sigma bar can capture an entire hidden state.
        return np.clip(Z, -self.clip_sigma, self.clip_sigma)

    def fit_transform(self, X: np.ndarray, names: list[str] | None = None) -> np.ndarray:
        return self.fit(X, names).transform(X)


def build_features(df: pd.DataFrame, cfg: FeatureConfig | None = None,
                   *, close_col: str = "close") -> pd.DataFrame:
    """Build the raw (unscaled) observation frame for one symbol.

    Returns a frame indexed by date with columns :data:`FEATURE_NAMES`. Rows
    where any feature is undefined (the warm-up period) are dropped, so the
    result is directly usable as an HMM input once scaled.
    """
    cfg = cfg or FeatureConfig()
    if close_col not in df.columns:
        raise KeyError(f"build_features: no {close_col!r} column")

    close = df[close_col].astype(float)
    out = pd.DataFrame(index=df.index)

    out["ret"] = np.log(close / close.shift(1))

    vol = realized_volatility(out["ret"], cfg.volatility_window, annualize=True)
    # Floor before logging: a genuinely zero-volatility window (a halted or
    # stale series) would otherwise produce -inf and poison the fit.
    out["log_vol"] = np.log(vol.clip(lower=1e-6))

    volume = df["volume"].astype(float) if "volume" in df.columns else pd.Series(0.0, index=df.index)
    if (volume <= 0).all():
        # Indices such as ^VIX carry no volume. A constant zero is the honest
        # encoding: "no information", not "no trading".
        LOG.debug("no volume data for %s; volume channel set to neutral",
                  df.attrs.get("symbol", "?"))
        out["volume_z"] = 0.0
    else:
        out["volume_z"] = rolling_zscore(np.log1p(volume), cfg.volume_window)

    before = len(out)
    out = out.replace([np.inf, -np.inf], np.nan).dropna()
    dropped = before - len(out)
    if dropped:
        LOG.debug("dropped %d warm-up/invalid rows (%d remain)", dropped, len(out))
    if out.empty:
        raise ValueError(
            "no usable feature rows. The series is probably shorter than the "
            f"{cfg.volatility_window}-day volatility warm-up."
        )
    return out[list(FEATURE_NAMES)]


def train_test_split_by_index(features: pd.DataFrame, train_end: int
                              ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split positionally into training and out-of-sample halves."""
    if not 0 < train_end < len(features):
        raise ValueError(f"train_end={train_end} outside (0, {len(features)})")
    return features.iloc[:train_end], features.iloc[train_end:]


def prepare_for_hmm(features: pd.DataFrame, train_end: int | None = None,
                    cfg: FeatureConfig | None = None
                    ) -> tuple[np.ndarray, FeatureScaler]:
    """Scale a feature frame for the HMM, fitting the scaler causally.

    Parameters
    ----------
    features:
        Output of :func:`build_features`.
    train_end:
        Number of leading rows to fit the scaler on. ``None`` fits on
        everything, which is correct only for a one-shot descriptive fit over
        history -- never for generating a signal you intend to trade.
    """
    cfg = cfg or FeatureConfig()
    values = features.to_numpy(dtype=float)
    scaler = FeatureScaler(clip_sigma=cfg.clip_sigma)
    fit_slice = values if train_end is None else values[:train_end]
    if train_end is not None and train_end < 100:
        raise ValueError(f"train_end={train_end} is too small to estimate feature scale")
    scaler.fit(fit_slice, list(features.columns))
    return scaler.transform(values), scaler
