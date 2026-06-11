import { useEffect, useMemo, useState } from 'react';
import ReactFlow, { Background, Controls, MiniMap } from 'reactflow';
import 'reactflow/dist/style.css';
import { stageColor, stageTitle } from '../../lib/handbook.js';

const COL_W = 280;
const ROW_H = 64;

function readThemeColors() {
  const cs = getComputedStyle(document.documentElement);
  const hsl = (name) => `hsl(${cs.getPropertyValue(name).trim()})`;
  return {
    card: hsl('--card'),
    background: hsl('--background'),
    foreground: hsl('--foreground'),
    primaryFg: hsl('--primary-foreground'),
    border: hsl('--border'),
  };
}

export default function GraphView({ artifacts, nodeStageMap, order, selectedId, onSelect }) {
  const [showBoundary, setShowBoundary] = useState(false);
  const [palette, setPalette] = useState(readThemeColors);

  useEffect(() => {
    const obs = new MutationObserver(() => setPalette(readThemeColors()));
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
    return () => obs.disconnect();
  }, []);

  const { nodes, edges, stages } = useMemo(() => {
    const graph = artifacts?.graph;
    if (!graph) return { nodes: [], edges: [], stages: [] };
    const allNodes = graph.nodes || {};

    const internalIds = Object.keys(allNodes).filter(
      (id) => allNodes[id].kind === 'internal' && !allNodes[id].synthetic
    );

    // Column order: handbook order, then any leftover stages, then "unmapped".
    const stageIds = [];
    const seen = new Set();
    for (const sid of order || []) {
      if (!seen.has(sid)) {
        stageIds.push(sid);
        seen.add(sid);
      }
    }
    for (const id of internalIds) {
      const sid = nodeStageMap[id] || '__unmapped__';
      if (!seen.has(sid)) {
        stageIds.push(sid);
        seen.add(sid);
      }
    }

    const colIndex = Object.fromEntries(stageIds.map((s, i) => [s, i]));
    const colCounts = {};
    const rfNodes = internalIds.map((id) => {
      const n = allNodes[id];
      const sid = nodeStageMap[id] || '__unmapped__';
      const col = colIndex[sid] ?? stageIds.length;
      const row = colCounts[sid] || 0;
      colCounts[sid] = row + 1;
      const color = sid === '__unmapped__' ? '#475569' : stageColor(sid, order);
      return {
        id,
        position: { x: col * COL_W, y: row * ROW_H },
        data: { label: n.qualname },
        style: {
          background: selectedId === id ? color : palette.card,
          color: selectedId === id ? palette.primaryFg : palette.foreground,
          border: `1px solid ${color}`,
          borderRadius: 8,
          fontSize: 10,
          width: COL_W - 40,
          padding: 6,
        },
      };
    });

    const idSet = new Set(internalIds);
    const rfEdges = (graph.edges || [])
      .filter((e) => idSet.has(e.caller_id) && (showBoundary || idSet.has(e.callee_id)))
      .filter((e) => idSet.has(e.callee_id))
      .map((e, i) => ({
        id: `e${i}`,
        source: e.caller_id,
        target: e.callee_id,
        animated: !!e.is_await,
        style: {
          stroke: e.call_type?.startsWith('boundary') ? '#475569' : '#3a4256',
          strokeDasharray: e.call_type?.startsWith('boundary') ? '4 4' : undefined,
        },
      }));

    return { nodes: rfNodes, edges: rfEdges, stages: stageIds };
  }, [artifacts, nodeStageMap, order, selectedId, showBoundary, palette]);

  if (!artifacts?.graph) {
    return (
      <div className="flex h-full items-center justify-center text-slate-500">
        No call graph yet. Run Phase 1 (Map call graph).
      </div>
    );
  }

  return (
    <div className="relative h-full w-full">
      <div className="absolute left-3 top-3 z-10 flex flex-wrap gap-1">
        {stages
          .filter((s) => s !== '__unmapped__')
          .map((sid) => (
            <span
              key={sid}
              className="chip"
              style={{ backgroundColor: stageColor(sid, order) + '33', color: stageColor(sid, order) }}
            >
              {stageTitle(artifacts, sid)}
            </span>
          ))}
      </div>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodeClick={(_, n) => onSelect(n.id)}
        fitView
        minZoom={0.05}
        proOptions={{ hideAttribution: true }}
      >
        <Background color={palette.border} gap={20} />
        <Controls />
        <MiniMap pannable zoomable nodeColor={(n) => n.style?.border?.replace('1px solid ', '') || '#5b8cff'} />
      </ReactFlow>
    </div>
  );
}
