import { useMemo } from 'react';

export default function HealthPanel({ artifacts }) {
  const graph = artifacts?.graph;
  const dropped = artifacts?.dropped;
  const mapping = artifacts?.mapping;

  const stats = useMemo(() => {
    const nodes = graph?.nodes || {};
    const internal = Object.values(nodes).filter((n) => n.kind === 'internal' && !n.synthetic);
    const boundary = Object.values(nodes).filter((n) => n.kind === 'boundary');
    const edges = graph?.edges || [];
    const unmapped = mapping?.unmapped_functions || [];
    let mapped = 0;
    for (const s of Object.values(mapping?.stages || {})) mapped += (s.members || []).length;
    return {
      internal: internal.length,
      boundary: boundary.length,
      edges: edges.length,
      mapped,
      unmapped: unmapped.length,
    };
  }, [graph, mapping]);

  const droppedByCat = dropped?.metadata?.by_category || {};

  const Stat = ({ label, value, sub }) => (
    <div className="card px-4 py-3">
      <div className="text-2xl font-semibold text-foreground">{value}</div>
      <div className="text-xs text-slate-400">{label}</div>
      {sub && <div className="mt-0.5 text-[11px] text-slate-600">{sub}</div>}
    </div>
  );

  if (!graph) {
    return (
      <div className="flex h-full items-center justify-center text-slate-500">
        No data yet. Run Phase 1.
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto p-6">
      <h2 className="mb-4 text-lg font-semibold text-foreground">Coverage &amp; health</h2>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-5">
        <Stat label="Internal functions" value={stats.internal} />
        <Stat label="Boundary nodes" value={stats.boundary} />
        <Stat label="Call edges" value={stats.edges} />
        <Stat label="Mapped members" value={stats.mapped} sub="placed into stages" />
        <Stat label="Unmapped" value={stats.unmapped} sub="api surface / dead / synthetic" />
      </div>

      <div className="mt-6">
        <h3 className="mb-2 text-sm font-semibold text-slate-200">
          Dropped calls (unresolved during static analysis)
        </h3>
        <p className="mb-3 text-xs text-slate-500">
          These call sites could not be resolved to a named function (inherited methods, local
          variables, builtins, etc.) and are excluded from the graph — useful to gauge graph
          completeness.
        </p>
        <div className="grid grid-cols-2 gap-2 md:grid-cols-3">
          {Object.entries(droppedByCat).map(([cat, n]) => (
            <div key={cat} className="card flex items-center justify-between px-3 py-2">
              <span className="text-xs text-slate-300">{cat.replace(/_/g, ' ')}</span>
              <span className="chip bg-ink-700 text-slate-300">{n}</span>
            </div>
          ))}
          {Object.keys(droppedByCat).length === 0 && (
            <div className="text-xs text-slate-600">no dropped-call log found</div>
          )}
        </div>
      </div>
    </div>
  );
}
