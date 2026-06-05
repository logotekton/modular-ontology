from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .pack_index import search_pack
from .store import node_neighborhood, search_documents, search_nodes


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
    neighborhoods = []
    for node in nodes[:3]:
        neighborhoods.append(
            {
                "nodeId": node["id"],
                "label": node["label"],
                "relationships": node_neighborhood(pack_id, node["id"], limit=8, db_path=db_path),
            }
        )

    local_answer = _compose_answer(question, evidence, nodes, neighborhoods)
    answer = local_answer
    mode = "local-graph-rag"
    llm_error = None

    should_use_openai = use_openai if use_openai is not None else _openai_enabled()
    if should_use_openai:
        try:
            answer = _compose_openai_answer(question, evidence, nodes, neighborhoods, local_answer, llm_client)
            mode = "openai-graph-rag"
        except Exception as exc:  # pragma: no cover - exact SDK/network errors vary by environment
            llm_error = str(exc)

    should_use_ollama = use_ollama if use_ollama is not None else _ollama_enabled()
    if should_use_ollama and mode == "local-graph-rag":
        try:
            answer = _compose_ollama_answer(question, evidence, nodes, neighborhoods, local_answer, llm_client)
            mode = "ollama-qwen-graph-rag"
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
        },
        "mode": mode,
        "llmError": llm_error,
    }


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
    llm_client: Any | None = None,
) -> str:
    model = os.environ.get("MODDULAR_GRAPH_OPENAI_MODEL")
    if not model:
        raise RuntimeError("Set MODDULAR_GRAPH_OPENAI_MODEL to enable OpenAI synthesis.")
    client = llm_client or _make_openai_client()
    prompt = _build_llm_prompt(question, evidence, nodes, neighborhoods, local_answer)
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
    llm_client: Any | None = None,
) -> str:
    model = _ollama_model()
    prompt = _build_llm_prompt(question, evidence, nodes, neighborhoods, local_answer)
    if llm_client is not None:
        if callable(llm_client):
            return str(llm_client(model=model, prompt=prompt)).strip()
        generate = getattr(llm_client, "generate", None)
        if generate:
            return str(generate(model=model, prompt=prompt)).strip()
    payload = _post_ollama_json(
        f"{_ollama_base_url()}/api/generate",
        {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "think": False,
            "options": {
                "temperature": float(os.environ.get("MODDULAR_GRAPH_OLLAMA_TEMPERATURE", "0.2")),
                "num_ctx": int(os.environ.get("MODDULAR_GRAPH_OLLAMA_NUM_CTX", "8192")),
            },
        },
        timeout=float(os.environ.get("MODDULAR_GRAPH_OLLAMA_TIMEOUT", "180")),
    )
    text = payload.get("response")
    if not text:
        thinking = payload.get("thinking")
        if thinking:
            raise RuntimeError("Ollama returned thinking content but no final response text.")
        raise RuntimeError("Ollama returned no response text.")
    return str(text).strip()


def _ollama_model() -> str:
    return os.environ.get("MODDULAR_GRAPH_OLLAMA_MODEL", "qwen3:14b")


def _ollama_base_url() -> str:
    return os.environ.get("OLLAMA_HOST") or os.environ.get("MODDULAR_GRAPH_OLLAMA_URL", "http://127.0.0.1:11434")


def _post_ollama_json(url: str, payload: dict[str, Any], timeout: float = 10) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ollama request failed: HTTP {exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Ollama is not reachable at {url}: {exc.reason}") from exc


def _get_ollama_json(url: str, timeout: float = 10) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ollama request failed: HTTP {exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Ollama is not reachable at {url}: {exc.reason}") from exc


def _make_openai_client() -> Any:
    from openai import OpenAI

    return OpenAI()


def _build_llm_prompt(
    question: str,
    evidence: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    neighborhoods: list[dict[str, Any]],
    local_answer: str,
) -> str:
    payload = {
        "question": question,
        "localAnswer": local_answer,
        "evidence": evidence[:6],
        "graphNodes": nodes[:6],
        "relationships": neighborhoods[:3],
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
) -> str:
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
