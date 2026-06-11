import { useEffect, useRef, useState } from 'react';
import {
  useThread,
  startRun,
  abortRun,
  sendChat as storeSendChat,
  newSession,
  switchSession,
  renameSession,
  deleteSession,
} from '../lib/threadStore.js';
import Markdown from './Markdown.jsx';
import SkeletonEditor from './SkeletonEditor.jsx';

const STEPS = [
  { id: 'phase1', short: 'Map graph', label: '1 · Map call graph' },
  { id: 'skeleton', short: 'Draft skeleton', label: '2 · Draft skeleton' },
  { id: 'phase2', short: 'Classify', label: '3 · Classify stages' },
  { id: 'phase3', short: 'Write docs', label: '4 · Write docs' },
];

function buildContext(artifacts) {
  const refs = artifacts?.references || {};
  const parts = [];
  if (refs['overview.md']) parts.push(`# OVERVIEW\n${refs['overview.md']}`);
  if (refs['index.md']) parts.push(`# INDEX (stages + registers)\n${refs['index.md']}`);
  if (refs['registers.md']) parts.push(`# STATE REGISTERS\n${refs['registers.md']}`);
  return parts.join('\n\n---\n\n');
}

const SYSTEM = [
  'You are a code navigation assistant for a specific repository. A structural HANDBOOK of the repo',
  'is provided below as your PRIMARY source of truth — it was generated from this exact codebase.',
  'Base your answer on the HANDBOOK first: reference its stage names, state registers, and sections',
  'explicitly (e.g. "per the handbook, Stage 2 (…)"). Do NOT rely on generic prior knowledge of the',
  'project; if the handbook and your assumptions disagree, trust the handbook. You are also running',
  'inside the repo, so you may read files to confirm details. When asked where a change must take',
  'effect, enumerate EVERY relevant code site and cite file:line. If the handbook does not cover',
  'something, say so explicitly before answering from the code. Keep answers focused and concrete.',
].join('\n');

function buildPrompt(context, focusText, history, question) {
  const lines = [SYSTEM, ''];
  if (context) lines.push('===== HANDBOOK =====', context, '===== END HANDBOOK =====', '');
  if (focusText) {
    lines.push(
      '===== USER IS CURRENTLY VIEWING =====',
      focusText,
      '===== END CURRENTLY VIEWING =====',
      'The user is looking at the above item in the handbook UI right now. If they say "this",',
      '"it", or ask without naming a target, they mean this item. Prioritize it in your answer.',
      ''
    );
  }
  if (history.length) {
    lines.push('===== CONVERSATION SO FAR =====');
    for (const m of history) lines.push(`${m.role === 'user' ? 'USER' : 'ASSISTANT'}: ${m.text}`);
    lines.push('===== END CONVERSATION =====', '');
  }
  lines.push(`USER: ${question}`, 'ASSISTANT:');
  return lines.join('\n');
}

export default function Workspace({ project, artifacts, focus, onClearFocus, onArtifactsChange, onOpenViewer }) {
  const { messages, running, sessions, activeId } = useThread(project?.id);
  const [input, setInput] = useState('');
  const [sourceRoot, setSourceRoot] = useState('');
  const [mode, setMode] = useState('fast');
  const [editingSkeleton, setEditingSkeleton] = useState(false);
  const scrollRef = useRef(null);
  const present = artifacts?.present || {};

  const ctxKb = (() => {
    const t = buildContext(artifacts);
    return t ? Math.max(1, Math.round(t.length / 1024)) : 0;
  })();

  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [messages]);

  const runPipeline = (step) => {
    if (running) return;
    const label =
      step === 'full'
        ? 'Generate full handbook'
        : STEPS.find((s) => s.id === step)?.label || step;
    startRun(project.id, { step, label, sourceRoot: sourceRoot.trim() || undefined, mode });
  };

  const stop = () => abortRun(project.id);

  const sendChat = () => {
    const q = input.trim();
    if (!q || running) return;
    setInput('');
    const history = messages
      .filter((m) => m.kind === 'user' || m.kind === 'assistant')
      .map((m) => ({ role: m.kind, text: m.text }));
    storeSendChat(project.id, {
      displayText: q,
      prompt: buildPrompt(buildContext(artifacts), focus?.text || '', history, q),
    });
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <SessionBar
        sessions={sessions}
        activeId={activeId}
        onSwitch={(sid) => switchSession(project.id, sid)}
        onNew={() => newSession(project.id)}
        onRename={(sid) => {
          const cur = sessions.find((s) => s.id === sid);
          const title = window.prompt('Rename session', cur?.title || '');
          if (title != null) renameSession(project.id, sid, title.trim() || 'Untitled');
        }}
        onDelete={(sid) => {
          if (sessions.length <= 1 || window.confirm('Delete this session?')) deleteSession(project.id, sid);
        }}
      />
      <div ref={scrollRef} className="min-h-0 flex-1 space-y-4 overflow-y-auto p-5">
        {messages.length === 0 && <EmptyState present={present} />}
        {messages.map((m) =>
          m.kind === 'run' ? (
            <RunBlock key={m.id} m={m} />
          ) : m.kind === 'system' ? (
            <div key={m.id} className="flex justify-center">
              <span className="chip bg-secondary text-muted-foreground">{m.text}</span>
            </div>
          ) : (
            <div key={m.id} className={m.kind === 'user' ? 'flex justify-end' : 'flex justify-start'}>
              <div
                className={`max-w-[80%] rounded-lg px-4 py-2 ${
                  m.kind === 'user'
                    ? 'bg-primary text-primary-foreground'
                    : m.error
                      ? 'border border-destructive/40 bg-destructive/10'
                      : 'card'
                }`}
              >
                {m.kind === 'user' ? (
                  <span className="text-sm">{m.text}</span>
                ) : m.text ? (
                  <Markdown>{m.text}</Markdown>
                ) : (
                  <ThinkingDots />
                )}
                {m.streaming && m.text && (
                  <span className="hs-caret ml-0.5 inline-block h-4 w-[2px] bg-primary align-text-bottom" />
                )}
              </div>
            </div>
          )
        )}
      </div>

      <div className="border-t border-border bg-card/40 p-3">
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <button onClick={() => runPipeline('full')} disabled={running} className="btn-primary text-xs">
            {running ? 'Generating…' : 'Generate full handbook'}
          </button>
          {STEPS.map((s) => (
            <button
              key={s.id}
              onClick={() => runPipeline(s.id)}
              disabled={running}
              className="btn-ghost px-2 py-1 text-xs"
            >
              {s.short}
            </button>
          ))}
          {present.skeleton && (
            <button onClick={() => setEditingSkeleton(true)} disabled={running} className="btn-ghost px-2 py-1 text-xs">
              Edit skeleton
            </button>
          )}
          {present.handbook && (
            <button onClick={onOpenViewer} className="btn-ghost px-2 py-1 text-xs">
              Open handbook →
            </button>
          )}
          {running && (
            <button onClick={stop} className="btn-ghost px-2 py-1 text-xs text-destructive">
              Stop
            </button>
          )}
          <select
            value={mode}
            onChange={(e) => setMode(e.target.value)}
            disabled={running}
            title="How many actor-critic rounds to run. Fewer rounds = far fewer LLM calls."
            className="ml-auto rounded border border-border bg-background px-2 py-1 text-[11px] text-foreground outline-none focus:border-primary"
          >
            <option value="thorough">Thorough (full actor-critic)</option>
            <option value="fast">Fast (1 critic, no revise)</option>
            <option value="minimal">Minimal (actor only, no critic)</option>
          </select>
          <input
            value={sourceRoot}
            onChange={(e) => setSourceRoot(e.target.value)}
            placeholder="scope: src/.../terminus_2 (blank = whole repo)"
            disabled={running}
            className="w-64 rounded border border-border bg-background px-2 py-1 text-[11px] text-foreground outline-none focus:border-primary"
          />
        </div>

        <div className="mb-2 flex flex-wrap items-center gap-2 text-[11px]">
          {ctxKb > 0 ? (
            <span className="chip bg-emerald-500/15 text-emerald-400" title="The handbook overview, stage/register index, and registers are sent with every message.">
              ◆ Handbook attached · {ctxKb} KB
            </span>
          ) : (
            <span className="chip bg-secondary text-muted-foreground" title="Generate the handbook (Write docs) so chat can be grounded in it.">
              ○ No handbook context yet — answers won’t be grounded
            </span>
          )}
          {focus && (
            <span className="chip inline-flex items-center gap-1 bg-primary/15 text-primary" title="Selected in the handbook viewer; sent with your next message so you can say “explain this”.">
              {focus.kind === 'stage' ? '▣' : 'ƒ'} Focused: {focus.label}
              <button
                onClick={() => onClearFocus?.()}
                className="ml-1 rounded px-1 text-primary/70 hover:bg-primary/20 hover:text-primary"
                title="Clear focus"
              >
                ×
              </button>
            </span>
          )}
        </div>

        <div className="flex gap-2">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && sendChat()}
            placeholder={
              running
                ? 'Generating… chat is paused until it finishes'
                : 'Ask the agent about this repo, or click an action above…'
            }
            disabled={running}
            className="flex-1 rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground outline-none focus:border-primary"
          />
          <button onClick={sendChat} disabled={running || !input.trim()} className="btn-primary">
            Send
          </button>
        </div>
      </div>

      {editingSkeleton && (
        <SkeletonEditor
          project={project}
          onClose={() => setEditingSkeleton(false)}
          onSaved={() => {
            setEditingSkeleton(false);
            onArtifactsChange?.();
          }}
        />
      )}
    </div>
  );
}

function SessionBar({ sessions, activeId, onSwitch, onNew, onRename, onDelete }) {
  return (
    <div className="flex items-center gap-1 border-b border-border bg-card/60 px-2 py-1.5">
      <div className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto">
        {sessions.map((s) => {
          const active = s.id === activeId;
          return (
            <div
              key={s.id}
              className={`group flex shrink-0 items-center gap-1 rounded-md border px-2 py-1 text-xs transition-colors ${
                active
                  ? 'border-primary/40 bg-primary/15 text-foreground'
                  : 'border-transparent bg-secondary text-muted-foreground hover:text-foreground'
              }`}
            >
              <button
                onClick={() => onSwitch(s.id)}
                onDoubleClick={() => onRename(s.id)}
                className="max-w-[160px] truncate"
                title={`${s.title} — double-click to rename`}
              >
                {s.title}
              </button>
              <button
                onClick={() => onRename(s.id)}
                className={`rounded px-1 text-muted-foreground hover:bg-primary/20 hover:text-primary ${
                  active ? 'opacity-100' : 'opacity-0 group-hover:opacity-100'
                }`}
                title="Rename session"
              >
                ✎
              </button>
              <button
                onClick={() => onDelete(s.id)}
                className="rounded px-1 text-muted-foreground opacity-0 hover:bg-destructive/20 hover:text-destructive group-hover:opacity-100"
                title="Delete session"
              >
                ×
              </button>
            </div>
          );
        })}
      </div>
      <button onClick={onNew} className="btn-ghost shrink-0 px-2 py-1 text-xs" title="New session">
        + New
      </button>
    </div>
  );
}

function EmptyState({ present }) {
  return (
    <div className="mx-auto max-w-xl pt-12 text-center">
      <div className="text-lg font-medium text-foreground">Generate & chat in one place</div>
      <p className="mt-2 text-sm text-muted-foreground">
        Click <span className="text-foreground">Generate full handbook</span> (or run a single step)
        below — progress streams right here. Then ask the agent questions about the repo; it answers
        grounded in the handbook and can read the actual files.
      </p>
      {!present.graph && (
        <p className="mt-3 text-xs text-muted-foreground">
          Tip: set a scope (e.g. the agent subfolder) to keep generation fast and focused.
        </p>
      )}
    </div>
  );
}

// Pull the most recent "[N/M] ..." counter out of the streamed log.
function parseProgress(log) {
  for (let i = log.length - 1; i >= 0; i--) {
    const mt = log[i].match(/\[(\d+)\/(\d+)\]/);
    if (mt) return { current: Number(mt[1]), total: Number(mt[2]) };
  }
  return null;
}

function Spinner() {
  return (
    <span className="inline-block h-3 w-3 animate-spin rounded-full border-[1.5px] border-current border-t-transparent" />
  );
}

function ThinkingDots() {
  return (
    <span className="inline-flex items-center gap-2">
      <span className="thinking-shimmer text-sm font-medium">Thinking</span>
      <span className="inline-flex items-end gap-1">
        {[0, 1, 2].map((i) => (
          <span
            key={i}
            className="h-1.5 w-1.5 animate-bounce rounded-full bg-muted-foreground"
            style={{ animationDelay: `${i * 0.16}s`, animationDuration: '1s' }}
          />
        ))}
      </span>
    </span>
  );
}

function RunBlock({ m }) {
  const [open, setOpen] = useState(true);
  const logRef = useRef(null);
  useEffect(() => {
    if (open && logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [m.log, open]);

  const progress = m.status === 'running' ? parseProgress(m.log) : null;
  const activeStep = STEPS.find((s) => s.id === m.activeStep);
  const pct = progress ? Math.round((progress.current / progress.total) * 100) : null;

  return (
    <div className="card overflow-hidden">
      <div className="flex items-center gap-2 px-3 py-2">
        {m.status === 'running' ? (
          <span className="text-primary">
            <Spinner />
          </span>
        ) : (
          <span
            className={`h-2 w-2 rounded-full ${
              m.status === 'error' ? 'bg-destructive' : 'bg-emerald-500'
            }`}
          />
        )}
        <span className="text-sm font-medium text-foreground">{m.label}</span>
        <span className="text-[11px] text-muted-foreground">
          {m.status === 'running'
            ? activeStep
              ? `${activeStep.label}${progress ? ` · ${progress.current}/${progress.total}` : ''}`
              : 'running…'
            : m.status === 'error'
              ? 'failed'
              : 'done'}
        </span>
        <button onClick={() => setOpen((v) => !v)} className="ml-auto text-[11px] text-muted-foreground hover:text-foreground">
          {open ? 'hide log' : 'show log'}
        </button>
      </div>

      <div className="flex flex-wrap items-center gap-1 px-3 pb-2">
        {STEPS.map((s) => {
          const isDone = m.done[s.id];
          const isActive = m.activeStep === s.id && m.status === 'running';
          return (
            <span
              key={s.id}
              className={`chip inline-flex items-center gap-1 ${
                isDone
                  ? 'bg-emerald-500/15 text-emerald-400'
                  : isActive
                    ? 'bg-primary/20 text-primary'
                    : 'bg-secondary text-muted-foreground'
              }`}
            >
              {isActive && <Spinner />}
              {isDone && <span aria-hidden>✓</span>}
              {s.short}
              {isActive && progress ? ` ${progress.current}/${progress.total}` : ''}
            </span>
          );
        })}
      </div>

      {pct != null && (
        <div className="px-3 pb-2">
          <div className="h-1 w-full overflow-hidden rounded-full bg-secondary">
            <div
              className="h-full rounded-full bg-primary transition-all duration-300"
              style={{ width: `${pct}%` }}
            />
          </div>
        </div>
      )}

      {open && (
        <div
          ref={logRef}
          className="code-font max-h-72 overflow-auto border-t border-border bg-background/60 p-3 text-[11px] leading-relaxed text-slate-300"
        >
          {m.log.length === 0 ? (
            <span className="text-muted-foreground">starting…</span>
          ) : (
            m.log.map((line, i) => (
              <div
                key={i}
                className={
                  line.startsWith('ERROR')
                    ? 'text-destructive'
                    : line.startsWith('──') || line.startsWith('▶')
                      ? 'text-primary'
                      : ''
                }
              >
                {line}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}
