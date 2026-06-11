import express from 'express';
import os from 'os';
import { execFile } from 'child_process';
import { listProjects, addProject, removeProject, getProject } from '../projects.js';
import { ensureHandbookIntro } from '../handbook-intro.js';

const router = express.Router();

router.get('/api/projects', (_req, res) => {
  res.json({ projects: listProjects() });
});

// Open the OS-native folder picker (macOS Finder via osascript) and return the
// chosen absolute path, mirroring dr-claw's native dialog.
router.post('/api/projects/pick', (_req, res) => {
  if (os.platform() !== 'darwin') {
    res.status(501).json({ error: 'native folder picker only available on macOS', unsupported: true });
    return;
  }
  const script =
    'POSIX path of (choose folder with prompt "Select a code repository to generate a handbook for")';
  execFile('osascript', ['-e', script], (err, stdout, stderr) => {
    if (err) {
      if (/User canceled|-128/.test(stderr || '')) {
        res.json({ canceled: true });
        return;
      }
      res.status(500).json({ error: String((stderr || err.message).trim()) });
      return;
    }
    res.json({ path: stdout.trim().replace(/\/$/, '') });
  });
});

router.post('/api/projects', (req, res) => {
  try {
    const { path: projectPath, name } = req.body || {};
    if (!projectPath) {
      res.status(400).json({ error: 'path required' });
      return;
    }
    const project = addProject(projectPath, name);
    let intro = null;
    try {
      intro = ensureHandbookIntro(project);
    } catch {
      intro = null;
    }
    res.json({ project, intro });
  } catch (err) {
    res.status(400).json({ error: String(err?.message || err) });
  }
});

router.delete('/api/projects/:id', (req, res) => {
  if (!getProject(req.params.id)) {
    res.status(404).json({ error: 'not found' });
    return;
  }
  removeProject(req.params.id);
  res.json({ ok: true });
});

export default router;
