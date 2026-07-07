// 온톨로지 그래프 뷰 — ontology-graph-explorer의 캔버스 엔진 이식판.
// sigma 사전 레이아웃 방식과 달리 즉시 렌더 + 라이브 물리(Barnes-Hut)로
// 수만 노드도 첫 화면이 바로 뜬다. 차수 필터·포커스(hop)·타입 레전드·
// 노드 상세 다이얼로그를 포함한다.
import { useEffect, useRef, useState } from "react";

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

type SimEdge = { a: string; b: string; rel: string };

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

export type OgController = { selectById: (id: string | null) => void };

type EngineApi = {
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

function endpointId(value: string | { id: string }): string {
  return typeof value === "object" ? String(value.id) : String(value);
}

export function OntologyGraph<N extends OgNode, E extends OgEdge>({
  nodes,
  edges,
  onSelectNode,
  onDetail,
  controllerRef,
}: {
  nodes: N[];
  edges: E[];
  onSelectNode?: (node: N | null) => void;
  onDetail?: (detail: OgDetail<N> | null) => void;
  controllerRef?: { current: OgController | null };
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const engineRef = useRef<EngineApi | null>(null);

  const [legend, setLegend] = useState<LegendEntry[]>([]);
  const [hiddenTypes, setHiddenTypes] = useState<Set<string>>(new Set());
  const [minDeg, setMinDeg] = useState(0);
  const [maxDegSlider, setMaxDegSlider] = useState(20);
  const [hop, setHop] = useState(1);
  const [rep, setRep] = useState(1);
  const [link, setLink] = useState(60);
  const [focusOn, setFocusOn] = useState(false);
  const [paused, setPaused] = useState(false);
  const [stats, setStats] = useState({ shownNodes: 0, shownEdges: 0, totalNodes: 0, totalEdges: 0 });

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
    const simEdges: SimEdge[] = [];
    const adj = new Map<string, { id: string; rel: string; dir: string }[]>();
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
      adj.get(a)!.push({ id: b, rel, dir: "→" });
      adj.get(b)!.push({ id: a, rel, dir: "←" });
      na.degree++;
      nb.degree++;
    }
    for (const node of simNodes) node.r = 3.5 + Math.min(36, Math.sqrt(node.degree) * 1.6);

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
    let alpha = 0;
    let engMinDeg = 0;
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

    // 대용량 가드: 표시 노드가 한도 이하가 되도록 최소 차수 자동 상향
    while (engMinDeg < 200 && simNodes.filter((n) => n.degree >= engMinDeg).length > MAX_VISIBLE_DEFAULT) engMinDeg++;

    const focusSet = (): Set<string> | null => {
      if (!engFocus || !selected) return null;
      const set = new Set<string>([selected.id]);
      let frontier = [selected.id];
      for (let h = 0; h < engHop; h++) {
        const next: string[] = [];
        for (const id of frontier) {
          for (const nb of adj.get(id) ?? []) {
            if (!set.has(nb.id)) {
              set.add(nb.id);
              next.push(nb.id);
            }
          }
        }
        frontier = next;
      }
      return set;
    };

    const rebuildVisible = () => {
      const fs = focusSet();
      vNodes = simNodes.filter((n) => n.degree >= engMinDeg && !typeOff.has(n.type) && (!fs || fs.has(n.id)));
      const visible = new Set(vNodes.map((n) => n.id));
      vEdges = simEdges.filter((e) => visible.has(e.a) && visible.has(e.b));
      setStats({ shownNodes: vNodes.length, shownEdges: vEdges.length, totalNodes: simNodes.length, totalEdges: simEdges.length });
      reheat(0.5);
    };

    const reheat = (value = 1) => {
      alpha = Math.max(alpha, value);
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
    };
    const draw = () => {
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.fillStyle = THEME.bg;
      ctx.fillRect(0, 0, W, H);
      ctx.translate(cam.x, cam.y);
      ctx.scale(cam.k, cam.k);

      const highlight = new Set<string>();
      if (selected) {
        highlight.add(selected.id);
        for (const nb of adj.get(selected.id) ?? []) highlight.add(nb.id);
      }

      ctx.lineWidth = 1 / cam.k;
      ctx.strokeStyle = selected ? THEME.edgeDim : THEME.edge;
      ctx.beginPath();
      for (const e of vEdges) {
        if (selected && (e.a === selected.id || e.b === selected.id)) continue;
        const a = byId.get(e.a)!;
        const b = byId.get(e.b)!;
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
      }
      ctx.stroke();
      if (selected) {
        const lit: SimEdge[] = [];
        ctx.strokeStyle = THEME.sel;
        ctx.globalAlpha = 0.85;
        ctx.lineWidth = 1.8 / cam.k;
        ctx.beginPath();
        for (const e of vEdges) {
          if (e.a !== selected.id && e.b !== selected.id) continue;
          const a = byId.get(e.a)!;
          const b = byId.get(e.b)!;
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
          lit.push(e);
        }
        ctx.stroke();
        ctx.globalAlpha = 1;
        ctx.lineWidth = 1 / cam.k;
        if (cam.k > 0.5) {
          ctx.fillStyle = THEME.rel;
          ctx.font = `${10 / cam.k}px sans-serif`;
          ctx.textAlign = "center";
          for (const e of lit) {
            if (!e.rel) continue;
            const a = byId.get(e.a)!;
            const b = byId.get(e.b)!;
            ctx.fillText(e.rel, (a.x + b.x) / 2, (a.y + b.y) / 2 - 3 / cam.k);
          }
        }
      }
      for (const n of vNodes) {
        const isHl = highlight.has(n.id);
        ctx.globalAlpha = selected && !isHl ? 0.18 : 1;
        ctx.fillStyle = n.color;
        ctx.beginPath();
        ctx.arc(n.x, n.y, n.r, 0, 6.2832);
        ctx.fill();
        if (n === selected || n === hovered) {
          ctx.strokeStyle = n === selected ? THEME.text : `${THEME.text}99`;
          ctx.lineWidth = 2 / cam.k;
          ctx.beginPath();
          ctx.arc(n.x, n.y, n.r + 3 / cam.k, 0, 6.2832);
          ctx.stroke();
        }
        ctx.globalAlpha = 1;
      }
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      let labelBudget = 350;
      for (const n of vNodes) {
        const isHl = highlight.has(n.id) || hovered === n;
        const show = isHl || (cam.k * n.r > 5 && labelBudget > 0);
        if (!show) continue;
        if (!isHl) labelBudget--;
        let fsScreen = Math.max(10, Math.min(15, n.r * 1.4)) * Math.min(cam.k, 1.4);
        if (isHl) fsScreen = Math.max(fsScreen, 11);
        const fs = fsScreen / cam.k;
        ctx.font = `${fs}px Figtree, "Pretendard Variable", Pretendard, sans-serif`;
        ctx.globalAlpha = selected && !isHl ? 0.25 : isHl ? 1 : 0.8;
        const label = n.label.length > 24 ? `${n.label.slice(0, 24)}…` : n.label;
        ctx.strokeStyle = THEME.halo;
        ctx.lineWidth = 3 / cam.k;
        ctx.strokeText(label, n.x, n.y + n.r + 3 / cam.k);
        ctx.fillStyle = isHl ? THEME.text : THEME.label;
        ctx.fillText(label, n.x, n.y + n.r + 3 / cam.k);
        ctx.globalAlpha = 1;
      }
    };
    let raf = 0;
    const frame = () => {
      if (disposed) return;
      tick();
      draw();
      raf = requestAnimationFrame(frame);
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
    const fitView = () => {
      if (!vNodes.length || !W || !H) return;
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      for (const n of vNodes) {
        x0 = Math.min(x0, n.x);
        x1 = Math.max(x1, n.x);
        y0 = Math.min(y0, n.y);
        y1 = Math.max(y1, n.y);
      }
      const pad = 70;
      cam.k = Math.min(2.5, Math.min((W - pad * 2) / Math.max(x1 - x0, 10), (H - pad * 2) / Math.max(y1 - y0, 10)));
      cam.x = W / 2 - ((x0 + x1) / 2) * cam.k;
      cam.y = H / 2 - ((y0 + y1) / 2) * cam.k;
    };
    const centerOn = (n: SimNode<N>) => {
      cam.k = Math.max(cam.k, 1.1);
      cam.x = W / 2 - n.x * cam.k;
      cam.y = H / 2 - n.y * cam.k;
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
      if (engFocus) rebuildVisible();
      onDetail?.(node ? buildDetail(node) : null);
      onSelectNode?.(node ? node.input : null);
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
    const onPointerDown = (ev: PointerEvent) => {
      try {
        canvas.setPointerCapture(ev.pointerId);
      } catch {
        // 합성 이벤트 등 비활성 포인터는 캡처 없이 진행
      }
      const p = local(ev);
      px = p.x;
      py = p.y;
      moved = false;
      dragNode = nodeAt(p.x, p.y);
      panning = !dragNode;
      canvas.style.cursor = "grabbing";
    };
    const onPointerMove = (ev: PointerEvent) => {
      const p = local(ev);
      const dx = p.x - px;
      const dy = p.y - py;
      if ((dragNode || panning) && Math.abs(dx) + Math.abs(dy) > 3) moved = true;
      if (dragNode) {
        const w = toWorld(p.x, p.y);
        dragNode.fx = w.x;
        dragNode.fy = w.y;
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
    };
    const onPointerUp = (ev: PointerEvent) => {
      canvas.style.cursor = "grab";
      if (dragNode) dragNode.fx = dragNode.fy = null;
      if (!moved) {
        const p = local(ev);
        select(nodeAt(p.x, p.y));
      }
      dragNode = null;
      panning = false;
    };
    const onWheel = (ev: WheelEvent) => {
      ev.preventDefault();
      const p = local(ev);
      const factor = Math.pow(1.0015, -ev.deltaY);
      const k2 = Math.min(8, Math.max(0.03, cam.k * factor));
      const w = toWorld(p.x, p.y);
      cam.x = p.x - w.x * k2;
      cam.y = p.y - w.y * k2;
      cam.k = k2;
    };

    canvas.addEventListener("pointerdown", onPointerDown);
    canvas.addEventListener("pointermove", onPointerMove);
    canvas.addEventListener("pointerup", onPointerUp);
    canvas.addEventListener("wheel", onWheel, { passive: false });
    const observer = new ResizeObserver(() => {
      resize();
    });
    observer.observe(container);

    /* ── React 패널 ↔ 엔진 API ── */
    engineRef.current = {
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
        if (on && selected) setTimeout(fitView, 400);
      },
      setPaused: (on) => {
        engPaused = on;
        if (!on) reheat(0.3);
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
    setMinDeg(engMinDeg);
    setMaxDegSlider(Math.max(20, engMinDeg + 10));
    setHop(1);
    setFocusOn(false);
    setPaused(false);
    onDetail?.(null);
    if (controllerRef) controllerRef.current = { selectById: (id) => engineRef.current?.selectById(id) };

    resize();
    cam.x = W / 2;
    cam.y = H / 2;
    rebuildVisible();
    reheat(1);
    const fitTimer = window.setTimeout(fitView, 600);
    raf = requestAnimationFrame(frame);
    // 헤드리스 검증/e2e용 디버그 훅 — 숨김 탭에서도 수동으로 프레임을 돌릴 수 있다
    (container as HTMLDivElement & { __og?: unknown }).__og = {
      pump: (frames = 1) => {
        for (let i = 0; i < frames; i++) tick();
        draw();
      },
      fit: fitView,
      stats: () => ({ visible: vNodes.length, edges: vEdges.length, alpha }),
      select: (id: string | null) => {
        select(id ? byId.get(id) ?? null : null);
        draw();
      },
      firstVisibleId: () => (vNodes.length ? vNodes.reduce((a, b) => (a.degree > b.degree ? a : b)).id : null),
    };

    return () => {
      disposed = true;
      cancelAnimationFrame(raf);
      window.clearTimeout(fitTimer);
      observer.disconnect();
      canvas.removeEventListener("pointerdown", onPointerDown);
      canvas.removeEventListener("pointermove", onPointerMove);
      canvas.removeEventListener("pointerup", onPointerUp);
      canvas.removeEventListener("wheel", onWheel);
      engineRef.current = null;
      if (controllerRef) controllerRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodes, edges]);

  const engine = () => engineRef.current;

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

      <aside className="og-panel og-controls" aria-label="필터 · 물리">
        <div className="og-panel-title">필터 · 물리</div>
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

      <div className="og-panel og-stats">
        노드 <strong>{stats.shownNodes.toLocaleString()}</strong> / {stats.totalNodes.toLocaleString()} · 엣지{" "}
        <strong>{stats.shownEdges.toLocaleString()}</strong> / {stats.totalEdges.toLocaleString()}
      </div>

    </div>
  );
}
