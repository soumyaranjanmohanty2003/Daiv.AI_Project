"""Extracts a concise, LLM-friendly failure summary from Maestro output."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .runner import RunResult

# Patterns that usually pinpoint the root cause in Maestro logs.
_INTERESTING = re.compile(
    r"(FAILED|Failed|Exception|Assertion is false|not found|No element|Timeout|timed out|"
    r"Unable to|Invalid|Error|Element matching|could not|Cannot|CRASH|ANR)",
    re.IGNORECASE,
)

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


@dataclass
class FailureSummary:
    flow: str
    error_lines: str
    log_tail: str
    debug_excerpt: str
    timed_out: bool

    def to_prompt_block(self) -> str:
        parts = [f"### Failed flow: {self.flow}"]
        if self.timed_out:
            parts.append("NOTE: the run TIMED OUT.")
        if self.error_lines:
            parts.append(f"### Key error lines:\n{self.error_lines}")
        parts.append(f"### Maestro output (tail):\n{self.log_tail}")
        if self.debug_excerpt:
            parts.append(f"### Debug artifacts excerpt:\n{self.debug_excerpt}")
        return "\n\n".join(parts)


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def parse_failure(result: RunResult, tail_lines: int = 120) -> FailureSummary:
    output = strip_ansi(result.output or "")
    lines = [ln.rstrip() for ln in output.splitlines() if ln.strip()]

    error_lines = [ln for ln in lines if _INTERESTING.search(ln)]
    # Dedupe while preserving order; cap for prompt budget.
    seen: set[str] = set()
    unique_errors: list[str] = []
    for ln in error_lines:
        key = ln.strip()
        if key not in seen:
            seen.add(key)
            unique_errors.append(ln)
    unique_errors = unique_errors[:40]

    tail = "\n".join(lines[-tail_lines:])

    return FailureSummary(
        flow=result.flow,
        error_lines="\n".join(unique_errors),
        log_tail=tail[-6000:],
        debug_excerpt=strip_ansi(result.extra_logs or "")[-6000:],
        timed_out=result.timed_out,
    )
