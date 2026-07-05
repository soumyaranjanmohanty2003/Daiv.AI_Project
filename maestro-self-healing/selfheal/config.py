"""Configuration for the self-healing tool."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    # Groq
    groq_api_key: str = field(default_factory=lambda: os.environ.get("GROQ_API_KEY", ""))
    groq_model: str = field(default_factory=lambda: os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile"))
    groq_base_url: str = field(
        default_factory=lambda: os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
    )
    groq_temperature: float = 0.1
    groq_max_tokens: int = 4096
    groq_timeout_s: int = 120
    groq_max_retries: int = 5

    # Healing behaviour
    max_heal_attempts: int = field(default_factory=lambda: int(os.environ.get("MAX_HEAL_ATTEMPTS", "3")))
    max_flows_to_heal: int = field(default_factory=lambda: int(os.environ.get("MAX_FLOWS_TO_HEAL", "10")))
    min_confidence: float = field(default_factory=lambda: float(os.environ.get("MIN_CONFIDENCE", "0.5")))
    restore_on_failure: bool = field(default_factory=lambda: _env_bool("RESTORE_ON_FAILURE", True))
    allow_assertion_removal: bool = field(default_factory=lambda: _env_bool("ALLOW_ASSERTION_REMOVAL", False))
    allow_unknown_commands: bool = field(default_factory=lambda: _env_bool("ALLOW_UNKNOWN_COMMANDS", False))
    dry_run: bool = field(default_factory=lambda: _env_bool("DRY_RUN", False))

    # Maestro
    maestro_bin: str = field(default_factory=lambda: os.environ.get("MAESTRO_BIN", "maestro"))
    maestro_extra_args: str = field(default_factory=lambda: os.environ.get("MAESTRO_EXTRA_ARGS", ""))
    maestro_timeout_s: int = field(default_factory=lambda: int(os.environ.get("MAESTRO_TIMEOUT_S", "900")))
    debug_output_dir: str = field(default_factory=lambda: os.environ.get("DEBUG_OUTPUT_DIR", ".selfheal/debug"))

    # Reporting
    report_path: str = field(default_factory=lambda: os.environ.get("REPORT_PATH", ".selfheal/report.md"))
    log_tail_lines: int = 120

    def validate(self) -> None:
        if not self.dry_run and not self.groq_api_key:
            raise SystemExit(
                "GROQ_API_KEY is not set. Get a free key at https://console.groq.com and "
                "add it as a repository secret."
            )
        if self.max_heal_attempts < 1:
            raise SystemExit("MAX_HEAL_ATTEMPTS must be >= 1")
        if not 0.0 <= self.min_confidence <= 1.0:
            raise SystemExit("MIN_CONFIDENCE must be between 0 and 1")
