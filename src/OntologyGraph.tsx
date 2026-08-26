import { MultiGraph } from "graphology";
import Sigma from "sigma";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

export type OgNode = {
  id: string;
  originalId?: string;
  occurrence?: number;
  label: string;
  type: string;
  color?: string;
  size?: number;
  packId?: string;
  x?: number;
  y?: number;
  placeholder?: boolean;
  properties?: Record<string, unknown>;
};

export type OgEdge = {
  id?: string;
  source: string | { id: string };
  target: string | { id: string };
  relation?: string;
  packId?: string;
};

type LegendEntry = { type: string; count: number; color: string };

export type OgNeighborGroup = {
  rel: string;
  items: { id: string; label: string; color: string }[];
  total: number;
};

export type OgDetail<N> = {
  node: N;
  id: string;
  label: string;
  type: string;
  color: string;
  degree: number;
  props: Record<string, unknown>;
  groups: OgNeighborGroup[];
};

export type OgController = {
  selectById: (id: string | null) => void;
  setHighlight: (ids: string[] | null) => void;
};

type EngineApi = {
  setMinDeg: (value: number) => void;
  setHop: (value: number) => void;
  setFocus: (on: boolean) => void;
  toggleType: (type: string) => void;
  fit: () => void;
  selectById: (id: string | null) => void;
  setHighlight: (ids: string[] | null) => void;
};

type SigmaNodeAttributes<N> = Record<string, unknown> & {
  x: number;
  y: number;
  size: number;
  color: string;
  label: string;
  ontologyType: string;
  originalId: string;
  input: N;
  props: Record<string, unknown>;
  placeholder: boolean;
};

type SigmaEdgeAttributes = Record<string, unknown> & {
  size: number;
  color: string;
  relation: string;
  sourceKey: string;
  targetKey: string;
};

const THEME = {
  edge: "#d4d4d4",
  edgeDim: "#eeeeee",
  selection: "#0074e2",
  ai: "#c0990e",
  text: "#171717",
  placeholder: "#a3a3a3",
};

const FALLBACK_PALETTE = [
  "#6d9cfe",
  "#dd74f0",
  "#69ad67",
  "#e2883e",
  "#63ab9d",
  "#f273aa",
  "#c0990e",
  "#67a7b8",
  "#ff6f6c",
  "#737373",
  "#a05fb8",
  "#84c980",
];

const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5));
// Keep Graphology insertion and degree indexing below a single frame budget.
const GRAPH_BUILD_SLICE_MS = 8;
const GRAPH_BUILD_CHECK_INTERVAL = 256;

type CooperativeScheduler = {
  yield?: () => Promise<void>;
};

function yieldToMainThread(): Promise<void> {
  const scheduler = (
    globalThis as typeof globalThis & { scheduler?: CooperativeScheduler }
  ).scheduler;
  if (typeof scheduler?.yield === "function") {
    return scheduler.yield();
  }
  if (typeof MessageChannel === "undefined") {
    return new Promise((resolve) => {
      setTimeout(resolve, 0);
    });
  }
  return new Promise((resolve) => {
    const channel = new MessageChannel();
    channel.port1.onmessage = () => {
      channel.port1.close();
      channel.port2.close();
      resolve();
    };
    channel.port2.postMessage(null);
  });
}

function endpointId(value: string | { id: string }): string {
  return typeof value === "object" ? String(value.id) : String(value);
}

function finiteCoordinate(value: unknown): number | null {
  const numberValue = Number(value);
  return Number.isFinite(numberValue) ? numberValue : null;
}

function fallbackPosition(index: number, id: string) {
  let hash = 2166136261;
  for (let offset = 0; offset < id.length; offset += 1) {
    hash ^= id.charCodeAt(offset);
    hash = Math.imul(hash, 16777619);
  }
  const jitter = ((hash >>> 0) % 997) / 9970;
  const distance = 9 * Math.sqrt(index + 1);
  const angle = index * GOLDEN_ANGLE + jitter;
  return { x: distance * Math.cos(angle), y: distance * Math.sin(angle) };
}

function uniqueKey(base: string, index: number, used: Set<string>) {
  if (!used.has(base)) {
    used.add(base);
    return base;
  }
  let suffix = index;
  let candidate = `${base}::duplicate:${suffix}`;
  while (used.has(candidate)) {
    suffix += 1;
    candidate = `${base}::duplicate:${suffix}`;
  }
  used.add(candidate);
  return candidate;
}

export function OntologyGraph<N extends OgNode, E extends OgEdge>({
  nodes,
  edges,
  onSelectNode,
  onDetail,
  controllerRef,
  serverStats,
}: {
  nodes: N[];
  edges: E[];
  onSelectNode?: (node: N | null) => void;
  onDetail?: (detail: OgDetail<N> | null) => void;
  controllerRef?: { current: OgController | null };
  serverStats?: { totalNodes: number; totalEdges: number };
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const rendererHostRef = useRef<HTMLDivElement | null>(null);
  const engineRef = useRef<EngineApi | null>(null);
  const onSelectNodeRef = useRef(onSelectNode);
  const onDetailRef = useRef(onDetail);

  const [legend, setLegend] = useState<LegendEntry[]>([]);
  const [hiddenTypes, setHiddenTypes] = useState<Set<string>>(new Set());
  const [minDeg, setMinDeg] = useState(0);
  const [maxDegSlider, setMaxDegSlider] = useState(20);
  const [hop, setHop] = useState(1);
  const [focusOn, setFocusOn] = useState(false);
  const [stats, setStats] = useState({
    shownNodes: 0,
    shownEdges: 0,
    totalNodes: 0,
    totalEdges: 0,
  });
  const [controlHost, setControlHost] = useState<HTMLElement | null>(null);

  useEffect(() => {
    onSelectNodeRef.current = onSelectNode;
  }, [onSelectNode]);

  useEffect(() => {
    onDetailRef.current = onDetail;
  }, [onDetail]);

  useEffect(() => {
    setControlHost(document.getElementById("graph-sidebar-controls"));
  }, []);

  useEffect(() => {
    const container = containerRef.current;
    const rendererHost = rendererHostRef.current;
    if (!container || !rendererHost) return;

    let cancelled = false;
    let disposeRenderer: (() => void) | null = null;
    const disposeActiveRenderer = () => {
      const dispose = disposeRenderer;
      disposeRenderer = null;
      dispose?.();
    };

    const buildGraph = async () => {
      const graph = new MultiGraph<
        SigmaNodeAttributes<N>,
        SigmaEdgeAttributes
      >();
      const typeColors = new Map<string, string>();
      const colorOf = (type: string, given?: string) => {
        if (given) return given;
        if (type === "미해결참조") return THEME.placeholder;
        let color = typeColors.get(type);
        if (!color) {
          color = FALLBACK_PALETTE[typeColors.size % FALLBACK_PALETTE.length];
          typeColors.set(type, color);
        }
        return color;
      };

      const usedNodeKeys = new Set<string>();
      const firstKeyByOriginalId = new Map<string, string>();
      let sliceStartedAt = performance.now();
      const yieldBuild = async () => {
        await yieldToMainThread();
        sliceStartedAt = performance.now();
        return !cancelled;
      };
      const sliceBudgetReached = (index: number) =>
        (index + 1) % GRAPH_BUILD_CHECK_INTERVAL === 0 &&
        performance.now() - sliceStartedAt >= GRAPH_BUILD_SLICE_MS;

      for (let index = 0; index < nodes.length; index += 1) {
        const input = nodes[index];
        const originalId = String(input.id || `node:${index}`);
        const key = uniqueKey(originalId, index, usedNodeKeys);
        if (!firstKeyByOriginalId.has(originalId))
          firstKeyByOriginalId.set(originalId, key);
        const fallback = fallbackPosition(index, originalId);
        const x =
          finiteCoordinate(input.x ?? input.properties?.x) ?? fallback.x;
        const y =
          finiteCoordinate(input.y ?? input.properties?.y) ?? fallback.y;
        const ontologyType =
          input.type || (input.placeholder ? "미해결참조" : "node");
        graph.addNode(key, {
          x,
          y,
          size:
            Number.isFinite(input.size) && Number(input.size) > 0
              ? Number(input.size)
              : 4,
          color: colorOf(ontologyType, input.color),
          label: input.label || originalId,
          ontologyType,
          originalId,
          input,
          props: input.properties ?? {},
          placeholder: Boolean(input.placeholder),
        });
        if (sliceBudgetReached(index) && !(await yieldBuild())) return;
      }
      if (!(await yieldBuild())) return;

      const ensureEndpoint = (
        rawId: string,
        edgeIndex: number,
        role: "source" | "target",
      ) => {
        const normalized = rawId || `__missing_${role}__:edge:${edgeIndex}`;
        const existing = firstKeyByOriginalId.get(normalized);
        if (existing) return existing;
        const key = uniqueKey(
          normalized,
          graph.order + edgeIndex,
          usedNodeKeys,
        );
        const fallback = fallbackPosition(graph.order, normalized);
        const input = {
          id: normalized,
          label: normalized,
          type: "미해결참조",
          color: THEME.placeholder,
          size: 3.5,
          placeholder: true,
          properties: { placeholder: true, unresolved_endpoint: normalized },
        } as unknown as N;
        graph.addNode(key, {
          x: fallback.x,
          y: fallback.y,
          size: 3.5,
          color: THEME.placeholder,
          label: normalized,
          ontologyType: "미해결참조",
          originalId: normalized,
          input,
          props: input.properties ?? {},
          placeholder: true,
        });
        firstKeyByOriginalId.set(normalized, key);
        return key;
      };

      const usedEdgeKeys = new Set<string>();
      for (let index = 0; index < edges.length; index += 1) {
        const input = edges[index];
        const sourceKey = ensureEndpoint(
          endpointId(input.source),
          index,
          "source",
        );
        const targetKey = ensureEndpoint(
          endpointId(input.target),
          index,
          "target",
        );
        const rawKey = String(input.id || `edge:${index}`);
        const edgeKey = uniqueKey(rawKey, index, usedEdgeKeys);
        const relation = input.relation || "related_to";
        graph.addEdgeWithKey(edgeKey, sourceKey, targetKey, {
          size: 0.7,
          color: THEME.edge,
          relation,
          sourceKey,
          targetKey,
        });
        if (sliceBudgetReached(index) && !(await yieldBuild())) return;
      }
      if (!(await yieldBuild())) return;

      let maximumDegree = 0;
      const counts = new Map<string, { count: number; color: string }>();
      let degreeIndex = 0;
      for (const key of usedNodeKeys) {
        const attributes = graph.getNodeAttributes(key);
        const degree = graph.degree(key);
        maximumDegree = Math.max(maximumDegree, degree);
        const desiredSize = 2.4 + Math.min(18, Math.sqrt(degree) * 0.85);
        if (
          !Number.isFinite(attributes.size) ||
          attributes.size <= 0 ||
          attributes.size === 4
        ) {
          graph.setNodeAttribute(key, "size", desiredSize);
        }
        const entry = counts.get(attributes.ontologyType);
        if (entry) entry.count += 1;
        else
          counts.set(attributes.ontologyType, {
            count: 1,
            color: attributes.color,
          });
        if (sliceBudgetReached(degreeIndex) && !(await yieldBuild())) return;
        degreeIndex += 1;
      }
      if (!(await yieldBuild())) return;

      let selectedKey: string | null = null;
      let aiHighlight = new Set<string>();
      let selectionHighlight = new Set<string>();
      // Reuse the already-complete key set instead of allocating another ~200k-entry set.
      let visibleNodeKeys: Set<string> = usedNodeKeys;
      let visibleEdgeCount = graph.size;
      let engMinDeg = 0;
      let engHop = 1;
      let engFocus = false;
      const typeOff = new Set<string>();

      const renderer = new Sigma(graph, rendererHost, {
        allowInvalidContainer: true,
        defaultNodeColor: "#737373",
        defaultEdgeColor: THEME.edge,
        renderLabels: true,
        renderEdgeLabels: false,
        enableEdgeEvents: false,
        hideEdgesOnMove: false,
        hideLabelsOnMove: true,
        labelFont: 'Figtree, "Pretendard Variable", Pretendard, sans-serif',
        labelSize: 12,
        labelDensity: 0.8,
        labelGridCellSize: 120,
        labelRenderedSizeThreshold: 7,
        minEdgeThickness: 0.35,
        stagePadding: 45,
        zIndex: true,
        nodeReducer: (key, data) => {
          const hidden = !visibleNodeKeys.has(key);
          const selectionHit = selectionHighlight.has(key);
          const aiHit = aiHighlight.has(key);
          const emphasized = selectionHit || aiHit;
          const dimmed =
            (selectedKey !== null && !selectionHit) ||
            (aiHighlight.size > 0 && !aiHit);
          return {
            ...data,
            hidden,
            color: dimmed ? "#d4d4d4" : data.color,
            size: emphasized ? Number(data.size) * 1.45 : Number(data.size),
            highlighted: emphasized,
            forceLabel: emphasized,
            zIndex: selectedKey === key ? 3 : emphasized ? 2 : 0,
          };
        },
        edgeReducer: (_key, data) => {
          const sourceKey = String(data.sourceKey);
          const targetKey = String(data.targetKey);
          if (
            !visibleNodeKeys.has(sourceKey) ||
            !visibleNodeKeys.has(targetKey)
          ) {
            return { ...data, hidden: true };
          }
          const selectedEdge =
            selectedKey !== null &&
            (sourceKey === selectedKey || targetKey === selectedKey);
          const aiEdge =
            aiHighlight.has(sourceKey) && aiHighlight.has(targetKey);
          const dimmed =
            (selectedKey !== null && !selectedEdge) ||
            (aiHighlight.size > 0 && !aiEdge);
          return {
            ...data,
            hidden: false,
            color: selectedEdge
              ? THEME.selection
              : aiEdge
                ? THEME.ai
                : dimmed
                  ? THEME.edgeDim
                  : data.color,
            size: selectedEdge || aiEdge ? 1.8 : Number(data.size),
            zIndex: selectedEdge || aiEdge ? 1 : 0,
          };
        },
      });
      disposeRenderer = () => renderer.kill();

      let paintCount = 0;
      renderer.on("afterRender", () => {
        paintCount += 1;
      });

      const focusSet = () => {
        if (!engFocus || !selectedKey) return null;
        const focused = new Set<string>([selectedKey]);
        let frontier = [selectedKey];
        for (let depth = 0; depth < engHop; depth += 1) {
          const next: string[] = [];
          for (const key of frontier) {
            for (const neighbor of graph.neighbors(key)) {
              if (focused.has(neighbor)) continue;
              focused.add(neighbor);
              next.push(neighbor);
            }
          }
          frontier = next;
        }
        return focused;
      };

      const rebuildVisible = () => {
        const focused = focusSet();
        const nextVisible = new Set<string>();
        graph.forEachNode((key, attributes) => {
          const forced = key === selectedKey || aiHighlight.has(key);
          if (
            forced ||
            (graph.degree(key) >= engMinDeg &&
              !typeOff.has(attributes.ontologyType) &&
              (!focused || focused.has(key)))
          ) {
            nextVisible.add(key);
          }
        });
        let visibleEdges = 0;
        graph.forEachEdge((_key, _attributes, source, target) => {
          if (nextVisible.has(source) && nextVisible.has(target))
            visibleEdges += 1;
        });
        visibleNodeKeys = nextVisible;
        visibleEdgeCount = visibleEdges;
        setStats({
          shownNodes: nextVisible.size,
          shownEdges: visibleEdges,
          totalNodes: graph.order,
          totalEdges: graph.size,
        });
        renderer.refresh();
      };

      const buildDetail = (key: string): OgDetail<N> => {
        const attributes = graph.getNodeAttributes(key);
        const groups = new Map<string, OgNeighborGroup>();
        graph.forEachEdge(key, (_edgeKey, edgeAttributes, source, target) => {
          const outgoing = source === key;
          const neighborKey = outgoing ? target : source;
          const neighbor = graph.getNodeAttributes(neighborKey);
          const groupKey = `${outgoing ? "→" : "←"} ${edgeAttributes.relation || "related_to"}`;
          let group = groups.get(groupKey);
          if (!group) {
            group = { rel: groupKey, items: [], total: 0 };
            groups.set(groupKey, group);
          }
          group.total += 1;
          if (group.items.length < 40) {
            group.items.push({
              id: neighbor.originalId,
              label: neighbor.label,
              color: neighbor.color,
            });
          }
        });
        return {
          node: attributes.input,
          id: attributes.originalId,
          label: attributes.label,
          type: attributes.ontologyType,
          color: attributes.color,
          degree: graph.degree(key),
          props: attributes.props,
          groups: [...groups.values()],
        };
      };

      const fitToKeys = (keys: Iterable<string>) => {
        const points = [...keys]
          .map((key) => renderer.getNodeDisplayData(key))
          .filter((item): item is NonNullable<typeof item> => Boolean(item));
        if (!points.length) return;
        let x0 = Infinity;
        let x1 = -Infinity;
        let y0 = Infinity;
        let y1 = -Infinity;
        for (const point of points) {
          x0 = Math.min(x0, point.x);
          x1 = Math.max(x1, point.x);
          y0 = Math.min(y0, point.y);
          y1 = Math.max(y1, point.y);
        }
        const span = Math.max(x1 - x0, y1 - y0);
        void renderer.getCamera().animate(
          {
            x: (x0 + x1) / 2,
            y: (y0 + y1) / 2,
            ratio: Math.max(0.025, Math.min(1, span * 1.8 || 0.08)),
          },
          { duration: 280 },
        );
      };

      const select = (key: string | null) => {
        selectedKey = key;
        selectionHighlight = new Set<string>();
        if (key) {
          selectionHighlight.add(key);
          for (const neighbor of graph.neighbors(key))
            selectionHighlight.add(neighbor);
        }
        if (engFocus) rebuildVisible();
        else renderer.refresh();
        if (!key) {
          onDetailRef.current?.(null);
          onSelectNodeRef.current?.(null);
          return;
        }
        const attributes = graph.getNodeAttributes(key);
        onDetailRef.current?.(buildDetail(key));
        onSelectNodeRef.current?.(attributes.input);
      };

      const selectByOriginalId = (id: string | null) => {
        const key = id ? (firstKeyByOriginalId.get(id) ?? null) : null;
        select(key);
        if (key) fitToKeys([key]);
      };

      const setHighlight = (ids: string[] | null) => {
        aiHighlight = new Set(
          (ids ?? [])
            .map((id) => firstKeyByOriginalId.get(id))
            .filter((key): key is string => Boolean(key)),
        );
        rebuildVisible();
        if (aiHighlight.size) fitToKeys(aiHighlight);
      };

      renderer.on("clickNode", ({ node }) => {
        select(node);
      });
      renderer.on("clickStage", () => {
        select(null);
      });

      let draggedNode: string | null = null;
      renderer.on("downNode", ({ node, event }) => {
        draggedNode = node;
        if (!renderer.getCustomBBox())
          renderer.setCustomBBox(renderer.getBBox());
        event.preventSigmaDefault();
      });
      const mouseCaptor = renderer.getMouseCaptor();
      mouseCaptor.on("mousemovebody", (event) => {
        if (!draggedNode) return;
        const position = renderer.viewportToGraph(event);
        graph.mergeNodeAttributes(draggedNode, {
          x: position.x,
          y: position.y,
        });
        event.preventSigmaDefault();
        event.original.preventDefault();
      });
      mouseCaptor.on("mouseup", () => {
        draggedNode = null;
      });

      engineRef.current = {
        setMinDeg: (value) => {
          engMinDeg = value;
          rebuildVisible();
        },
        setHop: (value) => {
          engHop = value;
          if (engFocus) rebuildVisible();
        },
        setFocus: (on) => {
          engFocus = on;
          rebuildVisible();
          if (on && selectedKey) fitToKeys(visibleNodeKeys);
        },
        toggleType: (type) => {
          if (typeOff.has(type)) typeOff.delete(type);
          else typeOff.add(type);
          rebuildVisible();
        },
        fit: () => {
          void renderer.getCamera().animatedReset({ duration: 280 });
        },
        selectById: selectByOriginalId,
        setHighlight,
      };
      if (controllerRef) {
        controllerRef.current = {
          selectById: selectByOriginalId,
          setHighlight,
        };
      }

      setLegend(
        [...counts.entries()]
          .map(([type, value]) => ({
            type,
            count: value.count,
            color: value.color,
          }))
          .sort((left, right) => right.count - left.count),
      );
      setHiddenTypes(new Set());
      setMinDeg(0);
      setMaxDegSlider(Math.max(20, maximumDegree));
      setHop(1);
      setFocusOn(false);
      setStats({
        shownNodes: graph.order,
        shownEdges: graph.size,
        totalNodes: graph.order,
        totalEdges: graph.size,
      });
      onDetailRef.current?.(null);
      onSelectNodeRef.current?.(null);

      (container as HTMLDivElement & { __og?: unknown }).__og = {
        pump: () => renderer.refresh(),
        fit: () => renderer.getCamera().animatedReset({ duration: 0 }),
        stats: () => ({
          visible: visibleNodeKeys.size,
          edges: visibleEdgeCount,
          alpha: 0,
          k: 1 / renderer.getCamera().getState().ratio,
        }),
        renderStats: () => ({
          renderer: "sigma-webgl",
          simulationTicks: 0,
          paints: paintCount,
          settled: true,
          lastPaintSimulationActive: false,
          lastPaintOrdinaryLabels: renderer.getNodeDisplayedLabels().size,
          lastPaintInteractiveLabels:
            selectionHighlight.size + aiHighlight.size,
        }),
        select: selectByOriginalId,
        highlight: setHighlight,
        firstVisibleId: () => {
          const first = visibleNodeKeys.values().next().value;
          return first ? graph.getNodeAttribute(first, "originalId") : null;
        },
      };
    };

    void buildGraph().catch((error: unknown) => {
      if (cancelled) return;
      disposeActiveRenderer();
      engineRef.current = null;
      delete (container as HTMLDivElement & { __og?: unknown }).__og;
      if (controllerRef) controllerRef.current = null;
      console.error("Failed to construct the full ontology graph.", error);
    });

    return () => {
      cancelled = true;
      disposeActiveRenderer();
      engineRef.current = null;
      delete (container as HTMLDivElement & { __og?: unknown }).__og;
      if (controllerRef) controllerRef.current = null;
    };
  }, [nodes, edges, controllerRef]);

  const controlsPanel = (
    <aside
      className={
        controlHost ? "og-panel og-controls in-sidebar" : "og-panel og-controls"
      }
      aria-label="필터와 보기"
    >
      <div className="og-panel-title">
        필터와 보기 <em>WebGL · 전체 그래프</em>
      </div>
      <label>
        <span>최소 차수 (degree)</span>
        <strong>{minDeg}</strong>
        <input
          type="range"
          min={0}
          max={maxDegSlider}
          step={1}
          value={minDeg}
          onChange={(event) => {
            const value = Number(event.target.value);
            setMinDeg(value);
            engineRef.current?.setMinDeg(value);
          }}
        />
      </label>
      <label>
        <span>이웃 탐색 반경 (hop)</span>
        <strong>{hop}</strong>
        <input
          type="range"
          min={1}
          max={4}
          step={1}
          value={hop}
          onChange={(event) => {
            const value = Number(event.target.value);
            setHop(value);
            engineRef.current?.setHop(value);
          }}
        />
      </label>
      <div className="og-controls-row">
        <button type="button" onClick={() => engineRef.current?.fit()}>
          전체 맞춤
        </button>
        <button
          type="button"
          className={focusOn ? "on" : ""}
          onClick={() => {
            const next = !focusOn;
            setFocusOn(next);
            engineRef.current?.setFocus(next);
          }}
        >
          이웃 집중
        </button>
      </div>
    </aside>
  );

  return (
    <div ref={containerRef} className="og-stage">
      <div ref={rendererHostRef} className="og-canvas og-sigma-canvas" />

      <aside className="og-panel og-legend" aria-label="노드 범례">
        <div className="og-panel-title">
          노드 유형 <em>클릭=숨김</em>
        </div>
        {legend.map((entry) => (
          <button
            key={entry.type}
            type="button"
            className={
              hiddenTypes.has(entry.type)
                ? "og-legend-item off"
                : "og-legend-item"
            }
            onClick={() => {
              engineRef.current?.toggleType(entry.type);
              setHiddenTypes((previous) => {
                const next = new Set(previous);
                if (next.has(entry.type)) next.delete(entry.type);
                else next.add(entry.type);
                return next;
              });
            }}
          >
            <i style={{ background: entry.color }} />
            <span>{entry.type}</span>
            <em>{entry.count.toLocaleString()}</em>
          </button>
        ))}
      </aside>

      {controlHost ? createPortal(controlsPanel, controlHost) : controlsPanel}

      <div className="og-panel og-stats">
        노드 <strong>{stats.shownNodes.toLocaleString()}</strong> /{" "}
        {stats.totalNodes.toLocaleString()} · 엣지{" "}
        <strong>{stats.shownEdges.toLocaleString()}</strong> /{" "}
        {stats.totalEdges.toLocaleString()}
        {serverStats &&
        (serverStats.totalNodes > stats.totalNodes ||
          serverStats.totalEdges > stats.totalEdges) ? (
          <em className="og-stats-truncated">
            {" "}
            · 서버 전체 {serverStats.totalNodes.toLocaleString()} /{" "}
            {serverStats.totalEdges.toLocaleString()} 중 일부만 로드됨
          </em>
        ) : null}
      </div>
    </div>
  );
}
