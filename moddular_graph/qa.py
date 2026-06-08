from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .pack_index import get_module, list_modules, search_pack
from .store import node_neighborhood, search_documents, search_nodes

MODULE_ID_RE = re.compile(r"\b\d+-\d{2}-[A-Z][A-Z0-9-]*\b", flags=re.I)
WEIGHT_TERMS = ("weight", "mass", "kg", "ton", "중량", "총중량", "무게", "톤")
MODULE_TERMS = ("module", "모듈")


def answer_pack_question(
    pack_id: str,
    question: str,
    limit: int = 6,
    db_path: Path | None = None,
    use_openai: bool | None = None,
    use_ollama: bool | None = None,
    llm_client: Any | None = None,
) -> dict[str, Any]:
    """Build a local Graph RAG answer from indexed evidence and graph context.

    The return shape is intentionally LLM-ready: GPT/Codex can consume the same
    evidence and graph context through MCP, while the web UI can render it without
    requiring an OpenAI API key during local prototyping.
    """

    evidence = search_documents(pack_id, question, limit=limit, db_path=db_path)
    if not evidence:
        evidence = search_pack(pack_id, question, limit=limit)

    nodes = search_nodes(pack_id, question, limit=limit, db_path=db_path)
    specialized = _specialized_weight_context(pack_id, question, limit=max(limit, 12))
    evidence = _merge_evidence(evidence, specialized["evidence"])
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
            answer = _compose_openai_answer(question, evidence, nodes, neighborhoods, local_answer, specialized["facts"], llm_client)
            mode = "openai-graph-rag"
        except Exception as exc:  # pragma: no cover - exact SDK/network errors vary by environment
            llm_error = str(exc)

    should_use_ollama = use_ollama if use_ollama is not None else _ollama_enabled()
    if should_use_ollama and mode == "local-graph-rag":
        try:
            answer = _compose_ollama_answer(question, evidence, nodes, neighborhoods, local_answer, specialized["facts"], llm_client)
            mode = "ollama-graph-rag"
        except Exception as exc:  # pragma: no cover - exact service/network errors vary by environment
            llm_error = str(exc)

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
    if not _has_any_term(question, WEIGHT_TERMS):
        return {"evidence": [], "facts": []}

    evidence: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []
    for module_id in _extract_module_ids(question):
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

    if facts or not _has_any_term(question, MODULE_TERMS):
        return {"evidence": evidence, "facts": facts}

    try:
        modules = [
            module
            for module in list_modules(pack_id, limit=500).get("modules", [])
            if module.get("total_weight_kg") is not None
        ]
    except Exception:
        return {"evidence": evidence, "facts": facts}

    rows = [
        {
            "module_id": module.get("module_id"),
            "module_type": module.get("module_type"),
            "total_weight_kg": module.get("total_weight_kg"),
            "assembly_count": module.get("assembly_count"),
            "single_part_count": module.get("single_part_count"),
            "evidence_path": module.get("evidence_path"),
        }
        for module in modules[:limit]
    ]
    if not rows:
        return {"evidence": evidence, "facts": facts}

    facts.append({"kind": "module_weight_list", "modules": rows, "module_count": len(modules)})
    evidence.append(
        {
            "path": "documents/indexes/module_weight_index.md",
            "title": "Module weight index",
            "snippet": "\n".join(
                f"{row['module_id']} | {row.get('module_type')} | {row.get('assembly_count')} assemblies | "
                f"{row.get('single_part_count')} parts | {row.get('total_weight_kg')} kg"
                for row in rows
            ),
            "score": 2.0,
            "source": "module-weight-helper",
        }
    )
    return {"evidence": evidence, "facts": facts}


def _openai_enabled() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY") and os.environ.get("MODDULAR_GRAPH_OPENAI_MODEL"))


def _ollama_enabled() -> bool:
    return os.environ.get("MODDULAR_GRAPH_USE_OLLAMA") == "1"


def ollama_status() -> dict[str, Any]:
    model = _ollama_model()
    base_url = _ollama_base_url()
    try:
        payload = _get_ollama_json(f"{base_url}/api/tags")
        models = [item.get("name") for item in payload.get("models", []) if isinstance(item, dict)]
        return {
            "available": True,
            "baseUrl": base_url,
            "model": model,
            "modelInstalled": model in models,
            "models": models,
        }
    except Exception as exc:
        return {
            "available": False,
            "baseUrl": base_url,
            "model": model,
            "modelInstalled": False,
            "models": [],
            "error": str(exc),
        }


def _compose_openai_answer(
    question: str,
    evidence: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    neighborhoods: list[dict[str, Any]],
    local_answer: str,
    facts: list[dict[str, Any]],
    llm_client: Any | None = None,
) -> str:
    model = os.environ.get("MODDULAR_GRAPH_OPENAI_MODEL")
    if not model:
        raise RuntimeError("Set MODDULAR_GRAPH_OPENAI_MODEL to enable OpenAI synthesis.")
    client = llm_client or _make_openai_client()
    prompt = _build_llm_prompt(question, evidence, nodes, neighborhoods, local_answer, facts)
    response = client.responses.create(model=model, input=prompt)
    text = getattr(response, "output_text", None)
    if text:
        return str(text).strip()
    output = getattr(response, "output", None)
    if output:
        return str(output).strip()
    return str(response).strip()


def _compose_ollama_answer(
    question: str,
    evidence: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    neighborhoods: list[dict[str, Any]],
    local_answer: str,
    facts: list[dict[str, Any]],
    llm_client: Any | None = None,
) -> str:
    model = _ollama_model()
    prompt = _build_llm_prompt(question, evidence, nodes, neighborhoods, local_answer, facts)
    if llm_client is not None:
        if callable(llm_client):
            return str(llm_client(model=model, prompt=prompt)).strip()
        generate = getattr(llm_client, "generate", None)
        if generate:
            return str(generate(model=model, prompt=prompt)).strip()
    payload = _post_ollama_json(
        f"{_ollama_base_url()}/api/chat",
        {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You answer questions about BIM/Revit/Advance Steel ontology packs. "
                        "Use only the supplied evidence and graph context. Answer in Korean when the user asks in Korean."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "think": False,
            "options": {
                "temperature": float(os.environ.get("MODDULAR_GRAPH_OLLAMA_TEMPERATURE", "0.2")),
                "num_ctx": int(os.environ.get("MODDULAR_GRAPH_OLLAMA_NUM_CTX", "8192")),
                "num_predict": int(os.environ.get("MODDULAR_GRAPH_OLLAMA_NUM_PREDICT", "512")),
            },
        },
        timeout=float(os.environ.get("MODDULAR_GRAPH_OLLAMA_TIMEOUT", "180")),
    )
    message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    text = message.get("content") or payload.get("response")
    if not text:
        thinking = message.get("thinking") or payload.get("thinking")
        if thinking:
            raise RuntimeError("Ollama returned thinking content but no final response text.")
        raise RuntimeError("Ollama returned no response text.")
    return str(text).strip()


def _ollama_model() -> str:
    return os.environ.get("MODDULAR_GRAPH_OLLAMA_MODEL", "gemma4:12b-it-q4_K_M")


def _ollama_base_url() -> str:
    return os.environ.get("OLLAMA_HOST") or os.environ.get("MODDULAR_GRAPH_OLLAMA_URL", "http://127.0.0.1:11434")


def _post_ollama_json(url: str, payload: dict[str, Any], timeout: float = 10) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=_ollama_headers("application/json"), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ollama request failed: HTTP {exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Ollama is not reachable at {url}: {exc.reason}") from exc


def _get_ollama_json(url: str, timeout: float = 10) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=_ollama_headers(), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ollama request failed: HTTP {exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Ollama is not reachable at {url}: {exc.reason}") from exc


def _ollama_headers(content_type: str | None = None) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if content_type:
        headers["Content-Type"] = content_type
    token = os.environ.get("MODDULAR_GRAPH_OLLAMA_TOKEN")
    if token:
        headers["X-Modular-AI-Token"] = token
    return headers


def _make_openai_client() -> Any:
    from openai import OpenAI

    return OpenAI()


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
        "localAnswer": local_answer,
        "evidence": evidence[:6],
        "graphNodes": nodes[:6],
        "relationships": neighborhoods[:3],
        "facts": facts or [],
    }
    return (
        "You answer questions about BIM/Revit/Advance Steel ontology packs. "
        "Use only the provided evidence and graph context. If the context is insufficient, say what is missing. "
        "Keep the answer concise and cite evidence paths or node IDs when useful.\n\n"
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
