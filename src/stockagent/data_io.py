"""Loading and validating price history.

Reads the CSVs written by ``scripts/fetch_data.py`` and enforces the invariants
the rest of the system assumes: a sorted unique DatetimeIndex, numeric OHLCV,
no non-positive prices, and high >= low.

Bad data is the cheapest way to get a confident wrong answer, so validation
raises rather than warning where an assumption is load-bearing.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .logging_setup import get_logger

LOG = get_logger("data_io")

REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")


class DataError(ValueError):
    """Raised when price data fails validation."""


@dataclass(frozen=True)
class DataQuality:
    """What we know about a series' reliability, reported alongside decisions."""

    symbol: str
    bars: int
    start: pd.Timestamp
    end: pd.Timestamp
    #: Calendar days since the last bar. Large values mean stale data.
    staleness_days: int
    #: Bars whose absolute log return exceeds 25% -- candidate bad prints.
    extreme_bars: int
    #: True when the series has no volume at all (an index such as ^VIX).
    synthetic_volume: bool
    gaps: int

    @property
    def is_usable(self) -> bool:
        return self.bars >= 250 and self.staleness_days <= 10

    def warnings(self) -> list[str]:
        out: list[str] = []
        if self.staleness_days > 5:
            out.append(f"{self.symbol}: data is {self.staleness_days} days stale")
        if self.bars < 400:
            out.append(f"{self.symbol}: only {self.bars} bars; estimates will be noisy")
        if self.extreme_bars:
            out.append(f"{self.symbol}: {self.extreme_bars} bars move >25% (check for bad prints)")
        if self.synthetic_volume:
            out.append(f"{self.symbol}: no volume data; volume features are neutral placeholders")
        if self.gaps > 10:
            out.append(f"{self.symbol}: {self.gaps} gaps >5 trading days in the series")
        return out


def load_prices(path: pathlib.Path | str, *, symbol: str | None = None) -> pd.DataFrame:
    """Load one OHLCV CSV into a validated, date-indexed frame."""
    path = pathlib.Path(path)
    symbol = symbol or path.stem
    if not path.exists():
        raise DataError(f"{symbol}: no such file {path}. Run scripts/fetch_data.py first.")

    df = pd.read_csv(path)
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

    date_col = next((c for c in ("date", "datetime", "timestamp") if c in df.columns), None)
    if date_col is None:
        raise DataError(f"{symbol}: no date column in {sorted(df.columns)}")

    df[date_col] = pd.to_datetime(df[date_col], errors="coerce", utc=False)
    if df[date_col].isna().any():
        bad = int(df[date_col].isna().sum())
        LOG.warning("%s: dropping %d rows with unparseable dates", symbol, bad)
        df = df.dropna(subset=[date_col])

    df = df.set_index(date_col).sort_index()
    df.index.name = "date"

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise DataError(f"{symbol}: missing required columns {missing}")

    for col in [*REQUIRED_COLUMNS, *(["adj_close"] if "adj_close" in df.columns else [])]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # A duplicated date usually means an appended partial bar; keep the last.
    if df.index.has_duplicates:
        dupes = int(df.index.duplicated().sum())
        LOG.warning("%s: collapsing %d duplicate dates", symbol, dupes)
        df = df[~df.index.duplicated(keep="last")]

    before = len(df)
    df = df.dropna(subset=["close"])
    if len(df) < before:
        LOG.warning("%s: dropped %d rows with no close", symbol, before - len(df))

    if (df["close"] <= 0).any():
        raise DataError(f"{symbol}: contains non-positive close prices")
    if (df["high"] < df["low"]).any():
        n = int((df["high"] < df["low"]).sum())
        raise DataError(f"{symbol}: {n} bars have high < low; the file is corrupt")

    # Prefer split/dividend-adjusted closes for return calculations. Keeping the
    # raw close would put phantom -50% returns in the training data on every
    # 2-for-1 split, and the HMM would happily learn them as a crash regime.
    if "adj_close" in df.columns and df["adj_close"].notna().all():
        ratio = (df["adj_close"] / df["close"]).replace([np.inf, -np.inf], np.nan)
        df["raw_close"] = df["close"]
        for col in ("open", "high", "low", "close"):
            df[col] = df[col] * ratio

    df["volume"] = df["volume"].fillna(0.0).clip(lower=0.0)
    df.attrs["symbol"] = symbol
    LOG.debug("loaded %s: %d bars %s..%s", symbol, len(df),
              df.index[0].date(), df.index[-1].date())
    return df


def assess_quality(df: pd.DataFrame, symbol: str | None = None,
                   *, asof: pd.Timestamp | None = None) -> DataQuality:
    """Summarise how much the series can be trusted."""
    symbol = symbol or df.attrs.get("symbol", "?")
    asof = asof or pd.Timestamp.today().normalize()
    log_ret = np.log(df["close"] / df["close"].shift(1))
    spacing = df.index.to_series().diff().dt.days
    return DataQuality(
        symbol=symbol,
        bars=len(df),
        start=df.index[0],
        end=df.index[-1],
        staleness_days=max(0, int((asof - df.index[-1]).days)),
        extreme_bars=int((log_ret.abs() > 0.25).sum()),
        synthetic_volume=bool((df["volume"] <= 0).all()),
        gaps=int((spacing > 7).sum()),
    )


def load_universe(data_dir: pathlib.Path | str, symbols: list[str] | None = None,
                  *, min_bars: int = 400) -> dict[str, pd.DataFrame]:
    """Load every requested symbol, skipping any that fail validation.

    Returns only usable frames. A symbol that cannot be loaded is logged and
    omitted rather than raising, so one bad CSV cannot stop a scan of forty.
    """
    data_dir = pathlib.Path(data_dir)
    if not data_dir.exists():
        raise DataError(f"data directory {data_dir} does not exist")

    if symbols:
        paths = [data_dir / f"{s}.csv" for s in symbols]
    else:
        paths = sorted(data_dir.glob("*.csv"))

    out: dict[str, pd.DataFrame] = {}
    for path in paths:
        symbol = path.stem
        try:
            df = load_prices(path, symbol=symbol)
        except (DataError, pd.errors.ParserError) as exc:
            LOG.error("skipping %s: %s", symbol, exc)
            continue
        if len(df) < min_bars:
            LOG.warning("skipping %s: %d bars < required %d", symbol, len(df), min_bars)
            continue
        out[symbol] = df

    if not out:
        raise DataError(f"no usable price files in {data_dir}")
    LOG.info("loaded %d symbols from %s", len(out), data_dir)
    return out


def align(frames: dict[str, pd.DataFrame], *, how: str = "inner") -> dict[str, pd.DataFrame]:
    """Restrict frames to a shared calendar.

    Needed before any cross-sectional comparison. ``inner`` is the honest
    default: BTC trades weekends and SPY does not, and silently forward-filling
    equities across a weekend invents prices that never traded.
    """
    if not frames:
        return {}
    index = None
    for df in frames.values():
        index = df.index if index is None else (
            index.intersection(df.index) if how == "inner" else index.union(df.index)
        )
    return {sym: df.reindex(index).dropna(subset=["close"]) for sym, df in frames.items()}
