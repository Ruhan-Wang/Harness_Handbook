import { useEffect, useMemo, useState } from 'react';
import Markdown from '../Markdown.jsx';
import GraphView from './GraphView.jsx';
import StageNavigator from './StageNavigator.jsx';
import RegisterExplorer from './RegisterExplorer.jsx';
import FunctionCard from './FunctionCard.jsx';
import HealthPanel from './HealthPanel.jsx';
import SourceViewer from './SourceViewer.jsx';
import {
  buildNodeStageMap,
  buildPurposeMap,
  buildTranslationMap,
  stageTitle,
} from '../../lib/handbook.js';

function translationToText(t) {
  if (!t) return '';
  if (typeof t === 'string') return t;
  return Object.entries(t)
    .filter(([k]) => !['schema_version', 'type'].includes(k))
    .map(([k, v]) => `${k.replace(/_/g, ' ')}: ${typeof v === 'string' ? v : JSON.stringify(v)}`)
    .join('\n');
}

const TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'graph', label: 'Call graph' },
  { id: 'stages', label: 'Stages' },
  { id: 'registers', label: 'State registers' },
  { id: 'health', label: 'Health' },
];

export default function HandbookViewer({ project, artifacts, onFocusChange }) {
  const [tab, setTab] = useState('overview');
  const [selectedNodeId, setSelectedNodeId] = useState(null);
  const [activeStageId, setActiveStageId] = useState(null);
  const [source, setSource] = useState(null); // { path, lineRange }

  const purposeMap = useMemo(() => buildPurposeMap(artifacts?.mapping), [artifacts]);
  const translationMap = useMemo(() => buildTranslationMap(artifacts?.handbook), [artifacts]);

  // Report whatever the user is looking at up to the chat, so "explain this"
  // targets the selected function/stage.
  useEffect(() => {
    if (!onFocusChange) return;
    if (tab === 'graph' && selectedNodeId) {
      const node = artifacts?.graph?.nodes?.[selectedNodeId];
      if (!node) {
        onFocusChange(null);
        return;
      }
      const line = node.line_start ? `:${node.line_start}` : '';
      const desc = translationToText(translationMap[selectedNodeId]?.translation);
      const purpose = purposeMap[selectedNodeId];
      onFocusChange({
        kind: 'function',
        id: selectedNodeId,
        label: node.qualname || selectedNodeId,
        text: [
          `## Currently focused FUNCTION: ${node.qualname || selectedNodeId}`,
          `File: ${node.file || '?'}${line}`,
          node.signature ? `Signature: ${node.signature}` : '',
          purpose ? `Purpose: ${purpose}` : '',
          desc ? `Handbook description:\n${desc}` : '',
        ]
          .filter(Boolean)
          .join('\n'),
      });
    } else if (tab === 'stages' && activeStageId) {
      const stage = artifacts?.handbook?.stages?.[activeStageId];
      const title = stage?.title || stageTitle(artifacts, activeStageId) || activeStageId;
      onFocusChange({
        kind: 'stage',
        id: activeStageId,
        label: title,
        text: [
          `## Currently focused STAGE: ${title} (${activeStageId})`,
          stage?.logical_md || '',
        ]
          .filter(Boolean)
          .join('\n'),
      });
    } else {
      onFocusChange(null);
    }
  }, [tab, selectedNodeId, activeStageId, artifacts, onFocusChange, purposeMap, translationMap]);

  const order = useMemo(
    () => artifacts?.handbook?.order || Object.keys(artifacts?.mapping?.stages || {}),
    [artifacts]
  );
  const nodeStageMap = useMemo(() => buildNodeStageMap(artifacts?.mapping), [artifacts]);

  const openSource = (path, lineRange) => {
    if (path) setSource({ path, lineRange });
  };
  const selectNode = (id) => {
    setSelectedNodeId(id);
    const n = artifacts?.graph?.nodes?.[id];
    if (n?.file && n.line_start) {
      // do not auto-open source; user clicks "View source"
    }
  };

  const overviewMd =
    artifacts?.handbook?.overview?.content_md || artifacts?.references?.['overview.md'] || '';

  return (
    <div className="flex h-full min-h-0">
      <div className="flex min-w-0 flex-1 flex-col">
        <nav className="flex gap-1 border-b border-ink-600 bg-ink-800 px-4">
          {TABS.map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`-mb-px border-b-2 px-4 py-2 text-sm font-medium transition-colors ${
                tab === t.id
                  ? 'border-accent text-foreground'
                  : 'border-transparent text-slate-400 hover:text-slate-200'
              }`}
            >
              {t.label}
            </button>
          ))}
        </nav>

        <div className="min-h-0 flex-1 overflow-hidden">
          {tab === 'overview' && (
            <div className="h-full overflow-y-auto p-6">
              {overviewMd ? (
                <Markdown>{overviewMd}</Markdown>
              ) : (
                <div className="text-slate-500">
                  No overview yet. Run Phase 3 (Write docs) to generate the system overview.
                </div>
              )}
            </div>
          )}

          {tab === 'graph' && (
            <div className="flex h-full">
              <div className="min-w-0 flex-1">
                <GraphView
                  artifacts={artifacts}
                  nodeStageMap={nodeStageMap}
                  order={order}
                  selectedId={selectedNodeId}
                  onSelect={selectNode}
                />
              </div>
              {selectedNodeId && (
                <div className="w-96 shrink-0 border-l border-ink-600 bg-ink-800">
                  <FunctionCard
                    artifacts={artifacts}
                    nodeId={selectedNodeId}
                    nodeStageMap={nodeStageMap}
                    order={order}
                    onOpenSource={openSource}
                  />
                </div>
              )}
            </div>
          )}

          {tab === 'stages' && (
            <StageNavigator
              artifacts={artifacts}
              order={order}
              onOpenSource={openSource}
              onActiveChange={setActiveStageId}
              onSelectNode={(id) => {
                setSelectedNodeId(id);
                setTab('graph');
              }}
            />
          )}

          {tab === 'registers' && (
            <RegisterExplorer
              artifacts={artifacts}
              onOpenSource={openSource}
              onSelectNode={(id) => {
                setSelectedNodeId(id);
                setTab('graph');
              }}
            />
          )}

          {tab === 'health' && <HealthPanel artifacts={artifacts} />}
        </div>
      </div>

      {source && (
        <div className="w-[44%] min-w-[360px] border-l border-ink-600 bg-ink-900">
          <SourceViewer
            projectId={project.id}
            path={source.path}
            lineRange={source.lineRange}
            onClose={() => setSource(null)}
          />
        </div>
      )}
    </div>
  );
}
