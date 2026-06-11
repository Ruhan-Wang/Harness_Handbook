import { useMemo } from 'react';
import Markdown from '../Markdown.jsx';
import { buildPurposeMap, buildTranslationMap, projectRelPath, stageColor, stageTitle } from '../../lib/handbook.js';

function shortName(id) {
  if (!id) return id;
  return id.replace(/^boundary:/, '').replace(/^unresolved:/, '');
}

export default function FunctionCard({ artifacts, nodeId, nodeStageMap, order, onOpenSource }) {
  const node = artifacts?.graph?.nodes?.[nodeId];
  const purposeMap = useMemo(() => buildPurposeMap(artifacts?.mapping), [artifacts]);
  const translationMap = useMemo(() => buildTranslationMap(artifacts?.handbook), [artifacts]);

  const { callers, callees } = useMemo(() => {
    const edges = artifacts?.graph?.edges || [];
    const callersList = [];
    const calleesList = [];
    for (const e of edges) {
      if (e.callee_id === nodeId) callersList.push(e.caller_id);
      if (e.caller_id === nodeId) calleesList.push(e.callee_id);
    }
    return { callers: [...new Set(callersList)], callees: [...new Set(calleesList)] };
  }, [artifacts, nodeId]);

  if (!node) {
    return <div className="p-4 text-sm text-slate-500">Select a node to see details.</div>;
  }

  const sid = nodeStageMap[nodeId];
  const purpose = purposeMap[nodeId];
  const translation = translationMap[nodeId]?.translation;
  const filePath = projectRelPath(artifacts, node.file);
  const lineRange = node.line_start ? [node.line_start, node.line_end] : null;

  return (
    <div className="flex h-full flex-col overflow-y-auto p-4">
      <div className="mb-1 flex items-center gap-2">
        {sid && (
          <span
            className="chip text-white"
            style={{ backgroundColor: stageColor(sid, order) + '40', color: stageColor(sid, order) }}
          >
            {stageTitle(artifacts, sid)}
          </span>
        )}
        {node.is_async && <span className="chip bg-purple-500/20 text-purple-300">async</span>}
        {node.kind === 'boundary' && <span className="chip bg-slate-500/20 text-slate-300">boundary</span>}
      </div>

      <div className="code-font break-all text-sm font-semibold text-foreground">{node.qualname}</div>
      {node.signature && (
        <div className="code-font mt-1 break-all text-[11px] text-slate-400">{node.signature}</div>
      )}

      <div className="mt-2 flex items-center gap-2 text-xs text-slate-500">
        <span className="code-font">{node.file}{lineRange ? `:${lineRange[0]}` : ''}</span>
        {filePath && lineRange && (
          <button
            className="btn-ghost px-2 py-0.5 text-[11px]"
            onClick={() => onOpenSource(filePath, lineRange)}
          >
            View source
          </button>
        )}
      </div>

      {purpose && (
        <div className="mt-3">
          <div className="text-xs font-semibold uppercase tracking-wide text-slate-500">Purpose</div>
          <p className="mt-1 text-xs text-slate-300">{purpose}</p>
        </div>
      )}

      {translation && (
        <div className="mt-3 border-t border-ink-600 pt-3">
          <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
            Handbook description
          </div>
          {typeof translation === 'string' ? (
            <Markdown>{translation}</Markdown>
          ) : (
            <div className="space-y-2">
              {Object.entries(translation)
                .filter(([k]) => !['schema_version', 'type'].includes(k))
                .map(([k, v]) => (
                  <div key={k}>
                    <div className="text-[11px] font-semibold capitalize text-slate-400">
                      {k.replace(/_/g, ' ')}
                    </div>
                    <div className="text-xs text-slate-300">
                      {typeof v === 'string' ? v : JSON.stringify(v)}
                    </div>
                  </div>
                ))}
            </div>
          )}
        </div>
      )}

      <div className="mt-3 grid grid-cols-2 gap-3 border-t border-ink-600 pt-3">
        <div>
          <div className="text-[11px] font-semibold uppercase text-slate-500">
            Callers ({callers.length})
          </div>
          <ul className="mt-1 space-y-0.5">
            {callers.slice(0, 30).map((c) => (
              <li key={c} className="code-font truncate text-[11px] text-sky-300" title={c}>
                {shortName(c)}
              </li>
            ))}
          </ul>
        </div>
        <div>
          <div className="text-[11px] font-semibold uppercase text-slate-500">
            Callees ({callees.length})
          </div>
          <ul className="mt-1 space-y-0.5">
            {callees.slice(0, 40).map((c) => (
              <li key={c} className="code-font truncate text-[11px] text-amber-300" title={c}>
                {shortName(c)}
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );
}
