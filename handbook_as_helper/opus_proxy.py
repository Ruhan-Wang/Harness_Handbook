#!/usr/bin/env python3
"""opus_proxy.py — let NexAU's Anthropic client reach the trpc gateway's Claude Opus.

NexAU's `anthropic_chat_completion` client authenticates with an `x-api-key` header, but the
gateway at trpc-gpt-eval.production.polaris:8080/v1/messages wants:
    Authorization: Bearer {APP_ID}:{APP_KEY}?provider=anthropic&model=<model>&timeout=<t>

This tiny localhost proxy takes NexAU's standard Anthropic request, swaps in that auth, and
forwards (streaming-aware) to the gateway. Point NexAU at `base_url=http://localhost:<port>`.
It is non-invasive: NexAU and the gateway are both untouched.

Run it (credentials only ever live in YOUR shell, never in the repo):
    OPUS_APP_ID=...  OPUS_APP_KEY=...  python opus_proxy.py

Env:
    OPUS_APP_ID, OPUS_APP_KEY   required — gateway credentials
    OPUS_GATEWAY                default http://trpc-gpt-eval.production.polaris:8080
    OPUS_PROXY_PORT             default 9100
    OPUS_TIMEOUT                default 600  (seconds; also sent to the gateway)
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

# Gateway credentials come ONLY from the environment — never hardcode secrets.
#   OPUS_APP_ID=...  OPUS_APP_KEY=...  python opus_proxy.py
APP_ID = os.environ.get("OPUS_APP_ID", "")
APP_KEY = os.environ.get("OPUS_APP_KEY", "")
GATEWAY = os.environ.get("OPUS_GATEWAY", "http://trpc-gpt-eval.production.polaris:8080").rstrip("/")
PORT = int(os.environ.get("OPUS_PROXY_PORT", "9100"))
TIMEOUT = int(os.environ.get("OPUS_TIMEOUT", "600"))


class Handler(BaseHTTPRequestHandler):
    def _forward(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""

        model = "claude-opus-4-8"
        try:
            model = json.loads(body).get("model", model)
        except Exception:  # noqa: BLE001
            pass

        query = f"provider=anthropic&model={model}&timeout={TIMEOUT}"
        url = GATEWAY + self.path  # the SDK calls /v1/messages
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {APP_ID}:{APP_KEY}?{query}",
        }
        r = requests.post(
            url, data=body, headers=headers, timeout=TIMEOUT,
            proxies={"http": None, "https": None},  # never route localhost-bound creds via Squid
        )
        content = r.content  # read the full body (non-streaming; the agent runs stream=False)
        self.send_response(r.status_code)
        self.send_header("Content-Type", r.headers.get("Content-Type", "application/json"))
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._forward()
        except Exception as e:  # noqa: BLE001
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": f"[opus_proxy] {e!r}"}).encode())

    def log_message(self, *_args) -> None:  # quiet
        pass


if __name__ == "__main__":
    print(f"opus_proxy: http://localhost:{PORT}  ->  {GATEWAY}/v1/messages  (Claude Opus via Bearer)")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
