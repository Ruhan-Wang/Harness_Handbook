# -*- coding: utf-8 -*-
"""LLM API client wrapper (OpenAI-compatible).

A thin client for any OpenAI-compatible `/chat/completions` endpoint, with retry,
JSON-response extraction, and a single-call helper that fits the per-function
Phase 2/3 workflow.

Configure it entirely from the environment:

    OPENAI_API_KEY   (required)                       -> Bearer token
    OPENAI_MODEL     (default: gpt-4o-mini)           -> the model name
    OPENAI_BASE_URL  (default: https://api.openai.com/v1)

Point `OPENAI_BASE_URL` at any OpenAI-compatible server (a self-hosted vLLM, a
proxy, Azure OpenAI, …); for a keyless local endpoint set `OPENAI_API_KEY=EMPTY`.
The lower-level `HANDBOOK_LLM_MODEL` / `HANDBOOK_LLM_BASE_URL` / `HANDBOOK_LLM_API_KEY`
names are still honored and win over the `OPENAI_*` ones.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

import requests

# Endpoint/model resolve from the standard OpenAI env vars, then the HANDBOOK_LLM_*
# overrides, then public OpenAI defaults.
DEFAULT_MODEL = (os.environ.get("OPENAI_MODEL")
                 or os.environ.get("HANDBOOK_LLM_MODEL") or "gpt-4o-mini")
DEFAULT_BASE_URL = (os.environ.get("OPENAI_BASE_URL")
                    or os.environ.get("HANDBOOK_LLM_BASE_URL")
                    or "https://api.openai.com/v1").rstrip("/")
DEFAULT_API_KEY = (os.environ.get("OPENAI_API_KEY")
                   or os.environ.get("HANDBOOK_LLM_API_KEY") or "")
DEFAULT_MAX_TOKENS = int(os.environ.get("OPENAI_MAX_TOKENS")
                         or os.environ.get("HANDBOOK_LLM_MAX_TOKENS") or "16000")


def _is_reasoning_model(model: str) -> bool:
    """gpt-5 / gpt-4.1 / o-series need `max_completion_tokens` and reject
    `temperature`; classic chat models take `max_tokens` and accept `temperature`."""
    return bool(re.search(r"gpt-5|gpt-4\.1|o[1-9]", model or "", re.IGNORECASE))


logger = logging.getLogger(__name__)


@dataclass
class LLMCallResult:
    """Wrapper around one successful LLM call."""

    raw_text: str
    status_code: int
    request_id: str
    elapsed_sec: float
    parsed_json: dict | None = None
    error: str | None = None


class Api:
    """Thin client for an OpenAI-compatible endpoint, with retry + JSON extraction."""

    def __init__(
        self,
        host: str | None = None,       # accepted for backward-compat; unused
        port: int | None = None,       # accepted for backward-compat; unused
        user: str | None = None,       # accepted for backward-compat; unused
        apikey: str | None = None,     # accepted for backward-compat; unused
        model_marker: str = DEFAULT_MODEL,
        request_timeout: int = 3600,
        call_timeout: int = 6000,
        max_retries: int = int(os.environ.get("HANDBOOK_LLM_MAX_RETRIES", "3")),
        retry_backoff_sec: float = float(os.environ.get("HANDBOOK_LLM_RETRY_BACKOFF", "2.0")),
        base_url: str = DEFAULT_BASE_URL,
        api_key: str = DEFAULT_API_KEY,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self.model_marker = model_marker
        self.request_timeout = request_timeout
        self.call_timeout = call_timeout
        self.max_retries = max_retries
        self.retry_backoff_sec = retry_backoff_sec
        self.max_tokens = max_tokens
        self.base_url = base_url.rstrip("/") + "/chat/completions"
        # An explicit ctor api_key wins; else the env-resolved key. Require SOME key
        # (fail loud instead of sending "Bearer " and getting a 401 mid-run). For a
        # keyless local endpoint, set OPENAI_API_KEY=EMPTY.
        self.api_key = api_key or DEFAULT_API_KEY
        if not self.api_key:
            raise EnvironmentError(
                "missing API key: set OPENAI_API_KEY (or HANDBOOK_LLM_API_KEY). For a "
                "keyless local endpoint, set OPENAI_API_KEY=EMPTY.")
        # kept for callers/smoke tests that introspect the transport mode
        self.openai_mode = True

    def call(self, prompt: str, params: dict | None = None) -> LLMCallResult:
        """Send a single-turn user prompt; return the raw response text + any extracted JSON.

        Retries on transient failures up to ``max_retries`` times with linear backoff
        plus jitter. Raises only if all retries fail.
        """
        request_id = str(uuid.uuid4())
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self.api_key}"}
        reasoning = _is_reasoning_model(self.model_marker)
        payload: dict[str, Any] = {
            "model": self.model_marker,
            "messages": [{"role": "user", "content": prompt}],
            # reasoning models (gpt-5.x / o-series) require max_completion_tokens and
            # reject temperature; classic models take max_tokens.
            ("max_completion_tokens" if reasoning else "max_tokens"): self.max_tokens,
        }
        temp = (params or {}).get("temperature")
        if temp is not None and not reasoning:
            payload["temperature"] = temp

        last_err: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            t0 = time.time()
            try:
                rsp = requests.post(
                    url=self.base_url,
                    headers=headers,
                    json=payload,
                    timeout=self.request_timeout,
                )
                elapsed = time.time() - t0
                if rsp.status_code != 200:
                    last_err = RuntimeError(
                        f"HTTP {rsp.status_code}: {rsp.text[:500]}"
                    )
                    logger.warning(
                        "LLM call attempt %d/%d: %s",
                        attempt,
                        self.max_retries,
                        last_err,
                    )
                    # Permanent client errors should NOT be retried — retrying an
                    # auth-misconfigured (401) or bad-payload (400) call just burns
                    # wall time. 408 (request timeout) and 429 (rate limit) are
                    # transient and stay in the retry loop; 5xx are also transient.
                    if rsp.status_code in (400, 401, 403, 404, 405, 410, 422):
                        raise last_err
                else:
                    text = self._extract_assistant_text(rsp.text)
                    parsed = _extract_json_block(text)
                    return LLMCallResult(
                        raw_text=text,
                        status_code=rsp.status_code,
                        request_id=request_id,
                        elapsed_sec=elapsed,
                        parsed_json=parsed,
                        error=None if parsed is not None else "no JSON block found",
                    )
            except RuntimeError:
                # Permanent client error already raised above; propagate without
                # waiting for the backoff sleep on the way out.
                raise
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning(
                    "LLM call attempt %d/%d raised %s: %s",
                    attempt,
                    self.max_retries,
                    type(e).__name__,
                    e,
                )

            if attempt < self.max_retries:
                jitter = random.uniform(0, 0.5)
                time.sleep(self.retry_backoff_sec * attempt + jitter)

        # All retries failed.
        raise RuntimeError(
            f"LLM call failed after {self.max_retries} attempts: {last_err}"
        )

    @staticmethod
    def _extract_assistant_text(raw_response_text: str) -> str:
        """The server returns a JSON body; extract the assistant text.

        Handles the OpenAI chat/completions shape plus a few common fallbacks; if
        none match, returns the raw body so the caller can still pull a JSON block.
        """
        try:
            body = json.loads(raw_response_text)
        except json.JSONDecodeError:
            return raw_response_text

        candidates = [
            lambda b: b["choices"][0]["message"]["content"],
            lambda b: b["data"]["choices"][0]["message"]["content"],
            lambda b: b["data"]["response"],
            lambda b: b["response"],
            lambda b: b["result"]["content"],
            lambda b: b["data"]["content"],
            lambda b: b["text"],
        ]
        for getter in candidates:
            try:
                value = getter(body)
                if isinstance(value, str):
                    return value
            except (KeyError, IndexError, TypeError):
                continue

        # If none matched, return pretty-printed JSON; caller can still pull a fenced block.
        return json.dumps(body, ensure_ascii=False)


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.DOTALL)


def _extract_json_block(text: str) -> dict | None:
    """Find a JSON object inside the model's text response.

    Strategy:
      1. Look for ```json ... ``` fenced blocks (preferred).
      2. Fall back to the first balanced ``{...}`` block.
    Returns the parsed dict, or None on failure.
    """
    for match in _JSON_FENCE_RE.finditer(text):
        candidate = match.group(1).strip()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    # Fallback: find first balanced { ... }
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"' and not in_string:
                in_string = True
            elif ch == '"' and in_string:
                in_string = False
            elif not in_string:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start : i + 1])
                        except json.JSONDecodeError:
                            break
        start = text.find("{", start + 1)
    return None
