/// <reference lib="webworker" />

type ChunkKind = "nodes" | "edges" | "mixed";

type ChunkDescriptor = {
  kind?: ChunkKind | string;
};

type CompactNode = {
  id: string;
  originalId?: string;
  occurrence?: number;
  label: string;
  type: string;
  packId?: string;
  color: string;
  size: number;
  properties: Record<string, unknown>;
  x?: number;
  y?: number;
  placeholder?: boolean;
};

type PendingEdge = {
  id: string;
  source: unknown;
  target: unknown;
  relation: string;
  packId?: string;
};

type CompactEdge = {
  id: string;
  source: string;
  target: string;
  relation: string;
  packId?: string;
};

type WorkerRequest =
  | { type: "start" }
  | {
      type: "chunk";
      requestId: number;
      descriptor: ChunkDescriptor;
      buffer: ArrayBuffer;
    }
  | { type: "finish"; requestId: number };

const workerScope = self as unknown as DedicatedWorkerGlobalScope;
const textDecoder = new TextDecoder();

let nodes: CompactNode[] = [];
let edges: PendingEdge[] = [];

function finiteNumber(value: unknown): number | undefined {
  const numberValue = Number(value);
  return Number.isFinite(numberValue) ? numberValue : undefined;
}

function stringValue(value: unknown, fallback = ""): string {
  return value === undefined || value === null ? fallback : String(value);
}

function endpointValue(value: unknown): unknown {
  if (value && typeof value === "object" && "id" in value) {
    return (value as { id: unknown }).id;
  }
  return value;
}

function normalizeNode(value: unknown, index: number): CompactNode | null {
  if (Array.isArray(value)) {
    const id = stringValue(value[0]).trim();
    if (!id) return null;
    const packId = stringValue(value[3]).trim() || undefined;
    const placeholder = Boolean(value[7]);
    return {
      id,
      originalId: stringValue(value[10]).trim() || undefined,
      occurrence: finiteNumber(value[11]),
      label: stringValue(value[1], id),
      type: stringValue(value[2], placeholder ? "미해결참조" : "node"),
      packId,
      x: finiteNumber(value[4]),
      y: finiteNumber(value[5]),
      size: finiteNumber(value[6]) ?? 4,
      placeholder,
      color: stringValue(value[8]),
      properties:
        value[9] && typeof value[9] === "object"
          ? (value[9] as Record<string, unknown>)
          : packId
            ? { pack_id: packId, placeholder }
            : { placeholder },
    };
  }
  if (!value || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  const id = stringValue(record.id ?? record.nodeId ?? record.key).trim();
  if (!id) return null;
  const packId = stringValue(record.packId ?? record.pack_id).trim() || undefined;
  const placeholder = Boolean(record.placeholder ?? record.isPlaceholder);
  const properties =
    record.properties && typeof record.properties === "object"
      ? { ...(record.properties as Record<string, unknown>) }
      : {};
  if (packId && properties.pack_id === undefined) properties.pack_id = packId;
  if (placeholder && properties.placeholder === undefined) properties.placeholder = true;
  return {
    id,
    originalId: stringValue(record.originalId ?? record.original_id).trim() || undefined,
    occurrence: finiteNumber(record.occurrence),
    label: stringValue(record.label ?? record.name, id),
    type: stringValue(record.type ?? record.nodeType, placeholder ? "미해결참조" : "node"),
    packId,
    x: finiteNumber(record.x),
    y: finiteNumber(record.y),
    size: finiteNumber(record.size) ?? 4,
    placeholder,
    color: stringValue(record.color),
    properties,
  };
}

function normalizeEdge(value: unknown, index: number): PendingEdge | null {
  if (Array.isArray(value)) {
    return {
      id: stringValue(value[0], `edge:${edges.length + index}`),
      source: endpointValue(value[1]),
      target: endpointValue(value[2]),
      relation: stringValue(value[3], "related_to"),
      packId: stringValue(value[4]).trim() || undefined,
    };
  }
  if (!value || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  return {
    id: stringValue(record.id, `edge:${edges.length + index}`),
    source: endpointValue(record.source ?? record.from),
    target: endpointValue(record.target ?? record.to),
    relation: stringValue(record.relation ?? record.label, "related_to"),
    packId: stringValue(record.packId ?? record.pack_id).trim() || undefined,
  };
}

async function decodeBuffer(buffer: ArrayBuffer): Promise<unknown> {
  let decoded = buffer;
  const bytes = new Uint8Array(buffer);
  if (bytes.length >= 2 && bytes[0] === 0x1f && bytes[1] === 0x8b) {
    if (typeof DecompressionStream === "undefined") {
      throw new Error("이 브라우저는 gzip 그래프 청크 압축 해제를 지원하지 않습니다.");
    }
    const stream = new Blob([buffer]).stream().pipeThrough(new DecompressionStream("gzip"));
    decoded = await new Response(stream).arrayBuffer();
  }
  return JSON.parse(textDecoder.decode(decoded)) as unknown;
}

function chunkRecords(payload: unknown, descriptor: ChunkDescriptor) {
  const kind = String(descriptor.kind || "").toLowerCase();
  if (Array.isArray(payload)) {
    return kind === "edges"
      ? { nodeRecords: [] as unknown[], edgeRecords: payload }
      : { nodeRecords: payload, edgeRecords: [] as unknown[] };
  }
  if (!payload || typeof payload !== "object") {
    return { nodeRecords: [] as unknown[], edgeRecords: [] as unknown[] };
  }
  const record = payload as Record<string, unknown>;
  return {
    nodeRecords: Array.isArray(record.nodes) ? record.nodes : [],
    edgeRecords: Array.isArray(record.edges) ? record.edges : [],
  };
}

function addChunk(payload: unknown, descriptor: ChunkDescriptor) {
  const { nodeRecords, edgeRecords } = chunkRecords(payload, descriptor);
  for (let index = 0; index < nodeRecords.length; index += 1) {
    const node = normalizeNode(nodeRecords[index], index);
    if (node) nodes.push(node);
  }
  for (let index = 0; index < edgeRecords.length; index += 1) {
    const edge = normalizeEdge(edgeRecords[index], index);
    if (edge) edges.push(edge);
  }
}

function finishGraph() {
  const firstNodeById = new Map<string, string>();
  for (const node of nodes) {
    if (!firstNodeById.has(node.id)) firstNodeById.set(node.id, node.id);
  }

  const indexedNodeCount = nodes.length;
  let authoredNodeCount = 0;
  let placeholderCount = 0;
  let synthesizedPlaceholderCount = 0;
  for (const node of nodes) {
    if (node.placeholder) placeholderCount += 1;
    else authoredNodeCount += 1;
  }
  const ensureEndpoint = (raw: unknown, edge: PendingEdge, role: "source" | "target"): string => {
    let id = "";
    if (typeof raw === "number" && Number.isInteger(raw) && raw >= 0 && raw < indexedNodeCount) {
      id = nodes[raw]?.id ?? "";
    } else {
      id = stringValue(raw).trim();
    }
    if (!id) id = `__missing_${role}__:${edge.id}`;
    const existing = firstNodeById.get(id);
    if (existing) return existing;
    const placeholderId = id;
    const packId = edge.packId;
    nodes.push({
      id: placeholderId,
      label: id,
      type: "미해결참조",
      packId,
      color: "#a3a3a3",
      size: 3.5,
      placeholder: true,
      properties: {
        placeholder: true,
        unresolved_endpoint: id,
        ...(packId ? { pack_id: packId } : {}),
      },
    });
    firstNodeById.set(id, placeholderId);
    placeholderCount += 1;
    synthesizedPlaceholderCount += 1;
    return placeholderId;
  };

  const normalizedEdges: CompactEdge[] = edges.map((edge) => ({
    id: edge.id,
    source: ensureEndpoint(edge.source, edge, "source"),
    target: ensureEndpoint(edge.target, edge, "target"),
    relation: edge.relation,
    packId: edge.packId,
  }));

  return {
    nodes,
    edges: normalizedEdges,
    authoredNodeCount,
    placeholderCount,
    synthesizedPlaceholderCount,
  };
}

workerScope.onmessage = async (event: MessageEvent<WorkerRequest>) => {
  const message = event.data;
  try {
    if (message.type === "start") {
      nodes = [];
      edges = [];
      return;
    }
    if (message.type === "chunk") {
      const payload = await decodeBuffer(message.buffer);
      addChunk(payload, message.descriptor);
      workerScope.postMessage({
        type: "chunkDone",
        requestId: message.requestId,
        loadedNodes: nodes.length,
        loadedEdges: edges.length,
      });
      return;
    }
    if (message.type === "finish") {
      workerScope.postMessage({
        type: "result",
        requestId: message.requestId,
        result: finishGraph(),
      });
    }
  } catch (error) {
    workerScope.postMessage({
      type: "error",
      requestId: "requestId" in message ? message.requestId : -1,
      message: error instanceof Error ? error.message : String(error),
    });
  }
};
