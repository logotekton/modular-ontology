from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import TypeVar


T = TypeVar("T")

ENV_PREFIX = "MODULAR_ONTOLOGY"
LEGACY_ENV_PREFIXES = ("MOD" + "DULAR_GRAPH", "MODULAR_" + "GRAPH")
ADMIN_FOLDER = "00_Admin"
DATABASE_FOLDER = "01_Database"
PROJECTS_FOLDER = "02_Projects"
IFC_MODELS_FOLDER = "03_IFC_Models"
ONTOLOGY_PACKS_FOLDER = "04_Ontology_Packs"
MCP_FOLDER = "05_MCP"
BACKUPS_FOLDER = "07_Backups"
ARCHIVE_FOLDER = "99_Archive"

LEGACY_ONTOLOGY_PACKS_FOLDER = "02_Ontology_Packs"
LEGACY_PROJECTS_FOLDER = "03_Projects"
LEGACY_MCP_FOLDER = "04_MCP"
LEGACY_BACKUPS_FOLDER = "05_Backups"


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


def is_ephemeral_runtime(
    root: Path = ROOT,
    environment: Mapping[str, str] | None = None,
) -> bool:
    """Detect Vercel/AWS Lambda even when optional Vercel system env exposure is disabled."""

    runtime_env = os.environ if environment is None else environment
    if (
        runtime_env.get("VERCEL")
        or runtime_env.get("AWS_LAMBDA_FUNCTION_NAME")
        or runtime_env.get("LAMBDA_TASK_ROOT")
    ):
        return True
    root_path = root.as_posix().rstrip("/")
    return root_path == "/var/task" or root_path.startswith("/var/task/")
# Vercel 서버리스에서는 /tmp만 쓰기 가능하고 콜드 스타트마다 초기화된다 —
# 동기화 상태(파일 캐시/팩/SQLite)가 영속되지 않으므로 동기화는 로컬 실행 전용.
EPHEMERAL_STORAGE = is_ephemeral_runtime()
DEFAULT_DATA_DIR = Path("/tmp/modular-ontology") if EPHEMERAL_STORAGE else ROOT / "data"
DATA_DIR = Path(env("MODULAR_ONTOLOGY_DATA_DIR", DEFAULT_DATA_DIR)).resolve()
STRUCTURED_PACKS_DIR = DATA_DIR / ONTOLOGY_PACKS_FOLDER / "indexed"
LEGACY_STRUCTURED_PACKS_DIR = DATA_DIR / LEGACY_ONTOLOGY_PACKS_FOLDER / "indexed"
USE_STRUCTURED_DATA_DIR = (
    env("MODULAR_ONTOLOGY_STRUCTURED_DATA_DIR") == "1"
    or bool(env("MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID"))
    or (DATA_DIR / DATABASE_FOLDER).exists()
    or (DATA_DIR / ONTOLOGY_PACKS_FOLDER).exists()
    or (DATA_DIR / LEGACY_ONTOLOGY_PACKS_FOLDER).exists()
    or (DATA_DIR / ADMIN_FOLDER).exists()
)

PACKS_DIR = Path(
    env(
        "MODULAR_ONTOLOGY_PACKS_DIR",
        STRUCTURED_PACKS_DIR if USE_STRUCTURED_DATA_DIR else DATA_DIR / "packs",
    )
).resolve()
DB_PATH = Path(
    env(
        "MODULAR_ONTOLOGY_DB_PATH",
        DATA_DIR / DATABASE_FOLDER / "modular_ontology.sqlite3" if USE_STRUCTURED_DATA_DIR else DATA_DIR / "modular_ontology.sqlite3",
    )
).resolve()
USERS_FILE = Path(
    env(
        "MODULAR_ONTOLOGY_USERS_FILE",
        DATA_DIR / ADMIN_FOLDER / "users.json" if USE_STRUCTURED_DATA_DIR else DATA_DIR / "users.json",
    )
).resolve()
MCP_REMOTE_FILE = Path(
    env(
        "MODULAR_ONTOLOGY_MCP_REMOTE_FILE",
        DATA_DIR / ADMIN_FOLDER / "mcp_remote.json" if USE_STRUCTURED_DATA_DIR else DATA_DIR / "mcp_remote.json",
    )
).resolve()
MCP_TOKENS_FILE = Path(
    env(
        "MODULAR_ONTOLOGY_MCP_TOKENS_FILE",
        DATA_DIR / ADMIN_FOLDER / "mcp_tokens.json" if USE_STRUCTURED_DATA_DIR else DATA_DIR / "mcp_tokens.json",
    )
).resolve()
