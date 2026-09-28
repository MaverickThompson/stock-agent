import datetime as dt
import json

import pytest

from stockagent.study_state import StudyState, V2_SESSION_TARGET, V2_START_DATE


def test_initial_state_is_ineligible_before_start_and_eligible_on_start():
    state = StudyState(
        schema_version=1,
        start_date=dt.date(2026, 9, 29),
        completed_sessions=0,
        last_session_date=None,
        status="active",
    )

    assert V2_START_DATE == dt.date(2026, 9, 29)
    assert V2_SESSION_TARGET == 60
    assert not state.is_eligible(dt.date(2026, 9, 28))
    assert state.is_eligible(dt.date(2026, 9, 29))


def test_successful_session_increments_count_and_records_date():
    state = StudyState(
        schema_version=1,
        start_date=dt.date(2026, 9, 29),
        completed_sessions=0,
        last_session_date=None,
        status="active",
    )

    updated = state.record_success(dt.date(2026, 9, 29))

    assert updated.completed_sessions == 1
    assert updated.last_session_date == dt.date(2026, 9, 29)
    assert updated.status == "active"
    assert state.completed_sessions == 0


def test_recording_the_same_date_twice_is_idempotent():
    state = StudyState(
        schema_version=1,
        start_date=dt.date(2026, 9, 29),
        completed_sessions=1,
        last_session_date=dt.date(2026, 9, 29),
        status="active",
    )

    assert state.record_success(dt.date(2026, 9, 29)) == state


def test_sixtieth_successful_session_completes_study():
    state = StudyState(
        schema_version=1,
        start_date=dt.date(2026, 9, 29),
        completed_sessions=59,
        last_session_date=dt.date(2026, 12, 14),
        status="active",
    )

    completed = state.record_success(dt.date(2026, 12, 15))

    assert completed.completed_sessions == 60
    assert completed.last_session_date == dt.date(2026, 12, 15)
    assert completed.status == "complete"
    assert not completed.is_eligible(dt.date(2026, 12, 16))
    assert completed.record_success(dt.date(2026, 12, 16)) == completed


@pytest.mark.parametrize(
    "document",
    [
        "{not json",
        json.dumps({
            "schema_version": 2,
            "start_date": "2026-09-29",
            "completed_sessions": 0,
            "last_session_date": None,
            "status": "active",
        }),
        json.dumps({
            "schema_version": 1,
            "start_date": "2026-09-29",
            "completed_sessions": 61,
            "last_session_date": "2026-09-29",
            "status": "complete",
        }),
        json.dumps({
            "schema_version": 1,
            "start_date": "2026-09-30",
            "completed_sessions": 0,
            "last_session_date": None,
            "status": "active",
        }),
        json.dumps({
            "schema_version": 1,
            "start_date": "2026-09-29",
            "completed_sessions": 2,
            "last_session_date": "2026-09-29",
            "status": "active",
        }),
        json.dumps({
            "schema_version": 1,
            "start_date": "2026-09-29",
            "completed_sessions": 60,
            "last_session_date": "2026-09-29",
            "status": "complete",
        }),
        json.dumps({
            "schema_version": 1,
            "start_date": "2026-09-29",
            "completed_sessions": 0,
            "last_session_date": "2026-09-29",
            "status": "active",
        }),
        json.dumps({
            "schema_version": 1,
            "start_date": "09/29/2026",
            "completed_sessions": 0,
            "last_session_date": None,
            "status": "active",
        }),
    ],
)
def test_load_rejects_invalid_json_or_state_schema(tmp_path, document):
    path = tmp_path / "state.json"
    path.write_text(document, encoding="utf-8")

    with pytest.raises((ValueError, TypeError)):
        StudyState.load(path)


def test_save_atomically_round_trips_state(tmp_path):
    path = tmp_path / "nested" / "state.json"
    state = StudyState(
        schema_version=1,
        start_date=dt.date(2026, 9, 29),
        completed_sessions=1,
        last_session_date=dt.date(2026, 9, 29),
        status="active",
    )

    state.save(path)

    assert StudyState.load(path) == state
    assert set(path.parent.iterdir()) == {path}


def test_initial_state_file_has_zero_progress():
    state = StudyState.load("study/v2_state.json")

    assert state.start_date == dt.date(2026, 9, 29)
    assert state.completed_sessions == 0
    assert state.last_session_date is None
    assert state.status == "active"
