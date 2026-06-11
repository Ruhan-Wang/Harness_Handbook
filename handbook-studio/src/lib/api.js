async function jsonFetch(url, options = {}) {
  const res = await fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data?.error || `${res.status} ${res.statusText}`);
  }
  return data;
}

export const api = {
  pickFolder: () => jsonFetch('/api/projects/pick', { method: 'POST', body: '{}' }),
  cliStatus: () => jsonFetch('/api/cli/status'),
  cliSettings: (patch) =>
    jsonFetch('/api/cli/settings', { method: 'PUT', body: JSON.stringify(patch) }),
  cliLogin: (provider) =>
    jsonFetch('/api/cli/login', { method: 'POST', body: JSON.stringify({ provider }) }),
  cliPrompt: (prompt, model, provider) =>
    jsonFetch('/api/cli/prompt', {
      method: 'POST',
      body: JSON.stringify({ prompt, model, provider }),
    }),

  listProjects: () => jsonFetch('/api/projects'),
  addProject: (path, name) =>
    jsonFetch('/api/projects', { method: 'POST', body: JSON.stringify({ path, name }) }),
  removeProject: (id) => jsonFetch(`/api/projects/${id}`, { method: 'DELETE' }),

  artifacts: (id) => jsonFetch(`/api/handbook/${id}/artifacts`),
  file: (id, path) => jsonFetch(`/api/handbook/${id}/file?path=${encodeURIComponent(path)}`),
  getSkeleton: (id) => jsonFetch(`/api/handbook/${id}/skeleton`),
  saveSkeleton: (id, content) =>
    jsonFetch(`/api/handbook/${id}/skeleton`, {
      method: 'PUT',
      body: JSON.stringify({ content }),
    }),
};
