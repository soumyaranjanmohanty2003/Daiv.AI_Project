"""Runs Maestro flows and captures results."""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config

log = logging.getLogger("selfheal.runner")


@dataclass
class RunResult:
    flow: str
    passed: bool
    exit_code: int
    output: str
    duration_s: float
    debug_dir: str = ""
    timed_out: bool = False
    extra_logs: str = field(default="", repr=False)


class MaestroRunner:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def run_flow(self, flow_path: str, tag: str = "") -> RunResult:
        debug_dir = Path(self.cfg.debug_output_dir) / (
            Path(flow_path).stem + (f"-{tag}" if tag else "")
        )
        debug_dir.mkdir(parents=True, exist_ok=True)

        cmd = [self.cfg.maestro_bin, "test", flow_path, "--debug-output", str(debug_dir)]
        if self.cfg.maestro_extra_args:
            cmd.extend(shlex.split(self.cfg.maestro_extra_args))

        log.info("Running: %s", " ".join(cmd))
        start = time.monotonic()
        timed_out = False
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=self.cfg.maestro_timeout_s,
            )
            exit_code = proc.returncode
            output = (proc.stdout or "") + "\n" + (proc.stderr or "")
        except subprocess.TimeoutExpired as exc:
            exit_code = -1
            timed_out = True
            output = (
                (exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""))
                + "\n"
                + (exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or ""))
                + f"\n[selfheal] Maestro timed out after {self.cfg.maestro_timeout_s}s"
            )
        except FileNotFoundError:
            raise SystemExit(
                f"Maestro binary not found: {self.cfg.maestro_bin!r}. "
                "Install Maestro or set MAESTRO_BIN / install-maestro: 'true' in the action."
            )
        duration = time.monotonic() - start

        extra = self._collect_debug_logs(debug_dir)
        return RunResult(
            flow=flow_path,
            passed=exit_code == 0,
            exit_code=exit_code,
            output=output,
            duration_s=round(duration, 1),
            debug_dir=str(debug_dir),
            timed_out=timed_out,
            extra_logs=extra,
        )

    @staticmethod
    def _collect_debug_logs(debug_dir: Path, max_chars: int = 20_000) -> str:
        """Pull the tail of maestro.log and any command failure metadata."""
        chunks: list[str] = []
        try:
            for name in ("maestro.log", "commands-*.json"):
                for f in sorted(debug_dir.rglob(name), key=os.path.getmtime, reverse=True)[:2]:
                    text = f.read_text(errors="replace")
                    chunks.append(f"--- {f.name} (tail) ---\n{text[-8000:]}")
        except OSError:
            pass
        return "\n".join(chunks)[:max_chars]
