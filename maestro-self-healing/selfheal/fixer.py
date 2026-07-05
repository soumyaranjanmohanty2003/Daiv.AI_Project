"""Asks Groq for a fix to a failing Maestro flow (including its runFlow subflows)
and applies it safely."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import Config
from .failure_parser import FailureSummary
from .groq_client import GroqClient, GroqError
from .validator import (
    count_assertions_text,
    header_app_identity,
    validate_flow_text,
)

log = logging.getLogger("selfheal.fixer")

SYSTEM_PROMPT = """\
You are an expert in Maestro, the mobile UI testing framework that uses YAML flow files.
You are given a failing Maestro flow, any subflow files it calls via `runFlow`, plus the
error logs and debug output. Your job is to repair the FLOW FILE(S) ONLY so the test
passes, while preserving the test's intent. The failure may live in the main flow OR in
one of the subflows - pick the correct file(s) to change.

Rules:
1. Only change what is necessary to fix the failure (selectors, waits, ids, text,
   ordering, timeouts, optional flags, yaml syntax, runFlow paths).
2. NEVER change an appId or url. NEVER delete assertions to make the test pass.
3. Prefer resilient selectors: id over text, regex (.*Text.*) for partial matches,
   `optional: true` only for genuinely optional steps.
4. Add `extendedWaitUntil` / `waitForAnimationToEnd` / `retry` when the failure is
   timing-related (element not found immediately, animations, network delays).
5. If the YAML itself is malformed, fix the syntax exactly.
6. For every file you change, output its COMPLETE new content - valid Maestro YAML,
   including the header and `---` separator if the original had them.
7. Only use file paths exactly as given to you. Do not invent new files.

Respond with a single JSON object:
{
  "root_cause": "<one-sentence diagnosis>",
  "fix_description": "<what you changed, in which file, and why; 1-3 sentences>",
  "confidence": <0.0-1.0, how likely this fix makes the test pass>,
  "fixes": [
    {"path": "<exact file path as provided>", "fixed_yaml": "<entire new file content>"}
  ]
}
Change as few files as possible (usually exactly one). If the failure clearly cannot be
fixed by editing flow files (e.g. app crash, device offline, missing APK), return
"fixes": [] and explain in "root_cause".
"""


@dataclass
class FileFix:
    path: str
    fixed_yaml: str


@dataclass
class FixAttempt:
    attempt: int
    accepted: bool
    root_cause: str = ""
    fix_description: str = ""
    confidence: float = 0.0
    rejection_reason: str = ""
    changed_files: list[str] = field(default_factory=list)


@dataclass
class HealRecord:
    flow: str
    healed: bool = False
    unfixable_reason: str = ""
    attempts: list[FixAttempt] = field(default_factory=list)
    originals: dict[str, str] = field(default_factory=dict)  # path -> original text
    finals: dict[str, str] = field(default_factory=dict)      # path -> healed text


class Fixer:
    def __init__(self, cfg: Config, client: Optional[GroqClient] = None):
        self.cfg = cfg
        self.client = client or GroqClient(cfg)

    def propose_fix(
        self,
        flow_path: str,
        file_set: dict[str, str],
        failure: FailureSummary,
        previous_attempts: list[FixAttempt],
    ) -> tuple[Optional[list[FileFix]], FixAttempt]:
        """file_set maps path -> current text for the main flow and its subflows.

        Returns (list of validated FileFix or None, attempt record).
        """
        attempt_no = len(previous_attempts) + 1

        files_block_parts = []
        for path, text in file_set.items():
            role = "MAIN FLOW" if path == flow_path else "SUBFLOW (called via runFlow)"
            files_block_parts.append(f"### {role}: {path}\n```yaml\n{text}\n```")
        files_block = "\n\n".join(files_block_parts)

        history = ""
        if previous_attempts:
            lines = []
            for a in previous_attempts:
                outcome = a.rejection_reason or "applied but the test still failed"
                lines.append(f"- Attempt {a.attempt}: {a.fix_description or a.root_cause} -> {outcome}")
            history = "### Previous fix attempts (do NOT repeat these):\n" + "\n".join(lines)

        user_prompt = (
            f"{files_block}\n\n{failure.to_prompt_block()}\n\n{history}\n\n"
            "Return the JSON object now."
        )

        try:
            data = self.client.chat_json(SYSTEM_PROMPT, user_prompt)
        except GroqError as exc:
            log.error("Groq call failed: %s", exc)
            return None, FixAttempt(
                attempt=attempt_no, accepted=False, rejection_reason=f"Groq error: {exc}"
            )

        record = FixAttempt(
            attempt=attempt_no,
            accepted=False,
            root_cause=str(data.get("root_cause", "")).strip()[:500],
            fix_description=str(data.get("fix_description", "")).strip()[:500],
            confidence=_safe_float(data.get("confidence")),
        )

        fixes = self._extract_fixes(data)
        if not fixes:
            record.rejection_reason = "model reported the failure is not fixable in the flow file"
            return None, record

        if record.confidence < self.cfg.min_confidence:
            record.rejection_reason = (
                f"confidence {record.confidence:.2f} below threshold {self.cfg.min_confidence:.2f}"
            )
            return None, record

        validated = self._validate_fixes(fixes, file_set, record)
        if validated is None:
            return None, record

        record.accepted = True
        record.changed_files = [f.path for f in validated]
        return validated, record

    @staticmethod
    def _extract_fixes(data: dict) -> list[FileFix]:
        raw = data.get("fixes")
        fixes: list[FileFix] = []
        if isinstance(raw, list):
            for item in raw:
                if (
                    isinstance(item, dict)
                    and isinstance(item.get("path"), str)
                    and isinstance(item.get("fixed_yaml"), str)
                    and item["fixed_yaml"].strip()
                ):
                    fixes.append(FileFix(path=item["path"], fixed_yaml=item["fixed_yaml"]))
        # Backward compatibility: single top-level fixed_yaml (older prompt format).
        elif isinstance(data.get("fixed_yaml"), str) and data["fixed_yaml"].strip():
            fixes.append(FileFix(path="__MAIN__", fixed_yaml=data["fixed_yaml"]))
        return fixes

    def _validate_fixes(
        self, fixes: list[FileFix], file_set: dict[str, str], record: FixAttempt
    ) -> Optional[list[FileFix]]:
        """All-or-nothing validation of the proposed multi-file fix."""
        main_path = next(iter(file_set))  # first key is always the main flow
        allowed = set(file_set.keys())
        cleaned: list[FileFix] = []

        for fix in fixes:
            path = main_path if fix.path == "__MAIN__" else fix.path
            if path not in allowed:
                record.rejection_reason = (
                    f"guardrails rejected fix: model targeted unknown file {fix.path!r}"
                )
                return None

            original = file_set[path]
            if fix.fixed_yaml.strip() == original.strip():
                continue  # no-op change, drop it

            res = validate_flow_text(
                fix.fixed_yaml, allow_unknown_commands=self.cfg.allow_unknown_commands
            )
            if not res.ok:
                record.rejection_reason = f"guardrails rejected fix for {path}: {res.message()}"
                return None
            for warning in res.warnings:
                log.warning("Fix warning for %s: %s", path, warning)

            # App identity must not change in any file that declared it.
            orig_id = header_app_identity(original)
            new_id = header_app_identity(fix.fixed_yaml)
            for key, value in orig_id.items():
                if new_id.get(key) != value:
                    record.rejection_reason = (
                        f"guardrails rejected fix: {path} changed header {key!r} "
                        f"from {value!r} to {new_id.get(key)!r}"
                    )
                    return None

            cleaned.append(FileFix(path=path, fixed_yaml=fix.fixed_yaml))

        if not cleaned:
            record.rejection_reason = "guardrails rejected fix: fix is identical to the original file(s)"
            return None

        # Assertions may move between files, so compare TOTALS across the file set.
        if not self.cfg.allow_assertion_removal:
            changed = {f.path: f.fixed_yaml for f in cleaned}
            before = sum(count_assertions_text(t) for t in file_set.values())
            after = sum(
                count_assertions_text(changed.get(p, t)) for p, t in file_set.items()
            )
            if after < before:
                record.rejection_reason = (
                    f"guardrails rejected fix: assertions reduced across files "
                    f"({before} -> {after}); set ALLOW_ASSERTION_REMOVAL=true to permit"
                )
                return None

        return cleaned

    @staticmethod
    def apply_fixes(fixes: list[FileFix]) -> None:
        for fix in fixes:
            text = fix.fixed_yaml if fix.fixed_yaml.endswith("\n") else fix.fixed_yaml + "\n"
            Path(fix.path).write_text(text, encoding="utf-8")


def _safe_float(value: object) -> float:
    try:
        return max(0.0, min(1.0, float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
