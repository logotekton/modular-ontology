from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import Response


OLLAMA_BASE_URL = os.environ.get("MODDULAR_GRAPH_LOCAL_AI_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
TOKEN_ENV_NAMES = ("MODDULAR_GRAPH_LOCAL_AI_TOKEN", "MODDULAR_GRAPH_OLLAMA_TOKEN")

app = FastAPI(
    title="Modular Ontology Local AI Proxy",
    description="Token-protected localhost proxy for forwarding ModularOntology AI requests to Ollama.",
    version="0.1.0",
)


def _expected_token() -> str:
    for name in TOKEN_ENV_NAMES:
        token = os.environ.get(name)
        if token:
            return token
    raise HTTPException(status_code=503, detail="Local AI proxy token is not configured.")


def _require_token(x_modular_ai_token: str | None) -> None:
    if not x_modular_ai_token or x_modular_ai_token != _expected_token():
        raise HTTPException(status_code=401, detail="Unauthorized.")


def _forward_to_ollama(path: str, *, method: str, body: bytes | None = None) -> Response:
    url = f"{OLLAMA_BASE_URL}{path}"
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=float(os.environ.get("MODDULAR_GRAPH_LOCAL_AI_TIMEOUT", "180"))) as response:
            content = response.read()
            content_type = response.headers.get("Content-Type", "application/json")
            return Response(content=content, status_code=response.status, media_type=content_type.split(";", 1)[0])
    except urllib.error.HTTPError as exc:
        detail = exc.read()
        content_type = exc.headers.get("Content-Type", "application/json")
        return Response(content=detail, status_code=exc.code, media_type=content_type.split(";", 1)[0])
    except urllib.error.URLError as exc:
        payload = {"error": f"Ollama is not reachable at {url}: {exc.reason}"}
        return Response(content=json.dumps(payload).encode("utf-8"), status_code=502, media_type="application/json")


@app.get("/api/tags")
def tags(x_modular_ai_token: str | None = Header(default=None)) -> Response:
    _require_token(x_modular_ai_token)
    return _forward_to_ollama("/api/tags", method="GET")


@app.post("/api/chat")
async def chat(request: Request, x_modular_ai_token: str | None = Header(default=None)) -> Response:
    _require_token(x_modular_ai_token)
    return _forward_to_ollama("/api/chat", method="POST", body=await request.body())
