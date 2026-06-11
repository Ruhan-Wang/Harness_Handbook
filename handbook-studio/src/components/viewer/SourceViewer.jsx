import { useEffect, useRef, useState } from 'react';
import { api } from '../../lib/api.js';

// Lightweight source viewer: fetches a file and renders it with line numbers,
// highlighting + scrolling to a target line range. Avoids heavy editor deps.
export default function SourceViewer({ projectId, path, lineRange, onClose }) {
  const [content, setContent] = useState(null);
  const [error, setError] = useState('');
  const targetRef = useRef(null);

  useEffect(() => {
    setContent(null);
    setError('');
    if (!path) return;
    (async () => {
      try {
        const { content: c } = await api.file(projectId, path);
        setContent(c);
      } catch (err) {
        setError(String(err.message));
      }
    })();
  }, [projectId, path]);

  useEffect(() => {
    if (content && targetRef.current) {
      targetRef.current.scrollIntoView({ block: 'center' });
    }
  }, [content]);

  if (!path) return null;
  const [start, end] = lineRange || [];
  const lines = content ? content.split('\n') : [];

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-ink-600 px-4 py-2">
        <div className="code-font truncate text-xs text-slate-300">
          {path}
          {start ? `  :${start}${end && end !== start ? `-${end}` : ''}` : ''}
        </div>
        {onClose && (
          <button onClick={onClose} className="text-slate-400 hover:text-foreground">
            ✕
          </button>
        )}
      </div>
      <div className="min-h-0 flex-1 overflow-auto">
        {error && <div className="p-4 text-xs text-red-400">{error}</div>}
        {!content && !error && <div className="p-4 text-xs text-slate-500">Loading source…</div>}
        {content && (
          <table className="code-font w-full border-collapse text-[11px] leading-relaxed">
            <tbody>
              {lines.map((line, i) => {
                const ln = i + 1;
                const inRange = start && ln >= start && ln <= (end || start);
                return (
                  <tr
                    key={ln}
                    ref={inRange && ln === start ? targetRef : null}
                    className={inRange ? 'bg-accent/15' : ''}
                  >
                    <td className="select-none border-r border-ink-700 px-2 text-right text-slate-600">
                      {ln}
                    </td>
                    <td className="whitespace-pre px-3 text-slate-300">{line || ' '}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
