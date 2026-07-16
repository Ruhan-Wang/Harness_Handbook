#!/usr/bin/env python3
"""resync_llm.py — the shared LLM backend for the resync engines.

Both resync engines (the MEMBER-level `resync_handbook.py` and the FILE-level
`resync_large.py`) need one thing from the LLM: an object exposing
`.call(prompt, params=...) -> result` where `result.raw_text` / `result.parsed_json`
mirror the generators' own `api_client.Api` contract, so the classification /
translation / read / rollup code paths cannot tell the backends apart.

This module is deliberately dependency-light: it imports the active generator's
`api_client` (LLMCallResult / _extract_json_block) LAZILY, inside the methods — so
importing `resync_llm` never forces a particular generator onto `sys.path`. The caller
(a resync engine) is responsible for having put the generator's `shared/` dir on
`sys.path` before the first `.call()`.

Backend: the OpenAI-compatible endpoint (the same OPENAI_*/LLM_* env the agents use),
via one bare `/chat/completions` POST per call (`get_api()`, created once per process).

Per-call token usage is appended to a JSONL ledger when `set_usage_path` has
been pointed at one (the agents' usage lives in their NexAU traces; these
single-shot calls would otherwise be invisible).
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

# ─── usage accounting ─────────────────────────────────────────────────────────
_LLM_PHASE = "unknown"
_USAGE_PATH: Path | None = None
_USAGE_LOCK = threading.Lock()


def set_usage_path(path: Path | None) -> None:
    """Point the usage ledger at `path` (fresh per run — reruns must not
    accumulate). None disables accounting."""
    global _USAGE_PATH
    _USAGE_PATH = path
    if path is not None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def set_phase(phase: str) -> None:
    """Tag subsequent usage records with the current phase (read / assign /
    organize / rollup / ...)."""
    global _LLM_PHASE
    _LLM_PHASE = phase


def log_usage(model: str, usage: dict) -> None:
    if _USAGE_PATH is None or not usage:
        return
    rec = {"phase": _LLM_PHASE, "model": model,
           "in": usage.get("prompt_tokens", 0),
           "out": usage.get("completion_tokens", 0)}
    cached = usage.get("prompt_cache_hit_tokens",
                       (usage.get("prompt_tokens_details") or {}).get("cached_tokens"))
    if cached is not None:
        rec["cached"] = cached
    try:
        with _USAGE_LOCK, _USAGE_PATH.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError:
        pass                                   # accounting must never break resync


# ─── env-endpoint backend (bare /chat/completions POST) ───────────────────────
class EnvLLM:
    """Resync LLM on the SAME OpenAI-compatible endpoint/model the agents use. One
    bare /chat/completions POST per call, mirroring api_client.Api's contract
    (.call(prompt, params=...) → .raw_text / .parsed_json). Retries transient
    failures. Each call's token usage is appended to the usage ledger."""

    def __init__(self, max_retries: int = 3, backoff_sec: float = 2.0) -> None:
        # Resolve the OpenAI-compatible endpoint from the standard OpenAI env vars, then
        # the LLM_* equivalents, then the public OpenAI defaults — so resync works even
        # when code_agent's env bridge has not been imported in this process.
        self.base = (os.environ.get("LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
                     or "https://api.openai.com/v1").rstrip("/")
        self.model = (os.environ.get("LLM_MODEL") or os.environ.get("OPENAI_MODEL")
                      or "gpt-4o-mini")
        # Require an explicit key (mirrors the planner's _load_official_dict): fail loud on a
        # missing key rather than sending "Bearer EMPTY" and getting a 401 mid-run. For a
        # keyless local endpoint, set OPENAI_API_KEY=EMPTY (or LLM_API_KEY=EMPTY) explicitly.
        self.key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if not self.key:
            raise EnvironmentError(
                "missing API key: set OPENAI_API_KEY (or LLM_API_KEY). For a keyless local "
                "endpoint, set OPENAI_API_KEY=EMPTY.")
        self.extra = (json.loads(os.environ["LLM_EXTRA_BODY"])
                      if os.environ.get("LLM_EXTRA_BODY") else {})
        self.max_retries = max_retries
        self.backoff_sec = backoff_sec

    def call(self, prompt: str, params: dict | None = None):
        import requests
        from api_client import LLMCallResult, _extract_json_block

        body = {"model": self.model, "temperature": 0.0, "max_tokens": 12000,
                "messages": [{"role": "user", "content": prompt}], **self.extra,
                **(params or {})}
        last: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            t0 = time.time()
            try:
                r = requests.post(
                    f"{self.base}/chat/completions",
                    headers={"Content-Type": "application/json",
                             "Authorization": f"Bearer {self.key}"},
                    json=body, timeout=600,
                )
                r.raise_for_status()
                data = r.json()
                text = data["choices"][0]["message"]["content"] or ""
                log_usage(self.model, data.get("usage") or {})
                return LLMCallResult(
                    raw_text=text, status_code=r.status_code, request_id="",
                    elapsed_sec=time.time() - t0,
                    parsed_json=_extract_json_block(text))
            except Exception as e:  # noqa: BLE001
                last = e
                if attempt < self.max_retries:
                    time.sleep(self.backoff_sec * attempt)
        assert last is not None
        raise last


_api = None


def get_api():
    """The resync LLM backend (the OpenAI-compatible OPENAI_*/LLM_* endpoint), created
    once per process."""
    global _api
    if _api is None:
        _api = EnvLLM()
    return _api


def set_api(api) -> None:
    """Inject a backend explicitly (used by tests to stub the LLM offline)."""
    global _api
    _api = api
