// Adapted from dr-claw/server/utils/cliResolution.js — CLI candidate building + probing.
import { spawn, spawnSync } from 'child_process';

function isCommandNotFoundExitCode(code) {
  return code === 127 || code === 9009;
}

export function getCliCommandCandidates({
  envVarName,
  legacyEnvVarNames = [],
  defaultCommands,
  env = process.env,
  platform = process.platform,
  appendWindowsSuffixes = false,
}) {
  let envCommand = '';
  for (const key of [envVarName, ...legacyEnvVarNames].filter(Boolean)) {
    const s = String(env[key] || '').trim();
    if (s) {
      envCommand = s;
      break;
    }
  }

  const rawCandidates = [];
  if (envCommand) rawCandidates.push(envCommand);
  for (const command of defaultCommands) {
    if (command) rawCandidates.push(command);
  }

  const candidates = [];
  for (const candidate of rawCandidates) {
    candidates.push(candidate);
    if (appendWindowsSuffixes && platform === 'win32' && !/\.(cmd|exe|bat)$/i.test(candidate)) {
      candidates.push(`${candidate}.cmd`, `${candidate}.exe`);
    }
  }
  return [...new Set(candidates)];
}

export function isCommandAvailable(command, args = ['--version'], platform = process.platform) {
  if (!command) return false;
  const result = spawnSync(command, args, {
    stdio: 'ignore',
    env: process.env,
    shell: platform === 'win32',
  });
  return !result.error && !isCommandNotFoundExitCode(result.status);
}

export { spawn };
