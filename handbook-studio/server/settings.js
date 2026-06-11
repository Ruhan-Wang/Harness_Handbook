// Persisted app settings: which CLI provider + model to use for generation/chat.
import fs from 'fs';
import path from 'path';
import os from 'os';

const STORE_DIR = path.join(os.homedir(), '.handbook-studio');
const FILE = path.join(STORE_DIR, 'settings.json');

const DEFAULTS = {
  provider: 'auto',
  model: '',
  internalHost: '',
  internalPort: 8080,
  internalUser: '',
  internalKey: '',
  internalModel: 'api_azure_openai_gpt-5.4-2026-03-05',
};

export function getSettings() {
  try {
    return { ...DEFAULTS, ...JSON.parse(fs.readFileSync(FILE, 'utf8')) };
  } catch {
    return { ...DEFAULTS };
  }
}

export function setSettings(patch) {
  const next = { ...getSettings(), ...patch };
  if (!fs.existsSync(STORE_DIR)) fs.mkdirSync(STORE_DIR, { recursive: true });
  fs.writeFileSync(FILE, JSON.stringify(next, null, 2));
  return next;
}
