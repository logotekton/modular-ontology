import { getJson, request } from "../api/http";

export type FullGraphChunkDescriptor = {
  url: string;
  file?: string;
  kind?: "nodes" | "edges" | "mixed" | string;
  count?: number;
  compressedBytes?: number;
  uncompressedBytes?: number;
  sha256?: string;
};

export type FullGraphManifest = {
  version?: string | number;
  algorithm?: string;
  layout?: string;
  hash?: string;
  selectionKey?: string;
  projectId?: string;
  projectName?: string;
  packIds?: string[];
  packCount?: number;
  totalNodes?: number;
  totalEdges?: number;
  project?: Record<string, unknown>;
  pack?: Record<string, unknown>;
  packs?: Record<string, unknown>[];
  activePackIds?: string[];
  chunks: FullGraphChunkDescriptor[];
  totals?: {
    nodes?: number;
    edges?: number;
    totalNodes?: number;
    totalEdges?: number;
  };
  stats?: {
    authoredNodes?: number;
    placeholderNodes?: number;
    authoredEdges?: number;
    resolvedEdges?: number;
    unresolvedEdges?: number;
    placeholderEndpointReferences?: number;
    nodes?: number;
    edges?: number;
    totalNodes?: number;
    totalEdges?: number;
  };
  diagnostics?: Record<string, unknown>;
};

export type FullGraphNode = {
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

export type FullGraphEdge = {
  id: string;
  source: string;
  target: string;
  relation: string;
  packId?: string;
};

export type FullGraphProgress = {
  phase: "manifest" | "download" | "decode" | "ready";
  loadedBytes: number;
  totalBytes: number;
  loadedChunks: number;
  totalChunks: number;
  loadedNodes: number;
  totalNodes: number;
  loadedEdges: number;
  totalEdges: number;
};

export type FullGraphResult = {
  manifest: FullGraphManifest;
  nodes: FullGraphNode[];
  edges: FullGraphEdge[];
  authoredNodeCount: number;
  placeholderCount: number;
  synthesizedPlaceholderCount: number;
  transferredBytes: number;
};

type WorkerReply =
  | {
      type: "chunkDone";
      requestId: number;
      loadedNodes: number;
      loadedEdges: number;
    }
  | {
      type: "result";
      requestId: number;
      result: {
        nodes: FullGraphNode[];
        edges: FullGraphEdge[];
        authoredNodeCount: number;
        placeholderCount: number;
        synthesizedPlaceholderCount: number;
      };
    }
  | { type: "error"; requestId: number; message: string };

const FULL_GRAPH_DOWNLOAD_CONCURRENCY = 4;

function manifestTotals(manifest: FullGraphManifest) {
  const totals = (manifest.totals ?? manifest.stats ?? {}) as Record<string, unknown>;
  return {
    nodes: Number(manifest.totalNodes ?? totals.totalNodes ?? totals.nodes ?? 0) || 0,
    edges: Number(manifest.totalEdges ?? totals.totalEdges ?? totals.edges ?? 0) || 0,
  };
}

function expectedCount(value: unknown): number | null {
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : null;
}

function assertManifestCounts(
  manifest: FullGraphManifest,
  result: {
    nodes: FullGraphNode[];
    edges: FullGraphEdge[];
    authoredNodeCount: number;
    placeholderCount: number;
    synthesizedPlaceholderCount: number;
  },
) {
  const expected = {
    totalNodes: expectedCount(manifest.totalNodes ?? manifest.stats?.totalNodes),
    totalEdges: expectedCount(manifest.totalEdges ?? manifest.stats?.totalEdges),
    authoredNodes: expectedCount(manifest.stats?.authoredNodes),
    placeholderNodes: expectedCount(manifest.stats?.placeholderNodes),
  };
  const actual = {
    totalNodes: result.nodes.length,
    totalEdges: result.edges.length,
    authoredNodes: result.authoredNodeCount,
    placeholderNodes: result.placeholderCount,
  };
  for (const key of Object.keys(expected) as Array<keyof typeof expected>) {
    const expectedValue = expected[key];
    if (expectedValue !== null && expectedValue !== actual[key]) {
      throw new Error(
        `전체 그래프 무결성 검사 실패: ${key} ${actual[
          key
        ].toLocaleString()} / ${expectedValue.toLocaleString()}`,
      );
    }
  }
  if (Number(manifest.version) === 3 && result.synthesizedPlaceholderCount !== 0) {
    throw new Error(
      `전체 그래프 무결성 검사 실패: 누락된 endpoint ${result.synthesizedPlaceholderCount.toLocaleString()}개`,
    );
  }
}

function sameOrigin(url: URL) {
  return url.origin === window.location.origin;
}

function waitForWorker<T extends WorkerReply>(
  worker: Worker,
  requestId: number,
  expectedType: T["type"],
  signal: AbortSignal,
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => {
      cleanup();
      reject(new DOMException("Aborted", "AbortError"));
    };
    const onError = (event: ErrorEvent) => {
      cleanup();
      reject(new Error(event.message || "그래프 워커 실행에 실패했습니다."));
    };
    const onMessage = (event: MessageEvent<WorkerReply>) => {
      const message = event.data;
      if (message.requestId !== requestId) return;
      if (message.type === "error") {
        cleanup();
        reject(new Error(message.message));
        return;
      }
      if (message.type !== expectedType) return;
      cleanup();
      resolve(message as T);
    };
    const cleanup = () => {
      signal.removeEventListener("abort", onAbort);
      worker.removeEventListener("error", onError);
      worker.removeEventListener("message", onMessage);
    };
    signal.addEventListener("abort", onAbort, { once: true });
    worker.addEventListener("error", onError);
    worker.addEventListener("message", onMessage);
  });
}

export async function getFullGraphManifest(options: {
  projectId: string;
  packIds: string[];
  token?: string;
  signal: AbortSignal;
}): Promise<FullGraphManifest> {
  const query = new URLSearchParams({ pack_ids: options.packIds.join(",") });
  const path = `/api/projects/${encodeURIComponent(options.projectId)}/graph/full/manifest?${query.toString()}`;
  const manifest = await getJson<FullGraphManifest>(path, {
    token: options.token,
    signal: options.signal,
  });
  if (!Array.isArray(manifest.chunks)) {
    throw new Error("전체 그래프 아티팩트 응답에 청크 목록이 없습니다.");
  }
  return manifest;
}

export async function loadFullGraph(options: {
  manifest: FullGraphManifest;
  token?: string;
  signal: AbortSignal;
  onProgress?: (progress: FullGraphProgress) => void;
}): Promise<FullGraphResult> {
  const { manifest, signal, token, onProgress } = options;
  const totals = manifestTotals(manifest);
  const totalBytes = manifest.chunks.reduce(
    (sum, chunk) => sum + Math.max(0, Number(chunk.compressedBytes) || 0),
    0,
  );
  const progress: FullGraphProgress = {
    phase: "download",
    loadedBytes: 0,
    totalBytes,
    loadedChunks: 0,
    totalChunks: manifest.chunks.length,
    loadedNodes: 0,
    totalNodes: totals.nodes,
    loadedEdges: 0,
    totalEdges: totals.edges,
  };
  onProgress?.({ ...progress });

  const worker = new Worker(new URL("./fullGraph.worker.ts", import.meta.url), { type: "module" });
  const abortWorker = () => worker.terminate();
  signal.addEventListener("abort", abortWorker, { once: true });
  worker.postMessage({ type: "start" });

  try {
    const kindOrder = (kind?: string) => {
      if (kind === "nodes") return 0;
      if (kind === "mixed") return 1;
      if (kind === "edges") return 2;
      return 1;
    };
    const orderedChunks = manifest.chunks
      .map((descriptor, index) => ({ descriptor, index }))
      .sort(
        (left, right) =>
          kindOrder(left.descriptor.kind) - kindOrder(right.descriptor.kind) ||
          left.index - right.index,
      );
    let requestId = 1;

    // Keep only a small batch of decoded response buffers on the main thread.
    // Every buffer is transferred (not copied) to the worker immediately after
    // its batch finishes, bounding peak memory while retaining parallel I/O.
    for (
      let offset = 0;
      offset < orderedChunks.length;
      offset += FULL_GRAPH_DOWNLOAD_CONCURRENCY
    ) {
      const batch = orderedChunks.slice(offset, offset + FULL_GRAPH_DOWNLOAD_CONCURRENCY);
      const fetched = await Promise.all(
        batch.map(async ({ descriptor, index }) => {
          const resolvedUrl = new URL(descriptor.url, window.location.origin);
          const response = await request(resolvedUrl, {
            token: sameOrigin(resolvedUrl) ? token : undefined,
            signal,
            headers: { Accept: "application/octet-stream, application/json" },
          });
          const buffer = await response.arrayBuffer();
          // fetch() transparently expands Content-Encoding:gzip. Track the
          // compressed transfer size from the immutable manifest, not the
          // decoded ArrayBuffer size, so progress never exceeds 100%.
          const declaredBytes = Math.max(0, Number(descriptor.compressedBytes) || 0);
          const responseBytes = Math.max(0, Number(response.headers.get("content-length")) || 0);
          const transferredBytes = declaredBytes || responseBytes || buffer.byteLength;
          if (!declaredBytes) progress.totalBytes += transferredBytes;
          progress.loadedBytes += transferredBytes;
          if (progress.totalBytes > 0) {
            progress.loadedBytes = Math.min(progress.loadedBytes, progress.totalBytes);
          }
          onProgress?.({ ...progress });
          return { descriptor, index, buffer };
        }),
      );
      fetched.sort((left, right) => left.index - right.index);
      progress.phase = "decode";
      onProgress?.({ ...progress });
      for (const chunk of fetched) {
        if (signal.aborted) throw new DOMException("Aborted", "AbortError");
        const currentRequestId = requestId;
        requestId += 1;
        const replyPromise = waitForWorker<
          Extract<WorkerReply, { type: "chunkDone" }>
        >(worker, currentRequestId, "chunkDone", signal);
        worker.postMessage(
          {
            type: "chunk",
            requestId: currentRequestId,
            descriptor: chunk.descriptor,
            buffer: chunk.buffer,
          },
          [chunk.buffer],
        );
        const reply = await replyPromise;
        progress.loadedChunks += 1;
        progress.loadedNodes = reply.loadedNodes;
        progress.loadedEdges = reply.loadedEdges;
        onProgress?.({ ...progress });
      }
    }

    const finishRequestId = requestId;
    const resultPromise = waitForWorker<Extract<WorkerReply, { type: "result" }>>(
      worker,
      finishRequestId,
      "result",
      signal,
    );
    worker.postMessage({ type: "finish", requestId: finishRequestId });
    const reply = await resultPromise;
    assertManifestCounts(manifest, reply.result);
    progress.phase = "ready";
    progress.loadedBytes = progress.totalBytes || progress.loadedBytes;
    progress.loadedChunks = progress.totalChunks;
    progress.loadedNodes = reply.result.nodes.length;
    progress.loadedEdges = reply.result.edges.length;
    onProgress?.({ ...progress });
    return {
      manifest,
      ...reply.result,
      transferredBytes: progress.loadedBytes,
    };
  } finally {
    signal.removeEventListener("abort", abortWorker);
    worker.terminate();
  }
}
