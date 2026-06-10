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
  GitBranch,
  KeyRound,
  LoaderCircle,
  LockKeyhole,
  Network,
  PackageCheck,
  PlugZap,
  RefreshCw,
  SendHorizontal,
  ServerCog,
  ShieldCheck,
  Upload,
  Users,
  Waypoints,
} from "lucide-react";
import Graph from "graphology";
import Sigma from "sigma";

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
  source: string;
  validationStatus: string;
  counts: Record<string, number>;
  modelSummary?: Record<string, unknown>;
  ingest?: { status: string; documents?: number; nodes?: number; edges?: number };
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
  projects?: number;
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

type UploadProjectTarget = {
  mode: "existing" | "new";
  projectId: string;
  name: string;
  company: string;
  manager: string;
  discipline: string;
  description: string;
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
  { label: "Ontology Packs", icon: FileArchive },
  { label: "Graph Explorer", icon: Waypoints },
  { label: "MCP Connections", icon: ServerCog },
  { label: "Admin", icon: LockKeyhole },
] as const;

type AppTab = (typeof nav)[number]["label"];

const DEFAULT_TAB: AppTab = "Dashboard";

const ROUTE_BY_TAB: Record<AppTab, string> = {
  Dashboard: "/dashboard",
  Projects: "/projects",
  "Ontology Packs": "/upload",
  "Graph Explorer": "/graph",
  "MCP Connections": "/mcp-connection",
  Admin: "/admin",
};

const TAB_BY_ROUTE = new Map<string, AppTab>(
  Object.entries(ROUTE_BY_TAB).map(([tab, route]) => [route, tab as AppTab])
);

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

const TAB_LABELS: Record<string, string> = {
  Dashboard: "대시보드",
  Projects: "프로젝트",
  "Ontology Packs": "업로드",
  "Graph Explorer": "그래프 탐색기",
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

function tabLabel(tab: string) {
  return TAB_LABELS[tab] ?? tab;
}

function nodeTypeLabel(type?: string) {
  if (!type) return "";
  return NODE_TYPE_LABELS[type] ?? type;
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
  const [uploadProjectTarget, setUploadProjectTarget] = useState<UploadProjectTarget>({
    mode: "existing",
    projectId: "",
    name: "",
    company: "Kumkang Kind",
    manager: "",
    discipline: "",
    description: "",
  });
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const selectedProject = projects.find((project) => project.id === selectedProjectId) ?? projects[0];
  const selectedPackId = selectedGraphPackIds[0] ?? selectedProject?.packIds[0] ?? "";
  const graphPackOptions = selectedProject
    ? packs.filter((pack) => selectedProject.packIds.includes(pack.id))
    : [];
  const visibleNav = nav.filter((item) => item.label !== "Admin" || currentUser?.role === "admin");

  function navigateToTab(tab: string, options: { replace?: boolean } = {}) {
    const nextTab = nav.some((item) => item.label === tab) ? (tab as AppTab) : DEFAULT_TAB;
    const nextRoute = routeForTab(nextTab);
    setActiveTab(nextTab);
    if (window.location.pathname !== nextRoute) {
      const method = options.replace ? "replaceState" : "pushState";
      window.history[method]({ tab: nextTab }, "", nextRoute);
    }
  }

  function setAllGraphPacks() {
    setSelectedGraphPackIds(graphPackOptions.map((pack) => pack.id));
  }

  function toggleGraphPack(packId: string) {
    setSelectedGraphPackIds((current) => {
      if (current.includes(packId)) {
        return current.length > 1 ? current.filter((id) => id !== packId) : current;
      }
      return [...current, packId];
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
    navigateToTab(tabFromPathname(window.location.pathname), { replace: true });
    const handlePopState = () => {
      setActiveTab(tabFromPathname(window.location.pathname));
    };
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  useEffect(() => {
    refreshPublicStatus().catch(() => undefined);
    refreshData().catch((error: Error) => setStatus(error.message));
  }, []);

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
    if (currentUser && currentUser.role !== "admin" && activeTab === "Admin") {
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
    if (!projects.length) return;
    setUploadProjectTarget((target) => {
      if (target.mode === "existing" && projects.some((project) => project.id === target.projectId)) {
        return target;
      }
      return { ...target, mode: "existing", projectId: projects[0].id };
    });
  }, [projects]);

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
      return valid.length ? valid : project.packIds;
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
    await Promise.all([
      getJson<IndexStats>("/api/index/status").then(setIndexStats),
      getJson<McpStatus>("/api/mcp/status", token || undefined).then(setMcpStatus),
    ]);
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
      setSelectedGraphPackIds(nextPackId && nextProject.packIds.includes(nextPackId) ? [nextPackId] : nextProject.packIds);
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
    setAuthToken(payload.token);
    setCurrentUser(payload.user);
    await refreshData(undefined, payload.token);
    setLoginPassword("");
    setShowSignup(false);
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

  async function saveProject(form: ProjectForm) {
    if (currentUser?.role !== "admin") {
      setUploadStatus("관리자 세션이 필요합니다");
      return;
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
      return;
    }
    const payload = (await res.json()) as { project: Project };
    await refreshData(undefined, authToken);
    setUploadStatus(`${payload.project.name} 프로젝트를 저장했습니다.`);
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
    await refreshData(undefined, "");
    setUploadStatus("로그아웃됨");
  }

  async function uploadPack(file: File | undefined) {
    if (!file) return;
    if (currentUser?.role !== "admin") {
      setUploadStatus("관리자 세션이 필요합니다");
      return;
    }
    setUploadStatus("업로드 중");
    const formData = new FormData();
    formData.append("file", file);
    formData.append("project_mode", uploadProjectTarget.mode);
    if (uploadProjectTarget.mode === "new") {
      if (uploadProjectTarget.name.trim()) {
        formData.append("new_project_name", uploadProjectTarget.name.trim());
      }
      formData.append("new_project_company", uploadProjectTarget.company.trim());
      formData.append("new_project_manager", uploadProjectTarget.manager.trim());
      formData.append("new_project_discipline", uploadProjectTarget.discipline.trim());
      formData.append("new_project_description", uploadProjectTarget.description.trim());
    } else if (uploadProjectTarget.projectId) {
      formData.append("project_id", uploadProjectTarget.projectId);
    }
    const res = await fetch("/api/packs/upload", {
      method: "POST",
      headers: { Authorization: `Bearer ${authToken}` },
      body: formData,
    });
    if (!res.ok) {
      setUploadStatus("업로드 실패");
      return;
    }
    const uploaded = (await res.json()) as Pack;
    await refreshData(uploaded.id);
    setUploadStatus(
      uploaded.ingest?.status === "indexed"
        ? `${numberLabel(uploaded.ingest.documents)}개 문서 / ${numberLabel(uploaded.ingest.nodes)}개 노드 색인됨`
        : "수집 완료"
    );
  }

  async function uploadIfc(file: File | undefined, projectId: string) {
    if (!file) return;
    if (currentUser?.role !== "admin") {
      setUploadStatus("관리자 세션이 필요합니다");
      return;
    }
    setUploadStatus("IFC 업로드 중");
    const formData = new FormData();
    formData.append("file", file);
    if (projectId) formData.append("project_id", projectId);
    const res = await fetch("/api/ifc/upload", {
      method: "POST",
      headers: { Authorization: `Bearer ${authToken}` },
      body: formData,
    });
    if (!res.ok) {
      const payload = await res.json().catch(() => null);
      setUploadStatus(payload?.detail ?? "IFC 업로드 실패");
      return;
    }
    const payload = (await res.json()) as { filename: string; sizeBytes: number; projectName?: string | null };
    await refreshData(undefined, authToken);
    setUploadStatus(`${payload.projectName || "프로젝트"} IFC 저장 완료: ${payload.filename}`);
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

  async function reindexPacks() {
    if (currentUser?.role !== "admin") {
      setUploadStatus("관리자 세션이 필요합니다");
      return;
    }
    setUploadStatus("색인 중");
    const res = await fetch("/api/admin/reindex", {
      method: "POST",
      headers: { Authorization: `Bearer ${authToken}` },
    });
    if (!res.ok) {
      setUploadStatus("색인 실패");
      return;
    }
    const payload = (await res.json()) as { stats: IndexStats };
    setIndexStats(payload.stats);
    setUploadStatus("색인 준비 완료");
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
    <div className="app-shell">
      <aside className={activeTab === "Graph Explorer" ? "sidebar graph-sidebar-active" : "sidebar"}>
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
                className={activeTab === item.label ? "nav-item active" : "nav-item"}
                key={item.label}
                onClick={() => navigateToTab(item.label)}
                type="button"
              >
                <Icon size={18} />
                {tabLabel(item.label)}
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

      <main className={activeTab === "Graph Explorer" ? "workspace graph-workspace" : "workspace"}>
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
            <input
              ref={fileInputRef}
              className="file-input"
              type="file"
              accept=".zip"
              onChange={(event) => uploadPack(event.currentTarget.files?.[0])}
            />
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
        {activeTab !== "Graph Explorer" && activeTab !== "MCP Connections" && (
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
            onSetIfcModelProject={setIfcModelProject}
            onSetProjectPacks={setProjectPacks}
          />
        )}

        {activeTab === "Ontology Packs" && (
          <OntologyPacksView
            currentUser={currentUser}
            packs={packs}
            projects={projects}
            selectedPackId={selectedPackId}
            uploadProjectTarget={uploadProjectTarget}
            onReindex={reindexPacks}
            onUploadIfc={uploadIfc}
            onUploadProjectTargetChange={setUploadProjectTarget}
            onUpload={() => fileInputRef.current?.click()}
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
            indexStats={indexStats}
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
            onUpload={() => fileInputRef.current?.click()}
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
                <button type="button" onClick={setAllGraphPacks}>
                  전체 팩
                </button>
                <div>
                  {graphPackOptions.map((pack) => (
                    <label className={selectedGraphPackIds.includes(pack.id) ? "active" : ""} key={pack.id}>
                      <input
                        checked={selectedGraphPackIds.includes(pack.id)}
                        type="checkbox"
                        onChange={() => toggleGraphPack(pack.id)}
                      />
                      <span>{pack.title}</span>
                    </label>
                  ))}
                </div>
              </div>
            ) : null}
            <GraphCanvas graph={graph} selectedNode={selectedNode} onSelectNode={setSelectedNode} />
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
            <span><strong>{numberLabel(indexStats?.projects)}</strong>전체 프로젝트</span>
            <span><strong>{numberLabel(indexStats?.packs)}</strong>전체 온톨로지 팩</span>
            <span><strong>{numberLabel(indexStats?.nodes)}</strong>전체 색인 노드</span>
            <span><strong>{numberLabel(indexStats?.edges)}</strong>전체 관계 엣지</span>
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
      <Metric icon={FileArchive} label="온톨로지 팩" value={numberLabel(packs.length)} tone="green" />
    </section>
  );
}

function DashboardView({
  packs,
  projects,
  onGoToTab,
  onOpenPack,
}: {
  packs: Pack[];
  projects: Project[];
  onGoToTab: (tab: string) => void;
  onOpenPack: (packId: string) => void;
}) {
  const visiblePackIds = new Set(projects.flatMap((project) => project.packIds));
  const scopedPacks = packs.filter((pack) => visiblePackIds.size === 0 || visiblePackIds.has(pack.id));
  const scopedStats = scopedPacks.reduce(
    (stats, pack) => ({
      documents: stats.documents + (pack.counts.documents ?? 0),
      nodes: stats.nodes + (pack.counts.nodes ?? 0),
      edges: stats.edges + (pack.counts.edges ?? 0),
    }),
    { documents: 0, nodes: 0, edges: 0 }
  );
  const scopedCompanyCount = new Set(projects.map((project) => project.company).filter(Boolean)).size;

  return (
    <section className="dashboard-grid">
      <div className="overview-panel wide">
        <div className="panel-header slim">
          <div>
            <h2>운영 현황</h2>
            <span>현재 계정에 할당된 프로젝트와 온톨로지 팩 기준</span>
          </div>
          <Activity size={19} />
        </div>
        <div className="status-grid">
          <StatusTile icon={FolderKanban} label="할당 프로젝트" value={numberLabel(projects.length)} />
          <StatusTile icon={FileArchive} label="할당 팩" value={numberLabel(scopedPacks.length)} />
          <StatusTile icon={FileArchive} label="문서" value={numberLabel(scopedStats.documents)} />
          <StatusTile icon={Waypoints} label="관계 엣지" value={numberLabel(scopedStats.edges)} />
        </div>
      </div>

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

      <div className="overview-panel">
        <div className="panel-header slim">
          <div>
            <h2>최근 팩</h2>
            <span>그래프 워크스페이스 열기</span>
          </div>
          <FileArchive size={19} />
        </div>
        <div className="compact-list">
          {packs.slice(0, 4).map((pack) => (
            <button className="compact-row" key={pack.id} onClick={() => onOpenPack(pack.id)}>
              <strong>{pack.title}</strong>
              <span>{numberLabel(pack.counts.nodes)}개 노드 / {numberLabel(pack.counts.edges)}개 엣지</span>
            </button>
          ))}
        </div>

      </div>

      <div className="overview-panel wide">
        <div className="panel-header slim">
          <div>
            <h2>할당 범위</h2>
            <span>회사, 프로젝트, 팩 단위로 권한이 제한됩니다</span>
          </div>
          <ShieldCheck size={19} />
        </div>
        <div className="status-grid">
          <StatusTile icon={Building2} label="회사" value={numberLabel(scopedCompanyCount)} />
          <StatusTile icon={FolderKanban} label="프로젝트" value={numberLabel(projects.length)} />
          <StatusTile icon={FileArchive} label="팩" value={numberLabel(scopedPacks.length)} />
          <StatusTile icon={Waypoints} label="노드" value={numberLabel(scopedStats.nodes)} />
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
  onSetIfcModelProject,
  onSetProjectPacks,
}: {
  currentUser: CurrentUser | null;
  ifcModels: IfcModel[];
  packs: Pack[];
  projects: Project[];
  selectedPackId: string;
  onConfirm: (options: ConfirmDialogOptions) => void;
  onDeleteProject: (projectId: string) => void;
  onSaveProject: (form: ProjectForm) => Promise<void> | void;
  onSetIfcModelProject: (modelId: string, projectId: string | null) => Promise<void> | void;
  onSetProjectPacks: (projectId: string, packIds: string[]) => Promise<void> | void;
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
  const [draft, setDraft] = useState<ProjectForm>(emptyForm);
  const [dialogDraft, setDialogDraft] = useState<ProjectForm>(emptyForm);
  const [dialogMode, setDialogMode] = useState<"add" | "edit" | null>(null);
  const [ifcDraftLinks, setIfcDraftLinks] = useState<Record<string, string | null>>({});
  const [pressedAction, setPressedAction] = useState("");

  useEffect(() => {
    if (!projects.length) {
      setSelectedProjectId("");
      setDraft(emptyForm);
      return;
    }
    if (!selectedProjectId || !projects.some((project) => project.id === selectedProjectId)) {
      setSelectedProjectId(projects[0].id);
    }
  }, [projects, selectedProjectId]);

  useEffect(() => {
    if (!selectedProject) {
      setDraft(emptyForm);
      return;
    }
    setDraft({
      id: selectedProject.id,
      name: selectedProject.name,
      company: selectedProject.company || "",
      manager: selectedProject.manager || "",
      discipline: selectedProject.discipline || "",
      description: selectedProject.description || "",
      packIds: selectedProject.packIds,
    });
  }, [selectedProject?.id, selectedProject?.packIds.join("|")]);

  useEffect(() => {
    const nextLinks: Record<string, string | null> = {};
    ifcModels.forEach((model) => {
      nextLinks[model.id] = model.projectId ?? null;
    });
    setIfcDraftLinks(nextLinks);
  }, [ifcModels]);

  const linkedPacks = selectedProject
    ? selectedProject.packIds
        .map((packId) => packs.find((pack) => pack.id === packId))
        .filter((pack): pack is Pack => Boolean(pack))
    : [];
  const linkedModels = selectedProject
    ? ifcModels.filter((model) => (ifcDraftLinks[model.id] ?? model.projectId ?? null) === selectedProject.id)
    : [];
  const modelLinkChanged = ifcModels.some((model) => (ifcDraftLinks[model.id] ?? null) !== (model.projectId ?? null));
  const packLinkChanged = selectedProject
    ? draft.packIds.slice().sort().join("|") !== selectedProject.packIds.slice().sort().join("|")
    : false;
  const connectionDirty = modelLinkChanged || packLinkChanged;

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

  function openAddDialog() {
    setDialogDraft(emptyForm);
    setDialogMode("add");
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

  function toggleDraftPack(packId: string) {
    if (!selectedProject) return;
    setDraft((current) => ({
      ...current,
      packIds: current.packIds.includes(packId)
        ? current.packIds.filter((id) => id !== packId)
        : [...current.packIds, packId],
    }));
  }

  function toggleDraftIfc(modelId: string) {
    if (!selectedProject) return;
    setIfcDraftLinks((current) => {
      const linkedToSelected = (current[modelId] ?? null) === selectedProject.id;
      return { ...current, [modelId]: linkedToSelected ? null : selectedProject.id };
    });
  }

  async function saveDialogDraft() {
    await Promise.resolve(onSaveProject(dialogDraft));
    setDialogMode(null);
  }

  function deleteSelectedProject() {
    if (!selectedProject) return;
    onConfirm({
      title: "프로젝트 삭제",
      message: `${selectedProject.name} 프로젝트를 삭제하시겠습니까? 연결된 팩과 IFC 모델 원본은 삭제되지 않습니다.`,
      confirmLabel: "삭제",
      tone: "danger",
      onConfirm: () => onDeleteProject(selectedProject.id),
    });
  }

  async function saveConnections() {
    if (!selectedProject) return;
    if (!connectionDirty) return;
    await Promise.resolve(onSetProjectPacks(selectedProject.id, draft.packIds));
    const changedModels = ifcModels.filter((model) => (ifcDraftLinks[model.id] ?? null) !== (model.projectId ?? null));
    await Promise.all(
      changedModels.map((model) => Promise.resolve(onSetIfcModelProject(model.id, ifcDraftLinks[model.id] ?? null))),
    );
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
                className={actionButtonClass("project-add")}
                type="button"
                onClick={() => runProjectAction("project-add", openAddDialog)}
              >
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
                className={actionButtonClass("project-delete")}
                disabled={!selectedProject}
                type="button"
                onClick={() => runProjectAction("project-delete", deleteSelectedProject)}
              >
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
                <em>{projectModels.length}개 IFC / {projectPacks.length}개 온톨로지 팩</em>
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
                <dd>{numberLabel(linkedPacks.length)}개</dd>
              </div>
            </dl>
          </div>

          <div className="project-linked-summary">
            <div className="linked-summary-columns">
              <div>
                <strong>IFC 모델</strong>
                <div className="mini-list">
                  {linkedModels.length ? linkedModels.map((model) => (
                    <span key={model.id}>{model.filename}</span>
                  )) : <em>연결된 IFC 모델 없음</em>}
                </div>
              </div>
              <div>
                <strong>온톨로지 팩</strong>
                <div className="mini-list">
                  {linkedPacks.length ? linkedPacks.map((pack) => (
                    <span key={pack.id}>{pack.title}</span>
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
            <h2>모델 / 온톨로지 팩 연결 편집</h2>
            <span>업로드된 모든 IFC 모델과 온톨로지 팩을 프로젝트에 연결합니다</span>
          </div>
          {isAdmin && (
            <button
              className={actionButtonClass("connections-save")}
              disabled={!selectedProject}
              type="button"
              onClick={() => runProjectAction("connections-save", saveConnections)}
            >
              연결 저장
            </button>
          )}
        </div>
        <div className="project-connection-grid">
          <div className="connection-list-panel">
            <div className="connection-list-title">
              <strong>IFC 모델</strong>
              <span>{numberLabel(ifcModels.length)}개 업로드됨</span>
            </div>
            <div className="connection-scroll-list">
              {ifcModels.length ? ifcModels.map((model) => {
                const linked = Boolean(selectedProject && (ifcDraftLinks[model.id] ?? null) === selectedProject.id);
                return (
                  <div className={linked ? "connection-row active" : "connection-row"} key={model.id}>
                    <Database size={18} />
                    <span>
                      <strong>{model.filename}</strong>
                      <em>{model.projectName ? `현재 연결: ${model.projectName}` : "미연결"}</em>
                    </span>
                    <input
                      checked={linked}
                      disabled={!isAdmin || !selectedProject}
                      type="checkbox"
                      onChange={() => toggleDraftIfc(model.id)}
                    />
                  </div>
                );
              }) : <p className="empty-list-note">업로드된 IFC 모델이 없습니다.</p>}
            </div>
          </div>

          <div className="connection-list-panel">
            <div className="connection-list-title">
              <strong>온톨로지 팩</strong>
              <span>{numberLabel(packs.length)}개 업로드됨</span>
            </div>
            <div className="connection-scroll-list">
              {packs.length ? packs.map((pack) => {
                const linked = draft.packIds.includes(pack.id);
                return (
                  <div className={linked ? "connection-row active" : "connection-row"} key={pack.id}>
                    <FileArchive size={18} />
                    <span>
                      <strong>{pack.title}</strong>
                      <em>{pack.filename} / {numberLabel(pack.counts.edges)}개 엣지</em>
                    </span>
                    <input
                      checked={linked}
                      disabled={!isAdmin || !selectedProject}
                      type="checkbox"
                      onChange={() => toggleDraftPack(pack.id)}
                    />
                  </div>
                );
              }) : <p className="empty-list-note">업로드된 온톨로지 팩이 없습니다.</p>}
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
              <h2>{dialogMode === "add" ? "프로젝트 추가" : "프로젝트 편집"}</h2>
              <span>프로젝트 기본 정보만 편집합니다</span>
            </div>
            <FolderKanban size={19} />
          </div>
          <div className="project-form-grid">
            <label>
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
              disabled={!dialogDraft.name.trim()}
              type="button"
              onClick={() => runProjectAction("project-save", saveDialogDraft)}
            >
              프로젝트 저장
            </button>
          </div>
        </section>
      </div>,
      document.body,
    )}
    </>
  );
}

function OntologyPacksView({
  currentUser,
  packs,
  projects,
  selectedPackId,
  uploadProjectTarget,
  onReindex,
  onUploadIfc,
  onUploadProjectTargetChange,
  onUpload,
}: {
  currentUser: CurrentUser | null;
  packs: Pack[];
  projects: Project[];
  selectedPackId: string;
  uploadProjectTarget: UploadProjectTarget;
  onReindex: () => void;
  onUploadIfc: (file: File | undefined, projectId: string) => void;
  onUploadProjectTargetChange: (target: UploadProjectTarget) => void;
  onUpload: () => void;
}) {
  const isAdmin = currentUser?.role === "admin";
  const ifcInputRef = useRef<HTMLInputElement | null>(null);
  const [ifcProjectId, setIfcProjectId] = useState(projects[0]?.id ?? "");
  const projectByPackId = new Map<string, string>();
  projects.forEach((project) => {
    project.packIds.forEach((packId) => projectByPackId.set(packId, project.name));
  });
  const ifcProject = projects.find((project) => project.id === ifcProjectId) ?? projects[0];

  useEffect(() => {
    if (!projects.length) {
      setIfcProjectId("");
      return;
    }
    setIfcProjectId((current) => (current && projects.some((project) => project.id === current) ? current : projects[0].id));
  }, [projects]);

  return (
    <section className="management-grid">
      <div className="overview-panel ifc-upload-panel">
        <div className="panel-header slim">
          <div>
            <h2>IFC 업로드</h2>
            <span>{isAdmin ? "프로젝트 기준 IFC 원본 저장" : "관리자 세션이 필요합니다"}</span>
          </div>
          <Database size={19} />
        </div>
        <div className="action-stack">
          <label className="ifc-upload-target">
            <span>대상 프로젝트</span>
            <select
              disabled={!isAdmin}
              value={ifcProjectId}
              onChange={(event) => setIfcProjectId(event.target.value)}
            >
              {projects.map((project) => (
                <option key={project.id} value={project.id}>{project.name}</option>
              ))}
            </select>
          </label>
          <input
            ref={ifcInputRef}
            className="file-input"
            type="file"
            accept=".ifc,.ifczip,.zip"
            onChange={(event) => {
              onUploadIfc(event.currentTarget.files?.[0], ifcProjectId);
              event.currentTarget.value = "";
            }}
          />
          <button className="primary-button block" disabled={!isAdmin || !ifcProjectId} onClick={() => ifcInputRef.current?.click()}>
            <Upload size={17} />
            IFC 파일 업로드
          </button>
        </div>
      </div>

      <div className="overview-panel wide ifc-staging-panel">
        <div className="panel-header slim">
          <div>
            <h2>IFC 모델</h2>
            <span>뷰어 연결 전 원본 파일을 프로젝트에 매핑합니다</span>
          </div>
          <FileArchive size={19} />
        </div>
        <div className="ifc-staging-body">
          <div>
            <strong>{ifcProject?.name ?? "프로젝트를 선택하세요"}</strong>
            <span>{ifcProject ? `${ifcProject.packIds.length}개 온톨로지 팩과 함께 관리` : "IFC 업로드 대상 없음"}</span>
          </div>
          <div className="ifc-format-list">
            <span>.ifc</span>
            <span>.ifczip</span>
            <span>.zip</span>
          </div>
          <p>IFC 뷰어가 연결되면 이 영역에서 모델 미리보기와 그래프 노드 하이라이트를 함께 제공합니다.</p>
        </div>
      </div>

      <div className="overview-panel ontology-upload-panel">
        <div className="panel-header slim">
          <div>
            <h2>온톨로지 ZIP 업로드</h2>
            <span>{isAdmin ? "팩 생성 및 재색인" : "관리자 세션이 필요합니다"}</span>
          </div>
          <PackageCheck size={19} />
        </div>
        <div className="action-stack">
          <div className="upload-project-target">
            <div className="segmented-control">
              <button
                className={uploadProjectTarget.mode === "existing" ? "active" : ""}
                type="button"
                onClick={() => onUploadProjectTargetChange({ ...uploadProjectTarget, mode: "existing" })}
              >
                기존 프로젝트
              </button>
              <button
                className={uploadProjectTarget.mode === "new" ? "active" : ""}
                type="button"
                onClick={() => onUploadProjectTargetChange({ ...uploadProjectTarget, mode: "new" })}
              >
                새 프로젝트
              </button>
            </div>
            {uploadProjectTarget.mode === "existing" ? (
              <select
                disabled={!isAdmin}
                value={uploadProjectTarget.projectId}
                onChange={(event) => onUploadProjectTargetChange({ ...uploadProjectTarget, projectId: event.target.value })}
              >
                {projects.map((project) => (
                  <option key={project.id} value={project.id}>{project.name}</option>
                ))}
              </select>
            ) : (
              <div className="upload-project-form">
                <input
                  disabled={!isAdmin}
                  placeholder="새 프로젝트명"
                  value={uploadProjectTarget.name}
                  onChange={(event) => onUploadProjectTargetChange({ ...uploadProjectTarget, name: event.target.value })}
                />
                <input
                  disabled={!isAdmin}
                  placeholder="회사"
                  value={uploadProjectTarget.company}
                  onChange={(event) => onUploadProjectTargetChange({ ...uploadProjectTarget, company: event.target.value })}
                />
                <input
                  disabled={!isAdmin}
                  placeholder="분야"
                  value={uploadProjectTarget.discipline}
                  onChange={(event) => onUploadProjectTargetChange({ ...uploadProjectTarget, discipline: event.target.value })}
                />
              </div>
            )}
          </div>
          <button className="primary-button block" disabled={!isAdmin} onClick={onUpload}>
            <Upload size={17} />
            ZIP 팩 업로드
          </button>
          <button className="secondary-button" disabled={!isAdmin} onClick={onReindex}>
            <RefreshCw size={17} />
            색인 재생성
          </button>
        </div>
      </div>

      <div className="overview-panel wide ontology-pack-list-panel">
        <div className="panel-header slim">
          <div>
            <h2>온톨로지 팩</h2>
            <span>로컬 ZIP 팩 {packs.length}개 발견</span>
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
  indexStats,
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
  indexStats: IndexStats | null;
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
  const unknownCompany = "미지정";
  const selectedProjectIds = companyProjectAccess[selectedCompany] ?? [];
  const selectedCompanyIsInternal =
    selectedCompany !== "all" &&
    (selectedCompany.toLowerCase().includes("kumkang") || selectedCompany.includes("금강"));
  const companyNames = Array.from(new Set(companies)).sort();
  const visibleUsers = selectedCompany === "all" ? users : users.filter((user) => (user.company || "미지정") === selectedCompany);

  useEffect(() => {
    setCompanyDraft(selectedCompany === "all" ? "" : selectedCompany);
  }, [selectedCompany]);

  useEffect(() => {
    if (selectedUserEmail && !users.some((user) => user.email === selectedUserEmail)) {
      setSelectedUserEmail("");
    }
  }, [selectedUserEmail, users]);

  function companyStats(company: string) {
    const companyUsers = company === "all" ? users : users.filter((user) => (user.company || "미지정") === company);
    return {
      total: companyUsers.length,
      pending: companyUsers.filter((user) => user.status === "pending").length,
      admins: companyUsers.filter((user) => user.role === "admin").length,
    };
  }

  function addCompany() {
    const name = companyDraft.trim();
    if (!name) return;
    onAddCompany(name);
    setCompanyDraft("");
  }

  function renameCompany() {
    if (selectedCompany === "all" || !companyDraft.trim()) return;
    onRenameCompany(selectedCompany, companyDraft);
    setSelectedCompany(companyDraft.trim());
  }

  function deleteCompany() {
    if (selectedCompany === "all") return;
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
    if (company === "all") return;
    const email = event.dataTransfer.getData("text/plain");
    if (email) {
      onMoveUserCompany(email, company);
      setSelectedUserEmail(email);
      setSelectedCompany(company);
    }
  }

  function selectCompanyCard(company: string) {
    setSelectedCompany(company);
  }

  function selectUserCard(user: ManagedUser) {
    setSelectedUserEmail(user.email);
    if (user.company) setSelectedCompany(user.company);
  }

  function toggleCompanyProject(projectId: string) {
    if (selectedCompany === "all" || selectedCompanyIsInternal) return;
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
            <Upload size={17} />
            팩 업로드
          </button>
          <button className="secondary-button" onClick={onReindex}>
            <RefreshCw size={17} />
            저장소 재색인
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
            <input
              value={companyDraft}
              placeholder={selectedCompany === "all" ? "새 회사명" : "회사명"}
              onChange={(event) => setCompanyDraft(event.target.value)}
            />
            <button type="button" onClick={addCompany}>회사 추가</button>
            <button type="button" disabled={selectedCompany === "all"} onClick={renameCompany}>회사명 수정</button>
            <button type="button" disabled={selectedCompany === "all"} onClick={deleteCompany}>회사 삭제</button>
            <button className="icon-action" type="button" onClick={onRefreshUsers} title="새로고침">
              <RefreshCw size={17} />
            </button>
          </div>
        </div>
        <div className="company-admin-grid">
          <div className="company-list">
            {["all", ...companyNames].map((company) => {
              const stats = companyStats(company);
              return (
                <button
                  className={selectedCompany === company ? "company-card active" : "company-card"}
                  key={company}
                  type="button"
                  onDragOver={(event) => {
                    if (company !== "all") event.preventDefault();
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
                  <span><span className="field-label">회사</span>{user.company} · {user.email}</span>
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
                : selectedCompanyIsInternal
                  ? "금강 계열 회사는 모든 프로젝트에 자동 접근합니다."
                  : "고객사/발주처는 관리자가 지정한 프로젝트만 볼 수 있습니다."}
            </span>
          </div>
          {selectedCompany !== "all" && !selectedCompanyIsInternal && (
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
            <h2>색인 저장소</h2>
            <span>SQLite 기반 문서, 노드, 엣지 색인</span>
          </div>
          <Database size={19} />
        </div>
        <div className="status-grid">
          <StatusTile icon={FileArchive} label="팩" value={numberLabel(indexStats?.packs)} />
          <StatusTile icon={Database} label="문서" value={numberLabel(indexStats?.documents)} />
          <StatusTile icon={Network} label="노드" value={numberLabel(indexStats?.nodes)} />
          <StatusTile icon={GitBranch} label="엣지" value={numberLabel(indexStats?.edges)} />
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
          <span><CheckCircle2 size={16} /> 관리자: 회원 승인, 권한 변경, 팩 업로드, 재색인</span>
          <span><CheckCircle2 size={16} /> 멤버: 프로젝트와 그래프 탐색, MCP 연결 정보 확인</span>
          <span><CheckCircle2 size={16} /> 승인 대기: 로그인 차단</span>
        </div>
      </div>
    </section>
  );
}

function StatusTile({
  icon: Icon,
  label,
  value,
}: {
  icon: typeof Activity;
  label: string;
  value: string;
}) {
  return (
    <div className="status-tile">
      <Icon size={18} />
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
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
