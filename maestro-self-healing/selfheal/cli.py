"""CLI orchestrator: run flows -> heal failures with Groq -> re-run -> report.

Exit codes:
  0 = all flows pass (originally or after healing)
  1 = at least one flow still fails after healing
  2 = configuration / environment error
"""

from __future__ import annotations

import argparse
import glob
import logging
import os
import sys
from pathlib import Path

from .config import Config
from .failure_parser import parse_failure
from .fixer import Fixer, HealRecord
from .report import build_report, write_report
from .runner import MaestroRunner
from .subflows import resolve_subflows

log = logging.getLogger("selfheal")


def discover_flows(patterns: list[str]) -> list[str]:
    flows: list[str] = []
    for pattern in patterns:
        p = Path(pattern)
        if p.is_dir():
            flows.extend(str(f) for f in sorted(p.rglob("*.yaml")))
            flows.extend(str(f) for f in sorted(p.rglob("*.yml")))
        else:
            flows.extend(sorted(glob.glob(pattern, recursive=True)))
    seen: set[str] = set()
    result = []
    for f in flows:
        rf = str(Path(f))
        if rf not in seen and Path(rf).is_file():
            seen.add(rf)
            result.append(rf)
    return result


def set_github_output(**kwargs: str) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    with open(out, "a", encoding="utf-8") as fh:
        for key, value in kwargs.items():
            if "\n" in value:
                fh.write(f"{key}<<SELFHEAL_EOF\n{value}\nSELFHEAL_EOF\n")
            else:
                fh.write(f"{key}={value}\n")


def heal_flow(cfg: Config, runner: MaestroRunner, fixer: Fixer, flow: str, first_result) -> HealRecord:
    record = HealRecord(flow=flow)

    # Snapshot the main flow AND every subflow it calls via runFlow -
    # the fix may need to land in any of them.
    file_set: dict[str, str] = {}
    involved = [flow] + resolve_subflows(flow)
    for path in involved:
        try:
            file_set[path] = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.warning("Could not read %s: %s", path, exc)
    record.originals = dict(file_set)
    if len(involved) > 1:
        log.info("[%s] includes %d subflow(s): %s", flow, len(involved) - 1, involved[1:])

    failure = parse_failure(first_result, cfg.log_tail_lines)

    for _ in range(cfg.max_heal_attempts):
        fixes, attempt = fixer.propose_fix(flow, file_set, failure, record.attempts)
        record.attempts.append(attempt)

        if fixes is None:
            log.info("[%s] attempt %d not applied: %s", flow, attempt.attempt, attempt.rejection_reason)
            if "not fixable" in attempt.rejection_reason:
                record.unfixable_reason = attempt.root_cause or attempt.rejection_reason
                break
            continue

        if cfg.dry_run:
            log.info("[%s] DRY RUN - fix proposed but not applied", flow)
            record.finals = {f.path: f.fixed_yaml for f in fixes}
            record.unfixable_reason = "dry-run: fix proposed, not applied"
            break

        fixer.apply_fixes(fixes)
        log.info(
            "[%s] applied fix to %s (attempt %d), re-running...",
            flow, [f.path for f in fixes], attempt.attempt,
        )
        rerun = runner.run_flow(flow, tag=f"heal{attempt.attempt}")

        if rerun.passed:
            record.healed = True
            record.finals = {f.path: f.fixed_yaml for f in fixes}
            log.info("[%s] HEALED after %d attempt(s)", flow, attempt.attempt)
            return record

        log.info("[%s] fix did not pass re-run (exit %d)", flow, rerun.exit_code)
        failure = parse_failure(rerun, cfg.log_tail_lines)
        for path in file_set:
            try:
                file_set[path] = Path(path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass

    if not record.healed and cfg.restore_on_failure and not cfg.dry_run:
        for path, text in record.originals.items():
            Path(path).write_text(text, encoding="utf-8")
        log.info("[%s] restored original file(s) after failed healing", flow)
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="selfheal",
        description="Self-healing runner for Maestro YAML flows (Groq-powered).",
    )
    parser.add_argument("flows", nargs="+", help="Flow files, globs, or directories")
    parser.add_argument("--dry-run", action="store_true", help="Propose fixes without applying them")
    parser.add_argument("--report", default=None, help="Path for the Markdown report")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )

    cfg = Config()
    if args.dry_run:
        cfg.dry_run = True
    if args.report:
        cfg.report_path = args.report
    cfg.validate()

    flows = discover_flows(args.flows)
    if not flows:
        log.error("No flow files matched: %s", args.flows)
        return 2
    log.info("Discovered %d flow(s)", len(flows))

    runner = MaestroRunner(cfg)
    fixer = Fixer(cfg)

    passed_first_try: list[str] = []
    records: list[HealRecord] = []
    healed_budget = cfg.max_flows_to_heal

    for flow in flows:
        result = runner.run_flow(flow, tag="initial")
        if result.passed:
            log.info("[%s] PASSED (%.1fs)", flow, result.duration_s)
            passed_first_try.append(flow)
            continue

        log.warning("[%s] FAILED (exit %d) - starting self-heal", flow, result.exit_code)
        if healed_budget <= 0:
            rec = HealRecord(flow=flow, unfixable_reason="max_flows_to_heal budget exhausted")
            records.append(rec)
            continue
        healed_budget -= 1
        records.append(heal_flow(cfg, runner, fixer, flow, result))

    healed = [r.flow for r in records if r.healed]
    changed_files = sorted({p for r in records if r.healed for p in r.finals})
    still_failing = [r.flow for r in records if not r.healed]

    report = build_report(records, passed_first_try)
    write_report(report, cfg.report_path)
    log.info("Report written to %s", cfg.report_path)

    set_github_output(
        any_healed="true" if healed else "false",
        all_passed="true" if not still_failing else "false",
        healed_files="\n".join(healed),
        changed_files="\n".join(changed_files),
        failing_files="\n".join(still_failing),
        report_path=cfg.report_path,
    )

    if still_failing:
        log.error("Flows still failing after healing: %s", ", ".join(still_failing))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
