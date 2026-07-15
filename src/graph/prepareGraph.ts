export type GraphNodeInput = {
  id: string;
  label: string;
  type: string;
  color?: string;
  properties?: Record<string, unknown>;
};

export type GraphEdgeInput = {
  source: string | { id: string };
  target: string | { id: string };
  relation?: string;
};

export type GraphNeighbor = { id: string; rel: string; dir: string };

export type PreparedNode<N> = {
  id: string;
  label: string;
  type: string;
  color: string;
  props: Record<string, unknown>;
  input: N;
  degree: number;
  x: number;
  y: number;
  vx: number;
  vy: number;
  fx: number | null;
  fy: number | null;
  r: number;
};

export type PreparedEdge = { a: string; b: string; rel: string };

export type PreparedGraph<N> = {
  simNodes: PreparedNode<N>[];
  byId: Map<string, PreparedNode<N>>;
  simEdges: PreparedEdge[];
  adj: Map<string, GraphNeighbor[]>;
};

export function endpointId(value: string | { id: string }): string {
  return typeof value === "object" ? String(value.id) : String(value);
}

export function prepareGraph<N extends GraphNodeInput, E extends GraphEdgeInput>(
  nodes: N[],
  edges: E[],
  colorOf: (type: string, given?: string) => string,
): PreparedGraph<N> {
  const simNodes: PreparedNode<N>[] = [];
  const byId = new Map<string, PreparedNode<N>>();

  for (const input of nodes) {
    const id = String(input.id);
    if (byId.has(id)) continue;
    const type = input.type || "node";
    const node: PreparedNode<N> = {
      id,
      label: input.label || id,
      type,
      color: colorOf(type, input.color),
      props: input.properties ?? {},
      input,
      degree: 0,
      x: 0,
      y: 0,
      vx: 0,
      vy: 0,
      fx: null,
      fy: null,
      r: 4,
    };
    byId.set(id, node);
    simNodes.push(node);
  }

  const simEdges: PreparedEdge[] = [];
  const adj = new Map<string, GraphNeighbor[]>();
  for (const node of simNodes) adj.set(node.id, []);

  for (const edge of edges) {
    const a = endpointId(edge.source);
    const b = endpointId(edge.target);
    if (a === b) continue;
    const na = byId.get(a);
    const nb = byId.get(b);
    if (!na || !nb) continue;
    const rel = edge.relation ?? "";
    simEdges.push({ a, b, rel });
    adj.get(a)!.push({ id: b, rel, dir: "\u2192" });
    adj.get(b)!.push({ id: a, rel, dir: "\u2190" });
    na.degree++;
    nb.degree++;
  }

  return { simNodes, byId, simEdges, adj };
}
