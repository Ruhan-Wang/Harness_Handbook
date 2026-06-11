import { useEffect, useMemo, useState } from 'react';
import Markdown from '../Markdown.jsx';
import { projectRelPath, stageColor, stageTitle } from '../../lib/handbook.js';

export default function StageNavigator({ artifacts, order, onOpenSource, onSelectNode, onActiveChange }) {
  const stages = artifacts?.handbook?.stages || {};
  const stageOrder = order && order.length ? order : Object.keys(stages);
  const [active, setActive] = useState(stageOrder[0] || null);

  useEffect(() => {
    onActiveChange?.(active);
  }, [active, onActiveChange]);

  const members = useMemo(() => {
    const mp = artifacts?.mapping?.stages?.[active]?.members || [];
    return mp;
  }, [artifacts, active]);

  if (stageOrder.length === 0) {
    return (
      <div className="flex h-full items-center justify-center text-slate-500">
        No stages yet. Run Phase 3 (Write docs).
      </div>
    );
  }

  const stage = stages[active];

  return (
    <div className="flex h-full min-h-0">
      <div className="w-64 overflow-y-auto border-r border-ink-600 p-2">
        {stageOrder.map((sid) => (
          <button
            key={sid}
            onClick={() => setActive(sid)}
            className={`mb-1 flex w-full items-center gap-2 rounded-md px-3 py-2 text-left text-sm transition-colors ${
              active === sid ? 'bg-ink-700 text-foreground' : 'text-slate-300 hover:bg-ink-700/50'
            }`}
          >
            <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: stageColor(sid, order) }} />
            <span className="min-w-0">
              <span className="block truncate">{stageTitle(artifacts, sid)}</span>
              <span className="block truncate text-[11px] text-slate-500">{sid}</span>
            </span>
          </button>
        ))}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-5">
        {stage ? (
          <>
            <h2 className="mb-3 text-lg font-semibold text-foreground">
              {stage.chapter ? `${stage.chapter} · ` : ''}
              {stage.title || active}
            </h2>
            <Markdown>{stage.logical_md || ''}</Markdown>

            {members.length > 0 && (
              <div className="mt-6">
                <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
                  Functions in this stage ({members.length})
                </div>
                <div className="space-y-1">
                  {members.map((m) => (
                    <div
                      key={m.qualname + (m.line_range || []).join('-')}
                      className="card flex items-center justify-between px-3 py-2"
                    >
                      <button
                        onClick={() => onSelectNode?.(m.qualname)}
                        className="code-font min-w-0 truncate text-left text-xs text-slate-200 hover:text-accent"
                        title={m.purpose}
                      >
                        {m.qualname}
                        {m.type === 'region' && (
                          <span className="ml-2 chip bg-ink-700 text-slate-400">region</span>
                        )}
                      </button>
                      {m.line_range && (
                        <button
                          className="ml-2 shrink-0 text-[11px] text-slate-500 hover:text-accent"
                          onClick={() =>
                            onOpenSource(projectRelPath(artifacts, m.file), m.line_range)
                          }
                        >
                          {m.file}:{m.line_range[0]}
                        </button>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}
          </>
        ) : (
          <div className="text-slate-500">Select a stage.</div>
        )}
      </div>
    </div>
  );
}
