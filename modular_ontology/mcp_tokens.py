from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import Any

from .auth import User
from .config import MCP_TOKENS_FILE


TOKEN_PREFIX = "mom_"


def _load_token_payload(path: Path = MCP_TOKENS_FILE) -> dict[str, Any]:
    if not path.exists():
        return {"tokens": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {"tokens": []}
    if not isinstance(data, dict):
        return {"tokens": []}
    tokens = data.get("tokens")
    if not isinstance(tokens, list):
        data["tokens"] = []
    return data


def _save_token_payload(payload: dict[str, Any], path: Path = MCP_TOKENS_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _new_token() -> str:
    return f"{TOKEN_PREFIX}{secrets.token_urlsafe(30)}"


def _public_token_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "token": record.get("token", ""),
        "userEmail": record.get("userEmail", ""),
        "userName": record.get("userName", ""),
        "company": record.get("company", ""),
        "role": record.get("role", "member"),
        "status": record.get("status", "active"),
        "createdAt": record.get("createdAt"),
        "updatedAt": record.get("updatedAt"),
    }


def ensure_mcp_token_for_user(user: User, *, path: Path = MCP_TOKENS_FILE) -> dict[str, Any]:
    payload = _load_token_payload(path)
    tokens = [item for item in payload.get("tokens", []) if isinstance(item, dict)]
    normalized_email = user.email.strip().lower()
    now = time.time()
    for record in tokens:
        if str(record.get("userEmail", "")).strip().lower() != normalized_email:
            continue
        if record.get("status") != "active":
            continue
        record.update(
            {
                "userName": user.name,
                "company": user.company,
                "role": user.role,
                "updatedAt": now,
            }
        )
        payload["tokens"] = tokens
        _save_token_payload(payload, path)
        return _public_token_record(record)

    record = {
        "token": _new_token(),
        "userEmail": normalized_email,
        "userName": user.name,
        "company": user.company,
        "role": user.role,
        "status": "active",
        "createdAt": now,
        "updatedAt": now,
    }
    tokens.append(record)
    payload["tokens"] = tokens
    _save_token_payload(payload, path)
    return _public_token_record(record)


def regenerate_mcp_token_for_user(user: User, *, path: Path = MCP_TOKENS_FILE) -> dict[str, Any]:
    payload = _load_token_payload(path)
    tokens = [item for item in payload.get("tokens", []) if isinstance(item, dict)]
    normalized_email = user.email.strip().lower()
    now = time.time()
    for record in tokens:
        if str(record.get("userEmail", "")).strip().lower() != normalized_email:
            continue
        if record.get("status") == "active":
            record["status"] = "revoked"
            record["revokedAt"] = now
            record["updatedAt"] = now

    record = {
        "token": _new_token(),
        "userEmail": normalized_email,
        "userName": user.name,
        "company": user.company,
        "role": user.role,
        "status": "active",
        "createdAt": now,
        "updatedAt": now,
    }
    tokens.append(record)
    payload["tokens"] = tokens
    _save_token_payload(payload, path)
    return _public_token_record(record)


def get_mcp_token_record(token: str, *, path: Path = MCP_TOKENS_FILE) -> dict[str, Any] | None:
    token = token.strip()
    if not token:
        return None
    payload = _load_token_payload(path)
    for record in payload.get("tokens", []):
        if not isinstance(record, dict):
            continue
        if record.get("token") == token and record.get("status") == "active":
            return _public_token_record(record)
    return None


def build_user_mcp_urls(public_base_url: str | None, token: str) -> dict[str, str | None]:
    local_url = f"http://127.0.0.1:8011/mcp/{token}"
    public_url = None
    if public_base_url:
        public_url = f"{public_base_url.rstrip('/')}/mcp/{token}"
    return {"localUrl": local_url, "publicUrl": public_url}
