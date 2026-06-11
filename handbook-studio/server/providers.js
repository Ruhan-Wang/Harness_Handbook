// Multi-provider CLI registry (mirrors the set dr-claw drives: Claude, Cursor,
// Codex, Gemini). Each provider knows how to: be resolved on PATH, build a
// single-shot prompt invocation, and (best-effort) be logged in.
import { spawnSync } from 'child_process';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { getCliCommandCandidates, isCommandAvailable } from './cliResolution.js';
import { getSettings } from './settings.js';

const HOME = os.homedir();
const exists = (...p) => {
  try {
    return fs.existsSync(path.join(HOME, ...p));
  } catch {
    return false;
  }
};
const fileHas = (rel, needle) => {
  try {
    return fs.readFileSync(path.join(HOME, rel), 'utf8').includes(needle);
  } catch {
    return false;
  }
};

export const PROVIDERS = {
  // HTTP provider: your internal HMAC-signed data_eval endpoint (no CLI, no
  // subscription rate limits). Configured via the UI or HS_INTERNAL_* env vars.
  internal: {
    id: 'internal',
    label: 'Internal endpoint',
    kind: 'http',
    models: [
      { value: 'api_azure_openai_gpt-5.4-2026-03-05', label: 'GPT-5.4 (Azure)' },
      { value: 'api_azure_openai_gpt-5.5', label: 'GPT-5.5 (Azure)' },
    ],
  },
  claude: {
    id: 'claude',
    label: 'Claude Code',
    envVar: 'CLAUDE_CLI_PATH',
    commands: ['claude'],
    // claude -p "<prompt>" --output-format stream-json
    buildArgs: (prompt, model) => {
      const a = ['-p', prompt, '--output-format', 'stream-json', '--verbose'];
      if (model) a.push('--model', model);
      return a;
    },
    format: 'stream-json',
    models: [
      { value: 'opus', label: 'Opus' },
      { value: 'sonnet', label: 'Sonnet' },
      { value: 'haiku', label: 'Haiku' },
      { value: 'claude-opus-4-7', label: 'Opus 4.7' },
      { value: 'claude-opus-4-6', label: 'Opus 4.6' },
      { value: 'sonnet[1m]', label: 'Sonnet [1M]' },
    ],
    checkAuth: () =>
      !!process.env.ANTHROPIC_API_KEY ||
      exists('.claude', '.credentials.json') ||
      fileHas('.claude.json', 'oauthAccount'),
    // Claude Code auth is interactive (no URL-printing subcommand).
    loginArgs: null,
    loginHint: 'In a terminal run  claude  then type  /login  (or run  claude setup-token).',
  },
  cursor: {
    id: 'cursor',
    label: 'Cursor',
    envVar: 'CURSOR_CLI_PATH',
    commands: ['cursor-agent', 'agent'],
    buildArgs: (prompt, model) => {
      const a = ['-p', prompt, '--output-format', 'stream-json', '--trust'];
      if (model) a.push('--model', model);
      return a;
    },
    format: 'stream-json',
    models: [
      { value: 'auto', label: 'Auto' },
      { value: 'gpt-5.2', label: 'GPT-5.2' },
      { value: 'gpt-5.2-high', label: 'GPT-5.2 High' },
      { value: 'gpt-5.1-codex', label: 'GPT-5.1 Codex' },
      { value: 'gpt-5.1-codex-max', label: 'GPT-5.1 Codex Max' },
      { value: 'opus-4.5', label: 'Claude 4.5 Opus' },
      { value: 'opus-4.5-thinking', label: 'Claude 4.5 Opus (Thinking)' },
      { value: 'sonnet-4.5', label: 'Claude 4.5 Sonnet' },
      { value: 'gemini-3-pro', label: 'Gemini 3 Pro' },
      { value: 'composer-1', label: 'Composer 1' },
      { value: 'grok', label: 'Grok' },
    ],
    // cursor-agent status prints login state; fall back to config dir presence.
    checkAuth: (command) => {
      if (command) {
        try {
          const r = spawnSync(command, ['status'], { encoding: 'utf8', timeout: 4000 });
          const out = `${r.stdout || ''}${r.stderr || ''}`.toLowerCase();
          if (out.includes('logged in') || out.includes('authenticated')) return true;
          if (out.includes('not logged in') || out.includes('log in')) return false;
        } catch {
          /* ignore */
        }
      }
      return exists('.cursor', 'cli-config.json') ? null : false;
    },
    loginArgs: ['login'],
    loginHint: 'cursor-agent login  (prints a URL to authorize in your browser).',
  },
  gemini: {
    id: 'gemini',
    label: 'Gemini',
    envVar: 'GEMINI_CLI_PATH',
    commands: ['gemini'],
    buildArgs: (prompt, model) => {
      const a = ['--prompt', prompt, '--output-format', 'stream-json'];
      if (model) a.push('--model', model);
      return a;
    },
    format: 'stream-json',
    models: [
      { value: 'gemini-3.1-pro-preview', label: 'Gemini 3.1 Pro Preview' },
      { value: 'gemini-3-flash-preview', label: 'Gemini 3 Flash Preview' },
      { value: 'gemini-2.5-pro', label: 'Gemini 2.5 Pro' },
      { value: 'gemini-2.5-flash', label: 'Gemini 2.5 Flash' },
      { value: 'gemini-2.5-flash-lite', label: 'Gemini 2.5 Flash Lite' },
    ],
    checkAuth: () =>
      !!(process.env.GEMINI_API_KEY || process.env.GOOGLE_API_KEY) ||
      exists('.gemini', 'oauth_creds.json') ||
      exists('.gemini', 'google_accounts.json'),
    loginArgs: null,
    loginHint: 'Run  gemini  in a terminal and complete the Google sign-in prompt.',
  },
  codex: {
    id: 'codex',
    label: 'Codex',
    envVar: 'CODEX_CLI_PATH',
    commands: ['codex'],
    // codex exec runs non-interactively. --skip-git-repo-check lets it run in our
    // scratch cwd; --output-last-message writes only the final answer to a file
    // (stdout is full of progress logs we don't want).
    buildArgs: (prompt, model, ctx = {}) => {
      const a = ['exec', '--skip-git-repo-check'];
      if (ctx.outFile) a.push('--output-last-message', ctx.outFile);
      if (model) a.push('-m', model);
      a.push(prompt);
      return a;
    },
    format: 'file',
    models: [
      { value: 'gpt-5.5', label: 'GPT-5.5' },
      { value: 'gpt-5.4', label: 'GPT-5.4' },
      { value: 'gpt-5.3-codex', label: 'GPT-5.3 Codex' },
      { value: 'gpt-5.2-codex', label: 'GPT-5.2 Codex' },
      { value: 'gpt-5.2', label: 'GPT-5.2' },
      { value: 'gpt-5.1-codex-max', label: 'GPT-5.1 Codex Max' },
      { value: 'o3', label: 'O3' },
      { value: 'o4-mini', label: 'O4-mini' },
    ],
    checkAuth: () => !!process.env.OPENAI_API_KEY || exists('.codex', 'auth.json'),
    loginArgs: ['login'],
    loginHint: 'codex login  (opens your browser to sign in to ChatGPT/OpenAI).',
  },
};

export const PROVIDER_ORDER = ['internal', 'claude', 'cursor', 'codex', 'gemini'];

const cache = {};

// Resolve the internal endpoint config from env (preferred) then saved settings.
export function getInternalConfig() {
  const s = getSettings();
  return {
    host: process.env.HS_INTERNAL_HOST || s.internalHost || '',
    port: Number(process.env.HS_INTERNAL_PORT || s.internalPort || 8080),
    user: process.env.HS_INTERNAL_USER || s.internalUser || '',
    key: process.env.HS_INTERNAL_KEY || s.internalKey || '',
    model:
      process.env.HS_INTERNAL_MODEL ||
      s.internalModel ||
      'api_azure_openai_gpt-5.4-2026-03-05',
  };
}

function internalConfigured() {
  const c = getInternalConfig();
  return !!(c.host && c.user && c.key);
}

export function resolveProvider(id, { refresh = false } = {}) {
  const p = PROVIDERS[id];
  if (!p) return null;
  if (p.kind === 'http') return internalConfigured() ? 'http' : null;
  if (!refresh && cache[id] !== undefined) return cache[id];
  const candidates = getCliCommandCandidates({ envVarName: p.envVar, defaultCommands: p.commands });
  let found = null;
  for (const c of candidates) {
    if (isCommandAvailable(c, ['--version'])) {
      found = c;
      break;
    }
  }
  cache[id] = found;
  return found;
}

export function providerVersion(command) {
  if (!command) return null;
  try {
    const r = spawnSync(command, ['--version'], { encoding: 'utf8' });
    if (r.error) return null;
    return String(r.stdout || r.stderr || '').trim().split('\n')[0] || null;
  } catch {
    return null;
  }
}

export function listProviders({ refresh = false } = {}) {
  return PROVIDER_ORDER.map((id) => {
    const p = PROVIDERS[id];
    if (p.kind === 'http') {
      const c = getInternalConfig();
      const ok = internalConfigured();
      return {
        id,
        label: p.label,
        kind: 'http',
        found: ok,
        command: ok ? `${c.host}:${c.port}` : null,
        version: ok ? c.model : null,
        loggedIn: ok,
        models: p.models || [],
      };
    }
    const command = resolveProvider(id, { refresh });
    let loggedIn = null;
    if (command && typeof p.checkAuth === 'function') {
      try {
        loggedIn = p.checkAuth(command);
      } catch {
        loggedIn = null;
      }
    }
    return {
      id,
      label: p.label,
      found: !!command,
      command,
      kind: 'cli',
      version: command ? providerVersion(command) : null,
      loggedIn,
      models: p.models || [],
    };
  });
}

// Pick the effective provider id given the user's choice ('auto' or a specific id).
export function pickProvider(choice, { refresh = false } = {}) {
  if (choice && choice !== 'auto' && resolveProvider(choice, { refresh })) return choice;
  for (const id of PROVIDER_ORDER) {
    if (resolveProvider(id, { refresh })) return id;
  }
  return null;
}
