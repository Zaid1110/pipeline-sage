# pipeline-sage — CI/CD Failure Triage Bot

![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white) ![License: MIT](https://img.shields.io/badge/License-MIT-green) ![Demo: no API key needed](https://img.shields.io/badge/demo-no_API_key_needed-orange)

## What this is

pipeline-sage reads a CI build log (e.g. a GitHub Actions run log), finds the
failing step and the surrounding error context, and prints a plain-English
diagnosis with a suggested fix. Two analysis modes:

- **heuristic (default)** — offline regex/pattern matching over common failure
  categories (test failures, dependency installs, Docker builds, auth issues,
  missing env vars, timeouts, lint errors, K8s deploys). Works with zero API
  keys and zero network access.
- **ai** — sends the error context to Anthropic Claude for a richer root-cause
  analysis. Needs your own `ANTHROPIC_API_KEY`.

Optional extra: `--post-comment` posts the diagnosis as a comment on a GitHub
PR via the `gh` CLI.

## What problem it solves

A red CI build dumps hundreds or thousands of log lines on the developer. The
actual error is usually buried: which step failed, what the real error was,
and what to do about it. Junior engineers (and tired senior ones at 2 AM)
lose 15–45 minutes scrolling logs for failures that fall into a handful of
well-known categories.

## Benefits

- **Faster triage:** the failing step, key error lines, likely cause and fix
  are extracted in seconds instead of manual log scrolling.
- **Works offline:** heuristic mode needs nothing but Python 3.10+ — usable on
  locked-down build agents and in air-gapped environments.
- **Actionable by default:** every diagnosis ships with a concrete next step
  (exact command to reproduce locally, where the secret lives, etc.).
- **Pluggable AI:** when you want deeper analysis, flip to `--mode ai` with
  your own Anthropic API key — no vendor lock-in, the offline mode stays.

## Before vs After

| Before | After |
|---|---|
| Build fails → open the Actions UI → scroll through 800 lines → guess which step failed | `python3 pipeline_sage.py analyze build.log` → failing step + cause + fix in seconds |
| Same class of failure (e.g. expired token) debugged from scratch every time | Pattern library recognises the category immediately and points at the known fix |
| Failure context shared by pasting raw log chunks into chat | Clean Markdown report, or auto-posted as a PR comment with `--post-comment` |

## See it in action

**The problem: a red build, hundreds of log lines**

```text
$ ./demo.sh
=== pipeline-sage demo (heuristic mode, no API key needed) ===

# pipeline-sage diagnosis (heuristic mode)

**Failing step:** `npm test`

**Category:** Test failures

**Likely cause:** One or more automated tests failed. The log shows failing assertions (expected vs received mismatches) in the test suite.

**Suggested fix:** Run the failing test file locally to reproduce, e.g. `npx jest src/routes/orders.test.js`. Fix the code under test (here every endpoint returned 500, so check the route handler / DB connection), then re-run the full suite before pushing.

**Key error lines:**

    2026-10-18T12:01:54.120Z FAIL  src/routes/orders.test.js
    2026-10-18T12:01:54.123Z     Expected: 201
    2026-10-18T12:01:54.124Z     Received: 500
    2026-10-18T12:01:54.125Z       at Object.<anonymous> (src/routes/orders.test.js:41:23)
    2026-10-18T12:01:54.128Z     Expected: 400
    2026-10-18T12:01:54.129Z     Received: 500
    2026-10-18T12:01:54.130Z       at Object.<anonymous> (src/routes/orders.test.js:58:7)
    2026-10-18T12:01:54.133Z     Expected: 404
    2026-10-18T12:01:54.134Z     Received: 500
    2026-10-18T12:01:56.400Z Test Suites: 1 failed, 1 passed, 2 total
    2026-10-18T12:01:56.401Z Tests:       3 failed, 5 passed, 8 total
    2026-10-18T12:01:56.420Z ##[error]Process completed with exit code 1.
```

## How to run the demo

```bash
chmod +x demo.sh
./demo.sh
```

Expected output: a diagnosis of the sample failing GitHub Actions log —
failing step `npm test`, category **Test failures**, with likely cause and a
suggested fix. No API key or network needed.

AI mode (needs your own key):

```bash
cp .env.example .env        # add your ANTHROPIC_API_KEY
pip install -r requirements.txt
python3 pipeline_sage.py analyze sample_data/failing-build.log --mode ai
```

Post the diagnosis to a PR (needs `gh` CLI authenticated):

```bash
python3 pipeline_sage.py analyze build.log --post-comment --repo owner/repo --pr 42
```

## Architecture (short)

```
build.log
   │  read_log()
   ▼
find_failing_step()  ── last ##[group] before ##[error]/exit-code marker
   │
   ▼
error_context()      ── ±N lines around the failure
   │
   ├──► heuristic_diagnose() ── ordered regex pattern library ──► report
   │
   └──► ai_diagnose() ── Anthropic messages API (opt-in) ──► report
```

## Roadmap

- Learn new patterns from labelled failures (small local classifier).
- Native GitHub Actions integration (run on `workflow_run`, comment on the PR).
- Multi-log diff: compare a failing run against the last green run.
- Support for GitLab CI and Jenkins log formats.

## Notes

- The sample log in `sample_data/` is synthetic, written for the demo.
- Heuristic mode never sends your logs anywhere — analysis is 100% local.
