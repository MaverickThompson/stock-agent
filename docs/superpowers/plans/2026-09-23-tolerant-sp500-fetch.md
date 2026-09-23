# Tolerant S&P 500 Fetch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent one invalid or delisted S&P 500 constituent from aborting the entire scheduled study session.

**Architecture:** Keep the existing per-symbol fetch and failure logging. Add an explicit tolerant mode for broad S&P 500 scans, where individual constituent failures are reported but do not make the command fail; default/core-symbol fetches remain strict. The workflow opts into tolerant S&P 500 mode.

**Tech Stack:** Python 3.10+, argparse, requests, pytest, GitHub Actions.

## Global Constraints

- Core/default symbols must still cause a non-zero exit when they fail.
- S&P 500 constituent failures must remain visible in logs.
- No changes to strategy, signal evaluation, or study-log schemas.
- Tests must cover both tolerant and strict failure behavior.

---

### Task 1: Add regression tests for fetch failure policy

**Files:**
- Create: `tests/test_fetch_data.py`

**Interfaces:**
- Consumes: `scripts/fetch_data.py::main`
- Produces: assertions for `main(["--sp500", ...])` and strict `main(["--symbols", ...])`

- [ ] **Step 1: Write failing tests** for a broad scan continuing after one `FetchError`, and a strict core-symbol scan returning failure.
- [ ] **Step 2: Run `python -m pytest tests/test_fetch_data.py -q` and confirm the tolerant test fails because the flag/behavior is absent.
- [ ] **Step 3: Keep test fixtures isolated with monkeypatches for symbol discovery, fetch, and sleep.**

### Task 2: Implement tolerant broad-scan behavior

**Files:**
- Modify: `scripts/fetch_data.py:main`
- Modify: `.github/workflows/study-session.yml:Fetch market data`

**Interfaces:**
- Consumes: `--sp500` and existing failure collection.
- Produces: `--allow-partial` CLI option; broad scans continue after individual failures and return success when at least one symbol succeeds.

- [ ] **Step 1: Add `--allow-partial` with help text explaining that individual failures are logged and tolerated.**
- [ ] **Step 2: Track successful writes separately from failures.**
- [ ] **Step 3: Return non-zero for strict mode failures, and for tolerant mode only when no symbol succeeds.**
- [ ] **Step 4: Update the workflow command to `python scripts/fetch_data.py --sp500 --skip-existing --allow-partial`.**
- [ ] **Step 5: Run the focused tests and confirm they pass.**

### Task 3: Validate the complete change

**Files:**
- No additional files.

- [ ] **Step 1: Run `python -m pytest tests/test_fetch_data.py -q`.**
- [ ] **Step 2: Run the full suite with `python -m pytest tests/ -q`.**
- [ ] **Step 3: Inspect the diff and confirm only fetch policy, workflow wiring, tests, and this plan changed.**
