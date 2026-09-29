"""Alpaca order submission tests using SDK and client fakes."""

import datetime as dt
import importlib.util
import pathlib
import sys
import types

import pytest

BROKER_PATH = pathlib.Path(__file__).parent.parent / "src" / "stockagent" / "broker.py"
SPEC = importlib.util.spec_from_file_location("broker_under_test", BROKER_PATH)
broker = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = broker
SPEC.loader.exec_module(broker)


class FakeMarketOrderRequest:
    def __init__(self, **values):
        self.__dict__.update(values)


def install_sdk_fakes(monkeypatch):
    alpaca = types.ModuleType("alpaca")
    alpaca.__path__ = []
    trading = types.ModuleType("alpaca.trading")
    trading.__path__ = []
    enums = types.ModuleType("alpaca.trading.enums")
    enums.OrderSide = types.SimpleNamespace(BUY="buy", SELL="sell")
    enums.TimeInForce = types.SimpleNamespace(DAY="day")
    requests = types.ModuleType("alpaca.trading.requests")
    requests.MarketOrderRequest = FakeMarketOrderRequest

    for name, module in (
        ("alpaca", alpaca),
        ("alpaca.trading", trading),
        ("alpaca.trading.enums", enums),
        ("alpaca.trading.requests", requests),
    ):
        monkeypatch.setitem(sys.modules, name, module)


def quote():
    return broker.Quote(
        symbol="AAPL", bid=100.9, ask=101.0,
        timestamp="2026-09-29T14:31:00Z", age_seconds=1.0)


def settled_order():
    return types.SimpleNamespace(
        id="alpaca-order-123", status="filled", filled_qty="1",
        filled_avg_price="101.25",
        filled_at=dt.datetime(2026, 9, 29, 14, 31, tzinfo=dt.timezone.utc))


class FakeTradingClient:
    def __init__(self, *, submit=None, existing=None):
        self._submit = submit or (lambda request: settled_order())
        self.existing = existing or settled_order()
        self.requests = []
        self.lookups = []
        self.order_id_lookups = []

    def submit_order(self, request):
        self.requests.append(request)
        return self._submit(request)

    def get_order_by_client_id(self, client_order_id):
        self.lookups.append(client_order_id)
        return self.existing

    def get_order_by_id(self, order_id):
        self.order_id_lookups.append(order_id)
        return self.existing


def make_broker(client):
    instance = object.__new__(broker.AlpacaBroker)
    instance._trading = client
    return instance


def test_transient_retry_reuses_the_same_client_order_id(monkeypatch):
    install_sdk_fakes(monkeypatch)
    attempts = 0

    def submit(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("request timed out")
        return settled_order()

    monkeypatch.setattr(broker.time, "sleep", lambda _seconds: None)
    client = FakeTradingClient(submit=submit)
    fill = make_broker(client).submit(
        "AAPL", 1, "buy", client_order_id="v2-20260929-example", quote=quote())

    assert fill.order_id == "alpaca-order-123"
    assert len(client.requests) == 2
    assert [request.client_order_id for request in client.requests] == [
        "v2-20260929-example", "v2-20260929-example"]


def test_duplicate_client_order_id_looks_up_and_reconciles_existing_order(monkeypatch):
    install_sdk_fakes(monkeypatch)

    class DuplicateOrderError(Exception):
        status_code = 422

    duplicate_order = settled_order()
    normal_client = FakeTradingClient()
    normal_fill = make_broker(normal_client).submit(
        "AAPL", 1, "buy", client_order_id="v2-20260929-entry", quote=quote())

    def reject_duplicate(_request):
        raise DuplicateOrderError("client_order_id must be unique")

    duplicate_client = FakeTradingClient(
        submit=reject_duplicate, existing=duplicate_order)
    recovered_fill = make_broker(duplicate_client).submit(
        "AAPL", 1, "buy", client_order_id="v2-20260929-entry", quote=quote())

    assert duplicate_client.lookups == ["v2-20260929-entry"]
    assert len(duplicate_client.requests) == 1
    assert duplicate_client.order_id_lookups == ["alpaca-order-123"]
    assert recovered_fill == normal_fill


@pytest.mark.parametrize("status", ["canceled", "rejected"])
def test_duplicate_unfilled_order_does_not_create_a_fill(monkeypatch, status):
    install_sdk_fakes(monkeypatch)

    class DuplicateOrderError(Exception):
        status_code = 422

    unfilled_order = types.SimpleNamespace(
        id="alpaca-order-123", status=status, filled_qty="0",
        filled_avg_price=None, filled_at=None)
    client = FakeTradingClient(
        submit=lambda _request: (_ for _ in ()).throw(
            DuplicateOrderError("client_order_id must be unique")),
        existing=unfilled_order)

    with pytest.raises(broker.BrokerError, match="zero filled shares"):
        make_broker(client).submit(
            "AAPL", 10, "buy", client_order_id="v2-20260929-entry", quote=quote())

    assert client.lookups == ["v2-20260929-entry"]


def test_canceled_order_uses_actual_partial_filled_quantity(monkeypatch):
    install_sdk_fakes(monkeypatch)
    partial = types.SimpleNamespace(
        id="alpaca-order-123", status="canceled", filled_qty="3",
        filled_avg_price="101.25",
        filled_at=dt.datetime(2026, 9, 29, 14, 31, tzinfo=dt.timezone.utc))
    client = FakeTradingClient(existing=partial)
    fill = make_broker(client).submit(
        "AAPL", 10, "buy", client_order_id="v2-20260929-entry", quote=quote())

    assert fill.qty == 3


def test_pending_order_at_timeout_does_not_create_a_fill(monkeypatch):
    install_sdk_fakes(monkeypatch)
    clock = [0.0]
    pending = types.SimpleNamespace(
        id="alpaca-order-123", status="partially_filled", filled_qty="2",
        filled_avg_price="101.25", filled_at=None)
    client = FakeTradingClient(submit=lambda _request: pending, existing=pending)
    monkeypatch.setattr(broker.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(broker.time, "sleep", lambda _seconds: clock.__setitem__(0, 31.0))

    with pytest.raises(broker.BrokerError, match="still pending"):
        make_broker(client).submit(
            "AAPL", 10, "buy", client_order_id="v2-20260929-entry", quote=quote())


def test_exhausted_ambiguous_submission_looks_up_accepted_order(monkeypatch):
    install_sdk_fakes(monkeypatch)
    monkeypatch.setattr(broker.time, "sleep", lambda _seconds: None)
    client = FakeTradingClient(
        submit=lambda _request: (_ for _ in ()).throw(TimeoutError("request timed out")))

    fill = make_broker(client).submit(
        "AAPL", 1, "buy", client_order_id="v2-20260929-entry", quote=quote())

    assert fill.order_id == "alpaca-order-123"
    assert len(client.requests) == 1 + len(broker.RETRY_DELAYS)
    assert client.lookups == ["v2-20260929-entry"]


def test_absent_ambiguous_submission_reraises_submission_failure(monkeypatch):
    install_sdk_fakes(monkeypatch)
    monkeypatch.setattr(broker.time, "sleep", lambda _seconds: None)

    class MissingOrderError(Exception):
        status_code = 404

    client = FakeTradingClient(
        submit=lambda _request: (_ for _ in ()).throw(TimeoutError("request timed out")))
    client.get_order_by_client_id = lambda client_order_id: (
        client.lookups.append(client_order_id) or
        (_ for _ in ()).throw(MissingOrderError("not found")))

    with pytest.raises(broker.BrokerError, match="submit_order"):
        make_broker(client).submit(
            "AAPL", 1, "buy", client_order_id="v2-20260929-entry", quote=quote())

    assert client.lookups == ["v2-20260929-entry"]
