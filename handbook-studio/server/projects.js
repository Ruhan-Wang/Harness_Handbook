// Lightweight project registry persisted to ~/.handbook-studio/projects.json
import fs from 'fs';
import path from 'path';
import os from 'os';
import crypto from 'crypto';

const STORE_DIR = path.join(os.homedir(), '.handbook-studio');
const STORE_FILE = path.join(STORE_DIR, 'projects.json');

function ensureStore() {
  if (!fs.existsSync(STORE_DIR)) fs.mkdirSync(STORE_DIR, { recursive: true });
  if (!fs.existsSync(STORE_FILE)) fs.writeFileSync(STORE_FILE, JSON.stringify({ projects: [] }, null, 2));
}

function readStore() {
  ensureStore();
  try {
    return JSON.parse(fs.readFileSync(STORE_FILE, 'utf8'));
  } catch {
    return { projects: [] };
  }
}

function writeStore(data) {
  ensureStore();
  fs.writeFileSync(STORE_FILE, JSON.stringify(data, null, 2));
}

function idFor(absPath) {
  return crypto.createHash('md5').update(absPath).digest('hex').slice(0, 12);
}

export function listProjects() {
  const store = readStore();
  return store.projects.map((p) => ({
    ...p,
    exists: fs.existsSync(p.path),
    hasHandbook:
      fs.existsSync(path.join(p.path, '.handbook', 'phase3', 'output', 'handbook_en.json')) ||
      fs.existsSync(path.join(p.path, '.handbook', 'phase3', 'output', 'handbook.json')),
    hasGraph: fs.existsSync(path.join(p.path, '.handbook', 'phase1', 'graph.json')),
    hasSkeleton: fs.existsSync(path.join(p.path, '.handbook', 'phase2', 'skeleton.yaml')),
  }));
}

export function addProject(rawPath, name) {
  const absPath = path.resolve(rawPath.replace(/^~(?=$|\/|\\)/, os.homedir()));
  if (!fs.existsSync(absPath) || !fs.statSync(absPath).isDirectory()) {
    throw new Error(`Not a directory: ${absPath}`);
  }
  const store = readStore();
  const id = idFor(absPath);
  if (store.projects.some((p) => p.id === id)) {
    return store.projects.find((p) => p.id === id);
  }
  const project = {
    id,
    path: absPath,
    name: name || path.basename(absPath),
    addedAt: new Date().toISOString(),
  };
  store.projects.push(project);
  writeStore(store);
  return project;
}

export function removeProject(id) {
  const store = readStore();
  store.projects = store.projects.filter((p) => p.id !== id);
  writeStore(store);
}

export function getProject(id) {
  return readStore().projects.find((p) => p.id === id) || null;
}
