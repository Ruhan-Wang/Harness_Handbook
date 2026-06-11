// Provider-aware CLI routes: list/detect installed coding CLIs, read/write the
// active provider+model settings, drive login, and a single-shot prompt passthrough.
import express from 'express';
import { spawn } from 'child_process';
import { listProviders, resolveProvider, pickProvider, PROVIDERS } from '../providers.js';
import { getSettings, setSettings } from '../settings.js';
import { runPrompt } from '../llm-gateway.js';

const router = express.Router();

router.get('/api/cli/status', (_req, res) => {
  const providers = listProviders({ refresh: true });
  const settings = getSettings();
  const activeId = pickProvider(settings.provider, { refresh: true });
  res.json({ providers, settings, active: activeId });
});

router.put('/api/cli/settings', (req, res) => {
  const body = req.body || {};
  const patch = {};
  for (const k of [
    'provider',
    'model',
    'internalHost',
    'internalUser',
    'internalKey',
    'internalModel',
  ]) {
    if (typeof body[k] === 'string') patch[k] = body[k];
  }
  if (body.internalPort !== undefined) patch.internalPort = Number(body.internalPort) || 8080;
  res.json({ settings: setSettings(patch) });
});

// Login: for URL-based CLIs (cursor/codex) spawn the login command and surface
// the auth URL; for interactive CLIs (claude/gemini) return the command to run
// in a terminal. Always returns something actionable for the UI.
router.post('/api/cli/login', (req, res) => {
  const id = (req.body || {}).provider || pickProvider(getSettings().provider);
  const provider = PROVIDERS[id];
  const command = provider ? resolveProvider(id, { refresh: true }) : null;
  if (!command) {
    res.status(400).json({ error: `CLI for provider "${id}" not found` });
    return;
  }

  // Interactive providers can't be driven from a piped child process.
  if (!provider.loginArgs) {
    res.json({
      started: false,
      provider: id,
      interactive: true,
      hint: provider.loginHint || `Run "${command}" in a terminal to sign in.`,
    });
    return;
  }

  const manualCommand = `${command} ${provider.loginArgs.join(' ')}`;
  const child = spawn(command, provider.loginArgs, { stdio: ['ignore', 'pipe', 'pipe'] });
  let buffer = '';
  let responded = false;
  const urlRe = /(https?:\/\/[^\s'"]+)/i;

  const tryRespond = () => {
    if (responded) return;
    const m = buffer.match(urlRe);
    if (m) {
      responded = true;
      res.json({ started: true, provider: id, authUrl: m[1], command: manualCommand });
    }
  };

  child.stdout.on('data', (d) => {
    buffer += d.toString();
    tryRespond();
  });
  child.stderr.on('data', (d) => {
    buffer += d.toString();
    tryRespond();
  });
  child.on('close', () => {
    if (!responded) {
      responded = true;
      res.json({
        started: true,
        provider: id,
        authUrl: null,
        command: manualCommand,
        hint: provider.loginHint,
        output: buffer.slice(0, 2000),
      });
    }
  });
  child.on('error', (err) => {
    if (!responded) {
      responded = true;
      res.status(500).json({ error: String(err.message) });
    }
  });
  setTimeout(() => {
    if (!responded) {
      responded = true;
      res.json({
        started: true,
        provider: id,
        authUrl: null,
        command: manualCommand,
        hint: provider.loginHint,
        output: buffer.slice(0, 2000),
      });
    }
  }, 8000);
});

router.post('/api/cli/prompt', async (req, res) => {
  try {
    const { prompt, model, provider } = req.body || {};
    if (!prompt) {
      res.status(400).json({ error: 'missing prompt' });
      return;
    }
    const result = await runPrompt(prompt, { model, provider });
    res.json(result);
  } catch (err) {
    res.status(502).json({ error: String(err?.message || err) });
  }
});

export default router;
