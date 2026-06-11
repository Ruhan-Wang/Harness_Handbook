// Read + write handbook artifacts living under <repo>/.handbook/
import express from 'express';
import fs from 'fs';
import path from 'path';
import yaml from 'js-yaml';
import { getProject } from '../projects.js';

const router = express.Router();

function hb(project) {
  return path.join(project.path, '.handbook');
}

function safeRead(file) {
  try {
    return fs.readFileSync(file, 'utf8');
  } catch {
    return null;
  }
}

function safeJson(file) {
  const raw = safeRead(file);
  if (raw == null) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

// Summary of which artifacts exist and parsed payloads for the viewer.
router.get('/api/handbook/:id/artifacts', (req, res) => {
  const project = getProject(req.params.id);
  if (!project) {
    res.status(404).json({ error: 'project not found' });
    return;
  }
  const root = hb(project);
  const graph = safeJson(path.join(root, 'phase1', 'graph.json'));
  const dropped = safeJson(path.join(root, 'phase1', 'dropped_calls.json'));
  const handbook =
    safeJson(path.join(root, 'phase3', 'output', 'handbook_en.json')) ||
    safeJson(path.join(root, 'phase3', 'output', 'handbook.json'));
  const mappingRaw = safeRead(path.join(root, 'phase2', 'iterations', 'final', 'mapping.yaml'))
    || safeRead(path.join(root, 'phase2', 'mapping.yaml'));
  let mapping = null;
  if (mappingRaw != null) {
    try {
      mapping = yaml.load(mappingRaw);
    } catch {
      mapping = null;
    }
  }
  const skeletonRaw = safeRead(path.join(root, 'phase2', 'skeleton.yaml'));
  let skeleton = null;
  if (skeletonRaw != null) {
    try {
      skeleton = yaml.load(skeletonRaw);
    } catch {
      skeleton = null;
    }
  }

  const meta = safeJson(path.join(root, 'meta.json')) || { sourceRootRel: '.' };

  const references = {};
  const refDir = path.join(root, 'references');
  for (const name of ['overview.md', 'index.md', 'registers.md']) {
    references[name] = safeRead(path.join(refDir, name));
  }
  let stages = [];
  const stagesDir = path.join(refDir, 'stages');
  if (fs.existsSync(stagesDir)) {
    stages = fs.readdirSync(stagesDir).filter((f) => f.endsWith('.md'));
  }

  res.json({
    project: { id: project.id, name: project.name, path: project.path },
    present: {
      graph: !!graph,
      mapping: !!mapping,
      skeleton: !!skeleton,
      handbook: !!handbook,
      references: Object.values(references).some(Boolean),
    },
    meta,
    graph,
    dropped,
    handbook,
    mapping,
    skeleton,
    references,
    stages,
  });
});

// Raw file content. Allows reading source files (for the register explorer) and
// handbook markdown. Restricted to inside the project directory.
router.get('/api/handbook/:id/file', (req, res) => {
  const project = getProject(req.params.id);
  if (!project) {
    res.status(404).json({ error: 'project not found' });
    return;
  }
  const rel = String(req.query.path || '');
  const abs = path.resolve(project.path, rel);
  if (!abs.startsWith(path.resolve(project.path))) {
    res.status(400).json({ error: 'path escapes project' });
    return;
  }
  const content = safeRead(abs);
  if (content == null) {
    res.status(404).json({ error: 'file not found', path: rel });
    return;
  }
  res.json({ path: rel, content });
});

router.get('/api/handbook/:id/skeleton', (req, res) => {
  const project = getProject(req.params.id);
  if (!project) {
    res.status(404).json({ error: 'project not found' });
    return;
  }
  const content = safeRead(path.join(hb(project), 'phase2', 'skeleton.yaml'));
  res.json({ content: content || '' });
});

router.put('/api/handbook/:id/skeleton', (req, res) => {
  const project = getProject(req.params.id);
  if (!project) {
    res.status(404).json({ error: 'project not found' });
    return;
  }
  const { content } = req.body || {};
  if (typeof content !== 'string') {
    res.status(400).json({ error: 'content required' });
    return;
  }
  try {
    yaml.load(content); // validate
  } catch (err) {
    res.status(400).json({ error: `invalid YAML: ${err.message}` });
    return;
  }
  const dir = path.join(hb(project), 'phase2');
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, 'skeleton.yaml'), content);
  res.json({ ok: true });
});

export default router;
