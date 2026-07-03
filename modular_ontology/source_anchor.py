from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "anchor/0.1"

ANCHOR_TYPES = {
    "revit_element",
    "revit_sheet",
    "revit_view",
    "revit_schedule_row",
    "boq_sheet_row",
    "dxf_entity",
    "pdf_page",
    "unknown",
}

CONFIDENCE_LEVELS = {"exact", "partial", "unresolvable"}
SOURCE_KINDS = {"revit_model", "boq", "dxf", "pdf", "unknown"}

_REVIT_ELEMENT_NODE_RE = re.compile(r"(?:^|:)element:(?P<element_id>-?\d+)(?:$|:)", re.IGNORECASE)
_REVIT_SHEET_NODE_RE = re.compile(r"(?:^|:)sheet:(?P<sheet_id>-?\d+)(?:$|:)", re.IGNORECASE)
_REVIT_VIEW_NODE_RE = re.compile(r"(?:^|:)view:(?P<view_id>-?\d+)(?:$|:)", re.IGNORECASE)


@dataclass(frozen=True)
class SourceAnchor:
    anchor_type: str
    source_kind: str
    document_key: str | None = None
    ids: dict[str, Any] = field(default_factory=dict)
    confidence: str = "unresolvable"
    raw: dict[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.anchor_type not in ANCHOR_TYPES:
            raise ValueError(f"Unsupported anchor_type: {self.anchor_type}")
        if self.source_kind not in SOURCE_KINDS:
            raise ValueError(f"Unsupported source_kind: {self.source_kind}")
        if self.confidence not in CONFIDENCE_LEVELS:
            raise ValueError(f"Unsupported confidence: {self.confidence}")

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "anchor_type": self.anchor_type,
            "source_kind": self.source_kind,
            "document_key": self.document_key,
            "ids": dict(self.ids),
            "confidence": self.confidence,
            "open_hint": open_hint(self),
        }
        if self.raw:
            payload["raw"] = dict(self.raw)
        return payload


def anchors_from_chunk(chunk: Mapping[str, Any]) -> list[SourceAnchor]:
    candidates: list[SourceAnchor] = []
    candidates.extend(anchors_from_node_properties(chunk, include_unknown=False))

    metadata = _mapping(chunk.get("metadata"))
    if metadata:
        candidates.extend(anchors_from_node_properties(metadata, include_unknown=False))
        candidates.extend(anchors_from_node_properties(_mapping(metadata.get("compact_row")), include_unknown=False))
        candidates.extend(anchors_from_node_properties(_mapping(metadata.get("join_keys")), include_unknown=False))
        candidates.extend(_anchors_from_source_refs(_list_of_mappings(metadata.get("source_refs"))))

    candidates.extend(_anchors_from_source_refs(_list_of_mappings(chunk.get("source_refs"))))
    if not candidates:
        candidates.extend(_legacy_revit_element_anchors_from_text(chunk))
    return _dedupe_or_unknown(candidates, chunk)


def anchors_from_node_properties(
    props: Mapping[str, Any] | None,
    *,
    include_unknown: bool = True,
) -> list[SourceAnchor]:
    if not props:
        return _unknown_list(props) if include_unknown else []

    anchors: list[SourceAnchor] = []
    anchors.extend(_revit_element_anchors(props))
    anchors.extend(_boq_sheet_row_anchors(props))
    anchors.extend(_revit_sheet_anchors(props))
    anchors.extend(_revit_view_anchors(props))
    anchors.extend(_revit_schedule_row_anchors(props))
    anchors.extend(_dxf_entity_anchors(props))
    anchors.extend(_pdf_page_anchors(props))
    anchors.extend(_anchors_from_source_refs(_list_of_mappings(props.get("source_refs"))))

    if anchors:
        return _dedupe(anchors)
    return _unknown_list(props) if include_unknown else []


def open_hint(anchor: SourceAnchor | Mapping[str, Any]) -> str:
    anchor_type, ids, document_key = _anchor_parts(anchor)
    if anchor_type == "revit_element":
        unique_id = _first_text(ids.get("unique_id"))
        element_id = _first_text(ids.get("element_id"))
        if unique_id:
            return f"Revit > Add-in > UniqueId로 선택: {unique_id}"
        if element_id:
            return f"Revit > 관리 > ID로 선택: {element_id}"
        return "Revit > 요소 앵커는 있으나 선택 ID가 없습니다."
    if anchor_type == "revit_sheet":
        sheet_number = _first_text(ids.get("sheet_number"))
        sheet_id = _first_text(ids.get("sheet_id"))
        return f"Revit > 시트 열기: {sheet_number or sheet_id or document_key or '알 수 없음'}"
    if anchor_type == "revit_view":
        view_name = _first_text(ids.get("view_name"))
        view_id = _first_text(ids.get("view_id"))
        return f"Revit > 뷰 열기: {view_name or view_id or document_key or '알 수 없음'}"
    if anchor_type == "revit_schedule_row":
        schedule_name = _first_text(ids.get("schedule_name"))
        row = _first_text(ids.get("row"))
        cell = _first_text(ids.get("cell"))
        section = _first_text(ids.get("section"))
        location = f"row {row}" if row else "행 미상"
        if cell:
            location = f"{location}, cell {cell}"
        if section:
            location = f"{section} > {location}"
        return f"Revit > 일람표 확인: {schedule_name or document_key or '일람표 미상'} > {location}"
    if anchor_type == "boq_sheet_row":
        sheet = _first_text(ids.get("source_sheet"))
        row = _first_text(ids.get("source_row"))
        return f"BOQ/산출근거 > {sheet or document_key or '시트 미상'} > row {row or '미상'}"
    if anchor_type == "dxf_entity":
        source_file = _first_text(ids.get("source_file"))
        handle = _first_text(_first_present(ids.get("handle"), ids.get("entity_key"), ids.get("entity_id")))
        layer = _first_text(ids.get("layer"))
        suffix = f" > entity {handle}" if handle else ""
        if layer:
            suffix += f" > layer {layer}"
        return f"DXF > {source_file or document_key or '파일 미상'}{suffix}"
    if anchor_type == "pdf_page":
        source_file = _first_text(ids.get("source_file"))
        page = _first_text(ids.get("page"))
        return f"PDF > {source_file or document_key or '파일 미상'} > page {page or '미상'}"
    return "원본 앵커 없음: 원본 파일/행/요소로 되돌아갈 수 있는 식별자가 부족합니다."


def coverage(anchors: Iterable[SourceAnchor | Mapping[str, Any]]) -> dict[str, Any]:
    rows = list(anchors)
    by_type = {anchor_type: 0 for anchor_type in sorted(ANCHOR_TYPES)}
    by_confidence = {confidence: 0 for confidence in sorted(CONFIDENCE_LEVELS)}
    for anchor in rows:
        anchor_type, _, _ = _anchor_parts(anchor)
        confidence = anchor.confidence if isinstance(anchor, SourceAnchor) else str(anchor.get("confidence") or "unresolvable")
        by_type[anchor_type if anchor_type in ANCHOR_TYPES else "unknown"] += 1
        by_confidence[confidence if confidence in CONFIDENCE_LEVELS else "unresolvable"] += 1

    total = len(rows)
    resolvable = by_confidence["exact"] + by_confidence["partial"]
    return {
        "total": total,
        "resolvable": resolvable,
        "unresolvable": by_confidence["unresolvable"],
        "exact": by_confidence["exact"],
        "partial": by_confidence["partial"],
        "resolvable_rate": round(resolvable / total, 4) if total else 0.0,
        "exact_rate": round(by_confidence["exact"] / total, 4) if total else 0.0,
        "by_type": by_type,
        "by_confidence": by_confidence,
    }


def _revit_element_anchors(props: Mapping[str, Any]) -> list[SourceAnchor]:
    source_node_id = _first_text(props.get("source_node_id") or props.get("node_id"))
    element_id = _first_int(
        props.get("source_element_id")
        or props.get("element_id")
        or props.get("revit_element_id")
        or _match_group(_REVIT_ELEMENT_NODE_RE, source_node_id, "element_id")
    )
    unique_id = _first_text(props.get("unique_id") or props.get("element_unique_id") or props.get("source_unique_id"))
    ifc_guid = _first_text(props.get("ifc_guid") or props.get("ifc_global_id"))

    if element_id is None and not unique_id and not ifc_guid:
        return []

    ids = {
        "element_id": element_id,
        "unique_id": unique_id,
        "ifc_guid": ifc_guid,
        "category": _first_text(props.get("category") or props.get("category_name")),
        "workset_name": _first_text(props.get("workset_name")),
        "source_node_id": source_node_id,
    }
    return [
        SourceAnchor(
            anchor_type="revit_element",
            source_kind="revit_model",
            document_key=_document_key(props),
            ids=_drop_empty(ids),
            confidence="exact" if unique_id else "partial",
            raw=_raw_excerpt(props),
        )
    ]


def _boq_sheet_row_anchors(props: Mapping[str, Any]) -> list[SourceAnchor]:
    source_sheet = _first_text(props.get("source_sheet"))
    source_row = _first_int(props.get("source_row"))
    if not source_sheet and source_row is None:
        return []
    ids = {
        "source_sheet": source_sheet,
        "source_row": source_row,
        "source_file": _first_text(props.get("source_file")),
        "item_name": _first_text(props.get("item_name")),
        "module_type": _first_text(props.get("module_type")),
    }
    return [
        SourceAnchor(
            anchor_type="boq_sheet_row",
            source_kind="boq",
            document_key=source_sheet or _document_key(props),
            ids=_drop_empty(ids),
            confidence="exact" if source_sheet and source_row is not None else "partial",
            raw=_raw_excerpt(props),
        )
    ]


def _revit_sheet_anchors(props: Mapping[str, Any]) -> list[SourceAnchor]:
    source_node_id = _first_text(props.get("source_node_id") or props.get("node_id"))
    record_type = _first_text(props.get("record_type"))
    explicit_sheet = any(
        key in props
        for key in (
            "source_sheet_id",
            "sheet_id",
            "sheet_number",
            "source_sheet_number",
            "sheet_unique_id",
        )
    ) or bool(_match_group(_REVIT_SHEET_NODE_RE, source_node_id, "sheet_id"))
    if not explicit_sheet and record_type != "sheet":
        return []
    fallback_element_id = props.get("element_id") if record_type == "sheet" else None
    fallback_unique_id = props.get("unique_id") if record_type == "sheet" else None
    sheet_id = _first_int(
        props.get("source_sheet_id")
        or props.get("sheet_id")
        or fallback_element_id
        or _match_group(_REVIT_SHEET_NODE_RE, source_node_id, "sheet_id")
    )
    sheet_number = _first_text(props.get("sheet_number") or props.get("source_sheet_number"))
    sheet_unique_id = _first_text(props.get("sheet_unique_id") or fallback_unique_id)
    if sheet_id is None and not sheet_number and not sheet_unique_id:
        return []
    ids = {
        "sheet_id": sheet_id,
        "sheet_unique_id": sheet_unique_id,
        "sheet_number": sheet_number,
        "sheet_name": _first_text(props.get("sheet_name") or props.get("name")),
        "source_node_id": source_node_id,
    }
    return [
        SourceAnchor(
            anchor_type="revit_sheet",
            source_kind="revit_model",
            document_key=_document_key(props),
            ids=_drop_empty(ids),
            confidence="exact" if sheet_unique_id else "partial",
            raw=_raw_excerpt(props),
        )
    ]


def _revit_view_anchors(props: Mapping[str, Any]) -> list[SourceAnchor]:
    source_node_id = _first_text(props.get("source_node_id") or props.get("node_id"))
    record_type = _first_text(props.get("record_type"))
    explicit_view = any(
        key in props
        for key in (
            "view_id",
            "source_view_id",
            "view_unique_id",
            "view_name",
            "view_type",
        )
    ) or bool(_match_group(_REVIT_VIEW_NODE_RE, source_node_id, "view_id"))
    if not explicit_view and record_type != "view":
        return []
    fallback_element_id = props.get("element_id") if record_type == "view" else None
    fallback_unique_id = props.get("unique_id") if record_type == "view" else None
    view_id = _first_int(
        props.get("view_id")
        or props.get("source_view_id")
        or fallback_element_id
        or _match_group(_REVIT_VIEW_NODE_RE, source_node_id, "view_id")
    )
    view_unique_id = _first_text(props.get("view_unique_id") or fallback_unique_id)
    view_name = _first_text(props.get("view_name") or props.get("name"))
    if view_id is None and not view_unique_id and not view_name:
        return []
    ids = {
        "view_id": view_id,
        "view_unique_id": view_unique_id,
        "view_name": view_name,
        "view_type": _first_text(props.get("view_type")),
        "source_node_id": source_node_id,
    }
    return [
        SourceAnchor(
            anchor_type="revit_view",
            source_kind="revit_model",
            document_key=_document_key(props),
            ids=_drop_empty(ids),
            confidence="exact" if view_unique_id else "partial",
            raw=_raw_excerpt(props),
        )
    ]


def _revit_schedule_row_anchors(props: Mapping[str, Any]) -> list[SourceAnchor]:
    schedule_unique_id = _first_text(props.get("schedule_unique_id"))
    schedule_id = _first_int(_first_present(props.get("schedule_id"), props.get("source_schedule_id")))
    row = _first_int(_first_present(props.get("row"), props.get("row_index"), props.get("source_row_index")))
    cell = _first_int(_first_present(props.get("column"), props.get("column_index"), props.get("cell_index")))
    if not schedule_unique_id and schedule_id is None and row is None:
        return []
    ids = {
        "schedule_id": schedule_id,
        "schedule_unique_id": schedule_unique_id,
        "schedule_name": _first_text(props.get("schedule_name") or props.get("name")),
        "section": _first_text(props.get("section") or props.get("schedule_section")),
        "row": row,
        "cell": cell,
    }
    return [
        SourceAnchor(
            anchor_type="revit_schedule_row",
            source_kind="revit_model",
            document_key=_document_key(props),
            ids=_drop_empty(ids),
            confidence="exact" if schedule_unique_id and row is not None else "partial",
            raw=_raw_excerpt(props),
        )
    ]


def _dxf_entity_anchors(props: Mapping[str, Any]) -> list[SourceAnchor]:
    source_file = _first_text(_first_present(props.get("dxf_file_name"), props.get("dxf_file"), props.get("source_file")))
    entity_type = _first_text(props.get("entity_type"))
    ai_category = _first_text(props.get("ai_category") or props.get("evidence_type"))
    handle = _first_text(_first_present(props.get("handle"), props.get("entity_handle"), props.get("entity_id")))
    entity_key = _first_text(_first_present(props.get("entity_key"), props.get("source_entity_key")))
    locator = handle or entity_key
    if not locator and ai_category != "dxf_entity" and entity_type not in {"LINE", "LWPOLYLINE", "TEXT", "MTEXT", "DIMENSION"}:
        return []
    ids = {
        "source_file": source_file,
        "source_jsonl": _first_text(props.get("source_jsonl")),
        "sheet_no": _first_text(props.get("sheet_no") or props.get("sheet_number")),
        "entity_type": entity_type,
        "handle": handle,
        "entity_key": entity_key,
        "layer": _first_text(props.get("layer")),
    }
    exact_source = bool(source_file and source_file.lower().endswith(".dxf"))
    return [
        SourceAnchor(
            anchor_type="dxf_entity",
            source_kind="dxf",
            document_key=source_file or _document_key(props),
            ids=_drop_empty(ids),
            confidence="exact" if exact_source and locator else "partial",
            raw=_raw_excerpt(props),
        )
    ]


def _pdf_page_anchors(props: Mapping[str, Any]) -> list[SourceAnchor]:
    source_file = _first_text(props.get("source_file") or props.get("pdf_file"))
    page = _first_int(props.get("page") or props.get("page_number"))
    is_pdf = bool(source_file and source_file.lower().endswith(".pdf")) or "pdf_file" in props
    if not is_pdf:
        return []
    if not source_file or page is None:
        return []
    return [
        SourceAnchor(
            anchor_type="pdf_page",
            source_kind="pdf",
            document_key=source_file,
            ids={"source_file": source_file, "page": page},
            confidence="exact",
            raw=_raw_excerpt(props),
        )
    ]


def _anchors_from_source_refs(refs: list[Mapping[str, Any]]) -> list[SourceAnchor]:
    anchors: list[SourceAnchor] = []
    for ref in refs:
        ref_props = dict(ref)
        ref_props.pop("source_refs", None)
        delegated = anchors_from_node_properties(ref_props, include_unknown=False)
        if delegated:
            anchors.extend(delegated)
            continue

        source_file = _first_text(ref.get("source_file"))
        source_node_id = _first_text(ref.get("source_node_id"))
        element_id = _first_int(_match_group(_REVIT_ELEMENT_NODE_RE, source_node_id, "element_id"))
        if element_id is not None:
            anchors.append(
                SourceAnchor(
                    anchor_type="revit_element",
                    source_kind="revit_model",
                    document_key=_document_key(ref),
                    ids=_drop_empty(
                        {
                            "element_id": element_id,
                            "source_file": source_file,
                            "source_id": _first_text(ref.get("source_id")),
                            "source_node_id": source_node_id,
                            "raw_record_hash": _first_text(ref.get("raw_record_hash")),
                        }
                    ),
                    confidence="partial",
                    raw=_raw_excerpt(ref),
                )
            )
            continue

        sheet_id = _first_int(_match_group(_REVIT_SHEET_NODE_RE, source_node_id, "sheet_id"))
        if sheet_id is not None:
            anchors.append(
                SourceAnchor(
                    anchor_type="revit_sheet",
                    source_kind="revit_model",
                    document_key=_document_key(ref),
                    ids=_drop_empty({"sheet_id": sheet_id, "source_file": source_file, "source_node_id": source_node_id}),
                    confidence="partial",
                    raw=_raw_excerpt(ref),
                )
            )
            continue

        view_id = _first_int(_match_group(_REVIT_VIEW_NODE_RE, source_node_id, "view_id"))
        if view_id is not None:
            anchors.append(
                SourceAnchor(
                    anchor_type="revit_view",
                    source_kind="revit_model",
                    document_key=_document_key(ref),
                    ids=_drop_empty({"view_id": view_id, "source_file": source_file, "source_node_id": source_node_id}),
                    confidence="partial",
                    raw=_raw_excerpt(ref),
                )
            )
            continue

        if source_file and source_file.lower().endswith(".pdf"):
            page = _first_int(ref.get("page") or ref.get("page_number"))
            anchors.append(
                SourceAnchor(
                    anchor_type="pdf_page",
                    source_kind="pdf",
                    document_key=source_file,
                    ids=_drop_empty({"source_file": source_file, "page": page}),
                    confidence="exact" if page is not None else "partial",
                    raw=_raw_excerpt(ref),
                )
            )
    return anchors


def _legacy_revit_element_anchors_from_text(chunk: Mapping[str, Any], limit: int = 20) -> list[SourceAnchor]:
    text = _first_text(chunk.get("text"))
    if not text or "|" not in text:
        return []
    metadata = _mapping(chunk.get("metadata"))
    document_key = _first_text(metadata.get("source_path") or chunk.get("path") or chunk.get("document_id"))
    headers: list[str] = []
    anchors: list[SourceAnchor] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("|") or line.count("|") < 2:
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        lowered = [cell.lower() for cell in cells]
        if any("elementid" in cell.replace(" ", "") or "element id" in cell for cell in lowered):
            headers = cells
            continue
        if not headers or all(set(cell) <= {"-", " "} for cell in cells):
            continue
        if len(cells) != len(headers):
            continue
        if not any(_first_int(cell) is not None for cell in cells):
            headers = []
            continue
        element_col = _header_index(headers, "revit elementid", "revit element id", "elementid", "element id")
        if element_col is None or element_col >= len(cells):
            continue
        element_id = _first_int(cells[element_col])
        if element_id is None:
            continue
        guid_col = _header_index(headers, "ifc guid", "ifc_guid", "guid")
        ifc_guid = _first_text(cells[guid_col]) if guid_col is not None and guid_col < len(cells) else None
        anchors.append(
            SourceAnchor(
                anchor_type="revit_element",
                source_kind="revit_model",
                document_key=document_key,
                ids=_drop_empty(
                    {
                        "element_id": element_id,
                        "ifc_guid": ifc_guid,
                        "source_path": document_key,
                        "text_fallback": "markdown_table",
                    }
                ),
                confidence="partial",
                raw=_raw_excerpt(
                    {
                        **dict(chunk),
                        "anchor_origin": "legacy_markdown_table",
                        "source_path": document_key,
                    }
                ),
            )
        )
        if len(anchors) >= limit:
            break
    return anchors


def _dedupe_or_unknown(anchors: list[SourceAnchor], raw: Mapping[str, Any] | None) -> list[SourceAnchor]:
    deduped = _dedupe(anchors)
    return deduped if deduped else _unknown_list(raw)


def _dedupe(anchors: Iterable[SourceAnchor]) -> list[SourceAnchor]:
    seen: set[tuple[str, str | None, str]] = set()
    deduped: list[SourceAnchor] = []
    for anchor in anchors:
        key = (anchor.anchor_type, anchor.document_key, repr(sorted(anchor.ids.items())))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(anchor)
    return deduped


def _unknown_list(raw: Mapping[str, Any] | None) -> list[SourceAnchor]:
    return [
        SourceAnchor(
            anchor_type="unknown",
            source_kind="unknown",
            document_key=_document_key(raw or {}),
            ids={},
            confidence="unresolvable",
            raw=_raw_excerpt(raw or {}),
        )
    ]


def _anchor_parts(anchor: SourceAnchor | Mapping[str, Any]) -> tuple[str, Mapping[str, Any], str | None]:
    if isinstance(anchor, SourceAnchor):
        return anchor.anchor_type, anchor.ids, anchor.document_key
    anchor_type = str(anchor.get("anchor_type") or "unknown")
    ids = _mapping(anchor.get("ids"))
    document_key = _first_text(anchor.get("document_key"))
    return anchor_type, ids, document_key


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list_of_mappings(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _document_key(props: Mapping[str, Any]) -> str | None:
    return _first_text(
        props.get("document_key")
        or props.get("model_key")
        or props.get("project_key")
        or props.get("source_document")
        or props.get("source_file")
        or props.get("source_path")
        or props.get("path")
    )


def _first_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is None or value == "":
            continue
        return value
    return None


def _first_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"-?\d+", text):
            return int(text)
    return None


def _match_group(pattern: re.Pattern[str], value: str | None, group: str) -> str | None:
    if not value:
        return None
    match = pattern.search(value)
    return match.group(group) if match else None


def _header_index(headers: list[str], *candidates: str) -> int | None:
    normalized = [header.lower().replace("_", " ").replace("-", " ").strip() for header in headers]
    compact = [header.replace(" ", "") for header in normalized]
    for candidate in candidates:
        needle = candidate.lower().replace("_", " ").replace("-", " ").strip()
        needle_compact = needle.replace(" ", "")
        for index, header in enumerate(normalized):
            if header == needle or compact[index] == needle_compact:
                return index
    return None


def _drop_empty(values: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value not in (None, "", [], {})}


def _raw_excerpt(props: Mapping[str, Any]) -> dict[str, Any]:
    excerpt: dict[str, Any] = {}
    for key in (
        "pack_id",
        "chunk_id",
        "document_id",
        "derived_file",
        "row_id",
        "source_refs",
        "source_node_id",
        "source_path",
        "raw_record_hash",
        "anchor_origin",
        "dxf_file_name",
        "entity_key",
        "source_entity_key",
        "source_jsonl",
    ):
        if key in props:
            excerpt[key] = props[key]
    return excerpt
