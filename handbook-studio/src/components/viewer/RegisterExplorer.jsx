import { useMemo, useState } from 'react';
import { projectRelPath } from '../../lib/handbook.js';

// The handbook's signature asset: for each state variable, every read + write site.
// Built directly from graph.self_attrs (structured), with function ids resolved to
// file:line via graph.nodes so each site links to source.
export default function RegisterExplorer({ artifacts, onOpenSource, onSelectNode }) {
  const registers = useMemo(() => {
    const selfAttrs = artifacts?.graph?.self_attrs || {};
    const nodes = artifacts?.graph?.nodes || {};
    const out = [];
    for (const [cls, attrs] of Object.entries(selfAttrs)) {
      for (const [attr, usage] of Object.entries(attrs)) {
        const reads = (usage.read_in || []).map((id) => ({ id, node: nodes[id] }));
        const writes = (usage.written_in || []).map((id) => ({ id, node: nodes[id] }));
        out.push({
          key: `${cls}.${attr}`,
          cls,
          attr,
          reads,
          writes,
          total: reads.length + writes.length,
        });
      }
    }
    return out.sort((a, b) => b.total - a.total);
  }, [artifacts]);

  const [active, setActive] = useState(registers[0]?.key || null);
  const reg = registers.find((r) => r.key === active);

  if (registers.length === 0) {
    return (
      <div className="flex h-full items-center justify-center text-slate-500">
        No state registers detected. Run Phase 1 first.
      </div>
    );
  }

  const Site = ({ site, kind }) => {
    const n = site.node;
    const filePath = n ? projectRelPath(artifacts, n.file) : null;
    return (
      <div className="card flex items-center justify-between px-3 py-2">
        <button
          onClick={() => onSelectNode?.(site.id)}
          className="code-font min-w-0 truncate text-left text-xs text-slate-200 hover:text-accent"
        >
          {n?.qualname || site.id}
        </button>
        <div className="flex items-center gap-2">
          <span
            className={`chip ${kind === 'write' ? 'bg-rose-500/20 text-rose-300' : 'bg-sky-500/20 text-sky-300'}`}
          >
            {kind}
          </span>
          {n?.line_start ? (
            <button
              className="shrink-0 text-[11px] text-slate-500 hover:text-accent"
              onClick={() => onOpenSource(filePath, [n.line_start, n.line_end])}
            >
              {n.file}:{n.line_start}
            </button>
          ) : null}
        </div>
      </div>
    );
  };

  return (
    <div className="flex h-full min-h-0">
      <div className="w-72 overflow-y-auto border-r border-ink-600 p-2">
        <div className="px-2 py-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
          State registers ({registers.length})
        </div>
        {registers.map((r) => (
          <button
            key={r.key}
            onClick={() => setActive(r.key)}
            className={`mb-1 flex w-full items-center justify-between rounded-md px-3 py-2 text-left transition-colors ${
              active === r.key ? 'bg-ink-700' : 'hover:bg-ink-700/50'
            }`}
          >
            <span className="code-font min-w-0 truncate text-xs text-slate-200">{r.attr}</span>
            <span className="ml-2 flex shrink-0 gap-1">
              <span className="chip bg-sky-500/20 text-sky-300">{r.reads.length}R</span>
              <span className="chip bg-rose-500/20 text-rose-300">{r.writes.length}W</span>
            </span>
          </button>
        ))}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-5">
        {reg && (
          <>
            <h2 className="code-font text-lg font-semibold text-foreground">
              {reg.cls}.<span className="text-accent">{reg.attr}</span>
            </h2>
            <p className="mt-1 text-xs text-slate-500">
              {reg.writes.length} write site(s) and {reg.reads.length} read site(s). A change to this
              register must stay consistent across all of them.
            </p>

            <div className="mt-4">
              <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-rose-300">
                Written in
              </div>
              <div className="space-y-1">
                {reg.writes.map((s) => (
                  <Site key={`w-${s.id}`} site={s} kind="write" />
                ))}
                {reg.writes.length === 0 && <div className="text-xs text-slate-600">none</div>}
              </div>
            </div>

            <div className="mt-5">
              <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-sky-300">
                Read in
              </div>
              <div className="space-y-1">
                {reg.reads.map((s) => (
                  <Site key={`r-${s.id}`} site={s} kind="read" />
                ))}
                {reg.reads.length === 0 && <div className="text-xs text-slate-600">none</div>}
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
