import { useEffect, useState } from 'react';
import { api } from '../lib/api.js';

// Lives in the left sidebar. Shows which coding CLIs are installed, lets the user
// pick the active provider + model, and log in. Mirrors dr-claw's multi-CLI model.
export default function CliConnector({ onActiveChange }) {
  const [providers, setProviders] = useState([]);
  const [settings, setSettings] = useState({ provider: 'auto', model: '' });
  const [activeId, setActiveId] = useState(null);
  const [loading, setLoading] = useState(true);
  const [loginInfo, setLoginInfo] = useState(null);
  const [loggingIn, setLoggingIn] = useState(false);

  const refresh = async () => {
    setLoading(true);
    try {
      const s = await api.cliStatus();
      setProviders(s.providers || []);
      setSettings(s.settings || { provider: 'auto', model: '' });
      setActiveId(s.active || null);
      onActiveChange?.(s.active || null);
    } catch {
      setProviders([]);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  const updateProvider = async (provider) => {
    setSettings((s) => ({ ...s, provider, model: '' }));
    const { settings: next } = await api.cliSettings({ provider, model: '' });
    setSettings(next);
    refresh();
  };

  const updateModel = async (model) => {
    setSettings((s) => ({ ...s, model }));
    await api.cliSettings({ model });
  };

  const saveInternal = async (patch) => {
    setSettings((s) => ({ ...s, ...patch }));
    await api.cliSettings(patch);
    refresh();
  };

  const login = async () => {
    setLoginInfo(null);
    setLoggingIn(true);
    try {
      const res = await api.cliLogin(settings.provider === 'auto' ? activeId : settings.provider);
      setLoginInfo(res);
      if (res.authUrl) window.open(res.authUrl, '_blank');
      setTimeout(refresh, 3000);
    } catch (err) {
      setLoginInfo({ error: String(err.message) });
    } finally {
      setLoggingIn(false);
    }
  };

  const anyFound = providers.some((p) => p.found);
  const chosenId = settings.provider === 'auto' ? activeId : settings.provider;
  const chosenProvider = providers.find((p) => p.id === chosenId);
  const activeProvider = providers.find((p) => p.id === activeId);
  const isHttp = chosenProvider?.kind === 'http';
  const loggedIn = activeProvider?.loggedIn;

  const dotColor = loading
    ? 'bg-slate-500'
    : !activeProvider
      ? 'bg-red-500'
      : loggedIn === true
        ? 'bg-emerald-500'
        : loggedIn === false
          ? 'bg-amber-500'
          : 'bg-sky-500';

  const authLabel =
    loggedIn === true ? 'connected' : loggedIn === false ? 'not logged in' : 'login unknown';

  return (
    <div className="border-t border-ink-600 p-3">
      <div className="mb-2 flex items-center justify-between">
        <span className="text-xs font-semibold uppercase tracking-wide text-slate-500">
          AI provider
        </span>
        <button onClick={refresh} className="text-xs text-slate-500 hover:text-slate-300" title="Refresh">
          ↻
        </button>
      </div>

      <div className="mb-1 flex items-center gap-2">
        <span className={`h-2 w-2 shrink-0 rounded-full ${dotColor}`} />
        <span className="min-w-0 truncate text-xs text-slate-300">
          {loading
            ? 'detecting…'
            : activeProvider
              ? `${activeProvider.label}${activeProvider.version ? ` · ${activeProvider.version}` : ''}`
              : 'no CLI detected'}
        </span>
      </div>
      {!loading && activeProvider && (
        <div
          className={`mb-2 text-[11px] ${
            loggedIn === true
              ? 'text-emerald-400'
              : loggedIn === false
                ? 'text-amber-400'
                : 'text-slate-500'
          }`}
        >
          {authLabel}
        </div>
      )}

      <label className="mb-1 block text-[11px] text-slate-500">Provider</label>
      <select
        value={settings.provider}
        onChange={(e) => updateProvider(e.target.value)}
        className="mb-2 w-full rounded border border-ink-600 bg-ink-900 px-2 py-1.5 text-xs text-slate-200 outline-none"
      >
        <option value="auto">Auto (first available)</option>
        {providers.map((p) => (
          <option key={p.id} value={p.id} disabled={!p.found && p.kind !== 'http'}>
            {p.label}
            {p.found ? '' : p.kind === 'http' ? ' (needs setup)' : ' (not installed)'}
          </option>
        ))}
      </select>

      {isHttp ? (
        <div className="mb-2 space-y-1.5 rounded border border-ink-600 bg-ink-900 p-2">
          <div className="text-[11px] text-slate-500">Internal data_eval endpoint (HMAC)</div>
          <input
            value={settings.internalHost || ''}
            onChange={(e) => setSettings((s) => ({ ...s, internalHost: e.target.value }))}
            onBlur={(e) => saveInternal({ internalHost: e.target.value })}
            placeholder="host (e.g. trpc-gpt-eval.production.polaris)"
            className="w-full rounded border border-ink-600 bg-background px-2 py-1 text-[11px] text-slate-200 outline-none focus:border-accent"
          />
          <input
            value={settings.internalPort ?? 8080}
            onChange={(e) => setSettings((s) => ({ ...s, internalPort: e.target.value }))}
            onBlur={(e) => saveInternal({ internalPort: e.target.value })}
            placeholder="port (8080)"
            className="w-full rounded border border-ink-600 bg-background px-2 py-1 text-[11px] text-slate-200 outline-none focus:border-accent"
          />
          <input
            value={settings.internalUser || ''}
            onChange={(e) => setSettings((s) => ({ ...s, internalUser: e.target.value }))}
            onBlur={(e) => saveInternal({ internalUser: e.target.value })}
            placeholder="secretId / user"
            className="w-full rounded border border-ink-600 bg-background px-2 py-1 text-[11px] text-slate-200 outline-none focus:border-accent"
          />
          <input
            type="password"
            value={settings.internalKey || ''}
            onChange={(e) => setSettings((s) => ({ ...s, internalKey: e.target.value }))}
            onBlur={(e) => saveInternal({ internalKey: e.target.value })}
            placeholder="secretKey"
            className="w-full rounded border border-ink-600 bg-background px-2 py-1 text-[11px] text-slate-200 outline-none focus:border-accent"
          />
          <select
            value={settings.internalModel || ''}
            onChange={(e) => saveInternal({ internalModel: e.target.value })}
            className="w-full rounded border border-ink-600 bg-background px-2 py-1 text-[11px] text-slate-200 outline-none"
          >
            {(chosenProvider?.models || []).map((m) => (
              <option key={m.value} value={m.value}>
                {m.label}
              </option>
            ))}
            {settings.internalModel &&
              !(chosenProvider?.models || []).some((m) => m.value === settings.internalModel) && (
                <option value={settings.internalModel}>{settings.internalModel}</option>
              )}
          </select>
        </div>
      ) : (
        <>
          <label className="mb-1 block text-[11px] text-slate-500">Model</label>
          <select
            value={settings.model}
            onChange={(e) => updateModel(e.target.value)}
            disabled={!activeProvider}
            className="mb-2 w-full rounded border border-ink-600 bg-ink-900 px-2 py-1.5 text-xs text-slate-200 outline-none disabled:opacity-50"
          >
            <option value="">CLI default</option>
            {(activeProvider?.models || []).map((m) => (
              <option key={m.value} value={m.value}>
                {m.label}
              </option>
            ))}
          </select>

          <button
            onClick={login}
            disabled={!anyFound || loggingIn}
            className="btn-ghost w-full justify-center text-xs"
          >
            {loggingIn ? 'Starting…' : `Log in ${activeProvider ? `(${activeProvider.label})` : ''}`}
          </button>
        </>
      )}

      {loginInfo && (
        <div className="mt-2 rounded border border-ink-600 bg-ink-900 p-2 text-[11px]">
          {loginInfo.error && <div className="text-red-400">{loginInfo.error}</div>}
          {loginInfo.authUrl && (
            <a
              href={loginInfo.authUrl}
              target="_blank"
              rel="noreferrer"
              className="block truncate text-accent underline"
            >
              Open auth link in browser
            </a>
          )}
          {!loginInfo.authUrl && !loginInfo.error && (
            <div className="space-y-1 text-slate-400">
              <div>{loginInfo.hint || 'Run this in a terminal to sign in:'}</div>
              {loginInfo.command && (
                <code className="block select-all rounded bg-ink-700 px-1.5 py-1 text-slate-200">
                  {loginInfo.command}
                </code>
              )}
              <button onClick={refresh} className="text-accent underline">
                I&apos;ve logged in — recheck
              </button>
            </div>
          )}
        </div>
      )}

      <div className="mt-2 flex flex-wrap gap-1">
        {providers.map((p) => {
          const cls = !p.found
            ? 'bg-ink-700 text-slate-500'
            : p.loggedIn === true
              ? 'bg-emerald-500/15 text-emerald-300'
              : p.loggedIn === false
                ? 'bg-amber-500/15 text-amber-300'
                : 'bg-sky-500/15 text-sky-300';
          const title = !p.found
            ? 'not installed'
            : p.loggedIn === true
              ? `${p.command} · connected`
              : p.loggedIn === false
                ? `${p.command} · not logged in`
                : `${p.command} · login unknown`;
          return (
            <span key={p.id} className={`chip ${cls}`} title={title}>
              {p.label}
            </span>
          );
        })}
      </div>
    </div>
  );
}
