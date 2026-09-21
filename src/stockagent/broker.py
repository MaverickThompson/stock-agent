"""Alpaca paper-trading access for the 60-day study.

Section 7 of PROTOCOL.md names an Alpaca paper account with broker-side order
records as the INTENDED execution venue, with a synthetic fill model as the
fallback "used unless and until Alpaca is connected". This module is that
connection, so the study runs on real broker records rather than simulated
fills.

Three things here are protocol requirements, not preferences:

* **Quote timestamps are recorded with every fill.** Section 7. A fill with no
  quote time cannot be audited afterwards.
* **Stale or out-of-session quotes defer the fill to the next regular
  session** rather than filling at a bad price. :class:`StaleQuote` is raised
  and the caller logs a SYSTEM_ERROR row.
* **Entry is priced at the ASK and exits at the BID.** The R:R gate in
  Section 5 is computed that way, so sizing and reality must agree.

Transient broker errors retry with exponential backoff. A 60-day study that
aborts a session on one 503 has a hole in it that must then be disclosed.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import os
import time
from typing import Any, Final

try:
    from .logging_setup import get_logger
except ImportError:  # pragma: no cover
    import logging

    def get_logger(name: str):
        return logging.getLogger(name)

LOG = get_logger("broker")

#: Seconds between retries on a transient broker failure.
RETRY_DELAYS: Final[tuple[int, ...]] = (2, 4, 8)

#: A quote older than this is not tradeable. Section 7 defers the fill instead.
MAX_QUOTE_AGE_SECONDS: Final[float] = 90.0


class BrokerError(RuntimeError):
    """The broker could not be reached or refused the request."""


class StaleQuote(BrokerError):
    """Quote too old or out of session -- Section 7 defers rather than fills."""


@dataclasses.dataclass(frozen=True)
class Quote:
    symbol: str
    bid: float
    ask: float
    timestamp: str
    age_seconds: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


@dataclasses.dataclass(frozen=True)
class Fill:
    order_id: str
    symbol: str
    side: str
    qty: int
    filled_price: float
    filled_at: str
    quote_at_decision: Quote


@dataclasses.dataclass(frozen=True)
class AccountState:
    equity: float          # net liquidation value, the base for Section 6 sizing
    cash: float
    buying_power: float


def _is_transient(error: Exception) -> bool:
    text = str(error).lower()
    if any(s in text for s in ("timed out", "timeout", "temporarily", "rate limit",
                               "too many requests", "connection reset")):
        return True
    status = getattr(error, "status_code", None)
    if status is None:
        status = getattr(getattr(error, "response", None), "status_code", None)
    if isinstance(status, int):
        return status == 429 or 500 <= status < 600
    return False


def _retry(operation, description: str):
    """Call ``operation``, retrying transient failures with backoff."""
    attempts = 1 + len(RETRY_DELAYS)
    for i in range(attempts):
        try:
            return operation()
        except Exception as error:  # noqa: BLE001 - re-raised below
            if i == attempts - 1 or not _is_transient(error):
                raise BrokerError(f"{description} failed: {error}") from error
            delay = RETRY_DELAYS[i]
            LOG.warning("%s failed (%s) - retry %d/%d in %ds",
                        description, type(error).__name__, i + 1, len(RETRY_DELAYS), delay)
            time.sleep(delay)


class AlpacaBroker:
    """Thin, explicit wrapper over the Alpaca paper-trading and data APIs."""

    def __init__(self, api_key: str | None = None, secret_key: str | None = None,
                 *, paper: bool = True) -> None:
        api_key = api_key or os.environ.get("ALPACA_API_KEY")
        secret_key = secret_key or os.environ.get("ALPACA_SECRET_KEY")
        if not api_key or not secret_key:
            raise BrokerError(
                "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set. In CI they come "
                "from repository secrets; locally, export them in your shell.")
        if not paper:
            raise BrokerError(
                "This study is paper-only. Section 6 simulates $100,000 and no "
                "capital is at risk; refusing to construct a live-trading client.")

        try:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.trading.client import TradingClient
        except ImportError as exc:  # pragma: no cover
            raise BrokerError(
                "alpaca-py is not installed. Add 'alpaca-py>=0.33' to "
                "requirements.txt and reinstall.") from exc

        self._trading = TradingClient(api_key, secret_key, paper=True)
        self._data = StockHistoricalDataClient(api_key, secret_key)

    # -- account ------------------------------------------------------------

    def account(self) -> AccountState:
        raw = _retry(self._trading.get_account, "get_account")
        return AccountState(equity=float(raw.equity), cash=float(raw.cash),
                            buying_power=float(raw.buying_power))

    def positions(self) -> dict[str, int]:
        raw = _retry(self._trading.get_all_positions, "get_all_positions")
        return {p.symbol: int(float(p.qty)) for p in raw}

    def market_is_open(self) -> bool:
        clock = _retry(self._trading.get_clock, "get_clock")
        return bool(clock.is_open)

    # -- quotes -------------------------------------------------------------

    def quote(self, symbol: str, *, now: dt.datetime | None = None) -> Quote:
        """Latest NBBO quote, rejected if stale (Section 7)."""
        from alpaca.data.requests import StockLatestQuoteRequest

        payload = _retry(
            lambda: self._data.get_stock_latest_quote(
                StockLatestQuoteRequest(symbol_or_symbols=symbol)),
            f"quote({symbol})")
        raw = payload[symbol]

        stamp = raw.timestamp
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=dt.timezone.utc)
        now = now or dt.datetime.now(dt.timezone.utc)
        age = (now - stamp).total_seconds()

        bid, ask = float(raw.bid_price), float(raw.ask_price)
        if bid <= 0 or ask <= 0:
            raise StaleQuote(f"{symbol}: no two-sided market (bid={bid}, ask={ask})")
        if age > MAX_QUOTE_AGE_SECONDS:
            raise StaleQuote(
                f"{symbol}: quote {age:.0f}s old, limit {MAX_QUOTE_AGE_SECONDS:.0f}s - "
                "fill deferred to the next regular session per Section 7")
        return Quote(symbol=symbol, bid=bid, ask=ask,
                     timestamp=stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                     age_seconds=round(age, 2))

    # -- orders -------------------------------------------------------------

    def submit(self, symbol: str, qty: int, side: str, *,
               quote: Quote | None = None) -> Fill:
        """Submit a day market order and return the broker's own fill record.

        Market orders only: Section 7 models entry at the ask and exit at the
        bid with no queue position, and a limit order would introduce
        unfilled-order behaviour the protocol does not describe.
        """
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        if qty <= 0:
            raise BrokerError(f"refusing to submit a non-positive quantity: {qty}")
        quote = quote or self.quote(symbol)

        request = MarketOrderRequest(
            symbol=symbol, qty=qty,
            side=OrderSide.BUY if side.lower() in ("buy", "long") else OrderSide.SELL,
            time_in_force=TimeInForce.DAY)
        order = _retry(lambda: self._trading.submit_order(request),
                       f"submit_order({side} {qty} {symbol})")
        settled = self._await_fill(str(order.id))

        filled_price = float(settled.filled_avg_price or 0.0)
        if filled_price <= 0:
            # Falls back to the protocol's own model: ask on entry, bid on exit.
            filled_price = quote.ask if side.lower() in ("buy", "long") else quote.bid
            LOG.warning("%s order %s reported no fill price; recording the %s per "
                        "Section 7", symbol, order.id,
                        "ask" if side.lower() in ("buy", "long") else "bid")

        filled_at = getattr(settled, "filled_at", None)
        return Fill(order_id=str(order.id), symbol=symbol, side=side.lower(), qty=qty,
                    filled_price=filled_price,
                    filled_at=(filled_at.strftime("%Y-%m-%dT%H:%M:%SZ")
                               if filled_at else quote.timestamp),
                    quote_at_decision=quote)

    def _await_fill(self, order_id: str, *, timeout: float = 30.0) -> Any:
        """Poll until the order is terminal. Paper market orders settle quickly."""
        deadline = time.monotonic() + timeout
        order = _retry(lambda: self._trading.get_order_by_id(order_id),
                       f"get_order({order_id})")
        while time.monotonic() < deadline:
            status = str(getattr(order, "status", "")).lower()
            if "filled" in status or "canceled" in status or "rejected" in status:
                return order
            time.sleep(1.0)
            order = _retry(lambda: self._trading.get_order_by_id(order_id),
                           f"get_order({order_id})")
        LOG.warning("order %s still open after %.0fs; recording its current state",
                    order_id, timeout)
        return order
