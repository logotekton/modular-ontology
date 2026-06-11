from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from .config import DB_PATH, env


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_project_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS projects (
          id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          company TEXT NOT NULL DEFAULT '',
          manager TEXT NOT NULL DEFAULT '',
          discipline TEXT NOT NULL DEFAULT '',
          description TEXT NOT NULL DEFAULT '',
          role TEXT NOT NULL DEFAULT 'Admin',
          drive_folder_id TEXT,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS project_packs (
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          pack_id TEXT NOT NULL,
          linked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (project_id, pack_id)
        );

        CREATE INDEX IF NOT EXISTS idx_project_packs_pack ON project_packs(pack_id);
        """
    )
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(projects)").fetchall()}
    if "drive_folder_id" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN drive_folder_id TEXT")
    conn.commit()


def slugify_project_id(name: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z가-힣]+", "-", name.strip()).strip("-").lower()
    return normalized or "project"


def suggest_project_name(pack: dict[str, Any]) -> str:
    summary = pack.get("modelSummary") if isinstance(pack.get("modelSummary"), dict) else {}
    for key in ("project_name", "projectName", "model_name", "modelName", "building_name", "buildingName"):
        value = summary.get(key)
        if value:
            return str(value)
    title = str(pack.get("title") or pack.get("filename") or pack.get("id") or "Project")
    title = re.sub(r"\b(BM25|Evidence|Pack|LocalCrab|OpenCrab|IFC|Workset|Module)\b", " ", title, flags=re.I)
    title = re.sub(r"\s+", " ", title.replace(".zip", "")).strip()
    return title or "Project"


def _legacy_projects(packs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    advance_pack_ids = [pack["id"] for pack in packs if "advance" in pack["id"].lower()]
    revit_pack_ids = [pack["id"] for pack in packs if "revit" in pack["id"].lower()]
    projects = []
    if advance_pack_ids:
        projects.append(
            {
                "id": "samcheok-building-b",
                "name": "Samcheok Building B",
                "company": "Kumkang Kind",
                "manager": "Admin",
                "discipline": "Advance Steel",
                "description": "Advance Steel ontology workspace",
                "role": "Admin",
                "packIds": advance_pack_ids,
            }
        )
    if revit_pack_ids:
        projects.append(
            {
                "id": "yeoju-modular-dormitory",
                "name": "Yeoju Modular Dormitory",
                "company": "Kumkang Kind",
                "manager": "Admin",
                "discipline": "Revit IFC",
                "description": "Revit IFC ontology workspace",
                "role": "Admin",
                "packIds": revit_pack_ids,
            }
        )
    return projects


def seed_projects_from_packs(packs: list[dict[str, Any]], db_path: Path | None = None) -> None:
    if env("MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID"):
        return
    conn = connect(db_path)
    try:
        init_project_db(conn)
        existing = int(conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0])
        if existing:
            return
        with conn:
            for project in _legacy_projects(packs):
                conn.execute(
                    """
                    INSERT INTO projects (id, name, company, manager, discipline, description, role)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project["id"],
                        project["name"],
                        project["company"],
                        project["manager"],
                        project["discipline"],
                        project["description"],
                        project["role"],
                    ),
                )
                for pack_id in project["packIds"]:
                    conn.execute(
                        "INSERT OR IGNORE INTO project_packs (project_id, pack_id) VALUES (?, ?)",
                        (project["id"], pack_id),
                    )
    finally:
        conn.close()


def list_projects(packs: list[dict[str, Any]], db_path: Path | None = None) -> list[dict[str, Any]]:
    seed_projects_from_packs(packs, db_path)
    conn = connect(db_path)
    try:
        init_project_db(conn)
        rows = conn.execute(
            """
            SELECT id, name, company, manager, discipline, description, role, drive_folder_id
            FROM projects
            ORDER BY created_at ASC, name ASC
            """
        ).fetchall()
        pack_rows = conn.execute("SELECT project_id, pack_id FROM project_packs ORDER BY linked_at ASC").fetchall()
    finally:
        conn.close()

    pack_ids_by_project: dict[str, list[str]] = {}
    for row in pack_rows:
        pack_ids_by_project.setdefault(row["project_id"], []).append(row["pack_id"])
    valid_pack_ids = {pack["id"] for pack in packs}
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "company": row["company"],
            "manager": row["manager"],
            "discipline": row["discipline"],
            "description": row["description"],
            "role": row["role"],
            "driveFolderId": row["drive_folder_id"],
            "packIds": [pack_id for pack_id in pack_ids_by_project.get(row["id"], []) if pack_id in valid_pack_ids],
        }
        for row in rows
    ]


def create_project(
    *,
    name: str,
    company: str = "",
    manager: str = "",
    discipline: str = "",
    description: str = "",
    pack_ids: list[str] | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Project name is required.")
    base_id = slugify_project_id(clean_name)
    conn = connect(db_path)
    try:
        init_project_db(conn)
        project_id = base_id
        index = 2
        while conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone():
            project_id = f"{base_id}-{index}"
            index += 1
        with conn:
            conn.execute(
                """
                INSERT INTO projects (id, name, company, manager, discipline, description, drive_folder_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (project_id, clean_name, company.strip(), manager.strip(), discipline.strip(), description.strip(), None),
            )
            for pack_id in pack_ids or []:
                if str(pack_id).strip():
                    conn.execute(
                        "INSERT OR IGNORE INTO project_packs (project_id, pack_id) VALUES (?, ?)",
                        (project_id, str(pack_id).strip()),
                    )
        return get_project(project_id, db_path=db_path)
    finally:
        conn.close()


def get_project(project_id: str, db_path: Path | None = None) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        init_project_db(conn)
        row = conn.execute(
            """
            SELECT id, name, company, manager, discipline, description, role, drive_folder_id
            FROM projects
            WHERE id = ?
            """,
            (project_id,),
        ).fetchone()
        if not row:
            raise KeyError(project_id)
        pack_rows = conn.execute(
            "SELECT pack_id FROM project_packs WHERE project_id = ? ORDER BY linked_at ASC",
            (project_id,),
        ).fetchall()
        return {
            "id": row["id"],
            "name": row["name"],
            "company": row["company"],
            "manager": row["manager"],
            "discipline": row["discipline"],
            "description": row["description"],
            "role": row["role"],
            "driveFolderId": row["drive_folder_id"],
            "packIds": [pack_row["pack_id"] for pack_row in pack_rows],
        }
    finally:
        conn.close()


def update_project(
    project_id: str,
    *,
    name: str,
    company: str = "",
    manager: str = "",
    discipline: str = "",
    description: str = "",
    db_path: Path | None = None,
) -> None:
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Project name is required.")
    conn = connect(db_path)
    try:
        init_project_db(conn)
        with conn:
            result = conn.execute(
                """
                UPDATE projects
                SET name = ?, company = ?, manager = ?, discipline = ?, description = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (clean_name, company.strip(), manager.strip(), discipline.strip(), description.strip(), project_id),
            )
        if result.rowcount == 0:
            raise KeyError(project_id)
    finally:
        conn.close()


def sync_projects_from_drive_folders(
    drive_projects: list[dict[str, str]],
    db_path: Path | None = None,
) -> dict[str, Any]:
    normalized: list[dict[str, str]] = []
    seen_folder_ids: set[str] = set()
    for item in drive_projects:
        folder_id = str(item.get("folderId") or "").strip()
        project_id = str(item.get("projectId") or "").strip()
        name = str(item.get("name") or project_id).strip()
        if not folder_id or not project_id or folder_id in seen_folder_ids:
            continue
        seen_folder_ids.add(folder_id)
        normalized.append({"folderId": folder_id, "projectId": project_id, "name": name or project_id})

    created: list[str] = []
    updated: list[str] = []
    renamed: list[dict[str, str]] = []
    deleted: list[str] = []
    conflicts: list[dict[str, str]] = []
    active_folder_ids = {item["folderId"] for item in normalized}
    active_project_ids = {item["projectId"] for item in normalized}
    conn = connect(db_path)
    try:
        init_project_db(conn)
        with conn:
            for row in conn.execute(
                """
                SELECT id, drive_folder_id
                FROM projects
                """
            ).fetchall():
                project_id = str(row["id"])
                drive_folder_id = str(row["drive_folder_id"] or "")
                if project_id in active_project_ids or drive_folder_id in active_folder_ids:
                    continue
                conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
                deleted.append(project_id)

            for item in normalized:
                folder_id = item["folderId"]
                project_id = item["projectId"]
                name = item["name"]
                existing = conn.execute(
                    "SELECT * FROM projects WHERE drive_folder_id = ?",
                    (folder_id,),
                ).fetchone()
                if existing:
                    old_id = str(existing["id"])
                    if old_id != project_id:
                        conflict = conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone()
                        if conflict:
                            conn.execute(
                                """
                                UPDATE projects
                                SET name = ?, drive_folder_id = ?, updated_at = CURRENT_TIMESTAMP
                                WHERE id = ?
                                """,
                                (name, folder_id, old_id),
                            )
                            conflicts.append({"from": old_id, "to": project_id, "folderId": folder_id})
                            updated.append(old_id)
                            continue
                        conn.execute(
                            """
                            INSERT INTO projects
                              (id, name, company, manager, discipline, description, role, drive_folder_id, created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                            """,
                            (
                                project_id,
                                name,
                                existing["company"],
                                existing["manager"],
                                existing["discipline"],
                                existing["description"],
                                existing["role"],
                                folder_id,
                                existing["created_at"],
                            ),
                        )
                        conn.execute("UPDATE project_packs SET project_id = ? WHERE project_id = ?", (project_id, old_id))
                        conn.execute("DELETE FROM projects WHERE id = ?", (old_id,))
                        renamed.append({"from": old_id, "to": project_id, "folderId": folder_id})
                    else:
                        conn.execute(
                            """
                            UPDATE projects
                            SET name = ?, drive_folder_id = ?, updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                            """,
                            (name, folder_id, old_id),
                        )
                        updated.append(old_id)
                    continue

                existing_by_id = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
                if existing_by_id:
                    conn.execute(
                        """
                        UPDATE projects
                        SET name = ?, drive_folder_id = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (name, folder_id, project_id),
                    )
                    updated.append(project_id)
                    continue

                conn.execute(
                    """
                    INSERT INTO projects (id, name, company, manager, discipline, description, drive_folder_id)
                    VALUES (?, ?, '', '', '', '', ?)
                    """,
                    (project_id, name, folder_id),
                )
                created.append(project_id)
    finally:
        conn.close()
    return {"created": created, "updated": updated, "renamed": renamed, "deleted": deleted, "conflicts": conflicts}


def delete_project(project_id: str, db_path: Path | None = None) -> None:
    conn = connect(db_path)
    try:
        init_project_db(conn)
        with conn:
            result = conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        if result.rowcount == 0:
            raise KeyError(project_id)
    finally:
        conn.close()


def set_project_packs(project_id: str, pack_ids: list[str], db_path: Path | None = None) -> list[str]:
    normalized = [str(pack_id).strip() for pack_id in pack_ids if str(pack_id).strip()]
    conn = connect(db_path)
    try:
        init_project_db(conn)
        if not conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone():
            raise KeyError(project_id)
        with conn:
            conn.execute("DELETE FROM project_packs WHERE project_id = ?", (project_id,))
            for pack_id in dict.fromkeys(normalized):
                conn.execute(
                    "INSERT INTO project_packs (project_id, pack_id) VALUES (?, ?)",
                    (project_id, pack_id),
                )
        return list(dict.fromkeys(normalized))
    finally:
        conn.close()


def attach_pack_to_project(project_id: str, pack_id: str, db_path: Path | None = None) -> None:
    set_project_packs(project_id, [*get_project(project_id, db_path=db_path).get("packIds", []), pack_id], db_path=db_path)
