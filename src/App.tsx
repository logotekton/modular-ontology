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
  Trash2,
  Users,
  Waypoints,
} from "lucide-react";
import { ModelExplorerView } from "./ModelExplorer";
import { mergeLocalPacks, parseLocalPackFile } from "./localPacks";
import type { LocalEdge, LocalNode, ParsedLocalPack } from "./localPacks";
import { OntologyGraph } from "./OntologyGraph";
import type { OgController, OgDetail } from "./OntologyGraph";
import { AiChatPanel } from "./app/ai-chat/AiChatPanel";

const API_BASE = "";
const OPENAI_CHAT_MODEL = "gpt-4.1-mini";

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
  /** 답변/근거 텍스트에서 매칭된 그래프 노드 id — 하이라이트용 */
  refNodeIds?: string[];
};

/** 답변·근거 텍스트에 라벨이나 id가 등장하는 노드를 찾는다 (AI 참조 하이라이트용) */
const BOUNDARY_CLASS = "[0-9A-Za-z가-힣]";

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** label이 단어 경계로 등장하는지 — 'Wall'이 'Drywall'에 매칭되는 오탐 방지 */
function hasBoundedMatch(text: string, label: string): boolean {
  if (!text.includes(label)) return false; // 정규식 컴파일 전 저비용 프리필터
  const pattern = new RegExp(`(^|(?!${BOUNDARY_CLASS}).)${escapeRegExp(label)}($|(?!${BOUNDARY_CLASS}).)`, "s");
  return pattern.test(text);
}

function matchAnswerToNodes(text: string, nodes: GraphNode[]): string[] {
  if (!text) return [];
  const ids: string[] = [];
  for (const node of nodes) {
    const label = String(node.label ?? "");
    const idTail = node.id.split(":").slice(-2).join(":");
    const hit =
      (label.length >= 4 && hasBoundedMatch(text, label)) ||
      text.includes(node.id) ||
      (idTail.length >= 6 && text.includes(idTail));
    if (hit) {
      ids.push(node.id);
      if (ids.length >= 300) break;
    }
  }
  return ids;
}

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

async function getJson<T>(path: string, token?: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
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
  const [localGraph, setLocalGraph] = useState<GraphPayload | null>(null);
  const [localPackCount, setLocalPackCount] = useState(0);
  const [localPackStatus, setLocalPackStatus] = useState("");
  const [localDragOver, setLocalDragOver] = useState(false);
  const dragDepthRef = useRef(0);
  const localPackInputRef = useRef<HTMLInputElement | null>(null);
  const localDataRef = useRef<{ nodes: LocalNode[]; edges: LocalEdge[]; packCount: number } | null>(null);
  const [selectedNode, setSelectedNode] = useState<GraphNode | null>(null);
  const [graphDetail, setGraphDetail] = useState<OgDetail<GraphNode> | null>(null);
  const [syncing, setSyncing] = useState(false);
  const ogControllerRef = useRef<OgController | null>(null);

  const applyLocalPacks = async (files: FileList | File[]) => {
    const list = Array.from(files);
    if (!list.length) return;
    setLocalPackStatus("로컬 팩 읽는 중…");
    const parsed: ParsedLocalPack[] = [];
    const failures: string[] = [];
    for (const file of list) {
      try {
        parsed.push(await parseLocalPackFile(file));
      } catch (error) {
        failures.push(`${file.name}: ${error instanceof Error ? error.message : String(error)}`);
      }
    }
    if (!parsed.length) {
      setLocalPackStatus(failures.join(" / ") || "읽을 수 있는 파일이 없습니다");
      return;
    }
    const previous = localDataRef.current;
    const merged = mergeLocalPacks(previous ? { nodes: previous.nodes, edges: previous.edges } : null, parsed);
    const packCount = (previous?.packCount ?? 0) + parsed.length;
    localDataRef.current = { nodes: merged.nodes, edges: merged.edges, packCount };
    setLocalPackCount(packCount);
    setLocalGraph({
      pack: {
        id: "__local__",
        title: packCount === 1 ? parsed[0].title : `로컬 팩 ${packCount}개 병합`,
        filename: "local",
        source: "local",
        validationStatus: "local",
        counts: {},
      },
      nodes: merged.nodes,
      edges: merged.edges,
      stats: {
        visibleNodes: merged.nodes.length,
        visibleEdges: merged.edges.length,
        totalNodes: merged.nodes.length,
        totalEdges: merged.edges.length,
      },
    });
    setSelectedNode(null);
    const parts = [`+노드 ${merged.addedNodes.toLocaleString()}`, `+엣지 ${merged.addedEdges.toLocaleString()}`];
    if (merged.placeholders) parts.push(`미해결참조 ${merged.placeholders.toLocaleString()}`);
    if (merged.resolvedRefs) parts.push(`참조해결 ${merged.resolvedRefs.toLocaleString()}`);
    if (failures.length) parts.push(`실패 ${failures.length}건`);
    setLocalPackStatus(parts.join(" · "));
  };

  const clearLocalPacks = () => {
    localDataRef.current = null;
    setLocalGraph(null);
    setLocalPackCount(0);
    setLocalPackStatus("");
    setSelectedNode(null);
    setGraphDetail(null);
  };
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
    setGraphDetail(null);
    setAiMessages([]);
    setAiQuestion("");
    setGraph(null);
    setStatus("그래프 불러오는 중");
    const query = new URLSearchParams({
      // OntologyGraph 엔진은 차수 필터로 표시량을 관리하므로 캡을 크게 잡는다
      max_nodes: "20000",
      max_edges: "50000",
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
    const res = await fetch("/api/mcp/user-url/regenerate", {
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
    const res = await fetch("/api/auth/me", { headers: { Authorization: `Bearer ${token}` } });
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
    const res = await fetch("/api/auth/login", {
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
    const res = await fetch("/api/auth/register", {
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
    const res = await fetch("/api/admin/users", {
      headers: { Authorization: `Bearer ${authToken}` },
    });
    if (!res.ok) throw new Error(await res.text());
    const payload = (await res.json()) as { users: ManagedUser[] };
    setManagedUsers(payload.users);
  }

  async function fetchAdminCompanies() {
    if (!authToken) return;
    const res = await fetch("/api/admin/companies", {
      headers: { Authorization: `Bearer ${authToken}` },
    });
    if (!res.ok) throw new Error(await res.text());
    const payload = (await res.json()) as { companies: string[] };
    setCompanies(payload.companies);
  }

  async function fetchCompanyProjectAccess() {
    if (!authToken) return;
    const res = await fetch("/api/admin/company-project-access", {
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
    const res = await fetch(`/api/admin/users/${encodeURIComponent(email)}/approve`, {
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
    const res = await fetch(`/api/admin/users/${encodeURIComponent(email)}/role`, {
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
    const res = await fetch("/api/admin/companies", {
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
    const res = await fetch("/api/admin/companies/rename", {
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
    const res = await fetch(`/api/admin/companies/${encodeURIComponent(name)}${query}`, {
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
    const res = await fetch(`/api/admin/users/${encodeURIComponent(email)}`, {
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
    const res = await fetch(`/api/admin/users/${encodeURIComponent(email)}/company`, {
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
    const res = await fetch(`/api/admin/companies/${encodeURIComponent(company)}/projects`, {
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
    const res = await fetch(isUpdate ? `/api/admin/projects/${encodeURIComponent(form.id || "")}` : "/api/admin/projects", {
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
    const res = await fetch(`/api/admin/projects/${encodeURIComponent(projectId)}`, {
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
    const res = await fetch(`/api/admin/projects/${encodeURIComponent(projectId)}/packs`, {
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
    await fetch("/api/auth/logout", {
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
    const res = await fetch("/api/admin/ifc/models/link", {
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

  async function reindexPacks(scope: "project" | "all" = "project") {
    if (currentUser?.role !== "admin") {
      setUploadStatus("관리자 세션이 필요합니다");
      return;
    }
    if (syncing) return; // 진행 중 재클릭으로 동기화가 중첩되지 않게
    // selectedProject가 projects[0]으로 폴백하기 때문에, 명시적 scope 없이는
    // 전역 동기화가 도달 불가능한 경로였다 — "all"이면 전역 엔드포인트 강제.
    const syncProject = scope === "all" ? null : selectedProject ?? null;
    setSyncing(true);
    setUploadStatus(syncProject ? `${syncProject.name} 프로젝트 Drive 동기화 중` : "Drive 변경 파일 동기화 중");
    const endpoint = syncProject
      ? `/api/admin/storage/google-drive/projects/${encodeURIComponent(syncProject.id)}/sync`
      : "/api/admin/storage/google-drive/sync";
    try {
      const res = await fetch(endpoint, {
        method: "POST",
        headers: { Authorization: `Bearer ${authToken}` },
      });
      if (!res.ok) {
        const payload = await res.json().catch(() => null);
        const gatewayHint =
          res.status === 504 || res.status === 502
            ? " — 서버 시간제한/게이트웨이 오류입니다. 다시 동기화하면 남은 변경분만 이어서 반영됩니다."
            : "";
        setUploadStatus(payload?.detail ?? `Drive 동기화 실패 (HTTP ${res.status})${gatewayHint}`);
        return;
      }
      const payload = (await res.json()) as {
        stats?: IndexStats;
        reindexed?: IndexStats;
        downloaded?: unknown[];
        skipped?: unknown[];
        warnings?: unknown[];
        scope?: string;
        projectName?: string;
      };
      const nextStats = payload.reindexed ?? payload.stats;
      if (nextStats) setIndexStats(nextStats);
      await refreshData(undefined, authToken);
      await refreshPublicStatus(authToken);
      const changedCount = payload.downloaded?.length ?? 0;
      const skippedCount = payload.skipped?.length ?? 0;
      const warningCount = payload.warnings?.length ?? 0;
      const warningSuffix = warningCount > 0 ? ` · 경고 ${warningCount}건` : "";
      const syncLabel = payload.scope === "project" ? `${payload.projectName ?? syncProject?.name ?? "프로젝트"} 동기화` : "Drive 동기화";
      if (changedCount > 0 && skippedCount > 0) {
        setUploadStatus(`${syncLabel} 완료 · 변경 ${changedCount}개 반영 · 기존 ${skippedCount}개 유지${warningSuffix}`);
      } else if (changedCount > 0) {
        setUploadStatus(`${syncLabel} 완료 · 변경 ${changedCount}개 반영${warningSuffix}`);
      } else if (skippedCount > 0) {
        setUploadStatus(`${syncLabel} 완료 · 새 변경 없음 · 기존 ${skippedCount}개 유지${warningSuffix}`);
      } else {
        setUploadStatus(`${syncLabel} 완료 · 변경 파일 없음${warningSuffix}`);
      }
    } catch (error) {
      // 연결 끊김/터널 드롭 등 HTTP 응답조차 없는 실패 — 상태가 '동기화 중'에 멈추지 않게
      setUploadStatus(
        `동기화 연결이 끊겼습니다. 서버에서는 계속 진행 중일 수 있으니 잠시 후 다시 동기화하면 변경분만 반영됩니다. (${
          error instanceof Error ? error.message : String(error)
        })`,
      );
    } finally {
      setSyncing(false);
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
      const res = await fetch("/api/llm/openai/validate", {
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
    const rawNodePackId = String(selectedNode?.properties?.pack_id || selectedNode?.packId || "");
    // 로컬 팩 노드의 "__local__"은 서버 미등록 — 등록된 팩으로 폴백해야 질의가 실패하지 않는다
    const selectedNodePackId = rawNodePackId === "__local__" ? "" : rawNodePackId;
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
      const res = await fetch("/api/query", {
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
      // 답변+근거 텍스트를 현재 표시 그래프의 노드와 매칭 → 참조 노드 하이라이트
      const evidenceText = (payload.evidence ?? [])
        .map((item) => `${item.title ?? ""}\n${item.path ?? ""}\n${item.snippet ?? ""}`)
        .join("\n");
      const displayNodes = (localGraph ?? graph)?.nodes ?? [];
      const refNodeIds = matchAnswerToNodes(`${content}\n${evidenceText}`, displayNodes);
      // 오탐 폭주 방지: 매칭이 100개 이하일 때만 자동 적용 (버튼으로는 항상 가능)
      if (refNodeIds.length && refNodeIds.length <= 100) ogControllerRef.current?.setHighlight(refNodeIds);
      setAiMessages((messages) => [
        ...messages,
        {
          id: `${Date.now()}-assistant`,
          role: "assistant",
          content,
          evidence: payload.evidence ?? [],
          refNodeIds,
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
            onConfirm={confirmAction}
            onDeleteProject={deleteProject}
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
            selectedProjectId={selectedProject?.id ?? ""}
            syncing={syncing}
            uploadStatus={uploadStatus}
            onSelectProject={setSelectedProjectId}
            onSync={() => reindexPacks("project")}
            onSyncAll={() => reindexPacks("all")}
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
            onReindex={() => reindexPacks("all")}
            onRefreshUsers={refreshAdminDirectory}
            onSetUserRole={updateManagedUserRole}
            onSetCompanyProjectAccess={updateCompanyProjectAccess}
            onUpload={() => reindexPacks("all")}
          />
        )}

        {activeTab === "Graph Explorer" && (
        <>
        <section className="content-grid graph-explorer-grid">
          <div
            className={localDragOver ? "graph-panel local-drag" : "graph-panel"}
            onDragEnter={(event) => {
              event.preventDefault();
              dragDepthRef.current += 1;
              setLocalDragOver(true);
            }}
            onDragOver={(event) => event.preventDefault()}
            onDragLeave={() => {
              // 자식 요소 진입 때마다 발화하므로 depth 카운터로 실제 이탈만 감지
              dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
              if (dragDepthRef.current === 0) setLocalDragOver(false);
            }}
            onDrop={(event) => {
              event.preventDefault();
              dragDepthRef.current = 0;
              setLocalDragOver(false);
              if (event.dataTransfer.files.length) void applyLocalPacks(event.dataTransfer.files);
            }}
          >
            <div className="panel-header">
              <div>
                <h2>그래프 탐색기</h2>
                <span>
                  {localGraph
                    ? `${localGraph.pack.title} (세션 전용 · 서버 미등록)`
                    : selectedProject?.name ?? graph?.pack.title ?? status}
                  {!localGraph && graphPackOptions.length ? ` / ${selectedGraphPackIds.length}개 팩 표시` : ""}
                </span>
              </div>
              <div className="graph-local-actions">
                <button type="button" onClick={() => localPackInputRef.current?.click()}>
                  <FileArchive size={14} /> 로컬 팩 열기
                </button>
                {localGraph ? (
                  <button type="button" onClick={clearLocalPacks}>
                    <Trash2 size={14} /> 로컬 해제 ({localPackCount})
                  </button>
                ) : null}
                <input
                  ref={localPackInputRef}
                  type="file"
                  accept=".zip,.json,.jsonl"
                  multiple
                  hidden
                  onChange={(event) => {
                    if (event.target.files?.length) void applyLocalPacks(event.target.files);
                    event.target.value = "";
                  }}
                />
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
            {localPackStatus ? <div className="graph-local-status">{localPackStatus}</div> : null}
            {!localGraph && graphPackOptions.length ? (
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
            {localGraph ? (
              <OntologyGraph
                nodes={localGraph.nodes}
                edges={localGraph.edges}
                onSelectNode={setSelectedNode}
                onDetail={setGraphDetail}
                controllerRef={ogControllerRef}
              />
            ) : graphPackOptions.length ? (
              selectedGraphPackIds.length ? (
                graph ? (
                  <OntologyGraph
                    nodes={graph.nodes}
                    edges={graph.edges}
                    onSelectNode={setSelectedNode}
                    onDetail={setGraphDetail}
                    controllerRef={ogControllerRef}
                    serverStats={graph.stats}
                  />
                ) : (
                  <div className="graph-project-empty-state project-empty-state graph-loading-state">
                    <LoaderCircle className="loading-spinner" size={28} />
                    <strong>그래프 불러오는 중…</strong>
                    <span>대용량 팩은 수 초 걸릴 수 있습니다.</span>
                  </div>
                )
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
              <AiChatPanel
                keyReady={openAiKeyStatus === "valid"}
                loading={aiLoading}
                messages={aiMessages}
                question={aiQuestion}
                onQuestionChange={setAiQuestion}
                onSubmit={askGraphAi}
                onHighlight={(ids) => ogControllerRef.current?.setHighlight(ids)}
              />
            ) : graphDetail ? (
              <OgNodeInfo detail={graphDetail} onJump={(id) => ogControllerRef.current?.selectById(id)} />
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

/** 그래프에서 선택한 노드의 상세 — 인스펙터 [노드 정보] 탭 본문 */
function OgNodeInfo({ detail, onJump }: { detail: OgDetail<GraphNode>; onJump: (id: string) => void }) {
  const rows = Object.entries(detail.props).filter(
    ([key, value]) => !["title", "name", "label", "degree"].includes(key) && value !== null && value !== "",
  );
  const neighborTotal = detail.groups.reduce((sum, group) => sum + group.total, 0);
  return (
    <div className="og-inspector-detail">
      <div className="og-detail-name">{detail.label}</div>
      <div className="og-detail-chips">
        <span style={{ borderColor: detail.color, color: detail.color }}>{detail.type}</span>
        <span>차수 {detail.degree}</span>
      </div>
      {rows.length ? (
        <table>
          <tbody>
            {rows.slice(0, 15).map(([key, value]) => (
              <tr key={key}>
                <td>{key}</td>
                <td>{typeof value === "object" ? JSON.stringify(value) : String(value)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
      <div className="og-detail-neighbors">
        <div className="og-panel-title">이웃 {neighborTotal}</div>
        {detail.groups.map((group) => (
          <div key={group.rel} className="og-detail-group">
            <div className="og-detail-rel">
              {group.rel} ({group.total})
            </div>
            {group.items.map((item) => (
              <button key={item.id} type="button" onClick={() => onJump(item.id)}>
                <i style={{ background: item.color }} />
                <span>{item.label}</span>
              </button>
            ))}
            {group.total > group.items.length ? <div className="og-detail-more">… 외 {group.total - group.items.length}개</div> : null}
          </div>
        ))}
      </div>
    </div>
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
  ifcModels,
  packs,
  projects,
  onGoToTab,
  onOpenPack,
}: {
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
      </div>
    </section>
  );
}

function ProjectsView({
  currentUser,
  ifcModels,
  packs,
  projects,
  onConfirm,
  onDeleteProject,
  onSaveProject,
}: {
  currentUser: CurrentUser | null;
  ifcModels: IfcModel[];
  packs: Pack[];
  projects: Project[];
  selectedPackId: string;
  onConfirm: (options: ConfirmDialogOptions) => void;
  onDeleteProject: (projectId: string) => void;
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

  function confirmDeleteProject() {
    if (!selectedProject) return;
    onConfirm({
      title: "프로젝트 삭제",
      message: `${selectedProject.name} 프로젝트를 사이트 목록에서 삭제합니다. Google Drive 파일은 삭제하지 않습니다. Drive에 같은 폴더가 남아 있으면 다음 동기화 때 다시 표시될 수 있습니다.`,
      confirmLabel: "삭제",
      cancelLabel: "취소",
      tone: "danger",
      onConfirm: () => onDeleteProject(selectedProject.id),
    });
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
              <button
                aria-label="선택한 프로젝트 삭제"
                className={actionButtonClass("project-delete", "danger-action-button")}
                disabled={!selectedProject}
                title="선택한 프로젝트 삭제"
                type="button"
                onClick={() => runProjectAction("project-delete", confirmDeleteProject)}
              >
                <Trash2 size={14} />
                삭제
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
  selectedProjectId,
  syncing,
  uploadStatus,
  onSelectProject,
  onSync,
  onSyncAll,
}: {
  currentUser: CurrentUser | null;
  ifcModels: IfcModel[];
  packs: Pack[];
  projects: Project[];
  selectedPackId: string;
  selectedProjectId: string;
  syncing: boolean;
  uploadStatus: string;
  onSelectProject: (projectId: string) => void;
  onSync: () => void;
  onSyncAll: () => void;
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
            <span>{isAdmin ? "Google Drive에 올린 IFC/XKT와 팩을 변경분만 등록합니다" : "관리자 세션이 필요합니다"}</span>
          </div>
          <RefreshCw size={19} />
        </div>
        <div className="action-stack">
          <label className="select-wrap sync-project-select">
            <select
              disabled={!isAdmin || projects.length === 0}
              value={selectedProjectId}
              onChange={(event) => onSelectProject(event.target.value)}
            >
              {projects.map((project) => (
                <option key={project.id} value={project.id}>{project.name}</option>
              ))}
            </select>
            <ChevronDown size={16} />
          </label>
          <button className="primary-button block" disabled={!isAdmin || projects.length === 0 || syncing} onClick={onSync}>
            <RefreshCw size={17} />
            {syncing ? "동기화 진행 중…" : "선택 프로젝트 동기화"}
          </button>
          <button className="ghost-button block sync-all-button" disabled={!isAdmin || syncing} onClick={onSyncAll}>
            전체 Drive 동기화 (모든 프로젝트 + 재색인)
          </button>
          <span className="sync-status-text">{uploadStatus || "프로젝트의 Drive 폴더에 파일을 올린 뒤 동기화하세요"}</span>
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
              <em>{model.viewerStatus === "ready" ? "뷰어 준비" : "XKT 대기"}</em>
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
  onHighlight,
}: {
  openAiKeyStatus: AiKeyStatus;
  loading: boolean;
  messages: AiMessage[];
  question: string;
  onQuestionChange: (value: string) => void;
  onSubmit: () => void;
  onHighlight?: (ids: string[] | null) => void;
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
              {message.refNodeIds?.length && onHighlight ? (
                <div className="ai-highlight-actions">
                  <button type="button" onClick={() => onHighlight(message.refNodeIds ?? null)}>
                    🔦 그래프에서 보기 ({message.refNodeIds.length})
                  </button>
                  <button type="button" onClick={() => onHighlight(null)}>
                    해제
                  </button>
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
        {" "}또는 로컬 팩 ZIP을 이 패널에 바로 드래그하면 등록 없이 즉시 볼 수 있습니다.
      </em>
    </div>
  );
}

export default App;
