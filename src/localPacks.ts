// 로컬 온톨로지 팩 로더 — Drive 동기화 없이 opencrab-cloud-pack ZIP(.zip)이나
// JSON/JSONL 그래프 파일을 브라우저에서 직접 읽어 Graph Explorer에 병합한다.
// 의존성 없음: ZIP central directory를 직접 파싱하고 DecompressionStream으로 해제.

export type LocalNode = {
  id: string;
  label: string;
  type: string;
  packId?: string;
  color: string;
  size: number;
  properties: Record<string, unknown>;
};

export type LocalEdge = {
  id: string;
  source: string;
  target: string;
  relation: string;
  packId?: string;
};

export type ParsedLocalPack = { title: string; nodes: LocalNode[]; edges: LocalEdge[] };

export type LocalMergeResult = {
  nodes: LocalNode[];
  edges: LocalEdge[];
  addedNodes: number;
  addedEdges: number;
  resolvedRefs: number;
  placeholders: number;
};

export const LOCAL_PLACEHOLDER_TYPE = "미해결참조";

// Astryx theme-neutral(light) 팔레트 — @astryxdesign/theme-neutral 0.1.3 토큰
const ASTRYX_PALETTE = [
  "#6d9cfe", "#dd74f0", "#69ad67", "#e2883e", "#63ab9d", "#f273aa",
  "#c0990e", "#67a7b8", "#ff6f6c", "#737373", "#a05fb8", "#84c980",
];
const typeColorIndex = new Map<string, number>();

export function colorForLocalType(type: string): string {
  if (type === LOCAL_PLACEHOLDER_TYPE) return "#a3a3a3";
  let index = typeColorIndex.get(type);
  if (index === undefined) {
    index = typeColorIndex.size;
    typeColorIndex.set(type, index);
  }
  return ASTRYX_PALETTE[index % ASTRYX_PALETTE.length];
}

/* ── ZIP 리더 (opencrab-cloud-pack-v1) ── */
type ZipReader = {
  names: string[];
  has: (name: string) => boolean;
  read: (name: string) => Promise<string | null>;
};

async function readZip(buf: ArrayBuffer): Promise<ZipReader> {
  const dv = new DataView(buf);
  const u8 = new Uint8Array(buf);
  let eocd = -1;
  for (let i = buf.byteLength - 22; i >= Math.max(0, buf.byteLength - 22 - 65536); i--) {
    if (dv.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
  }
  if (eocd < 0) throw new Error("ZIP 형식이 아닙니다");
  const count = dv.getUint16(eocd + 10, true);
  let off = dv.getUint32(eocd + 16, true);
  const entries = new Map<string, { method: number; csize: number; lho: number }>();
  const decoder = new TextDecoder("utf-8");
  for (let i = 0; i < count; i++) {
    if (dv.getUint32(off, true) !== 0x02014b50) break;
    const method = dv.getUint16(off + 10, true);
    const csize = dv.getUint32(off + 20, true);
    const nlen = dv.getUint16(off + 28, true);
    const elen = dv.getUint16(off + 30, true);
    const clen = dv.getUint16(off + 32, true);
    const lho = dv.getUint32(off + 42, true);
    entries.set(decoder.decode(u8.subarray(off + 46, off + 46 + nlen)), { method, csize, lho });
    off += 46 + nlen + elen + clen;
  }
  return {
    names: [...entries.keys()],
    has: (name) => entries.has(name),
    async read(name) {
      const entry = entries.get(name);
      if (!entry) return null;
      const nlen = dv.getUint16(entry.lho + 26, true);
      const elen = dv.getUint16(entry.lho + 28, true);
      const start = entry.lho + 30 + nlen + elen;
      const comp = u8.subarray(start, start + entry.csize);
      if (entry.method === 0) return new TextDecoder().decode(comp);
      if (entry.method === 8) {
        const DS = (globalThis as { DecompressionStream?: new (format: string) => GenericTransformStream }).DecompressionStream;
        if (!DS) throw new Error("이 브라우저는 ZIP 해제를 지원하지 않습니다 (최신 Chrome/Edge 필요)");
        const stream = new Blob([comp]).stream().pipeThrough(new DS("deflate-raw"));
        return await new Response(stream).text();
      }
      throw new Error(`지원하지 않는 압축 방식: ${entry.method}`);
    },
  };
}

/* ── 정규화 ── */
type RawObject = Record<string, unknown>;

function asString(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  return String(value);
}

function endpointId(value: unknown): string | null {
  if (value && typeof value === "object") return asString((value as RawObject).id);
  return asString(value);
}

function normalizeNode(raw: RawObject): LocalNode | null {
  const id = asString(raw.id ?? raw.key ?? raw.node_id ?? raw.uid);
  if (!id) return null;
  const props = (raw.properties && typeof raw.properties === "object" ? raw.properties : {}) as RawObject;
  const type = asString(
    props.bimgraph_node_kind ?? raw.node_type ?? raw.type ?? raw.group ?? raw.category ?? raw.space,
  ) ?? "node";
  const label = asString(raw.label ?? raw.title ?? raw.name ?? props.title ?? props.name) ?? id;
  const properties: Record<string, unknown> = { ...props };
  if (Array.isArray(raw.evidence_refs)) properties.evidence_count = raw.evidence_refs.length;
  return { id, label, type, color: colorForLocalType(type), size: 5, properties, packId: "__local__" };
}

function normalizeEdge(raw: RawObject, index: number): LocalEdge | null {
  const source = endpointId(raw.from_id ?? raw.source ?? raw.from);
  const target = endpointId(raw.to_id ?? raw.target ?? raw.to);
  if (!source || !target || source === target) return null;
  const props = (raw.properties && typeof raw.properties === "object" ? raw.properties : {}) as RawObject;
  const relation = asString(props.original_relation ?? raw.relation ?? raw.label ?? raw.type ?? raw.rel) ?? "";
  return {
    id: asString(raw.id) ?? `local-edge:${source}:${relation}:${target}:${index}`,
    source,
    target,
    relation,
    packId: "__local__",
  };
}

function parseJsonl(text: string | null): RawObject[] {
  if (!text) return [];
  const out: RawObject[] = [];
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    try {
      const obj = JSON.parse(trimmed);
      if (obj && typeof obj === "object") out.push(obj as RawObject);
    } catch {
      // 손상 라인은 건너뛴다
    }
  }
  return out;
}

/* ── 파일 파서 (.zip / .json / .jsonl) ── */
export async function parseLocalPackFile(file: File): Promise<ParsedLocalPack> {
  const head = new Uint8Array(await file.slice(0, 2).arrayBuffer());
  const isZip = (head[0] === 0x50 && head[1] === 0x4b) || /\.zip$/i.test(file.name);
  if (isZip) return parseZipPack(file);
  return parseTextPack(file);
}

async function parseZipPack(file: File): Promise<ParsedLocalPack> {
  const zip = await readZip(await file.arrayBuffer());
  let title = file.name.replace(/\.zip$/i, "");
  let nodesEntry: string | null = null;
  let edgesEntry: string | null = null;
  const manifestText = await zip.read("manifest.json");
  if (manifestText) {
    try {
      const manifest = JSON.parse(manifestText) as RawObject;
      if (typeof manifest.title === "string" && manifest.title) title = manifest.title;
      const entrypoints = manifest.entrypoints as RawObject | undefined;
      if (entrypoints) {
        nodesEntry = asString(entrypoints.nodes);
        edgesEntry = asString(entrypoints.edges);
      }
    } catch {
      // manifest가 없거나 손상돼도 graph/*.jsonl 폴백으로 진행
    }
  }
  const nodesName = nodesEntry && zip.has(nodesEntry) ? nodesEntry : zip.names.find((n) => /(^|\/)nodes\.jsonl$/i.test(n));
  const edgesName = edgesEntry && zip.has(edgesEntry) ? edgesEntry : zip.names.find((n) => /(^|\/)edges\.jsonl$/i.test(n));
  if (!nodesName && !edgesName) throw new Error("ZIP 안에 nodes.jsonl / edges.jsonl이 없습니다");
  const nodes = parseJsonl(nodesName ? await zip.read(nodesName) : null)
    .map(normalizeNode)
    .filter((n): n is LocalNode => n !== null);
  const edges = parseJsonl(edgesName ? await zip.read(edgesName) : null)
    .map((raw, index) => normalizeEdge(raw, index))
    .filter((e): e is LocalEdge => e !== null);
  return { title, nodes, edges };
}

// 엣지로 분류: 양 끝점 키가 있고, 노드 신원(node_type)이 없는 레코드만.
// from/to 같은 속성명을 가진 노드 레코드가 엣지로 오분류돼 사라지는 것을 막는다.
function looksLikeEdge(obj: RawObject): boolean {
  if (obj.node_type != null) return false;
  return (obj.from_id ?? obj.source ?? obj.from) != null && (obj.to_id ?? obj.target ?? obj.to) != null;
}

async function parseTextPack(file: File): Promise<ParsedLocalPack> {
  const text = await file.text();
  let rawNodes: RawObject[] = [];
  let rawEdges: RawObject[] = [];
  try {
    const parsed = JSON.parse(text) as RawObject | RawObject[];
    if (Array.isArray(parsed)) {
      for (const obj of parsed) {
        if (looksLikeEdge(obj)) rawEdges.push(obj);
        else rawNodes.push(obj);
      }
    } else {
      rawNodes = (parsed.nodes ?? parsed.vertices ?? []) as RawObject[];
      rawEdges = (parsed.edges ?? parsed.links ?? parsed.relationships ?? []) as RawObject[];
    }
  } catch {
    for (const obj of parseJsonl(text)) {
      if (looksLikeEdge(obj)) rawEdges.push(obj);
      else rawNodes.push(obj);
    }
  }
  if (!rawNodes.length && !rawEdges.length) throw new Error("nodes/edges 데이터를 찾지 못했습니다");
  const nodes = rawNodes.map(normalizeNode).filter((n): n is LocalNode => n !== null);
  const edges = rawEdges.map((raw, index) => normalizeEdge(raw, index)).filter((e): e is LocalEdge => e !== null);
  return { title: file.name, nodes, edges };
}

/* ── 병합: 노드 중복 제거 + 팩 간 참조 자리표시 승격 ── */
function placeholderNode(id: string): LocalNode {
  const parts = id.split(":");
  return {
    id,
    label: parts.length > 2 ? parts.slice(-2).join(":") : id,
    type: LOCAL_PLACEHOLDER_TYPE,
    color: colorForLocalType(LOCAL_PLACEHOLDER_TYPE),
    size: 4,
    properties: {},
    packId: "__local__",
  };
}

export function mergeLocalPacks(
  existing: { nodes: LocalNode[]; edges: LocalEdge[] } | null,
  packs: ParsedLocalPack[],
): LocalMergeResult {
  // React 상태가 참조 중인 기존 노드 객체를 제자리 변경(승격/크기 재계산)하지 않도록 얕은 복사
  const nodes: LocalNode[] = existing ? existing.nodes.map((n) => ({ ...n })) : [];
  const edges: LocalEdge[] = existing ? [...existing.edges] : [];
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const edgeKeys = new Set(edges.map((e) => `${e.source}${e.relation}${e.target}`));
  let addedNodes = 0;
  let addedEdges = 0;
  let resolvedRefs = 0;
  let placeholders = 0;

  for (const pack of packs) {
    for (const node of pack.nodes) {
      const found = byId.get(node.id);
      if (found) {
        if (found.type === LOCAL_PLACEHOLDER_TYPE && node.type !== LOCAL_PLACEHOLDER_TYPE) {
          found.label = node.label;
          found.type = node.type;
          found.color = node.color;
          found.properties = node.properties;
          resolvedRefs++;
        }
        continue;
      }
      byId.set(node.id, node);
      nodes.push(node);
      addedNodes++;
    }
    for (const edge of pack.edges) {
      const key = `${edge.source}${edge.relation}${edge.target}`;
      if (edgeKeys.has(key)) continue;
      for (const id of [edge.source, edge.target]) {
        if (!byId.has(id)) {
          const ph = placeholderNode(id);
          byId.set(id, ph);
          nodes.push(ph);
          placeholders++;
          addedNodes++;
        }
      }
      edgeKeys.add(key);
      edges.push(edge);
      addedEdges++;
    }
  }

  // 차수 기반 노드 크기
  const degree = new Map<string, number>();
  for (const edge of edges) {
    degree.set(edge.source, (degree.get(edge.source) ?? 0) + 1);
    degree.set(edge.target, (degree.get(edge.target) ?? 0) + 1);
  }
  for (const node of nodes) {
    node.size = 4 + Math.min(9, Math.sqrt(degree.get(node.id) ?? 0) * 1.2);
  }

  return { nodes, edges, addedNodes, addedEdges, resolvedRefs, placeholders };
}
