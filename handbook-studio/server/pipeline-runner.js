// Orchestrates the handbook generation phases as child processes, streaming
// progress to a callback (wired to the WebSocket). All LLM traffic is routed to
// the local gateway via env vars consumed by the Python api_client.
import path from 'path';
import fs from 'fs';
import { spawn } from 'child_process';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const GENERATE_DIR =
  process.env.HANDBOOK_GENERATE_DIR ||
  path.resolve(__dirname, '../../Harness_Handbook/handbook_generate');
const STUDIO_PY = path.resolve(__dirname, '../python');
const PYTHON = process.env.HANDBOOK_PYTHON || 'python3';

const activeRuns = new Map(); // runKey -> child process

function handbookDir(project) {
  return path.join(project.path, '.handbook');
}

function resolveSourceRoot(project, sourceRoot) {
  if (!sourceRoot) return project.path;
  return path.isAbsolute(sourceRoot) ? sourceRoot : path.join(project.path, sourceRoot);
}

function baseEnv(project, sourceRoot, serverPort) {
  const hb = handbookDir(project);
  return {
    ...process.env,
    HANDBOOK_LLM_HOST: '127.0.0.1',
    HANDBOOK_LLM_PORT: String(serverPort),
    HANDBOOK_SOURCE_ROOT: resolveSourceRoot(project, sourceRoot),
    HANDBOOK_REPO_ROOT: hb,
    HANDBOOK_OUT: hb,
    HANDBOOK_PHASE1_OUT: path.join(hb, 'phase1'),
    HANDBOOK_PHASE2_DIR: path.join(hb, 'phase2'),
    HANDBOOK_PHASE2_FINAL: path.join(hb, 'phase2', 'iterations', 'final'),
    HANDBOOK_PHASE3_ROOT: path.join(hb, 'phase3'),
    PYTHONUNBUFFERED: '1',
    PYTHONPATH: [
      path.join(GENERATE_DIR, 'phase2'),
      path.join(GENERATE_DIR, 'phase3'),
      STUDIO_PY,
      process.env.PYTHONPATH || '',
    ]
      .filter(Boolean)
      .join(path.delimiter),
  };
}

// Map a quality mode to phase-2 CLI flags + cost-knob env vars.
//   thorough — full actor-critic (all passes, revise rounds, multi-iter)
//   fast     — Pass A + Phase 3 only, no revise rounds, 1 iteration
//   minimal  — fast + skip the critic entirely (actor-only, 1 call/item)
function modeConfig(mode) {
  if (mode === 'thorough') {
    return { phase2Args: [], env: {} };
  }
  const phase2Args = ['--no-pass-b', '--no-pass-c', '--no-pass-d', '--no-ordering', '--max-iters', '1'];
  const env = { HANDBOOK_MAX_REVISE_ROUNDS: '0', HANDBOOK_TIER_MAX_ROUNDS: '1' };
  if (mode === 'minimal') env.HANDBOOK_SKIP_CRITIC = '1';
  return { phase2Args, env };
}

function ensureDirs(project) {
  const hb = handbookDir(project);
  for (const sub of ['phase1', 'phase2', 'phase3']) {
    fs.mkdirSync(path.join(hb, sub), { recursive: true });
  }
}

function writeMeta(project, sourceRoot) {
  const hb = handbookDir(project);
  const abs = resolveSourceRoot(project, sourceRoot);
  let rel = path.relative(project.path, abs);
  if (rel === '' || rel.startsWith('..')) rel = '.';
  fs.writeFileSync(
    path.join(hb, 'meta.json'),
    JSON.stringify({ sourceRootRel: rel, sourceRootAbs: abs, updatedAt: new Date().toISOString() }, null, 2)
  );
}

function runChild(runKey, cmd, args, opts, send) {
  return new Promise((resolve, reject) => {
    send({ type: 'log', line: `$ ${cmd} ${args.join(' ')}` });
    const child = spawn(cmd, args, { ...opts, stdio: ['ignore', 'pipe', 'pipe'] });
    activeRuns.set(runKey, child);

    const onLine = (chunk) => {
      const text = chunk.toString();
      for (const line of text.split('\n')) {
        if (line.trim()) send({ type: 'log', line });
      }
    };
    child.stdout.on('data', onLine);
    child.stderr.on('data', onLine);

    child.on('error', (err) => {
      activeRuns.delete(runKey);
      reject(err);
    });
    child.on('close', (code) => {
      activeRuns.delete(runKey);
      if (code === 0) resolve();
      else reject(new Error(`process exited with code ${code}`));
    });
  });
}

async function runPhase1(project, env, runKey, send) {
  send({ type: 'step-start', step: 'phase1', label: 'Mapping call graph (AST)' });
  await runChild(
    runKey,
    PYTHON,
    [path.join(GENERATE_DIR, 'phase1', 'extract_graph.py')],
    { cwd: GENERATE_DIR, env },
    send
  );
  send({ type: 'step-done', step: 'phase1' });
}

async function runSkeletonDraft(project, env, runKey, send) {
  send({ type: 'step-start', step: 'skeleton', label: 'Drafting skeleton' });
  await runChild(
    runKey,
    PYTHON,
    [path.join(STUDIO_PY, 'run_skeleton_draft.py')],
    { cwd: GENERATE_DIR, env },
    send
  );
  send({ type: 'step-done', step: 'skeleton' });
}

async function runPhase2(project, env, runKey, send, sourceRoot, phase2Args = []) {
  send({ type: 'step-start', step: 'phase2', label: 'Classifying stages (actor-critic loop)' });
  const hb = handbookDir(project);
  const finalMapping = path.join(hb, 'phase2', 'iterations', 'final', 'mapping.yaml');
  try {
    await runChild(
      runKey,
      PYTHON,
      [
        path.join(GENERATE_DIR, 'phase2', 'iterate_phase2.py'),
        '--skeleton-yaml', path.join(hb, 'phase2', 'skeleton.yaml'),
        '--graph', path.join(hb, 'phase1', 'graph.json'),
        '--source-root', resolveSourceRoot(project, sourceRoot),
        '--mapping', path.join(hb, 'phase2', 'mapping.yaml'),
        '--iterations-dir', path.join(hb, 'phase2', 'iterations'),
        ...phase2Args,
      ],
      { cwd: GENERATE_DIR, env },
      send
    );
  } catch (err) {
    // iterate_phase2 returns a non-zero exit when it stops at the iteration cap
    // WITHOUT a converged state_hash (expected in Fast/Minimal mode, which caps
    // --max-iters at 1). It still finalizes a complete mapping to iterations/final,
    // so treat that as success and continue to Phase 3.
    const ok = fs.existsSync(finalMapping) && fs.statSync(finalMapping).size > 0;
    if (!ok) throw err;
    send({
      type: 'log',
      line: 'Phase 2 stopped at the iteration cap but wrote a complete final mapping — continuing to Write docs.',
    });
  }
  send({ type: 'step-done', step: 'phase2' });
}

async function runPhase3(project, env, runKey, send) {
  send({ type: 'step-start', step: 'phase3', label: 'Writing handbook documents' });
  const env3 = { ...env, HANDBOOK_PROJECT_NAME: project.name };
  await runChild(
    runKey,
    PYTHON,
    [path.join(GENERATE_DIR, 'phase3', 'assemble_doc.py'), '--lang', 'en'],
    { cwd: GENERATE_DIR, env: env3 },
    send
  );
  // Package references/ + SKILL.md into the repo's .handbook folder.
  await runChild(
    runKey,
    PYTHON,
    [path.join(STUDIO_PY, 'package_skill.py')],
    { cwd: GENERATE_DIR, env: env3 },
    send
  );
  send({ type: 'step-done', step: 'phase3' });
}

export async function runStep({ project, step, sourceRoot, serverPort, mode }, send) {
  ensureDirs(project);
  writeMeta(project, sourceRoot);
  const { phase2Args, env: modeEnv } = modeConfig(mode || 'thorough');
  const env = { ...baseEnv(project, sourceRoot, serverPort), ...modeEnv };
  const runKey = `${project.id}`;
  if (activeRuns.has(runKey)) {
    throw new Error('A generation run is already in progress for this project.');
  }
  try {
    if (step === 'phase1') await runPhase1(project, env, runKey, send);
    else if (step === 'skeleton') await runSkeletonDraft(project, env, runKey, send);
    else if (step === 'phase2') await runPhase2(project, env, runKey, send, sourceRoot, phase2Args);
    else if (step === 'phase3') await runPhase3(project, env, runKey, send);
    else if (step === 'full') {
      await runPhase1(project, env, runKey, send);
      await runSkeletonDraft(project, env, runKey, send);
      await runPhase2(project, env, runKey, send, sourceRoot, phase2Args);
      await runPhase3(project, env, runKey, send);
    } else {
      throw new Error(`Unknown step: ${step}`);
    }
    send({ type: 'done', step });
  } catch (err) {
    send({ type: 'error', step, error: String(err?.message || err) });
  }
}

export function abortRun(projectId) {
  const child = activeRuns.get(projectId);
  if (child) {
    try {
      child.kill('SIGKILL');
    } catch {
      /* ignore */
    }
    activeRuns.delete(projectId);
    return true;
  }
  return false;
}
