#!/usr/bin/env node
import express from 'express';
import cors from 'cors';
import http from 'http';
import path from 'path';
import fs from 'fs';
import { fileURLToPath } from 'url';
import { WebSocketServer } from 'ws';

import { createGatewayRouter, streamPrompt } from './llm-gateway.js';
import projectsRoutes from './routes/projects.js';
import handbookRoutes from './routes/handbook.js';
import cliRoutes from './routes/cli.js';
import { runStep, abortRun } from './pipeline-runner.js';
import { getProject } from './projects.js';
import { ensureHandbookIntro } from './handbook-intro.js';

// Create HANDBOOK.md on first interaction with a project (chat or pipeline).
function maybeWriteIntro(project, send) {
  if (!project) return;
  try {
    const { path: p, created } = ensureHandbookIntro(project);
    if (created) send({ type: 'notice', message: `Generated HANDBOOK.md at ${p}` });
  } catch (err) {
    send({ type: 'notice', message: `Could not write HANDBOOK.md: ${String(err?.message || err)}` });
  }
}

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.HS_SERVER_PORT || 4319);

const app = express();
app.use(cors());
app.use(express.json({ limit: '25mb' }));

app.use(createGatewayRouter());
app.use(projectsRoutes);
app.use(handbookRoutes);
app.use(cliRoutes);

// Serve the built frontend if present (production / single-process mode).
const distDir = path.resolve(__dirname, '../dist');
if (fs.existsSync(distDir)) {
  app.use(express.static(distDir));
  app.get('*', (req, res, next) => {
    if (req.path.startsWith('/api')) return next();
    res.sendFile(path.join(distDir, 'index.html'));
  });
}

const server = http.createServer(app);
const wss = new WebSocketServer({ server, path: '/ws' });

// Broadcast to every connected client so progress survives page reloads and
// repo switches (any client can render any project's run).
function broadcast(obj) {
  const data = JSON.stringify(obj);
  for (const c of wss.clients) {
    if (c.readyState === c.OPEN) c.send(data);
  }
}

// Server-side snapshot of the current run block per project, so a freshly
// connected client can rebuild in-flight progress it missed.
const runStates = new Map(); // projectId -> run block

const STEP_LABELS = {
  phase1: '1 · Map call graph',
  skeleton: '2 · Draft skeleton',
  phase2: '3 · Classify stages',
  phase3: '4 · Write docs',
  full: 'Generate full handbook',
};

function applyToBlock(block, e) {
  if (e.type === 'log') {
    block.log.push(e.line);
    if (block.log.length > 1200) block.log = block.log.slice(-1200);
  } else if (e.type === 'step-start') {
    block.activeStep = e.step;
    block.log.push(`── ${e.label || e.step} ──`);
  } else if (e.type === 'step-done') {
    block.done[e.step] = true;
  } else if (e.type === 'done') {
    block.status = 'done';
    block.activeStep = null;
  } else if (e.type === 'error') {
    block.status = 'error';
    block.activeStep = null;
    block.log.push(`ERROR: ${e.error}`);
  }
}

wss.on('connection', (ws) => {
  const send = (obj) => {
    if (ws.readyState === ws.OPEN) ws.send(JSON.stringify(obj));
  };
  send({ type: 'connected' });
  // Replay any known run blocks so a reloaded/late client catches up.
  for (const block of runStates.values()) {
    send({ type: 'run-snapshot', block });
  }

  ws.on('message', async (raw) => {
    let msg;
    try {
      msg = JSON.parse(raw.toString());
    } catch {
      return;
    }

    if (msg.type === 'run') {
      const project = getProject(msg.projectId);
      if (!project) {
        send({ type: 'error', error: 'project not found' });
        return;
      }
      const pid = project.id;
      const block = {
        id: `srv-${Date.now()}`,
        kind: 'run',
        step: msg.step,
        label: STEP_LABELS[msg.step] || msg.step,
        activeStep: null,
        done: {},
        log: [],
        status: 'running',
        projectId: pid,
      };
      runStates.set(pid, block);
      // Tag + buffer + broadcast every pipeline event.
      const psend = (obj) => {
        const e = { ...obj, projectId: pid };
        if (e.type !== 'notice') applyToBlock(block, e);
        broadcast(e);
      };
      maybeWriteIntro(project, psend);
      await runStep(
        { project, step: msg.step, sourceRoot: msg.sourceRoot, mode: msg.mode, serverPort: PORT },
        psend
      );
    } else if (msg.type === 'abort') {
      const ok = abortRun(msg.projectId);
      const block = runStates.get(msg.projectId);
      if (block && block.status === 'running') {
        block.status = 'error';
        block.activeStep = null;
      }
      broadcast({ type: 'aborted', ok, projectId: msg.projectId });
    } else if (msg.type === 'chat') {
      const project = msg.projectId ? getProject(msg.projectId) : null;
      const id = msg.id;
      const pid = project?.id;
      maybeWriteIntro(project, (obj) => broadcast({ ...obj, projectId: pid }));
      try {
        const { text } = await streamPrompt(
          msg.prompt,
          { provider: msg.provider, model: msg.model, cwd: project?.path },
          (delta) => broadcast({ type: 'chat-delta', id, projectId: pid, delta })
        );
        broadcast({ type: 'chat-done', id, projectId: pid, text });
      } catch (err) {
        broadcast({ type: 'chat-error', id, projectId: pid, error: String(err?.message || err) });
      }
    }
  });
});

server.listen(PORT, () => {
  console.log(`[handbook-studio] server + LLM gateway listening on http://127.0.0.1:${PORT}`);
  console.log(`[handbook-studio] gateway endpoint: http://127.0.0.1:${PORT}/api/v1/data_eval`);
});
