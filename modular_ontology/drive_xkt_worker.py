from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from .config import PROJECTS_FOLDER, ROOT, env
from .google_drive_sync import (
    PROJECT_IFC_FOLDER,
    GoogleDriveClient,
    _children_by_name,
    _safe_drive_filename_or_none,
)


def _shell_arg(path: Path) -> str:
    if os.name != "nt":
        return shlex.quote(str(path))
    return subprocess.list2cmdline([str(path)])


def _node_command() -> str:
    command = str(env("MODULAR_ONTOLOGY_NODE_COMMAND") or "node").strip()
    command_path = Path(command)
    return _shell_arg(command_path) if command_path.exists() else command


def _npx_command() -> str:
    command = str(env("MODULAR_ONTOLOGY_NPX_COMMAND") or "npx").strip()
    command_path = Path(command)
    return _shell_arg(command_path) if command_path.exists() else command


def _xkt_converter_candidates() -> list[Path]:
    relative = Path("node_modules") / "@xeokit" / "xeokit-convert" / "convert2xkt.js"
    roots = [ROOT, Path.cwd(), Path(__file__).resolve().parents[1]]
    candidates: list[Path] = []
    for root in roots:
        candidate = (root / relative).resolve()
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _default_xkt_converter_command() -> str:
    if str(env("MODULAR_ONTOLOGY_DISABLE_DEFAULT_XKT_CONVERTER") or "").strip() == "1":
        return ""
    for converter_path in _xkt_converter_candidates():
        if converter_path.exists():
            return f"{_node_command()} {_shell_arg(converter_path)} -s {{ifc}} -f ifc -o {{xkt}}"
    return f"{_npx_command()} -y @xeokit/xeokit-convert@1.3.2 -s {{ifc}} -f ifc -o {{xkt}}"


def _xkt_converter_command() -> str:
    configured = str(env("MODULAR_ONTOLOGY_XKT_CONVERTER_CMD") or env("XKT_CONVERTER_CMD") or "").strip()
    return configured or _default_xkt_converter_command()


def convert_missing_drive_xkts(
    *,
    client: GoogleDriveClient | None = None,
    root_folder_id: str | None = None,
    max_files: int | None = None,
) -> dict[str, Any]:
    root_folder_id = root_folder_id or str(env("MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID", "")).strip()
    if not root_folder_id:
        return {"status": "skipped", "reason": "MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID is not set.", "converted": []}

    command_template = _xkt_converter_command()
    if not command_template:
        return {"status": "skipped", "reason": "XKT converter command is not configured.", "converted": []}

    if max_files is None:
        max_files = int(str(env("MODULAR_ONTOLOGY_XKT_WORKER_MAX_FILES", "5")))

    timeout_seconds = int(str(env("MODULAR_ONTOLOGY_XKT_CONVERTER_TIMEOUT_SECONDS", "900")))
    client = client or GoogleDriveClient.from_env()
    root_children = _children_by_name(client, root_folder_id)
    projects_root = root_children.get(PROJECTS_FOLDER)
    if not projects_root or not projects_root.is_folder:
        return {"status": "skipped", "reason": f"Google Drive folder not found: {PROJECTS_FOLDER}", "converted": []}

    converted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    warnings: list[str] = []
    started_at = time.time()

    with tempfile.TemporaryDirectory(prefix="modular-ontology-xkt-") as temp_dir:
        work_dir = Path(temp_dir)
        for project in client.list_children(projects_root.id):
            if len(converted) >= max_files:
                break
            if not project.is_folder:
                continue
            project_id = _safe_drive_filename_or_none(project.name, warnings, f"{PROJECTS_FOLDER} project folder")
            if not project_id:
                continue
            project_children = _children_by_name(client, project.id)
            ifc_folder = project_children.get(PROJECT_IFC_FOLDER)
            if not ifc_folder or not ifc_folder.is_folder:
                continue
            file_children = _children_by_name(client, ifc_folder.id)
            xkt_names = {name for name, item in file_children.items() if not item.is_folder and Path(name).suffix.lower() == ".xkt"}
            for name, item in file_children.items():
                if len(converted) >= max_files:
                    break
                if item.is_folder or Path(name).suffix.lower() != ".ifc":
                    continue
                safe_name = _safe_drive_filename_or_none(name, warnings, f"{PROJECTS_FOLDER}/{project.name}/{PROJECT_IFC_FOLDER}")
                if not safe_name:
                    continue
                xkt_name = f"{Path(safe_name).stem}.xkt"
                if xkt_name in xkt_names:
                    skipped.append({"projectId": project_id, "ifc": safe_name, "reason": "xkt-exists"})
                    continue

                ifc_path = work_dir / project_id / safe_name
                xkt_path = ifc_path.with_suffix(".xkt")
                try:
                    client.download_file(item.id, ifc_path)
                    command = command_template.format(ifc=_shell_arg(ifc_path), xkt=_shell_arg(xkt_path))
                    result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout_seconds)
                    if result.returncode != 0 or not xkt_path.exists():
                        errors.append(
                            {
                                "projectId": project_id,
                                "ifc": safe_name,
                                "error": (result.stderr or result.stdout or "XKT converter did not create an output file.").strip(),
                            }
                        )
                        continue
                    upload_result = client.upload_file_by_name(
                        ifc_folder.id,
                        xkt_path,
                        name=xkt_name,
                        mime_type="application/octet-stream",
                    )
                    converted.append(
                        {
                            "projectId": project_id,
                            "ifc": safe_name,
                            "xkt": xkt_name,
                            "upload": upload_result.get("status"),
                            "driveFileId": upload_result.get("id"),
                        }
                    )
                    xkt_names.add(xkt_name)
                except subprocess.TimeoutExpired:
                    errors.append({"projectId": project_id, "ifc": safe_name, "error": f"XKT converter timed out after {timeout_seconds} seconds."})
                except Exception as exc:
                    errors.append({"projectId": project_id, "ifc": safe_name, "error": str(exc)})

    status = "converted" if converted else "no-pending"
    if errors and not converted:
        status = "error"
    elif errors:
        status = "partial"
    return {
        "status": status,
        "converted": converted,
        "skipped": skipped,
        "errors": errors,
        "warnings": warnings,
        "durationSeconds": round(time.time() - started_at, 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert Google Drive IFC files to XKT and upload them back beside the source IFC.")
    parser.add_argument("--max-files", type=int, default=None)
    args = parser.parse_args()
    print(json.dumps(convert_missing_drive_xkts(max_files=args.max_files), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
