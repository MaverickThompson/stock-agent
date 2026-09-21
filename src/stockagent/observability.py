"""Sentry wiring for the study's unattended sessions.

The study runs once per market session for sixty days with nobody watching. A
session that dies quietly is worse than one that fails loudly: Section 10
requires missed sessions to be logged as SYSTEM_ERROR rows, and a crash that
nobody notices for a week produces a gap that has to be disclosed in the paper.

Sentry is therefore an integrity control, not a convenience.

Deliberately configured against Sentry's defaults:

* ``send_default_pii=False`` -- a headless batch job has no users and no
  requests; there is no PII worth shipping anywhere.
* ``traces_sample_rate=0`` -- the student plan caps transactions with
  on-demand billing disabled, so tracing every outbound call would exhaust the
  quota and start dropping the error events this exists to capture.
* Logs ARE forwarded, because the log line next to a crash is what explains it.
"""

from __future__ import annotations

import os

try:
    from .logging_setup import get_logger
except ImportError:  # pragma: no cover
    import logging

    def get_logger(name: str):
        return logging.getLogger(name)

LOG = get_logger("observability")


def init_sentry(*, environment: str = "study", release: str | None = None) -> bool:
    """Initialise Sentry if a DSN is present. Returns whether it is active.

    Absence of a DSN is not an error -- local runs and tests proceed without
    it. Absence of the SDK when a DSN IS set is worth a warning, because it
    means the alerting the operator believes is on is silently off.
    """
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        LOG.info("SENTRY_DSN not set; error reporting is off for this run")
        return False

    try:
        import sentry_sdk
    except ImportError:
        LOG.warning("SENTRY_DSN is set but sentry-sdk is not installed - "
                    "this session is running with NO error alerting")
        return False

    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        release=release,
        send_default_pii=False,
        enable_logs=True,
        traces_sample_rate=0.0,
        profiles_sample_rate=0.0,
    )
    LOG.info("sentry initialised (environment=%s)", environment)
    return True


def report(exc: BaseException, *, stage: str) -> None:
    """Send an exception to Sentry, tagged with the session stage it came from."""
    try:
        import sentry_sdk
    except ImportError:
        return
    with sentry_sdk.push_scope() as scope:
        scope.set_tag("stage", stage)
        sentry_sdk.capture_exception(exc)


def flush(timeout: float = 5.0) -> None:
    """Drain the queue before the process exits.

    Without this a crash at the end of a GitHub Actions run can terminate
    before the event is transmitted, which is precisely the case that matters.
    """
    try:
        import sentry_sdk
    except ImportError:
        return
    sentry_sdk.flush(timeout=timeout)
