// Persistent, per-project, multi-session conversation/run store. Lives OUTSIDE
// React so pipeline progress and chat streams keep flowing across repo switches,
// tab changes, and page reloads. Chat history is persisted to localStorage so
// sessions survive reloads; transient run blocks are not persisted (the server
// replays in-flight runs via run-snapshot on reconnect).
import { useEffect, useReducer } from 'react';
import { connectWs, onWsMessage, sendWs } from './ws.js';

const LS_KEY = 'hs-sessions-v1';

// projectId -> { sessions: Session[], activeId, run: { runId, running, runSessionId } }
// Session = { id, title, messages, createdAt, updatedAt }
const projects = new Map();
const subscribers = new Set();
const runCompleteHandlers = new Set();
const idToLoc = new Map(); // chat message id -> { pid, sid }
let started = false;
let counter = 0;
let saveTimer = null;

const uid = (p) => `${p}-${Date.now()}-${(counter++).toString(36)}-${Math.random().toString(36).slice(2, 6)}`;

function newSessionObj() {
  const now = Date.now();
  return { id: uid('s'), title: 'New chat', messages: [], createdAt: now, updatedAt: now };
}

function getProj(pid) {
  if (!pid) return { sessions: [], activeId: null, run: { runId: null, running: false, runSessionId: null } };
  let p = projects.get(pid);
  if (!p) {
    const s = newSessionObj();
    p = { sessions: [s], activeId: s.id, run: { runId: null, running: false, runSessionId: null } };
    projects.set(pid, p);
  }
  if (!p.sessions.length) {
    const s = newSessionObj();
    p.sessions.push(s);
    p.activeId = s.id;
  }
  if (!p.activeId || !p.sessions.some((s) => s.id === p.activeId)) {
    p.activeId = p.sessions[0].id;
  }
  return p;
}

function getSession(pid, sid) {
  const p = getProj(pid);
  return p.sessions.find((s) => s.id === sid) || null;
}
function activeSession(pid) {
  const p = getProj(pid);
  return p.sessions.find((s) => s.id === p.activeId) || p.sessions[0];
}

function notify() {
  subscribers.forEach((fn) => fn());
  scheduleSave();
}

function setSessionMessages(pid, sid, updater) {
  const s = getSession(pid, sid);
  if (!s) return;
  s.messages = updater(s.messages);
  s.updatedAt = Date.now();
}

function patchInSession(pid, sid, id, fn) {
  setSessionMessages(pid, sid, (ms) => ms.map((m) => (m.id === id ? fn(m) : m)));
}

// Auto-title a session from its first user message.
function maybeTitle(session) {
  if (session.title && session.title !== 'New chat') return;
  const firstUser = session.messages.find((m) => m.kind === 'user');
  if (firstUser?.text) {
    session.title = firstUser.text.slice(0, 40) + (firstUser.text.length > 40 ? '…' : '');
  }
}

// ── Persistence ───────────────────────────────────────────────────────────
function scheduleSave() {
  if (saveTimer) return;
  saveTimer = setTimeout(() => {
    saveTimer = null;
    save();
  }, 400);
}
function save() {
  try {
    const byProject = {};
    for (const [pid, p] of projects.entries()) {
      byProject[pid] = {
        activeId: p.activeId,
        sessions: p.sessions.map((s) => ({
          id: s.id,
          title: s.title,
          createdAt: s.createdAt,
          updatedAt: s.updatedAt,
          // Persist only conversational messages, never transient run blocks.
          messages: s.messages
            .filter((m) => m.kind === 'user' || m.kind === 'assistant' || m.kind === 'system')
            .map((m) => ({ ...m, streaming: false })),
        })),
      };
    }
    localStorage.setItem(LS_KEY, JSON.stringify({ byProject }));
  } catch {
    /* ignore quota / serialization errors */
  }
}
function load() {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (!raw) return;
    const data = JSON.parse(raw);
    for (const [pid, p] of Object.entries(data.byProject || {})) {
      const sessions = (p.sessions || []).map((s) => ({
        id: s.id,
        title: s.title || 'New chat',
        createdAt: s.createdAt || Date.now(),
        updatedAt: s.updatedAt || Date.now(),
        messages: (s.messages || []).map((m) => ({ ...m, streaming: false })),
      }));
      if (sessions.length) {
        projects.set(pid, {
          sessions,
          activeId: p.activeId && sessions.some((s) => s.id === p.activeId) ? p.activeId : sessions[0].id,
          run: { runId: null, running: false, runSessionId: null },
        });
      }
    }
  } catch {
    /* ignore */
  }
}
load();

// ── WebSocket handling ──────────────────────────────────────────────────────
function singleRunningPid() {
  const running = [...projects.entries()].filter(([, p]) => p.run.runId).map(([id]) => id);
  return running.length === 1 ? running[0] : null;
}

function handle(msg) {
  if (msg.type === 'run-snapshot') {
    const b = msg.block;
    const pid = b?.projectId;
    if (!pid) return;
    const p = getProj(pid);
    const sid = p.run.runSessionId || p.activeId;
    const sess = getSession(pid, sid);
    if (!sess) return;
    const exists = sess.messages.some((m) => m.id === b.id);
    setSessionMessages(pid, sid, (ms) => (exists ? ms.map((m) => (m.id === b.id ? b : m)) : [...ms, b]));
    if (b.status === 'running') {
      p.run = { runId: b.id, running: true, runSessionId: sid };
    } else if (p.run.runId === b.id) {
      p.run = { runId: null, running: false, runSessionId: null };
    }
    notify();
    return;
  }

  if (msg.type === 'aborted') {
    const pid = msg.projectId;
    if (!pid) return;
    const p = getProj(pid);
    if (p.run.runId) patchInSession(pid, p.run.runSessionId, p.run.runId, (m) => ({ ...m, status: 'error', activeStep: null }));
    p.run = { runId: null, running: false, runSessionId: null };
    notify();
    return;
  }

  const RUN_EVENTS = ['log', 'step-start', 'step-done', 'done', 'error'];
  if (RUN_EVENTS.includes(msg.type)) {
    const pid = msg.projectId || singleRunningPid();
    if (!pid) return;
    const p = getProj(pid);
    const rid = p.run.runId;
    const sid = p.run.runSessionId;
    if (!rid || !sid) return;
    if (msg.type === 'log') {
      patchInSession(pid, sid, rid, (m) => ({ ...m, log: [...m.log.slice(-1200), msg.line] }));
    } else if (msg.type === 'step-start') {
      patchInSession(pid, sid, rid, (m) => ({
        ...m,
        activeStep: msg.step,
        log: [...m.log, `── ${msg.label || msg.step} ──`],
      }));
    } else if (msg.type === 'step-done') {
      patchInSession(pid, sid, rid, (m) => ({ ...m, done: { ...m.done, [msg.step]: true } }));
    } else if (msg.type === 'done') {
      patchInSession(pid, sid, rid, (m) => ({ ...m, status: 'done', activeStep: null }));
      p.run = { runId: null, running: false, runSessionId: null };
      runCompleteHandlers.forEach((fn) => fn(pid));
    } else if (msg.type === 'error') {
      patchInSession(pid, sid, rid, (m) => ({
        ...m,
        status: 'error',
        activeStep: null,
        log: [...m.log, `ERROR: ${msg.error}`],
      }));
      p.run = { runId: null, running: false, runSessionId: null };
      runCompleteHandlers.forEach((fn) => fn(pid));
    }
    notify();
    return;
  }

  if (msg.type === 'notice') {
    const pid = msg.projectId;
    if (!pid) return;
    const sid = getProj(pid).run.runSessionId || getProj(pid).activeId;
    setSessionMessages(pid, sid, (ms) => [...ms, { id: uid('sys'), kind: 'system', text: msg.message }]);
    notify();
    return;
  }

  if (msg.type === 'chat-delta' || msg.type === 'chat-done' || msg.type === 'chat-error') {
    const loc = idToLoc.get(msg.id);
    const pid = msg.projectId || loc?.pid;
    const sid = loc?.sid || (pid ? getProj(pid).activeId : null);
    if (!pid || !sid) return;
    if (msg.type === 'chat-delta') {
      patchInSession(pid, sid, msg.id, (m) => ({ ...m, text: m.text + msg.delta }));
    } else if (msg.type === 'chat-done') {
      patchInSession(pid, sid, msg.id, (m) => ({ ...m, text: msg.text || m.text, streaming: false }));
    } else {
      patchInSession(pid, sid, msg.id, (m) => ({ ...m, text: `Error: ${msg.error}`, error: true, streaming: false }));
    }
    notify();
  }
}

function ensureStarted() {
  if (started) return;
  started = true;
  connectWs().catch(() => {});
  onWsMessage(handle);
}

// ── Public session actions ──────────────────────────────────────────────────
export function listSessions(pid) {
  return getProj(pid).sessions;
}
export function getActiveId(pid) {
  return getProj(pid).activeId;
}
export function newSession(pid) {
  const p = getProj(pid);
  const s = newSessionObj();
  p.sessions.push(s);
  p.activeId = s.id;
  notify();
  return s.id;
}
export function switchSession(pid, sid) {
  const p = getProj(pid);
  if (p.sessions.some((s) => s.id === sid)) {
    p.activeId = sid;
    notify();
  }
}
export function renameSession(pid, sid, title) {
  const s = getSession(pid, sid);
  if (s) {
    s.title = title || 'Untitled';
    notify();
  }
}
export function deleteSession(pid, sid) {
  const p = getProj(pid);
  p.sessions = p.sessions.filter((s) => s.id !== sid);
  if (!p.sessions.length) p.sessions.push(newSessionObj());
  if (p.activeId === sid) p.activeId = p.sessions[0].id;
  if (p.run.runSessionId === sid) p.run = { runId: null, running: false, runSessionId: null };
  notify();
}

// ── Public run/chat actions ─────────────────────────────────────────────────
export function startRun(pid, { step, label, sourceRoot, mode }) {
  ensureStarted();
  const p = getProj(pid);
  if (p.run.running) return;
  const sid = p.activeId;
  const id = uid('run');
  p.run = { runId: id, running: true, runSessionId: sid };
  setSessionMessages(pid, sid, (ms) => [
    ...ms,
    { id, kind: 'run', step, label, activeStep: null, done: {}, log: [], status: 'running' },
  ]);
  notify();
  sendWs({ type: 'run', projectId: pid, step, sourceRoot: sourceRoot || undefined, mode }).catch(() => {});
}

export function abortRun(pid) {
  const p = getProj(pid);
  sendWs({ type: 'abort', projectId: pid }).catch(() => {});
  if (p.run.runId) patchInSession(pid, p.run.runSessionId, p.run.runId, (m) => ({ ...m, status: 'error', activeStep: null }));
  p.run = { runId: null, running: false, runSessionId: null };
  notify();
}

export function sendChat(pid, { prompt, displayText, provider, model }) {
  ensureStarted();
  const p = getProj(pid);
  if (p.run.running) return;
  const sid = p.activeId;
  const aid = uid('chat');
  idToLoc.set(aid, { pid, sid });
  setSessionMessages(pid, sid, (ms) => [
    ...ms,
    { id: uid('u'), kind: 'user', text: displayText },
    { id: aid, kind: 'assistant', text: '', streaming: true },
  ]);
  const sess = getSession(pid, sid);
  if (sess) maybeTitle(sess);
  notify();
  sendWs({ type: 'chat', id: aid, projectId: pid, prompt, provider, model }).catch(() => {});
}

export function onRunComplete(fn) {
  runCompleteHandlers.add(fn);
  return () => runCompleteHandlers.delete(fn);
}

// React hook: subscribe to a project's active session + session list.
export function useThread(pid) {
  const [, force] = useReducer((x) => x + 1, 0);
  useEffect(() => {
    ensureStarted();
    const fn = () => force();
    subscribers.add(fn);
    return () => subscribers.delete(fn);
  }, []);
  const p = getProj(pid);
  const active = activeSession(pid);
  return {
    sessions: p.sessions,
    activeId: p.activeId,
    messages: active ? active.messages : [],
    running: p.run.running,
  };
}
