// 온톨로지 그래프 뷰 — ontology-graph-explorer의 캔버스 엔진 이식판.
// sigma 사전 레이아웃 방식과 달리 즉시 렌더 + 라이브 물리(Barnes-Hut)로
// 수만 노드도 첫 화면이 바로 뜬다. 차수 필터·포커스(hop)·타입 레전드·
// 노드 상세 다이얼로그를 포함한다.
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { edgePath, indexEdges, neighborhood, visibleNodes } from "./graphModel";
import type { EdgePath, IndexedEdge } from "./graphModel";

export type OgNode = {
  id: string;
  label: string;
  type: string;
  color?: string;
  size?: number;
  properties?: Record<string, unknown>;
};

export type OgEdge = {
  id?: string;
  source: string | { id: string };
  target: string | { id: string };
  relation?: string;
};

type SimNode<N> = {
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

type SimEdge = IndexedEdge;

type LegendEntry = { type: string; count: number; color: string };

export type OgNeighborGroup = { rel: string; items: { id: string; label: string; color: string }[]; total: number };

/** 선택 노드의 상세 정보 — 인스펙터([노드 정보] 탭)가 렌더링한다 */
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
  /** AI 답변 등에서 참조된 노드들을 강조 (null = 해제). 숨겨진 노드도 강제 표시. */
  setHighlight: (ids: string[] | null) => void;
};

type EngineApi = {
  setSearch: (value: string) => void;
  setMinDeg: (value: number) => void;
  setHop: (value: number) => void;
  setRep: (value: number) => void;
  setLink: (value: number) => void;
  setFocus: (on: boolean) => void;
  setPaused: (on: boolean) => void;
  toggleType: (type: string) => void;
  reheat: () => void;
  fit: () => void;
  selectById: (id: string | null) => void;
};

// Astryx theme-neutral(light) 캔버스 팔레트
const THEME = {
  bg: "#ffffff",
  edge: "rgba(82, 82, 82, 0.18)",
  edgeDim: "rgba(82, 82, 82, 0.06)",
  sel: "#0074e2",
  rel: "#c0990e",
  text: "#171717",
  label: "#525252",
  halo: "rgba(255, 255, 255, 0.88)",
  placeholder: "#a3a3a3",
};
const FALLBACK_PALETTE = [
  "#6d9cfe", "#dd74f0", "#69ad67", "#e2883e", "#63ab9d", "#f273aa",
  "#c0990e", "#67a7b8", "#ff6f6c", "#737373", "#a05fb8", "#84c980",
];
const MAX_VISIBLE_DEFAULT = 4000;

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
  /** 서버가 절단(truncation)한 경우 전체 규모 표시용 — GraphPayload.stats */
  serverStats?: { totalNodes: number; totalEdges: number };
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const engineRef = useRef<EngineApi | null>(null);
  const callbacksRef = useRef({ onSelectNode, onDetail });
  callbacksRef.current = { onSelectNode, onDetail };

  const [legend, setLegend] = useState<LegendEntry[]>([]);
  const [hiddenTypes, setHiddenTypes] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState("");
  const [minDeg, setMinDeg] = useState(0);
  const [maxDegSlider, setMaxDegSlider] = useState(20);
  const [hop, setHop] = useState(1);
  const [rep, setRep] = useState(1);
  const [link, setLink] = useState(60);
  const [focusOn, setFocusOn] = useState(false);
  const [paused, setPaused] = useState(false);
  const [stats, setStats] = useState({ shownNodes: 0, shownEdges: 0, totalNodes: 0, totalEdges: 0, capped: false });
  const [controlHost, setControlHost] = useState<HTMLElement | null>(null);

  // 필터·물리 패널은 그래프 안이 아니라 왼쪽 사이드바(#graph-sidebar-controls)로 포털
  useEffect(() => {
    setControlHost(document.getElementById("graph-sidebar-controls"));
  }, [nodes, edges]);

  useEffect(() => {
    const container = containerRef.current;
    const canvas = canvasRef.current;
    if (!container || !canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    /* ── 데이터 준비 ── */
    const typeIndex = new Map<string, number>();
    const colorOf = (type: string, given?: string): string => {
      if (given) return given;
      if (type === "미해결참조") return THEME.placeholder;
      let index = typeIndex.get(type);
      if (index === undefined) {
        index = typeIndex.size;
        typeIndex.set(type, index);
      }
      return FALLBACK_PALETTE[index % FALLBACK_PALETTE.length];
    };

    const simNodes: SimNode<N>[] = [];
    const byId = new Map<string, SimNode<N>>();
    for (const input of nodes) {
      const id = String(input.id);
      if (byId.has(id)) continue;
      const node: SimNode<N> = {
        id,
        label: input.label || id,
        type: input.type || "node",
        color: colorOf(input.type || "node", input.color),
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
    const { links: simEdges, adjacency: adj, degrees } = indexEdges(new Set(byId.keys()), edges);
    for (const node of simNodes) {
      node.degree = degrees.get(node.id)!;
      node.r = 3.5 + Math.min(36, Math.sqrt(node.degree) * 1.6);
    }
    const rankedNodes = [...simNodes].sort((a, b) => b.degree - a.degree);

    // 골든앵글 나선 초기 배치 — 사전 레이아웃 계산 없이 바로 그리기 시작
    const GA = Math.PI * (3 - Math.sqrt(5));
    simNodes.forEach((node, i) => {
      const d = 14 * Math.sqrt(i + 1);
      node.x = d * Math.cos(i * GA);
      node.y = d * Math.sin(i * GA);
    });

    /* ── 엔진 상태 ── */
    let vNodes: SimNode<N>[] = [];
    let vEdges: SimEdge[] = [];
    let selected: SimNode<N> | null = null;
    let hovered: SimNode<N> | null = null;
    let aiHighlight: Set<string> | null = null; // AI 참조 노드 강조
    let selectedNeighbors = new Set<string>();

    let alpha = 0;
    let engMinDeg = 0;
    let engSearch = "";
    let engHop = 1;
    let engRep = 1;
    let engLink = 60;
    let engFocus = false;
    let engPaused = false;
    const typeOff = new Set<string>();
    const cam = { x: 0, y: 0, k: 1 };
    let W = 0;
    let H = 0;
    let dpr = 1;
    let disposed = false;
    let raf = 0;
    let focusTimer = 0;
    // Request a frame only while moving or after a visible change. Idle/paused graphs sleep.
    const invalidate = () => {
      if (!disposed && !raf) raf = requestAnimationFrame(frame);
    };

    // 대용량 가드: 표시 노드가 한도 이하가 되도록 최소 차수 자동 상향
    if (rankedNodes.length > MAX_VISIBLE_DEFAULT) engMinDeg = rankedNodes[MAX_VISIBLE_DEFAULT - 1].degree;

    const focusSet = (): Set<string> | null => {
      if (!engFocus || !selected) return null;
      return neighborhood(adj, selected.id, engHop);
    };

    const rebuildVisible = () => {
      const forced = new Set(aiHighlight);
      if (selected) forced.add(selected.id);
      const result = visibleNodes(rankedNodes, { minDegree: engMinDeg, hiddenTypes: typeOff, focus: focusSet(), forced, limit: MAX_VISIBLE_DEFAULT, search: engSearch });
      vNodes = result.nodes;
      const visible = new Set(vNodes.map((n) => n.id));
      vEdges = simEdges.filter((e) => visible.has(e.a) && visible.has(e.b));
      if (hovered && !visible.has(hovered.id)) hovered = null;
      setStats({ shownNodes: vNodes.length, shownEdges: vEdges.length, totalNodes: simNodes.length, totalEdges: simEdges.length, capped: result.capped });
      reheat(0.5);
    };

    const reheat = (value = 1) => {
      alpha = Math.max(alpha, value);
      invalidate();
    };

    /* ── 물리 (Barnes-Hut + 안정화 가드) ── */
    type Quad = { x: number; y: number; s: number; n: SimNode<N> | null; kids: (Quad | null)[] | null; mass: number; cx: number; cy: number };
    const buildQuad = (ns: SimNode<N>[]): Quad | null => {
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      for (const n of ns) {
        if (n.x < x0) x0 = n.x;
        if (n.x > x1) x1 = n.x;
        if (n.y < y0) y0 = n.y;
        if (n.y > y1) y1 = n.y;
      }
      const size = Math.max(x1 - x0, y1 - y0, 1);
      if (!isFinite(size) || !isFinite(x0) || !isFinite(y0)) return null;
      const root: Quad = { x: x0, y: y0, s: size, n: null, kids: null, mass: 0, cx: 0, cy: 0 };
      const insert = (q: Quad, node: SimNode<N>) => {
        let depth = 0;
        for (;;) {
          if (++depth > 120) return;
          if (q.kids) {
            const h = q.s / 2;
            const i = (node.x >= q.x + h ? 1 : 0) + (node.y >= q.y + h ? 2 : 0);
            if (!q.kids[i]) q.kids[i] = { x: q.x + (i & 1 ? h : 0), y: q.y + (i & 2 ? h : 0), s: h, n: null, kids: null, mass: 0, cx: 0, cy: 0 };
            q = q.kids[i]!;
            continue;
          }
          if (!q.n) {
            q.n = node;
            return;
          }
          if (q.s < 1e-6) return;
          const old = q.n;
          q.n = null;
          q.kids = [null, null, null, null];
          const h = q.s / 2;
          const i = (old.x >= q.x + h ? 1 : 0) + (old.y >= q.y + h ? 2 : 0);
          q.kids[i] = { x: q.x + (i & 1 ? h : 0), y: q.y + (i & 2 ? h : 0), s: h, n: old, kids: null, mass: 0, cx: 0, cy: 0 };
        }
      };
      for (const n of ns) insert(root, n);
      const agg = (q: Quad | null): void => {
        if (!q) return;
        if (q.n && !q.kids) {
          q.mass = q.n.r;
          q.cx = q.n.x;
          q.cy = q.n.y;
          return;
        }
        let m = 0, cx = 0, cy = 0;
        if (q.kids) {
          for (const k of q.kids) {
            if (!k) continue;
            agg(k);
            m += k.mass;
            cx += k.cx * k.mass;
            cy += k.cy * k.mass;
          }
        }
        q.mass = m || 1e-9;
        q.cx = cx / q.mass;
        q.cy = cy / q.mass;
      };
      agg(root);
      return root;
    };
    const repel = (node: SimNode<N>, quad: Quad, strength: number) => {
      const stack: Quad[] = [quad];
      while (stack.length) {
        const c = stack.pop()!;
        if (!c || c.mass === 0) continue;
        let dx = node.x - c.cx;
        let dy = node.y - c.cy;
        let d2 = dx * dx + dy * dy;
        if (c.kids && c.s * c.s > d2 * 0.72) {
          for (const k of c.kids) if (k) stack.push(k);
          continue;
        }
        if (c.n === node) continue;
        if (d2 < 25) {
          dx = Math.random() - 0.5;
          dy = Math.random() - 0.5;
          d2 = 25;
        }
        const f = (strength * c.mass) / d2;
        node.vx += dx * f;
        node.vy += dy * f;
      }
    };
    const tick = () => {
      if (engPaused || alpha < 0.003 || !vNodes.length) return;
      alpha *= 0.985;
      const strength = 220 * engRep * alpha;
      const useQuad = vNodes.length > 60;
      const quad = useQuad ? buildQuad(vNodes) : null;
      for (const n of vNodes) {
        if (quad) repel(n, quad, strength);
        else if (!useQuad) {
          for (const m of vNodes) {
            if (m === n) continue;
            let dx = n.x - m.x;
            let dy = n.y - m.y;
            let d2 = dx * dx + dy * dy;
            if (d2 < 25) d2 = 25;
            const f = (strength * m.r) / d2;
            n.vx += dx * f;
            n.vy += dy * f;
          }
        }
        n.vx -= n.x * 0.012 * alpha;
        n.vy -= n.y * 0.012 * alpha;
      }
      for (const e of vEdges) {
        if (e.a === e.b) continue; // Reflexive statements are drawn, not used as springs.
        const a = byId.get(e.a)!;
        const b = byId.get(e.b)!;
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const d = Math.sqrt(dx * dx + dy * dy) || 1;
        const f = ((d - engLink) / d) * 0.4 * alpha;
        const wa = b.r / (a.r + b.r);
        a.vx += dx * f * wa;
        a.vy += dy * f * wa;
        b.vx -= dx * f * (1 - wa);
        b.vy -= dy * f * (1 - wa);
      }
      for (const n of vNodes) {
        if (n.fx !== null && n.fy !== null) {
          n.x = n.fx;
          n.y = n.fy;
          n.vx = n.vy = 0;
          continue;
        }
        n.vx *= 0.82;
        n.vy *= 0.82;
        const speed = Math.hypot(n.vx, n.vy);
        if (speed > 50) {
          n.vx *= 50 / speed;
          n.vy *= 50 / speed;
        }
        n.x += n.vx;
        n.y += n.vy;
        if (!isFinite(n.x) || !isFinite(n.y)) {
          n.x = (Math.random() - 0.5) * 400;
          n.y = (Math.random() - 0.5) * 400;
          n.vx = n.vy = 0;
        }
      }
    };

    /* ── 렌더링 ── */
    const resize = () => {
      const rect = container.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      dpr = window.devicePixelRatio || 1;
      W = rect.width;
      H = rect.height;
      canvas.width = Math.round(W * dpr);
      canvas.height = Math.round(H * dpr);
      invalidate();
    };
    const draw = () => {
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.fillStyle = THEME.bg;
      ctx.fillRect(0, 0, W, H);
      ctx.translate(cam.x, cam.y);
      ctx.scale(cam.k, cam.k);

      const highlight = selectedNeighbors;
      const margin = 100 / cam.k;
      const left = -cam.x / cam.k - margin, right = (W - cam.x) / cam.k + margin;
      const top = -cam.y / cam.k - margin, bottom = (H - cam.y) / cam.k + margin;
      const onScreen = (n: SimNode<N>) => n.x + n.r >= left && n.x - n.r <= right && n.y + n.r >= top && n.y - n.r <= bottom;
      const batches: { edge: SimEdge; path: EdgePath }[][] = [[], [], []];
      for (const e of vEdges) {
        const path = edgePath(byId.get(e.a)!, byId.get(e.b)!, e);
        const c2 = path.control2 ?? path.control;
        if (Math.max(path.start.x, path.end.x, path.control.x, c2.x) < left || Math.min(path.start.x, path.end.x, path.control.x, c2.x) > right ||
          Math.max(path.start.y, path.end.y, path.control.y, c2.y) < top || Math.min(path.start.y, path.end.y, path.control.y, c2.y) > bottom) continue;
        const batch = selected && (e.a === selected.id || e.b === selected.id) ? 2 : aiHighlight?.has(e.a) && aiHighlight.has(e.b) ? 1 : 0;
        batches[batch].push({ edge: e, path });
      }
      for (let i = 0; i < batches.length; i++) {
        ctx.strokeStyle = i === 2 ? THEME.sel : i === 1 ? "rgba(192, 153, 14, 0.6)" : selected || aiHighlight ? THEME.edgeDim : THEME.edge;
        ctx.lineWidth = (i === 2 ? 1.8 : i === 1 ? 1.6 : 1) / cam.k;
        ctx.beginPath();
        for (const { path: p } of batches[i]) {
          ctx.moveTo(p.start.x, p.start.y);
          if (p.control2) ctx.bezierCurveTo(p.control.x, p.control.y, p.control2.x, p.control2.y, p.end.x, p.end.y);
          else ctx.quadraticCurveTo(p.control.x, p.control.y, p.end.x, p.end.y);
          // Keep an overview uncluttered; emphasized predicates always show direction.
          if (cam.k >= 0.2 || i > 0) {
            const arrow = Math.min(8 / cam.k, 12);
            ctx.moveTo(p.end.x - Math.cos(p.angle - 0.45) * arrow, p.end.y - Math.sin(p.angle - 0.45) * arrow);
            ctx.lineTo(p.end.x, p.end.y);
            ctx.lineTo(p.end.x - Math.cos(p.angle + 0.45) * arrow, p.end.y - Math.sin(p.angle + 0.45) * arrow);
          }
        }
        ctx.stroke();
      }
      if (selected && cam.k > 0.5) {
        ctx.fillStyle = THEME.rel;
        ctx.font = `${10 / cam.k}px sans-serif`;
        ctx.textAlign = "center";
        ctx.textBaseline = "bottom";
        // High-degree hubs keep the text pass bounded; all predicates remain in the inspector.
        for (const { edge, path } of batches[2].slice(0, 200)) {
          if (!edge.rel) continue;
          ctx.strokeStyle = THEME.halo;
          ctx.lineWidth = 3 / cam.k;
          ctx.strokeText(edge.rel, path.label.x, path.label.y - 3 / cam.k);
          ctx.fillText(edge.rel, path.label.x, path.label.y - 3 / cam.k);
        }
      }
      for (const n of vNodes) {
        if (!onScreen(n)) continue;
        const isSelHl = highlight.has(n.id);
        const isAiHl = aiHighlight !== null && aiHighlight.has(n.id);
        let nodeAlpha = 1;
        if (selected) nodeAlpha = isSelHl ? 1 : 0.18;
        else if (aiHighlight) nodeAlpha = isAiHl ? 1 : 0.15;
        ctx.globalAlpha = nodeAlpha;
        ctx.fillStyle = n.color;
        ctx.beginPath();
        ctx.arc(n.x, n.y, n.r, 0, 6.2832);
        ctx.fill();
        if (isAiHl) {
          ctx.strokeStyle = "#c0990e";
          ctx.lineWidth = 2.4 / cam.k;
          ctx.beginPath();
          ctx.arc(n.x, n.y, n.r + 2.5 / cam.k, 0, 6.2832);
          ctx.stroke();
        }
        if (n === selected || n === hovered) {
          ctx.strokeStyle = n === selected ? THEME.text : `${THEME.text}99`;
          ctx.lineWidth = 2 / cam.k;
          ctx.beginPath();
          ctx.arc(n.x, n.y, n.r + (isAiHl ? 5.5 : 3) / cam.k, 0, 6.2832);
          ctx.stroke();
        }
        ctx.globalAlpha = 1;
      }
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      let labelBudget = 350;
      const aiLabelable = aiHighlight !== null && aiHighlight.size <= 60;
      for (const n of vNodes) {
        if (!onScreen(n)) continue;
        const isHl = highlight.has(n.id) || hovered === n || (aiLabelable && aiHighlight!.has(n.id));
        const mustShow = n === selected || n === hovered || (aiLabelable && aiHighlight!.has(n.id));
        const show = mustShow || (labelBudget > 0 && (isHl || cam.k * n.r > 5));
        if (!show) continue;
        if (!mustShow) labelBudget--;
        let fsScreen = Math.max(10, Math.min(15, n.r * 1.4)) * Math.min(cam.k, 1.4);
        if (isHl) fsScreen = Math.max(fsScreen, 11);
        const fs = fsScreen / cam.k;
        ctx.font = `${fs}px Figtree, "Pretendard Variable", Pretendard, sans-serif`;
        ctx.globalAlpha = (selected || aiHighlight) && !isHl ? 0.25 : isHl ? 1 : 0.8;
        const label = n.label.length > 24 ? `${n.label.slice(0, 24)}…` : n.label;
        ctx.strokeStyle = THEME.halo;
        ctx.lineWidth = 3 / cam.k;
        ctx.strokeText(label, n.x, n.y + n.r + 3 / cam.k);
        ctx.fillStyle = isHl ? THEME.text : THEME.label;
        ctx.fillText(label, n.x, n.y + n.r + 3 / cam.k);
        ctx.globalAlpha = 1;
      }
    };
    const frame = () => {
      raf = 0;
      if (disposed) return;
      tick();
      draw();
      if (!engPaused && alpha >= 0.003 && vNodes.length) invalidate();
    };

    /* ── 카메라 · 인터랙션 ── */
    const toWorld = (sx: number, sy: number) => ({ x: (sx - cam.x) / cam.k, y: (sy - cam.y) / cam.k });
    const nodeAt = (sx: number, sy: number): SimNode<N> | null => {
      const w = toWorld(sx, sy);
      let best: SimNode<N> | null = null;
      let bestDist = Infinity;
      for (const n of vNodes) {
        const dx = n.x - w.x;
        const dy = n.y - w.y;
        const d = dx * dx + dy * dy;
        const hit = Math.max(n.r, 6 / cam.k) + 2 / cam.k;
        if (d < hit * hit && d < bestDist) {
          best = n;
          bestDist = d;
        }
      }
      return best;
    };
    const fitTo = (ns: SimNode<N>[]) => {
      if (!ns.length || !W || !H) return;
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      for (const n of ns) {
        x0 = Math.min(x0, n.x - n.r);
        x1 = Math.max(x1, n.x + n.r);
        y0 = Math.min(y0, n.y - n.r);
        y1 = Math.max(y1, n.y + n.r);
      }
      const pad = 70;
      cam.k = Math.max(0.001, Math.min(2.5, Math.max(W - pad * 2, W * 0.2) / Math.max(x1 - x0, 10), Math.max(H - pad * 2, H * 0.2) / Math.max(y1 - y0, 10)));
      cam.x = W / 2 - ((x0 + x1) / 2) * cam.k;
      cam.y = H / 2 - ((y0 + y1) / 2) * cam.k;
      invalidate();
    };
    const fitView = () => fitTo(vNodes);
    const applyHighlight = (ids: string[] | null) => {
      // 현재 그래프에 존재하는 id만 강조 — 이전 그래프의 스테일 id로
      // 전체가 흐려지고 아무것도 강조되지 않는 상태 방지
      const valid = ids ? ids.filter((id) => byId.has(id)) : [];
      aiHighlight = valid.length ? new Set(valid) : null;
      rebuildVisible();
      if (aiHighlight) fitTo(vNodes.filter((n) => aiHighlight!.has(n.id)));
    };
    const centerOn = (n: SimNode<N>) => {
      cam.k = Math.max(cam.k, 1.1);
      cam.x = W / 2 - n.x * cam.k;
      cam.y = H / 2 - n.y * cam.k;
      invalidate();
    };

    const buildDetail = (node: SimNode<N>): OgDetail<N> => {
      const groups = new Map<string, OgNeighborGroup>();
      for (const nb of adj.get(node.id) ?? []) {
        const key = `${nb.dir} ${nb.rel || "related"}`;
        let group = groups.get(key);
        if (!group) {
          group = { rel: key, items: [], total: 0 };
          groups.set(key, group);
        }
        group.total++;
        if (group.items.length < 40) {
          const m = byId.get(nb.id)!;
          group.items.push({ id: m.id, label: m.label, color: m.color });
        }
      }
      return {
        node: node.input,
        id: node.id,
        label: node.label,
        type: node.type,
        color: node.color,
        degree: node.degree,
        props: node.props,
        groups: [...groups.values()],
      };
    };
    const select = (node: SimNode<N> | null) => {
      selected = node;
      selectedNeighbors = node ? neighborhood(adj, node.id, 1) : new Set();
      // Recompute on every selection: the previous forced node may need hiding again.
      rebuildVisible();
      callbacksRef.current.onDetail?.(node ? buildDetail(node) : null);
      callbacksRef.current.onSelectNode?.(node ? node.input : null);
    };

    const local = (ev: PointerEvent | WheelEvent | MouseEvent) => {
      const rect = canvas.getBoundingClientRect();
      return { x: ev.clientX - rect.left, y: ev.clientY - rect.top };
    };
    let dragNode: SimNode<N> | null = null;
    let panning = false;
    let moved = false;
    let px = 0;
    let py = 0;
    let startX = 0;
    let startY = 0;
    let pointerId: number | null = null;
    const onPointerDown = (ev: PointerEvent) => {
      if (ev.button !== 0 || pointerId !== null) return;
      pointerId = ev.pointerId;
      try {
        canvas.setPointerCapture(ev.pointerId);
      } catch {
        // 합성 이벤트 등 비활성 포인터는 캡처 없이 진행
      }
      const p = local(ev);
      px = p.x;
      py = p.y;
      startX = p.x;
      startY = p.y;
      moved = false;
      dragNode = nodeAt(p.x, p.y);
      panning = !dragNode;
      canvas.style.cursor = "grabbing";
    };
    const onPointerMove = (ev: PointerEvent) => {
      if (pointerId !== null && ev.pointerId !== pointerId) return;
      const p = local(ev);
      const dx = p.x - px;
      const dy = p.y - py;
      if ((dragNode || panning) && Math.hypot(p.x - startX, p.y - startY) > 3) moved = true;
      if (dragNode) {
        const w = toWorld(p.x, p.y);
        dragNode.fx = w.x;
        dragNode.fy = w.y;
        // Drag remains responsive with the simulation paused.
        dragNode.x = w.x;
        dragNode.y = w.y;
        dragNode.vx = dragNode.vy = 0;
        reheat(0.35);
      } else if (panning) {
        cam.x += dx;
        cam.y += dy;
      } else {
        hovered = nodeAt(p.x, p.y);
        canvas.style.cursor = hovered ? "pointer" : "grab";
      }
      px = p.x;
      py = p.y;
      invalidate();
    };
    const onPointerUp = (ev: PointerEvent) => {
      if (ev.pointerId !== pointerId) return;
      canvas.style.cursor = "grab";
      if (dragNode) dragNode.fx = dragNode.fy = null;
      if (!moved) {
        const p = local(ev);
        select(nodeAt(p.x, p.y));
      }
      dragNode = null;
      panning = false;
      pointerId = null;
      if (canvas.hasPointerCapture(ev.pointerId)) canvas.releasePointerCapture(ev.pointerId);
      invalidate();
    };
    const onPointerCancel = (ev: PointerEvent) => {
      if (ev.pointerId !== pointerId) return;
      if (dragNode) dragNode.fx = dragNode.fy = null;
      dragNode = null;
      panning = false;
      pointerId = null;
      canvas.style.cursor = "grab";
      invalidate();
    };
    const onPointerLeave = () => {
      if (pointerId !== null) return;
      hovered = null;
      invalidate();
    };
    const onWheel = (ev: WheelEvent) => {
      ev.preventDefault();
      const p = local(ev);
      const factor = Math.pow(1.0015, -ev.deltaY);
      const k2 = Math.min(16, Math.max(0.001, cam.k * factor));
      const w = toWorld(p.x, p.y);
      cam.x = p.x - w.x * k2;
      cam.y = p.y - w.y * k2;
      cam.k = k2;
      invalidate();
    };

    canvas.addEventListener("pointerdown", onPointerDown);
    canvas.addEventListener("pointermove", onPointerMove);
    canvas.addEventListener("pointerup", onPointerUp);
    canvas.addEventListener("pointercancel", onPointerCancel);
    canvas.addEventListener("lostpointercapture", onPointerCancel);
    canvas.addEventListener("pointerleave", onPointerLeave);
    canvas.addEventListener("wheel", onWheel, { passive: false });
    const observer = new ResizeObserver(() => {
      resize();
    });
    observer.observe(container);

    /* ── React 패널 ↔ 엔진 API ── */
    engineRef.current = {
      setSearch: (value) => {
        engSearch = value;
        rebuildVisible();
        fitView();
        window.clearTimeout(focusTimer);
        focusTimer = window.setTimeout(fitView, 400);
      },
      setMinDeg: (value) => {
        engMinDeg = value;
        rebuildVisible();
      },
      setHop: (value) => {
        engHop = value;
        if (engFocus) rebuildVisible();
      },
      setRep: (value) => {
        engRep = value;
        reheat(0.6);
      },
      setLink: (value) => {
        engLink = value;
        reheat(0.6);
      },
      setFocus: (on) => {
        engFocus = on;
        rebuildVisible();
        window.clearTimeout(focusTimer);
        if (on && selected) focusTimer = window.setTimeout(fitView, 400);
      },
      setPaused: (on) => {
        engPaused = on;
        if (!on) reheat(0.3);
        else invalidate();
      },
      toggleType: (type) => {
        if (typeOff.has(type)) typeOff.delete(type);
        else typeOff.add(type);
        rebuildVisible();
      },
      reheat: () => reheat(1),
      fit: fitView,
      selectById: (id) => {
        const node = id ? byId.get(id) ?? null : null;
        select(node);
        if (node) centerOn(node);
      },
    };

    /* ── 초기화 ── */
    const counts = new Map<string, { count: number; color: string }>();
    for (const n of simNodes) {
      const entry = counts.get(n.type);
      if (entry) entry.count++;
      else counts.set(n.type, { count: 1, color: n.color });
    }
    setLegend([...counts.entries()].map(([type, v]) => ({ type, count: v.count, color: v.color })).sort((a, b) => b.count - a.count));
    setHiddenTypes(new Set());
    setSearch("");
    setMinDeg(engMinDeg);
    setMaxDegSlider(Math.max(20, engMinDeg + 10));
    setHop(1);
    setFocusOn(false);
    setPaused(false);
    setRep(1);
    setLink(60);
    callbacksRef.current.onDetail?.(null);
    callbacksRef.current.onSelectNode?.(null); // 그래프 교체 시 부모의 selectedNode(AI 컨텍스트)도 함께 초기화
    if (controllerRef)
      controllerRef.current = {
        selectById: (id) => engineRef.current?.selectById(id),
        setHighlight: (ids) => applyHighlight(ids),
      };

    resize();
    cam.x = W / 2;
    cam.y = H / 2;
    rebuildVisible();
    reheat(1);
    const fitTimer = window.setTimeout(fitView, 600);
    invalidate();
    // 헤드리스 검증/e2e용 디버그 훅 — 숨김 탭에서도 수동으로 프레임을 돌릴 수 있다
    (container as HTMLDivElement & { __og?: unknown }).__og = {
      pump: (frames = 1) => {
        for (let i = 0; i < frames; i++) tick();
        draw();
      },
      fit: fitView,
      stats: () => ({ visible: vNodes.length, edges: vEdges.length, alpha, k: cam.k, framePending: Boolean(raf), paused: engPaused }),
      select: (id: string | null) => {
        select(id ? byId.get(id) ?? null : null);
        draw();
      },
      highlight: (ids: string[] | null) => {
        applyHighlight(ids);
        draw();
      },
      firstVisibleId: () => (vNodes.length ? vNodes.reduce((a, b) => (a.degree > b.degree ? a : b)).id : null),
    };

    return () => {
      disposed = true;
      cancelAnimationFrame(raf);
      window.clearTimeout(fitTimer);
      window.clearTimeout(focusTimer);
      observer.disconnect();
      canvas.removeEventListener("pointerdown", onPointerDown);
      canvas.removeEventListener("pointermove", onPointerMove);
      canvas.removeEventListener("pointerup", onPointerUp);
      canvas.removeEventListener("pointercancel", onPointerCancel);
      canvas.removeEventListener("lostpointercapture", onPointerCancel);
      canvas.removeEventListener("pointerleave", onPointerLeave);
      canvas.removeEventListener("wheel", onWheel);
      engineRef.current = null;
      if (controllerRef) controllerRef.current = null;
      delete (container as HTMLDivElement & { __og?: unknown }).__og;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodes, edges]);

  const engine = () => engineRef.current;

  const controlsPanel = (
    <aside className={controlHost ? "og-panel og-controls in-sidebar" : "og-panel og-controls"} aria-label="필터 · 물리">
      <div className="og-panel-title">필터 · 물리</div>
      <label>
        <span>노드 검색 (이름·ID)</span>
        <input
          type="search"
          value={search}
          placeholder="로드된 노드에서 검색"
          title="차수·타입·포커스 필터도 함께 적용됩니다. 선택·강조 노드는 계속 표시됩니다."
          style={{ height: 30, padding: "4px 6px", boxSizing: "border-box" }}
          onChange={(ev) => {
            const value = ev.target.value;
            setSearch(value);
            engine()?.setSearch(value);
          }}
        />
      </label>
      <label>
        <span>최소 차수 (degree)</span>
        <strong>{minDeg}</strong>
        <input
          type="range"
          min={0}
          max={maxDegSlider}
          step={1}
          value={minDeg}
          onChange={(ev) => {
            const value = Number(ev.target.value);
            setMinDeg(value);
            engine()?.setMinDeg(value);
          }}
        />
      </label>
      <label>
        <span>포커스 반경 (hop)</span>
        <strong>{hop}</strong>
        <input
          type="range"
          min={1}
          max={4}
          step={1}
          value={hop}
          onChange={(ev) => {
            const value = Number(ev.target.value);
            setHop(value);
            engine()?.setHop(value);
          }}
        />
      </label>
      <label>
        <span>반발력</span>
        <strong>{rep.toFixed(1)}</strong>
        <input
          type="range"
          min={0.2}
          max={3}
          step={0.1}
          value={rep}
          onChange={(ev) => {
            const value = Number(ev.target.value);
            setRep(value);
            engine()?.setRep(value);
          }}
        />
      </label>
      <label>
        <span>링크 거리</span>
        <strong>{link}</strong>
        <input
          type="range"
          min={20}
          max={200}
          step={5}
          value={link}
          onChange={(ev) => {
            const value = Number(ev.target.value);
            setLink(value);
            engine()?.setLink(value);
          }}
        />
      </label>
      <div className="og-controls-row">
        <button type="button" onClick={() => engine()?.reheat()}>
          🔥 재가열
        </button>
        <button
          type="button"
          onClick={() => {
            const next = !paused;
            setPaused(next);
            engine()?.setPaused(next);
          }}
        >
          {paused ? "▶ 재개" : "⏸ 일시정지"}
        </button>
        <button
          type="button"
          className={focusOn ? "on" : ""}
          onClick={() => {
            const next = !focusOn;
            setFocusOn(next);
            engine()?.setFocus(next);
          }}
        >
          ◎ 포커스
        </button>
      </div>
    </aside>
  );

  return (
    <div ref={containerRef} className="og-stage">
      <canvas ref={canvasRef} className="og-canvas" />

      <aside className="og-panel og-legend" aria-label="노드 타입">
        <div className="og-panel-title">
          노드 타입 <em>클릭=토글</em>
        </div>
        {legend.map((entry) => (
          <button
            key={entry.type}
            type="button"
            className={hiddenTypes.has(entry.type) ? "og-legend-item off" : "og-legend-item"}
            onClick={() => {
              engine()?.toggleType(entry.type);
              setHiddenTypes((prev) => {
                const next = new Set(prev);
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
        노드 <strong>{stats.shownNodes.toLocaleString()}</strong> / {stats.totalNodes.toLocaleString()} · 엣지{" "}
        <strong>{stats.shownEdges.toLocaleString()}</strong> / {stats.totalEdges.toLocaleString()}
        {stats.capped ? <em className="og-stats-truncated"> · 표시 한도 {MAX_VISIBLE_DEFAULT.toLocaleString()}개 (선택·강조 노드 우선)</em> : null}
        {serverStats && (serverStats.totalNodes > stats.totalNodes || serverStats.totalEdges > stats.totalEdges) ? (
          <em className="og-stats-truncated">
            {" "}· 서버 전체 {serverStats.totalNodes.toLocaleString()} / {serverStats.totalEdges.toLocaleString()} 중 일부만 로드됨
          </em>
        ) : null}
      </div>

    </div>
  );
}
