import { useEffect, useState } from 'react';
import { api } from '../lib/api.js';

export default function SkeletonEditor({ project, onClose, onSaved }) {
  const [content, setContent] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    (async () => {
      try {
        const { content: c } = await api.getSkeleton(project.id);
        setContent(c);
      } catch (err) {
        setError(String(err.message));
      } finally {
        setLoading(false);
      }
    })();
  }, [project.id]);

  const save = async () => {
    setSaving(true);
    setError('');
    try {
      await api.saveSkeleton(project.id, content);
      onSaved?.();
    } catch (err) {
      setError(String(err.message));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-8">
      <div className="card flex h-full max-h-[85vh] w-full max-w-4xl flex-col">
        <div className="flex items-center justify-between border-b border-ink-600 px-5 py-3">
          <div>
            <div className="text-sm font-semibold text-foreground">Skeleton editor</div>
            <div className="text-xs text-slate-500">
              The lifecycle stage breakdown that drives Phase 2. Edit, then re-run Classify stages.
            </div>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-foreground">
            ✕
          </button>
        </div>

        <div className="min-h-0 flex-1 p-4">
          {loading ? (
            <div className="text-sm text-slate-500">Loading…</div>
          ) : (
            <textarea
              value={content}
              onChange={(e) => setContent(e.target.value)}
              spellCheck={false}
              className="code-font h-full w-full resize-none rounded-md border border-ink-600 bg-background p-3 text-xs leading-relaxed text-slate-200 outline-none focus:border-accent"
            />
          )}
        </div>

        <div className="flex items-center justify-between border-t border-ink-600 px-5 py-3">
          <span className="text-xs text-red-400">{error}</span>
          <div className="flex gap-2">
            <button onClick={onClose} className="btn-ghost">
              Cancel
            </button>
            <button onClick={save} disabled={saving || loading} className="btn-primary">
              {saving ? 'Saving…' : 'Save skeleton'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
