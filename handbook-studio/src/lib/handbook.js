// Helpers shared across the handbook viewer.

const PALETTE = [
  '#5b8cff', '#22c55e', '#f59e0b', '#ec4899', '#06b6d4', '#a78bfa',
  '#ef4444', '#14b8a6', '#eab308', '#f97316', '#8b5cf6', '#10b981',
];

export function stageColor(sid, order) {
  if (!sid) return '#64748b';
  const idx = order ? order.indexOf(sid) : 0;
  const i = idx >= 0 ? idx : Math.abs(hashStr(sid));
  return PALETTE[i % PALETTE.length];
}

function hashStr(s) {
  let h = 0;
  for (let i = 0; i < s.length; i += 1) h = (h * 31 + s.charCodeAt(i)) | 0;
  return h;
}

// Map graph node id -> stage id, using the Phase 2 mapping.
export function buildNodeStageMap(mapping) {
  const map = {};
  const stages = mapping?.stages || {};
  for (const [sid, stage] of Object.entries(stages)) {
    for (const m of stage.members || []) {
      if (m.qualname) map[m.qualname] = sid;
    }
  }
  return map;
}

// Member purpose lookup: node id -> purpose text.
export function buildPurposeMap(mapping) {
  const map = {};
  const stages = mapping?.stages || {};
  for (const stage of Object.values(stages)) {
    for (const m of stage.members || []) {
      if (m.qualname && m.purpose) map[m.qualname] = m.purpose;
    }
  }
  return map;
}

// Handbook Tier-3 translation lookup: node id (qualname) -> { translation, stageId }.
export function buildTranslationMap(handbook) {
  const map = {};
  const stages = handbook?.stages || {};
  for (const [sid, s] of Object.entries(stages)) {
    for (const fn of s.functions || []) {
      if (fn.qualname) map[fn.qualname] = { translation: fn.translation, stageId: sid };
    }
  }
  return map;
}

export function stageTitle(artifacts, sid) {
  const fromHb = artifacts?.handbook?.stages?.[sid]?.title;
  if (fromHb) return fromHb;
  const sk = (artifacts?.skeleton?.stages || []).find((s) => s.id === sid);
  return sk?.title || sid;
}

// Resolve a source file (relative to source root) to a path relative to the project.
export function projectRelPath(artifacts, fileRelToSource) {
  const root = artifacts?.meta?.sourceRootRel || '.';
  if (!fileRelToSource) return null;
  if (root === '.' || !root) return fileRelToSource;
  return `${root.replace(/\/$/, '')}/${fileRelToSource}`;
}
