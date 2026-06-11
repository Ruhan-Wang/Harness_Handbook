# -*- coding: utf-8 -*-
"""LLM API client wrapper.

Wraps the company's internal data_eval endpoint (see test_api.py at repo root).
Adds retry, JSON-response extraction, and a single-call helper that fits the
Phase 2 LLM-per-function workflow.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
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

API_VERSION = "v2.03"
# Host/port are env-overridable so the same pipeline can target either the
# internal trpc-gpt-eval endpoint (production) or a local LLM gateway that
# fronts the Cursor subscription CLI (Handbook Studio). When HANDBOOK_LLM_HOST
# is set, the client talks to that gateway instead of the internal endpoint.
DEFAULT_HOST = os.environ.get("HANDBOOK_LLM_HOST", "trpc-gpt-eval.production.polaris")
DEFAULT_PORT = int(os.environ.get("HANDBOOK_LLM_PORT", "8080"))
DEFAULT_MODEL_MARKER = os.environ.get(
    "HANDBOOK_LLM_MODEL", "api_azure_openai_gpt-5.4-2026-03-05"
)

# Credentials must come from the environment (never hardcode secrets in source).
# Set HANDBOOK_LLM_USER / HANDBOOK_LLM_KEY, or pass user=/apikey= to Api(...).
# When targeting the local Handbook Studio gateway these can be left empty.
DEFAULT_USER = os.environ.get("HANDBOOK_LLM_USER", "")
DEFAULT_KEY = os.environ.get("HANDBOOK_LLM_KEY", "")

logger = logging.getLogger(__name__)


def _get_simple_auth(source: str, secret_id: str, secret_key: str) -> tuple[str, str]:
    # Use timezone-aware UTC; datetime.utcnow() is deprecated in 3.12+ and
    # slated for removal. The "GMT" suffix in the format string remains
    # accurate since we're explicitly in UTC.
    date_time = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%a, %d %b %Y %H:%M:%S GMT"
    )
    auth = (
        'hmac id="' + secret_id + '", algorithm="hmac-sha1", '
        'headers="date source", signature="'
    )
    sign_str = "date: " + date_time + "\n" + "source: " + source
    sign = hmac.new(secret_key.encode(), sign_str.encode(), hashlib.sha1).digest()
    sign = base64.b64encode(sign).decode()
    return auth + sign + '"', date_time


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
    """Thin client for the data_eval endpoint, with retry + JSON extraction."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        user: str = DEFAULT_USER,
        apikey: str = DEFAULT_KEY,
        model_marker: str = DEFAULT_MODEL_MARKER,
        request_timeout: int = 3600,
        call_timeout: int = 6000,
        max_retries: int = 3,
        retry_backoff_sec: float = 2.0,
    ) -> None:
        self.base_url = f"http://{host}:{port}/api/v1/data_eval"
        self.user = user
        self.apikey = apikey
        self.model_marker = model_marker
        self.request_timeout = request_timeout
        self.call_timeout = call_timeout
        self.max_retries = max_retries
        self.retry_backoff_sec = retry_backoff_sec

    def _headers(self) -> dict[str, str]:
        source = "xxxxxx"  # signature watermark; arbitrary value accepted by server
        sign, date_time = _get_simple_auth(source, self.user, self.apikey)
        return {
            "Apiversion": API_VERSION,
            "Authorization": sign,
            "Date": date_time,
            "Source": source,
        }

    def call(self, prompt: str, params: dict | None = None) -> LLMCallResult:
        """Send a single-turn user prompt; return the raw response text + any extracted JSON.

        Retries on transient failures up to ``max_retries`` times with linear backoff
        plus jitter. Raises only if all retries fail.
        """
        request_id = str(uuid.uuid4())
        payload: dict[str, Any] = {
            "request_id": request_id,
            "model_marker": self.model_marker,
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "value": prompt}],
                }
            ],
            "params": params or {},
            "timeout": self.call_timeout,
        }

        last_err: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            t0 = time.time()
            try:
                rsp = requests.post(
                    url=self.base_url,
                    headers=self._headers(),
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
                    # Permanent client errors should NOT be retried — retrying
                    # an auth-misconfigured (401) or bad-payload (400) call
                    # just burns wall time. 408 (request timeout) and 429
                    # (rate limit) are transient and stay in the retry loop;
                    # 5xx server errors are also transient.
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
        """The server returns a JSON body; extract assistant text from common shapes.

        Tries several known response shapes; falls back to returning the raw body so
        the caller can still attempt to extract a JSON block from it.
        """
        try:
            body = json.loads(raw_response_text)
        except json.JSONDecodeError:
            return raw_response_text

        # Common shapes seen in trpc-gpt-eval responses
        candidates = [
            # trpc-gpt-eval canonical shape: {code, msg, answer: [{type: "text", value: "..."}, ...]}
            lambda b: _first_text_value(b["answer"]),
            lambda b: _first_text_value(b["data"]["answer"]),
            # OpenAI-ish
            lambda b: b["data"]["choices"][0]["message"]["content"],
            lambda b: b["choices"][0]["message"]["content"],
            # Simple nested
            lambda b: b["data"]["response"],
            lambda b: b["response"],
            lambda b: b["result"]["content"],
            lambda b: b["data"]["content"],
            # Last resort
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


def _first_text_value(items):
    """Walk a list of {type, value} dicts and return the first ``value`` whose type is ``text``."""
    if not isinstance(items, list):
        raise TypeError("not a list")
    for item in items:
        if isinstance(item, dict) and item.get("type") == "text":
            value = item.get("value")
            if isinstance(value, str):
                return value
    raise KeyError("no text item with string value")


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
