"""Validates Maestro YAML and enforces safety guardrails on LLM-proposed fixes.

A fix is only accepted when:
  * the YAML parses (Maestro's `header --- commands` multi-doc format),
  * every command is a known Maestro command (unless allow_unknown_commands),
  * appId / url in the header was not changed,
  * assertions were not silently removed (unless allow_assertion_removal).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import yaml

KNOWN_COMMANDS = {
    "addMedia", "assertVisible", "assertNotVisible", "assertTrue", "assertNoDefectsWithAI",
    "assertWithAI", "back", "clearKeychain", "clearState", "copyTextFrom", "doubleTapOn",
    "eraseText", "evalScript", "extendedWaitUntil", "extractTextWithAI", "hideKeyboard",
    "inputText", "inputRandomEmail", "inputRandomPersonName", "inputRandomNumber",
    "inputRandomText", "killApp", "launchApp", "longPressOn", "openLink", "pasteText",
    "pressKey", "repeat", "retry", "runFlow", "runScript", "scroll", "scrollUntilVisible",
    "setAirplaneMode", "setLocation", "setOrientation", "startRecording", "stopApp",
    "stopRecording", "swipe", "takeScreenshot", "tapOn", "toggleAirplaneMode", "travel",
    "waitForAnimationToEnd", "waitUntilVisible", "waitUntilNotVisible",
}

ASSERTION_COMMANDS = {
    "assertVisible", "assertNotVisible", "assertTrue", "assertWithAI", "assertNoDefectsWithAI",
    "extendedWaitUntil",
}


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def message(self) -> str:
        return "; ".join(self.errors + self.warnings)


def parse_flow(text: str) -> tuple[Optional[dict], Optional[list]]:
    """Parse Maestro flow into (header, commands). Either part may be missing."""
    docs = list(yaml.safe_load_all(text))
    docs = [d for d in docs if d is not None]
    header: Optional[dict] = None
    commands: Optional[list] = None
    for doc in docs:
        if isinstance(doc, dict) and commands is None and header is None:
            header = doc
        elif isinstance(doc, list):
            commands = doc
    return header, commands


def _command_names(commands: list) -> list[str]:
    names: list[str] = []
    for step in commands or []:
        if isinstance(step, str):
            names.append(step.split(":")[0].strip())
        elif isinstance(step, dict) and step:
            names.append(next(iter(step.keys())))
    return names


def _count_assertions(commands: list) -> int:
    return sum(1 for n in _command_names(commands) if n in ASSERTION_COMMANDS)


def count_assertions_text(text: str) -> int:
    """Total assertion commands in a flow file; 0 if unparseable."""
    try:
        _, cmds = parse_flow(text)
    except yaml.YAMLError:
        return 0
    return _count_assertions(cmds or [])


def header_app_identity(text: str) -> dict:
    """Return {appId, url} from the header (only keys that exist)."""
    try:
        header, _ = parse_flow(text)
    except yaml.YAMLError:
        return {}
    out = {}
    for key in ("appId", "url"):
        if isinstance(header, dict) and key in header:
            out[key] = header[key]
    return out


def validate_flow_text(text: str, allow_unknown_commands: bool = False) -> ValidationResult:
    res = ValidationResult(ok=True)
    if not text or not text.strip():
        return ValidationResult(ok=False, errors=["fixed YAML is empty"])
    try:
        header, commands = parse_flow(text)
    except yaml.YAMLError as exc:
        return ValidationResult(ok=False, errors=[f"YAML parse error: {exc}"])

    if commands is None:
        return ValidationResult(ok=False, errors=["no command list found in flow"])
    if not commands:
        return ValidationResult(ok=False, errors=["command list is empty"])

    for name in _command_names(commands):
        if name not in KNOWN_COMMANDS:
            msg = f"unknown Maestro command: {name!r}"
            if allow_unknown_commands:
                res.warnings.append(msg)
            else:
                res.ok = False
                res.errors.append(msg)

    if header is not None and not isinstance(header, dict):
        res.ok = False
        res.errors.append("flow header is not a mapping")
    return res


def validate_fix(
    original_text: str,
    fixed_text: str,
    allow_assertion_removal: bool = False,
    allow_unknown_commands: bool = False,
) -> ValidationResult:
    res = validate_flow_text(fixed_text, allow_unknown_commands=allow_unknown_commands)
    if not res.ok:
        return res

    if fixed_text.strip() == original_text.strip():
        res.ok = False
        res.errors.append("fix is identical to the original file")
        return res

    try:
        orig_header, orig_cmds = parse_flow(original_text)
        new_header, new_cmds = parse_flow(fixed_text)
    except yaml.YAMLError:
        # Original may itself be broken YAML - that's fine, the fix repairs it.
        return res

    # Guardrail: identity of the app under test must not change.
    for key in ("appId", "url"):
        o = (orig_header or {}).get(key)
        n = (new_header or {}).get(key)
        if o is not None and n != o:
            res.ok = False
            res.errors.append(f"fix changed header {key!r} from {o!r} to {n!r}")

    # Guardrail: don't let the model "fix" a test by deleting its assertions.
    if not allow_assertion_removal and orig_cmds and new_cmds is not None:
        before, after = _count_assertions(orig_cmds), _count_assertions(new_cmds)
        if after < before:
            res.ok = False
            res.errors.append(
                f"fix removed assertions ({before} -> {after}); "
                "set ALLOW_ASSERTION_REMOVAL=true to permit"
            )

    # Guardrail: flag suspiciously large rewrites.
    if orig_cmds and new_cmds:
        ratio = len(new_cmds) / max(len(orig_cmds), 1)
        if ratio > 3 or ratio < 0.4:
            res.warnings.append(f"large structural change: {len(orig_cmds)} -> {len(new_cmds)} steps")
    return res
