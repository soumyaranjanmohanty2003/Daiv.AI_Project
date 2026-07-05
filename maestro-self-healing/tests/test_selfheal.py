"""Unit tests: validator guardrails, failure parsing, subflow resolution,
fixer with mocked Groq (single-file and subflow fixes)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from selfheal.config import Config
from selfheal.failure_parser import parse_failure
from selfheal.fixer import Fixer
from selfheal.runner import RunResult
from selfheal.subflows import extract_runflow_refs, resolve_subflows
from selfheal.validator import validate_fix, validate_flow_text

ORIGINAL = """\
appId: com.example.app
---
- launchApp
- tapOn: "Sign In"
- inputText: "user@example.com"
- assertVisible: "Welcome"
"""

FIXED_OK = """\
appId: com.example.app
---
- launchApp
- extendedWaitUntil:
    visible: "Log In"
    timeout: 10000
- tapOn: "Log In"
- inputText: "user@example.com"
- assertVisible: "Welcome"
"""

PARENT_WITH_SUBFLOW = """\
appId: com.example.app
---
- launchApp
- runFlow: subflows/login.yaml
- assertVisible: "Dashboard"
"""

SUBFLOW = """\
- tapOn: "Sign In"
- inputText: "user@example.com"
- assertVisible: "Welcome"
"""

SUBFLOW_FIXED = """\
- extendedWaitUntil:
    visible: "Log In"
    timeout: 10000
- tapOn: "Log In"
- inputText: "user@example.com"
- assertVisible: "Welcome"
"""


# ---------- validator ----------

def test_valid_flow_passes():
    assert validate_flow_text(ORIGINAL).ok


def test_subflow_without_header_passes():
    assert validate_flow_text(SUBFLOW).ok


def test_broken_yaml_rejected():
    assert not validate_flow_text("appId: x\n---\n- tapOn: [unclosed").ok


def test_empty_rejected():
    assert not validate_flow_text("").ok


def test_unknown_command_rejected():
    bad = 'appId: x\n---\n- clickMagically: "foo"\n- assertVisible: "x"\n'
    assert not validate_flow_text(bad).ok
    assert validate_flow_text(bad, allow_unknown_commands=True).ok


def test_good_fix_accepted():
    assert validate_fix(ORIGINAL, FIXED_OK).ok


def test_identical_fix_rejected():
    res = validate_fix(ORIGINAL, ORIGINAL)
    assert not res.ok and "identical" in res.message()


def test_appid_change_rejected():
    evil = FIXED_OK.replace("com.example.app", "com.evil.app")
    res = validate_fix(ORIGINAL, evil)
    assert not res.ok and "appId" in res.message()


def test_assertion_removal_rejected():
    stripped = "appId: com.example.app\n---\n- launchApp\n- tapOn: \"Log In\"\n"
    res = validate_fix(ORIGINAL, stripped)
    assert not res.ok and "assertions" in res.message()
    assert validate_fix(ORIGINAL, stripped, allow_assertion_removal=True).ok


# ---------- subflow resolution ----------

def test_extract_runflow_refs_all_syntaxes():
    text = """\
appId: com.x
---
- launchApp
- runFlow: a.yaml
- runFlow:
    file: b.yaml
    env:
      USER: x
- repeat:
    times: 2
    commands:
      - runFlow: c.yaml
"""
    assert extract_runflow_refs(text) == ["a.yaml", "b.yaml", "c.yaml"]


def test_resolve_subflows_transitive(tmp_path):
    (tmp_path / "subflows").mkdir()
    main = tmp_path / "main.yaml"
    main.write_text("appId: com.x\n---\n- runFlow: subflows/a.yaml\n- assertVisible: 'x'\n")
    (tmp_path / "subflows" / "a.yaml").write_text("- runFlow: b.yaml\n- tapOn: 'y'\n")
    (tmp_path / "subflows" / "b.yaml").write_text("- tapOn: 'z'\n")
    resolved = resolve_subflows(str(main))
    names = [Path(p).name for p in resolved]
    assert names == ["a.yaml", "b.yaml"]


def test_resolve_subflows_missing_file_skipped(tmp_path):
    main = tmp_path / "main.yaml"
    main.write_text("appId: com.x\n---\n- runFlow: nope.yaml\n")
    assert resolve_subflows(str(main)) == []


# ---------- failure parser ----------

def test_parse_failure_extracts_errors():
    result = RunResult(
        flow="login.yaml",
        passed=False,
        exit_code=1,
        output="Running...\n\x1b[31mAssertion is false: id=welcome not found\x1b[0m\nFAILED\n",
        duration_s=4.2,
    )
    summary = parse_failure(result)
    assert "Assertion is false" in summary.error_lines
    assert "\x1b" not in summary.error_lines  # ANSI stripped
    block = summary.to_prompt_block()
    assert "login.yaml" in block and "FAILED" in block


# ---------- fixer with mocked Groq ----------

class MockClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def chat_json(self, system_prompt, user_prompt):
        self.calls.append(user_prompt)
        return self.response


def _failure():
    return parse_failure(RunResult(
        flow="login.yaml", passed=False, exit_code=1,
        output="Element matching 'Sign In' not found\nFAILED", duration_s=1.0,
    ))


def _cfg():
    return Config(groq_api_key="test", dry_run=False)


def _response(fixes, confidence=0.9):
    return {"root_cause": "Button renamed", "fix_description": "Updated selector",
            "confidence": confidence, "fixes": fixes}


def test_fixer_accepts_valid_fix():
    client = MockClient(_response([{"path": "login.yaml", "fixed_yaml": FIXED_OK}]))
    fixes, attempt = Fixer(_cfg(), client=client).propose_fix(
        "login.yaml", {"login.yaml": ORIGINAL}, _failure(), [])
    assert fixes and fixes[0].fixed_yaml == FIXED_OK and attempt.accepted
    assert attempt.changed_files == ["login.yaml"]


def test_fixer_accepts_subflow_fix():
    file_set = {"main.yaml": PARENT_WITH_SUBFLOW, "subflows/login.yaml": SUBFLOW}
    client = MockClient(_response([{"path": "subflows/login.yaml", "fixed_yaml": SUBFLOW_FIXED}]))
    fixes, attempt = Fixer(_cfg(), client=client).propose_fix(
        "main.yaml", file_set, _failure(), [])
    assert fixes and fixes[0].path == "subflows/login.yaml" and attempt.accepted
    # Prompt included both files, labelled correctly
    assert "MAIN FLOW: main.yaml" in client.calls[0]
    assert "SUBFLOW" in client.calls[0]


def test_fixer_rejects_unknown_target_file():
    client = MockClient(_response([{"path": "../../etc/evil.yaml", "fixed_yaml": FIXED_OK}]))
    fixes, attempt = Fixer(_cfg(), client=client).propose_fix(
        "login.yaml", {"login.yaml": ORIGINAL}, _failure(), [])
    assert fixes is None and "unknown file" in attempt.rejection_reason


def test_fixer_rejects_cross_file_assertion_removal():
    # Model deletes the subflow's assertion -> totals drop -> reject.
    file_set = {"main.yaml": PARENT_WITH_SUBFLOW, "subflows/login.yaml": SUBFLOW}
    stripped = "- tapOn: \"Log In\"\n- inputText: \"user@example.com\"\n"
    client = MockClient(_response([{"path": "subflows/login.yaml", "fixed_yaml": stripped}]))
    fixes, attempt = Fixer(_cfg(), client=client).propose_fix(
        "main.yaml", file_set, _failure(), [])
    assert fixes is None and "assertions" in attempt.rejection_reason


def test_fixer_rejects_low_confidence():
    client = MockClient(_response([{"path": "login.yaml", "fixed_yaml": FIXED_OK}], confidence=0.2))
    fixes, attempt = Fixer(_cfg(), client=client).propose_fix(
        "login.yaml", {"login.yaml": ORIGINAL}, _failure(), [])
    assert fixes is None and "confidence" in attempt.rejection_reason


def test_fixer_rejects_appid_change():
    evil = FIXED_OK.replace("com.example.app", "com.evil.app")
    client = MockClient(_response([{"path": "login.yaml", "fixed_yaml": evil}]))
    fixes, attempt = Fixer(_cfg(), client=client).propose_fix(
        "login.yaml", {"login.yaml": ORIGINAL}, _failure(), [])
    assert fixes is None and "appId" in attempt.rejection_reason


def test_fixer_handles_unfixable():
    client = MockClient({"root_cause": "App crashes on startup", "fix_description": "",
                         "confidence": 0.9, "fixes": []})
    fixes, attempt = Fixer(_cfg(), client=client).propose_fix(
        "login.yaml", {"login.yaml": ORIGINAL}, _failure(), [])
    assert fixes is None and "not fixable" in attempt.rejection_reason


def test_fixer_backward_compat_single_fixed_yaml():
    client = MockClient({"root_cause": "x", "fix_description": "y",
                         "confidence": 0.9, "fixed_yaml": FIXED_OK})
    fixes, attempt = Fixer(_cfg(), client=client).propose_fix(
        "login.yaml", {"login.yaml": ORIGINAL}, _failure(), [])
    assert fixes and fixes[0].path == "login.yaml" and attempt.accepted


def test_fixer_includes_history_in_prompt():
    from selfheal.fixer import FixAttempt
    client = MockClient(_response([{"path": "login.yaml", "fixed_yaml": FIXED_OK}]))
    prev = [FixAttempt(attempt=1, accepted=True, fix_description="tried regex selector")]
    Fixer(_cfg(), client=client).propose_fix(
        "login.yaml", {"login.yaml": ORIGINAL}, _failure(), prev)
    assert "tried regex selector" in client.calls[0]


def test_apply_fixes_writes_files(tmp_path):
    from selfheal.fixer import FileFix
    f = tmp_path / "login.yaml"
    f.write_text(ORIGINAL)
    Fixer.apply_fixes([FileFix(path=str(f), fixed_yaml=FIXED_OK.rstrip("\n"))])
    assert f.read_text().endswith("\n") and "Log In" in f.read_text()


# ---------- report ----------

def test_report_builds_with_multifile_diffs():
    from selfheal.fixer import FixAttempt, HealRecord
    from selfheal.report import build_report
    rec = HealRecord(flow="main.yaml", healed=True)
    rec.originals = {"main.yaml": PARENT_WITH_SUBFLOW, "subflows/login.yaml": SUBFLOW}
    rec.finals = {"subflows/login.yaml": SUBFLOW_FIXED}
    rec.attempts.append(FixAttempt(attempt=1, accepted=True, root_cause="renamed button",
                                   fix_description="updated selector in subflow",
                                   confidence=0.9, changed_files=["subflows/login.yaml"]))
    md = build_report([rec], passed_first_try=["home.yaml"])
    assert "Auto-healed" in md and "subflows/login.yaml" in md and "```diff" in md
