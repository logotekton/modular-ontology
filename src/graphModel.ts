/** Pure graph indexing/filtering/geometry shared by the canvas and regression tests. */
export type IndexedEdge = { a: string; b: string; rel: string; lane: number };
export type Neighbor = { id: string; rel: string; dir: "→" | "←" | "↺" };

export function indexEdges(
  nodeIds: ReadonlySet<string>,
  edges: readonly { source: string | { id: string }; target: string | { id: string }; relation?: string }[],
) {
  const links: IndexedEdge[] = [];
  const adjacency = new Map<string, Neighbor[]>();
  const degrees = new Map<string, number>();
  const pairs = new Map<string, IndexedEdge[]>();
  for (const id of nodeIds) {
    adjacency.set(id, []);
    degrees.set(id, 0);
  }
  for (const edge of edges) {
    const a = String(typeof edge.source === "object" ? edge.source.id : edge.source);
    const b = String(typeof edge.target === "object" ? edge.target.id : edge.target);
    if (!nodeIds.has(a) || !nodeIds.has(b)) continue;
    const rel = edge.relation ?? "";
    const link = { a, b, rel, lane: 0 };
    links.push(link);
    adjacency.get(a)!.push({ id: b, rel, dir: a === b ? "↺" : "→" });
    if (a !== b) adjacency.get(b)!.push({ id: a, rel, dir: "←" });
    degrees.set(a, degrees.get(a)! + 1);
    degrees.set(b, degrees.get(b)! + 1);
    // JSON tuple keys avoid collisions when RDF identifiers contain separators.
    const key = JSON.stringify(a <= b ? [a, b] : [b, a]);
    const pair = pairs.get(key);
    if (pair) pair.push(link);
    else pairs.set(key, [link]);
  }
  for (const pair of pairs.values()) {
    pair.forEach((edge, i) => { edge.lane = edge.a === edge.b ? i : i - (pair.length - 1) / 2; });
  }
  return { links, adjacency, degrees };
}

export function neighborhood(adjacency: ReadonlyMap<string, readonly { id: string }[]>, id: string, hops: number) {
  const visited = new Set([id]);
  let frontier = [id];
  for (let hop = 0; hop < hops && frontier.length; hop++) {
    const next: string[] = [];
    for (const current of frontier) {
      for (const neighbor of adjacency.get(current) ?? []) {
        if (visited.has(neighbor.id)) continue;
        visited.add(neighbor.id);
        next.push(neighbor.id);
      }
    }
    frontier = next;
  }
  return visited;
}

/** Input is ranked once by descending degree; forced nodes take precedence over the cap. */
export function visibleNodes<T extends { id: string; label?: string; degree: number; type: string }>(
  ranked: readonly T[],
  options: { minDegree: number; hiddenTypes: ReadonlySet<string>; focus: ReadonlySet<string> | null; forced: ReadonlySet<string>; limit: number; search?: string },
) {
  const search = options.search?.trim().toLocaleLowerCase() ?? "";
  const mandatory = ranked.filter((node) => options.forced.has(node.id));
  const ordinary = ranked.filter((node) => !options.forced.has(node.id) && node.degree >= options.minDegree &&
    !options.hiddenTypes.has(node.type) && (!options.focus || options.focus.has(node.id)) &&
    (!search || node.id.toLocaleLowerCase().includes(search) || node.label?.toLocaleLowerCase().includes(search)));
  const budget = Math.max(0, options.limit - mandatory.length);
  return { nodes: [...mandatory, ...ordinary.slice(0, budget)], capped: ordinary.length > budget };
}

type Point = { x: number; y: number };
type Circle = Point & { r: number };
export type EdgePath = { start: Point; end: Point; control: Point; control2?: Point; label: Point; angle: number };

/** Clip arrows to node borders and fan parallel/reverse predicates into distinct curves. */
export function edgePath(a: Circle, b: Circle, edge: IndexedEdge): EdgePath {
  if (edge.a === edge.b) {
    const radius = a.r + 18 + edge.lane * 12;
    const start = { x: a.x - a.r * 0.7, y: a.y - a.r * 0.7 };
    const end = { x: a.x + a.r * 0.7, y: a.y - a.r * 0.7 };
    const control = { x: a.x - radius, y: a.y - radius * 2 };
    const control2 = { x: a.x + radius, y: a.y - radius * 2 };
    return { start, end, control, control2, label: { x: a.x, y: a.y - radius * 1.5 }, angle: Math.atan2(end.y - control2.y, end.x - control2.x) };
  }
  const dx = b.x - a.x, dy = b.y - a.y;
  const distance = Math.hypot(dx, dy) || 1;
  const offset = edge.lane * 26 * (edge.a < edge.b ? 1 : -1);
  const control = { x: (a.x + b.x) / 2 - dy / distance * offset, y: (a.y + b.y) / 2 + dx / distance * offset };
  const fromLength = Math.hypot(control.x - a.x, control.y - a.y) || 1;
  const toLength = Math.hypot(b.x - control.x, b.y - control.y) || 1;
  // Closely overlapping nodes still get finite, non-inverted clipped segments.
  const fromRadius = Math.min(a.r, fromLength * 0.8), toRadius = Math.min(b.r, toLength * 0.8);
  const start = { x: a.x + (control.x - a.x) / fromLength * fromRadius, y: a.y + (control.y - a.y) / fromLength * fromRadius };
  const end = { x: b.x - (b.x - control.x) / toLength * toRadius, y: b.y - (b.y - control.y) / toLength * toRadius };
  return { start, end, control, label: { x: (start.x + 2 * control.x + end.x) / 4, y: (start.y + 2 * control.y + end.y) / 4 }, angle: Math.atan2(end.y - control.y, end.x - control.x) };
}
