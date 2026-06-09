from __future__ import annotations

import os
from pathlib import Path
from typing import TypeVar


T = TypeVar("T")

ENV_PREFIX = "MODULAR_ONTOLOGY"
LEGACY_ENV_PREFIXES = ("MOD" + "DULAR_GRAPH", "MODULAR_" + "GRAPH")


def env(name: str, default: T | None = None) -> str | T | None:
    """Read current Modular Ontology env vars, with quiet legacy-prefix fallback."""

    for candidate in _env_candidates(name):
        value = os.environ.get(candidate)
        if value is not None:
            return value
    return default


def _env_candidates(name: str) -> list[str]:
    candidates = [name]
    if name.startswith(ENV_PREFIX):
        suffix = name[len(ENV_PREFIX) :]
        candidates.extend(f"{prefix}{suffix}" for prefix in LEGACY_ENV_PREFIXES)
    return candidates


ROOT = Path(env("MODULAR_ONTOLOGY_ROOT", Path(__file__).resolve().parents[1])).resolve()
DEFAULT_DATA_DIR = Path("/tmp/modular-ontology") if os.environ.get("VERCEL") else ROOT / "data"
DATA_DIR = Path(env("MODULAR_ONTOLOGY_DATA_DIR", DEFAULT_DATA_DIR)).resolve()
USE_STRUCTURED_DATA_DIR = (
    env("MODULAR_ONTOLOGY_STRUCTURED_DATA_DIR") == "1"
    or bool(env("MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID"))
    or (DATA_DIR / "01_Database").exists()
    or (DATA_DIR / "02_Ontology_Packs").exists()
    or (DATA_DIR / "00_Admin").exists()
)

PACKS_DIR = Path(
    env(
        "MODULAR_ONTOLOGY_PACKS_DIR",
        DATA_DIR / "02_Ontology_Packs" / "indexed" if USE_STRUCTURED_DATA_DIR else DATA_DIR / "packs",
    )
).resolve()
DB_PATH = Path(
    env(
        "MODULAR_ONTOLOGY_DB_PATH",
        DATA_DIR / "01_Database" / "modular_ontology.sqlite3" if USE_STRUCTURED_DATA_DIR else DATA_DIR / "modular_ontology.sqlite3",
    )
).resolve()
USERS_FILE = Path(
    env(
        "MODULAR_ONTOLOGY_USERS_FILE",
        DATA_DIR / "00_Admin" / "users.json" if USE_STRUCTURED_DATA_DIR else DATA_DIR / "users.json",
    )
).resolve()
MCP_REMOTE_FILE = Path(
    env(
        "MODULAR_ONTOLOGY_MCP_REMOTE_FILE",
        DATA_DIR / "00_Admin" / "mcp_remote.json" if USE_STRUCTURED_DATA_DIR else DATA_DIR / "mcp_remote.json",
    )
).resolve()
MCP_TOKENS_FILE = Path(
    env(
        "MODULAR_ONTOLOGY_MCP_TOKENS_FILE",
        DATA_DIR / "00_Admin" / "mcp_tokens.json" if USE_STRUCTURED_DATA_DIR else DATA_DIR / "mcp_tokens.json",
    )
).resolve()
