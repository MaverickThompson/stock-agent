"""Does the code that is about to run implement the recorded study version?

PROTOCOL.md Section 5 sets a reward-to-risk floor but does not say where
Target 1 sits. That mapping was recorded on 2026-09-28 as the v1/v2 break.
Nothing, however, checked at run time that the code still implemented it --
which is why "did today run v1 or v2?" could only be answered by comparing
the spacing of two logged target prices.

This module makes the question answerable before the session acts, and makes
a mismatch loud instead of silent. Section 11 note: it adds no decision rule
and changes no threshold. It refuses to open positions under a configuration
that is not the recorded one.
"""

from __future__ import annotations

import hashlib
from typing import Any, Final

from . import study_rules as rules

#: The recorded v2 implementation mapping (PROTOCOL.md amendment 2026-09-28).
V2_TARGET_R_MULTIPLES: Final[tuple[float, ...]] = (2.0, 4.0)

#: Bump when the recorded mapping changes; every change is a Section 11 entry.
STUDY_VERSION: Final[str] = "v2"

_TOL: Final[float] = 1e-9


def _as_tuple(value: Any) -> tuple[float, ...]:
    try:
        return tuple(float(v) for v in value)
    except (TypeError, ValueError):
        return ()


def config_fingerprint(cfg: Any) -> str:
    """A short stable hash of the parameters that define the study version.

    Logged with every session so the question this module exists to answer
    can be settled with one grep rather than arithmetic on target prices.
    """
    risk = getattr(cfg, "risk", cfg)
    parts = [
        f"targets={_as_tuple(getattr(risk, 'target_r_multiples', ()))}",
        f"min_rr_cfg={getattr(risk, 'min_reward_risk', None)}",
        f"min_rr_rules={rules.MIN_REWARD_TO_RISK}",
        f"atr_stop={getattr(risk, 'atr_stop_multiple', None)}",
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:12]


def check_conformance(cfg: Any) -> str | None:
    """None when the running config is the recorded version, else why not.

    Three separate places in this codebase encode the reward-to-risk
    relationship. Each was individually valid while the study could not place
    a single trade. All three are checked here, against each other.
    """
    risk = getattr(cfg, "risk", cfg)
    targets = _as_tuple(getattr(risk, "target_r_multiples", ()))
    floor = float(rules.MIN_REWARD_TO_RISK)
    cfg_floor = getattr(risk, "min_reward_risk", None)
    problems: list[str] = []

    if targets != V2_TARGET_R_MULTIPLES:
        problems.append(
            f"target_r_multiples is {targets or '()'}, recorded mapping is "
            f"{V2_TARGET_R_MULTIPLES}")
    if not targets:
        problems.append("no take-profit targets are configured")
    elif min(targets) + _TOL < floor:
        # This is the v1 defect stated as an invariant: the gate cannot be
        # satisfied when Target 1 sits below the floor it is measured against.
        problems.append(
            f"Target 1 at {min(targets)}R is below the {floor} floor, so the "
            "Section 5 entry gate is unsatisfiable by construction")
    if cfg_floor is not None and abs(float(cfg_floor) - floor) > _TOL:
        problems.append(
            f"config min_reward_risk {cfg_floor} disagrees with "
            f"study_rules.MIN_REWARD_TO_RISK {floor}")

    if not problems:
        return None
    return (f"running configuration is not the recorded {STUDY_VERSION} mapping: "
            + "; ".join(problems))


PROVENANCE_COLUMNS: Final[tuple[str, ...]] = (
    "timestamp", "study_version", "config_fingerprint", "target_r_multiples",
    "min_reward_risk_cfg", "min_reward_risk_rules", "git_sha",
    "completed_sessions_before", "conformance",
)


def record_session_provenance(study_dir: Any, *, cfg: Any, timestamp: str,
                              completed_sessions: int = 0,
                              git_sha: str = "") -> None:
    """Append one row per session naming the rules that were about to run.

    Written to ``study/sessions.csv``. A separate file, so no existing
    Section 10 schema changes and no historical row is touched.
    """
    import csv
    import pathlib

    path = pathlib.Path(study_dir) / "sessions.csv"
    risk = getattr(cfg, "risk", cfg)
    mismatch = check_conformance(cfg)
    row = {
        "timestamp": timestamp,
        "study_version": STUDY_VERSION,
        "config_fingerprint": config_fingerprint(cfg),
        "target_r_multiples": "/".join(
            f"{v:g}" for v in _as_tuple(getattr(risk, "target_r_multiples", ()))),
        "min_reward_risk_cfg": getattr(risk, "min_reward_risk", ""),
        "min_reward_risk_rules": rules.MIN_REWARD_TO_RISK,
        "git_sha": git_sha,
        "completed_sessions_before": completed_sessions,
        "conformance": "OK" if mismatch is None else mismatch,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(PROVENANCE_COLUMNS))
        if not exists:
            writer.writeheader()
        writer.writerow(row)
