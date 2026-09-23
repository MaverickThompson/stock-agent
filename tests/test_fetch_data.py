import importlib.util
import pathlib

import pytest


SCRIPT = pathlib.Path(__file__).parents[1] / "scripts" / "fetch_data.py"
SPEC = importlib.util.spec_from_file_location("fetch_data", SCRIPT)
fetch_data = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(fetch_data)


def test_allow_partial_continues_after_invalid_sp500_constituent(tmp_path, monkeypatch):
    monkeypatch.setattr(
        fetch_data,
        "sp500_symbols",
        lambda: {"GOOD": "GOOD", "IQVIA": "IQVIA"},
    )
    written = []

    def fake_fetch(name, ticker, out_dir, **kwargs):
        if ticker == "IQVIA":
            raise fetch_data.FetchError("IQVIA: unavailable")
        written.append(name)
        return out_dir / f"{name}.csv"

    monkeypatch.setattr(fetch_data, "fetch_symbol", fake_fetch)
    monkeypatch.setattr(fetch_data.time, "sleep", lambda _: None)

    result = fetch_data.main(["--sp500", "--allow-partial", "--out", str(tmp_path)])

    assert result == 0
    assert "GOOD" in written
    assert "IQVIA" not in written


def test_strict_mode_still_fails_when_a_requested_symbol_is_unavailable(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        fetch_data,
        "fetch_symbol",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            fetch_data.FetchError("unavailable")
        ),
    )
    monkeypatch.setattr(fetch_data.time, "sleep", lambda _: None)

    result = fetch_data.main(["--symbols", "GOOD=GOOD", "--out", str(tmp_path)])

    assert result == 1


def test_allow_partial_fails_when_every_symbol_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_data, "sp500_symbols", lambda: {"IQVIA": "IQVIA"})
    monkeypatch.setattr(
        fetch_data,
        "fetch_symbol",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            fetch_data.FetchError("unavailable")
        ),
    )
    monkeypatch.setattr(fetch_data.time, "sleep", lambda _: None)

    result = fetch_data.main(["--sp500", "--allow-partial", "--out", str(tmp_path)])

    assert result == 1
