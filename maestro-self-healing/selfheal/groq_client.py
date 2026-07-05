"""Minimal, robust Groq client (OpenAI-compatible chat completions API).

Uses plain `requests` so there is no SDK version drift in CI.
Handles rate limits (429), transient 5xx errors, and network failures
with exponential backoff, and never logs the API key.
"""

from __future__ import annotations

import json
import logging
import random
import time
from typing import Optional

import requests

from .config import Config

log = logging.getLogger("selfheal.groq")


class GroqError(RuntimeError):
    pass


class GroqClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._session = requests.Session()

    def chat_json(self, system_prompt: str, user_prompt: str) -> dict:
        """Send a chat request and return the parsed JSON object from the model."""
        content = self._chat(system_prompt, user_prompt, force_json=True)
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            # Some models occasionally wrap JSON in code fences despite JSON mode.
            stripped = content.strip()
            if stripped.startswith("```"):
                stripped = stripped.strip("`")
                if stripped.startswith("json"):
                    stripped = stripped[4:]
                try:
                    return json.loads(stripped)
                except json.JSONDecodeError:
                    pass
            raise GroqError(f"Model did not return valid JSON. First 300 chars: {content[:300]!r}")

    def _chat(self, system_prompt: str, user_prompt: str, force_json: bool = False) -> str:
        url = f"{self.cfg.groq_base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": self.cfg.groq_model,
            "temperature": self.cfg.groq_temperature,
            "max_tokens": self.cfg.groq_max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if force_json:
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {self.cfg.groq_api_key}",
            "Content-Type": "application/json",
        }

        last_err: Optional[str] = None
        for attempt in range(1, self.cfg.groq_max_retries + 1):
            try:
                resp = self._session.post(
                    url, json=payload, headers=headers, timeout=self.cfg.groq_timeout_s
                )
            except requests.RequestException as exc:
                last_err = f"network error: {exc.__class__.__name__}"
                log.warning("Groq request failed (%s), attempt %d", last_err, attempt)
                self._sleep(attempt)
                continue

            if resp.status_code == 200:
                data = resp.json()
                try:
                    return data["choices"][0]["message"]["content"]
                except (KeyError, IndexError) as exc:
                    raise GroqError(f"Unexpected Groq response shape: {exc}") from exc

            if resp.status_code in (429, 500, 502, 503, 504):
                retry_after = resp.headers.get("retry-after")
                wait = float(retry_after) if retry_after else None
                last_err = f"HTTP {resp.status_code}"
                log.warning("Groq returned %s, attempt %d/%d", last_err, attempt, self.cfg.groq_max_retries)
                self._sleep(attempt, override=wait)
                continue

            # Non-retryable (401, 400, 404 ...)
            body = resp.text[:500]
            raise GroqError(f"Groq API error HTTP {resp.status_code}: {body}")

        raise GroqError(f"Groq API unavailable after {self.cfg.groq_max_retries} attempts ({last_err})")

    @staticmethod
    def _sleep(attempt: int, override: Optional[float] = None) -> None:
        delay = override if override is not None else min(2 ** attempt + random.uniform(0, 1), 60)
        time.sleep(delay)
