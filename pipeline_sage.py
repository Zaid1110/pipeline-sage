#!/usr/bin/env python3
"""
pipeline-sage: CI/CD Failure Triage Bot.

Reads a CI build log (e.g. GitHub Actions), finds the failing step and the
error context, and prints a diagnosis with a suggested fix.

Two analysis modes:
  * heuristic (default) - offline regex/pattern matching, no API keys needed.
  * ai            - uses Anthropic Claude (needs ANTHROPIC_API_KEY in env).

Optional: --post-comment posts the diagnosis as a comment on a GitHub PR
using the `gh` CLI (needs to be authenticated).

Usage:
    python3 pipeline_sage.py analyze sample_data/failing-build.log
    python3 pipeline_sage.py analyze build.log --mode ai
    python3 pipeline_sage.py analyze build.log --post-comment --repo owner/repo --pr 42
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import textwrap

# ---------------------------------------------------------------- patterns

# Each entry: (category, compiled regex, likely_cause, suggested_fix)
PATTERNS: list[tuple[str, "re.Pattern[str]", str, str]] = [
    (
        "Test failures",
        re.compile(
            r"(Test Suites?:\s*\d+\s*failed|Tests?:\s*\d+\s*failed|\bFAIL\b|AssertionError|expect\(received\))"
        ),
        "One or more automated tests failed. The log shows failing assertions "
        "(expected vs received mismatches) in the test suite.",
        "Run the failing test file locally to reproduce, e.g. `npx jest "
        "src/routes/orders.test.js`. Fix the code under test (here every "
        "endpoint returned 500, so check the route handler / DB connection), "
        "then re-run the full suite before pushing.",
    ),
    (
        "Dependency install failure",
        re.compile(r"(npm ERR!|pip .*error|Could not resolve dependency|EACCES|ModuleNotFoundError|No matching distribution)"),
        "The dependency installation step failed - the build cannot fetch or "
        "install the packages the project needs.",
        "Check the lock file is committed (package-lock.json / poetry.lock). "
        "Look for version conflicts in the log, pin the offending dependency, "
        "and re-run the install locally with a clean cache.",
    ),
    (
        "Docker build failure",
        re.compile(r"(failed to solve|ERROR: failed to build|docker: Error response|Cannot connect to the Docker daemon)"),
        "The container image build failed - either the Dockerfile has an "
        "error or the build environment cannot reach the Docker daemon/registry.",
        "Build the image locally with `docker build .` to reproduce, read the "
        "first ERROR line (Docker stops at the first failing layer), and fix "
        "that layer.",
    ),
    (
        "Authentication / permissions",
        re.compile(r"(denied:|authentication required|Invalid credentials|Permission denied \(publickey\)|403 Forbidden|unauthorized)"),
        "The pipeline failed on credentials - a token, SSH key or registry "
        "login is missing, expired or lacks permission.",
        "Check the secret exists in the repo/org settings, verify it has not "
        "expired, and confirm the token scopes allow the operation being attempted.",
    ),
    (
        "Missing secret / env var",
        re.compile(r"(environment variable .* not set|secret .* not found|is not defined|\bundefined\b.*env)"),
        "The job references a secret or environment variable that does not "
        "exist in this environment.",
        "Add the missing variable/secret to the workflow env or repository "
        "secrets, and give the failing step a default or a clear error "
        "message when it is absent.",
    ),
    (
        "Timeout / cancelled",
        re.compile(r"(timed out|The operation was canceled|exceeded the maximum execution time|context deadline exceeded)"),
        "The job ran longer than the allowed time (or a previous step timed "
        "out and cancelled the run).",
        "Split the job into smaller steps, cache dependencies, run tests in "
        "parallel, or raise the timeout for that specific step.",
    ),
    (
        "Lint / syntax error",
        re.compile(r"(SyntaxError|eslint|flake8|ruff|mypy|tsc .*error)"),
        "A linter or compiler rejected the code - style rule or syntax "
        "violation.",
        "Run the same linter locally (`npm run lint`, `ruff check .`) and "
        "fix the reported violations before pushing.",
    ),
    (
        "Kubernetes deploy failure",
        re.compile(r"(kubectl.*error|ImagePullBackOff|CrashLoopBackOff|deployment .* failed)"),
        "The deployment step failed - usually an image that cannot be "
        "pulled or a container that crashes on startup.",
        "Check `kubectl describe pod` and `kubectl logs` for the failing "
        "pod; verify the image tag exists in the registry.",
    ),
]

GROUP_RE = re.compile(r"##\[group\]Run\s+(.*)")
ERROR_MARKERS = (
    "##[error]",
    "Process completed with exit code",
    "exit status 1",
    "BUILD FAILURE",
    "BUILD FAILED",
)


# --------------------------------------------------------------- analysis


def read_log(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.readlines()


def find_failing_step(lines: list[str]) -> tuple[str, int]:
    """Return (step name, line index of the first error marker)."""
    error_idx = None
    for i, line in enumerate(lines):
        if any(m in line for m in ERROR_MARKERS):
            error_idx = i
            break
    if error_idx is None:
        # Fall back: last line that looks like a failure.
        for i in range(len(lines) - 1, -1, -1):
            if re.search(r"\b(failed|FAILED|error|Error)\b", lines[i]):
                error_idx = i
                break
    if error_idx is None:
        return ("(unknown - no failure marker found)", len(lines) - 1)

    step = "(unknown step)"
    for i in range(error_idx, -1, -1):
        m = GROUP_RE.search(lines[i])
        if m:
            step = m.group(1).strip()
            break
    return step, error_idx


def error_context(lines: list[str], error_idx: int, before: int = 25, after: int = 3) -> list[str]:
    start = max(0, error_idx - before)
    end = min(len(lines), error_idx + after + 1)
    return lines[start:end]


def heuristic_diagnose(context_lines: list[str]) -> dict:
    blob = "".join(context_lines)
    for category, pattern, cause, fix in PATTERNS:
        if pattern.search(blob):
            snippet = _key_lines(context_lines)
            return {
                "mode": "heuristic",
                "category": category,
                "likely_cause": cause,
                "suggested_fix": fix,
                "key_lines": snippet,
            }
    return {
        "mode": "heuristic",
        "category": "Unknown failure",
        "likely_cause": "No known failure pattern matched the error context.",
        "suggested_fix": "Paste the 'Key error lines' below into a search "
        "engine or re-run the failing step locally with verbose logging.",
        "key_lines": _key_lines(context_lines),
    }


def _key_lines(context_lines: list[str], limit: int = 12) -> list[str]:
    """Pick the most informative lines: failures/errors first, then tail."""
    scored: list[tuple[int, str]] = []
    for line in context_lines:
        text = line.strip()
        if not text:
            continue
        score = 0
        if re.search(r"##\[error\]|AssertionError|Expected:|Received:|npm ERR!|SyntaxError|denied", text):
            score = 3
        elif re.search(r"\bFAIL\b|\bfailed\b|\berror\b|\bError\b", text, re.IGNORECASE):
            score = 2
        elif "at " in text and "(" in text:  # stack trace line
            score = 1
        scored.append((score, text))
    scored.sort(key=lambda t: -t[0])
    picked = [t for _, t in scored[:limit]]
    # Keep original order for readability.
    order = {t: i for i, t in enumerate(text.strip() for text in context_lines if text.strip())}
    picked.sort(key=lambda t: order.get(t, 0))
    return picked


def ai_diagnose(context_text: str, step: str) -> dict:
    """Richer analysis via Anthropic Claude. Needs ANTHROPIC_API_KEY."""
    try:
        import anthropic
    except ImportError:
        return {"error": "The 'anthropic' package is not installed. Run: pip install -r requirements.txt"}

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {"error": "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key."}

    client = anthropic.Anthropic(api_key=api_key)
    prompt = (
        "You are a senior DevOps engineer triaging a failed CI build.\n"
        f"Failing step: {step}\n\n"
        "Log excerpt:\n```\n" + context_text[:6000] + "\n```\n\n"
        "Reply in Markdown with: 1) What failed, 2) Most likely root cause, "
        "3) Concrete fix (commands where useful), 4) How to prevent recurrence."
    )
    message = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(block.text for block in message.content if getattr(block, "type", "") == "text")
    return {"mode": "ai", "analysis": text}


def format_report(step: str, diagnosis: dict) -> str:
    if "error" in diagnosis:
        return f"# pipeline-sage diagnosis\n\nERROR: {diagnosis['error']}\n"
    if diagnosis.get("mode") == "ai":
        return (
            "# pipeline-sage diagnosis (AI mode)\n\n"
            f"**Failing step:** `{step}`\n\n"
            f"{diagnosis['analysis']}\n"
        )
    key_lines = "\n".join(f"    {line}" for line in diagnosis["key_lines"])
    return (
        "# pipeline-sage diagnosis (heuristic mode)\n\n"
        f"**Failing step:** `{step}`\n\n"
        f"**Category:** {diagnosis['category']}\n\n"
        f"**Likely cause:** {diagnosis['likely_cause']}\n\n"
        f"**Suggested fix:** {diagnosis['suggested_fix']}\n\n"
        "**Key error lines:**\n\n"
        f"{key_lines}\n"
    )


def post_pr_comment(report: str, repo: str, pr: int) -> None:
    body = report + "\n\n---\n*Diagnosed by pipeline-sage (heuristic mode).*"
    result = subprocess.run(
        ["gh", "pr", "comment", str(pr), "--repo", repo, "--body", body],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"gh CLI failed: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(2)
    print(f"Posted diagnosis as a comment on PR #{pr} in {repo}.")


# ------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CI/CD failure triage bot.")
    parser.add_argument("command", choices=["analyze"], help="Action to run.")
    parser.add_argument("logfile", help="Path to the CI build log file.")
    parser.add_argument(
        "--mode",
        choices=["heuristic", "ai"],
        default="heuristic",
        help="Analysis mode (default: heuristic, works offline).",
    )
    parser.add_argument(
        "--json", action="store_true", help="Print the diagnosis as JSON."
    )
    parser.add_argument(
        "--post-comment",
        action="store_true",
        help="Post the diagnosis as a PR comment via the gh CLI.",
    )
    parser.add_argument("--repo", help="GitHub repo (owner/name) for --post-comment.")
    parser.add_argument("--pr", type=int, help="PR number for --post-comment.")
    args = parser.parse_args(argv)

    if not os.path.isfile(args.logfile):
        print(f"Log file not found: {args.logfile}", file=sys.stderr)
        return 1

    lines = read_log(args.logfile)
    step, error_idx = find_failing_step(lines)
    context = error_context(lines, error_idx)

    if args.mode == "ai":
        diagnosis = ai_diagnose("".join(context), step)
    else:
        diagnosis = heuristic_diagnose(context)

    if args.json:
        print(json.dumps({"failing_step": step, "diagnosis": diagnosis}, indent=2))
    else:
        print(format_report(step, diagnosis))

    if args.post_comment:
        if not args.repo or not args.pr:
            print("--post-comment needs --repo and --pr.", file=sys.stderr)
            return 1
        post_pr_comment(format_report(step, diagnosis), args.repo, args.pr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
