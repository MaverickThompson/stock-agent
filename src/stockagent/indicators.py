"""Technical indicators, implemented in pandas.

TA-Lib is deliberately not a dependency: it needs a C build step that is awkward
on Windows, and everything below is a few lines of pandas. All functions are
**causal** -- the value at bar *t* uses only bars <= *t*. That is not a stylistic
choice, it is what makes the backtest meaningful.

Wilder's smoothing (RSI, ATR) is ``ewm(alpha=1/period, adjust=False)``, which
matches the recursive definition in his book and the values TA-Lib reports.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = [
    "ema", "sma", "rsi", "macd", "true_range", "atr", "bollinger",
    "obv", "realized_volatility", "rolling_zscore", "add_all_indicators",
]


def _validate(series: pd.Series, period: int, name: str) -> None:
    if period < 1:
        raise ValueError(f"{name}: period must be >= 1, got {period}")
    if not isinstance(series, pd.Series):
        raise TypeError(f"{name}: expected a pandas Series, got {type(series).__name__}")


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average with a ``2/(n+1)`` smoothing factor."""
    _validate(series, period, "ema")
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple moving average."""
    _validate(series, period, "sma")
    return series.rolling(period, min_periods=period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's Relative Strength Index, on a 0-100 scale.

    A run with no down-closes gives an undefined RS (division by zero); that
    case is pinned to 100, and its mirror to 0, which is the conventional
    reading of "maximally overbought / oversold".
    """
    _validate(close, period, "rsi")
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)

    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()

    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    out = out.where(avg_loss != 0.0, 100.0)
    out = out.where(~((avg_gain == 0.0) & (avg_loss == 0.0)), 50.0)
    return out.where(avg_gain.notna(), np.nan).rename("rsi")


def macd(close: pd.Series, fast: int = 12, slow: int = 26,
         signal: int = 9) -> pd.DataFrame:
    """MACD line, signal line and histogram."""
    if fast >= slow:
        raise ValueError(f"macd: fast ({fast}) must be < slow ({slow})")
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame(
        {"macd": macd_line, "macd_signal": signal_line,
         "macd_hist": macd_line - signal_line}
    )


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """True Range: the widest of today's range and the two gap-adjusted ranges."""
    prev_close = close.shift(1)
    ranges = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    )
    return ranges.max(axis=1).rename("true_range")


def atr(high: pd.Series, low: pd.Series, close: pd.Series,
        period: int = 14) -> pd.Series:
    """Average True Range (Wilder). The unit of risk for stop placement."""
    _validate(close, period, "atr")
    tr = true_range(high, low, close)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean().rename("atr")


def bollinger(close: pd.Series, period: int = 20,
              n_std: float = 2.0) -> pd.DataFrame:
    """Bollinger Bands plus %B and bandwidth.

    Uses the population standard deviation (``ddof=0``), which is the original
    definition and what charting packages display.
    """
    _validate(close, period, "bollinger")
    mid = sma(close, period)
    std = close.rolling(period, min_periods=period).std(ddof=0)
    upper, lower = mid + n_std * std, mid - n_std * std
    width = (upper - lower).replace(0.0, np.nan)
    return pd.DataFrame(
        {
            "bb_mid": mid,
            "bb_upper": upper,
            "bb_lower": lower,
            "bb_pct_b": (close - lower) / width,
            "bb_bandwidth": (upper - lower) / mid.replace(0.0, np.nan),
        }
    )


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """On-Balance Volume: cumulative signed volume."""
    direction = np.sign(close.diff()).fillna(0.0)
    return (direction * volume.fillna(0.0)).cumsum().rename("obv")


def realized_volatility(returns: pd.Series, window: int = 21,
                        annualize: bool = True) -> pd.Series:
    """Rolling standard deviation of returns, annualised by default (252d)."""
    _validate(returns, window, "realized_volatility")
    vol = returns.rolling(window, min_periods=window).std(ddof=0)
    if annualize:
        vol = vol * np.sqrt(252.0)
    return vol.rename("realized_vol")


def rolling_zscore(series: pd.Series, window: int,
                   min_periods: int | None = None) -> pd.Series:
    """Z-score against a trailing window.

    Standardising against a *trailing* window rather than the full sample is
    what keeps this causal: a full-sample mean would leak the future into
    every historical bar.
    """
    _validate(series, window, "rolling_zscore")
    min_periods = min_periods or max(2, window // 4)
    mean = series.rolling(window, min_periods=min_periods).mean()
    std = series.rolling(window, min_periods=min_periods).std(ddof=0)
    return (series - mean) / std.replace(0.0, np.nan)


def add_all_indicators(df: pd.DataFrame, *, close_col: str = "close") -> pd.DataFrame:
    """Attach the full indicator set named in ``strategy/indicators.md``.

    Expects columns ``open, high, low, close, volume`` and returns a new frame
    with the originals plus RSI, MACD, EMA 20/50/200, ATR, Bollinger Bands, OBV
    and volume statistics.
    """
    required = {"high", "low", close_col, "volume"}
    missing = required - set(df.columns)
    if missing:
        raise KeyError(f"add_all_indicators: missing columns {sorted(missing)}")

    out = df.copy()
    close, high, low, volume = out[close_col], out["high"], out["low"], out["volume"]

    out["rsi"] = rsi(close, 14)
    out = out.join(macd(close))
    for span in (20, 50, 200):
        out[f"ema_{span}"] = ema(close, span)
    out["atr"] = atr(high, low, close, 14)
    #: ATR as a fraction of price, so it is comparable across symbols.
    out["atr_pct"] = out["atr"] / close.replace(0.0, np.nan)
    out = out.join(bollinger(close))
    out["obv"] = obv(close, volume)

    out["log_return"] = np.log(close / close.shift(1))
    out["realized_vol"] = realized_volatility(out["log_return"], 21)
    out["dollar_volume"] = (close * volume).rolling(20, min_periods=10).median()
    # Volume is lognormal-ish and trends with liquidity; z-score the log against
    # a trailing window rather than using raw share counts.
    out["volume_z"] = rolling_zscore(np.log1p(volume.astype("float64")), 63)

    # Trend structure, used by the discovery and devil's-advocate agents.
    out["above_ema_50"] = (close > out["ema_50"]).astype("float64")
    out["above_ema_200"] = (close > out["ema_200"]).astype("float64")
    out["golden_cross"] = (out["ema_50"] > out["ema_200"]).astype("float64")
    out["dist_ema_200"] = (close - out["ema_200"]) / out["ema_200"].replace(0.0, np.nan)
    for lookback in (21, 63, 126, 252):
        out[f"mom_{lookback}"] = close.pct_change(lookback)
    # Drawdown from the running peak *so far* -- expanding, therefore causal.
    out["drawdown"] = close / close.cummax() - 1.0
    return out
