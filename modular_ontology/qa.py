from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .config import env
from .pack_index import get_module, list_modules, list_nodes, search_pack
from .store import node_neighborhood, search_documents, search_nodes

MODULE_ID_RE = re.compile(r"\b\d+-\d{2}-[A-Z][A-Z0-9-]*\b", flags=re.I)
FLOOR_RE = re.compile(r"(?<!\d)(\d+)\s*(?:층|floor|f)\b", flags=re.I)
WEIGHT_TERMS = (
    "weight", "weigh", "mass", "heaviest", "lightest", "kg", "ton",
    "중량", "총중량", "무게", "무거", "가벼", "톤",
)
MODULE_TERMS = ("module", "modules", "modulelist", "module list", "모듈", "모듈리스트", "모듈 리스트")

QUERY_EXPANSIONS = {
    "모듈": ("module", "modules", "module_id", "module_type", "documents/modules"),
    "모듈리스트": ("module list", "module overview", "documents/modules"),
    "중량": ("weight", "total_weight_kg", "kg", "module_weight_index"),
    "무게": ("weight", "total_weight_kg", "kg", "module_weight_index"),
    "총중량": ("total weight", "total_weight_kg", "module_weight_index"),
    "어셈블리": ("assembly", "assembly_count", "assembly mark"),
    "부재": ("single part", "element", "section", "material"),
    "단면": ("section", "profile", "section name"),
    "자재": ("material", "grade"),
}


def answer_pack_question(
    pack_id: str,
    question: str,
    limit: int = 6,
    db_path: Path | None = None,
    use_openai: bool | None = None,
    openai_api_key: str | None = None,
    openai_model: str | None = None,
    llm_client: Any | None = None,
) -> dict[str, Any]:
    """Build a local Graph RAG answer from indexed evidence and graph context.

    The return shape is intentionally LLM-ready: GPT/Codex can consume the same
    evidence and graph context through MCP, while the web UI can render it without
    requiring an OpenAI API key during local prototyping.
    """

    search_query = _expanded_query(question)
    evidence = search_documents(pack_id, search_query, limit=limit, db_path=db_path)
    if not evidence:
        evidence = search_pack(pack_id, search_query, limit=limit)

    nodes = search_nodes(pack_id, search_query, limit=limit, db_path=db_path)
    specialized = _specialized_weight_context(pack_id, question, limit=max(limit, 12))
    evidence = _merge_evidence(evidence, specialized["evidence"])
    if not evidence and not nodes and not specialized["facts"]:
        # Keyword search found nothing (e.g. a broad/vague question). Give the LLM a
        # representative slice of the pack so it can still reason instead of replying
        # that no data exists.
        fallback = _fallback_pack_context(pack_id, limit=limit)
        evidence = _merge_evidence(evidence, fallback["evidence"])
        if not nodes:
            nodes = fallback["nodes"]
    neighborhoods = []
    for node in nodes[:3]:
        neighborhoods.append(
            {
                "nodeId": node["id"],
                "label": node["label"],
                "relationships": node_neighborhood(pack_id, node["id"], limit=8, db_path=db_path),
            }
        )

    local_answer = _compose_answer(question, evidence, nodes, neighborhoods, specialized["facts"])
    answer = local_answer
    mode = "local-graph-rag"
    llm_error = None

    should_use_openai = use_openai if use_openai is not None else _openai_enabled()
    if should_use_openai:
        try:
            answer = _compose_openai_answer(
                question,
                evidence,
                nodes,
                neighborhoods,
                local_answer,
                specialized["facts"],
                llm_client,
                api_key=openai_api_key,
                model=openai_model,
            )
            mode = "openai-graph-rag"
        except Exception as exc:  # pragma: no cover - exact SDK/network errors vary by environment
            llm_error = _safe_llm_error(str(exc), openai_api_key)

    return {
        "packId": pack_id,
        "question": question,
        "answer": answer,
        "evidence": evidence,
        "graphContext": {
            "nodes": nodes,
            "neighborhoods": neighborhoods,
            "facts": specialized["facts"],
        },
        "mode": mode,
        "llmError": llm_error,
    }


def _has_any_term(question: str, terms: tuple[str, ...]) -> bool:
    lowered = question.casefold()
    return any(term.casefold() in lowered for term in terms)


def _expanded_query(question: str) -> str:
    terms = [question]
    lowered = question.casefold()
    for trigger, expansions in QUERY_EXPANSIONS.items():
        if trigger.casefold() in lowered:
            terms.extend(expansions)
    for floor in _extract_floor_numbers(question):
        terms.extend([f"{floor}-", f"{floor}F", f"{floor}층"])
    return " ".join(dict.fromkeys(str(term) for term in terms if term))


def _extract_module_ids(question: str) -> list[str]:
    seen = set()
    ids = []
    for match in MODULE_ID_RE.finditer(question):
        module_id = match.group(0).upper()
        if module_id in seen:
            continue
        ids.append(module_id)
        seen.add(module_id)
    return ids


def _extract_floor_numbers(question: str) -> list[str]:
    seen = set()
    floors = []
    for match in FLOOR_RE.finditer(question):
        floor = str(int(match.group(1)))
        if floor in seen:
            continue
        floors.append(floor)
        seen.add(floor)
    return floors


def _merge_evidence(base: list[dict[str, Any]], extra: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = []
    seen = set()
    for item in [*extra, *base]:
        key = str(item.get("path") or item.get("title") or item.get("snippet"))
        if key in seen:
            continue
        merged.append(item)
        seen.add(key)
    return merged


def _kg(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:,.3f}".rstrip("0").rstrip(".")


def _specialized_weight_context(pack_id: str, question: str, limit: int = 12) -> dict[str, list[dict[str, Any]]]:
    module_ids = _extract_module_ids(question)
    wants_weight = _has_any_term(question, WEIGHT_TERMS)
    wants_module = _has_any_term(question, MODULE_TERMS)
    if not module_ids and not wants_weight and not wants_module:
        return {"evidence": [], "facts": []}

    evidence: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []
    for module_id in module_ids:
        try:
            module = get_module(pack_id, module_id, max_chars=8000)
        except Exception:
            continue
        weight = module.get("total_weight_kg")
        if weight is None:
            continue
        fact = {
            "kind": "module_weight",
            "module_id": module.get("module_id") or module_id,
            "module_type": module.get("module_type"),
            "total_weight_kg": weight,
            "assembly_count": module.get("assembly_count"),
            "single_part_count": module.get("single_part_count"),
            "total_length_m": module.get("total_length_m"),
            "evidence_path": module.get("evidence_path"),
        }
        facts.append(fact)
        evidence.append(
            {
                "path": module.get("evidence_path"),
                "title": f"Module {fact['module_id']} weight",
                "snippet": (
                    f"{fact['module_id']} module_type={fact.get('module_type')} "
                    f"assembly_count={fact.get('assembly_count')} single_part_count={fact.get('single_part_count')} "
                    f"total_weight_kg={fact.get('total_weight_kg')} total_length_m={fact.get('total_length_m')}"
                ),
                "score": 2.0,
                "source": "module-weight-helper",
            }
        )

    if facts or not (wants_module or wants_weight):
        return {"evidence": evidence, "facts": facts}

    try:
        modules = [
            module
            for module in list_modules(pack_id, limit=500).get("modules", [])
            if module.get("total_weight_kg") is not None
        ]
    except Exception:
        return {"evidence": evidence, "facts": facts}

    all_rows = [
        {
            "module_id": module.get("module_id"),
            "floor": _module_floor(module.get("module_id")),
            "module_type": module.get("module_type"),
            "total_weight_kg": module.get("total_weight_kg"),
            "assembly_count": module.get("assembly_count"),
            "single_part_count": module.get("single_part_count"),
            "evidence_path": module.get("evidence_path"),
        }
        for module in modules
    ]
    facts.append(
        {
            "kind": "module_table",
            "module_count": len(all_rows),
            "modules": all_rows,
            "evidence_path": "documents/indexes/module_weight_index.md",
            "note": "Use floor when the module_id prefix before '-' represents a floor, e.g. 1-05-ST belongs to floor 1.",
        }
    )

    floor_numbers = _extract_floor_numbers(question)
    for floor in floor_numbers:
        floor_modules = [module for module in all_rows if module.get("floor") == floor]
        if not floor_modules:
            continue
        floor_total = sum(float(module.get("total_weight_kg") or 0) for module in floor_modules)
        rows = floor_modules
        fact = {
            "kind": "floor_module_weight_total",
            "floor": floor,
            "module_count": len(rows),
            "total_weight_kg": round(floor_total, 3),
            "modules": rows,
            "evidence_path": "documents/indexes/module_weight_index.md",
        }
        facts.append(fact)
        evidence.append(
            {
                "path": fact["evidence_path"],
                "title": f"{floor}F module total weight",
                "snippet": (
                    f"{floor}F modules={len(rows)} total_weight_kg={fact['total_weight_kg']}\n"
                    + "\n".join(
                        f"{row['module_id']} | {row.get('module_type')} | {row.get('total_weight_kg')} kg"
                        for row in rows
                    )
                ),
                "score": 2.0,
                "source": "floor-module-weight-helper",
            }
        )

    if any(fact.get("kind") == "floor_module_weight_total" for fact in facts):
        return {"evidence": evidence, "facts": facts}

    rows = all_rows[:limit]
    if not rows:
        return {"evidence": evidence, "facts": facts}

    if wants_weight:
        facts.append({"kind": "module_weight_list", "modules": rows, "module_count": len(modules)})
        title = "Module weight index"
        source = "module-weight-helper"
        snippet = "\n".join(
            f"{row['module_id']} | {row.get('module_type')} | {row.get('assembly_count')} assemblies | "
            f"{row.get('single_part_count')} parts | {row.get('total_weight_kg')} kg"
            for row in rows
        )
    else:
        title = "Module table"
        source = "module-table-helper"
        snippet = "\n".join(
            f"{row['module_id']} | floor={row.get('floor')} | {row.get('module_type')} | "
            f"{row.get('assembly_count')} assemblies | {row.get('single_part_count')} parts"
            for row in rows
        )
    evidence.append(
        {
            "path": "documents/indexes/module_weight_index.md",
            "title": title,
            "snippet": snippet,
            "score": 2.0,
            "source": source,
        }
    )
    return {"evidence": evidence, "facts": facts}


def _module_floor(module_id: Any) -> str | None:
    match = re.match(r"^(\d+)-", str(module_id or ""))
    if not match:
        return None
    return str(int(match.group(1)))


def _fallback_pack_context(pack_id: str, limit: int = 6) -> dict[str, list[dict[str, Any]]]:
    """Representative pack context used when keyword search returns nothing, so the
    LLM always has grounded data to reason over (works for any pack)."""
    evidence: list[dict[str, Any]] = []
    nodes: list[dict[str, Any]] = []
    try:
        modules = list_modules(pack_id, limit=200).get("modules", [])
    except Exception:
        modules = []
    if modules:
        rows = modules[: max(limit, 20)]
        lines = []
        for module in rows:
            parts = [str(module.get("module_id") or module.get("id") or "?")]
            for key, label in (
                ("module_type", "type"),
                ("total_weight_kg", "weight_kg"),
                ("assembly_count", "assemblies"),
                ("single_part_count", "parts"),
            ):
                if module.get(key) is not None:
                    parts.append(f"{label}={module.get(key)}")
            lines.append(" | ".join(parts))
        evidence.append(
            {
                "path": "documents/modules/ (overview)",
                "title": "Module overview",
                "snippet": "\n".join(lines),
                "score": 0.5,
                "source": "pack-overview",
            }
        )
    try:
        nodes = list_nodes(pack_id, limit=max(limit, 12)).get("nodes", [])[: max(limit, 10)]
    except Exception:
        nodes = []
    return {"evidence": evidence, "nodes": nodes}


def _openai_enabled() -> bool:
    return False


def validate_openai_api_key(api_key: str | None, model: str | None = None, llm_client: Any | None = None) -> dict[str, Any]:
    clean_key = api_key.strip() if api_key else ""
    selected_model = str(model or env("MODULAR_ONTOLOGY_OPENAI_MODEL") or "gpt-4.1-mini").strip()
    if not clean_key:
        return {"valid": False, "model": selected_model, "error": "OpenAI API key is required."}
    try:
        client = llm_client or _make_openai_client(api_key=clean_key)
        client.responses.create(model=selected_model, input="Return only OK.", max_output_tokens=16)
        return {"valid": True, "model": selected_model}
    except Exception as exc:  # pragma: no cover - exact SDK/network errors vary by environment
        return {"valid": False, "model": selected_model, "error": _safe_llm_error(str(exc), clean_key)}


def _compose_openai_answer(
    question: str,
    evidence: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    neighborhoods: list[dict[str, Any]],
    local_answer: str,
    facts: list[dict[str, Any]],
    llm_client: Any | None = None,
    *,
    api_key: str | None = None,
    model: str | None = None,
) -> str:
    model = str(model or env("MODULAR_ONTOLOGY_OPENAI_MODEL") or "gpt-4.1-mini").strip()
    if not model:
        raise RuntimeError("Set MODULAR_ONTOLOGY_OPENAI_MODEL to enable OpenAI synthesis.")
    clean_key = api_key.strip() if api_key else ""
    if not clean_key and llm_client is None:
        raise RuntimeError("OpenAI API key is required for web AI Query. Provide a user API key.")
    client = llm_client or _make_openai_client(api_key=api_key)
    prompt = _build_llm_prompt(question, evidence, nodes, neighborhoods, local_answer, facts)
    response = client.responses.create(model=model, input=prompt)
    text = getattr(response, "output_text", None)
    if text:
        return _finalize_llm_answer(str(text), local_answer)
    output = getattr(response, "output", None)
    if output:
        return _finalize_llm_answer(str(output), local_answer)
    return _finalize_llm_answer(str(response), local_answer)


def _make_openai_client(api_key: str | None = None) -> Any:
    from openai import OpenAI

    clean_key = api_key.strip() if api_key else None
    if not clean_key:
        raise RuntimeError("OpenAI API key is required.")
    return OpenAI(api_key=clean_key)


def _safe_llm_error(error: str, secret: str | None = None) -> str:
    safe = error
    if secret and secret.strip():
        safe = safe.replace(secret.strip(), "[redacted-api-key]")
    safe = re.sub(r"Incorrect API key provided: [^.\s]+", "Incorrect API key provided: [redacted-api-key]", safe)
    safe = re.sub(r"sk-[A-Za-z0-9_*.-]{4,}", "sk-[redacted]", safe)
    return safe


def _llm_answer_too_weak(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text.strip()).casefold()
    if not normalized:
        return True
    if normalized in {"based", "based on", "근거", "근거:"}:
        return True
    if re.fullmatch(r"(based|based on|basis|evidence)[:.\s-]*", normalized):
        return True
    return len(normalized) < 12


def _finalize_llm_answer(text: str, local_answer: str) -> str:
    text = text.strip()
    if _llm_answer_too_weak(text):
        return local_answer
    return text


def _build_llm_prompt(
    question: str,
    evidence: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    neighborhoods: list[dict[str, Any]],
    local_answer: str,
    facts: list[dict[str, Any]] | None = None,
) -> str:
    payload = {
        "question": question,
        "draftAnswer": local_answer,
        "evidence": evidence[:12],
        "graphNodes": nodes[:12],
        "relationships": neighborhoods[:8],
        "facts": facts or [],
    }
    return (
        "You are a BIM/Revit/Advance Steel ontology analyst. The provided graph nodes, edges, "
        "relationships, facts, and evidence are the authoritative source of truth for this project.\n"
        "Use the structured `facts` first, then use evidence snippets and graph nodes to verify or add detail. "
        "For tables such as `module_table`, reason across all rows: filter, group, sum, count, compare, and "
        "compute derived values when the question asks for totals, lists, rankings, or summaries. In this "
        "dataset, a module_id prefix before '-' can represent the floor, so `1-05-ST` belongs to floor 1.\n"
        "Grounding rule: every conclusion must follow from the provided graph/evidence. Do not bring in "
        "outside world knowledge and do not invent node values that are not present. If a value is absent, "
        "say exactly what is missing and which evidence would be needed.\n"
        "`draftAnswer` is a fallback calculation, not the final authority. Verify it against `facts`; correct "
        "it when the facts show a better answer.\n"
        "Answer format: give a direct answer first, then a compact calculation or basis, then cite evidence "
        "paths or node IDs. Answer in Korean when the user asks in Korean.\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def _compose_answer(
    question: str,
    evidence: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    neighborhoods: list[dict[str, Any]],
    facts: list[dict[str, Any]] | None = None,
) -> str:
    facts = facts or []
    floor_weight_facts = [fact for fact in facts if fact.get("kind") == "floor_module_weight_total"]
    if floor_weight_facts:
        lines = []
        for fact in floor_weight_facts:
            modules = fact.get("modules", [])
            module_rows = "; ".join(
                f"{module.get('module_id')}: {_kg(module.get('total_weight_kg'))} kg"
                for module in modules
            )
            lines.append(
                f"{fact.get('floor')}층 모듈 {fact.get('module_count')}개의 총 무게는 "
                f"{_kg(fact.get('total_weight_kg'))} kg입니다. "
                f"모듈별 중량은 {module_rows}입니다. "
                f"근거: `{fact.get('evidence_path')}` 및 각 `documents/modules/*.md`."
            )
        return "\n".join(lines)

    module_weight_facts = [fact for fact in facts if fact.get("kind") == "module_weight"]
    if module_weight_facts:
        lines = []
        for fact in module_weight_facts:
            lines.append(
                f"{fact.get('module_id')} 모듈의 총중량은 {_kg(fact.get('total_weight_kg'))} kg입니다. "
                f"모듈 타입은 {fact.get('module_type')}, 어셈블리 {fact.get('assembly_count')}개, "
                f"단품 {fact.get('single_part_count')}개입니다. 근거: `{fact.get('evidence_path')}`."
            )
        return "\n".join(lines)

    module_weight_lists = [fact for fact in facts if fact.get("kind") == "module_weight_list"]
    if module_weight_lists:
        modules = module_weight_lists[0].get("modules", [])
        rows = [
            f"{module.get('module_id')}: {_kg(module.get('total_weight_kg'))} kg"
            for module in modules
        ]
        return (
            f"모듈별 중량 {len(modules)}개를 찾았습니다"
            f"{' (일부 표시)' if module_weight_lists[0].get('module_count', len(modules)) > len(modules) else ''}: "
            + "; ".join(rows)
            + ". 근거: `documents/indexes/module_weight_index.md` 및 각 `documents/modules/*.md`."
        )

    module_tables = [fact for fact in facts if fact.get("kind") == "module_table"]
    if module_tables:
        table = module_tables[0]
        modules = table.get("modules", [])
        rows = [
            f"{module.get('module_id')}({module.get('module_type')}): {_kg(module.get('total_weight_kg'))} kg"
            for module in modules
        ]
        return (
            f"전체 모듈 {table.get('module_count', len(modules))}개를 찾았습니다: "
            + "; ".join(rows[:24])
            + ". 근거: `documents/indexes/module_weight_index.md` 및 각 `documents/modules/*.md`."
        )

    if not evidence and not nodes:
        return (
            "No indexed evidence or graph node matched the question yet. Rebuild the index, "
            "try a module ID, element type, assembly mark, material, or section name."
        )

    parts = ["Local Graph RAG result:"]
    if evidence:
        top = evidence[0]
        parts.append(f"the strongest evidence match is `{top['path']}`.")
    if nodes:
        node_labels = ", ".join(f"{node['label']} ({node['type']})" for node in nodes[:3])
        parts.append(f"related graph nodes include {node_labels}.")
    relation_count = sum(len(item["relationships"]) for item in neighborhoods)
    if relation_count:
        parts.append(f"{relation_count} nearby graph relationships are available for follow-up traversal.")
    parts.append("Use the evidence snippets and graph context below as grounded source material for GPT/Codex.")
    return " ".join(parts)
