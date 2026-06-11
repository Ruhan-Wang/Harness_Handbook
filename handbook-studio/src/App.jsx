import { useCallback, useEffect, useState } from 'react';
import { api } from './lib/api.js';
import { onRunComplete } from './lib/threadStore.js';
import ProjectSidebar from './components/ProjectSidebar.jsx';
import CliConnector from './components/CliConnector.jsx';
import Workspace from './components/Workspace.jsx';
import HandbookViewer from './components/viewer/HandbookViewer.jsx';

function useTheme() {
  const [theme, setTheme] = useState(
    () => localStorage.getItem('hs-theme') || 'dark'
  );
  useEffect(() => {
    document.documentElement.classList.toggle('dark', theme !== 'light');
    localStorage.setItem('hs-theme', theme);
  }, [theme]);
  return [theme, setTheme];
}

export default function App() {
  const [projects, setProjects] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [artifacts, setArtifacts] = useState(null);
  const [activeProvider, setActiveProvider] = useState(null);
  const [tab, setTab] = useState('workspace');
  const [splitPct, setSplitPct] = useState(55);
  const [focus, setFocus] = useState(null); // { kind, id, label, text } from the viewer
  const [theme, setTheme] = useTheme();

  const startDrag = useCallback((e) => {
    e.preventDefault();
    const container = e.currentTarget.parentElement;
    const onMove = (ev) => {
      const rect = container.getBoundingClientRect();
      let pct = ((ev.clientX - rect.left) / rect.width) * 100;
      pct = Math.min(75, Math.max(25, pct));
      setSplitPct(pct);
    };
    const onUp = () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      document.body.style.userSelect = '';
    };
    document.body.style.userSelect = 'none';
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  }, []);

  const refreshProjects = useCallback(async () => {
    const { projects: list } = await api.listProjects();
    setProjects(list);
    return list;
  }, []);

  const refreshArtifacts = useCallback(async (id) => {
    if (!id) {
      setArtifacts(null);
      return;
    }
    try {
      setArtifacts(await api.artifacts(id));
    } catch {
      setArtifacts(null);
    }
  }, []);

  useEffect(() => {
    refreshProjects();
  }, [refreshProjects]);

  useEffect(() => {
    refreshArtifacts(selectedId);
    setFocus(null);
  }, [selectedId, refreshArtifacts]);

  // Refresh artifacts whenever a pipeline run finishes for any project; if it's
  // the one currently selected, the viewer/badges update immediately.
  useEffect(
    () =>
      onRunComplete((pid) => {
        if (pid === selectedId) refreshArtifacts(selectedId);
      }),
    [selectedId, refreshArtifacts]
  );

  const selected = projects.find((p) => p.id === selectedId) || null;
  const hasHandbook = artifacts?.present?.handbook;
  const canViewer = hasHandbook || artifacts?.present?.graph;

  const workspaceEl = selected && (
    <Workspace
      project={selected}
      artifacts={artifacts}
      focus={focus}
      onClearFocus={() => setFocus(null)}
      onArtifactsChange={() => refreshArtifacts(selectedId)}
      onOpenViewer={() => setTab('split')}
    />
  );
  const viewerEl = selected && (
    <HandbookViewer
      project={selected}
      artifacts={artifacts}
      activeProvider={activeProvider}
      onFocusChange={setFocus}
    />
  );

  return (
    <div className="flex h-full w-full overflow-hidden bg-background text-foreground">
      <div className="flex w-72 shrink-0 flex-col border-r border-border bg-card">
        <ProjectSidebar
          projects={projects}
          selectedId={selectedId}
          onSelect={setSelectedId}
          onChange={refreshProjects}
        />
        <CliConnector onActiveChange={setActiveProvider} />
      </div>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-between border-b border-border bg-card px-5 py-3">
          <div className="flex min-w-0 items-center gap-3">
            <div
              className="h-9 w-9 shrink-0 rounded-xl bg-white shadow-sm ring-1 ring-border"
              style={{
                backgroundImage: 'url(/logo.png)',
                backgroundRepeat: 'no-repeat',
                backgroundSize: '165%',
                backgroundPosition: 'center 30%',
              }}
              title="Harness Handbook Hub"
            />
            <div className="min-w-0">
              <div className="brand-title text-lg">Harness Handbook Hub</div>
              {selected ? (
                <div className="code-font truncate text-xs text-muted-foreground">{selected.path}</div>
              ) : (
                <div className="text-xs text-muted-foreground">
                  Generate and explore code handbooks with your coding CLI.
                </div>
              )}
            </div>
          </div>
          <button
            onClick={() => setTheme(theme === 'light' ? 'dark' : 'light')}
            className="btn-ghost px-3 py-1.5 text-xs"
            title={`Switch to ${theme === 'light' ? 'dark' : 'light'} mode`}
          >
            {theme === 'light' ? '\u263d Dark' : '\u2600 Light'}
          </button>
        </header>

        {!selected ? (
          <div className="flex flex-1 items-center justify-center text-muted-foreground">
            <div className="text-center">
              <div className="text-lg font-medium text-foreground">No repository selected</div>
              <div className="mt-1 text-sm">
                Open a code repo from the left to generate or explore its handbook.
              </div>
            </div>
          </div>
        ) : (
          <>
            <nav className="flex gap-1 border-b border-border bg-card px-4">
              {[
                { id: 'workspace', label: 'Workspace' },
                { id: 'viewer', label: 'Handbook', disabled: !canViewer },
                { id: 'split', label: 'Split view', disabled: !canViewer },
              ].map((t) => (
                <button
                  key={t.id}
                  disabled={t.disabled}
                  onClick={() => setTab(t.id)}
                  className={`-mb-px border-b-2 px-4 py-2 text-sm font-medium transition-colors disabled:opacity-30 ${
                    tab === t.id
                      ? 'border-primary text-foreground'
                      : 'border-transparent text-muted-foreground hover:text-foreground'
                  }`}
                >
                  {t.label}
                </button>
              ))}
            </nav>

            <main className="min-h-0 flex-1 overflow-hidden">
              {tab === 'workspace' && <div className="h-full">{workspaceEl}</div>}
              {tab === 'viewer' && <div className="h-full">{viewerEl}</div>}
              {tab === 'split' && (
                <div className="flex h-full min-h-0">
                  <div
                    style={{ width: `calc(${splitPct}% - 3px)` }}
                    className="min-w-0 min-h-0 overflow-hidden border-r border-border"
                  >
                    {workspaceEl}
                  </div>
                  <div
                    onMouseDown={startDrag}
                    className="w-1.5 shrink-0 cursor-col-resize bg-border transition-colors hover:bg-primary"
                    title="Drag to resize"
                  />
                  <div
                    style={{ width: `calc(${100 - splitPct}% - 3px)` }}
                    className="min-w-0 min-h-0 overflow-hidden"
                  >
                    {viewerEl}
                  </div>
                </div>
              )}
            </main>
          </>
        )}
      </div>
    </div>
  );
}
