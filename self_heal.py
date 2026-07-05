"""
Self-healing system for Maestro flow failures.

Runs after "maestro test flows" has already failed. For each failed flow it:
  1. Re-runs that single flow in isolation to reproduce the failure and grab
     a fresh UI hierarchy dump from the connected device.
  2. Asks Groq (Llama 3.3 70B) for a corrected flow YAML.
  3. Writes the proposal to a NEW branch via a git worktree (main working
     copy is never touched) and verifies it there with "maestro test".
  4. Opens a PR from that branch with a healing-report.md summary.

Nothing here ever edits, stages, or commits a file inside this checkout.
All writes happen inside an isolated `git worktree` tied to a fresh branch.

NOTE on log parsing: Maestro's plain-text CLI output format is not
guaranteed stable across versions. FAIL_MARKERS below is a best-effort
heuristic to find which flows failed in reports/maestro-output.log. If your
Maestro version's output doesn't match, tune that regex -- the rest of the
pipeline (isolated re-run -> hierarchy -> Groq -> branch -> PR) does not
depend on the log format, since it re-derives the real error by re-running
each suspected-failed flow on its own.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    import yaml
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False

REPO_ROOT = Path(__file__).resolve().parent
FLOWS_DIR = REPO_ROOT / "flows"
LOG_PATH = REPO_ROOT / "reports" / "maestro-output.log"
MAESTRO_TESTS_DIR = Path.home() / ".maestro" / "tests"
HEALING_REPORT_NAME = "healing-report.md"

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"
MAX_RETRIES = 3
INITIAL_BACKOFF_SECONDS = 5
MAX_HIERARCHY_CHARS = 12000
MAX_ERROR_CHARS = 4000

FAIL_MARKERS = re.compile(r"(❌|FAIL(?:ED)?\b|Failed\b)", re.IGNORECASE)


@dataclass
class FlowFailure:
    name: str
    relative_path: Path


@dataclass
class HealResult:
    failure: FlowFailure
    status: str  # "verified" | "needs_review" | "skipped_flaky" | "generation_failed"
    original_yaml: str = ""
    fixed_yaml: Optional[str] = None
    error_text: str = ""
    test_output: str = ""


def run(cmd, cwd=None, env=None, check=True, timeout=None):
    result = subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, env=env, shell=True,
        capture_output=True, text=True, timeout=timeout,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed: {cmd}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def discover_flows() -> dict:
    mapping = {}
    for path in FLOWS_DIR.rglob("*.yaml"):
        if path.name == "config.yaml":
            continue
        mapping[path.stem] = path.relative_to(REPO_ROOT)
    return mapping


def find_failed_flow_names(flow_map: dict) -> list:
    """Best-effort scan of the combined log to see which flows failed."""
    if not LOG_PATH.exists():
        print(f"No log found at {LOG_PATH}; nothing to parse.")
        return []

    lines = LOG_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()
    failed = []
    for stem, relpath in flow_map.items():
        posix = str(relpath).replace("\\", "/")
        hit_indices = [
            i for i, line in enumerate(lines)
            if stem in line or relpath.name in line or posix in line
        ]
        if not hit_indices:
            continue
        for idx in hit_indices:
            window = "\n".join(lines[max(0, idx - 2): idx + 15])
            if FAIL_MARKERS.search(window):
                failed.append(FlowFailure(name=stem, relative_path=relpath))
                break
    return failed


def truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    head = max_chars * 2 // 3
    tail = max_chars - head
    return text[:head] + "\n... [TRUNCATED] ...\n" + text[-tail:]


def capture_hierarchy() -> str:
    """Dump the current on-device UI hierarchy (adb first, maestro hierarchy fallback)."""
    device_xml = "/sdcard/window_dump.xml"
    dump = run(["adb", "shell", "uiautomator", "dump", device_xml], check=False)
    if dump.returncode == 0:
        with tempfile.TemporaryDirectory() as tmp:
            local_path = Path(tmp) / "window_dump.xml"
            pulled = run(["adb", "pull", device_xml, str(local_path)], check=False)
            if pulled.returncode == 0 and local_path.exists():
                return truncate(local_path.read_text(encoding="utf-8", errors="ignore"), MAX_HIERARCHY_CHARS)

    hierarchy = run(["maestro", "hierarchy"], check=False)
    if hierarchy.returncode == 0 and hierarchy.stdout.strip():
        return truncate(hierarchy.stdout, MAX_HIERARCHY_CHARS)

    return "(unable to capture UI hierarchy from device)"


def reproduce_failure(flow_path: Path) -> tuple:
    """Re-run a single flow in isolation so the failure + on-screen state are fresh."""
    result = run(["maestro", "test", str(flow_path)], cwd=REPO_ROOT, check=False, timeout=180)
    if result.returncode == 0:
        return None, None  # flaky: passed this time
    error_text = truncate((result.stdout + "\n" + result.stderr).strip(), MAX_ERROR_CHARS)
    hierarchy_text = capture_hierarchy()
    return error_text, hierarchy_text


def strip_code_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines)
    return t.strip() + "\n"


def is_valid_yaml(text: str) -> bool:
    if not HAVE_YAML:
        return True
    try:
        list(yaml.safe_load_all(text))
        return True
    except Exception:
        return False


def call_groq(api_key: str, flow_yaml: str, error_text: str, hierarchy_text: str) -> Optional[str]:
    system = (
        "You are an expert at fixing Maestro mobile UI test flows for Android. "
        "You will be given a failing Maestro flow YAML, the error it produced, and the "
        "actual current UI hierarchy dump from the device. Respond with ONLY the full "
        "corrected YAML content for the flow file - no explanations, no markdown code "
        "fences, no extra commentary. Prefer minimal, targeted changes: fix selector "
        "text to match what's actually on screen, add `optional: true` where an element "
        "isn't always present, add `extendedWaitUntil` / `waitForAnimationToEnd` for "
        "timing issues, correct element ids, or insert a step to dismiss an unexpected "
        "popup if the hierarchy shows one. Keep the rest of the flow unchanged."
    )
    user = (
        f"Failing flow YAML:\n---\n{flow_yaml}\n---\n\n"
        f"Error from Maestro:\n---\n{error_text}\n---\n\n"
        f"Actual UI hierarchy currently on screen (may be truncated):\n---\n{hierarchy_text}\n---\n"
    )
    payload = json.dumps({
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
    }).encode("utf-8")

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    delay = INITIAL_BACKOFF_SECONDS

    for attempt in range(1, MAX_RETRIES + 1):
        req = urllib.request.Request(GROQ_URL, data=payload, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return strip_code_fences(body["choices"][0]["message"]["content"])
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < MAX_RETRIES:
                wait = delay
                retry_after = e.headers.get("Retry-After") if e.headers else None
                if retry_after:
                    try:
                        wait = float(retry_after)
                    except ValueError:
                        pass
                print(f"Groq rate limited (429). Retrying in {wait:.0f}s (attempt {attempt}/{MAX_RETRIES})...")
                time.sleep(wait)
                delay *= 3
                continue
            print(f"Groq API error {e.code}: {e.read().decode('utf-8', 'ignore')}")
            return None
        except urllib.error.URLError as e:
            print(f"Groq API request failed: {e}")
            return None
    return None


def create_healing_branch_worktree(branch_name: str, base_sha: str) -> Path:
    run(["git", "branch", branch_name, base_sha], cwd=REPO_ROOT)
    worktree_path = Path(tempfile.mkdtemp(prefix="maestro-self-heal-"))
    shutil.rmtree(worktree_path)  # git worktree add requires the dir not to exist
    run(["git", "worktree", "add", str(worktree_path), branch_name], cwd=REPO_ROOT)
    return worktree_path


def verify_fix(worktree_path: Path, relative_path: Path) -> tuple:
    result = run(["maestro", "test", str(worktree_path / relative_path)],
                 cwd=worktree_path, check=False, timeout=180)
    output = truncate((result.stdout + "\n" + result.stderr).strip(), MAX_ERROR_CHARS)
    return result.returncode == 0, output


def unified_diff(before: str, after: str, label: str) -> str:
    import difflib
    diff = difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"a/{label}", tofile=f"b/{label}",
    )
    return "".join(diff) or "(no textual diff)"


def write_healing_report(worktree_path: Path, branch_name: str, base_sha: str, results: list) -> Path:
    verified = sum(1 for r in results if r.status == "verified")
    needs_review = sum(1 for r in results if r.status == "needs_review")
    skipped = sum(1 for r in results if r.status == "skipped_flaky")
    failed_gen = sum(1 for r in results if r.status == "generation_failed")

    STATUS_LABEL = {
        "verified": "✅ Fix verified passing",
        "needs_review": "⚠️ Needs human review (fix proposed but did not pass verification)",
        "skipped_flaky": "⏭️ Skipped - flow passed on isolated re-run (flaky, not healed)",
        "generation_failed": "❌ Could not generate a fix (Groq call failed)",
    }

    lines = [
        f"# Self-Heal Report",
        "",
        f"Generated: {datetime.utcnow().isoformat()}Z",
        f"Branch: `{branch_name}`",
        f"Base commit: `{base_sha}`",
        "",
        "## Summary",
        f"- {len(results)} failed flow(s) analyzed",
        f"- {verified} fix(es) verified passing",
        f"- {needs_review} fix(es) need human review",
        f"- {skipped} flow(s) skipped (flaky on re-run)",
        f"- {failed_gen} flow(s) had no fix generated",
        "",
    ]

    for r in results:
        rel = str(r.failure.relative_path).replace("\\", "/")
        lines.append(f"## {rel}")
        lines.append(f"**Status:** {STATUS_LABEL[r.status]}")
        lines.append("")
        if r.error_text:
            lines.append("**Root cause (Maestro error output):**")
            lines.append("```")
            lines.append(r.error_text)
            lines.append("```")
            lines.append("")
        if r.status in ("verified", "needs_review"):
            lines.append("**Verification output (`maestro test` on healing branch):**")
            lines.append("```")
            lines.append(r.test_output)
            lines.append("```")
            lines.append("")
            lines.append("**Proposed diff:**")
            lines.append("```diff")
            lines.append(unified_diff(r.original_yaml, r.fixed_yaml or "", rel))
            lines.append("```")
            lines.append("")

    report_path = worktree_path / HEALING_REPORT_NAME
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def commit_and_push(worktree_path: Path, branch_name: str, changed_files: list):
    env = dict(os.environ)
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "Maestro Self-Heal Bot"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "self-heal-bot@users.noreply.github.com"

    run(["git", "add", HEALING_REPORT_NAME, *changed_files], cwd=worktree_path, env=env)
    run(["git", "commit", "-m", f"Self-heal: propose fixes for {len(changed_files)} flow(s)"],
        cwd=worktree_path, env=env)
    run(["git", "push", "-u", "origin", branch_name], cwd=worktree_path, env=env)


def create_pull_request(worktree_path: Path, branch_name: str, results: list):
    if shutil.which("gh") is None:
        print("WARNING: GitHub CLI ('gh') not found on PATH; skipping PR creation. "
              f"The branch '{branch_name}' has been pushed - open the PR manually.")
        return
    title = f"Self-heal: {len(results)} flow fix{'es' if len(results) != 1 else ''} proposed"
    run([
        "gh", "pr", "create",
        "--title", title,
        "--body-file", HEALING_REPORT_NAME,
        "--base", "main",
        "--head", branch_name,
    ], cwd=worktree_path)


def cleanup_worktree(worktree_path: Path):
    try:
        run(["git", "worktree", "remove", "--force", str(worktree_path)], cwd=REPO_ROOT, check=False)
    except Exception as e:
        print(f"Warning: failed to clean up worktree {worktree_path}: {e}")


def main() -> int:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        print("ERROR: GROQ_API_KEY environment variable is not set.")
        return 1

    flow_map = discover_flows()
    failures = find_failed_flow_names(flow_map)
    if not failures:
        print("No failed flows detected in reports/maestro-output.log. Nothing to heal.")
        return 0

    print(f"Detected {len(failures)} failed flow(s): {[f.name for f in failures]}")

    results = []
    for failure in failures:
        print(f"\n--- Reproducing failure for {failure.relative_path} ---")
        error_text, hierarchy_text = reproduce_failure(REPO_ROOT / failure.relative_path)
        if error_text is None:
            print(f"{failure.name} passed on isolated re-run; treating as flaky, skipping.")
            results.append(HealResult(failure=failure, status="skipped_flaky"))
            continue

        original_yaml = (REPO_ROOT / failure.relative_path).read_text(encoding="utf-8")
        print(f"Asking Groq for a fix for {failure.name}...")
        fixed_yaml = call_groq(api_key, original_yaml, error_text, hierarchy_text)

        if not fixed_yaml:
            results.append(HealResult(
                failure=failure, status="generation_failed", error_text=error_text,
            ))
            continue

        results.append(HealResult(
            failure=failure, status="pending", original_yaml=original_yaml,
            fixed_yaml=fixed_yaml, error_text=error_text,
        ))

    to_heal = [r for r in results if r.status == "pending"]
    if not to_heal:
        print("No fixes were generated; nothing to branch or open a PR for.")
        return 0

    run_id = os.environ.get("GITHUB_RUN_ID", datetime.utcnow().strftime("%Y%m%d%H%M%S"))
    branch_name = f"self-heal/{run_id}"
    base_sha = run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT).stdout.strip()

    print(f"\nCreating healing branch '{branch_name}' from {base_sha}...")
    worktree_path = create_healing_branch_worktree(branch_name, base_sha)

    try:
        changed_files = []
        for r in to_heal:
            target = worktree_path / r.failure.relative_path
            if is_valid_yaml(r.fixed_yaml):
                target.write_text(r.fixed_yaml, encoding="utf-8")
                passed, test_output = verify_fix(worktree_path, r.failure.relative_path)
                r.status = "verified" if passed else "needs_review"
                r.test_output = test_output
            else:
                target.write_text(r.fixed_yaml, encoding="utf-8")
                r.status = "needs_review"
                r.test_output = "Skipped verification: Groq response was not valid YAML."
            changed_files.append(str(r.failure.relative_path).replace("\\", "/"))

        write_healing_report(worktree_path, branch_name, base_sha, results)
        commit_and_push(worktree_path, branch_name, changed_files)
        create_pull_request(worktree_path, branch_name, to_heal)
    finally:
        cleanup_worktree(worktree_path)

    verified = sum(1 for r in to_heal if r.status == "verified")
    print(f"\nDone. {verified}/{len(to_heal)} proposed fixes verified passing. "
          f"See PR on branch '{branch_name}' (main branch untouched).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
