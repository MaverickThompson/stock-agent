"""Download daily OHLCV history to ``data/*.csv``.

Deliberately depends on nothing but the standard library and ``requests`` so it
can be run before the scientific stack is installed, and so a cold clone can
populate ``data/`` in one command:

    python scripts/fetch_data.py

Source is the Yahoo Finance v8 chart endpoint, which needs no API key. The MCP
market-data connectors are used elsewhere in this project for *live* quotes,
news and fundamentals; they are a poor fit for bulk history because a 20-year
daily series is megabytes of JSON per symbol.

Output schema (one file per symbol, ascending by date):

    date,open,high,low,close,adj_close,volume
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import pathlib
import sys
import time
from typing import Any

import requests

LOG = logging.getLogger("fetch_data")

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

# Local file name -> Yahoo ticker. VIX is an index (^VIX) and has no volume;
# BTC-USD trades 7 days a week. Both quirks are handled downstream.
DEFAULT_SYMBOLS: dict[str, str] = {
    "SPY": "SPY",
    "QQQ": "QQQ",
    "VIX": "^VIX",
    "BTC": "BTC-USD",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"


class FetchError(RuntimeError):
    """Raised when a symbol could not be downloaded or parsed."""


def sp500_symbols(*, timeout: int = 45) -> dict[str, str]:
    """Current S&P 500 constituents as ``{name: yahoo_ticker}``.

    Breadth is the lever a small systematic strategy actually controls:
    information ratio scales with the square root of the number of independent
    bets, so a four-symbol universe caps your return no matter how good the
    signal is. This is here to make a 500-name universe one flag away.
    """
    import re

    response = requests.get(SP500_URL, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    table = response.text.split('id="constituents"', 1)[-1].split("</table>", 1)[0]

    tickers = re.findall(
        r'<td><a rel="nofollow" class="external text" href="[^"]+">([A-Z][A-Z\.\-]{0,6})</a>',
        table,
    )
    if not tickers:  # markup changed; fall back to a looser pattern
        tickers = re.findall(r">([A-Z]{1,5}(?:\.[A-Z])?)</a>\s*</td>", table)
    if not tickers:
        raise FetchError("could not parse any tickers from the S&P 500 page")

    out: dict[str, str] = {}
    for ticker in dict.fromkeys(tickers):          # de-dupe, keep order
        # Yahoo writes class shares with a hyphen: BRK.B -> BRK-B.
        out[ticker.replace(".", "-")] = ticker.replace(".", "-")
    LOG.info("resolved %d S&P 500 constituents", len(out))
    return out


def _request_chart(ticker: str, range_: str, *, timeout: int, retries: int) -> dict[str, Any]:
    """GET the chart payload for ``ticker``, retrying transient failures."""
    url = CHART_URL.format(symbol=requests.utils.quote(ticker))
    params = {"range": range_, "interval": "1d", "events": "div,split"}

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, json.JSONDecodeError) as exc:
            last_error = exc
            wait = 2**attempt
            LOG.warning("%s: attempt %d/%d failed (%s); retrying in %ds",
                        ticker, attempt, retries, exc, wait)
            time.sleep(wait)
            continue

        error = (payload.get("chart") or {}).get("error")
        if error:
            raise FetchError(f"{ticker}: API returned error {error}")
        return payload

    raise FetchError(f"{ticker}: giving up after {retries} attempts ({last_error})")


def _rows_from_payload(payload: dict[str, Any], ticker: str) -> list[dict[str, Any]]:
    """Flatten Yahoo's column-oriented payload into row dicts.

    Yahoo pads gaps with nulls. A bar is kept only when it has a usable close;
    other missing fields fall back to the close so OHLC stays self-consistent.
    """
    try:
        result = payload["chart"]["result"][0]
        stamps = result["timestamp"]
        quote = result["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise FetchError(f"{ticker}: unexpected payload shape ({exc})") from exc

    adjclose_block = (result.get("indicators", {}).get("adjclose") or [{}])[0]
    adjclose = adjclose_block.get("adjclose") or [None] * len(stamps)

    rows: list[dict[str, Any]] = []
    for i, stamp in enumerate(stamps):
        close = quote.get("close", [None] * len(stamps))[i]
        if close is None:
            continue

        def pick(field: str, fallback: float = close) -> float:
            value = quote.get(field, [None] * len(stamps))[i]
            return float(value) if value is not None else float(fallback)

        adj = adjclose[i] if i < len(adjclose) and adjclose[i] is not None else close
        rows.append(
            {
                "date": dt.datetime.utcfromtimestamp(stamp).strftime("%Y-%m-%d"),
                "open": round(pick("open"), 6),
                "high": round(pick("high"), 6),
                "low": round(pick("low"), 6),
                "close": round(float(close), 6),
                "adj_close": round(float(adj), 6),
                # ^VIX and other indices report no volume; 0 is the honest value.
                "volume": int(quote.get("volume", [None] * len(stamps))[i] or 0),
            }
        )

    # Yahoo occasionally repeats the most recent (partial) bar. Keep the last
    # occurrence of each date so the freshest values win, then sort.
    deduped = {row["date"]: row for row in rows}
    return sorted(deduped.values(), key=lambda r: r["date"])


def fetch_symbol(name: str, ticker: str, out_dir: pathlib.Path, *,
                 range_: str = "20y", timeout: int = 45, retries: int = 3) -> pathlib.Path:
    """Download one symbol and write ``out_dir/<name>.csv``. Returns the path."""
    LOG.info("fetching %s (%s), range=%s", name, ticker, range_)
    rows = _rows_from_payload(_request_chart(ticker, range_, timeout=timeout, retries=retries), ticker)
    if not rows:
        raise FetchError(f"{ticker}: no usable bars returned")

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}.csv"
    fields = ["date", "open", "high", "low", "close", "adj_close", "volume"]

    # Write to a temp file and replace, so an interrupted run never leaves a
    # half-written CSV that later looks like valid data.
    tmp = path.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)

    LOG.info("wrote %s: %d bars, %s -> %s", path.name, len(rows), rows[0]["date"], rows[-1]["date"])
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=pathlib.Path,
                        default=pathlib.Path(__file__).resolve().parents[1] / "data",
                        help="output directory (default: <project>/data)")
    parser.add_argument("--range", dest="range_", default="20y",
                        help="Yahoo range string: 5y, 10y, 20y, max (default: 20y)")
    parser.add_argument("--symbols", nargs="*", default=None,
                        help="NAME=TICKER pairs, e.g. ERIC=ERIC. Default: SPY QQQ VIX BTC")
    parser.add_argument("--sp500", action="store_true",
                        help="fetch all S&P 500 constituents (adds breadth)")
    parser.add_argument("--delay", type=float, default=0.3,
                        help="seconds between requests, to stay polite (default 0.3)")
    parser.add_argument("--skip-existing", action="store_true",
                        help="skip symbols whose CSV is already up to date")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

    symbols = dict(DEFAULT_SYMBOLS)
    if args.symbols:
        symbols = {}
        for pair in args.symbols:
            name, _, ticker = pair.partition("=")
            symbols[name] = ticker or name
    if args.sp500:
        symbols = {**DEFAULT_SYMBOLS, **sp500_symbols()}

    today = dt.date.today().isoformat()
    failures: list[str] = []
    for i, (name, ticker) in enumerate(symbols.items(), 1):
        path = args.out / f"{name}.csv"
        if args.skip_existing and path.exists():
            try:
                last = path.read_text(encoding="utf-8").rstrip().rsplit("\n", 1)[-1]
                if last.startswith(today):
                    LOG.debug("%s already current, skipping", name)
                    continue
            except OSError:
                pass
        try:
            fetch_symbol(name, ticker, args.out, range_=args.range_)
        except FetchError as exc:
            LOG.error("%s", exc)
            failures.append(name)
        except requests.RequestException as exc:
            LOG.error("%s: network error (%s)", name, exc)
            failures.append(name)
        if args.delay and i < len(symbols):
            time.sleep(args.delay)

    if failures:
        LOG.error("failed: %s", ", ".join(failures))
        return 1
    LOG.info("all symbols written to %s", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
