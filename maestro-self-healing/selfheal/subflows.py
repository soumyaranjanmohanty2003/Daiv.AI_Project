"""Resolves `runFlow` references so subflows can be healed too.

Handles all Maestro runFlow syntaxes:
  - runFlow: subflows/login.yaml
  - runFlow:
      file: subflows/login.yaml
      env: { USER: x }
  - nested commands inside repeat/retry/runFlow blocks

Paths are resolved relative to the referencing file. Missing files are skipped.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from .validator import parse_flow

log = logging.getLogger("selfheal.subflows")

MAX_DEPTH = 4


def _walk_commands(commands: list, refs: list[str]) -> None:
    for step in commands or []:
        if not isinstance(step, dict) or not step:
            continue
        name, value = next(iter(step.items()))
        if name == "runFlow":
            if isinstance(value, str):
                refs.append(value)
            elif isinstance(value, dict):
                if isinstance(value.get("file"), str):
                    refs.append(value["file"])
                if isinstance(value.get("commands"), list):
                    _walk_commands(value["commands"], refs)
        elif isinstance(value, dict) and isinstance(value.get("commands"), list):
            # repeat / retry blocks with nested commands
            _walk_commands(value["commands"], refs)


def extract_runflow_refs(flow_text: str) -> list[str]:
    try:
        _, commands = parse_flow(flow_text)
    except yaml.YAMLError:
        return []
    refs: list[str] = []
    _walk_commands(commands or [], refs)
    return refs


def resolve_subflows(flow_path: str, max_depth: int = MAX_DEPTH) -> list[str]:
    """Return all existing subflow files referenced (transitively) by flow_path."""
    resolved: list[str] = []
    seen: set[str] = {str(Path(flow_path).resolve())}
    queue: list[tuple[str, int]] = [(flow_path, 0)]

    while queue:
        current, depth = queue.pop(0)
        if depth >= max_depth:
            continue
        try:
            text = Path(current).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        base = Path(current).parent
        for ref in extract_runflow_refs(text):
            candidate = (base / ref).resolve()
            key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            if candidate.is_file():
                # Keep a path relative to cwd when possible (nicer in prompts/PRs).
                try:
                    display = str(candidate.relative_to(Path.cwd()))
                except ValueError:
                    display = str(candidate)
                resolved.append(display)
                queue.append((display, depth + 1))
            else:
                log.warning("runFlow reference not found: %s (from %s)", ref, current)
    return resolved
