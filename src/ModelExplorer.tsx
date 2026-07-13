import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type MouseEvent as ReactMouseEvent } from "react";
import {
  Box,
  Boxes,
  Building2,
  Camera,
  Check,
  ChevronDown,
  ChevronRight,
  Copy,
  Database,
  Download,
  Eye,
  EyeOff,
  FolderTree,
  Focus,
  Glasses,
  Layers,
  LoaderCircle,
  MapPin,
  Minus,
  MousePointer2,
  Paintbrush,
  RotateCcw,
  Ruler,
  Scissors,
  Search,
  X,
} from "lucide-react";
import { getJson, isAbortError, request } from "./api/http";

type Project = {
  id: string;
  name: string;
  discipline: string;
  packIds: string[];
};

type IfcModel = {
  id: string;
  filename: string;
  projectId?: string | null;
  projectName?: string | null;
  sizeBytes?: number | null;
  storage?: string;
  localPath?: string;
  viewerStatus?: string | null;
  xktPath?: string | null;
  xktError?: string | null;
};

type ModelManifest = {
  modelId: string;
  filename: string;
  status: "ready" | "pending-xkt" | "missing-file" | "error";
  xktUrl?: string | null;
  error?: string | null;
};

type ViewerLoadStage = "idle" | "manifest" | "downloading" | "parsing" | "ready" | "error";

type SelectedObject = {
  objectId: string;
  label?: string;
  type?: string;
  source?: string;
  properties?: Record<string, unknown>;
};

type ViewerCommand = {
  seq: number;
  type:
    | "select"
    | "fitSelected"
    | "resetCamera"
    | "toggleSectionBox"
    | "clearSections"
    | "showAll"
    | "clearSelection"
    | "hideSelected"
    | "isolateSelected"
    | "colorSelected"
    | "isolateIds"
    | "colorIds"
    | "clearOverrides"
    | "toggleMeasure"
    | "clearMeasurements"
    | "screenshot";
  payload?: Record<string, unknown>;
};

type ModelTreeNode = {
  id: string;
  label: string;
  type: string;
  childCount: number;
  selectable: boolean;
  children: ModelTreeNode[];
  objectIds?: string[];
  syntheticGroup?: boolean;
  properties?: Record<string, unknown>;
};

type PropertyFilter = {
  key: string;
  value: string;
};

type FilterOption = {
  label: string;
  value: string;
  count: number;
  objectIds: string[];
};

const MODEL_FILTER_KEYS = [
  { key: "category", label: "category" },
  { key: "type", label: "type" },
  { key: "storey", label: "storey" },
  { key: "module_id", label: "module_id" },
  { key: "family", label: "family" },
];

function displayValue(value: unknown) {
  if (value == null || value === "") return "-";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export function ModelExplorerView({
  authToken,
  ifcModels,
  projects,
  selectedProjectId,
}: {
  authToken: string;
  ifcModels: IfcModel[];
  projects: Project[];
  selectedProjectId: string;
}) {
  const project = useMemo(
    () => projects.find((item) => item.id === selectedProjectId) ?? projects[0],
    [projects, selectedProjectId],
  );
  const projectModels = useMemo(
    () => ifcModels.filter((model) => !project?.id || model.projectId === project.id),
    [ifcModels, project?.id],
  );
  const visibleModels = useMemo(() => (project ? projectModels : []), [project, projectModels]);
  const [selectedModelId, setSelectedModelId] = useState("");
  const [manifest, setManifest] = useState<ModelManifest | null>(null);
  const [manifestError, setManifestError] = useState("");
  const [modelTree, setModelTree] = useState<ModelTreeNode[]>([]);
  const [expandedTreeIds, setExpandedTreeIds] = useState<Set<string>>(new Set());
  const [treeSearch, setTreeSearch] = useState("");
  const [selectedObject, setSelectedObject] = useState<SelectedObject | null>(null);
  const [selectedTreeNodeIds, setSelectedTreeNodeIds] = useState<Set<string>>(new Set());
  const [propertyFilter, setPropertyFilter] = useState<PropertyFilter>({ key: "category", value: "" });
  const [viewerCommand, setViewerCommand] = useState<ViewerCommand | null>(null);
  const [viewerError, setViewerError] = useState("");
  const [viewerLoadStage, setViewerLoadStage] = useState<ViewerLoadStage>("idle");
  const objectDetailControllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    setSelectedModelId((current) => {
      if (current && visibleModels.some((model) => model.id === current)) return current;
      return visibleModels[0]?.id ?? "";
    });
  }, [visibleModels]);

  useEffect(() => {
    setManifest(null);
    setModelTree([]);
    setExpandedTreeIds(new Set());
    setSelectedObject(null);
    setSelectedTreeNodeIds(new Set());
    setPropertyFilter((current) => ({ ...current, value: "" }));
    setViewerError("");
    setViewerLoadStage(selectedModelId ? "manifest" : "idle");
  }, [selectedModelId]);

  useEffect(() => {
    if (!selectedModelId) {
      setManifest(null);
      setManifestError("");
      setViewerLoadStage("idle");
      return;
    }
    const controller = new AbortController();
    setManifestError("");
    getJson<ModelManifest>(`/api/ifc/model-viewer/manifest?model_id=${encodeURIComponent(selectedModelId)}`, {
      token: authToken,
      signal: controller.signal,
    })
      .then((payload) => {
        if (!controller.signal.aborted) {
          setManifest(payload);
          setViewerLoadStage(payload.xktUrl ? "downloading" : "idle");
        }
      })
      .catch((error) => {
        if (!controller.signal.aborted && !isAbortError(error)) {
          setManifest(null);
          setManifestError(error instanceof Error ? error.message : "Model manifest load failed");
          setViewerLoadStage("error");
        }
      });
    return () => {
      controller.abort();
    };
  }, [authToken, selectedModelId]);

  const filteredTree = useMemo(() => filterModelTree(modelTree, treeSearch), [modelTree, treeSearch]);
  const filterOptionsByKey = useMemo(() => buildModelFilterOptions(modelTree), [modelTree]);
  const activeFilterOptions = filterOptionsByKey[propertyFilter.key] ?? [];
  const selectedFilterOption = activeFilterOptions.find((option) => option.value === propertyFilter.value) ?? null;
  const forceExpandedTree = Boolean(treeSearch.trim());
  const viewerWindowRef = useRef<HTMLDivElement | null>(null);
  const modelTreeListRef = useRef<HTMLDivElement | null>(null);
  const modelTreeParentIndexRef = useRef<Map<string, string>>(new Map());
  const modelTreeNodeIndex = useMemo(() => buildModelTreeNodeIndex(modelTree), [modelTree]);
  const selectedTreeNodes = useMemo(
    () =>
      [...selectedTreeNodeIds]
        .map((nodeId) => modelTreeNodeIndex.get(nodeId))
        .filter((node): node is ModelTreeNode => Boolean(node)),
    [modelTreeNodeIndex, selectedTreeNodeIds],
  );
  const selectedObjectCount = useMemo(() => getUniqueObjectIdsForNodes(selectedTreeNodes).length, [selectedTreeNodes]);
  const treeObjectCount = useMemo(() => getUniqueObjectIdsForNodes(modelTree).length, [modelTree]);
  const viewerIsLoading = viewerLoadStage === "manifest" || viewerLoadStage === "downloading" || viewerLoadStage === "parsing";

  const issueViewerCommand = useCallback((type: ViewerCommand["type"], payload: Record<string, unknown> = {}) => {
    setViewerCommand({ type, payload, seq: Date.now() + Math.random() });
  }, []);

  useEffect(() => {
    const element = viewerWindowRef.current;
    if (!element) return;
    const containWheel = (event: WheelEvent) => {
      const target = event.target;
      if (target instanceof Element && target.closest(".model-properties-panel")) return;
      event.preventDefault();
    };
    element.addEventListener("wheel", containWheel, { passive: false, capture: true });
    return () => {
      element.removeEventListener("wheel", containWheel, { capture: true });
    };
  }, []);

  const handleObjectPick = useCallback(
    async (objectId: string, picked?: Record<string, unknown>) => {
      if (!objectId) return;
      objectDetailControllerRef.current?.abort();
      objectDetailControllerRef.current = null;
      const pickedProperties = picked?.properties;
      const fallback: SelectedObject = {
        objectId,
        label: String(picked?.label ?? picked?.name ?? objectId),
        type: String(picked?.type ?? "Viewer Object"),
        source: String(picked?.source ?? "IFC model"),
        properties:
          pickedProperties && typeof pickedProperties === "object" && !Array.isArray(pickedProperties)
            ? (pickedProperties as Record<string, unknown>)
            : picked,
      };
      setSelectedTreeNodeIds(new Set([objectId]));
      setExpandedTreeIds((current) => expandTreeAncestors(current, objectId, modelTreeParentIndexRef.current));
      setSelectedObject(fallback);
      if (pickedProperties) return;
      if (!selectedModelId) return;
      const controller = new AbortController();
      objectDetailControllerRef.current = controller;
      try {
        const resolved = await getJson<SelectedObject>(
          `/api/ifc/model-viewer/object?model_id=${encodeURIComponent(selectedModelId)}&object_id=${encodeURIComponent(objectId)}`,
          { token: authToken, signal: controller.signal },
        );
        if (!controller.signal.aborted) setSelectedObject(resolved);
      } catch (error) {
        if (!controller.signal.aborted && !isAbortError(error)) setSelectedObject(fallback);
      } finally {
        if (objectDetailControllerRef.current === controller) objectDetailControllerRef.current = null;
      }
    },
    [authToken, selectedModelId],
  );

  useEffect(() => {
    return () => objectDetailControllerRef.current?.abort();
  }, [authToken, selectedModelId]);

  const handleModelTreeLoaded = useCallback((nodes: ModelTreeNode[]) => {
    modelTreeParentIndexRef.current = buildModelTreeParentIndex(nodes);
    setModelTree(nodes);
    setExpandedTreeIds(new Set(getDefaultExpandedTreeIds(nodes)));
  }, []);

  const handleObjectClear = useCallback(() => {
    setSelectedObject(null);
    setSelectedTreeNodeIds(new Set());
  }, []);

  useEffect(() => {
    if (selectedTreeNodeIds.size !== 1) return;
    const frame = requestAnimationFrame(() => {
      const list = modelTreeListRef.current;
      list?.querySelector<HTMLElement>('[data-tree-selected="true"]')?.scrollIntoView({ block: "nearest", inline: "nearest" });
      if (list) list.scrollLeft = 0;
    });
    return () => cancelAnimationFrame(frame);
  }, [expandedTreeIds, selectedTreeNodeIds]);

  useEffect(() => {
    setPropertyFilter((current) => {
      const options = filterOptionsByKey[current.key] ?? [];
      if (options.some((option) => option.value === current.value)) return current;
      return { ...current, value: options[0]?.value ?? "" };
    });
  }, [filterOptionsByKey]);

  function updatePropertyFilterKey(key: string) {
    const options = filterOptionsByKey[key] ?? [];
    setPropertyFilter({ key, value: options[0]?.value ?? "" });
  }

  function updatePropertyFilterValue(value: string) {
    setPropertyFilter((current) => ({ ...current, value }));
  }

  function runFilterCommand(type: "isolateIds" | "colorIds") {
    if (!selectedFilterOption) return;
    issueViewerCommand(type, { objectIds: selectedFilterOption.objectIds });
  }

  function toggleTreeNode(nodeId: string) {
    setExpandedTreeIds((current) => {
      const next = new Set(current);
      if (next.has(nodeId)) {
        next.delete(nodeId);
      } else {
        next.add(nodeId);
      }
      return next;
    });
  }

  function applyTreeSelection(node: ModelTreeNode, additive: boolean) {
    const nextIds = new Set(additive ? selectedTreeNodeIds : []);
    if (additive && nextIds.has(node.id)) {
      nextIds.delete(node.id);
    } else {
      nextIds.add(node.id);
    }
    const nextNodes = [...nextIds]
      .map((nodeId) => (nodeId === node.id ? node : modelTreeNodeIndex.get(nodeId)))
      .filter((item): item is ModelTreeNode => Boolean(item));
    const objectIds = getUniqueObjectIdsForNodes(nextNodes);
    setSelectedTreeNodeIds(new Set(nextNodes.map((item) => item.id)));
    setSelectedObject(selectedObjectFromTreeNodes(nextNodes, objectIds));
    issueViewerCommand("select", { objectIds });
  }

  function selectTreeNode(node: ModelTreeNode, event: ReactMouseEvent<HTMLButtonElement>) {
    applyTreeSelection(node, event.ctrlKey || event.metaKey);
  }

  function toggleTreeNodeSelection(node: ModelTreeNode) {
    applyTreeSelection(node, true);
  }

  return (
    <section className={selectedObject ? "model-explorer-grid with-properties" : "model-explorer-grid"}>
      <aside className="model-tree-panel">
        <div className="panel-header slim">
          <div>
            <h2>모델 트리</h2>
            <span>{project?.name ?? "프로젝트 없음"}</span>
          </div>
          <Building2 size={18} />
        </div>

        <label className="model-select-field">
          <span>IFC 모델</span>
          <select value={selectedModelId} onChange={(event) => setSelectedModelId(event.target.value)}>
            {visibleModels.length ? (
              visibleModels.map((model) => (
                <option key={model.id} value={model.id}>
                  {model.filename}
                </option>
              ))
            ) : (
              <option value="">이 프로젝트에 IFC 모델 없음</option>
            )}
          </select>
        </label>

        <label className="model-tree-search">
          <Search size={16} />
          <input value={treeSearch} placeholder="모델 객체 검색" onChange={(event) => setTreeSearch(event.target.value)} />
          {treeSearch ? (
            <button type="button" onClick={() => setTreeSearch("")} aria-label="검색어 지우기" title="검색어 지우기">
              <X size={14} />
            </button>
          ) : null}
        </label>

        <div className="model-tree-status" aria-live="polite">
          <span>{viewerIsLoading ? "불러오는 중" : `${treeObjectCount.toLocaleString()}개 객체`}</span>
          {selectedTreeNodeIds.size ? (
            <button
              type="button"
              onClick={() => {
                handleObjectClear();
                issueViewerCommand("clearSelection");
              }}
            >
              {selectedTreeNodeIds.size}개 항목 · {selectedObjectCount.toLocaleString()}개 객체 선택
              <X size={13} />
            </button>
          ) : (
            <em>선택 없음</em>
          )}
        </div>

        <div className="model-tree-list" role="tree" ref={modelTreeListRef}>
          {filteredTree.length ? (
            filteredTree.map((node) => (
              <ModelTreeNodeItem
                expandedIds={expandedTreeIds}
                forceExpanded={forceExpandedTree}
                key={node.id}
                level={0}
                node={node}
                selectedTreeNodeIds={selectedTreeNodeIds}
                onSelect={selectTreeNode}
                onToggle={toggleTreeNode}
                onToggleSelection={toggleTreeNodeSelection}
              />
            ))
          ) : viewerIsLoading ? (
            <ModelTreeLoading stage={viewerLoadStage} />
          ) : (
            <div className="model-tree-empty">
              <Database size={18} />
              <strong>{visibleModels.length ? "모델 트리를 불러올 수 없습니다." : "이 프로젝트에 IFC 모델이 없습니다."}</strong>
              <span>
                {visibleModels.length
                  ? "IFC 모델 구조를 불러오는 중이거나, 이 모델에는 구조 정보가 없습니다."
                  : "동기화 탭에서 Drive 모델을 등록하면 모델 트리가 표시됩니다."}
              </span>
            </div>
          )}
        </div>
      </aside>

      <div className="model-viewer-panel">
        <div className="panel-header">
          <div>
            <h2>모델 탐색기</h2>
            <span>
              {visibleModels.length
                ? manifest?.filename ?? visibleModels.find((model) => model.id === selectedModelId)?.filename ?? "모델을 선택하세요"
                : `${project?.name ?? "선택한 프로젝트"}에 연결된 IFC 모델이 없습니다.`}
            </span>
          </div>
          <div className="model-header-actions">
            <div
              className="model-header-tools"
              aria-label="모델 뷰어 도구"
              role="toolbar"
              onWheel={(event) => {
                event.preventDefault();
                event.stopPropagation();
              }}
            >
              <button
                type="button"
                aria-label="선택 객체 보기"
                title="선택 객체 보기"
                disabled={!selectedObject}
                onClick={() => issueViewerCommand("fitSelected")}
              >
                <Focus size={17} />
              </button>
              <button type="button" aria-label="거리 측정" title="거리 측정" onClick={() => issueViewerCommand("toggleMeasure")}>
                <Ruler size={17} />
              </button>
              <button type="button" aria-label="단면 상자" title="단면 상자" onClick={() => issueViewerCommand("toggleSectionBox")}>
                <Scissors size={17} />
              </button>
              <button type="button" aria-label="단면 제거" title="단면 제거" onClick={() => issueViewerCommand("clearSections")}>
                <X size={17} />
              </button>
              <button
                type="button"
                aria-label="선택 객체만 보기"
                title="선택 객체만 보기"
                disabled={!selectedObject}
                onClick={() => issueViewerCommand("isolateSelected")}
              >
                <Layers size={17} />
              </button>
              <button
                type="button"
                aria-label="선택 객체 숨기기"
                title="선택 객체 숨기기"
                disabled={!selectedObject}
                onClick={() => issueViewerCommand("hideSelected")}
              >
                <EyeOff size={17} />
              </button>
              <button
                type="button"
                aria-label="선택 객체 색상 강조"
                title="선택 객체 색상 강조"
                disabled={!selectedObject}
                onClick={() => issueViewerCommand("colorSelected")}
              >
                <Paintbrush size={17} />
              </button>
              <button type="button" aria-label="전체 표시" title="전체 표시" onClick={() => issueViewerCommand("showAll")}>
                <Eye size={17} />
              </button>
              <button type="button" aria-label="표시 초기화" title="표시 초기화" onClick={() => issueViewerCommand("clearOverrides")}>
                <RotateCcw size={17} />
              </button>
              <button type="button" aria-label="측정 제거" title="측정 제거" onClick={() => issueViewerCommand("clearMeasurements")}>
                <Ruler size={17} />
              </button>
              <button type="button" aria-label="카메라 초기화" title="카메라 초기화" onClick={() => issueViewerCommand("resetCamera")}>
                <Camera size={17} />
              </button>
              <button type="button" aria-label="이미지 저장" title="이미지 저장" onClick={() => issueViewerCommand("screenshot")}>
                <Download size={17} />
              </button>
              <select
                aria-label="필터 기준"
                value={propertyFilter.key}
                onChange={(event) => updatePropertyFilterKey(event.target.value)}
              >
                {MODEL_FILTER_KEYS.map((filterKey) => (
                  <option key={filterKey.key} value={filterKey.key}>
                    {filterKey.label}
                  </option>
                ))}
              </select>
              <select
                aria-label="필터 값"
                value={propertyFilter.value}
                disabled={!activeFilterOptions.length}
                onChange={(event) => updatePropertyFilterValue(event.target.value)}
              >
                {activeFilterOptions.length ? (
                  activeFilterOptions.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))
                ) : (
                  <option value="">없음</option>
                )}
              </select>
              <button
                type="button"
                aria-label="필터 객체만 보기"
                title="필터 객체만 보기"
                disabled={!selectedFilterOption}
                onClick={() => runFilterCommand("isolateIds")}
              >
                <Glasses size={17} />
              </button>
              <button
                type="button"
                aria-label="필터 객체 색상 강조"
                title="필터 객체 색상 강조"
                disabled={!selectedFilterOption}
                onClick={() => runFilterCommand("colorIds")}
              >
                <Paintbrush size={17} />
              </button>
            </div>
          </div>
        </div>

        <div className="model-viewer-window" ref={viewerWindowRef}>
          {visibleModels.length ? (
            <ModelViewerCanvas
              authToken={authToken}
              command={viewerCommand}
              manifest={manifest}
              onModelTreeLoaded={handleModelTreeLoaded}
              onObjectClear={handleObjectClear}
              onLoadStageChange={setViewerLoadStage}
              onViewerError={setViewerError}
              onObjectPick={handleObjectPick}
            />
          ) : (
            <ModelProjectEmptyState projectName={project?.name} />
          )}
          {visibleModels.length && viewerIsLoading ? <ModelViewerLoading stage={viewerLoadStage} /> : null}
        </div>

        {manifestError || viewerError ? <p className="model-viewer-error">{manifestError || viewerError}</p> : null}
      </div>
      {selectedObject ? (
        <ModelPropertiesPanel
          object={selectedObject}
          onClose={() => {
            handleObjectClear();
            issueViewerCommand("clearSelection");
          }}
        />
      ) : null}
    </section>
  );
}

function ModelProjectEmptyState({ projectName }: { projectName?: string }) {
  return (
    <div className="model-empty-viewer project-empty-state">
      <Database size={30} />
      <strong>IFC 모델이 없습니다.</strong>
      <span>{projectName ? `${projectName} 프로젝트에 연결된 IFC 모델이 없습니다.` : "선택한 프로젝트에 연결된 IFC 모델이 없습니다."}</span>
      <em>Drive에 IFC/XKT 파일을 올린 뒤 동기화 탭에서 등록하면 모델 탐색기를 사용할 수 있습니다.</em>
    </div>
  );
}

function viewerLoadLabel(stage: ViewerLoadStage) {
  if (stage === "manifest") return "모델 정보를 확인하는 중";
  if (stage === "downloading") return "Drive에서 XKT를 불러오는 중";
  return "3D 객체와 속성을 분석하는 중";
}

function ModelTreeLoading({ stage }: { stage: ViewerLoadStage }) {
  return (
    <div className="model-tree-loading" role="status">
      <LoaderCircle size={18} />
      <strong>{viewerLoadLabel(stage)}</strong>
      <span>모델 구조가 준비되면 자동으로 표시됩니다.</span>
    </div>
  );
}

function ModelViewerLoading({ stage }: { stage: ViewerLoadStage }) {
  return (
    <div className="model-viewer-loading" role="status">
      <LoaderCircle size={22} />
      <strong>{viewerLoadLabel(stage)}</strong>
    </div>
  );
}

function ModelTreeNodeItem({
  expandedIds,
  forceExpanded,
  level,
  node,
  selectedTreeNodeIds,
  onSelect,
  onToggle,
  onToggleSelection,
}: {
  expandedIds: Set<string>;
  forceExpanded: boolean;
  level: number;
  node: ModelTreeNode;
  selectedTreeNodeIds: Set<string>;
  onSelect: (node: ModelTreeNode, event: ReactMouseEvent<HTMLButtonElement>) => void;
  onToggle: (nodeId: string) => void;
  onToggleSelection: (node: ModelTreeNode) => void;
}) {
  const hasChildren = node.children.length > 0;
  const expanded = forceExpanded || expandedIds.has(node.id);
  const selected = selectedTreeNodeIds.has(node.id);
  const kind = modelTreeNodeKind(node);
  const objectCount = getModelTreeNodeObjectCount(node);
  return (
    <div
      className="model-tree-branch"
      role="treeitem"
      aria-expanded={hasChildren ? expanded : undefined}
      aria-selected={selected}
      data-tree-selected={selected ? "true" : undefined}
    >
      <div
        className={selected ? `model-tree-node ${kind} active` : `model-tree-node ${kind}`}
        style={{ "--tree-depth": level } as CSSProperties}
      >
        <button
          className="model-tree-toggle"
          disabled={!hasChildren}
          type="button"
          aria-label={expanded ? "하위 항목 접기" : "하위 항목 펼치기"}
          onClick={() => onToggle(node.id)}
        >
          {hasChildren ? expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} /> : null}
        </button>
        {node.selectable ? (
          <input
            className="model-tree-checkbox"
            type="checkbox"
            checked={selected}
            onChange={() => onToggleSelection(node)}
            onClick={(event) => event.stopPropagation()}
            aria-label={`${node.label || node.id} 선택`}
          />
        ) : (
          <span className="model-tree-checkbox-spacer" />
        )}
        <button className="model-tree-node-content" type="button" onClick={(event) => onSelect(node, event)}>
          <ModelTreeNodeIcon node={node} />
          <span className="model-tree-node-label">
            <strong>{node.label || node.id}</strong>
            <em>{node.type}</em>
          </span>
          {objectCount ? <small title={`${objectCount.toLocaleString()}개 객체`}>{objectCount.toLocaleString()}</small> : null}
        </button>
      </div>
      {hasChildren && expanded ? (
        <div className="model-tree-children" role="group">
          {node.children.map((child) => (
            <ModelTreeNodeItem
              expandedIds={expandedIds}
              forceExpanded={forceExpanded}
              key={child.id}
              level={level + 1}
              node={child}
              selectedTreeNodeIds={selectedTreeNodeIds}
              onSelect={onSelect}
              onToggle={onToggle}
              onToggleSelection={onToggleSelection}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function ModelTreeNodeIcon({ node }: { node: ModelTreeNode }) {
  const kind = modelTreeNodeKind(node);
  if (kind === "project") return <FolderTree size={14} aria-hidden="true" />;
  if (kind === "site") return <MapPin size={14} aria-hidden="true" />;
  if (kind === "building") return <Building2 size={14} aria-hidden="true" />;
  if (kind === "storey") return <Layers size={14} aria-hidden="true" />;
  if (kind === "group") return <Boxes size={14} aria-hidden="true" />;
  return <Box size={13} aria-hidden="true" />;
}

function modelTreeNodeKind(node: ModelTreeNode) {
  if (node.syntheticGroup) return "group";
  const type = node.type.toLowerCase();
  if (type.includes("project")) return "project";
  if (type.includes("site")) return "site";
  if (type.includes("buildingstorey") || type.includes("storey")) return "storey";
  if (type.includes("building")) return "building";
  return "object";
}

function getModelTreeNodeObjectCount(node: ModelTreeNode) {
  if (node.children.length === 0) return 0;
  return new Set(node.objectIds ?? []).size || node.childCount;
}

function filterModelTree(nodes: ModelTreeNode[], search: string) {
  const query = search.trim().toLowerCase();
  if (!query) return nodes;
  const filterNode = (node: ModelTreeNode): ModelTreeNode | null => {
    const ownText = `${node.label} ${node.id} ${node.type}`.toLowerCase();
    const filteredChildren = node.children.map(filterNode).filter((item): item is ModelTreeNode => Boolean(item));
    if (ownText.includes(query) || filteredChildren.length) {
      return { ...node, children: filteredChildren };
    }
    return null;
  };
  return nodes.map(filterNode).filter((item): item is ModelTreeNode => Boolean(item));
}

function getDefaultExpandedTreeIds(nodes: ModelTreeNode[], depth = 0): string[] {
  const ids: string[] = [];
  nodes.forEach((node) => {
    if (node.children.length && depth < 3) {
      ids.push(node.id);
      ids.push(...getDefaultExpandedTreeIds(node.children, depth + 1));
    }
  });
  return ids;
}

function buildModelTreeNodeIndex(nodes: ModelTreeNode[]) {
  const index = new Map<string, ModelTreeNode>();
  const visit = (node: ModelTreeNode) => {
    index.set(node.id, node);
    node.children.forEach(visit);
  };
  nodes.forEach(visit);
  return index;
}

function buildModelTreeParentIndex(nodes: ModelTreeNode[]) {
  const parents = new Map<string, string>();
  const visit = (node: ModelTreeNode, parentId?: string) => {
    if (parentId) parents.set(node.id, parentId);
    node.children.forEach((child) => visit(child, node.id));
  };
  nodes.forEach((node) => visit(node));
  return parents;
}

function expandTreeAncestors(current: Set<string>, nodeId: string, parents: Map<string, string>) {
  const next = new Set(current);
  let parentId = parents.get(nodeId);
  while (parentId) {
    next.add(parentId);
    parentId = parents.get(parentId);
  }
  return next;
}

function getUniqueObjectIdsForNodes(nodes: ModelTreeNode[]) {
  const objectIds = new Set<string>();
  nodes.forEach((node) => {
    const ids = node.objectIds?.length ? node.objectIds : [node.id];
    ids.forEach((id) => objectIds.add(id));
  });
  return [...objectIds];
}

function selectedObjectFromTreeNodes(nodes: ModelTreeNode[], objectIds: string[]): SelectedObject | null {
  if (!nodes.length) return null;
  if (nodes.length === 1) {
    const [node] = nodes;
    return {
      objectId: node.id,
      label: node.label,
      type: node.type,
      source: "IFC model",
      properties: node.properties ?? { objectCount: objectIds.length },
    };
  }
  return {
    objectId: `selection-${nodes.length}-${objectIds.length}`,
    label: `${nodes.length}개 항목 선택`,
    type: "Selection",
    source: "IFC model",
    properties: {
      selectedItems: nodes.length,
      objectCount: objectIds.length,
      labels: nodes.slice(0, 12).map((node) => node.label),
    },
  };
}

function buildModelFilterOptions(nodes: ModelTreeNode[]): Record<string, FilterOption[]> {
  const buckets: Record<string, Map<string, { label: string; count: number; objectIds: Set<string> }>> = {};
  MODEL_FILTER_KEYS.forEach(({ key }) => {
    buckets[key] = new Map();
  });

  const visit = (node: ModelTreeNode, storeyLabel = "") => {
    const nextStoreyLabel = node.type === "IfcBuildingStorey" ? node.label : storeyLabel;
    const objectIds = node.objectIds ?? [];
    if (node.selectable && objectIds.length && !node.syntheticGroup) {
      MODEL_FILTER_KEYS.forEach(({ key }) => {
        const value = getModelFilterValue(node, key, nextStoreyLabel);
        if (value) addFilterBucket(buckets[key], value, objectIds);
      });
    }
    node.children.forEach((child) => visit(child, nextStoreyLabel));
  };

  nodes.forEach((node) => visit(node));

  return MODEL_FILTER_KEYS.reduce<Record<string, FilterOption[]>>((result, { key }) => {
    result[key] = [...(buckets[key]?.entries() ?? [])]
      .map(([, bucket]) => ({
        value: bucket.label,
        label: `${bucket.label} (${bucket.count})`,
        count: bucket.count,
        objectIds: [...bucket.objectIds],
      }))
      .sort((left, right) => compareFilterOptions(key, left, right));
    return result;
  }, {});
}

function addFilterBucket(
  bucketMap: Map<string, { label: string; count: number; objectIds: Set<string> }>,
  rawValue: string,
  objectIds: string[],
) {
  const label = normalizeFilterValue(rawValue);
  if (!label) return;
  const key = label.toLowerCase();
  const bucket = bucketMap.get(key) ?? { label, count: 0, objectIds: new Set<string>() };
  objectIds.forEach((id) => bucket.objectIds.add(id));
  bucket.count = bucket.objectIds.size;
  bucketMap.set(key, bucket);
}

function getModelFilterValue(node: ModelTreeNode, key: string, storeyLabel: string) {
  const properties = node.properties ?? {};
  if (key === "category") {
    const category = friendlyIfcCategory(node.type);
    return ["Project", "Site", "Building", "Storey"].includes(category) ? "" : category;
  }
  if (key === "type") return node.type;
  if (key === "storey") {
    return (
      storeyLabel ||
      findPropertyValue(properties, [/building\s*storey/i, /\bstorey\b/i, /\blevel\b/i, /층/]) ||
      ""
    );
  }
  if (key === "module_id") {
    return findPropertyValue(properties, [/module[_\s-]*id/i, /module\s*id/i, /모듈/]) || "";
  }
  if (key === "family") {
    return findPropertyValue(properties, [/family/i, /패밀리/]) || "";
  }
  return "";
}

function findPropertyValue(properties: Record<string, unknown>, patterns: RegExp[]) {
  for (const [key, value] of Object.entries(properties)) {
    if (!patterns.some((pattern) => pattern.test(key))) continue;
    const normalized = normalizeFilterValue(value);
    if (normalized) return normalized;
  }
  return "";
}

function normalizeFilterValue(value: unknown) {
  if (value == null) return "";
  if (typeof value === "object") return "";
  return String(value).trim();
}

function friendlyIfcCategory(type: string) {
  const normalized = type.replace(/^Ifc/i, "");
  const categoryMap: Record<string, string> = {
    WallStandardCase: "Wall",
    CurtainWall: "Wall",
    Window: "Window",
    Door: "Door",
    Slab: "Slab",
    Roof: "Roof",
    Column: "Column",
    Beam: "Beam",
    Plate: "Plate",
    Member: "Member",
    Stair: "Stair",
    StairFlight: "Stair",
    Railing: "Railing",
    Covering: "Covering",
    Space: "Space",
    BuildingStorey: "Storey",
    Building: "Building",
    Site: "Site",
    Project: "Project",
  };
  return categoryMap[normalized] ?? (normalized || "Object");
}

function compareFilterOptions(key: string, left: FilterOption, right: FilterOption) {
  if (key === "category") {
    const order = ["Window", "Door", "Wall", "Slab", "Roof", "Column", "Beam", "Plate", "Member", "Stair", "Railing", "Covering"];
    const leftIndex = order.indexOf(left.value);
    const rightIndex = order.indexOf(right.value);
    if (leftIndex !== -1 || rightIndex !== -1) {
      return (leftIndex === -1 ? 999 : leftIndex) - (rightIndex === -1 ? 999 : rightIndex);
    }
  }
  if (right.count !== left.count) return right.count - left.count;
  return left.value.localeCompare(right.value, "ko");
}

function ModelViewerCanvas({
  authToken,
  manifest,
  command,
  onModelTreeLoaded,
  onObjectClear,
  onLoadStageChange,
  onViewerError,
  onObjectPick,
}: {
  authToken: string;
  manifest: ModelManifest | null;
  command: ViewerCommand | null;
  onModelTreeLoaded: (nodes: ModelTreeNode[]) => void;
  onObjectClear: () => void;
  onLoadStageChange: (stage: ViewerLoadStage) => void;
  onViewerError: (message: string) => void;
  onObjectPick: (objectId: string, picked?: Record<string, unknown>) => void;
}) {
  const canvasIdRef = useRef(`xeokit-canvas-${Math.random().toString(36).slice(2)}`);
  const canvasElementRef = useRef<HTMLCanvasElement | null>(null);
  const viewerRef = useRef<any>(null);
  const modelRef = useRef<any>(null);
  const xeokitRef = useRef<any>(null);
  const sectionPlanesRef = useRef<any>(null);
  const sectionBoxRef = useRef<any>(null);
  const distanceMeasurementsRef = useRef<any>(null);
  const distanceControlRef = useRef<any>(null);

  useEffect(() => {
    const canvas = canvasElementRef.current;
    if (!canvas || typeof ResizeObserver === "undefined") return;
    let frame = 0;
    const observer = new ResizeObserver(() => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        window.dispatchEvent(new Event("resize"));
        viewerRef.current?.scene?.render?.(true);
      });
    });
    observer.observe(canvas);
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    async function boot() {
      if (!manifest?.xktUrl) {
        onLoadStageChange("idle");
        return;
      }
      onViewerError("");
      onLoadStageChange("downloading");
      const xeokit = await import("@xeokit/xeokit-sdk");
      if (cancelled) return;
      const assetResponse = await request(manifest.xktUrl, { token: authToken, signal: controller.signal });
      const xktData = await assetResponse.arrayBuffer();
      if (cancelled) return;
      onLoadStageChange("parsing");
      xeokitRef.current = xeokit;
      viewerRef.current?.destroy?.();
      const viewer = new xeokit.Viewer({
        canvasId: canvasIdRef.current,
        transparent: false,
        backgroundColor: [0.91, 0.93, 0.96],
        backgroundColorFromAmbientLight: false,
        antialias: true,
        saoEnabled: true,
      });
      configureViewer(viewer, xeokit);
      viewerRef.current = viewer;
      resizeModelViewer(viewer);
      sectionPlanesRef.current = new xeokit.SectionPlanesPlugin(viewer, { overviewVisible: false });
      if (xeokit.DistanceMeasurementsPlugin && xeokit.DistanceMeasurementsMouseControl) {
        distanceMeasurementsRef.current = new xeokit.DistanceMeasurementsPlugin(viewer);
        const pointerLens = xeokit.PointerLens ? new xeokit.PointerLens(viewer) : undefined;
        distanceControlRef.current = new xeokit.DistanceMeasurementsMouseControl(distanceMeasurementsRef.current, {
          pointerLens,
        });
        distanceControlRef.current.snapToVertex = true;
        distanceControlRef.current.snapToEdge = true;
        distanceControlRef.current.deactivate?.();
      }
      const loader = new xeokit.XKTLoaderPlugin(viewer);
      const model = loader.load({
        id: manifest.modelId,
        xkt: xktData,
        edges: true,
      });
      modelRef.current = model;
      model.on("loaded", () => {
        if (cancelled) return;
        resizeModelViewer(viewer);
        const objectCount = Object.keys(viewer.scene.objects || {}).length;
        onModelTreeLoaded(buildModelTree(viewer, manifest.modelId));
        onLoadStageChange("ready");
        const modelAABB = getFiniteAABB(model.aabb);
        const sceneAABB = getFiniteAABB(viewer.scene.aabb);
        const targetAABB = modelAABB || sceneAABB;
        console.info("[model-explorer] XKT loaded", {
          modelId: manifest.modelId,
          objectCount,
          aabb: targetAABB,
        });
        if (targetAABB) {
          jumpViewerToAABB(viewer, targetAABB);
        } else {
          console.warn("[model-explorer] XKT loaded without a valid AABB", {
            modelId: manifest.modelId,
            modelAABB: model.aabb,
            sceneAABB: viewer.scene.aabb,
          });
          viewer.cameraFlight.jumpTo(viewer.scene);
        }
        viewer.scene.render(true);
      });
      model.on("error", (error: unknown) => {
        console.error("[model-explorer] XKT load failed", error);
        onLoadStageChange("error");
        onViewerError(`XKT 모델을 불러오지 못했습니다. ${String(error)}`);
      });
      viewer.cameraControl.on("picked", (pickResult: any) => {
        const entity = pickResult?.entity;
        if (!entity?.id) return;
        if (activateSectionBoxFace(sectionPlanesRef.current, sectionBoxRef, viewer.scene, entity.id)) return;
        selectViewerObjects(viewer, [entity.id]);
        const metaObject = viewer.metaScene?.metaObjects?.[entity.id];
        const metaSelection = metaObject ? selectedObjectFromMetaObject(metaObject, viewer.scene.objects || {}) : null;
        onObjectPick(entity.id, {
          ...(metaSelection ?? {}),
          id: entity.id,
          aabb: entity.aabb,
          visible: entity.visible,
          xrayed: entity.xrayed,
          selected: entity.selected,
        });
      });
      viewer.cameraControl.on("pickedNothing", () => {
        selectViewerObjects(viewer, []);
        viewer.scene.render(true);
        onObjectClear();
      });
    }
    boot().catch((error) => {
      if (controller.signal.aborted || isAbortError(error)) return;
      console.error("[model-explorer] XKT boot failed", error);
      onLoadStageChange("error");
      onViewerError(`XKT 파일을 불러오지 못했습니다. ${error instanceof Error ? error.message : String(error)}`);
      onModelTreeLoaded([]);
    });
    return () => {
      cancelled = true;
      controller.abort();
      viewerRef.current?.destroy?.();
      viewerRef.current = null;
      modelRef.current = null;
      xeokitRef.current = null;
      sectionPlanesRef.current = null;
      sectionBoxRef.current = null;
      distanceMeasurementsRef.current = null;
      distanceControlRef.current = null;
    };
  }, [authToken, manifest?.modelId, manifest?.xktUrl, onLoadStageChange, onModelTreeLoaded, onObjectClear, onObjectPick, onViewerError]);

  useEffect(() => {
    if (!command || !viewerRef.current) return;
    const viewer = viewerRef.current;
    const scene = viewer.scene;
    const commandIds = getCommandObjectIds(viewer, command);
    if (command.type === "select") {
      selectViewerObjects(viewer, commandIds);
    }
    if (command.type === "fitSelected") {
      fitViewerToObjects(viewer, commandIds.length ? commandIds : scene.selectedObjectIds || []);
    }
    if (command.type === "resetCamera") {
      viewer.cameraFlight.flyTo(modelRef.current || scene);
    }
    if (command.type === "showAll") {
      scene.setObjectsVisible(scene.objectIds || [], true);
      scene.setObjectsXRayed(scene.xrayedObjectIds || [], false);
      scene.setObjectsHighlighted(scene.highlightedObjectIds || [], false);
    }
    if (command.type === "clearSelection") {
      selectViewerObjects(viewer, []);
      onObjectClear();
    }
    if (command.type === "hideSelected") {
      if (commandIds.length) {
        scene.setObjectsVisible(commandIds, false);
        selectViewerObjects(viewer, []);
        onObjectClear();
      }
    }
    if (command.type === "isolateSelected") {
      isolateViewerObjects(viewer, commandIds);
    }
    if (command.type === "colorSelected") {
      colorizeViewerObjects(viewer, commandIds, [0.95, 0.62, 0.16]);
    }
    if (command.type === "isolateIds") {
      isolateViewerObjects(viewer, commandIds);
    }
    if (command.type === "colorIds") {
      colorizeViewerObjects(viewer, commandIds, [0.08, 0.49, 0.95]);
    }
    if (command.type === "clearOverrides") {
      resetViewerOverrides(viewer);
      onObjectClear();
    }
    if (command.type === "toggleSectionBox") {
      toggleSectionBox(sectionPlanesRef.current, sectionBoxRef, scene, xeokitRef.current);
    }
    if (command.type === "clearSections") {
      sectionPlanesRef.current?.clear?.();
      destroySectionBox(sectionBoxRef);
    }
    if (command.type === "toggleMeasure") {
      toggleDistanceMeasurement(distanceControlRef.current);
    }
    if (command.type === "clearMeasurements") {
      clearDistanceMeasurements(distanceControlRef.current, distanceMeasurementsRef.current);
    }
    if (command.type === "screenshot") {
      downloadViewerSnapshot(viewer);
    }
    scene.render(true);
  }, [command, onObjectClear]);

  if (!manifest?.xktUrl) {
    return (
      <div className="model-empty-viewer">
        <MousePointer2 size={30} />
        <strong>3D 뷰어 변환 대기 중</strong>
        <span>
          {manifest?.status === "pending-xkt"
            ? "Drive에 IFC만 있고 같은 이름의 XKT가 아직 없습니다. 변환한 XKT를 같은 폴더에 올린 뒤 동기화하세요."
            : "모델을 선택하거나 변환한 XKT가 올라온 모델을 동기화하세요."}
        </span>
        {manifest?.error ? <em>{manifest.error}</em> : null}
      </div>
    );
  }

  return <canvas ref={canvasElementRef} id={canvasIdRef.current} className="model-xeokit-canvas" />;
}

function configureViewer(viewer: any, xeokit: any) {
  const scene = viewer.scene;
  scene.clearLights();
  new xeokit.AmbientLight(scene, { color: [0.84, 0.87, 0.9], intensity: 0.55 });
  new xeokit.DirLight(scene, { dir: [-0.7, -0.45, -0.55], color: [1, 0.98, 0.94], intensity: 0.82, space: "view" });
  new xeokit.DirLight(scene, { dir: [0.45, -0.3, -0.8], color: [0.76, 0.84, 1], intensity: 0.28, space: "view" });
  scene.sao.enabled = true;
  scene.sao.kernelRadius = 72;
  scene.sao.bias = 0.72;
  scene.selectedMaterial.fill = true;
  scene.selectedMaterial.fillColor = [0.1, 0.46, 0.92];
  scene.selectedMaterial.fillAlpha = 0.22;
  scene.selectedMaterial.edges = true;
  scene.highlightMaterial.fill = true;
  scene.highlightMaterial.fillColor = [0.95, 0.7, 0.18];
  scene.highlightMaterial.fillAlpha = 0.18;
  viewer.camera.eye = [-10, 7, -13];
  viewer.camera.look = [0, 0, 0];
  viewer.camera.up = [0, 1, 0];
  viewer.camera.projection = "perspective";
}

function resizeModelViewer(viewer: any) {
  window.dispatchEvent(new Event("resize"));
  viewer.scene?.render?.(true);
}

function buildModelTree(viewer: any, modelId: string): ModelTreeNode[] {
  const metaScene = viewer.metaScene;
  const metaModel = metaScene?.metaModels?.[modelId] ?? Object.values(metaScene?.metaModels ?? {})[0];
  const roots = getRootMetaObjects(metaModel);
  if (!roots.length) return buildFlatSceneObjectTree(viewer);
  const sceneObjects = viewer.scene.objects || {};
  return roots
    .map((root) => serializeMetaObject(root, sceneObjects))
    .filter((node) => node.selectable || node.children.length > 0);
}

function getRootMetaObjects(metaModel: any): any[] {
  if (!metaModel) return [];
  if (Array.isArray(metaModel.rootMetaObjects)) return metaModel.rootMetaObjects;
  return metaModel.rootMetaObject ? [metaModel.rootMetaObject] : [];
}

function serializeMetaObject(metaObject: any, sceneObjects: Record<string, unknown>): ModelTreeNode {
  const childMetaObjects = Array.isArray(metaObject.children) ? metaObject.children : [];
  const rawChildren = childMetaObjects
    .map((child: any) => serializeMetaObject(child, sceneObjects))
    .filter((node: ModelTreeNode) => node.selectable || node.children.length > 0);
  const objectIds = getMetaObjectSceneIds(metaObject, sceneObjects);
  const label = String(metaObject.name || metaObject.id || metaObject.type || "Object");
  const type = String(metaObject.type || "Object");
  const id = String(metaObject.id || label);
  const children = groupModelTreeChildren(id, rawChildren);
  return {
    id,
    label,
    type,
    childCount: children.length,
    selectable: Boolean(sceneObjects?.[metaObject.id]) || objectIds.length > 0,
    children,
    objectIds,
    properties: getMetaObjectProperties(metaObject, objectIds.length),
  };
}

function groupModelTreeChildren(parentId: string, children: ModelTreeNode[]): ModelTreeNode[] {
  const buckets = new Map<
    string,
    { label: string; nodes: ModelTreeNode[]; objectIds: Set<string>; types: Set<string> }
  >();

  children.forEach((child) => {
    const groupLabel = getAutoGroupLabel(child);
    if (!groupLabel) return;
    const key = groupLabel.toLowerCase();
    const bucket = buckets.get(key) ?? {
      label: groupLabel,
      nodes: [],
      objectIds: new Set<string>(),
      types: new Set<string>(),
    };
    bucket.nodes.push(child);
    bucket.types.add(child.type);
    (child.objectIds ?? []).forEach((objectId) => bucket.objectIds.add(objectId));
    buckets.set(key, bucket);
  });

  const groupedKeys = new Set(
    [...buckets.entries()]
      .filter(([, bucket]) => bucket.nodes.length > 1 && bucket.objectIds.size > 1)
      .map(([key]) => key),
  );
  if (!groupedKeys.size) return children;

  const emitted = new Set<string>();
  return children.flatMap((child) => {
    const groupLabel = getAutoGroupLabel(child);
    const key = groupLabel?.toLowerCase() ?? "";
    if (!groupedKeys.has(key)) return [child];
    if (emitted.has(key)) return [];
    emitted.add(key);

    const bucket = buckets.get(key);
    if (!bucket) return [child];
    const objectIds = [...bucket.objectIds];
    const types = [...bucket.types];
    const type = types.length === 1 ? types[0] : `${bucket.label} group`;
    return [
      {
        id: `${parentId}::group::${treeIdSegment(bucket.label)}`,
        label: bucket.label,
        type,
        childCount: bucket.nodes.length,
        selectable: objectIds.length > 0,
        children: bucket.nodes,
        objectIds,
        syntheticGroup: true,
        properties: {
          category: bucket.label,
          type,
          childCount: bucket.nodes.length,
          objectCount: objectIds.length,
        },
      },
    ];
  });
}

function getAutoGroupLabel(node: ModelTreeNode) {
  if (node.syntheticGroup || node.children.length || !node.selectable || !(node.objectIds?.length)) return "";
  const category = friendlyIfcCategory(node.type);
  if (["Object", "Project", "Site", "Building", "Storey"].includes(category)) return "";
  return category;
}

function treeIdSegment(value: string) {
  return value.toLowerCase().replace(/[^a-z0-9_-]+/gi, "-").replace(/^-+|-+$/g, "") || "group";
}

function buildFlatSceneObjectTree(viewer: any): ModelTreeNode[] {
  const objectIds = Object.keys(viewer.scene.objects || {}).slice(0, 1200);
  return [
    {
      id: `${viewer.scene.id || "model"}-objects`,
      label: "Objects",
      type: "Scene",
      childCount: objectIds.length,
      selectable: false,
      objectIds: [],
      properties: { objectCount: objectIds.length },
      children: objectIds.map((id) => ({
        id,
        label: id,
        type: "Object",
        childCount: 0,
        selectable: true,
        children: [],
        objectIds: [id],
        properties: { id, source: "scene object" },
      })),
    },
  ];
}

function selectedObjectFromMetaObject(metaObject: any, sceneObjects: Record<string, unknown>): SelectedObject {
  const objectIds = getMetaObjectSceneIds(metaObject, sceneObjects);
  return {
    objectId: String(metaObject.id),
    label: String(metaObject.name || metaObject.id),
    type: String(metaObject.type || "Object"),
    source: "IFC model",
    properties: getMetaObjectProperties(metaObject, objectIds.length),
  };
}

function getMetaObjectSceneIds(metaObject: any, sceneObjects: Record<string, unknown>): string[] {
  const ids =
    typeof metaObject.getObjectIDsInSubtree === "function"
      ? metaObject.getObjectIDsInSubtree()
      : metaObject.id
        ? [metaObject.id]
        : [];
  return ids.map(String).filter((id: string) => Boolean(sceneObjects?.[id]));
}

function getMetaObjectProperties(metaObject: any, objectCount: number): Record<string, unknown> {
  const properties: Record<string, unknown> = {
    id: metaObject.id,
    name: metaObject.name,
    type: metaObject.type,
    originalSystemId: metaObject.originalSystemId,
    childCount: Array.isArray(metaObject.children) ? metaObject.children.length : 0,
    objectCount,
  };
  const external = metaObject.external && typeof metaObject.external === "object" ? metaObject.external : {};
  Object.entries(external)
    .slice(0, 24)
    .forEach(([key, value]) => {
      properties[`external.${key}`] = value;
    });
  const propertySets = Array.isArray(metaObject.propertySets) ? metaObject.propertySets : [];
  let count = 0;
  propertySets.forEach((set: any) => {
    const setName = String(set?.name || set?.type || set?.id || "PropertySet");
    const setProperties = Array.isArray(set?.properties) ? set.properties : [];
    setProperties.forEach((property: any) => {
      if (count >= 96) return;
      const name = String(property?.name || `property_${count + 1}`);
      properties[`${setName} - ${name}`] = property?.value ?? property?.description ?? "";
      count += 1;
    });
  });
  return properties;
}

function getCommandObjectIds(viewer: any, command: ViewerCommand) {
  const payloadIds = command.payload?.objectIds;
  if (Array.isArray(payloadIds)) {
    return resolveSceneObjectIds(viewer, payloadIds.map(String));
  }
  const payloadObjectId = command.payload?.objectId;
  if (payloadObjectId) {
    return resolveSceneObjectIds(viewer, [String(payloadObjectId)]);
  }
  return [...(viewer.scene.selectedObjectIds || [])];
}

function resolveSceneObjectIds(viewer: any, objectIds: string[]) {
  const sceneObjects = viewer.scene.objects || {};
  const resolved = new Set<string>();
  objectIds.forEach((id) => {
    if (sceneObjects[id]) {
      resolved.add(id);
      return;
    }
    const metaObject = viewer.metaScene?.metaObjects?.[id];
    if (!metaObject) return;
    getMetaObjectSceneIds(metaObject, sceneObjects).forEach((sceneObjectId) => resolved.add(sceneObjectId));
  });
  return [...resolved];
}

function selectViewerObjects(viewer: any, objectIds: string[]) {
  const ids = resolveSceneObjectIds(viewer, objectIds);
  viewer.scene.setObjectsSelected(viewer.scene.selectedObjectIds, false);
  viewer.scene.setObjectsHighlighted(viewer.scene.highlightedObjectIds, false);
  viewer.scene.setObjectsSelected(ids, true);
  viewer.scene.setObjectsHighlighted(ids, true);
}

function isolateViewerObjects(viewer: any, objectIds: string[]) {
  const ids = resolveSceneObjectIds(viewer, objectIds);
  if (!ids.length) return;
  const scene = viewer.scene;
  scene.setObjectsVisible(scene.objectIds || [], false);
  scene.setObjectsVisible(ids, true);
  selectViewerObjects(viewer, ids);
  fitViewerToObjects(viewer, ids);
}

function colorizeViewerObjects(viewer: any, objectIds: string[], color: number[]) {
  const ids = resolveSceneObjectIds(viewer, objectIds);
  if (!ids.length) return;
  const scene = viewer.scene;
  scene.setObjectsColorized(ids, color);
  scene.setObjectsHighlighted(scene.highlightedObjectIds || [], false);
  scene.setObjectsHighlighted(ids, true);
}

function resetViewerOverrides(viewer: any) {
  const scene = viewer.scene;
  scene.setObjectsVisible(scene.objectIds || [], true);
  scene.setObjectsXRayed(scene.xrayedObjectIds || [], false);
  scene.setObjectsHighlighted(scene.highlightedObjectIds || [], false);
  scene.setObjectsSelected(scene.selectedObjectIds || [], false);
  scene.setObjectsColorized(scene.colorizedObjectIds || [], null);
}

function fitViewerToObjects(viewer: any, objectIds: string[]) {
  const ids = resolveSceneObjectIds(viewer, objectIds);
  if (!ids.length) {
    const sceneAABB = getFiniteAABB(viewer.scene.aabb);
    if (sceneAABB) {
      jumpViewerToAABB(viewer, sceneAABB);
    } else {
      viewer.cameraFlight.jumpTo(viewer.scene);
    }
    return;
  }
  if (ids.length === 1) {
    const objectAABB = getFiniteAABB(viewer.scene.objects[ids[0]]?.aabb);
    if (objectAABB) {
      jumpViewerToAABB(viewer, objectAABB);
    }
    return;
  }
  const aabb = getFiniteAABB(getObjectsAABB(viewer.scene, ids));
  if (aabb) jumpViewerToAABB(viewer, aabb);
}

function getObjectsAABB(scene: any, objectIds: string[]) {
  let aabb: number[] | null = null;
  objectIds.forEach((id) => {
    const objectAABB = scene.objects[id]?.aabb;
    if (!objectAABB) return;
    if (!aabb) {
      aabb = [...objectAABB];
    } else {
      aabb[0] = Math.min(aabb[0], objectAABB[0]);
      aabb[1] = Math.min(aabb[1], objectAABB[1]);
      aabb[2] = Math.min(aabb[2], objectAABB[2]);
      aabb[3] = Math.max(aabb[3], objectAABB[3]);
      aabb[4] = Math.max(aabb[4], objectAABB[4]);
      aabb[5] = Math.max(aabb[5], objectAABB[5]);
    }
  });
  return aabb;
}

function getFiniteAABB(aabb: ArrayLike<number> | null | undefined): number[] | null {
  if (!aabb || aabb.length !== 6) return null;
  const values = Array.from(aabb, Number);
  if (!values.every(Number.isFinite)) return null;
  if (values[3] <= values[0] || values[4] <= values[1] || values[5] <= values[2]) return null;
  return values;
}

function jumpViewerToAABB(viewer: any, aabb: number[]) {
  viewer.cameraFlight.jumpTo({ aabb, fitFOV: 48 });
  viewer.scene.render(true);
}

function toggleDistanceMeasurement(control: any) {
  if (!control) return;
  if (control.active) {
    control.deactivate?.();
  } else {
    control.activate?.();
  }
}

function clearDistanceMeasurements(control: any, measurements: any) {
  control?.reset?.();
  Object.keys(measurements?.measurements ?? {}).forEach((id) => measurements.destroyMeasurement?.(id));
}

function downloadViewerSnapshot(viewer: any) {
  const dataUrl = typeof viewer.getSnapshot === "function" ? viewer.getSnapshot({ format: "png", includeGizmos: true }) : "";
  if (!dataUrl) return;
  const link = document.createElement("a");
  link.href = dataUrl;
  link.download = `model-viewer-snapshot-${new Date().toISOString().replace(/[:.]/g, "-")}.png`;
  document.body.appendChild(link);
  link.click();
  link.remove();
}

function toggleSectionBox(sectionPlanes: any, sectionBoxRef: any, scene: any, xeokit: any) {
  if (!sectionBoxRef || !xeokit) return;
  sectionPlanes?.clear?.();
  if (sectionBoxRef.current?.active) {
    destroySectionBox(sectionBoxRef);
    return;
  }
  const aabb = scene.aabb;
  if (!aabb) return;
  const mesh = new xeokit.Mesh(scene, {
    id: `model-section-box-${Date.now()}`,
    geometry: new xeokit.ReadableGeometry(scene, xeokit.buildBoxLinesGeometryFromAABB({ aabb })),
    material: new xeokit.PhongMaterial(scene, {
      emissive: [1.0, 0.05, 0.5],
      diffuse: [1.0, 0.18, 0.58],
      lineWidth: 4,
    }),
    clippable: false,
    collidable: false,
    pickable: false,
    isObject: false,
  });
  const faces = createSectionBoxFaces(xeokit, scene, aabb);
  sectionBoxRef.current = { active: true, mesh, faces };
}

function destroySectionBox(sectionBoxRef: any) {
  const current = sectionBoxRef?.current;
  current?.faces?.forEach((face: any) => face.mesh?.destroy?.());
  current?.mesh?.destroy?.();
  if (sectionBoxRef) sectionBoxRef.current = null;
}

function createSectionBoxFaces(xeokit: any, scene: any, aabb: number[]) {
  const [xMin, yMin, zMin, xMax, yMax, zMax] = aabb;
  const dx = Math.max(xMax - xMin, 0.01);
  const dy = Math.max(yMax - yMin, 0.01);
  const dz = Math.max(zMax - zMin, 0.01);
  const cx = (xMin + xMax) / 2;
  const cy = (yMin + yMax) / 2;
  const cz = (zMin + zMax) / 2;
  const thickness = Math.max(Math.max(dx, dy, dz) * 0.003, 0.025);
  const material = new xeokit.PhongMaterial(scene, {
    diffuse: [1.0, 0.18, 0.58],
    emissive: [0.55, 0.02, 0.22],
  });
  return [
    { name: "x-min", pos: [xMin, cy, cz], dir: [1, 0, 0], center: [xMin, cy, cz], size: [thickness, dy / 2, dz / 2] },
    { name: "x-max", pos: [xMax, cy, cz], dir: [-1, 0, 0], center: [xMax, cy, cz], size: [thickness, dy / 2, dz / 2] },
    { name: "y-min", pos: [cx, yMin, cz], dir: [0, 1, 0], center: [cx, yMin, cz], size: [dx / 2, thickness, dz / 2] },
    { name: "y-max", pos: [cx, yMax, cz], dir: [0, -1, 0], center: [cx, yMax, cz], size: [dx / 2, thickness, dz / 2] },
    { name: "z-min", pos: [cx, cy, zMin], dir: [0, 0, 1], center: [cx, cy, zMin], size: [dx / 2, dy / 2, thickness] },
    { name: "z-max", pos: [cx, cy, zMax], dir: [0, 0, -1], center: [cx, cy, zMax], size: [dx / 2, dy / 2, thickness] },
  ].map((face) => ({
    ...face,
    id: `section-box-face-${face.name}`,
    mesh: new xeokit.Mesh(scene, {
      id: `section-box-face-${face.name}`,
      geometry: new xeokit.ReadableGeometry(
        scene,
        xeokit.buildBoxGeometry({ center: face.center, xSize: face.size[0], ySize: face.size[1], zSize: face.size[2] }),
      ),
      material,
      opacity: 0.14,
      clippable: false,
      collidable: false,
      pickable: true,
      isObject: false,
    }),
  }));
}

function activateSectionBoxFace(sectionPlanes: any, sectionBoxRef: any, scene: any, entityId: string) {
  const state = sectionBoxRef?.current;
  const face = state?.faces?.find((item: any) => item.id === entityId);
  if (!sectionPlanes || !face) return false;
  sectionPlanes.clear();
  const plane = sectionPlanes.createSectionPlane({
    id: `section-box-plane-${face.name}`,
    pos: [...face.pos],
    dir: [...face.dir],
    active: false,
  });
  let readyForDrag = false;
  setTimeout(() => {
    readyForDrag = true;
  }, 0);
  plane.on("pos", () => {
    if (!readyForDrag) return;
    plane.active = true;
    scene._needRecompile = true;
    scene.render(true);
  });
  sectionPlanes.showControl?.(plane.id);
  return true;
}

function ModelPropertiesPanel({
  object,
  onClose,
}: {
  object: SelectedObject;
  onClose: () => void;
}) {
  const [propertySearch, setPropertySearch] = useState("");
  const groups = useMemo(() => groupModelProperties(object.properties ?? {}), [object.properties]);
  const query = propertySearch.trim().toLowerCase();
  const visibleGroups = useMemo(
    () =>
      groups
        .map((group) => ({
          ...group,
          entries: group.entries.filter(
            (entry) =>
              !query ||
              group.label.toLowerCase().includes(query) ||
              entry.key.toLowerCase().includes(query) ||
              entry.label.toLowerCase().includes(query) ||
              displayValue(entry.value).toLowerCase().includes(query),
          ),
        }))
        .filter((group) => group.entries.length),
    [groups, query],
  );
  const propertyCount = groups.reduce((total, group) => total + group.entries.length, 0);
  return (
    <aside className="model-properties-panel" aria-label="객체 특성">
      <div className="model-properties-header">
        <div className="model-properties-title">
          <span className="model-properties-type"><Box size={13} />{object.type ?? "Object"}</span>
          <h2>{object.label || object.objectId}</h2>
          <span className="model-properties-id" title={object.objectId}>{object.objectId}</span>
        </div>
        <div className="model-properties-actions">
          <button
            className="icon-only-button"
            type="button"
            onClick={() => void navigator.clipboard?.writeText(object.objectId)}
            aria-label="객체 ID 복사"
            title="객체 ID 복사"
          >
            <Copy size={15} />
          </button>
          <button className="icon-only-button" type="button" onClick={onClose} aria-label="객체 특성 닫기" title="닫기">
            <X size={16} />
          </button>
        </div>
      </div>

      <dl className="model-properties-summary">
        <div><dt>분류</dt><dd>{friendlyIfcCategory(object.type ?? "Object")}</dd></div>
        <div><dt>소스</dt><dd>{object.source ?? "IFC model"}</dd></div>
        <div><dt>속성</dt><dd>{propertyCount.toLocaleString()}개</dd></div>
      </dl>

      <label className="model-property-search">
        <Search size={15} />
        <input
          value={propertySearch}
          placeholder="속성 이름 또는 값 검색"
          onChange={(event) => setPropertySearch(event.target.value)}
        />
        {propertySearch ? (
          <button type="button" onClick={() => setPropertySearch("")} aria-label="속성 검색어 지우기" title="검색어 지우기">
            <X size={13} />
          </button>
        ) : null}
      </label>

      <div className="model-property-groups">
        {visibleGroups.length ? visibleGroups.map((group) => (
          <details className="model-property-group" key={group.id} open>
            <summary>
              <span>{group.label}</span>
              <small>{group.entries.length}</small>
              <ChevronDown size={14} />
            </summary>
            <dl className="model-property-list">
              {group.entries.map((entry) => (
                <div className="model-property-row" key={entry.key}>
                  <dt title={entry.key}>{entry.label}</dt>
                  <dd title={displayValue(entry.value)}><ModelPropertyValue value={entry.value} /></dd>
                  <button
                    type="button"
                    onClick={() => void navigator.clipboard?.writeText(displayValue(entry.value))}
                    aria-label={`${entry.label} 값 복사`}
                    title="값 복사"
                  >
                    <Copy size={12} />
                  </button>
                </div>
              ))}
            </dl>
          </details>
        )) : (
          <div className="model-property-empty">
            <Search size={18} />
            <strong>일치하는 속성이 없습니다.</strong>
          </div>
        )}
      </div>
    </aside>
  );
}

type ModelPropertyEntry = { key: string; label: string; value: unknown };
type ModelPropertyGroup = { id: string; label: string; entries: ModelPropertyEntry[] };

function groupModelProperties(properties: Record<string, unknown>): ModelPropertyGroup[] {
  const groups = new Map<string, ModelPropertyGroup>();
  const ensureGroup = (id: string, label: string) => {
    const current = groups.get(id) ?? { id, label, entries: [] };
    groups.set(id, current);
    return current;
  };

  Object.entries(properties).forEach(([key, value]) => {
    if (value == null || value === "" || ["id", "name", "type"].includes(key)) return;
    let groupId = "general";
    let groupLabel = "기타 속성";
    let propertyLabel = humanizePropertyKey(key);

    if (["originalSystemId", "childCount", "objectCount"].includes(key)) {
      groupId = "identity";
      groupLabel = "기본 정보";
    } else if (key.startsWith("external.")) {
      groupId = "external";
      groupLabel = "외부 데이터";
      propertyLabel = humanizePropertyKey(key.slice("external.".length));
    } else if (key.includes(" - ")) {
      const separator = key.indexOf(" - ");
      const rawGroup = key.slice(0, separator);
      const rawProperty = key.slice(separator + 3);
      groupId = `set:${rawGroup}`;
      groupLabel = humanizePropertyGroup(rawGroup);
      propertyLabel = humanizePropertyKey(rawProperty);
    }

    ensureGroup(groupId, groupLabel).entries.push({ key, label: propertyLabel, value });
  });

  const priority = new Map([["identity", 0], ["external", 90], ["general", 100]]);
  return [...groups.values()].sort(
    (left, right) => (priority.get(left.id) ?? 10) - (priority.get(right.id) ?? 10) || left.label.localeCompare(right.label),
  );
}

function humanizePropertyGroup(value: string) {
  const aliases: Record<string, string> = {
    ProfileProperties: "프로파일",
    BaseQuantities: "기본 수량",
  };
  if (aliases[value]) return aliases[value];
  return value
    .replace(/^Pset_/, "")
    .replace(/Properties$/, "")
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/_/g, " ")
    .trim();
}

function humanizePropertyKey(value: string) {
  const aliases: Record<string, string> = {
    originalSystemId: "원본 시스템 ID",
    childCount: "하위 항목 수",
    objectCount: "객체 수",
  };
  return aliases[value] ?? value.replace(/([a-z0-9])([A-Z])/g, "$1 $2").replace(/_/g, " ");
}

function ModelPropertyValue({ value }: { value: unknown }) {
  if (typeof value === "boolean") {
    return (
      <span className={value ? "model-property-boolean true" : "model-property-boolean false"}>
        {value ? <Check size={12} /> : <Minus size={12} />}
        {value ? "예" : "아니오"}
      </span>
    );
  }
  return <>{displayValue(value)}</>;
}
