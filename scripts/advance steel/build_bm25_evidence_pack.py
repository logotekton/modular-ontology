from __future__ import annotations

import json
import shutil
import zipfile
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PACK_SLUG = "advance-steel-samcheok-bldg-b-bm25-evidence-pack"
PACK_DIR = ROOT / PACK_SLUG
ZIP_PATH = ROOT / f"{PACK_SLUG}.zip"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join("" if cell is None else str(cell) for cell in row) + " |")
    return "\n".join(lines)


def safe_name(value: str | None) -> str:
    if not value:
        return "unmarked"
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in value)


def compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def kg(value: Any) -> float:
    return round(value or 0, 3)


def sum_length(row: dict[str, Any]) -> float:
    return round(
        sum((item.get("length_m") or 0) for item in row.get("by_part_category", {}).values()),
        3,
    )


def part_categories(row: dict[str, Any]) -> str:
    return ", ".join(str(key) for key in row.get("by_part_category", {}).keys())


def reset() -> None:
    if PACK_DIR.exists():
        shutil.rmtree(PACK_DIR)
    PACK_DIR.mkdir(parents=True)


def zip_pack() -> Path:
    zip_path = ZIP_PATH
    if ZIP_PATH.exists():
        try:
            ZIP_PATH.unlink()
        except PermissionError:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            zip_path = ROOT / f"{PACK_SLUG}-{stamp}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(PACK_DIR.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(PACK_DIR).as_posix())
    return zip_path


def module_mark_counts(row: dict[str, Any]) -> OrderedDict[str, int]:
    return OrderedDict((str(k), v) for k, v in row.get("assembly_mark_counts", {}).items())


def render_module(row: dict[str, Any]) -> str:
    module_id = row["module_id"]
    instances = row.get("assembly_instances", [])
    instance_rows = [
        [
            i + 1,
            item.get("assembly_mark") or "unmarked",
            item.get("assembly_role") or "",
            item.get("main_part") or "",
            item.get("single_part_count"),
            round(item.get("total_weight_kg") or 0, 3),
        ]
        for i, item in enumerate(instances)
    ]
    return "\n".join(
        [
            f"# Module {module_id}",
            "",
            f"**요약**: {row.get('answer_summary') or ''}",
            "",
            f"module_id: {module_id}",
            f"module_type: {row.get('module_type')}",
            f"assembly_count: {row.get('assembly_count')}",
            f"single_part_count: {row.get('single_part_count')}",
            f"main_single_part_count: {row.get('main_single_part_count')}",
            f"attached_single_part_count: {row.get('attached_single_part_count')}",
            f"total_weight_kg: {round(row.get('total_weight_kg') or 0, 3)}",
            f"total_length_m: {round(row.get('total_length_m') or 0, 3)}",
            "",
            "## Assembly Mark Counts",
            "",
            table(["assembly_mark", "count"], [[mark, count] for mark, count in module_mark_counts(row).items()]),
            "",
            "## Assembly Mark Sequence",
            "",
            ", ".join(str(mark) for mark in row.get("assembly_mark_sequence", [])),
            "",
            "## Assembly Instances",
            "",
            table(["seq", "assembly_mark", "assembly_role", "main_part", "single_part_count", "total_weight_kg"], instance_rows),
            "",
            "## JSON Back Reference",
            "",
            f"- backdata source: `backdata/user_views/module_user_view.jsonl`",
        ]
    )


def render_assembly_mark(row: dict[str, Any]) -> str:
    mark = row.get("assembly_mark") or "unmarked"
    composition = row.get("single_part_composition", [])
    instances = row.get("assembly_instances", [])
    composition_rows = [
        [
            "main" if item.get("is_main_part") else "attached",
            item.get("role") or "",
            item.get("single_part_mark") or "",
            item.get("name") or "",
            item.get("part_category") or "",
            item.get("material") or "",
            item.get("section") or "",
            item.get("single_part_count"),
            round(item.get("weight_kg") or 0, 3),
        ]
        for item in composition
    ]
    instance_rows = [
        [
            i + 1,
            item.get("module_id") or "",
            item.get("module_type") or "",
            item.get("assembly_role") or "",
            item.get("main_part_name") or "",
            item.get("single_part_count"),
            item.get("attached_single_part_count"),
            round(item.get("total_weight_kg") or 0, 3),
        ]
        for i, item in enumerate(instances)
    ]
    # Keep producer-only backing ids out of user-facing evidence tables.
    return "\n".join(
        [
            f"# Assembly Mark {mark}",
            "",
            f"**요약**: {mark} 어셈블리 마크는 전체 {row.get('assembly_count')}개이며, {row.get('module_count')}개 모듈에 사용되고, 총중량은 {kg(row.get('total_weight_kg'))} kg입니다.",
            "",
            f"assembly_mark: {mark}",
            f"assembly_count: {row.get('assembly_count')}",
            f"module_count: {row.get('module_count')}",
            f"single_part_count: {row.get('single_part_count')}",
            f"attached_single_part_count: {row.get('attached_single_part_count')}",
            f"total_weight_kg: {round(row.get('total_weight_kg') or 0, 3)}",
            f"module_counts: {compact(row.get('module_counts', {}))}",
            f"assembly_role_counts: {compact(row.get('assembly_role_counts', {}))}",
            f"main_part_counts: {compact(row.get('main_part_counts', {}))}",
            "",
            "## Single Part Composition",
            "",
            table(
                ["part_kind", "role", "single_part_mark", "name", "part_category", "material", "section", "single_part_count", "weight_kg"],
                composition_rows,
            ),
            "",
            "## Assembly Instances",
            "",
            table(
                ["seq", "module_id", "module_type", "assembly_role", "main_part_name", "single_part_count", "attached_single_part_count", "total_weight_kg"],
                instance_rows,
            ),
            "",
            "## JSON Back Reference",
            "",
            f"- backdata source: `backdata/user_views/assembly_mark_summary_view.jsonl`",
        ]
    )


def assembly_mark_doc_name(mark: str | None) -> str:
    mark_text = mark or "unmarked"
    if mark_text.isdigit():
        number = int(mark_text)
        start = ((number - 1) // 20) * 20 + 1
        end = start + 19
        return f"numeric_{start:02d}_{end:02d}.md"
    return f"{safe_name(mark_text)}.md"


def render_assembly_mark_group(rows: list[dict[str, Any]], doc_name: str) -> str:
    marks = [row.get("assembly_mark") or "unmarked" for row in rows]
    index_rows = [
        [
            row.get("assembly_mark") or "unmarked",
            row.get("assembly_count"),
            row.get("module_count"),
            kg(row.get("total_weight_kg")),
            compact(row.get("module_counts", {})),
        ]
        for row in rows
    ]
    sections: list[str] = [
        f"# Assembly Mark Group {doc_name.removesuffix('.md')}",
        "",
        "어셈블리 마크 그룹 문서입니다. 숫자형 마크는 문서 제한 200개를 넘지 않도록 범위별로 묶었습니다.",
        f"included_assembly_marks: {', '.join(marks)}",
        "",
        "## Mark Index",
        "",
        table(["assembly_mark", "assembly_count", "module_count", "total_weight_kg", "module_counts"], index_rows),
    ]
    for row in rows:
        mark = row.get("assembly_mark") or "unmarked"
        sections.extend(["", "---", "", render_assembly_mark(row)])
    return "\n".join(sections)


def h_beam_rows(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in sections if str(row.get("section_name") or "").startswith("H ")]


def render_section_weight_index(sections: list[dict[str, Any]]) -> str:
    h_rows = h_beam_rows(sections)
    h_count = sum(row.get("single_part_count") or 0 for row in h_rows)
    h_weight = kg(sum(row.get("total_weight_kg") or 0 for row in h_rows))
    h_length = kg(sum(sum_length(row) for row in h_rows))
    rows = [
        [
            row.get("section_name") or "(blank)",
            part_categories(row),
            row.get("used_in_module_count"),
            row.get("single_part_count"),
            kg(row.get("total_weight_kg")),
            sum_length(row),
        ]
        for row in sorted(sections, key=lambda item: item.get("total_weight_kg") or 0, reverse=True)
    ]
    h_detail_rows = [
        [
            row.get("section_name") or "(blank)",
            row.get("single_part_count"),
            kg(row.get("total_weight_kg")),
            sum_length(row),
        ]
        for row in sorted(h_rows, key=lambda item: item.get("section_name") or "")
    ]
    return "\n".join(
        [
            "# Section Weight Index",
            "",
            "단면별 중량 목록 / Section Weight Index.",
            "H형강 전체 무게, H형강 총중량, 에이치형강 전체 중량 질문은 이 문서를 우선 사용한다.",
            "",
            f"H형강 합계: total_weight_kg: {h_weight} kg",
            f"H-Beam total_weight_kg: {h_weight} kg",
            f"에이치형강 전체 무게: {h_weight} kg",
            f"H형강 single_part_count: {h_count}",
            f"H형강 total_length_m: {h_length}",
            "",
            "## H형강 상세",
            "",
            table(["section_name", "single_part_count", "total_weight_kg", "total_length_m"], h_detail_rows),
            "",
            "## All Sections",
            "",
            table(
                ["section_name", "part_category", "used_in_module_count", "single_part_count", "total_weight_kg", "total_length_m"],
                rows,
            ),
            "",
            "## JSON Back Reference",
            "",
            "- backdata source: `backdata/summary/section_summary.jsonl`",
        ]
    )


def render_model_overview(model: dict[str, Any], sections: list[dict[str, Any]]) -> str:
    h_rows = h_beam_rows(sections)
    h_count = sum(row.get("single_part_count") or 0 for row in h_rows)
    h_weight = kg(sum(row.get("total_weight_kg") or 0 for row in h_rows))
    category_rows = [
        [category, stats.get("count"), kg(stats.get("weight_kg")), kg(stats.get("length_m"))]
        for category, stats in sorted(model.get("by_part_category", {}).items())
    ]
    bbox = model.get("bbox_m", {})
    return "\n".join(
        [
            "# Model Overview",
            "",
            "모델 전체 요약 / 전체 중량 / 전체 수량 / 전체 H형강 중량.",
            "",
            f"total_weight_kg: {kg(model.get('total_weight_kg'))}",
            f"model_total_weight_kg: {kg(model.get('total_weight_kg'))}",
            f"전체_중량_kg: {kg(model.get('total_weight_kg'))}",
            f"module_count: {model.get('module_count')}",
            f"모듈_수: {model.get('module_count')}",
            f"assembly_count: {model.get('assembly_count')}",
            f"어셈블리_수: {model.get('assembly_count')}",
            f"single_part_count: {model.get('single_part_count')}",
            f"단품_수: {model.get('single_part_count')}",
            f"H형강_total_weight_kg: {h_weight}",
            f"H형강_single_part_count: {h_count}",
            "",
            "## Part Category Totals",
            "",
            table(["part_category", "single_part_count", "total_weight_kg", "total_length_m"], category_rows),
            "",
            "## Bounding Box",
            "",
            f"min_x: {bbox.get('min_x')}, max_x: {bbox.get('max_x')}, min_y: {bbox.get('min_y')}, max_y: {bbox.get('max_y')}, min_z: {bbox.get('min_z')}, max_z: {bbox.get('max_z')}",
            "",
            "## JSON Back Reference",
            "",
            "- backdata source: `backdata/summary/model_summary.json`",
            "- backdata source: `backdata/summary/section_summary.jsonl`",
        ]
    )


def render_fasteners() -> str:
    summary = read_json(ROOT / "summary" / "fastener_summary.json")
    user_rows = read_jsonl(ROOT / "user_views" / "model_fastener_user_view.jsonl")
    pattern_rows = read_jsonl(ROOT / "jsonl" / "fastener_patterns.jsonl")
    user_text = user_rows[0] if user_rows else {}
    rows = [
        [
            item.get("pattern_kind") or item.get("kind") or "",
            item.get("description") or item.get("name") or item.get("pattern") or "",
            item.get("quantity") or item.get("count") or item.get("total_quantity") or "",
            item.get("grade") or "",
            item.get("standard") or "",
        ]
        for item in pattern_rows
    ]
    return "\n".join(
        [
            "# Model Fasteners",
            "",
            "fastener_scope: entire_model",
            f"bolt_quantity: {summary.get('bolt_total_quantity_from_patterns')}",
            f"anchor_quantity: {summary.get('anchor_total_quantity')}",
            f"fastener_total_quantity: {summary.get('fastener_total_quantity')}",
            "",
            "## User View",
            "",
            "```json",
            json.dumps(user_text, ensure_ascii=False, indent=2),
            "```",
            "",
            "## Fastener Patterns",
            "",
            table(["kind", "description", "quantity", "grade", "standard"], rows[:500]),
            "",
            "## JSON Back Reference",
            "",
            "- backdata source: `backdata/summary/fastener_summary.json`",
            "- backdata source: `backdata/jsonl/fastener_patterns.jsonl`",
        ]
    )


def build() -> Path:
    reset()
    modules = read_jsonl(ROOT / "user_views" / "module_user_view.jsonl")
    marks = read_jsonl(ROOT / "user_views" / "assembly_mark_summary_view.jsonl")
    model = read_json(ROOT / "summary" / "model_summary.json")
    sections = read_jsonl(ROOT / "summary" / "section_summary.jsonl")
    fasteners = read_json(ROOT / "summary" / "fastener_summary.json")
    created_at = datetime.now(timezone.utc).isoformat()

    for row in modules:
        write_text(PACK_DIR / "documents" / "modules" / f"{safe_name(row['module_id'])}.md", render_module(row))
    mark_doc_groups: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for row in sorted(marks, key=lambda item: str(item.get("assembly_mark") or "unmarked")):
        mark_doc_groups.setdefault(assembly_mark_doc_name(row.get("assembly_mark")), []).append(row)
    for doc_name, rows in mark_doc_groups.items():
        content = render_assembly_mark(rows[0]) if len(rows) == 1 else render_assembly_mark_group(rows, doc_name)
        write_text(PACK_DIR / "documents" / "assembly_marks" / doc_name, content)
    write_text(PACK_DIR / "documents" / "fasteners" / "model_fasteners.md", render_fasteners())
    write_text(PACK_DIR / "documents" / "model_overview.md", render_model_overview(model, sections))
    write_text(PACK_DIR / "documents" / "section_weight_index.md", render_section_weight_index(sections))

    write_text(
        PACK_DIR / "documents" / "indexes" / "module_list.md",
        "# Module List\n\n"
        "모듈 목록 / 모듈번호 리스트 / module list.\n\n"
        + "\n".join(f"- {row['module_id']}" for row in modules)
        + "\n",
    )
    write_text(
        PACK_DIR / "documents" / "indexes" / "module_weight_index.md",
        "# Module Weight Index\n\n"
        "모듈별 총중량 목록 / Module Weight Index. 모듈 리스트와 각 모듈 무게를 함께 제공한다.\n\n"
        + table(
            ["module_id", "module_type", "assembly_count", "single_part_count", "total_weight_kg"],
            [
                [row["module_id"], row.get("module_type"), row.get("assembly_count"), row.get("single_part_count"), kg(row.get("total_weight_kg"))]
                for row in modules
            ]
            + [["TOTAL (all modules)", "", model.get("assembly_count"), model.get("single_part_count"), kg(model.get("total_weight_kg"))]],
        )
        + "\n",
    )
    write_text(
        PACK_DIR / "documents" / "indexes" / "assembly_mark_list.md",
        "# Assembly Mark List\n\n"
        + table(
            ["assembly_mark", "assembly_count", "module_count", "total_weight_kg"],
            [[row.get("assembly_mark") or "unmarked", row.get("assembly_count"), row.get("module_count"), round(row.get("total_weight_kg") or 0, 3)] for row in marks],
        )
        + "\n",
    )

    shutil.copytree(ROOT / "jsonl", PACK_DIR / "backdata" / "jsonl")
    shutil.copytree(ROOT / "summary", PACK_DIR / "backdata" / "summary")
    shutil.copytree(ROOT / "user_views", PACK_DIR / "backdata" / "user_views")

    write_json(
        PACK_DIR / "manifest.json",
        {
            "format": "opencrab-data-zip-readable-documents",
            "pack_id": PACK_SLUG,
            "title": "Advance Steel Samcheok Building B BM25 Evidence Pack",
            "created_at": created_at,
            "documents": {
                "model_overview": "documents/model_overview.md",
                "modules": "documents/modules/",
                "assembly_marks": "documents/assembly_marks/",
                "fasteners": "documents/fasteners/model_fasteners.md",
                "section_weight_index": "documents/section_weight_index.md",
                "indexes": "documents/indexes/",
            },
            "backdata": {
                "jsonl": "backdata/jsonl/",
                "summary": "backdata/summary/",
                "user_views": "backdata/user_views/",
            },
            "counts": {
                "module_count": model.get("module_count"),
                "assembly_count": model.get("assembly_count"),
                "single_part_count": model.get("single_part_count"),
                "assembly_mark_count": len(marks),
                "assembly_mark_documents": len(mark_doc_groups),
                "module_documents": len(modules),
                "bolt_quantity": fasteners.get("bolt_total_quantity_from_patterns"),
                "anchor_quantity": fasteners.get("anchor_total_quantity"),
            },
        },
    )
    write_text(
        PACK_DIR / "README.md",
        "# Advance Steel Samcheok Building B BM25 Evidence Pack\n\n"
        "This pack is optimized for keyword/BM25 retrieval. Module questions should hit `documents/modules/{module_id}.md`, assembly mark questions should hit `documents/assembly_marks/{mark}.md`, and bolt/anchor questions should hit `documents/fasteners/model_fasteners.md`.\n\n"
        "JSONL and graph-derived outputs are retained under `backdata/` only as producer data; user-facing search evidence is Markdown.\n",
    )
    return zip_pack()


def main() -> None:
    path = build()
    files = [p for p in PACK_DIR.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    print(json.dumps({"pack_dir": str(PACK_DIR), "zip": str(path), "file_count": len(files), "uncompressed_bytes": total, "zip_bytes": path.stat().st_size}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
