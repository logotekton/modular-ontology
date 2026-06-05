from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(os.environ.get("MODDULAR_GRAPH_ROOT", Path(__file__).resolve().parents[1])).resolve()
DATA_DIR = Path(os.environ.get("MODDULAR_GRAPH_DATA_DIR", ROOT / "data")).resolve()
USE_STRUCTURED_DATA_DIR = (
    os.environ.get("MODDULAR_GRAPH_STRUCTURED_DATA_DIR") == "1"
    or (DATA_DIR / "01_Database").exists()
    or (DATA_DIR / "02_Ontology_Packs").exists()
    or (DATA_DIR / "00_Admin").exists()
)

PACKS_DIR = Path(
    os.environ.get(
        "MODDULAR_GRAPH_PACKS_DIR",
        DATA_DIR / "02_Ontology_Packs" / "indexed" if USE_STRUCTURED_DATA_DIR else DATA_DIR / "packs",
    )
).resolve()
DB_PATH = Path(
    os.environ.get(
        "MODDULAR_GRAPH_DB_PATH",
        DATA_DIR / "01_Database" / "moddular_graph.sqlite3" if USE_STRUCTURED_DATA_DIR else DATA_DIR / "moddular_graph.sqlite3",
    )
).resolve()
USERS_FILE = Path(
    os.environ.get(
        "MODDULAR_GRAPH_USERS_FILE",
        DATA_DIR / "00_Admin" / "users.json" if USE_STRUCTURED_DATA_DIR else DATA_DIR / "users.json",
    )
).resolve()
MCP_REMOTE_FILE = Path(
    os.environ.get(
        "MODDULAR_GRAPH_MCP_REMOTE_FILE",
        DATA_DIR / "00_Admin" / "mcp_remote.json" if USE_STRUCTURED_DATA_DIR else DATA_DIR / "mcp_remote.json",
    )
).resolve()
