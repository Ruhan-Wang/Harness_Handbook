// LLM gateway: adapts whichever coding CLI is installed (Claude Code, Cursor,
// Codex, Gemini — the same set dr-claw drives) into a single-shot completion the
// Python handbook pipeline can call as if it were the internal `data_eval` endpoint.
//
// The pipeline posts:
//   { messages: [{ role: "user", content: [{ type: "text", value: "<prompt>" }] }], ... }
// and reads back:
//   { answer: [{ type: "text", value: "<assistant text>" }] }
//
// We spawn the active provider's CLI in single-shot mode in a throwaway cwd (all
// needed source is inlined in the prompt), accumulate the assistant text, return it.
import express from 'express';
import os from 'os';
import path from 'path';
import fs from 'fs';
import crypto from 'crypto';
import { spawn } from 'child_process';
import { PROVIDERS, pickProvider, resolveProvider, getInternalConfig } from './providers.js';
import { getSettings } from './settings.js';

const MAX_CONCURRENCY = Number(process.env.HS_LLM_CONCURRENCY || 4);

let active = 0;
const waiters = [];

function acquire() {
  if (active < MAX_CONCURRENCY) {
    active += 1;
    return Promise.resolve();
  }
  return new Promise((resolve) => waiters.push(resolve));
}

function release() {
  active -= 1;
  const next = waiters.shift();
  if (next) {
    active += 1;
    next();
  }
}

let sandboxDir = null;
function getSandboxCwd() {
  if (sandboxDir && fs.existsSync(sandboxDir)) return sandboxDir;
  sandboxDir = fs.mkdtempSync(path.join(os.tmpdir(), 'handbook-llm-'));
  return sandboxDir;
}

// Tolerant extraction from a stream-json line (shapes differ across Claude Code /
// Cursor / Gemini). Returns assistant `delta` text and any final `result` text
// separately, because providers emit the assistant message AND a duplicate
// `result` event — counting both double-prints the answer.
// Extract assistant text from the internal data_eval response body (tolerant of
// the known trpc-gpt-eval shapes).
function extractAnswerText(raw) {
  let body;
  try {
    body = JSON.parse(raw);
  } catch {
    return raw;
  }
  const firstText = (items) => {
    if (Array.isArray(items)) {
      for (const it of items) {
        if (it && it.type === 'text' && typeof it.value === 'string') return it.value;
      }
    }
    return undefined;
  };
  const getters = [
    () => firstText(body.answer),
    () => firstText(body.data?.answer),
    () => body.data?.choices?.[0]?.message?.content,
    () => body.choices?.[0]?.message?.content,
    () => body.data?.response,
    () => body.response,
    () => body.result?.content,
    () => body.text,
  ];
  for (const g of getters) {
    try {
      const v = g();
      if (typeof v === 'string') return v;
    } catch {
      /* keep trying */
    }
  }
  return JSON.stringify(body);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Token-bucket pacing for the internal endpoint, which enforces a per-minute
// quota (AIHub default 30/min). We pace request *starts* so we stay under it.
const INTERNAL_RPM = Number(process.env.HS_INTERNAL_RPM || 25);
const INTERNAL_MAX_RETRIES = Number(process.env.HS_INTERNAL_MAX_RETRIES || 5);
let rateChain = Promise.resolve();
let lastInternalStart = 0;

function internalRateGate() {
  const minGap = 60000 / Math.max(1, INTERNAL_RPM);
  rateChain = rateChain.then(async () => {
    const wait = Math.max(0, lastInternalStart + minGap - Date.now());
    if (wait > 0) await sleep(wait);
    lastInternalStart = Date.now();
  });
  return rateChain;
}

// Call the internal HMAC-signed data_eval endpoint (mirrors phase2/api_client.py).
// Paced to the per-minute quota and retried with backoff on 429/5xx.
async function callInternal(prompt, modelOverride) {
  const c = getInternalConfig();
  if (!c.host || !c.user || !c.key) {
    throw new Error('Internal endpoint not configured (need host, user/secretId, key/secretKey).');
  }
  const url = `http://${c.host}:${c.port}/api/v1/data_eval`;

  let lastErr = '';
  for (let attempt = 1; attempt <= INTERNAL_MAX_RETRIES; attempt += 1) {
    await internalRateGate();

    const source = 'xxxxxx';
    const dateTime = new Date().toUTCString();
    const signStr = `date: ${dateTime}\nsource: ${source}`;
    const sign = crypto.createHmac('sha1', c.key).update(signStr).digest('base64');
    const auth = `hmac id="${c.user}", algorithm="hmac-sha1", headers="date source", signature="${sign}"`;
    const payload = {
      request_id: crypto.randomUUID(),
      model_marker: modelOverride || c.model,
      messages: [{ role: 'user', content: [{ type: 'text', value: prompt }] }],
      params: {},
      timeout: 6000,
    };

    let res;
    try {
      res = await fetch(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Apiversion: 'v2.03',
          Authorization: auth,
          Date: dateTime,
          Source: source,
        },
        body: JSON.stringify(payload),
      });
    } catch (e) {
      lastErr = String(e?.message || e);
      await sleep(Math.min(30000, 1000 * 2 ** attempt));
      continue;
    }

    const txt = await res.text();
    if (res.ok) return extractAnswerText(txt).trim();

    // Rate-limited (429) or quota (body code 1005) → wait and retry.
    const quotaHit = res.status === 429 || /1005|api limit|限额/.test(txt);
    lastErr = `internal endpoint HTTP ${res.status}: ${txt.slice(0, 300)}`;
    if (quotaHit || res.status >= 500) {
      if (attempt < INTERNAL_MAX_RETRIES) {
        // For quota, wait long enough for the per-minute window to drain.
        const backoff = quotaHit
          ? Math.min(60000, (60000 / Math.max(1, INTERNAL_RPM)) * attempt + 2000)
          : Math.min(30000, 1000 * 2 ** attempt);
        await sleep(backoff);
        continue;
      }
    }
    throw new Error(lastErr);
  }
  throw new Error(lastErr || 'internal endpoint failed');
}

function parseEvent(evt) {
  let delta = '';
  const content = evt?.message?.content;
  if (Array.isArray(content)) {
    for (const block of content) {
      if (typeof block?.text === 'string') delta += block.text;
    }
  }
  if (typeof evt?.delta?.text === 'string') delta += evt.delta.text;
  let result = '';
  if (evt?.type === 'result' && typeof evt?.result === 'string') result = evt.result;
  return { delta, result };
}

/**
 * Run a single prompt through the active CLI provider.
 * @param {string} prompt
 * @param {{ provider?: string, model?: string, cwd?: string, timeoutMs?: number }} [opts]
 * @returns {Promise<{ text: string, provider: string, model: string|null }>}
 */
export function runPrompt(prompt, opts = {}) {
  const settings = getSettings();
  const providerId = pickProvider(opts.provider || settings.provider);
  if (!providerId) {
    return Promise.reject(
      new Error('No coding CLI found. Install one of: claude, cursor-agent, codex, gemini.')
    );
  }
  const provider = PROVIDERS[providerId];

  if (provider.kind === 'http') {
    return acquire().then(async () => {
      try {
        const text = await callInternal(prompt, opts.model);
        return { text, provider: providerId, model: null };
      } finally {
        release();
      }
    });
  }

  const command = resolveProvider(providerId);
  // Only fall back to the saved model when it actually belongs to the chosen
  // provider — otherwise (e.g. a Codex model handed to Claude) it errors out.
  const settingsModelApplies = settings.provider === 'auto' || settings.provider === providerId;
  const model = opts.model || (settingsModelApplies ? settings.model : '') || undefined;
  const timeoutMs = opts.timeoutMs || Number(process.env.HS_LLM_TIMEOUT_MS || 600000);
  const cwd = opts.cwd || getSandboxCwd();
  const outFile =
    provider.format === 'file'
      ? path.join(cwd, `out-${Date.now()}-${Math.random().toString(36).slice(2)}.txt`)
      : null;
  const args = provider.buildArgs(prompt, model, { outFile });

  return acquire().then(
    () =>
      new Promise((resolve, reject) => {
        let assistantText = '';
        let resultText = '';
        let stderr = '';
        let settled = false;

        const readOutFile = () => {
          if (!outFile) return;
          try {
            assistantText = fs.readFileSync(outFile, 'utf8');
          } catch {
            /* file may not exist if the CLI failed early */
          }
          try {
            fs.unlinkSync(outFile);
          } catch {
            /* ignore */
          }
        };

        const child = spawn(command, args, {
          cwd,
          stdio: ['ignore', 'pipe', 'pipe'],
          env: { ...process.env },
        });

        const timer = setTimeout(() => {
          if (settled) return;
          settled = true;
          try {
            child.kill('SIGKILL');
          } catch {
            /* ignore */
          }
          release();
          reject(new Error(`${command} timed out after ${timeoutMs}ms`));
        }, timeoutMs);

        let buf = '';
        child.stdout.on('data', (chunk) => {
          const s = chunk.toString();
          if (provider.format === 'text') {
            assistantText += s;
            return;
          }
          if (provider.format === 'file') {
            return; // answer is read from outFile on close
          }
          buf += s;
          const lines = buf.split('\n');
          buf = lines.pop() || '';
          for (const line of lines) {
            const trimmed = line.trim();
            if (!trimmed) continue;
            try {
              const { delta, result } = parseEvent(JSON.parse(trimmed));
              assistantText += delta;
              if (result) resultText = result;
            } catch {
              /* non-JSON line; ignore */
            }
          }
        });

        child.stderr.on('data', (d) => {
          stderr += d.toString();
        });

        child.on('error', (err) => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          release();
          reject(err);
        });

        child.on('close', (code) => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          release();
          readOutFile();
          // Prefer streamed assistant content; fall back to the result event.
          let finalText = (assistantText.trim() || resultText.trim()).trim();
          if (!finalText && code !== 0) {
            reject(new Error(`${command} exited ${code}: ${stderr.slice(0, 500)}`));
            return;
          }
          resolve({ text: finalText, provider: providerId, model: model || null });
        });
      })
  );
}

/**
 * Stream a prompt through the active CLI, emitting text deltas as they arrive.
 * stream-json providers (Claude/Cursor/Gemini) stream incrementally; file/text
 * providers (Codex) emit once at the end.
 * @param {string} prompt
 * @param {{ provider?: string, model?: string, cwd?: string, timeoutMs?: number }} opts
 * @param {(delta: string) => void} onDelta
 * @returns {Promise<{ text: string, provider: string }>}
 */
export function streamPrompt(prompt, opts = {}, onDelta = () => {}) {
  const settings = getSettings();
  const providerId = pickProvider(opts.provider || settings.provider);
  if (!providerId) {
    return Promise.reject(
      new Error('No coding CLI found. Install one of: claude, cursor-agent, codex, gemini.')
    );
  }
  const provider = PROVIDERS[providerId];

  if (provider.kind === 'http') {
    return acquire().then(async () => {
      try {
        const text = await callInternal(prompt, opts.model);
        try {
          onDelta(text);
        } catch {
          /* ignore */
        }
        return { text, provider: providerId };
      } finally {
        release();
      }
    });
  }

  const command = resolveProvider(providerId);
  const settingsModelApplies = settings.provider === 'auto' || settings.provider === providerId;
  const model = opts.model || (settingsModelApplies ? settings.model : '') || undefined;
  const timeoutMs = opts.timeoutMs || Number(process.env.HS_LLM_TIMEOUT_MS || 600000);
  const cwd = opts.cwd && fs.existsSync(opts.cwd) ? opts.cwd : getSandboxCwd();
  const outFile =
    provider.format === 'file'
      ? path.join(getSandboxCwd(), `out-${Date.now()}-${Math.random().toString(36).slice(2)}.txt`)
      : null;
  const args = provider.buildArgs(prompt, model, { outFile });

  return acquire().then(
    () =>
      new Promise((resolve, reject) => {
        let full = '';
        let resultText = '';
        let stderr = '';
        let buf = '';
        let settled = false;

        const child = spawn(command, args, {
          cwd,
          stdio: ['ignore', 'pipe', 'pipe'],
          env: { ...process.env },
        });

        const timer = setTimeout(() => {
          if (settled) return;
          settled = true;
          try {
            child.kill('SIGKILL');
          } catch {
            /* ignore */
          }
          release();
          reject(new Error(`${command} timed out after ${timeoutMs}ms`));
        }, timeoutMs);

        const emit = (delta) => {
          if (!delta) return;
          full += delta;
          try {
            onDelta(delta);
          } catch {
            /* ignore listener errors */
          }
        };

        child.stdout.on('data', (chunk) => {
          const s = chunk.toString();
          if (provider.format === 'text') {
            emit(s);
            return;
          }
          if (provider.format === 'file') return;
          buf += s;
          const lines = buf.split('\n');
          buf = lines.pop() || '';
          for (const line of lines) {
            const t = line.trim();
            if (!t) continue;
            try {
              const { delta, result } = parseEvent(JSON.parse(t));
              emit(delta);
              if (result) resultText = result;
            } catch {
              /* non-JSON line */
            }
          }
        });

        child.stderr.on('data', (d) => {
          stderr += d.toString();
        });

        child.on('error', (err) => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          release();
          reject(err);
        });

        child.on('close', (code) => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          release();
          if (outFile) {
            try {
              emit(fs.readFileSync(outFile, 'utf8'));
            } catch {
              /* ignore */
            }
            try {
              fs.unlinkSync(outFile);
            } catch {
              /* ignore */
            }
          }
          // If nothing streamed (provider only sent a result event), emit it now.
          if (!full.trim() && resultText.trim()) emit(resultText);
          if (!full.trim() && code !== 0) {
            reject(new Error(`${command} exited ${code}: ${stderr.slice(0, 500)}`));
            return;
          }
          resolve({ text: full.trim(), provider: providerId });
        });
      })
  );
}

export function createGatewayRouter() {
  const router = express.Router();

  router.post('/api/v1/data_eval', async (req, res) => {
    try {
      const body = req.body || {};
      const prompt = body?.messages?.[0]?.content?.[0]?.value;
      if (!prompt || typeof prompt !== 'string') {
        res.status(400).json({ code: 400, msg: 'missing prompt' });
        return;
      }
      const { text } = await runPrompt(prompt, {
        provider: body?.params?.provider,
        model: body?.params?.model,
      });
      res.json({ code: 0, msg: 'ok', answer: [{ type: 'text', value: text }] });
    } catch (err) {
      res.status(502).json({ code: 502, msg: String(err?.message || err) });
    }
  });

  router.get('/api/v1/health', (_req, res) => {
    res.json({ ok: true, concurrency: MAX_CONCURRENCY, active });
  });

  return router;
}
