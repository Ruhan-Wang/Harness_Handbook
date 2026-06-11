import { useState } from 'react';
import { api } from '../lib/api.js';

export default function ProjectSidebar({ projects, selectedId, onSelect, onChange }) {
  const [manual, setManual] = useState(false);
  const [path, setPath] = useState('');
  const [name, setName] = useState('');
  const [error, setError] = useState('');
  const [picking, setPicking] = useState(false);

  const register = async (folderPath, displayName) => {
    const { project } = await api.addProject(folderPath.trim(), (displayName || '').trim() || undefined);
    setPath('');
    setName('');
    setManual(false);
    await onChange();
    onSelect(project.id);
  };

  const openPicker = async () => {
    setError('');
    setPicking(true);
    try {
      const res = await api.pickFolder();
      if (res.canceled) return;
      if (res.unsupported) {
        setManual(true);
        return;
      }
      if (res.path) await register(res.path);
    } catch (err) {
      // Fall back to manual entry if the native dialog is unavailable.
      setManual(true);
      setError(String(err.message));
    } finally {
      setPicking(false);
    }
  };

  const submit = async (e) => {
    e.preventDefault();
    setError('');
    try {
      await register(path, name);
    } catch (err) {
      setError(String(err.message));
    }
  };

  const remove = async (e, id) => {
    e.stopPropagation();
    await api.removeProject(id);
    if (selectedId === id) onSelect(null);
    onChange();
  };

  return (
    <aside className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center justify-between px-4 py-3">
        <span className="text-sm font-semibold text-slate-200">Repositories</span>
        <button
          className="btn-ghost px-2 py-1 text-xs"
          onClick={openPicker}
          disabled={picking}
        >
          {picking ? 'Opening…' : '+ Open'}
        </button>
      </div>

      {manual && (
        <form onSubmit={submit} className="space-y-2 border-b border-ink-600 px-4 pb-3">
          <div className="text-[11px] text-slate-500">
            Native picker unavailable — enter a path manually.
          </div>
          <input
            autoFocus
            value={path}
            onChange={(e) => setPath(e.target.value)}
            placeholder="/absolute/path/to/repo"
            className="w-full rounded border border-ink-600 bg-ink-900 px-2 py-1.5 text-xs text-slate-200 outline-none focus:border-accent"
          />
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Display name (optional)"
            className="w-full rounded border border-ink-600 bg-ink-900 px-2 py-1.5 text-xs text-slate-200 outline-none focus:border-accent"
          />
          <div className="flex gap-2">
            <button type="submit" className="btn-primary flex-1 justify-center text-xs">
              Add repository
            </button>
            <button
              type="button"
              className="btn-ghost justify-center text-xs"
              onClick={() => setManual(false)}
            >
              Cancel
            </button>
          </div>
          {error && <div className="text-xs text-red-400">{error}</div>}
        </form>
      )}
      {!manual && error && <div className="px-4 pb-2 text-xs text-red-400">{error}</div>}

      <div className="min-h-0 flex-1 overflow-y-auto px-2 py-2">
        {projects.length === 0 && (
          <div className="px-3 py-6 text-center text-xs text-slate-500">
            No repos yet. Click "+ Open" to pick a folder.
          </div>
        )}
        {projects.map((p) => (
          <button
            key={p.id}
            onClick={() => onSelect(p.id)}
            className={`group mb-1 flex w-full items-center justify-between rounded-md px-3 py-2 text-left transition-colors ${
              selectedId === p.id ? 'bg-accent/20 ring-1 ring-accent/40' : 'hover:bg-ink-700'
            }`}
          >
            <span className="min-w-0">
              <span className="block truncate text-sm font-medium text-slate-200">{p.name}</span>
              <span className="block truncate text-[11px] text-slate-500">{p.path}</span>
              <span className="mt-1 flex gap-1">
                {p.hasGraph && <span className="chip bg-sky-500/20 text-sky-300">graph</span>}
                {p.hasSkeleton && <span className="chip bg-amber-500/20 text-amber-300">skeleton</span>}
                {p.hasHandbook && <span className="chip bg-emerald-500/20 text-emerald-300">handbook</span>}
                {!p.exists && <span className="chip bg-red-500/20 text-red-300">missing</span>}
              </span>
            </span>
            <span
              onClick={(e) => remove(e, p.id)}
              className="ml-2 hidden rounded px-1 text-xs text-slate-500 hover:text-red-400 group-hover:block"
              title="Remove from list"
            >
              ✕
            </span>
          </button>
        ))}
      </div>
    </aside>
  );
}
