import { useEffect, useRef, useState } from "react";
import type { DragEvent } from "react";
import { createPortal } from "react-dom";
import {
  Activity,
  AlertTriangle,
  Building2,
  ChevronDown,
  CheckCircle2,
  CircleUserRound,
  Database,
  FileArchive,
  FolderKanban,
  KeyRound,
  LoaderCircle,
  LockKeyhole,
  PackageCheck,
  PlugZap,
  Plus,
  RefreshCw,
  SendHorizontal,
  ServerCog,
  ShieldCheck,
  Users,
  Waypoints,
} from "lucide-react";
import Graph from "graphology";
import Sigma from "sigma";
import { ModelExplorerView } from "./ModelExplorer";

const API_BASE = "";
const OPENAI_CHAT_MODEL = "gpt-4.1-mini";
const NODE_COLLISION_PADDING = 1.45;
const GRAPH_EDGE_COLOR = "rgba(84, 84, 84, 0.48)";
const GRAPH_EDGE_SELECTED_COLOR = "rgba(13, 148, 136, 0.78)";
const GRAPH_EDGE_DIMMED_COLOR = "rgba(84, 84, 84, 0.28)";
const DEFAULT_GRAPH_CONTROLS = {
  nodeSize: 0.5,
  nodeDistance: 280,
  linkForce: 0.18,
  centerForce: 0.04,
  repelForce: 450,
};

type Pack = {
  id: string;
  title: string;
  filename: string;
  displayFilename?: string;
  displayName?: string;
  commonCategory?: string | null;
  driveScope?: string | null;
  driveCategory?: string | null;
  projectCategory?: string | null;
  commonScoped?: boolean;
  projectScoped?: boolean;
  source: string;
  validationStatus: string;
  counts: Record<string, number>;
  modelSummary?: Record<string, unknown>;
  ingest?: { status: string; documents?: number; nodes?: number; edges?: number };
};

type PackDisplayGroup = {
  id: string;
  label: string;
  subtitle: string;
  packIds: string[];
  packs: Pack[];
  grouped: boolean;
};

type Project = {
  id: string;
  name: string;
  company: string;
  manager: string;
  discipline: string;
  description: string;
  packIds: string[];
  role: string;
  driveFolderId?: string | null;
};

type IfcModel = {
  id: string;
  filename: string;
  projectId?: string | null;
  projectName?: string | null;
  sizeBytes?: number | null;
  uploadedAt?: number | null;
  storage?: string;
  localPath?: string;
  viewerStatus?: string | null;
  xktPath?: string | null;
  xktError?: string | null;
};

type GraphNode = {
  id: string;
  label: string;
  type: string;
  packId?: string;
  color: string;
  size: number;
  properties: Record<string, unknown>;
};

type GraphEdge = {
  id: string;
  source: string | { id: string };
  target: string | { id: string };
  relation: string;
  packId?: string;
};

type GraphPayload = {
  pack: Pack;
  project?: Project;
  packs?: Pack[];
  activePackIds?: string[];
  nodes: GraphNode[];
  edges: GraphEdge[];
  stats: {
    visibleNodes: number;
    visibleEdges: number;
    totalNodes: number;
    totalEdges: number;
  };
};

type GraphControls = typeof DEFAULT_GRAPH_CONTROLS;
type LayoutQuality = "full" | "interactive";
type DynamicGraphMode = "free" | "typeOrbit" | "radial";
type AiKeyStatus = "missing" | "untested" | "testing" | "valid" | "invalid";

type IndexStats = {
  users?: number;
  projects?: number;
  ifcModels?: number;
  packs: number;
  documents: number;
  nodes: number;
  edges: number;
};

type QueryEvidence = {
  title?: string;
  path?: string;
  snippet?: string;
};

type AiMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  evidence?: QueryEvidence[];
};

type McpStatus = {
  status: string;
  server: string;
  remote?: {
    transport: string;
    localUrl: string;
    publicUrl?: string | null;
    publicBaseUrl?: string | null;
    localUserUrlTemplate?: string | null;
    publicUserUrlTemplate?: string | null;
    userUrl?: {
      localUrl: string;
      publicUrl?: string | null;
      token: string;
      userEmail: string;
      userName: string;
      company: string;
      role: string;
    } | null;
    tokenSync?: Record<string, unknown> | null;
    tokenWriteBack?: Record<string, unknown> | null;
    command: string;
  };
  tools: string[];
};

type CurrentUser = {
  id: string;
  name: string;
  email: string;
  company: string;
  role: "admin" | "member";
  status: "pending" | "active" | "rejected";
  internalAccess?: boolean;
};

type ManagedUser = CurrentUser;

type CompanyProjectAccess = Record<string, string[]>;

type ProjectForm = {
  id?: string;
  name: string;
  company: string;
  manager: string;
  discipline: string;
  description: string;
  packIds: string[];
};

type SignupForm = {
  name: string;
  company: string;
  email: string;
  password: string;
};

type ConfirmDialogOptions = {
  title: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: "danger" | "default";
  onConfirm: () => void;
};

type ApiTiming = {
  id: string;
  path: string;
  method: string;
  ok: boolean;
  status: number;
  clientMs: number;
  serverMs: number | null;
  serverTiming: string;
  timingDetail: string;
  capturedAt: number;
};

const nav = [
  { label: "Dashboard", icon: Activity },
  { label: "Projects", icon: FolderKanban },
  { label: "Graph Explorer", icon: Waypoints },
  { label: "Model Explorer", icon: Building2 },
  { label: "MCP Connections", icon: ServerCog },
  { label: "Sync", icon: RefreshCw },
  { label: "Admin", icon: LockKeyhole },
] as const;

type AppTab = (typeof nav)[number]["label"];

const LANDING_ROUTE = "/home";
const ROOT_ROUTE = "/";
const DEFAULT_TAB: AppTab = "Dashboard";

const ROUTE_BY_TAB: Record<AppTab, string> = {
  Dashboard: "/dashboard",
  Projects: "/projects",
  "Graph Explorer": "/graph",
  "Model Explorer": "/model-explorer",
  Sync: "/sync",
  "MCP Connections": "/mcp-connection",
  Admin: "/admin",
};

const TAB_BY_ROUTE = new Map<string, AppTab>(
  Object.entries(ROUTE_BY_TAB).map(([tab, route]) => [route, tab as AppTab])
);
TAB_BY_ROUTE.set("/upload", "Sync");

const DRIVE_FOLDER_URLS = {
  projects: "https://drive.google.com/drive/folders/1qsFTMJphBJxLlgoSikRa5QZS9grvOA0W",
};

function normalizeRoutePath(pathname: string) {
  const normalized = pathname.replace(/\/+$/, "");
  return normalized || "/";
}

function tabFromPathname(pathname: string): AppTab {
  const normalized = normalizeRoutePath(pathname);
  if (normalized === "/") return DEFAULT_TAB;
  return TAB_BY_ROUTE.get(normalized) ?? DEFAULT_TAB;
}

function routeForTab(tab: string) {
  return ROUTE_BY_TAB[tab as AppTab] ?? ROUTE_BY_TAB[DEFAULT_TAB];
}

function replacePath(path: string, state: Record<string, unknown> = {}) {
  if (window.location.pathname !== path) {
    window.history.replaceState(state, "", path);
  }
}

const TAB_LABELS: Record<string, string> = {
  Dashboard: "대시보드",
  Projects: "프로젝트",
  Sync: "동기화",
  "Graph Explorer": "그래프 탐색기",
  "Model Explorer": "모델 탐색기",
  "MCP Connections": "MCP 연결",
  Admin: "관리자",
};

const NODE_TYPE_LABELS: Record<string, string> = {
  Module: "모듈",
  Assembly: "어셈블리",
  SinglePart: "단일 부재",
  Document: "문서",
  Material: "자재",
};

const ROLE_LABELS: Record<string, string> = {
  admin: "관리자",
  member: "멤버",
  guest: "게스트",
};

function numberLabel(value?: number) {
  if (!value) return "0";
  return new Intl.NumberFormat("ko-KR", { notation: value > 9999 ? "compact" : "standard" }).format(value);
}

function fileSizeLabel(value?: number | null) {
  if (!value) return "0 B";
  if (value >= 1024 * 1024 * 1024) return `${(value / (1024 * 1024 * 1024)).toFixed(1)} GB`;
  if (value >= 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  if (value >= 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${value} B`;
}

function tabLabel(tab: string) {
  return TAB_LABELS[tab] ?? tab;
}

function nodeTypeLabel(type?: string) {
  if (!type) return "";
  return NODE_TYPE_LABELS[type] ?? type;
}

function packTitle(pack: Pack) {
  return pack.displayName || pack.title || pack.displayFilename || pack.filename || pack.id;
}

function packDriveParts(pack: Pack) {
  const directScope = pack.driveScope?.trim();
  const directCategory = pack.projectCategory?.trim() || pack.commonCategory?.trim() || pack.driveCategory?.trim();
  if (directScope && directCategory) {
    return { scope: directScope, category: directCategory };
  }
  const parts = pack.filename.split("__");
  if (parts.length >= 3 && parts[0]?.trim() && parts[1]?.trim()) {
    return { scope: parts[0].trim(), category: parts[1].trim() };
  }
  return { scope: "", category: "" };
}

function packGroupSubtitle(group: PackDisplayGroup) {
  const count = numberLabel(group.packs.length);
  if (group.packs.every((pack) => pack.commonScoped)) return `${count}개 공통 팩`;
  if (group.packs.every((pack) => packDriveParts(pack).category)) return `${count}개 폴더 팩`;
  return `${count}개 팩`;
}

function groupPacksForDisplay(packs: Pack[]): PackDisplayGroup[] {
  const groups: PackDisplayGroup[] = [];
  const folderGroups = new Map<string, PackDisplayGroup>();
  for (const pack of packs) {
    const { scope, category } = packDriveParts(pack);
    if (category) {
      const groupId = `${scope || "drive"}:${category}`;
      let group = folderGroups.get(groupId);
      if (!group) {
        group = {
          id: groupId,
          label: category,
          subtitle: "",
          packIds: [],
          packs: [],
          grouped: true,
        };
        folderGroups.set(groupId, group);
        groups.push(group);
      }
      group.packIds.push(pack.id);
      group.packs.push(pack);
      group.subtitle = packGroupSubtitle(group);
      continue;
    }
    groups.push({
      id: `pack:${pack.id}`,
      label: packTitle(pack),
      subtitle: pack.displayFilename || pack.filename,
      packIds: [pack.id],
      packs: [pack],
      grouped: false,
    });
  }
  return groups;
}

function roleLabel(role?: string | null) {
  if (!role) return ROLE_LABELS.guest;
  return ROLE_LABELS[role] ?? role;
}

function validationLabel(status?: string) {
  if (!status) return "";
  const normalized = status.toLowerCase();
  if (normalized === "ready") return "준비됨";
  if (normalized === "pass") return "통과";
  if (normalized === "indexed") return "색인됨";
  return status;
}

const API_TIMING_EVENT = "modular-ontology-api-timing";

function emitApiTiming(timing: Omit<ApiTiming, "id" | "capturedAt">) {
  const payload: ApiTiming = {
    ...timing,
    id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
    capturedAt: Date.now(),
  };
  window.dispatchEvent(new CustomEvent<ApiTiming>(API_TIMING_EVENT, { detail: payload }));
  if (window.localStorage.getItem("modularOntologyPerfLog") === "1") {
    console.info("[api-timing]", payload);
  }
}

async function apiFetch(path: string, init: RequestInit = {}) {
  const startedAt = performance.now();
  const method = String(init.method || "GET").toUpperCase();
  try {
    const res = await fetch(path.startsWith("/api") ? `${API_BASE}${path}` : path, init);
    const clientMs = performance.now() - startedAt;
    const serverMsRaw = res.headers.get("x-response-time-ms");
    const serverMs = serverMsRaw ? Number.parseFloat(serverMsRaw) : Number.NaN;
    emitApiTiming({
      path,
      method,
      ok: res.ok,
      status: res.status,
      clientMs,
      serverMs: Number.isFinite(serverMs) ? serverMs : null,
      serverTiming: res.headers.get("server-timing") || "",
      timingDetail: res.headers.get("x-request-timing-detail") || "",
    });
    return res;
  } catch (error) {
    emitApiTiming({
      path,
      method,
      ok: false,
      status: 0,
      clientMs: performance.now() - startedAt,
      serverMs: null,
      serverTiming: "",
      timingDetail: "",
    });
    throw error;
  }
}

async function getJson<T>(path: string, token?: string): Promise<T> {
  const res = await apiFetch(path, {
    headers: token ? { Authorization: `Bearer ${token}` } : undefined,
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json() as Promise<T>;
}

function App() {
  const [activeTab, setActiveTab] = useState<AppTab>(() => tabFromPathname(window.location.pathname));
  const [packs, setPacks] = useState<Pack[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [ifcModels, setIfcModels] = useState<IfcModel[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState("");
  const [selectedGraphPackIds, setSelectedGraphPackIds] = useState<string[]>([]);
  const [graph, setGraph] = useState<GraphPayload | null>(null);
  const [selectedNode, setSelectedNode] = useState<GraphNode | null>(null);
  const [inspectorTab, setInspectorTab] = useState<"node" | "ai">("node");
  const [aiQuestion, setAiQuestion] = useState("");
  const [aiMessages, setAiMessages] = useState<AiMessage[]>([]);
  const [aiLoading, setAiLoading] = useState(false);
  const [openAiApiKey, setOpenAiApiKey] = useState(() => sessionStorage.getItem("modularOntologyOpenAiKey") ?? "");
  const [openAiKeyStatus, setOpenAiKeyStatus] = useState<AiKeyStatus>(() =>
    sessionStorage.getItem("modularOntologyOpenAiKey") ? "untested" : "missing"
  );
  const [openAiKeyMessage, setOpenAiKeyMessage] = useState("");
  const [isOpenAiKeyPanelOpen, setIsOpenAiKeyPanelOpen] = useState(false);
  const [status, setStatus] = useState("불러오는 중");
  const [uploadStatus, setUploadStatus] = useState("");
  const [indexStats, setIndexStats] = useState<IndexStats | null>(null);
  const [mcpStatus, setMcpStatus] = useState<McpStatus | null>(null);
  const [apiTimings, setApiTimings] = useState<ApiTiming[]>([]);
  const [authToken, setAuthToken] = useState(() => localStorage.getItem("modularOntologyToken") ?? "");
  const [sessionReady, setSessionReady] = useState(() => !localStorage.getItem("modularOntologyToken"));
  const [currentUser, setCurrentUser] = useState<CurrentUser | null>(null);
  const [loginEmail, setLoginEmail] = useState("");
  const [loginPassword, setLoginPassword] = useState("");
  const [showSignup, setShowSignup] = useState(false);
  const [signupForm, setSignupForm] = useState<SignupForm>({ name: "", company: "", email: "", password: "" });
  const [managedUsers, setManagedUsers] = useState<ManagedUser[]>([]);
  const [companies, setCompanies] = useState<string[]>([]);
  const [companyProjectAccess, setCompanyProjectAccess] = useState<CompanyProjectAccess>({});
  const [confirmDialog, setConfirmDialog] = useState<ConfirmDialogOptions | null>(null);
  const skipNextSessionRefreshRef = useRef("");
  const selectedProject = projects.find((project) => project.id === selectedProjectId) ?? projects[0];
  const selectedPackId = selectedGraphPackIds[0] ?? selectedProject?.packIds[0] ?? "";
  const graphPackOptions = selectedProject
    ? packs.filter((pack) => selectedProject.packIds.includes(pack.id))
    : [];
  const graphPackGroups = groupPacksForDisplay(graphPackOptions);
  const visibleNav = nav.filter((item) => !["Admin", "Sync"].includes(item.label) || currentUser?.role === "admin");

  function navigateToTab(tab: string, options: { replace?: boolean } = {}) {
    const nextTab = nav.some((item) => item.label === tab) ? (tab as AppTab) : DEFAULT_TAB;
    const nextRoute = routeForTab(nextTab);
    setActiveTab(nextTab);
    if (window.location.pathname !== nextRoute) {
      const method = options.replace ? "replaceState" : "pushState";
      window.history[method]({ tab: nextTab }, "", nextRoute);
    }
  }

  function toggleGraphPackGroup(group: PackDisplayGroup) {
    setSelectedGraphPackIds((current) => {
      const groupIds = new Set(group.packIds);
      const activeCount = group.packIds.filter((packId) => current.includes(packId)).length;
      if (activeCount > 0) {
        return current.filter((id) => !groupIds.has(id));
      }
      return [...current, ...group.packIds.filter((packId) => !current.includes(packId))];
    });
  }

  function confirmAction(options: ConfirmDialogOptions) {
    setConfirmDialog(options);
  }

  function closeConfirmDialog() {
    setConfirmDialog(null);
  }

  function runConfirmedAction() {
    const action = confirmDialog?.onConfirm;
    setConfirmDialog(null);
    action?.();
  }

  useEffect(() => {
    const handleApiTiming = (event: Event) => {
      const detail = (event as CustomEvent<ApiTiming>).detail;
      if (!detail?.path) return;
      setApiTimings((current) => [detail, ...current].slice(0, 12));
    };
    window.addEventListener(API_TIMING_EVENT, handleApiTiming);
    return () => window.removeEventListener(API_TIMING_EVENT, handleApiTiming);
  }, []);

  useEffect(() => {
    const handlePopState = () => {
      const normalized = normalizeRoutePath(window.location.pathname);
      if (!currentUser) {
        replacePath(LANDING_ROUTE, { landing: true });
        return;
      }
      if (normalized === ROOT_ROUTE || normalized === LANDING_ROUTE) {
        navigateToTab(DEFAULT_TAB, { replace: true });
        return;
      }
      setActiveTab(tabFromPathname(window.location.pathname));
    };
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, [currentUser]);

  useEffect(() => {
    if (!sessionReady) return;
    const normalized = normalizeRoutePath(window.location.pathname);
    if (!currentUser) {
      replacePath(LANDING_ROUTE, { landing: true });
      return;
    }
    if (normalized === ROOT_ROUTE || normalized === LANDING_ROUTE) {
      navigateToTab(DEFAULT_TAB, { replace: true });
      return;
    }
    navigateToTab(tabFromPathname(window.location.pathname), { replace: true });
  }, [sessionReady, currentUser?.email, currentUser?.role]);

  useEffect(() => {
    if (authToken) return;
    refreshPublicStatus().catch(() => undefined);
  }, [authToken]);

  useEffect(() => {
    if (openAiApiKey.trim()) {
      sessionStorage.setItem("modularOntologyOpenAiKey", openAiApiKey);
      setOpenAiKeyStatus("untested");
    } else {
      sessionStorage.removeItem("modularOntologyOpenAiKey");
      setOpenAiKeyStatus("missing");
    }
    setOpenAiKeyMessage("");
  }, [openAiApiKey]);

  useEffect(() => {
    if (skipNextSessionRefreshRef.current && skipNextSessionRefreshRef.current === authToken) {
      skipNextSessionRefreshRef.current = "";
      setSessionReady(true);
      return;
    }
    setSessionReady(false);
    refreshSession(authToken)
      .catch(() => setCurrentUser(null))
      .finally(() => setSessionReady(true));
  }, [authToken]);

  useEffect(() => {
    if (currentUser?.role === "admin") {
      refreshAdminDirectory().catch(() => {
        setManagedUsers([]);
        setCompanies([]);
        setCompanyProjectAccess({});
      });
      return;
    }
    setManagedUsers([]);
    setCompanies([]);
    setCompanyProjectAccess({});
  }, [currentUser?.role, authToken]);

  useEffect(() => {
    if (currentUser && currentUser.role !== "admin" && ["Admin", "Sync"].includes(activeTab)) {
      navigateToTab("Dashboard", { replace: true });
    }
  }, [activeTab, currentUser?.role]);

  useEffect(() => {
    if (activeTab !== "Admin" || currentUser?.role !== "admin" || !authToken) return;
    let cancelled = false;
    const refresh = () => {
      refreshAdminDirectory().catch(() => {
        if (!cancelled) setUploadStatus("관리자 목록 새로고침 실패");
      });
    };
    const refreshOnVisible = () => {
      if (document.visibilityState === "visible") refresh();
    };
    refresh();
    const intervalId = window.setInterval(refresh, 10000);
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", refreshOnVisible);
    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", refreshOnVisible);
    };
  }, [activeTab, currentUser?.role, authToken]);

  useEffect(() => {
    if (!projects.length) {
      setSelectedProjectId("");
      setSelectedGraphPackIds([]);
      return;
    }
    setSelectedProjectId((current) => (current && projects.some((project) => project.id === current) ? current : projects[0].id));
  }, [projects]);

  useEffect(() => {
    const project = projects.find((item) => item.id === selectedProjectId);
    if (!project) return;
    setSelectedGraphPackIds((current) => {
      const valid = current.filter((packId) => project.packIds.includes(packId));
      return valid;
    });
  }, [projects, selectedProjectId]);

  useEffect(() => {
    if (!selectedProjectId) return;
    const project = projects.find((item) => item.id === selectedProjectId);
    if (!project) return;
    const activePackIds = selectedGraphPackIds.filter((packId) => project.packIds.includes(packId));
    if (!activePackIds.length) {
      setSelectedNode(null);
      setAiMessages([]);
      setAiQuestion("");
      setGraph(null);
      setStatus("표시할 팩을 선택하세요");
      return;
    }
    let active = true;
    setSelectedNode(null);
    setAiMessages([]);
    setAiQuestion("");
    setGraph(null);
    setStatus("그래프 불러오는 중");
    const query = new URLSearchParams({
      max_nodes: "760",
      max_edges: "1400",
      pack_ids: activePackIds.join(","),
    });
    getJson<GraphPayload>(`/api/projects/${encodeURIComponent(selectedProjectId)}/graph?${query.toString()}`, authToken)
      .then((payload) => {
        if (!active) return;
        setGraph(payload);
        setStatus("준비됨");
      })
      .catch((error: Error) => {
        if (active) setStatus(error.message);
      });
    return () => {
      active = false;
    };
  }, [projects, selectedProjectId, selectedGraphPackIds, authToken]);

  async function refreshPublicStatus(token = authToken) {
    const tasks: Promise<unknown>[] = [getJson<IndexStats>("/api/index/status").then(setIndexStats)];
    if (token) {
      tasks.push(getJson<McpStatus>("/api/mcp/status", token).then(setMcpStatus));
    }
    await Promise.all(tasks);
  }

  async function regenerateMcpUrl() {
    if (!authToken) {
      setUploadStatus("로그인 후 MCP URL을 재발급할 수 있습니다.");
      return;
    }
    const res = await apiFetch("/api/mcp/user-url/regenerate", {
      method: "POST",
      headers: { Authorization: `Bearer ${authToken}` },
    });
    if (!res.ok) {
      setUploadStatus(await res.text());
      return;
    }
    const payload = (await res.json()) as McpStatus;
    setMcpStatus(payload);
    setUploadStatus("MCP URL이 재발급되었습니다.");
  }

  async function refreshData(nextPackId?: string, token = authToken) {
    const [packData, projectData, ifcModelData] = await Promise.all([
      getJson<Pack[]>("/api/packs", token),
      getJson<Project[]>("/api/projects", token),
      getJson<IfcModel[]>("/api/ifc/models", token).catch(() => []),
    ]);
    setPacks(packData);
    setProjects(projectData);
    setIfcModels(ifcModelData);
    refreshPublicStatus(token).catch(() => undefined);
    const nextProject =
      (nextPackId && projectData.find((project) => project.packIds.includes(nextPackId))) ||
      (selectedProjectId && projectData.find((project) => project.id === selectedProjectId)) ||
      projectData[0];
    if (nextProject) {
      setSelectedProjectId(nextProject.id);
      setSelectedGraphPackIds(nextPackId && nextProject.packIds.includes(nextPackId) ? [nextPackId] : []);
    } else {
      setSelectedProjectId("");
      setSelectedGraphPackIds([]);
    }
    setStatus("Ready");
  }

  async function refreshSession(token: string) {
    if (!token) {
      setCurrentUser(null);
      return;
    }
    const res = await apiFetch("/api/auth/me", { headers: { Authorization: `Bearer ${token}` } });
    const payload = (await res.json()) as { authenticated: boolean; user: CurrentUser | null };
    const user = payload.authenticated ? payload.user : null;
    setCurrentUser(user);
    if (user) {
      await refreshData(undefined, token);
    }
  }

  async function login() {
    if (!loginEmail.trim() || !loginPassword) {
      setUploadStatus("이메일과 비밀번호를 입력하세요.");
      return;
    }
    const res = await apiFetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: loginEmail, password: loginPassword }),
    });
    if (!res.ok) {
      const payload = await res.json().catch(() => null);
      setUploadStatus(payload?.detail ?? "로그인 실패");
      return;
    }
    const payload = (await res.json()) as { token: string; user: CurrentUser };
    localStorage.setItem("modularOntologyToken", payload.token);
    skipNextSessionRefreshRef.current = payload.token;
    setAuthToken(payload.token);
    setCurrentUser(payload.user);
    await refreshData(undefined, payload.token);
    setLoginPassword("");
    setShowSignup(false);
    navigateToTab(DEFAULT_TAB, { replace: true });
    setUploadStatus(`${roleLabel(payload.user.role)} 세션 활성화`);
  }

  async function signup() {
    if (!signupForm.email.trim() || !signupForm.password) {
      setUploadStatus("회원가입에는 이메일과 비밀번호가 필요합니다.");
      return;
    }
    const res = await apiFetch("/api/auth/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(signupForm),
    });
    if (!res.ok) {
      const payload = await res.json().catch(() => null);
      setUploadStatus(payload?.detail ?? "회원가입 실패");
      return;
    }
    setSignupForm({ name: "", company: "", email: "", password: "" });
    setShowSignup(false);
    setUploadStatus("회원가입 요청 완료. 관리자가 승인하면 로그인할 수 있습니다.");
  }

  async function fetchAdminUsers() {
    if (!authToken) return;
    const res = await apiFetch("/api/admin/users", {
      headers: { Authorization: `Bearer ${authToken}` },
    });
    if (!res.ok) throw new Error(await res.text());
    const payload = (await res.json()) as { users: ManagedUser[] };
    setManagedUsers(payload.users);
  }

  async function fetchAdminCompanies() {
    if (!authToken) return;
    const res = await apiFetch("/api/admin/companies", {
      headers: { Authorization: `Bearer ${authToken}` },
    });
    if (!res.ok) throw new Error(await res.text());
    const payload = (await res.json()) as { companies: string[] };
    setCompanies(payload.companies);
  }

  async function fetchCompanyProjectAccess() {
    if (!authToken) return;
    const res = await apiFetch("/api/admin/company-project-access", {
      headers: { Authorization: `Bearer ${authToken}` },
    });
    if (!res.ok) throw new Error(await res.text());
    const payload = (await res.json()) as { access: CompanyProjectAccess };
    setCompanyProjectAccess(payload.access);
  }

  async function refreshAdminDirectory() {
    await Promise.all([fetchAdminUsers(), fetchAdminCompanies(), fetchCompanyProjectAccess()]);
  }

  async function approveManagedUser(email: string, role: "admin" | "member" = "member") {
    const res = await apiFetch(`/api/admin/users/${encodeURIComponent(email)}/approve`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${authToken}` },
      body: JSON.stringify({ role }),
    });
    if (!res.ok) {
      setUploadStatus("회원 승인 실패");
      return;
    }
    await refreshAdminDirectory();
    setUploadStatus(`${email} 계정을 승인했습니다.`);
  }

  async function updateManagedUserRole(email: string, role: "admin" | "member") {
    const res = await apiFetch(`/api/admin/users/${encodeURIComponent(email)}/role`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${authToken}` },
      body: JSON.stringify({ role }),
    });
    if (!res.ok) {
      setUploadStatus("권한 변경 실패");
      return;
    }
    await refreshAdminDirectory();
    setUploadStatus(`${email} 권한을 ${roleLabel(role)}로 변경했습니다.`);
  }

  async function addManagedCompany(name: string) {
    const res = await apiFetch("/api/admin/companies", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${authToken}` },
      body: JSON.stringify({ name }),
    });
    if (!res.ok) {
      setUploadStatus("회사 추가 실패");
      return;
    }
    await refreshAdminDirectory();
    setUploadStatus(`${name} 회사를 추가했습니다.`);
  }

  async function renameManagedCompany(name: string, newName: string) {
    const res = await apiFetch("/api/admin/companies/rename", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${authToken}` },
      body: JSON.stringify({ name, new_name: newName }),
    });
    if (!res.ok) {
      setUploadStatus("회사명 수정 실패");
      return;
    }
    await refreshAdminDirectory();
    setUploadStatus(`${name} 회사명을 ${newName}로 수정했습니다.`);
  }

  async function deleteManagedCompany(name: string, deleteUsers = false) {
    const query = deleteUsers ? "?delete_users=true" : "";
    const res = await apiFetch(`/api/admin/companies/${encodeURIComponent(name)}${query}`, {
      method: "DELETE",
      headers: { Authorization: `Bearer ${authToken}` },
    });
    if (!res.ok) {
      const payload = await res.json().catch(() => null);
      setUploadStatus(payload?.detail ?? "회사 삭제 실패");
      return;
    }
    await refreshAdminDirectory();
    const payload = (await res.json()) as { deletedUsers?: number };
    setUploadStatus(`${name} 회사를 삭제했습니다.${payload.deletedUsers ? ` 함께 삭제된 회원 ${payload.deletedUsers}명.` : ""}`);
  }

  async function deleteManagedUser(email: string) {
    const res = await apiFetch(`/api/admin/users/${encodeURIComponent(email)}`, {
      method: "DELETE",
      headers: { Authorization: `Bearer ${authToken}` },
    });
    if (!res.ok) {
      const payload = await res.json().catch(() => null);
      setUploadStatus(payload?.detail ?? "회원 삭제 실패");
      return;
    }
    await refreshAdminDirectory();
    setUploadStatus(`${email} 회원을 삭제했습니다.`);
  }

  async function moveManagedUserCompany(email: string, company: string) {
    const res = await apiFetch(`/api/admin/users/${encodeURIComponent(email)}/company`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${authToken}` },
      body: JSON.stringify({ company }),
    });
    if (!res.ok) {
      setUploadStatus("회원 회사 이동 실패");
      return;
    }
    await refreshAdminDirectory();
    setUploadStatus(`${email} 사용자를 ${company} 회사로 이동했습니다.`);
  }

  async function updateCompanyProjectAccess(company: string, projectIds: string[]) {
    const res = await apiFetch(`/api/admin/companies/${encodeURIComponent(company)}/projects`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${authToken}` },
      body: JSON.stringify({ project_ids: projectIds }),
    });
    if (!res.ok) {
      const payload = await res.json().catch(() => null);
      setUploadStatus(payload?.detail ?? "프로젝트 접근권한 저장 실패");
      return;
    }
    const payload = (await res.json()) as { access: CompanyProjectAccess };
    setCompanyProjectAccess(payload.access);
    await refreshData(undefined, authToken);
    setUploadStatus(`${company} 회사의 프로젝트 접근권한을 저장했습니다.`);
  }

  async function saveProject(form: ProjectForm): Promise<Project | null> {
    if (currentUser?.role !== "admin") {
      setUploadStatus("관리자 세션이 필요합니다");
      return null;
    }
    const isUpdate = Boolean(form.id);
    const res = await apiFetch(isUpdate ? `/api/admin/projects/${encodeURIComponent(form.id || "")}` : "/api/admin/projects", {
      method: isUpdate ? "PUT" : "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${authToken}` },
      body: JSON.stringify({
        name: form.name,
        company: form.company,
        manager: form.manager,
        discipline: form.discipline,
        description: form.description,
        pack_ids: form.packIds,
      }),
    });
    if (!res.ok) {
      const payload = await res.json().catch(() => null);
      setUploadStatus(payload?.detail ?? "프로젝트 저장 실패");
      return null;
    }
    const payload = (await res.json()) as { project: Project };
    await refreshData(undefined, authToken);
    setSelectedProjectId(payload.project.id);
    setSelectedGraphPackIds([]);
    setUploadStatus(`${payload.project.name} 프로젝트를 저장했습니다.`);
    return payload.project;
  }

  async function deleteProject(projectId: string) {
    if (currentUser?.role !== "admin") {
      setUploadStatus("관리자 세션이 필요합니다");
      return;
    }
    const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}`, {
      method: "DELETE",
      headers: { Authorization: `Bearer ${authToken}` },
    });
    if (!res.ok) {
      const payload = await res.json().catch(() => null);
      setUploadStatus(payload?.detail ?? "프로젝트 삭제 실패");
      return;
    }
    await refreshData(undefined, authToken);
    setUploadStatus("프로젝트를 삭제했습니다.");
  }

  async function setProjectPacks(projectId: string, packIds: string[]) {
    if (currentUser?.role !== "admin") {
      setUploadStatus("관리자 세션이 필요합니다");
      return;
    }
    const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}/packs`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${authToken}` },
      body: JSON.stringify({ pack_ids: packIds }),
    });
    if (!res.ok) {
      const payload = await res.json().catch(() => null);
      setUploadStatus(payload?.detail ?? "팩 연결 저장 실패");
      return;
    }
    await refreshData(undefined, authToken);
    setUploadStatus("프로젝트 팩 연결을 저장했습니다.");
  }

  async function logout() {
    await apiFetch("/api/auth/logout", {
      method: "POST",
      headers: authToken ? { Authorization: `Bearer ${authToken}` } : undefined,
    }).catch(() => undefined);
    localStorage.removeItem("modularOntologyToken");
    setAuthToken("");
    setCurrentUser(null);
    setPacks([]);
    setProjects([]);
    setIfcModels([]);
    setSelectedProjectId("");
    setSelectedGraphPackIds([]);
    setGraph(null);
    await refreshPublicStatus("");
    setActiveTab(DEFAULT_TAB);
    replacePath(LANDING_ROUTE, { landing: true });
    setUploadStatus("로그아웃됨");
  }

  async function setIfcModelProject(modelId: string, projectId: string | null) {
    if (currentUser?.role !== "admin") {
      setUploadStatus("관리자 세션이 필요합니다");
      return;
    }
    const res = await apiFetch("/api/admin/ifc/models/link", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${authToken}` },
      body: JSON.stringify({ model_id: modelId, project_id: projectId }),
    });
    if (!res.ok) {
      const payload = await res.json().catch(() => null);
      setUploadStatus(payload?.detail ?? "IFC 연결 저장 실패");
      return;
    }
    const payload = (await res.json()) as { models: IfcModel[] };
    setIfcModels(payload.models);
    setUploadStatus("IFC 연결 저장 완료");
  }

  async function reindexPacks() {
    if (currentUser?.role !== "admin") {
      setUploadStatus("관리자 세션이 필요합니다");
      return;
    }
    setUploadStatus("Drive 동기화 및 XKT 변환 중");
    const res = await apiFetch("/api/admin/reindex", {
      method: "POST",
      headers: { Authorization: `Bearer ${authToken}` },
    });
    if (!res.ok) {
      const payload = await res.json().catch(() => null);
      setUploadStatus(payload?.detail ?? "Drive 동기화 실패");
      return;
    }
    const payload = (await res.json()) as {
      stats: IndexStats;
      xktConversion?: { status?: string; converted?: unknown[]; errors?: unknown[]; reason?: string };
    };
    setIndexStats(payload.stats);
    await refreshData(undefined, authToken);
    await refreshPublicStatus(authToken);
    const convertedCount = payload.xktConversion?.converted?.length ?? 0;
    const errorCount = payload.xktConversion?.errors?.length ?? 0;
    if (convertedCount > 0) {
      setUploadStatus(`Drive 동기화 완료 · XKT ${convertedCount}개 자동 변환`);
    } else if (errorCount > 0) {
      setUploadStatus(`Drive 동기화 완료 · XKT 변환 오류 ${errorCount}개`);
    } else if (payload.xktConversion?.status === "skipped") {
      setUploadStatus(`Drive 동기화 완료 · XKT 변환 건너뜀: ${payload.xktConversion.reason ?? "설정 없음"}`);
    } else {
      setUploadStatus("Drive 동기화 완료 · 변환할 IFC 없음");
    }
  }

  function selectPackForGraph(packId: string) {
    const project = projects.find((item) => item.packIds.includes(packId));
    if (project) {
      setSelectedProjectId(project.id);
      setSelectedGraphPackIds([packId]);
    }
    navigateToTab("Graph Explorer");
  }

  async function validateOpenAiKey() {
    const userOpenAiKey = openAiApiKey.trim();
    if (!userOpenAiKey || openAiKeyStatus === "testing") return;
    setOpenAiKeyStatus("testing");
    setOpenAiKeyMessage("");
    try {
      const res = await apiFetch("/api/llm/openai/validate", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(authToken ? { Authorization: `Bearer ${authToken}` } : {}),
        },
        body: JSON.stringify({
          openai_api_key: userOpenAiKey,
          openai_model: OPENAI_CHAT_MODEL,
        }),
      });
      if (!res.ok) throw new Error(await res.text());
      const payload = (await res.json()) as { valid?: boolean; error?: string };
      if (payload.valid) {
        setOpenAiKeyStatus("valid");
        setOpenAiKeyMessage("OpenAI key validated.");
      } else {
        setOpenAiKeyStatus("invalid");
        setOpenAiKeyMessage(formatAiQueryWarning(payload.error || "OpenAI key validation failed."));
      }
    } catch (error) {
      setOpenAiKeyStatus("invalid");
      setOpenAiKeyMessage(formatAiQueryWarning(error instanceof Error ? error.message : "OpenAI key validation failed."));
    }
  }

  async function askGraphAi() {
    const question = aiQuestion.trim();
    const selectedNodePackId = String(selectedNode?.properties?.pack_id || selectedNode?.packId || "");
    const targetPackId = selectedNodePackId || selectedGraphPackIds[0] || selectedPackId;
    if (!question || !targetPackId || aiLoading) return;
    const userOpenAiKey = openAiApiKey.trim();
    if (!userOpenAiKey) {
      setAiMessages((messages) => [
        ...messages,
        {
          id: `${Date.now()}-assistant-key-missing`,
          role: "assistant",
          content: "GPT 답변을 쓰려면 OpenAI API key를 먼저 입력해야 합니다.",
        },
      ]);
      return;
    }
    if (openAiKeyStatus !== "valid") {
      setAiMessages((messages) => [
        ...messages,
        {
          id: `${Date.now()}-assistant-key-untested`,
          role: "assistant",
          content: "OpenAI API key를 먼저 Test key로 검증해야 GPT 답변을 보낼 수 있습니다.",
        },
      ]);
      return;
    }
    const userMessage: AiMessage = { id: `${Date.now()}-user`, role: "user", content: question };
    setAiMessages((messages) => [...messages, userMessage]);
    setAiQuestion("");
    setAiLoading(true);
    try {
      const res = await apiFetch("/api/query", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(authToken ? { Authorization: `Bearer ${authToken}` } : {}),
        },
        body: JSON.stringify({
          pack_id: targetPackId,
          question,
          use_openai: true,
          openai_api_key: userOpenAiKey,
          openai_model: OPENAI_CHAT_MODEL,
        }),
      });
      if (!res.ok) throw new Error(await res.text());
      const payload = (await res.json()) as {
        answer?: string;
        evidence?: QueryEvidence[];
        llmError?: string | null;
        mode?: string;
      };
      const fallbackAnswer = payload.answer || "답변을 생성하지 못했습니다.";
      const content = payload.llmError
        ? `${formatAiQueryWarning(payload.llmError)}\n\nFallback Graph RAG answer:\n${fallbackAnswer}`
        : fallbackAnswer;
      setAiMessages((messages) => [
        ...messages,
        {
          id: `${Date.now()}-assistant`,
          role: "assistant",
          content,
          evidence: payload.evidence ?? [],
        },
      ]);
    } catch (error) {
      setAiMessages((messages) => [
        ...messages,
        {
          id: `${Date.now()}-assistant-error`,
          role: "assistant",
          content: `질의 중 오류가 발생했습니다. ${error instanceof Error ? error.message : ""}`.trim(),
        },
      ]);
    } finally {
      setAiLoading(false);
    }
  }

  if (!sessionReady) {
    return (
      <div className="landing-page boot-loading">
        <div className="brand-mark">
          <LoaderCircle className="loading-spinner" size={24} />
        </div>
      </div>
    );
  }

  if (!currentUser) {
    return (
      <LandingPage
        indexStats={indexStats}
        loginEmail={loginEmail}
        loginPassword={loginPassword}
        packs={packs}
        projects={projects}
        showSignup={showSignup}
        signupForm={signupForm}
        statusMessage={uploadStatus}
        onLogin={login}
        onLoginEmailChange={setLoginEmail}
        onLoginPasswordChange={setLoginPassword}
        onSignup={signup}
        onSignupChange={setSignupForm}
        onToggleSignup={() => setShowSignup((value) => !value)}
      />
    );
  }

  return (
    <>
    <div className={activeTab === "Model Explorer" ? "app-shell model-shell" : "app-shell"}>
      <aside
        className={
          activeTab === "Graph Explorer"
            ? "sidebar graph-sidebar-active"
            : activeTab === "Model Explorer"
              ? "sidebar model-sidebar-active"
              : "sidebar"
        }
      >
        <div className="brand">
          <div>
            <strong>Modular Ontology</strong>
            <span>BIM 온톨로지 플랫폼</span>
          </div>
        </div>
        <nav>
          {visibleNav.map((item) => {
            const Icon = item.icon;
            return (
              <button
                aria-label={tabLabel(item.label)}
                className={activeTab === item.label ? "nav-item active" : "nav-item"}
                key={item.label}
                onClick={() => navigateToTab(item.label)}
                type="button"
              >
                <Icon size={18} />
                <span className="nav-item-label">{tabLabel(item.label)}</span>
              </button>
            );
          })}
        </nav>
        {activeTab === "Graph Explorer" && <div id="graph-sidebar-controls" className="sidebar-graph-controls" />}
        {currentUser.role === "admin" && (
        <div className="sidebar-status">
          <ShieldCheck size={18} />
          <div>
            <strong>관리자 전용 팩</strong>
            <span>업로드와 수집 권한 잠김</span>
          </div>
        </div>
        )}
      </aside>

      <main
        className={
          activeTab === "Graph Explorer"
            ? "workspace graph-workspace"
            : activeTab === "Model Explorer"
              ? "workspace model-workspace"
              : "workspace"
        }
      >
        <header className="topbar">
          <div className="topbar-title">
            <p className="eyebrow">프로젝트 지식 그래프</p>
            <h1>{tabLabel(activeTab)}</h1>
          </div>
          <div className="topbar-actions">
            <label className="select-wrap">
              <select value={selectedProjectId} onChange={(event) => setSelectedProjectId(event.target.value)}>
                {projects.map((project) => (
                  <option value={project.id} key={project.id}>
                    {project.name}
                  </option>
                ))}
              </select>
              <ChevronDown size={16} />
            </label>
            <div className="topbar-user-cluster">
              <div className="role-pill">
                <CircleUserRound size={17} />
                {roleLabel(currentUser?.role)}
              </div>
              <div className="auth-actions">
                <span className="signed-user">{currentUser.email}</span>
                <button onClick={logout}>로그아웃</button>
              </div>
            </div>
          </div>
        </header>
        {activeTab !== "Graph Explorer" && activeTab !== "Model Explorer" && activeTab !== "MCP Connections" && (
          <MetricsStrip
            activeTab={activeTab}
            companies={companies}
            currentUser={currentUser}
            ifcModels={ifcModels}
            packs={packs}
            projects={projects}
            users={managedUsers}
          />
        )}

        {activeTab === "Dashboard" && (
          <DashboardView
            apiTimings={apiTimings}
            ifcModels={ifcModels}
            packs={packs}
            projects={projects}
            onGoToTab={navigateToTab}
            onOpenPack={selectPackForGraph}
          />
        )}

        {activeTab === "Projects" && (
          <ProjectsView
            currentUser={currentUser}
            ifcModels={ifcModels}
            packs={packs}
            projects={projects}
            selectedPackId={selectedPackId}
            onSaveProject={saveProject}
          />
        )}

        {activeTab === "Sync" && (
          <SyncView
            currentUser={currentUser}
            ifcModels={ifcModels}
            packs={packs}
            projects={projects}
            selectedPackId={selectedPackId}
            uploadStatus={uploadStatus}
            onSync={reindexPacks}
          />
        )}

        {activeTab === "Model Explorer" && (
          <ModelExplorerView
            authToken={authToken}
            ifcModels={ifcModels}
            projects={projects}
            selectedProjectId={selectedProjectId}
          />
        )}

        {activeTab === "MCP Connections" && (
          <McpConnectionsView
            mcpStatus={mcpStatus}
            onLoadUrl={() => refreshPublicStatus(authToken)}
            onRegenerateUrl={() =>
              confirmAction({
                title: "MCP URL 재발급",
                message: "새 MCP URL을 발급하면 기존 URL로 연결된 개인 AI Agent는 더 이상 사용할 수 없습니다.",
                confirmLabel: "재발급",
                cancelLabel: "취소",
                tone: "danger",
                onConfirm: regenerateMcpUrl,
              })
            }
          />
        )}

        {activeTab === "Admin" && currentUser?.role !== "admin" && <AdminLockedView />}

        {activeTab === "Admin" && currentUser?.role === "admin" && (
          <AdminView
            currentUser={currentUser}
            companies={companies}
            companyProjectAccess={companyProjectAccess}
            projects={projects}
            users={managedUsers}
            uploadStatus={uploadStatus}
            onAddCompany={addManagedCompany}
            onApproveUser={approveManagedUser}
            onConfirm={confirmAction}
            onDeleteCompany={deleteManagedCompany}
            onDeleteUser={deleteManagedUser}
            onMoveUserCompany={moveManagedUserCompany}
            onRenameCompany={renameManagedCompany}
            onReindex={reindexPacks}
            onRefreshUsers={refreshAdminDirectory}
            onSetUserRole={updateManagedUserRole}
            onSetCompanyProjectAccess={updateCompanyProjectAccess}
            onUpload={reindexPacks}
          />
        )}

        {activeTab === "Graph Explorer" && (
        <>
        <section className="content-grid graph-explorer-grid">
          <div className="graph-panel">
            <div className="panel-header">
              <div>
                <h2>그래프 탐색기</h2>
                <span>
                  {selectedProject?.name ?? graph?.pack.title ?? status}
                  {graphPackOptions.length ? ` / ${selectedGraphPackIds.length}개 팩 표시` : ""}
                </span>
              </div>
              <div className="legend">
                {["Module", "Assembly", "SinglePart", "Document", "Material"].map((type) => (
                  <span key={type}>
                    <i className={`dot ${type.toLowerCase()}`} />
                    {nodeTypeLabel(type)}
                  </span>
                ))}
              </div>
            </div>
            {graphPackOptions.length ? (
              <div className="graph-pack-filter" aria-label="프로젝트 팩 필터">
                <div>
                  {graphPackGroups.map((group) => {
                    const activeCount = group.packIds.filter((packId) => selectedGraphPackIds.includes(packId)).length;
                    const isActive = activeCount > 0;
                    return (
                    <label
                      className={isActive ? "active" : ""}
                      key={group.id}
                      title={group.grouped ? `${group.label}: ${group.packs.map(packTitle).join(", ")}` : group.subtitle}
                    >
                      <input
                        checked={isActive}
                        type="checkbox"
                        onChange={() => toggleGraphPackGroup(group)}
                      />
                      <span>{group.label}</span>
                    </label>
                    );
                  })}
                </div>
              </div>
            ) : null}
            {graphPackOptions.length ? (
              selectedGraphPackIds.length ? (
                <GraphCanvas graph={graph} selectedNode={selectedNode} onSelectNode={setSelectedNode} />
              ) : (
                <GraphProjectEmptyState hasPacks projectName={selectedProject?.name} />
              )
            ) : (
              <GraphProjectEmptyState projectName={selectedProject?.name} />
            )}
          </div>

          <aside className="inspector">
            <div className="panel-header slim graph-inspector-header">
              <div>
                <h2>{inspectorTab === "node" ? "Node Inspector" : "AI Query"}</h2>
                {inspectorTab === "node" ? (
                  <span>{selectedNode ? nodeTypeLabel(selectedNode.type) : "노드를 선택하세요"}</span>
                ) : (
                  <div className="openai-key-header">
                    <button
                      type="button"
                      className={`openai-key-button ${openAiKeyStatus} ${isOpenAiKeyPanelOpen ? "open" : ""}`}
                      aria-expanded={isOpenAiKeyPanelOpen}
                      onClick={() => setIsOpenAiKeyPanelOpen((value) => !value)}
                    >
                      <span>OpenAI API</span>
                    </button>
                    {isOpenAiKeyPanelOpen ? (
                      <div className="openai-key-popover">
                        <div className="ai-model-row">
                          <span>{OPENAI_CHAT_MODEL}</span>
                          <em className={`ai-key-status ${openAiKeyStatus}`}>{openAiKeyStatusLabel(openAiKeyStatus)}</em>
                        </div>
                        <div className="ai-key-row">
                          <label className="ai-key-field">
                            <input
                              type="password"
                              value={openAiApiKey}
                              placeholder="OpenAI API key"
                              autoComplete="off"
                              spellCheck={false}
                              onChange={(event) => setOpenAiApiKey(event.target.value)}
                            />
                          </label>
                          <button
                            type="button"
                            className="ai-key-test-button"
                            disabled={!openAiApiKey.trim() || openAiKeyStatus === "testing"}
                            onClick={validateOpenAiKey}
                          >
                            {openAiKeyStatus === "testing" ? "Testing" : "Test key"}
                          </button>
                        </div>
                        {openAiKeyMessage ? <p className={`ai-key-message ${openAiKeyStatus}`}>{openAiKeyMessage}</p> : null}
                      </div>
                    ) : null}
                  </div>
                )}
              </div>
            </div>
            <div className="inspector-tabs" role="tablist" aria-label="그래프 우측 패널">
              <button className={inspectorTab === "node" ? "active" : ""} type="button" onClick={() => setInspectorTab("node")}>
                노드 정보
              </button>
              <button className={inspectorTab === "ai" ? "active" : ""} type="button" onClick={() => setInspectorTab("ai")}>
                AI 질문
              </button>
            </div>
            {inspectorTab === "ai" ? (
              <AiQueryPanel
                openAiKeyStatus={openAiKeyStatus}
                loading={aiLoading}
                messages={aiMessages}
                question={aiQuestion}
                onQuestionChange={setAiQuestion}
                onSubmit={askGraphAi}
              />
            ) : selectedNode ? (
              <NodeDetails node={selectedNode} />
            ) : (
              <div className="empty-state node-empty-state">
                <strong>그래프 노드를 선택하세요</strong>
                <span>속성, evidence, 관계 타입이 이 패널에 표시됩니다.</span>
              </div>
            )}
          </aside>
        </section>

        </>
        )}
      </main>
    </div>
    <ConfirmDialog dialog={confirmDialog} onCancel={closeConfirmDialog} onConfirm={runConfirmedAction} />
    </>
  );
}

function ConfirmDialog({
  dialog,
  onCancel,
  onConfirm,
}: {
  dialog: ConfirmDialogOptions | null;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  if (!dialog) return null;
  const isDanger = dialog.tone !== "default";
  return createPortal(
    <div className="confirm-overlay" role="presentation" onMouseDown={onCancel}>
      <section
        aria-modal="true"
        className={isDanger ? "confirm-dialog danger" : "confirm-dialog"}
        role="dialog"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="confirm-dialog-icon">
          <AlertTriangle size={22} />
        </div>
        <div className="confirm-dialog-copy">
          <h2>{dialog.title}</h2>
          <p>{dialog.message}</p>
        </div>
        <div className="confirm-dialog-actions">
          <button className="secondary-button" type="button" onClick={onCancel}>
            {dialog.cancelLabel ?? "취소"}
          </button>
          <button className={isDanger ? "danger-confirm-button" : "primary-button"} type="button" onClick={onConfirm}>
            {dialog.confirmLabel ?? "확인"}
          </button>
        </div>
      </section>
    </div>,
    document.body,
  );
}

function LandingPage({
  indexStats,
  loginEmail,
  loginPassword,
  packs,
  projects,
  showSignup,
  signupForm,
  statusMessage,
  onLogin,
  onLoginEmailChange,
  onLoginPasswordChange,
  onSignup,
  onSignupChange,
  onToggleSignup,
}: {
  indexStats: IndexStats | null;
  loginEmail: string;
  loginPassword: string;
  packs: Pack[];
  projects: Project[];
  showSignup: boolean;
  signupForm: SignupForm;
  statusMessage: string;
  onLogin: () => void;
  onLoginEmailChange: (value: string) => void;
  onLoginPasswordChange: (value: string) => void;
  onSignup: () => void;
  onSignupChange: (value: SignupForm | ((form: SignupForm) => SignupForm)) => void;
  onToggleSignup: () => void;
}) {
  return (
    <main className="landing-page">
      <section className="landing-hero">
        <div className="hero-copy">
          <span className="landing-kicker">PROJECT KNOWLEDGE GRAPH</span>
          <h1>
            <span>프로젝트 모델 데이터를</span>
            <span>지식 그래프로 연결하세요</span>
          </h1>
          <p>
            <span>BIM 모델 데이터를 온톨로지 팩으로 관리합니다.</span>
            <span>MCP로 AI를 연결해 프로젝트 정보를 질의합니다.</span>
          </p>
          <div className="landing-metrics">
            <span><strong>{numberLabel(indexStats?.users)}</strong>사용자</span>
            <span><strong>{numberLabel(indexStats?.projects)}</strong>프로젝트</span>
            <span><strong>{numberLabel(indexStats?.ifcModels)}</strong>IFC 모델</span>
            <span><strong>{numberLabel(indexStats?.packs)}</strong>온톨로지 팩</span>
          </div>
        </div>

        <div className="landing-auth-card">
          <div className="auth-mode-tabs">
            <button className={!showSignup ? "active" : ""} type="button" onClick={showSignup ? onToggleSignup : undefined}>
              로그인
            </button>
            <button className={showSignup ? "active" : ""} type="button" onClick={!showSignup ? onToggleSignup : undefined}>
              회원가입
            </button>
          </div>

          {!showSignup ? (
            <div className="landing-form">
              <label>
                이메일 ID
                <input
                  type="email"
                  value={loginEmail}
                  placeholder="company@domain.com"
                  onChange={(event) => onLoginEmailChange(event.target.value)}
                />
              </label>
              <label>
                비밀번호
                <input
                  type="password"
                  value={loginPassword}
                  placeholder="비밀번호"
                  onChange={(event) => onLoginPasswordChange(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") onLogin();
                  }}
                />
              </label>
              <button className="landing-submit" type="button" onClick={onLogin}>
                워크스페이스 열기
              </button>
              <p>관리자 승인된 계정만 로그인할 수 있습니다.</p>
            </div>
          ) : (
            <div className="landing-form">
              <label>
                이름
                <input
                  value={signupForm.name}
                  placeholder="홍길동"
                  onChange={(event) => onSignupChange((form) => ({ ...form, name: event.target.value }))}
                />
              </label>
              <label>
                회사
                <input
                  value={signupForm.company}
                  placeholder="Kumkang Kind"
                  onChange={(event) => onSignupChange((form) => ({ ...form, company: event.target.value }))}
                />
              </label>
              <label>
                회사 이메일
                <input
                  type="email"
                  value={signupForm.email}
                  placeholder="company@domain.com"
                  onChange={(event) => onSignupChange((form) => ({ ...form, email: event.target.value }))}
                />
              </label>
              <label>
                비밀번호
                <input
                  type="password"
                  value={signupForm.password}
                  placeholder="6자 이상"
                  onChange={(event) => onSignupChange((form) => ({ ...form, password: event.target.value }))}
                />
              </label>
              <button className="landing-submit" type="button" onClick={onSignup}>
                승인 요청 보내기
              </button>
              <p>가입 요청은 관리자 화면에서 회사별로 검토됩니다.</p>
            </div>
          )}
          {statusMessage && <div className="landing-status">{statusMessage}</div>}
        </div>
      </section>
    </main>
  );
}

function MetricsStrip({
  activeTab,
  companies,
  currentUser,
  ifcModels,
  packs,
  projects,
  users,
}: {
  activeTab: string;
  companies: string[];
  currentUser: CurrentUser | null;
  ifcModels: IfcModel[];
  packs: Pack[];
  projects: Project[];
  users: ManagedUser[];
}) {
  if (activeTab === "Admin") {
    return (
      <section className="metrics">
        <Metric icon={Building2} label="회사" value={numberLabel(companies.length)} />
        <Metric icon={Users} label="회원" value={numberLabel(users.length)} />
        <Metric icon={CircleUserRound} label="승인 대기" value={numberLabel(users.filter((user) => user.status === "pending").length)} />
        <Metric icon={ShieldCheck} label="관리자" value={numberLabel(users.filter((user) => user.role === "admin").length)} tone="green" />
      </section>
    );
  }

  return (
    <section className="metrics">
      <Metric icon={Building2} label="회사명" value={currentUser?.company || "미지정"} />
      <Metric icon={FolderKanban} label="프로젝트" value={numberLabel(projects.length)} />
      <Metric icon={Database} label="IFC 모델" value={numberLabel(ifcModels.length)} />
      <Metric icon={FileArchive} label="온톨로지 팩" value={numberLabel(packs.length)} />
    </section>
  );
}

function DashboardView({
  apiTimings,
  ifcModels,
  packs,
  projects,
  onGoToTab,
  onOpenPack,
}: {
  apiTimings: ApiTiming[];
  ifcModels: IfcModel[];
  packs: Pack[];
  projects: Project[];
  onGoToTab: (tab: string) => void;
  onOpenPack: (packId: string) => void;
}) {
  const recentIfcModels = ifcModels.slice(0, 5);
  const recentPacks = packs.slice(0, 5);

  return (
    <section className="dashboard-grid">
      <div className="overview-panel">
        <div className="panel-header slim">
          <div>
            <h2>프로젝트</h2>
            <span>{projects.length}개 프로젝트 워크스페이스</span>
          </div>
          <FolderKanban size={19} />
        </div>
        <div className="compact-list">
          {projects.map((project) => (
            <button className="compact-row" key={project.id} onClick={() => onGoToTab("Projects")}>
              <strong>{project.name}</strong>
              <span>{project.discipline} / {project.packIds.length}개 팩</span>
            </button>
          ))}
        </div>
      </div>

      <div className="dashboard-recents-panel">
        <div className="overview-panel dashboard-recents-section">
          <div className="panel-header slim">
            <div>
              <h2>최근 IFC 모델</h2>
              <span>프로젝트에 업로드된 IFC 원본</span>
            </div>
            <Database size={19} />
          </div>
          <div className="compact-list dashboard-scroll-list">
            {recentIfcModels.length ? recentIfcModels.map((model) => (
              <div className="compact-row static" key={model.id}>
                <strong>{model.filename}</strong>
                <span>{model.projectName || "미연결"} / {model.storage || "local"}</span>
              </div>
            )) : (
              <div className="compact-row static empty-row">
                <strong>업로드된 IFC 모델 없음</strong>
                <span>동기화 탭에서 Drive 모델을 등록하세요</span>
              </div>
            )}
          </div>
        </div>

        <div className="overview-panel dashboard-recents-section">
          <div className="panel-header slim">
            <div>
              <h2>최근 온톨로지 팩</h2>
              <span>그래프 워크스페이스 열기</span>
            </div>
            <FileArchive size={19} />
          </div>
          <div className="compact-list dashboard-scroll-list">
            {recentPacks.map((pack) => (
              <button className="compact-row" key={pack.id} onClick={() => onOpenPack(pack.id)}>
                <strong>{pack.title}</strong>
                <span>{numberLabel(pack.counts.nodes)}개 노드 / {numberLabel(pack.counts.edges)}개 엣지</span>
              </button>
            ))}
          </div>
        </div>

        <PerformancePanel apiTimings={apiTimings} />
      </div>
    </section>
  );
}

function durationLabel(value: number | null | undefined) {
  if (value == null) return "-";
  if (value >= 1000) return `${(value / 1000).toFixed(2)}s`;
  return `${Math.round(value)}ms`;
}

function shortApiPath(path: string) {
  const [base, query] = path.split("?");
  const compactBase = base.length > 44 ? `...${base.slice(-44)}` : base;
  return query ? `${compactBase}?...` : compactBase;
}

function PerformancePanel({ apiTimings }: { apiTimings: ApiTiming[] }) {
  return (
    <div className="overview-panel dashboard-recents-section performance-panel">
      <div className="panel-header slim">
        <div>
          <h2>API 성능 계측</h2>
          <span>최근 브라우저 요청 / 서버 처리 시간</span>
        </div>
        <Activity size={19} />
      </div>
      <div className="performance-list">
        {apiTimings.length ? (
          apiTimings.slice(0, 8).map((timing) => (
            <div
              className={timing.ok ? "performance-row" : "performance-row error"}
              key={timing.id}
              title={timing.timingDetail ? `${timing.path}\n${timing.timingDetail}` : timing.path}
            >
              <span className="performance-path">{shortApiPath(timing.path)}</span>
              <span>{timing.method}</span>
              <strong>{durationLabel(timing.clientMs)}</strong>
              <em>{durationLabel(timing.serverMs)}</em>
              <i>{timing.status || "ERR"}</i>
            </div>
          ))
        ) : (
          <div className="performance-empty">
            <strong>아직 기록된 API 요청이 없습니다.</strong>
            <span>화면을 이동하거나 데이터를 새로고침하면 여기에 표시됩니다.</span>
          </div>
        )}
      </div>
    </div>
  );
}

function ProjectsView({
  currentUser,
  ifcModels,
  packs,
  projects,
  onSaveProject,
}: {
  currentUser: CurrentUser | null;
  ifcModels: IfcModel[];
  packs: Pack[];
  projects: Project[];
  selectedPackId: string;
  onSaveProject: (form: ProjectForm) => Promise<Project | null | void> | Project | null | void;
}) {
  const isAdmin = currentUser?.role === "admin";
  const emptyForm: ProjectForm = {
    name: "",
    company: "Kumkang Kind",
    manager: currentUser?.name ?? "",
    discipline: "",
    description: "",
    packIds: [],
  };
  const [selectedProjectId, setSelectedProjectId] = useState(projects[0]?.id ?? "");
  const selectedProject = projects.find((project) => project.id === selectedProjectId) ?? null;
  const [dialogDraft, setDialogDraft] = useState<ProjectForm>(emptyForm);
  const [dialogMode, setDialogMode] = useState<"create" | "edit" | null>(null);
  const [pressedAction, setPressedAction] = useState("");

  useEffect(() => {
    if (!projects.length) {
      setSelectedProjectId("");
      return;
    }
    if (!selectedProjectId || !projects.some((project) => project.id === selectedProjectId)) {
      setSelectedProjectId(projects[0].id);
    }
  }, [projects, selectedProjectId]);

  const linkedPacks = selectedProject
    ? selectedProject.packIds
        .map((packId) => packs.find((pack) => pack.id === packId))
        .filter((pack): pack is Pack => Boolean(pack))
    : [];
  const linkedPackGroups = groupPacksForDisplay(linkedPacks);
  const linkedModels = selectedProject
    ? ifcModels.filter((model) => model.projectId === selectedProject.id)
    : [];

  function showActionFeedback(action: string) {
    setPressedAction("");
    window.setTimeout(() => setPressedAction(action), 0);
    window.setTimeout(() => {
      setPressedAction((current) => (current === action ? "" : current));
    }, 460);
  }

  function actionButtonClass(action: string, baseClass = "") {
    return `${baseClass ? `${baseClass} ` : ""}action-feedback-button${pressedAction === action ? " is-confirming" : ""}`;
  }

  function runProjectAction(action: string, callback: () => Promise<void> | void) {
    showActionFeedback(action);
    void callback();
  }

  function openCreateDialog() {
    setDialogDraft({
      ...emptyForm,
      manager: currentUser?.name ?? "",
    });
    setDialogMode("create");
  }

  function openEditDialog() {
    if (!selectedProject) return;
    setDialogDraft({
      id: selectedProject.id,
      name: selectedProject.name,
      company: selectedProject.company || "",
      manager: selectedProject.manager || "",
      discipline: selectedProject.discipline || "",
      description: selectedProject.description || "",
      packIds: selectedProject.packIds,
    });
    setDialogMode("edit");
  }

  async function saveDialogDraft() {
    const savedProject = await Promise.resolve(onSaveProject(dialogDraft));
    if (savedProject && typeof savedProject === "object" && "id" in savedProject) {
      setSelectedProjectId(String(savedProject.id));
    }
    setDialogMode(null);
  }

  return (
    <>
    <section className="project-management-grid">
      <div className="project-list-panel">
        <div className="panel-header slim">
          <div>
            <h2>프로젝트</h2>
            <span>{projects.length}개 관리형 프로젝트</span>
          </div>
          {isAdmin && (
            <div className="project-header-actions">
              <button
                className={actionButtonClass("project-create")}
                type="button"
                onClick={() => runProjectAction("project-create", openCreateDialog)}
              >
                <Plus size={14} />
                추가
              </button>
              <button
                className={actionButtonClass("project-edit")}
                disabled={!selectedProject}
                type="button"
                onClick={() => runProjectAction("project-edit", openEditDialog)}
              >
                편집
              </button>
            </div>
          )}
        </div>
        <div className="project-card-list">
          {projects.map((project) => {
            const projectPacks = project.packIds
              .map((packId) => packs.find((pack) => pack.id === packId))
              .filter((pack): pack is Pack => Boolean(pack));
            const projectPackGroups = groupPacksForDisplay(projectPacks);
            const projectModels = ifcModels.filter((model) => model.projectId === project.id);
            return (
              <button
                className={project.id === selectedProject?.id ? "project-summary-card active" : "project-summary-card"}
                key={project.id}
                type="button"
                onClick={() => setSelectedProjectId(project.id)}
              >
                <strong>{project.name}</strong>
                <span>{project.company || "회사 미지정"} / {project.discipline || "분야 미지정"}</span>
                <em>{projectModels.length}개 IFC / {projectPackGroups.length}개 온톨로지 항목</em>
              </button>
            );
          })}
        </div>
      </div>

      <div className="project-panel project-status-panel">
        <div className="project-status-grid">
          <div className="project-status-copy">
            <div className="panel-header slim">
              <div>
                <h2>{selectedProject?.name ?? "프로젝트를 선택하세요"}</h2>
                <span>{selectedProject ? `${selectedProject.company || "회사 미지정"} / ${selectedProject.manager || "관리자 미지정"}` : "카드를 선택하면 상세 정보가 표시됩니다"}</span>
              </div>
            </div>
            <dl className="project-detail-list">
              <div>
                <dt>분야</dt>
                <dd>{selectedProject?.discipline || "미지정"}</dd>
              </div>
              <div>
                <dt>설명</dt>
                <dd>{selectedProject?.description || "설명 없음"}</dd>
              </div>
              <div>
                <dt>IFC 모델</dt>
                <dd>{numberLabel(linkedModels.length)}개</dd>
              </div>
              <div>
                <dt>온톨로지 팩</dt>
                <dd>{numberLabel(linkedPackGroups.length)}개 항목 / {numberLabel(linkedPacks.length)}개 팩</dd>
              </div>
            </dl>
          </div>

          <div className="project-linked-summary">
            <div className="linked-summary-columns">
              <div>
                <div className="mini-list">
                  <strong>IFC 모델</strong>
                  {linkedModels.length ? linkedModels.map((model) => (
                    <span key={model.id}>{model.filename}</span>
                  )) : <em>연결된 IFC 모델 없음</em>}
                </div>
              </div>
              <div>
                <div className="mini-list">
                  <strong>온톨로지 팩</strong>
                  {linkedPackGroups.length ? linkedPackGroups.map((group) => (
                    <span key={group.id}>{group.label}{group.grouped ? ` (${group.subtitle})` : ""}</span>
                  )) : <em>연결된 온톨로지 팩 없음</em>}
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div className="project-panel project-connection-panel">
        <div className="panel-header slim">
          <div>
            <h2>모델 / 온톨로지 팩 연결 현황</h2>
            <span>Google Drive 프로젝트 폴더에서 동기화된 항목입니다</span>
          </div>
          <FolderKanban size={19} />
        </div>
        <div className="project-connection-grid">
          <div className="connection-list-panel">
            <div className="connection-list-title">
              <strong>IFC 모델</strong>
              <span>{numberLabel(linkedModels.length)}개 연결됨</span>
            </div>
            <div className="connection-scroll-list">
              {linkedModels.length ? linkedModels.map((model) => (
                  <div className="connection-row active" key={model.id}>
                    <Database size={18} />
                    <span>
                      <strong>{model.filename}</strong>
                      <em>{model.viewerStatus === "ready" ? "뷰어 준비" : "동기화됨"}</em>
                    </span>
                  </div>
              )) : <p className="empty-list-note">선택 프로젝트에 연결된 IFC 모델이 없습니다.</p>}
            </div>
          </div>

          <div className="connection-list-panel">
            <div className="connection-list-title">
              <strong>온톨로지 팩</strong>
              <span>{numberLabel(linkedPackGroups.length)}개 항목</span>
            </div>
            <div className="connection-scroll-list">
              {linkedPackGroups.length ? linkedPackGroups.map((group) => (
                  <div className="connection-row active" key={group.id}>
                    <FileArchive size={18} />
                    <span>
                      <strong>{group.label}</strong>
                      <em>
                        {group.grouped
                          ? `${group.subtitle} / ${numberLabel(group.packs.reduce((sum, pack) => sum + Number(pack.counts.edges || 0), 0))}개 엣지`
                          : `${group.packs[0]?.displayFilename || group.packs[0]?.filename || ""} / ${numberLabel(group.packs[0]?.counts.edges)}개 엣지`}
                      </em>
                    </span>
                  </div>
              )) : <p className="empty-list-note">선택 프로젝트에 연결된 온톨로지 팩이 없습니다.</p>}
            </div>
          </div>
        </div>
      </div>
    </section>

    {dialogMode && createPortal(
      <div className="confirm-overlay" role="presentation" onMouseDown={() => setDialogMode(null)}>
        <section
          aria-modal="true"
          className="project-dialog"
          role="dialog"
          onMouseDown={(event) => event.stopPropagation()}
        >
          <div className="panel-header slim">
            <div>
              <h2>{dialogMode === "create" ? "프로젝트 추가" : "프로젝트 편집"}</h2>
              <span>
                {dialogMode === "create"
                  ? "Google Drive 하위 폴더와 README를 함께 생성합니다"
                  : "프로젝트명은 Google Drive 폴더명으로도 반영됩니다"}
              </span>
            </div>
            <FolderKanban size={19} />
          </div>
          <div className="project-form-grid">
            <label className="full">
              프로젝트명
              <input
                autoFocus
                value={dialogDraft.name}
                onChange={(event) => setDialogDraft({ ...dialogDraft, name: event.target.value })}
              />
            </label>
            <label>
              회사
              <input
                value={dialogDraft.company}
                onChange={(event) => setDialogDraft({ ...dialogDraft, company: event.target.value })}
              />
            </label>
            <label>
              관리자
              <input
                value={dialogDraft.manager}
                onChange={(event) => setDialogDraft({ ...dialogDraft, manager: event.target.value })}
              />
            </label>
            <label>
              분야
              <input
                value={dialogDraft.discipline}
                onChange={(event) => setDialogDraft({ ...dialogDraft, discipline: event.target.value })}
              />
            </label>
            <label className="full">
              설명
              <textarea
                value={dialogDraft.description}
                onChange={(event) => setDialogDraft({ ...dialogDraft, description: event.target.value })}
              />
            </label>
          </div>
          <div className="project-action-row">
            <button className="secondary-button" type="button" onClick={() => setDialogMode(null)}>
              취소
            </button>
            <button
              className={actionButtonClass("project-save", "primary-button")}
              disabled={!dialogDraft.name.trim() || (dialogMode === "edit" && !selectedProject)}
              type="button"
              onClick={() => runProjectAction("project-save", saveDialogDraft)}
            >
              {dialogMode === "create" ? "프로젝트 생성" : "프로젝트 저장"}
            </button>
          </div>
        </section>
      </div>,
      document.body,
    )}
    </>
  );
}

function SyncView({
  currentUser,
  ifcModels,
  packs,
  projects,
  selectedPackId,
  uploadStatus,
  onSync,
}: {
  currentUser: CurrentUser | null;
  ifcModels: IfcModel[];
  packs: Pack[];
  projects: Project[];
  selectedPackId: string;
  uploadStatus: string;
  onSync: () => void;
}) {
  const isAdmin = currentUser?.role === "admin";
  const projectByPackId = new Map<string, string>();
  projects.forEach((project) => {
    project.packIds.forEach((packId) => projectByPackId.set(packId, project.name));
  });

  return (
    <section className="management-grid">
      <div className="overview-panel ifc-upload-panel">
        <div className="panel-header slim">
          <div>
            <h2>Drive 동기화</h2>
            <span>{isAdmin ? "Google Drive에 올린 모델과 팩을 앱에 등록합니다" : "관리자 세션이 필요합니다"}</span>
          </div>
          <RefreshCw size={19} />
        </div>
        <div className="action-stack">
          <button className="primary-button block" disabled={!isAdmin} onClick={onSync}>
            <RefreshCw size={17} />
            Drive에서 동기화
          </button>
          <span className="sync-status-text">{uploadStatus || "관리자가 Drive에 파일을 올린 뒤 동기화하세요"}</span>
        </div>
      </div>

      <div className="overview-panel wide ifc-staging-panel">
        <div className="panel-header slim">
          <div>
            <h2>IFC 모델</h2>
            <span>프로젝트별 ifc-models 폴더에서 동기화된 모델입니다</span>
          </div>
          <FileArchive size={19} />
        </div>
        <div className="pack-table">
          {ifcModels.length ? ifcModels.map((model) => (
            <div className="pack-table-row" key={model.id}>
              <strong>{model.filename}</strong>
              <span>{model.projectName || model.projectId || "미연결"}</span>
              <span>{model.storage || "local"}</span>
              <span>{model.sizeBytes ? fileSizeLabel(model.sizeBytes) : "-"}</span>
              <em>{model.viewerStatus === "ready" ? "뷰어 준비" : "등록됨"}</em>
            </div>
          )) : (
            <p className="empty-list-note">동기화된 IFC 모델이 없습니다.</p>
          )}
        </div>
      </div>

      <div className="overview-panel ontology-upload-panel">
        <div className="panel-header slim">
          <div>
            <h2>Drive 폴더 규칙</h2>
            <span>관리자가 Drive에 직접 업로드합니다</span>
          </div>
          <PackageCheck size={19} />
        </div>
        <div className="action-stack">
          <div className="sync-folder-list">
            <a href={DRIVE_FOLDER_URLS.projects} target="_blank" rel="noreferrer">
              <strong>프로젝트</strong>
              Drive의 02_Projects 안에 프로젝트 폴더를 만들고 동기화하면 앱의 프로젝트 탭에 자동 등록됩니다.
            </a>
            <a href={DRIVE_FOLDER_URLS.projects} target="_blank" rel="noreferrer">
              <strong>IFC/XKT</strong>
              02_Projects/&lt;project-id&gt;/ifc-models
            </a>
            <a href={DRIVE_FOLDER_URLS.projects} target="_blank" rel="noreferrer">
              <strong>팩 ZIP</strong>
              02_Projects/&lt;project-id&gt;/ontology-packs
            </a>
          </div>
        </div>
      </div>

      <div className="overview-panel wide ontology-pack-list-panel">
        <div className="panel-header slim">
          <div>
            <h2>온톨로지 팩</h2>
            <span>프로젝트별 ontology-packs 폴더에서 동기화된 ZIP 팩 {packs.length}개</span>
          </div>
          <FileArchive size={19} />
        </div>
        <div className="pack-table">
          {packs.map((pack) => {
            const projectName = projectByPackId.get(pack.id) ?? "미지정";
            return (
              <div
                className={pack.id === selectedPackId ? "pack-table-row active" : "pack-table-row"}
                key={pack.id}
              >
                <strong>{pack.title}</strong>
                <span>{projectName}</span>
                <span>{pack.source}</span>
                <span>{numberLabel(pack.counts.nodes)}개 노드</span>
                <span>{numberLabel(pack.counts.edges)}개 엣지</span>
                <em>{validationLabel(pack.validationStatus)}</em>
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
}

function McpConnectionsView({
  mcpStatus,
  onLoadUrl,
  onRegenerateUrl,
}: {
  mcpStatus: McpStatus | null;
  onLoadUrl: () => Promise<void>;
  onRegenerateUrl: () => void;
}) {
  return (
    <section className="mcp-workspace-grid">
      <McpUrlPanel mcpStatus={mcpStatus} onLoadUrl={onLoadUrl} onRegenerateUrl={onRegenerateUrl} />
    </section>
  );
}

function McpUrlPanel({
  mcpStatus,
  onLoadUrl,
  onRegenerateUrl,
}: {
  mcpStatus: McpStatus | null;
  onLoadUrl: () => Promise<void>;
  onRegenerateUrl: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const [loading, setLoading] = useState(false);
  const userUrl = mcpStatus?.remote?.userUrl;
  const personalUrl = userUrl?.publicUrl || userUrl?.localUrl || "로그인 후 개인 MCP URL이 발급됩니다.";
  const activeUrl = personalUrl;
  const canCopy = activeUrl.startsWith("http") && !activeUrl.includes("{token}");

  async function copyUrl() {
    if (!canCopy) return;
    await navigator.clipboard?.writeText(activeUrl);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1400);
  }

  async function loadUrl() {
    setLoading(true);
    try {
      await onLoadUrl();
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="overview-panel mcp-url-panel">
      <div className="panel-header slim">
        <div>
          <h2>MCP URL</h2>
          <span>{mcpStatus?.remote?.transport ?? "streamable-http"}</span>
        </div>
        <div className="mcp-url-actions">
          <button type="button" onClick={loadUrl} disabled={loading}>
            {loading ? "불러오는 중" : "URL 불러오기"}
          </button>
          <button className="mcp-url-regenerate-button" type="button" onClick={onRegenerateUrl}>
            URL 재발급
          </button>
          <PlugZap size={19} />
        </div>
      </div>

      <div className="mcp-url-card">
        <span>사용자별 MCP URL</span>
        <code>{activeUrl}</code>
        <button type="button" disabled={!canCopy} onClick={copyUrl}>
          {copied ? "복사됨" : "복사"}
        </button>
      </div>

      {userUrl ? (
        <div className="mcp-url-scope">
          <span>사용자 {userUrl.userEmail}</span>
          <span>회사 {userUrl.company || "전체"}</span>
          <span>권한 {roleLabel(userUrl.role)}</span>
        </div>
      ) : null}

    </div>
  );
}

function AdminLockedView() {
  return (
    <section className="admin-locked" aria-label="관리자 전용">
      <LockKeyhole size={58} strokeWidth={1.8} />
    </section>
  );
}

function AdminView({
  companies,
  companyProjectAccess,
  currentUser,
  projects,
  users,
  uploadStatus,
  onAddCompany,
  onApproveUser,
  onConfirm,
  onDeleteCompany,
  onDeleteUser,
  onMoveUserCompany,
  onRenameCompany,
  onRefreshUsers,
  onReindex,
  onSetCompanyProjectAccess,
  onSetUserRole,
  onUpload,
}: {
  companies: string[];
  companyProjectAccess: CompanyProjectAccess;
  currentUser: CurrentUser;
  projects: Project[];
  users: ManagedUser[];
  uploadStatus: string;
  onAddCompany: (name: string) => void;
  onApproveUser: (email: string, role?: "admin" | "member") => void;
  onConfirm: (options: ConfirmDialogOptions) => void;
  onDeleteCompany: (name: string, deleteUsers?: boolean) => void;
  onDeleteUser: (email: string) => void;
  onMoveUserCompany: (email: string, company: string) => void;
  onRenameCompany: (name: string, newName: string) => void;
  onRefreshUsers: () => void;
  onReindex: () => void;
  onSetCompanyProjectAccess: (company: string, projectIds: string[]) => void;
  onSetUserRole: (email: string, role: "admin" | "member") => void;
  onUpload: () => void;
}) {
  const [selectedCompany, setSelectedCompany] = useState("all");
  const [companyDraft, setCompanyDraft] = useState("");
  const [selectedUserEmail, setSelectedUserEmail] = useState("");
  const suppressCompanyClickUntilRef = useRef(0);
  const unknownCompany = "미지정";
  const unassignedCompanyFilter = "__unassigned__";
  const isVirtualCompanyFilter = selectedCompany === unassignedCompanyFilter;
  const selectedProjectIds = companyProjectAccess[selectedCompany] ?? [];
  const selectedCompanyIsInternal =
    selectedCompany !== "all" &&
    !isVirtualCompanyFilter &&
    (selectedCompany.toLowerCase().includes("kumkang") || selectedCompany.includes("금강"));
  const companyNames = Array.from(new Set(companies)).sort();
  const companyFilters = ["all", ...companyNames];
  const visibleUsers =
    selectedCompany === "all"
      ? users
      : selectedCompany === unassignedCompanyFilter
        ? users.filter((user) => !user.company)
        : users.filter((user) => user.company === selectedCompany);

  useEffect(() => {
    setCompanyDraft(selectedCompany === "all" || selectedCompany === unassignedCompanyFilter ? "" : selectedCompany);
  }, [selectedCompany]);

  useEffect(() => {
    if (selectedUserEmail && !users.some((user) => user.email === selectedUserEmail)) {
      setSelectedUserEmail("");
    }
  }, [selectedUserEmail, users]);

  function companyStats(company: string) {
    const companyUsers =
      company === "all"
        ? users
        : company === unassignedCompanyFilter
          ? users.filter((user) => !user.company)
          : users.filter((user) => user.company === company);
    return {
      total: companyUsers.length,
      pending: companyUsers.filter((user) => user.status === "pending").length,
      admins: companyUsers.filter((user) => user.role === "admin").length,
    };
  }

  const unassignedStats = companyStats(unassignedCompanyFilter);

  function addCompany() {
    const name = companyDraft.trim();
    if (!name) return;
    onAddCompany(name);
    setCompanyDraft("");
  }

  function renameCompany() {
    if (selectedCompany === "all" || selectedCompany === unassignedCompanyFilter || !companyDraft.trim()) return;
    onRenameCompany(selectedCompany, companyDraft);
    setSelectedCompany(companyDraft.trim());
  }

  function deleteCompany() {
    if (selectedCompany === "all" || selectedCompany === unassignedCompanyFilter) return;
    const stats = companyStats(selectedCompany);
    onConfirm({
      title: "회사 삭제",
      message: stats.total > 0
        ? `${selectedCompany} 회사와 소속 사용자 ${stats.total}명을 함께 삭제하시겠습니까?`
        : `${selectedCompany} 회사를 삭제하시겠습니까?`,
      confirmLabel: "삭제",
      tone: "danger",
      onConfirm: () => {
        onDeleteCompany(selectedCompany, stats.total > 0);
        setSelectedCompany("all");
      },
    });
  }

  function deleteUser(user: ManagedUser) {
    onConfirm({
      title: "회원 삭제",
      message: `${user.name} (${user.email}) 회원을 삭제하시겠습니까?`,
      confirmLabel: "삭제",
      tone: "danger",
      onConfirm: () => onDeleteUser(user.email),
    });
  }

  function dropUserOnCompany(event: DragEvent<HTMLElement>, company: string) {
    event.preventDefault();
    event.stopPropagation();
    suppressCompanyClickUntilRef.current = Date.now() + 350;
    if (company === "all" || company === unassignedCompanyFilter) return;
    const email = event.dataTransfer.getData("text/plain");
    if (email) {
      onMoveUserCompany(email, company);
      setSelectedUserEmail(email);
    }
  }

  function selectCompanyCard(company: string) {
    if (Date.now() < suppressCompanyClickUntilRef.current) return;
    setSelectedCompany(company);
  }

  function toggleUnassignedFilter() {
    setSelectedUserEmail("");
    setSelectedCompany((company) => company === unassignedCompanyFilter ? "all" : unassignedCompanyFilter);
  }

  function selectUserCard(user: ManagedUser) {
    setSelectedUserEmail(user.email);
  }

  function toggleCompanyProject(projectId: string) {
    if (selectedCompany === "all" || selectedCompany === unassignedCompanyFilter || selectedCompanyIsInternal) return;
    const nextIds = selectedProjectIds.includes(projectId)
      ? selectedProjectIds.filter((id) => id !== projectId)
      : [...selectedProjectIds, projectId];
    onSetCompanyProjectAccess(selectedCompany, nextIds);
  }

  return (
    <section className="admin-grid">
      <div className="overview-panel">
        <div className="panel-header slim">
          <div>
            <h2>관리자 세션</h2>
            <span>{currentUser.email}</span>
          </div>
          <KeyRound size={19} />
        </div>
        <div className="auth-card">
          <strong>{currentUser.name}</strong>
          <span>{roleLabel(currentUser.role)} / 활성 계정</span>
        </div>
      </div>

      <div className="overview-panel">
        <div className="panel-header slim">
          <div>
            <h2>관리자 작업</h2>
            <span>{uploadStatus || "팩 생성과 색인은 관리자만 실행합니다"}</span>
          </div>
          <ShieldCheck size={19} />
        </div>
        <div className="action-stack">
          <button className="primary-button block" onClick={onUpload}>
            <RefreshCw size={17} />
            Drive 동기화
          </button>
        </div>
      </div>

      <div className="overview-panel wide user-admin-panel">
        <div className="panel-header slim">
          <div>
            <h2>회원 승인 및 권한</h2>
            <span>회사별로 회원가입 요청과 권한을 관리합니다</span>
          </div>
          <div className="company-action-bar">
            <button
              aria-pressed={selectedCompany === unassignedCompanyFilter}
              className={selectedCompany === unassignedCompanyFilter ? "unassigned-filter-button active" : "unassigned-filter-button"}
              title="회사 미지정 회원만 필터링"
              type="button"
              onClick={toggleUnassignedFilter}
            >
              미지정
              <span>{unassignedStats.total}</span>
            </button>
            <input
              value={companyDraft}
              placeholder={selectedCompany === "all" || selectedCompany === unassignedCompanyFilter ? "새 회사명" : "회사명"}
              onChange={(event) => setCompanyDraft(event.target.value)}
            />
            <button type="button" onClick={addCompany}>회사 추가</button>
            <button type="button" disabled={selectedCompany === "all" || selectedCompany === unassignedCompanyFilter} onClick={renameCompany}>회사명 수정</button>
            <button type="button" disabled={selectedCompany === "all" || selectedCompany === unassignedCompanyFilter} onClick={deleteCompany}>회사 삭제</button>
            <button className="icon-action" type="button" onClick={onRefreshUsers} title="새로고침">
              <RefreshCw size={17} />
            </button>
          </div>
        </div>
        <div className="company-admin-grid">
          <div className="company-list">
            {companyFilters.map((company) => {
              const stats = companyStats(company);
              const isDropTarget = company !== "all" && company !== unassignedCompanyFilter;
              return (
                <button
                  className={[
                    "company-card",
                    selectedCompany === company ? "active" : "",
                    isDropTarget ? "drop-target" : "",
                  ].filter(Boolean).join(" ")}
                  key={company}
                  type="button"
                  onDragOver={(event) => {
                    if (isDropTarget) event.preventDefault();
                  }}
                  onDrop={(event) => dropUserOnCompany(event, company)}
                  onClick={() => selectCompanyCard(company)}
                >
                  <strong>{company === "all" ? "전체 회사" : company}</strong>
                  <span>{stats.total}명 / 승인 대기 {stats.pending}명 / 관리자 {stats.admins}명</span>
                </button>
              );
            })}
          </div>

          <div className="user-table">
            {visibleUsers.map((user) => (
              <div
                className={selectedUserEmail === user.email ? "user-row selected" : "user-row"}
                draggable={user.email !== currentUser.email}
                key={user.email}
                onClick={() => selectUserCard(user)}
                onDragStart={(event) => {
                  event.dataTransfer.effectAllowed = "move";
                  event.dataTransfer.setData("text/plain", user.email);
                  setSelectedUserEmail(user.email);
                }}
              >
                <div>
                  <strong><span className="field-label">이름</span>{user.name}</strong>
                  <span><span className="field-label">회사</span>{user.company || unknownCompany} · {user.email}</span>
                </div>
                <em className={`status-badge ${user.status}`}>{user.status === "pending" ? "승인 대기" : "활성"}</em>
                <select
                  value={user.role}
                  disabled={user.email === currentUser.email}
                  onClick={(event) => event.stopPropagation()}
                  onChange={(event) => onSetUserRole(user.email, event.target.value as "admin" | "member")}
                >
                  <option value="member">멤버</option>
                  <option value="admin">관리자</option>
                </select>
                <div className="user-actions">
                  {user.status === "pending" ? (
                    <button
                      type="button"
                      onClick={(event) => {
                        event.stopPropagation();
                        onApproveUser(user.email, user.role);
                      }}
                    >
                      승인
                    </button>
                  ) : null}
                  <button
                    className="danger-button"
                    disabled={user.email === currentUser.email}
                    type="button"
                    onClick={(event) => {
                      event.stopPropagation();
                      deleteUser(user);
                    }}
                  >
                    삭제
                  </button>
                </div>
              </div>
            ))}
            {!visibleUsers.length && <p className="empty-note">해당 회사에 등록된 회원이 없습니다.</p>}
          </div>
        </div>

        <div className="project-access-panel">
          <div>
            <strong>프로젝트 접근권한</strong>
            <span>
              {selectedCompany === "all"
                ? "회사를 선택하면 프로젝트 접근권한을 지정할 수 있습니다."
                : selectedCompany === unassignedCompanyFilter
                  ? "회사 미지정 회원은 회사 카드로 드래그해 소속을 지정할 수 있습니다."
                : selectedCompanyIsInternal
                  ? "금강 계열 회사는 모든 프로젝트에 자동 접근합니다."
                  : "고객사/발주처는 관리자가 지정한 프로젝트만 볼 수 있습니다."}
            </span>
          </div>
          {selectedCompany !== "all" && selectedCompany !== unassignedCompanyFilter && !selectedCompanyIsInternal && (
            <div className="project-access-list">
              {projects.map((project) => (
                <label key={project.id}>
                  <input
                    checked={selectedProjectIds.includes(project.id)}
                    type="checkbox"
                    onChange={() => toggleCompanyProject(project.id)}
                  />
                  <span>
                    <strong>{project.name}</strong>
                    <small>{project.discipline} / {project.packIds.length}개 팩</small>
                  </span>
                </label>
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="overview-panel wide">
        <div className="panel-header slim">
          <div>
            <h2>권한 정책</h2>
            <span>일반 계정은 관리자 메뉴에 접근할 수 없습니다</span>
          </div>
          <Users size={19} />
        </div>
        <div className="policy-grid">
          <span><CheckCircle2 size={16} /> 관리자: 회원 승인, 권한 변경, Drive 동기화, 재색인</span>
          <span><CheckCircle2 size={16} /> 멤버: 프로젝트와 그래프 탐색, MCP 연결 정보 확인</span>
          <span><CheckCircle2 size={16} /> 승인 대기: 로그인 차단</span>
        </div>
      </div>
    </section>
  );
}

function Metric({
  icon: Icon,
  label,
  value,
  tone,
}: {
  icon: typeof Activity;
  label: string;
  value: string;
  tone?: "green";
}) {
  return (
    <div className="metric">
      <div className={tone === "green" ? "metric-icon green" : "metric-icon"}>
        <Icon size={20} />
      </div>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

type NodeIndexGroup = {
  label: string;
  values: string[];
  total?: number;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function compactIdentifier(value: unknown) {
  const text = String(value ?? "").trim();
  if (!text) return "";
  if (!text.includes(":")) return text;
  const parts = text.split(":");
  return parts[parts.length - 1] || text;
}

function uniqueValues(values: unknown[], limit = 18) {
  const seen = new Set<string>();
  const result: string[] = [];
  values.forEach((value) => {
    const text = compactIdentifier(value);
    if (!text || seen.has(text)) return;
    seen.add(text);
    if (result.length < limit) result.push(text);
  });
  return { values: result, total: seen.size };
}

function addIndexGroup(groups: NodeIndexGroup[], label: string, values: unknown[], limit = 18) {
  const index = uniqueValues(values, limit);
  if (!index.values.length) return;
  groups.push({ label, values: index.values, total: index.total });
}

function collectNodeIndexGroups(node: GraphNode): NodeIndexGroup[] {
  const props = node.properties ?? {};
  const groups: NodeIndexGroup[] = [];

  addIndexGroup(groups, "모듈 인덱스", [props.module_id, ...(Array.isArray(props.module_types) ? props.module_types : [])], 8);
  addIndexGroup(groups, "어셈블리 번호", [props.assembly_mark, props.assembly_no, props.assembly_number], 18);
  addIndexGroup(groups, "어셈블리 ID", [props.assembly_id], 18);
  addIndexGroup(groups, "싱글파트 번호", [props.single_part_mark, props.single_part_no, props.single_part_number], 18);
  addIndexGroup(groups, "싱글파트 ID", [props.single_part_id, props.main_single_part_id], 18);

  if (Array.isArray(props.assemblies)) {
    const assemblies = props.assemblies.filter(isRecord);
    addIndexGroup(groups, "어셈블리 번호", assemblies.map((item) => item.assembly_mark), 24);
    addIndexGroup(groups, "어셈블리 ID", assemblies.map((item) => item.assembly_id), 24);
    addIndexGroup(groups, "싱글파트 번호", assemblies.map((item) => item.single_part_mark ?? item.main_single_part_id), 24);
  }

  if (isRecord(props.by_section)) addIndexGroup(groups, "섹션 인덱스", Object.keys(props.by_section), 18);
  if (isRecord(props.by_material)) addIndexGroup(groups, "자재 인덱스", Object.keys(props.by_material), 18);
  if (isRecord(props.by_single_part_category)) addIndexGroup(groups, "부재 카테고리", Object.keys(props.by_single_part_category), 12);

  const merged = new Map<string, NodeIndexGroup>();
  groups.forEach((group) => {
    const current = merged.get(group.label);
    if (!current) {
      merged.set(group.label, group);
      return;
    }
    const next = uniqueValues([...current.values, ...group.values], 24);
    current.values = next.values;
    current.total = Math.max(current.total ?? 0, group.total ?? 0, next.total);
  });
  return Array.from(merged.values()).filter((group) => group.values.length);
}

function propertyLabel(key: string) {
  const labels: Record<string, string> = {
    id: "ID",
    module_id: "모듈 ID",
    module_type: "모듈 타입",
    assembly_id: "어셈블리 ID",
    assembly_mark: "어셈블리 번호",
    assembly_role: "어셈블리 역할",
    single_part_id: "싱글파트 ID",
    single_part_mark: "싱글파트 번호",
    main_single_part_id: "메인 싱글파트 ID",
    assembly_count: "어셈블리 수",
    single_part_count: "싱글파트 수",
    main_single_part_count: "메인 싱글파트 수",
    attached_single_part_count: "부착 싱글파트 수",
    total_weight_kg: "총 중량",
    total_length_m: "총 길이",
    weight_kg: "중량",
    length_m: "길이",
    count: "수량",
    quantity: "수량",
  };
  return labels[key] ?? key;
}

function formatPropertyValue(key: string, value: unknown) {
  if (typeof value !== "number") return String(value);
  const lower = key.toLowerCase();
  const formatter = new Intl.NumberFormat("ko-KR", {
    maximumFractionDigits: Math.abs(value) >= 100 ? 3 : 4,
  });
  if (lower.endsWith("_kg") || lower.includes("weight_kg")) return `${formatter.format(value)} kg`;
  if (lower.endsWith("_m") || lower.includes("length_m")) return `${formatter.format(value)} m`;
  if (lower.endsWith("_mm") || lower.includes("diameter_mm") || lower.includes("length_mm")) return `${formatter.format(value)} mm`;
  if (lower.endsWith("_count") || lower.endsWith("count") || lower.includes("quantity")) {
    return `${new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 0 }).format(value)} ea`;
  }
  return formatter.format(value);
}

function formatAiQueryWarning(error: string) {
  const lower = error.toLowerCase();
  if (lower.includes("incorrect api key") || lower.includes("invalid_api_key") || lower.includes("401")) {
    return "GPT request failed: OpenAI API key가 올바르지 않거나 선택한 OpenAI project에서 사용할 수 없습니다.";
  }
  if (lower.includes("insufficient_quota") || lower.includes("quota") || lower.includes("billing") || lower.includes("429")) {
    return "GPT request failed: OpenAI project의 결제/크레딧/쿼터 상태를 확인해야 합니다.";
  }
  if (lower.includes("model") && (lower.includes("not found") || lower.includes("does not exist") || lower.includes("access"))) {
    return `GPT request failed: 이 API key/project가 ${OPENAI_CHAT_MODEL} 모델에 접근하지 못합니다.`;
  }
  return `GPT request failed: ${error.slice(0, 220)}${error.length > 220 ? "..." : ""}`;
}

function openAiKeyStatusLabel(status: AiKeyStatus) {
  if (status === "valid") return "API key valid";
  if (status === "invalid") return "API key invalid";
  if (status === "testing") return "Testing key";
  if (status === "untested") return "API key untested";
  return "API key required";
}

function AiQueryPanel({
  openAiKeyStatus,
  loading,
  messages,
  question,
  onQuestionChange,
  onSubmit,
}: {
  openAiKeyStatus: AiKeyStatus;
  loading: boolean;
  messages: AiMessage[];
  question: string;
  onQuestionChange: (value: string) => void;
  onSubmit: () => void;
}) {
  return (
    <div className="ai-query-panel">
      <div className="ai-message-list" aria-live="polite">
        {messages.length ? (
          messages.map((message) => (
            <article className={`ai-message ${message.role}`} key={message.id}>
              <strong>{message.role === "user" ? "질문" : "AI Query"}</strong>
              <p>{message.content}</p>
              {message.evidence?.length ? (
                <div className="ai-evidence-list">
                  {message.evidence.slice(0, 3).map((item, index) => (
                    <span key={`${message.id}-${item.path ?? index}`}>
                      {item.title || item.path || `근거 ${index + 1}`}
                    </span>
                  ))}
                </div>
              ) : null}
            </article>
          ))
        ) : (
          <div className="ai-empty-state">
            <strong>프로젝트 데이터에 바로 질문하세요</strong>
            <span>선택한 온톨로지 팩의 문서, 노드, 관계를 근거로 답변합니다.</span>
          </div>
        )}
        {loading ? (
          <article className="ai-message assistant ai-message-pending">
            <strong>AI Query</strong>
            <p>
              <LoaderCircle className="inline-loading-spinner" size={14} />
              답변을 생성중입니다.
            </p>
          </article>
        ) : null}
      </div>

      <form
        className="ai-chat-form"
        onSubmit={(event) => {
          event.preventDefault();
          onSubmit();
        }}
      >
        <textarea
          value={question}
          placeholder={
            "앱에서 질문을 하려면 먼저 OpenAI API key를 등록하세요! (비용발생)\n또는 MCP URL을 생성하고 개인의 AI Agent를 통해 질문하세요!"
          }
          onChange={(event) => onQuestionChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              onSubmit();
            }
          }}
        />
        <button type="submit" disabled={loading || !question.trim() || openAiKeyStatus !== "valid"} title="Send GPT query">
          <SendHorizontal size={16} />
          <span>Send</span>
        </button>
      </form>
    </div>
  );
}

function NodeDetails({ node }: { node: GraphNode }) {
  const indexGroups = collectNodeIndexGroups(node);
  const compactIndexes = node.type === "Module";
  const [expandedGroups, setExpandedGroups] = useState<Record<string, boolean>>({});
  const entries = Object.entries(node.properties ?? {})
    .filter(([, value]) => value !== null && value !== undefined && typeof value !== "object")
    .slice(0, 14);

  useEffect(() => {
    setExpandedGroups({});
  }, [node.id]);

  return (
    <div className="node-details">
      <div className="node-title">
        <i style={{ background: node.color }} />
        <div>
          <strong>{node.label}</strong>
          <span>{node.id}</span>
        </div>
      </div>
      {indexGroups.length ? (
        <div className={compactIndexes ? "node-index-groups compact" : "node-index-groups"}>
          {indexGroups.map((group) => (
            <section
              className={
                compactIndexes && !expandedGroups[group.label]
                  ? "node-index-group collapsed"
                  : "node-index-group expanded"
              }
              key={group.label}
            >
              <div className="node-index-heading">
                <strong>{group.label}</strong>
                <span>
                  {group.total && group.total > group.values.length ? `+${group.total - group.values.length}` : `${group.values.length}`}
                </span>
                {compactIndexes ? (
                  <button
                    type="button"
                    onClick={() =>
                      setExpandedGroups((current) => ({
                        ...current,
                        [group.label]: !current[group.label],
                      }))
                    }
                  >
                    {expandedGroups[group.label] ? "접기" : "펼치기"}
                  </button>
                ) : null}
              </div>
              <p>
                {group.values.map((value) => (
                  <code key={`${group.label}-${value}`}>{value}</code>
                ))}
              </p>
            </section>
          ))}
        </div>
      ) : null}
      <dl>
        <div>
          <dt>타입</dt>
          <dd>{nodeTypeLabel(node.type)}</dd>
        </div>
        {entries.map(([key, value]) => (
          <div key={key}>
            <dt>{propertyLabel(key)}</dt>
            <dd title={String(value)}>{formatPropertyValue(key, value)}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

type GraphViewStats = {
  shownNodes: number;
  shownEdges: number;
  loadedNodes: number;
  loadedEdges: number;
  totalNodes: number;
  totalEdges: number;
  typeCount: number;
  filtered: boolean;
};

function computeGraphViewStats(graph: GraphPayload, activeNodeTypes: Set<string>): GraphViewStats {
  const filtered = activeNodeTypes.size > 0;
  const visibleNodeIds = new Set(
    graph.nodes.filter((node) => !filtered || activeNodeTypes.has(node.type)).map((node) => node.id)
  );
  const shownEdges = graph.edges.filter((edge) => {
    const source = edgeEndpointId(edge.source);
    const target = edgeEndpointId(edge.target);
    return Boolean(source && target && visibleNodeIds.has(source) && visibleNodeIds.has(target));
  }).length;

  return {
    shownNodes: visibleNodeIds.size,
    shownEdges,
    loadedNodes: graph.nodes.length,
    loadedEdges: graph.edges.length,
    totalNodes: graph.stats.totalNodes,
    totalEdges: graph.stats.totalEdges,
    typeCount: new Set(graph.nodes.map((node) => node.type)).size,
    filtered,
  };
}

function GraphStatsPanel({ stats }: { stats: GraphViewStats }) {
  return (
    <aside className="graph-stats-panel" aria-label="그래프 통계">
      <div className="graph-stats-heading">
        <strong>그래프 통계</strong>
        <span>{stats.filtered ? "필터 적용" : `${stats.typeCount}개 타입`}</span>
      </div>
      <div className="graph-stats-grid">
        <span>표시 노드</span>
        <strong>{numberLabel(stats.shownNodes)}</strong>
        <span>표시 엣지</span>
        <strong>{numberLabel(stats.shownEdges)}</strong>
        <span>전체 노드</span>
        <strong>{numberLabel(stats.totalNodes)}</strong>
        <span>전체 엣지</span>
        <strong>{numberLabel(stats.totalEdges)}</strong>
      </div>
    </aside>
  );
}

function GraphProjectEmptyState({ projectName, hasPacks = false }: { projectName?: string; hasPacks?: boolean }) {
  const projectLabel = projectName ? `${projectName} 프로젝트` : "선택한 프로젝트";
  return (
    <div className="graph-project-empty-state project-empty-state">
      <Database size={30} />
      <strong>{hasPacks ? "표시할 팩을 선택하세요." : "온톨로지 팩이 없습니다."}</strong>
      <span>
        {hasPacks
          ? `${projectLabel}에 연결된 팩은 있습니다. 상단 필터에서 폴더나 팩을 선택하면 그래프를 로드합니다.`
          : `${projectLabel}에 연결된 온톨로지 팩이 없습니다.`}
      </span>
      <em>
        {hasPacks
          ? "처음에는 무거운 전체 그래프를 자동으로 불러오지 않습니다."
          : "Drive에 온톨로지 ZIP 팩을 올린 뒤 동기화 탭에서 등록하면 그래프 탐색기를 사용할 수 있습니다."}
      </em>
    </div>
  );
}

function GraphCanvas({
  graph,
  selectedNode,
  onSelectNode,
}: {
  graph: GraphPayload | null;
  selectedNode: GraphNode | null;
  onSelectNode: (node: GraphNode | null) => void;
}) {
  const [controls, setControls] = useState<GraphControls>(DEFAULT_GRAPH_CONTROLS);
  const [dynamicMode, setDynamicMode] = useState<DynamicGraphMode>("free");
  const [activeNodeTypes, setActiveNodeTypes] = useState<Set<string>>(new Set());
  const [layoutSeed, setLayoutSeed] = useState(0);
  const [controlHost, setControlHost] = useState<HTMLElement | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const rendererRef = useRef<Sigma | null>(null);
  const nodeLookupRef = useRef<Map<string, GraphNode>>(new Map());
  const simulationFrameRef = useRef<number | null>(null);
  const controlsFrameRef = useRef<number | null>(null);
  const velocitiesRef = useRef<Map<string, { x: number; y: number }>>(new Map());
  const dynamicModeRef = useRef<DynamicGraphMode>("free");
  const controlsRef = useRef<GraphControls>(DEFAULT_GRAPH_CONTROLS);
  const graphStats = graph ? computeGraphViewStats(graph, activeNodeTypes) : null;

  useEffect(() => {
    dynamicModeRef.current = dynamicMode;
  }, [dynamicMode]);

  useEffect(() => {
    controlsRef.current = controls;
  }, [controls]);

  useEffect(() => {
    setControlHost(document.getElementById("graph-sidebar-controls"));
  }, [graph]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container || !graph) return;

    const sigmaGraph = new Graph({ multi: true, allowSelfLoops: true });
    const nodeLookup = new Map<string, GraphNode>();
    const positions = layoutGraphNodes(graph.nodes, graph.edges, controlsRef.current, "full");
    const velocities = new Map<string, { x: number; y: number }>();
    const draggedNodeRef: { id: string | null } = { id: null };
    velocitiesRef.current = velocities;
    graph.nodes.forEach((node) => {
      const point = positions.get(node.id) ?? { x: 0, y: 0 };
      nodeLookup.set(node.id, node);
      velocities.set(node.id, { x: 0, y: 0 });
      sigmaGraph.addNode(node.id, {
        x: point.x,
        y: point.y,
        label: node.label,
        nodeKind: node.type,
        color: node.color,
        baseColor: node.color,
        size: displayNodeSize(node, controlsRef.current),
        properties: node.properties,
      });
    });

    graph.edges.forEach((edge, index) => {
      const source = edgeEndpointId(edge.source);
      const target = edgeEndpointId(edge.target);
      if (!source || !target || !sigmaGraph.hasNode(source) || !sigmaGraph.hasNode(target)) return;

      const key = edge.id || `${source}-${target}-${index}`;
      const attributes = {
        label: edge.relation,
        relation: edge.relation,
        color: GRAPH_EDGE_COLOR,
        size: String(edge.relation).includes("chunk") ? 0.75 : 1.15,
      };

      if (sigmaGraph.hasEdge(key)) {
        sigmaGraph.addEdgeWithKey(`${key}-${index}`, source, target, attributes);
      } else {
        sigmaGraph.addEdgeWithKey(key, source, target, attributes);
      }
    });

    const renderer = new Sigma(sigmaGraph, container, {
      defaultEdgeColor: GRAPH_EDGE_COLOR,
      defaultEdgeType: "line",
      enableEdgeEvents: false,
      enableCameraPanning: true,
      labelColor: { color: "#334155" },
      labelDensity: 0.12,
      labelGridCellSize: 72,
      labelRenderedSizeThreshold: 8,
      minCameraRatio: 0.08,
      maxCameraRatio: 8,
      renderEdgeLabels: false,
      renderLabels: true,
      zIndex: true,
    });

    rendererRef.current = renderer;
    nodeLookupRef.current = nodeLookup;
    setCameraStateWithPan(renderer, {
      x: 0.5,
      y: 0.5,
      angle: 0,
      ratio: 1.08,
    });
    renderer.on("clickNode", ({ node }) => {
      const selected = nodeLookup.get(node);
      if (selected) onSelectNode(selected);
    });
    renderer.on("clickStage", () => {
      onSelectNode(null);
    });
    renderer.on("downNode", ({ node }) => {
      draggedNodeRef.id = node;
      sigmaGraph.setNodeAttribute(node, "highlighted", true);
      container.classList.add("dragging-node");
      renderer.getCamera().disable();
    });
    renderer.on("upNode", () => {
      if (draggedNodeRef.id) sigmaGraph.setNodeAttribute(draggedNodeRef.id, "highlighted", false);
      draggedNodeRef.id = null;
      container.classList.remove("dragging-node");
      renderer.getCamera().enable();
    });
    renderer.on("moveBody", ({ event }) => {
      const nodeId = draggedNodeRef.id;
      if (!nodeId) return;
      const point = renderer.viewportToGraph({ x: event.x, y: event.y });
      sigmaGraph.mergeNodeAttributes(nodeId, { x: point.x, y: point.y, fixed: true, zIndex: 20 });
      velocities.set(nodeId, { x: 0, y: 0 });
    });

    let middlePan:
      | {
          lastX: number;
          lastY: number;
        }
      | null = null;

    const viewportPointFromMouse = (event: MouseEvent) => {
      const rect = container.getBoundingClientRect();
      return {
        x: event.clientX - rect.left,
        y: event.clientY - rect.top,
      };
    };

    const handleMouseDown = (event: MouseEvent) => {
      if (event.button !== 1) return;
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation();
      const point = viewportPointFromMouse(event);
      middlePan = {
        lastX: point.x,
        lastY: point.y,
      };
      container.classList.add("middle-panning");
    };

    const handleMouseMove = (event: MouseEvent) => {
      if (!middlePan) return;
      event.preventDefault();
      event.stopPropagation();
      const point = viewportPointFromMouse(event);
      const lastMouse = renderer.viewportToFramedGraph({ x: middlePan.lastX, y: middlePan.lastY });
      const mouse = renderer.viewportToFramedGraph(point);
      const cameraState = renderer.getCamera().getState();
      setCameraStateWithPan(renderer, {
        ...cameraState,
        x: cameraState.x + lastMouse.x - mouse.x,
        y: cameraState.y + lastMouse.y - mouse.y,
      });
      middlePan.lastX = point.x;
      middlePan.lastY = point.y;
    };

    const handleMouseUp = () => {
      middlePan = null;
      container.classList.remove("middle-panning");
    };

    const blockMiddleClick = (event: MouseEvent) => {
      if (event.button !== 1) return;
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation();
    };

    container.addEventListener("mousedown", handleMouseDown, true);
    container.addEventListener("auxclick", blockMiddleClick, true);
    window.addEventListener("mousemove", handleMouseMove);
    window.addEventListener("mouseup", handleMouseUp);

    let frame = 0;
    const runSimulation = () => {
      frame += 1;
      stepDynamicGraph(sigmaGraph, graph, velocities, controlsRef.current, dynamicModeRef.current, draggedNodeRef.id);
      if (frame % 2 === 0) renderer.refresh();
      simulationFrameRef.current = requestAnimationFrame(runSimulation);
    };
    simulationFrameRef.current = requestAnimationFrame(runSimulation);

    return () => {
      if (simulationFrameRef.current !== null) {
        cancelAnimationFrame(simulationFrameRef.current);
        simulationFrameRef.current = null;
      }
      container.removeEventListener("mousedown", handleMouseDown, true);
      container.removeEventListener("auxclick", blockMiddleClick, true);
      window.removeEventListener("mousemove", handleMouseMove);
      window.removeEventListener("mouseup", handleMouseUp);
      renderer.kill();
      if (rendererRef.current === renderer) rendererRef.current = null;
      if (velocitiesRef.current === velocities) velocitiesRef.current = new Map();
    };
  }, [graph, layoutSeed, onSelectNode]);

  useEffect(() => {
    const renderer = rendererRef.current;
    if (!renderer || !graph) return;
    if (controlsFrameRef.current !== null) cancelAnimationFrame(controlsFrameRef.current);

    controlsFrameRef.current = requestAnimationFrame(() => {
      controlsFrameRef.current = null;
      const sigmaGraph = renderer.getGraph();
      const nodeIds: string[] = [];
      velocitiesRef.current.forEach((velocity) => {
        velocity.x = 0;
        velocity.y = 0;
      });

      graph.nodes.forEach((node) => {
        if (!sigmaGraph.hasNode(node.id)) return;
        nodeIds.push(node.id);
        sigmaGraph.mergeNodeAttributes(node.id, {
          size: displayNodeSize(node, controls),
        });
      });

      renderer.refresh({ partialGraph: { nodes: nodeIds } });
    });

    return () => {
      if (controlsFrameRef.current !== null) {
        cancelAnimationFrame(controlsFrameRef.current);
        controlsFrameRef.current = null;
      }
    };
  }, [controls, graph]);

  useEffect(() => {
    const renderer = rendererRef.current;
    if (!renderer || !graph) return;
    const sigmaGraph = renderer.getGraph();
    const hiddenTypes = activeNodeTypes;
    graph.nodes.forEach((node) => {
      if (!sigmaGraph.hasNode(node.id)) return;
      sigmaGraph.setNodeAttribute(node.id, "hidden", hiddenTypes.size > 0 && !hiddenTypes.has(node.type));
    });
    graph.edges.forEach((edge, index) => {
      const source = edgeEndpointId(edge.source);
      const target = edgeEndpointId(edge.target);
      const key = edge.id || `${source}-${target}-${index}`;
      const edgeKey = sigmaGraph.hasEdge(key) ? key : `${key}-${index}`;
      if (!sigmaGraph.hasEdge(edgeKey) || !source || !target) return;
      const hidden =
        (hiddenTypes.size > 0 && !hiddenTypes.has(nodeLookupRef.current.get(source)?.type ?? "")) ||
        (hiddenTypes.size > 0 && !hiddenTypes.has(nodeLookupRef.current.get(target)?.type ?? ""));
      sigmaGraph.setEdgeAttribute(edgeKey, "hidden", hidden);
    });
    renderer.refresh();
  }, [activeNodeTypes, graph]);

  useEffect(() => {
    const renderer = rendererRef.current;
    if (!renderer) return;

    const selectedId = selectedNode?.id;
    renderer.setSetting("nodeReducer", (node, data) => {
      const next = { ...data };
      if (!selectedId) return next;
      if (node === selectedId) {
        next.color = data.baseColor ?? data.color;
        next.zIndex = 10;
        return next;
      }
      next.color = "rgba(148, 163, 184, 0.62)";
      next.zIndex = 0;
      return next;
    });

    renderer.setSetting("edgeReducer", (edge, data) => {
      const next = { ...data };
      if (!selectedId) return next;

      const [source, target] = renderer.getGraph().extremities(edge);
      const connected = source === selectedId || target === selectedId;
      next.color = connected ? GRAPH_EDGE_SELECTED_COLOR : GRAPH_EDGE_DIMMED_COLOR;
      next.size = connected ? Number(data.size ?? 1) * 1.25 : Number(data.size ?? 1);
      next.zIndex = connected ? 5 : 0;
      return next;
    });
    renderer.refresh();
  }, [selectedNode]);

  const controlPanel = (
    <GraphControlPanel
        controls={controls}
        mode={dynamicMode}
        nodeTypes={graph ? Array.from(new Set(graph.nodes.map((node) => node.type))) : []}
        activeNodeTypes={activeNodeTypes}
        onChange={setControls}
        onModeChange={setDynamicMode}
        onReset={() => setLayoutSeed((seed) => seed + 1)}
        onToggleNodeType={(type) =>
          setActiveNodeTypes((current) => {
            const next = new Set(current);
            if (next.has(type)) next.delete(type);
            else next.add(type);
            return next;
          })
        }
      />
  );

  return (
    <div className="graph-canvas-wrap">
      <div ref={containerRef} className="sigma-stage" />
      {controlHost ? createPortal(controlPanel, controlHost) : null}
      {graphStats ? <GraphStatsPanel stats={graphStats} /> : null}
      {!graph && (
        <div className="graph-loading-overlay" aria-label="그래프 로딩 중">
          <LoaderCircle className="loading-spinner" size={26} />
        </div>
      )}
    </div>
  );
}

function edgeEndpointId(endpoint: GraphEdge["source"] | GraphEdge["target"]) {
  return typeof endpoint === "string" ? endpoint : endpoint?.id;
}

function stepDynamicGraph(
  sigmaGraph: Graph,
  payload: GraphPayload,
  velocities: Map<string, { x: number; y: number }>,
  controls: GraphControls,
  mode: DynamicGraphMode,
  draggedNodeId: string | null
) {
  const nodeIds = sigmaGraph.nodes().filter((nodeId) => !sigmaGraph.getNodeAttribute(nodeId, "hidden"));
  const nodeIndex = new Map(payload.nodes.map((node) => [node.id, node]));
  const typeOrder = Array.from(new Set(payload.nodes.map((node) => node.type)));
  const graphRadius = Math.max(90, Math.sqrt(nodeIds.length) * controls.nodeDistance * 0.12);
  const chargeRadius = Math.max(22, controls.nodeDistance * 0.18);
  const chargeStrength = controls.repelForce * 0.00042;
  const linkStrength = controls.linkForce * 0.012;
  const centerStrength = Math.max(0.002, controls.centerForce * 0.18);
  let freeCenterX = 0;
  let freeCenterY = 0;

  if (mode === "free" && nodeIds.length) {
    nodeIds.forEach((nodeId) => {
      freeCenterX += Number(sigmaGraph.getNodeAttribute(nodeId, "x") ?? 0);
      freeCenterY += Number(sigmaGraph.getNodeAttribute(nodeId, "y") ?? 0);
    });
    freeCenterX /= nodeIds.length;
    freeCenterY /= nodeIds.length;
  }

  payload.edges.forEach((edge) => {
    const source = edgeEndpointId(edge.source);
    const target = edgeEndpointId(edge.target);
    if (!source || !target || !sigmaGraph.hasNode(source) || !sigmaGraph.hasNode(target)) return;
    if (sigmaGraph.getNodeAttribute(source, "hidden") || sigmaGraph.getNodeAttribute(target, "hidden")) return;

    const sourceX = Number(sigmaGraph.getNodeAttribute(source, "x") ?? 0);
    const sourceY = Number(sigmaGraph.getNodeAttribute(source, "y") ?? 0);
    const targetX = Number(sigmaGraph.getNodeAttribute(target, "x") ?? 0);
    const targetY = Number(sigmaGraph.getNodeAttribute(target, "y") ?? 0);
    const dx = targetX - sourceX;
    const dy = targetY - sourceY;
    const distance = Math.max(0.001, Math.hypot(dx, dy));
    const desired = String(edge.relation).includes("chunk")
      ? controls.nodeDistance * 0.055
      : controls.nodeDistance * 0.092;
    const pull = (distance - desired) * linkStrength;
    const fx = (dx / distance) * pull;
    const fy = (dy / distance) * pull;
    if (source !== draggedNodeId) {
      const velocity = velocities.get(source);
      if (velocity) {
        velocity.x += fx;
        velocity.y += fy;
      }
    }
    if (target !== draggedNodeId) {
      const velocity = velocities.get(target);
      if (velocity) {
        velocity.x -= fx;
        velocity.y -= fy;
      }
    }
  });

  for (let i = 0; i < nodeIds.length; i += 1) {
    const a = nodeIds[i];
    const aX = Number(sigmaGraph.getNodeAttribute(a, "x") ?? 0);
    const aY = Number(sigmaGraph.getNodeAttribute(a, "y") ?? 0);
    for (let j = i + 1; j < nodeIds.length; j += 1) {
      const b = nodeIds[j];
      const bX = Number(sigmaGraph.getNodeAttribute(b, "x") ?? 0);
      const bY = Number(sigmaGraph.getNodeAttribute(b, "y") ?? 0);
      let dx = bX - aX;
      let dy = bY - aY;
      let distance = Math.hypot(dx, dy);
      if (distance > chargeRadius) continue;
      if (distance < 0.001) {
        const angle = ((stableHash(`${a}:${b}`) % 360) / 180) * Math.PI;
        dx = Math.cos(angle);
        dy = Math.sin(angle);
        distance = 1;
      }
      const push = ((chargeRadius - distance) / chargeRadius) * chargeStrength;
      const fx = (dx / distance) * push;
      const fy = (dy / distance) * push;
      const aVelocity = velocities.get(a);
      const bVelocity = velocities.get(b);
      if (a !== draggedNodeId && aVelocity) {
        aVelocity.x -= fx;
        aVelocity.y -= fy;
      }
      if (b !== draggedNodeId && bVelocity) {
        bVelocity.x += fx;
        bVelocity.y += fy;
      }
    }
  }

  nodeIds.forEach((nodeId, index) => {
    if (nodeId === draggedNodeId) return;
    const node = nodeIndex.get(nodeId);
    const velocity = velocities.get(nodeId);
    if (!node || !velocity) return;

    const x = Number(sigmaGraph.getNodeAttribute(nodeId, "x") ?? 0);
    const y = Number(sigmaGraph.getNodeAttribute(nodeId, "y") ?? 0);
    let targetX = 0;
    let targetY = 0;

    if (mode === "free") {
      velocity.x -= freeCenterX * centerStrength;
      velocity.y -= freeCenterY * centerStrength;
    } else if (mode === "typeOrbit") {
      const typeIndex = Math.max(0, typeOrder.indexOf(node.type));
      const angle = (typeIndex / Math.max(1, typeOrder.length)) * Math.PI * 2;
      const jitter = ((stableHash(node.id) % 100) - 50) * 0.05;
      targetX = Math.cos(angle) * graphRadius + jitter;
      targetY = Math.sin(angle) * graphRadius + jitter;
    } else if (mode === "radial") {
      const degreeBias = node.type === "Module" ? 0.22 : node.type === "Assembly" ? 0.52 : node.type === "SinglePart" ? 0.82 : 1.06;
      const angle = ((stableHash(node.id) % 3600) / 1800) * Math.PI + index * 0.002;
      targetX = Math.cos(angle) * graphRadius * degreeBias;
      targetY = Math.sin(angle) * graphRadius * degreeBias;
    }

    if (mode !== "free") {
      velocity.x += (targetX - x) * centerStrength;
      velocity.y += (targetY - y) * centerStrength;
    }
    velocity.x *= 0.82;
    velocity.y *= 0.82;
    const limit = mode === "free" ? 1.7 : 2.4;
    const nextX = x + Math.max(-limit, Math.min(limit, velocity.x));
    const nextY = y + Math.max(-limit, Math.min(limit, velocity.y));
    sigmaGraph.mergeNodeAttributes(nodeId, { x: nextX, y: nextY });
  });
}

function setCameraStateWithPan(
  renderer: Sigma,
  state: { x: number; y: number; angle: number; ratio: number }
) {
  const camera = renderer.getCamera();
  const previous = camera.enabledPanning;
  camera.enabledPanning = true;
  camera.setState(state);
  camera.enabledPanning = previous;
}

function GraphControlPanel({
  controls,
  mode,
  nodeTypes,
  activeNodeTypes,
  onChange,
  onModeChange,
  onReset,
  onToggleNodeType,
}: {
  controls: GraphControls;
  mode: DynamicGraphMode;
  nodeTypes: string[];
  activeNodeTypes: Set<string>;
  onChange: (controls: GraphControls) => void;
  onModeChange: (mode: DynamicGraphMode) => void;
  onReset: () => void;
  onToggleNodeType: (type: string) => void;
}) {
  const sliders: Array<{
    key: keyof GraphControls;
    label: string;
    min: number;
    max: number;
    step: number;
    digits: number;
  }> = [
    { key: "nodeSize", label: "노드 크기", min: 0.25, max: 1.2, step: 0.05, digits: 2 },
    { key: "nodeDistance", label: "노드 거리", min: 80, max: 520, step: 10, digits: 2 },
    { key: "linkForce", label: "링크 힘", min: 0.02, max: 0.6, step: 0.02, digits: 2 },
    { key: "centerForce", label: "중심 힘", min: 0, max: 0.16, step: 0.01, digits: 2 },
    { key: "repelForce", label: "척력", min: 120, max: 900, step: 10, digits: 2 },
  ];

  return (
    <div className="graph-control-panel">
      <div className="graph-control-title">
        <strong>Dynamic Graph</strong>
        <button type="button" onClick={onReset}>다시 정렬</button>
      </div>
      <div className="graph-mode-tabs" role="tablist" aria-label="그래프 레이아웃 모드">
        {[
          ["free", "자유"],
          ["typeOrbit", "타입별"],
          ["radial", "방사형"],
        ].map(([key, label]) => (
          <button
            className={mode === key ? "active" : ""}
            key={key}
            type="button"
            onClick={() => onModeChange(key as DynamicGraphMode)}
          >
            {label}
          </button>
        ))}
      </div>
      <div className="graph-type-filters">
        {nodeTypes.map((type) => (
          <button
            className={activeNodeTypes.size === 0 || activeNodeTypes.has(type) ? "active" : ""}
            key={type}
            type="button"
            onClick={() => onToggleNodeType(type)}
          >
            <i className={`dot ${type.toLowerCase()}`} />
            {nodeTypeLabel(type)}
          </button>
        ))}
      </div>
      {sliders.map((slider) => (
        <label className="graph-slider" key={slider.key}>
          <span>
            {slider.label}
            <output>{controls[slider.key].toFixed(slider.digits)}</output>
          </span>
          <input
            type="range"
            min={slider.min}
            max={slider.max}
            step={slider.step}
            value={controls[slider.key]}
            onChange={(event) =>
              onChange({
                ...controls,
                [slider.key]: Number(event.currentTarget.value),
              })
            }
          />
        </label>
      ))}
    </div>
  );
}

function displayNodeSize(node: GraphNode, controls: GraphControls) {
  return Math.max(1.8, Math.min(node.size * controls.nodeSize, 10));
}

function collisionRadiusFromSize(size: number) {
  return size * 0.42 + NODE_COLLISION_PADDING;
}

function graphCollisionRadius(node: GraphNode, controls: GraphControls) {
  return collisionRadiusFromSize(displayNodeSize(node, controls));
}

function layoutGraphNodes(
  nodes: GraphNode[],
  edges: GraphEdge[],
  controls: GraphControls,
  quality: LayoutQuality = "full"
) {
  const positions = new Map<string, { x: number; y: number }>();
  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  const degree = new Map(nodes.map((node) => [node.id, 0]));
  edges.forEach((edge) => {
    const source = edgeEndpointId(edge.source);
    const target = edgeEndpointId(edge.target);
    if (source && degree.has(source)) degree.set(source, (degree.get(source) ?? 0) + 1);
    if (target && degree.has(target)) degree.set(target, (degree.get(target) ?? 0) + 1);
  });
  const sortedNodes = [...nodes].sort((a, b) => {
    const byDegree = (degree.get(b.id) ?? 0) - (degree.get(a.id) ?? 0);
    return byDegree || stableHash(a.id) - stableHash(b.id);
  });
  const largestRadius = Math.max(2.8, ...nodes.map((node) => graphCollisionRadius(node, controls)));
  const baseSpacing = Math.max(
    largestRadius * (quality === "full" ? 1.18 : 1.35),
    controls.nodeDistance / (quality === "full" ? 96 : 70)
  );
  const goldenAngle = Math.PI * (3 - Math.sqrt(5));

  sortedNodes.forEach((node, index) => {
    const hash = stableHash(node.id);
    const angle = index * goldenAngle + ((hash % 1000) / 1000) * Math.PI * 2;
    const nodeDegree = degree.get(node.id) ?? 0;
    const outerBias = nodeDegree === 0 ? controls.nodeDistance / 12 : 0;
    const radius = Math.sqrt(index + 1) * baseSpacing + outerBias;
    positions.set(node.id, {
      x: Math.cos(angle) * radius,
      y: Math.sin(angle) * radius,
    });
  });

  const links = edges
    .map((edge) => ({
      source: edgeEndpointId(edge.source),
      target: edgeEndpointId(edge.target),
      relation: edge.relation,
    }))
    .filter(
      (edge): edge is { source: string; target: string; relation: string } =>
        Boolean(edge.source && edge.target && nodeById.has(edge.source) && nodeById.has(edge.target))
    );
  const forces = new Map(nodes.map((node) => [node.id, { x: 0, y: 0 }]));

  const iterations = quality === "full" ? 132 : 16;
  const collisionEvery = quality === "full" ? 6 : 8;
  const repelEvery = quality === "full" ? 12 : 4;
  const movementLimit = quality === "full" ? 5.2 : 1.8;

  for (let iteration = 0; iteration < iterations; iteration += 1) {
    forces.forEach((force) => {
      force.x = 0;
      force.y = 0;
    });

    links.forEach((edge) => {
      const sourcePoint = positions.get(edge.source);
      const targetPoint = positions.get(edge.target);
      const sourceNode = nodeById.get(edge.source);
      const targetNode = nodeById.get(edge.target);
      if (!sourcePoint || !targetPoint || !sourceNode || !targetNode) return;

      const dx = targetPoint.x - sourcePoint.x;
      const dy = targetPoint.y - sourcePoint.y;
      const distance = Math.max(0.001, Math.hypot(dx, dy));
      const desired =
        graphCollisionRadius(sourceNode, controls) +
        graphCollisionRadius(targetNode, controls) +
        controls.nodeDistance / (String(edge.relation).includes("chunk") ? 135 : 112);
      const pull =
        (distance - desired) *
        controls.linkForce *
        (quality === "full" ? 0.22 : 0.052) *
        (String(edge.relation).includes("chunk") ? 0.78 : 1);
      const fx = (dx / distance) * pull;
      const fy = (dy / distance) * pull;
      const sourceForce = forces.get(edge.source);
      const targetForce = forces.get(edge.target);
      if (sourceForce) {
        sourceForce.x += fx;
        sourceForce.y += fy;
      }
      if (targetForce) {
        targetForce.x -= fx;
        targetForce.y -= fy;
      }
    });

    sortedNodes.forEach((node) => {
      const point = positions.get(node.id);
      const force = forces.get(node.id);
      if (!point || !force) return;
      force.x -= point.x * controls.centerForce * (quality === "full" ? 0.12 : 0.055);
      force.y -= point.y * controls.centerForce * (quality === "full" ? 0.12 : 0.055);
      point.x += Math.max(-movementLimit, Math.min(movementLimit, force.x));
      point.y += Math.max(-movementLimit, Math.min(movementLimit, force.y));
    });

    if (iteration % collisionEvery === 0) resolveGraphOverlaps(nodes, positions, controls, 1);
    if (repelEvery && iteration % repelEvery === 0) applyGraphRepulsion(nodes, positions, controls, quality);
  }

  resolveGraphOverlaps(nodes, positions, controls, quality === "full" ? 54 : 4);
  return positions;
}

function resolveGraphOverlaps(
  nodes: GraphNode[],
  positions: Map<string, { x: number; y: number }>,
  controls: GraphControls,
  maxIterations = 120
) {
  const items = nodes
    .map((node) => ({
      id: node.id,
      radius: graphCollisionRadius(node, controls),
      point: positions.get(node.id) ?? { x: 0, y: 0 },
    }))
    .sort((a, b) => a.id.localeCompare(b.id));

  for (let iteration = 0; iteration < maxIterations; iteration += 1) {
    let moved = false;
    for (let i = 0; i < items.length; i += 1) {
      for (let j = i + 1; j < items.length; j += 1) {
        const a = items[i];
        const b = items[j];
        const minDistance = a.radius + b.radius;
        let dx = b.point.x - a.point.x;
        let dy = b.point.y - a.point.y;
        let distance = Math.hypot(dx, dy);

        if (distance >= minDistance) continue;
        if (distance < 0.001) {
          const angle = ((stableHash(`${a.id}:${b.id}`) % 360) / 180) * Math.PI;
          dx = Math.cos(angle);
          dy = Math.sin(angle);
          distance = 1;
        }

        const push = ((minDistance - distance) / 2) * 1.08;
        const ux = dx / distance;
        const uy = dy / distance;
        a.point.x -= ux * push;
        a.point.y -= uy * push;
        b.point.x += ux * push;
        b.point.y += uy * push;
        moved = true;
      }
    }
    if (!moved) break;
  }

  items.forEach((item) => positions.set(item.id, item.point));
}

function applyGraphRepulsion(
  nodes: GraphNode[],
  positions: Map<string, { x: number; y: number }>,
  controls: GraphControls,
  quality: LayoutQuality
) {
  const items = nodes
    .map((node) => ({
      id: node.id,
      radius: graphCollisionRadius(node, controls),
      point: positions.get(node.id) ?? { x: 0, y: 0 },
    }))
    .sort((a, b) => a.id.localeCompare(b.id));
  const maxDistance = controls.nodeDistance / (quality === "full" ? 10 : 8);
  const strength = controls.repelForce / (quality === "full" ? 28000 : 14000);

  for (let i = 0; i < items.length; i += 1) {
    const a = items[i];
    for (let j = i + 1; j < items.length; j += 1) {
      const b = items[j];
      let dx = b.point.x - a.point.x;
      let dy = b.point.y - a.point.y;
      let distance = Math.hypot(dx, dy);
      if (distance > maxDistance) continue;
      if (distance < 0.001) {
        const angle = ((stableHash(`${a.id}:${b.id}`) % 360) / 180) * Math.PI;
        dx = Math.cos(angle);
        dy = Math.sin(angle);
        distance = 1;
      }

      const push = ((maxDistance - distance) / maxDistance) * strength * (a.radius + b.radius);
      const ux = dx / distance;
      const uy = dy / distance;
      a.point.x -= ux * push;
      a.point.y -= uy * push;
      b.point.x += ux * push;
      b.point.y += uy * push;
    }
  }

  items.forEach((item) => positions.set(item.id, item.point));
}

function stableHash(text: string) {
  let hash = 0;
  for (let index = 0; index < text.length; index += 1) {
    hash = (hash * 31 + text.charCodeAt(index)) >>> 0;
  }
  return hash;
}

export default App;
