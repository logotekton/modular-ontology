from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


WORK_ROOT = Path(__file__).resolve().parents[1]
PACK_SLUG = "advance-steel-samcheok-bldg-b"
PACK_TITLE = "Advance Steel Samcheok Building B Ontology"
PRODUCT = "Advance Steel"
VERSION = "v1"
VENDOR = "Autodesk"
PACK_DIR = WORK_ROOT / f"{PACK_SLUG}-graph-pack"
ZIP_PATH = WORK_ROOT / f"{PACK_SLUG}-graph-pack.zip"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def reset_pack_dir() -> None:
    PACK_DIR.mkdir(parents=True, exist_ok=True)
    for child in PACK_DIR.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def write_zip() -> Path:
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    with zipfile.ZipFile(ZIP_PATH, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in PACK_DIR.rglob("*"):
            if ".git" in path.relative_to(PACK_DIR).parts:
                continue
            if path.is_file():
                archive.write(path, path.relative_to(WORK_ROOT))
    return ZIP_PATH


def edge_id(source: str, relation: str, target: str) -> str:
    digest = hashlib.sha1(f"{source}|{relation}|{target}".encode("utf-8")).hexdigest()[:16]
    return f"edge:{PACK_SLUG}:{digest}"


def node_name(row: dict[str, Any], fallback: str) -> str:
    for key in (
        "name",
        "module_type",
        "module_id",
        "assembly_mark",
        "section_name",
        "material_name",
        "single_part_mark",
        "id",
    ):
        value = row.get(key)
        if value:
            return str(value)
    return fallback


def base_node(node_id: str, labels: list[str], node_type: str, name: str, props: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "id": node_id,
        "labels": labels,
        "name": name,
        "node_type": node_type,
        "pack_slug": PACK_SLUG,
        "pack_title": PACK_TITLE,
        "product": PRODUCT,
        "vendor": VENDOR,
        "version": VERSION,
    }
    for key, value in props.items():
        if key not in payload:
            payload[key] = value
    return payload


def normalize_relation(relation: str) -> str:
    return relation.upper()


def base_edge(source: str, relation: str, target: str, props: dict[str, Any] | None = None) -> dict[str, Any]:
    rel = normalize_relation(relation)
    payload = {
        "id": edge_id(source, rel, target),
        "pack_slug": PACK_SLUG,
        "pack_title": PACK_TITLE,
        "product": PRODUCT,
        "vendor": VENDOR,
        "version": VERSION,
        "relation": rel,
        "source": source,
        "target": target,
    }
    if props:
        payload.update(props)
    return payload


def compact_properties(row: dict[str, Any]) -> str:
    return json.dumps(
        {k: v for k, v in row.items() if k not in {"id", "labels", "node_type", "name", "source", "target", "relation"}},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def write_csvs(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> None:
    with (PACK_DIR / "nodes.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["id", "labels", "node_type", "name", "pack_slug", "product", "version", "properties_json"],
        )
        writer.writeheader()
        for node in nodes:
            writer.writerow(
                {
                    "id": node["id"],
                    "labels": ";".join(node.get("labels", [])),
                    "node_type": node.get("node_type"),
                    "name": node.get("name"),
                    "pack_slug": node.get("pack_slug"),
                    "product": node.get("product"),
                    "version": node.get("version"),
                    "properties_json": compact_properties(node),
                }
            )

    with (PACK_DIR / "edges.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["id", "source", "target", "relation", "pack_slug", "product", "version", "properties_json"],
        )
        writer.writeheader()
        for edge in edges:
            writer.writerow(
                {
                    "id": edge["id"],
                    "source": edge["source"],
                    "target": edge["target"],
                    "relation": edge["relation"],
                    "pack_slug": edge.get("pack_slug"),
                    "product": edge.get("product"),
                    "version": edge.get("version"),
                    "properties_json": compact_properties(edge),
                }
            )


def markdown_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join("" if item is None else str(item) for item in row) + " |")
    return "\n".join(lines)


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def evidence_group_stem(value: str | None) -> str:
    label = value or "unmarked"
    if not value:
        return "unmarked"
    match = re.match(r"[A-Za-z]+", label)
    if match:
        label = match.group(0).upper()
    else:
        label = label[0].upper()
    safe = re.sub(r"[^0-9A-Za-z._-]+", "_", label).strip("._-")
    if not safe:
        safe = "unmarked"
    return safe


def write_assembly_mark_group_docs(evidence_dir: Path, assembly_mark_views: list[dict[str, Any]]) -> None:
    mark_dir = evidence_dir / "assembly_mark_groups"
    mark_dir.mkdir(exist_ok=True)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in assembly_mark_views:
        grouped.setdefault(evidence_group_stem(item.get("assembly_mark")), []).append(item)

    for stem, items in sorted(grouped.items()):
        lines = [
            f"# Assembly Mark Group {stem}",
            "",
            "User-facing evidence grouped by assembly mark prefix. Counts preserve repeated assembly instances; do not collapse modules or identical marks unless the user asks for unique values.",
            "",
            "## Marks",
            "",
        ]

        for item in sorted(items, key=lambda row: (row.get("assembly_mark") or "")):
            assembly_mark = item.get("assembly_mark")
            display_mark = assembly_mark or "unmarked"
            composition_rows = [
                [
                    "main" if part.get("is_main_part") else "attached",
                    part.get("role"),
                    part.get("single_part_mark"),
                    part.get("name"),
                    part.get("part_category"),
                    part.get("material"),
                    part.get("section"),
                    part.get("single_part_count"),
                    round(part.get("weight_kg") or 0, 3),
                ]
                for part in item.get("single_part_composition", [])
            ]
            instance_rows = [
                [
                    instance.get("module_id"),
                    instance.get("module_type"),
                    instance.get("assembly_role"),
                    instance.get("main_part_name"),
                    instance.get("single_part_count"),
                    instance.get("attached_single_part_count"),
                    round(instance.get("total_weight_kg") or 0, 3),
                    compact_json(instance.get("single_parts_summary", [])),
                ]
                for instance in item.get("assembly_instances", [])
            ]

            lines.extend(
                [
                    f"### {display_mark}",
                    "",
                    f"- assembly_mark: {display_mark}",
                    f"- assembly_count: {item.get('assembly_count')}",
                    f"- module_count: {item.get('module_count')}",
                    f"- single_part_count: {item.get('single_part_count')}",
                    f"- attached_single_part_count: {item.get('attached_single_part_count')}",
                    f"- total_weight_kg: {round(item.get('total_weight_kg') or 0, 3)}",
                    f"- module_counts: {compact_json(item.get('module_counts', {}))}",
                    f"- assembly_role_counts: {compact_json(item.get('assembly_role_counts', {}))}",
                    f"- main_part_counts: {compact_json(item.get('main_part_counts', {}))}",
                    "",
                    "Single part composition:",
                    "",
                    markdown_table(
                        [
                            "part_kind",
                            "role",
                            "single_part_mark",
                            "name",
                            "part_category",
                            "material",
                            "section",
                            "single_part_count",
                            "weight_kg",
                        ],
                        composition_rows,
                    ),
                    "",
                    "Assembly instances:",
                    "",
                    markdown_table(
                        [
                            "module_id",
                            "module_type",
                            "assembly_role",
                            "main_part_name",
                            "single_part_count",
                            "attached_single_part_count",
                            "total_weight_kg",
                            "single_parts_summary",
                        ],
                        instance_rows,
                    ),
                    "",
                ]
            )
        (mark_dir / f"{stem}.md").write_text("\n".join(lines), encoding="utf-8")


def build_evidence_docs() -> None:
    evidence_dir = PACK_DIR / "evidence"
    evidence_dir.mkdir(exist_ok=True)

    modules = read_jsonl(WORK_ROOT / "summary" / "module_summary.jsonl")
    module_types = read_jsonl(WORK_ROOT / "summary" / "module_type_summary.jsonl")
    assemblies = read_jsonl(WORK_ROOT / "summary" / "assembly_summary.jsonl")
    model = read_json(WORK_ROOT / "summary" / "model_summary.json")

    module_rows = [
        [
            item["module_id"],
            item.get("module_type") or ", ".join(item.get("module_types", [])),
            item["assembly_count"],
            item["single_part_count"],
            item["main_single_part_count"],
            item["attached_single_part_count"],
            round(item["total_weight_kg"], 3),
        ]
        for item in sorted(modules, key=lambda row: row["module_id"])
    ]
    (evidence_dir / "module_index.md").write_text(
        "# Module Index\n\n"
        "USER ATTRIBUTE 01 is the module ID. USER ATTRIBUTE 02 is the module type.\n\n"
        + markdown_table(
            [
                "module_id",
                "module_type",
                "assembly_count",
                "single_part_count",
                "main_parts",
                "attached_parts",
                "total_weight_kg",
            ],
            module_rows,
        )
        + "\n",
        encoding="utf-8",
    )

    module_type_rows = [
        [
            item["module_type"],
            item["module_count"],
            item["assembly_count"],
            item["single_part_count"],
            item["main_single_part_count"],
            item["attached_single_part_count"],
            round(item["total_weight_kg"], 3),
            ", ".join(item["modules"]),
        ]
        for item in sorted(module_types, key=lambda row: row["module_type"])
    ]
    (evidence_dir / "module_type_index.md").write_text(
        "# Module Type Index\n\n"
        "Module types are grouped from USER ATTRIBUTE 02.\n\n"
        + markdown_table(
            [
                "module_type",
                "module_count",
                "assembly_count",
                "single_part_count",
                "main_parts",
                "attached_parts",
                "total_weight_kg",
                "modules",
            ],
            module_type_rows,
        )
        + "\n",
        encoding="utf-8",
    )

    top_assemblies = sorted(assemblies, key=lambda row: row["total_weight_kg"], reverse=True)[:80]
    assembly_rows = [
        [
            item["id"],
            item.get("module_id") or ", ".join(item.get("module_ids", [])),
            item.get("module_type") or ", ".join(item.get("module_types", [])),
            item.get("assembly_mark"),
            item.get("assembly_role"),
            item.get("main_part", {}).get("name"),
            item["single_part_count"],
            item["attached_single_part_count"],
            round(item["total_weight_kg"], 3),
        ]
        for item in top_assemblies
    ]
    (evidence_dir / "assembly_index_top80.md").write_text(
        "# Assembly Index Top 80 By Weight\n\n"
        "Assemblies are grouped by `m_pMainPartEx.m_nID`. This file lists the heaviest assemblies for quick evidence retrieval.\n\n"
        + markdown_table(
            [
                "assembly_id",
                "module_id",
                "module_type",
                "assembly_mark",
                "assembly_role",
                "main_part",
                "single_parts",
                "attached_parts",
                "total_weight_kg",
            ],
            assembly_rows,
        )
        + "\n",
        encoding="utf-8",
    )

    user_views = WORK_ROOT / "user_views"
    module_user_views = read_jsonl(user_views / "module_user_view.jsonl")
    assembly_mark_views = read_jsonl(user_views / "assembly_mark_summary_view.jsonl")
    profile_user_views = read_jsonl(user_views / "module_profile_summary_view.jsonl")
    material_user_views = read_jsonl(user_views / "module_material_summary_view.jsonl")
    fastener_summary = read_json(WORK_ROOT / "summary" / "fastener_summary.json")

    module_user_rows = [
        [
            item["module_id"],
            item.get("module_type"),
            item["assembly_count"],
            item["single_part_count"],
            round(item["total_weight_kg"], 3),
            ", ".join(item.get("assembly_marks", [])),
            json.dumps(item.get("assembly_mark_counts", {}), ensure_ascii=False),
            item.get("answer_summary"),
        ]
        for item in sorted(module_user_views, key=lambda row: row["module_id"])
    ]
    (evidence_dir / "module_user_view_index.md").write_text(
        "# Module User View Index\n\n"
        "User-facing module summaries. Prefer this evidence for questions about module lists, module weight, and assembly marks.\n\n"
        + markdown_table(
            [
                "module_id",
                "module_type",
                "assembly_count",
                "single_part_count",
                "total_weight_kg",
                "assembly_marks",
                "assembly_mark_counts",
                "answer_summary",
            ],
            module_user_rows,
        )
        + "\n",
        encoding="utf-8",
    )

    assembly_mark_rows = [
        [
            item.get("assembly_mark"),
            item.get("assembly_count"),
            item.get("module_count"),
            round(item.get("total_weight_kg") or 0, 3),
            json.dumps(item.get("module_counts", {}), ensure_ascii=False),
            json.dumps(item.get("main_part_counts", {}), ensure_ascii=False),
            json.dumps(item.get("single_part_composition", []), ensure_ascii=False),
        ]
        for item in sorted(assembly_mark_views, key=lambda row: (row.get("assembly_mark") or ""))
    ]
    (evidence_dir / "assembly_mark_user_view_index.md").write_text(
        "# Assembly Mark User View Index\n\n"
        "User-facing assembly mark summaries. This evidence preserves repeated assembly instances and mark counts; do not treat assembly mark lists as unique-only counts. For exact mark questions, prefer the prefix-grouped files in `evidence/assembly_mark_groups/`.\n\n"
        + markdown_table(
            [
                "assembly_mark",
                "assembly_count",
                "module_count",
                "total_weight_kg",
                "module_counts",
                "main_part_counts",
                "single_part_composition",
            ],
            assembly_mark_rows,
        )
        + "\n",
        encoding="utf-8",
    )
    write_assembly_mark_group_docs(evidence_dir, assembly_mark_views)

    profile_user_rows = [
        [
            item["module_id"],
            item.get("profile_group"),
            item.get("profile_name_ko"),
            ", ".join(item.get("profile_aliases_ko", [])),
            item["single_part_count"],
            round(item["total_weight_kg"], 3),
            item.get("answer_summary"),
        ]
        for item in sorted(profile_user_views, key=lambda row: (row["module_id"], row.get("profile_group") or ""))
    ]
    (evidence_dir / "module_profile_user_view_index.md").write_text(
        "# Module Profile User View Index\n\n"
        "User-facing profile summaries by module. Prefer this evidence for H형강, 씨찬넬, 찬넬, ㄷ형강, 앵글, 각관, and 플레이트 weight questions.\n\n"
        + markdown_table(
            [
                "module_id",
                "profile_group",
                "profile_name_ko",
                "aliases",
                "single_part_count",
                "total_weight_kg",
                "answer_summary",
            ],
            profile_user_rows,
        )
        + "\n",
        encoding="utf-8",
    )

    material_user_rows = [
        [
            item["module_id"],
            item.get("material_name"),
            item["single_part_count"],
            round(item["total_weight_kg"], 3),
            item.get("answer_summary"),
        ]
        for item in sorted(material_user_views, key=lambda row: (row["module_id"], row.get("material_name") or ""))
    ]
    (evidence_dir / "module_material_user_view_index.md").write_text(
        "# Module Material User View Index\n\n"
        "User-facing material summaries by module. Prefer this evidence for material weight questions.\n\n"
        + markdown_table(
            ["module_id", "material_name", "single_part_count", "total_weight_kg", "answer_summary"],
            material_user_rows,
        )
        + "\n",
        encoding="utf-8",
    )

    fastener_lines = [
        "# Model Fastener User View Index",
        "",
        "User-facing bolt and anchor summaries. Bolt quantity is verified by CGREXBolt object count and CGREXBoltPattern grid quantity. Anchor quantity uses CGREXAnchorPattern locations.",
        "",
        "## Totals",
        "",
        f"- bolt_quantity: {fastener_summary['bolt_total_quantity_from_patterns']}",
        f"- bolt_object_count: {fastener_summary['bolt_object_count']}",
        f"- bolt_pattern_count: {fastener_summary['bolt_pattern_count']}",
        f"- anchor_quantity: {fastener_summary['anchor_total_quantity']}",
        f"- anchor_pattern_count: {fastener_summary['anchor_pattern_count']}",
        "",
        "## Bolt By Spec",
        "",
        markdown_table(
            ["description", "grade", "standard", "quantity"],
            [
                [item.get("description"), item.get("grade"), item.get("standard"), item.get("quantity")]
                for item in fastener_summary.get("bolt_patterns_by_spec", [])
            ],
        ),
        "",
        "## Anchor By Spec",
        "",
        markdown_table(
            ["description", "grade", "standard", "quantity"],
            [
                [item.get("description"), item.get("grade"), item.get("standard"), item.get("quantity")]
                for item in fastener_summary.get("anchor_patterns_by_spec", [])
            ],
        ),
    ]
    (evidence_dir / "model_fastener_user_view_index.md").write_text(
        "\n".join(fastener_lines) + "\n",
        encoding="utf-8",
    )

    c5 = next((item for item in assemblies if item["id"] == "as:assembly:248400"), None)
    examples = [
        "# Query Evidence Examples",
        "",
        "This document contains ready-to-retrieve evidence for common questions.",
        "",
        "## Model Totals",
        "",
        f"- module_type_count: {model['module_type_count']}",
        f"- module_count: {model['module_count']}",
        f"- assembly_count: {model['assembly_count']}",
        f"- single_part_count: {model['single_part_count']}",
        f"- total_weight_kg: {round(model['total_weight_kg'], 3)}",
        "",
        "## Example Questions",
        "",
        "- 모듈번호 목록 보여줘",
        "- 1-05-V 모듈의 assembly 목록과 single part 수를 보여줘",
        "- C5 assembly에 붙은 plate 목록과 중량을 보여줘",
        "- 모듈별 assembly 개수와 single part 개수를 비교해줘",
        "- TYPE-1V의 총 중량과 module 목록을 보여줘",
        "- End Plate가 가장 많은 모듈을 찾아줘",
        "",
    ]
    if c5:
        examples.extend(
            [
                "## C5 Assembly Example",
                "",
                f"- assembly_id: {c5['id']}",
                f"- module_id: {c5.get('module_id')}",
                f"- module_type: {c5.get('module_type')}",
                f"- main_part: {c5.get('main_part', {}).get('name')}",
                f"- single_part_count: {c5['single_part_count']}",
                f"- attached_single_part_count: {c5['attached_single_part_count']}",
                f"- total_weight_kg: {round(c5['total_weight_kg'], 3)}",
                "",
                "Attached parts summary:",
                "",
            ]
        )
        for part in c5.get("attached_parts_summary", []):
            examples.append(
                f"- {part.get('role')}: {part.get('single_part_mark')} / {part.get('name')} / "
                f"count {part.get('single_part_count')} / weight_kg {round(part.get('weight_kg', 0), 3)}"
            )
    (evidence_dir / "query_examples.md").write_text("\n".join(examples) + "\n", encoding="utf-8")


def build_nodes() -> list[dict[str, Any]]:
    jsonl = WORK_ROOT / "jsonl"
    summary = read_json(WORK_ROOT / "summary" / "model_summary.json")
    nodes: list[dict[str, Any]] = [
        base_node(
            f"pack:{PACK_SLUG}",
            ["OntologyPack", "AdvanceSteelPack"],
            "OntologyPack",
            PACK_TITLE,
            {
                "description": "OpenCrab graph pack generated from an Advance Steel XML export for Samcheok Building B.",
                "hierarchy": ["ModuleType", "Module", "Assembly", "SinglePart"],
                "source_file": summary.get("source_file"),
                "module_type_count": summary.get("module_type_count"),
                "module_count": summary.get("module_count"),
                "assembly_count": summary.get("assembly_count"),
                "single_part_count": summary.get("single_part_count"),
                "total_weight_kg": summary.get("total_weight_kg"),
            },
        )
    ]

    for row in read_jsonl(jsonl / "module_types.jsonl"):
        nodes.append(base_node(row["id"], ["ModuleType", "AdvanceSteelModuleType"], "ModuleType", node_name(row, row["id"]), row))

    for row in read_jsonl(jsonl / "modules.jsonl"):
        nodes.append(base_node(row["id"], ["Module", "AdvanceSteelModule"], "Module", node_name(row, row["id"]), row))

    for row in read_jsonl(jsonl / "assemblies.jsonl"):
        name = f"{row.get('assembly_mark') or row['id']} {row.get('assembly_role') or ''}".strip()
        nodes.append(base_node(row["id"], ["Assembly", "AdvanceSteelAssembly"], "Assembly", name, row))

    for row in read_jsonl(jsonl / "single_parts.jsonl"):
        labels = ["SinglePart", "AdvanceSteelSinglePart"]
        category = row.get("part_category")
        if category:
            labels.append(str(category))
        name = f"{row.get('mark') or ''} {row.get('single_part_mark') or ''} {row.get('name') or row['id']}".strip()
        nodes.append(base_node(row["id"], labels, "SinglePart", name, row))

    for row in read_jsonl(jsonl / "sections.jsonl"):
        nodes.append(base_node(row["id"], ["Section", "AdvanceSteelSection"], "Section", node_name(row, row["id"]), row))

    for row in read_jsonl(jsonl / "materials.jsonl"):
        nodes.append(base_node(row["id"], ["Material", "AdvanceSteelMaterial"], "Material", node_name(row, row["id"]), row))

    fastener_path = jsonl / "fastener_patterns.jsonl"
    if fastener_path.exists():
        for row in read_jsonl(fastener_path):
            labels = ["FastenerPattern", "AdvanceSteelFastenerPattern"]
            fastener_type = row.get("fastener_type")
            if fastener_type:
                labels.append(f"{fastener_type}Pattern")
            name = f"{row.get('fastener_type') or 'Fastener'} {row.get('description') or ''} x{row.get('quantity')}".strip()
            nodes.append(base_node(row["id"], labels, "FastenerPattern", name, row))

    user_views = WORK_ROOT / "user_views"
    user_view_files = {
        "module_user_view.jsonl": ("ModuleUserView", ["UserView", "ModuleUserView", "AdvanceSteelUserView"]),
        "assembly_user_view.jsonl": ("AssemblyUserView", ["UserView", "AssemblyUserView", "AdvanceSteelUserView"]),
        "assembly_mark_summary_view.jsonl": (
            "AssemblyMarkSummaryView",
            ["UserView", "AssemblyMarkSummaryView", "AdvanceSteelUserView"],
        ),
        "module_profile_summary_view.jsonl": (
            "ModuleProfileSummaryView",
            ["UserView", "ModuleProfileSummaryView", "AdvanceSteelUserView"],
        ),
        "module_material_summary_view.jsonl": (
            "ModuleMaterialSummaryView",
            ["UserView", "ModuleMaterialSummaryView", "AdvanceSteelUserView"],
        ),
        "model_fastener_user_view.jsonl": (
            "ModelFastenerUserView",
            ["UserView", "ModelFastenerUserView", "AdvanceSteelUserView"],
        ),
    }
    for filename, (node_type, labels) in user_view_files.items():
        path = user_views / filename
        if not path.exists():
            continue
        for row in read_jsonl(path):
            nodes.append(base_node(row["id"], labels, node_type, node_name(row, row["id"]), row))

    alias_path = user_views / "query_aliases.json"
    if alias_path.exists():
        aliases = read_json(alias_path)
        for canonical, terms in aliases.get("aliases", {}).items():
            node_id = f"view:query_alias:{canonical.replace(' ', '_')}"
            nodes.append(
                base_node(
                    node_id,
                    ["UserView", "QueryAlias", "AdvanceSteelUserView"],
                    "QueryAlias",
                    canonical,
                    {
                        "id": node_id,
                        "canonical_term": canonical,
                        "aliases": terms,
                        "field": "profile_group",
                    },
                )
            )

    return nodes


def build_edges(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    raw_edges = read_jsonl(WORK_ROOT / "jsonl" / "edges.jsonl")
    edges = [base_edge(row["from"], row["relation"], row["to"], {"source_edge_id": row.get("id")}) for row in raw_edges]

    pack_id = f"pack:{PACK_SLUG}"
    for node in nodes:
        if node["id"] == pack_id:
            continue
        node_type = node.get("node_type")
        if node_type == "ModuleType":
            edges.append(base_edge(pack_id, "HAS_MODULE_TYPE", node["id"]))
        elif node_type == "Material":
            edges.append(base_edge(pack_id, "HAS_MATERIAL", node["id"]))
        elif node_type == "Section":
            edges.append(base_edge(pack_id, "HAS_SECTION", node["id"]))
        elif node_type and str(node_type).endswith("UserView"):
            edges.append(base_edge(pack_id, "HAS_USER_VIEW", node["id"]))
            module_id = node.get("module_id")
            if module_id:
                edges.append(base_edge(node["id"], "SUMMARIZES_MODULE", f"as:module:{module_id}"))
            backing_assembly_id = node.get("backing_assembly_id")
            if backing_assembly_id:
                edges.append(base_edge(node["id"], "USER_VIEW_OF", backing_assembly_id))
        elif node_type == "QueryAlias":
            edges.append(base_edge(pack_id, "HAS_QUERY_ALIAS", node["id"]))

    deduped: dict[str, dict[str, Any]] = {}
    for edge in edges:
        deduped[edge["id"]] = edge
    return list(deduped.values())


def build_pack() -> Path:
    reset_pack_dir()

    nodes = build_nodes()
    edges = build_edges(nodes)
    write_jsonl(PACK_DIR / "nodes.jsonl", nodes)
    write_jsonl(PACK_DIR / "edges.jsonl", edges)
    write_csvs(nodes, edges)

    shutil.copy2(WORK_ROOT / "ingest_manifest.json", PACK_DIR / "ingest_manifest.json")
    shutil.copy2(WORK_ROOT / "summary" / "model_summary.json", PACK_DIR / "model_summary.json")
    shutil.copy2(WORK_ROOT / "README.md", PACK_DIR / "SOURCE_README.md")
    shutil.copytree(WORK_ROOT / "user_views", PACK_DIR / "user_views")
    build_evidence_docs()

    manifest = {
        "pack_slug": PACK_SLUG,
        "pack_title": PACK_TITLE,
        "product": PRODUCT,
        "vendor": VENDOR,
        "version": VERSION,
        "category": "bim",
        "source": "Advance Steel XML export",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "node_count": len(nodes),
        "edge_count": len(edges),
        "meets_minimum_500_nodes": len(nodes) >= 500,
        "hierarchy": ["ModuleType", "Module", "Assembly", "SinglePart"],
        "files": {
            "nodes_jsonl": "nodes.jsonl",
            "edges_jsonl": "edges.jsonl",
            "nodes_csv": "nodes.csv",
            "edges_csv": "edges.csv",
            "ingest_manifest": "ingest_manifest.json",
            "model_summary": "model_summary.json",
            "user_views": "user_views/",
            "evidence_module_index": "evidence/module_index.md",
            "evidence_module_type_index": "evidence/module_type_index.md",
            "evidence_assembly_index_top80": "evidence/assembly_index_top80.md",
            "evidence_module_user_view_index": "evidence/module_user_view_index.md",
            "evidence_assembly_mark_user_view_index": "evidence/assembly_mark_user_view_index.md",
            "evidence_assembly_mark_groups": "evidence/assembly_mark_groups/",
            "evidence_module_profile_user_view_index": "evidence/module_profile_user_view_index.md",
            "evidence_module_material_user_view_index": "evidence/module_material_user_view_index.md",
            "evidence_model_fastener_user_view_index": "evidence/model_fastener_user_view_index.md",
            "evidence_query_examples": "evidence/query_examples.md",
        },
    }
    write_json(PACK_DIR / "manifest.json", manifest)

    readme = f"""# {PACK_TITLE} Graph Pack

OpenCrab ingest-ready graph pack generated from the Advance Steel XML export.

## Contents

- `manifest.json`: pack metadata and file map.
- `nodes.jsonl` / `edges.jsonl`: OpenCrab-style graph records.
- `nodes.csv` / `edges.csv`: optional table export for tools that prefer CSV.
- `ingest_manifest.json`: source normalization metadata.
- `model_summary.json`: top-level quantity and quality summary.
- `user_views/`: user-facing module, assembly, profile, material, and query-alias views.
- `evidence/*.md`: GitHub-ingest-friendly evidence summaries for module, module type, assembly, and query examples.
- `evidence/assembly_mark_groups/*.md`: assembly mark evidence grouped by prefix for exact mark lookup such as G20 without excessive file count.

## Hierarchy

```text
ModuleType -> Module -> Assembly -> SinglePart
SinglePart -> Material
SinglePart -> Section
```

## Snapshot

- Nodes: {len(nodes)}
- Edges: {len(edges)}
- Module types: {manifest['hierarchy'][0]} records are grouped from USER ATTRIBUTE 02.
- Modules: grouped from USER ATTRIBUTE 01.

Cypher files are intentionally omitted because this package targets OpenCrab ingest, not direct Neo4j import.
"""
    (PACK_DIR / "README.md").write_text(readme, encoding="utf-8")

    return write_zip()


def main() -> None:
    path = build_pack()
    manifest = read_json(PACK_DIR / "manifest.json")
    print(json.dumps({"zip": str(path), "manifest": manifest}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
