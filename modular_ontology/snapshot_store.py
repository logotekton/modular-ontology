"""Canonical project snapshot manifests and a small file-backed registry.

A snapshot manifest fixes the exact pack set that is allowed to answer project
queries.  Snapshot identity is derived from normalized manifest content rather
than timestamps or JSON serialization order, so the same source policy always
has the same ID.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import tempfile
from typing import Any, Iterable, Mapping
from zipfile import BadZipFile, ZipFile


SCHEMA_VERSION = 1
REGISTRY_VERSION = 1
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SnapshotError(ValueError):
    """Base class for malformed snapshot state."""


class SnapshotManifestError(SnapshotError):
    """Raised when a snapshot manifest is malformed or self-inconsistent."""


class SnapshotVerificationError(SnapshotError):
    """Raised when an invalid snapshot is activated or resolved from registry."""

    def __init__(self, result: "SnapshotVerification") -> None:
        details = "; ".join(issue.message for issue in result.issues)
        super().__init__(f"snapshot {result.snapshot_id} failed verification: {details}")
        self.result = result


class SnapshotRegistryError(SnapshotError):
    """Raised when the active snapshot registry is malformed or stale."""


@dataclass(frozen=True, slots=True)
class SnapshotPack:
    """One pack declaration in a canonical snapshot."""

    pack_id: str
    path: Path
    sha256: str
    role: str
    shard_index: int | None
    included: bool
    declared_path: str


@dataclass(frozen=True, slots=True)
class ProjectSnapshot:
    """Loaded, normalized project snapshot."""

    schema_version: int
    project_id: str
    source_signature: str
    packs: tuple[SnapshotPack, ...]
    snapshot_hash: str
    snapshot_id: str
    manifest_path: Path


@dataclass(frozen=True, slots=True)
class VerificationIssue:
    """A machine-readable snapshot verification finding."""

    code: str
    message: str
    pack_id: str | None = None


@dataclass(frozen=True, slots=True)
class SnapshotVerification:
    """Result of verifying every included pack in a snapshot."""

    snapshot_id: str
    checked_pack_ids: tuple[str, ...]
    issues: tuple[VerificationIssue, ...]
    warnings: tuple[VerificationIssue, ...]

    @property
    def valid(self) -> bool:
        return not self.issues

    def __bool__(self) -> bool:
        return self.valid

    def require_valid(self) -> "SnapshotVerification":
        if not self.valid:
            raise SnapshotVerificationError(self)
        return self


def sha256_path(path: str | Path) -> str:
    """Return a deterministic SHA-256 for a file or directory.

    File hashing is the standard SHA-256 over bytes.  Directory hashing covers
    sorted POSIX-style relative paths and file bytes, deliberately excluding
    timestamps and permissions.
    """

    resolved = Path(path)
    if resolved.is_file():
        digest = hashlib.sha256()
        with resolved.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    if resolved.is_dir():
        digest = hashlib.sha256()
        files = sorted(
            (candidate for candidate in resolved.rglob("*") if candidate.is_file()),
            key=lambda candidate: candidate.relative_to(resolved).as_posix(),
        )
        for candidate in files:
            relative_path = candidate.relative_to(resolved).as_posix().encode("utf-8")
            digest.update(b"file\0")
            digest.update(relative_path)
            digest.update(b"\0")
            with candidate.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            digest.update(b"\0")
        return digest.hexdigest()

    raise FileNotFoundError(resolved)


def load_snapshot(manifest_path: str | Path) -> ProjectSnapshot:
    """Load and normalize a canonical snapshot JSON manifest."""

    path = Path(manifest_path).expanduser().resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotManifestError(f"cannot read snapshot manifest {path}: {exc}") from exc

    if not isinstance(payload, Mapping):
        raise SnapshotManifestError("snapshot manifest must be a JSON object")

    schema_version = payload.get("schema_version", SCHEMA_VERSION)
    if schema_version != SCHEMA_VERSION:
        raise SnapshotManifestError(
            f"unsupported schema_version {schema_version!r}; expected {SCHEMA_VERSION}"
        )

    project_id = _required_string(payload, "project_id")
    source_signature = _required_string(payload, "source_signature")
    raw_packs = payload.get("packs")
    if not isinstance(raw_packs, list):
        raise SnapshotManifestError("packs must be a JSON array")

    packs: list[SnapshotPack] = []
    identity_packs: list[dict[str, Any]] = []
    seen_pack_ids: set[str] = set()
    for index, raw_pack in enumerate(raw_packs):
        if not isinstance(raw_pack, Mapping):
            raise SnapshotManifestError(f"packs[{index}] must be a JSON object")
        pack_id = _required_string(raw_pack, "pack_id", prefix=f"packs[{index}].")
        if pack_id in seen_pack_ids:
            raise SnapshotManifestError(f"duplicate pack_id: {pack_id}")
        seen_pack_ids.add(pack_id)

        sha256 = _required_string(raw_pack, "sha256", prefix=f"packs[{index}].").lower()
        if not _SHA256_RE.fullmatch(sha256):
            raise SnapshotManifestError(f"packs[{index}].sha256 must be 64 hexadecimal characters")
        declared_path = _required_string(raw_pack, "path", prefix=f"packs[{index}].")
        normalized_path = _normalize_declared_path(declared_path)
        pack_path = Path(declared_path).expanduser()
        if not pack_path.is_absolute():
            pack_path = path.parent / pack_path
        pack_path = pack_path.resolve()
        if not pack_path.exists():
            configured_pack_root = os.environ.get("MODULAR_ONTOLOGY_SNAPSHOT_PACK_DIR", "").strip()
            pack_root = (
                Path(configured_pack_root).expanduser()
                if configured_pack_root
                else path.parent.parent / "canonical_packs"
            ).resolve()
            content_addressed_path = pack_root / f"{sha256}.zip"
            if content_addressed_path.is_file():
                pack_path = content_addressed_path
        role = _required_string(raw_pack, "role", prefix=f"packs[{index}].")

        shard_index = raw_pack.get("shard_index")
        if shard_index is not None and (
            isinstance(shard_index, bool) or not isinstance(shard_index, int) or shard_index < 0
        ):
            raise SnapshotManifestError(f"packs[{index}].shard_index must be a non-negative integer or null")

        included = raw_pack.get("included", True)
        if not isinstance(included, bool):
            raise SnapshotManifestError(f"packs[{index}].included must be a boolean")

        packs.append(
            SnapshotPack(
                pack_id=pack_id,
                path=pack_path,
                sha256=sha256,
                role=role,
                shard_index=shard_index,
                included=included,
                declared_path=normalized_path,
            )
        )
        identity_packs.append(
            {
                "pack_id": pack_id,
                "path": normalized_path,
                "sha256": sha256,
                "role": role,
                "shard_index": shard_index,
                "included": included,
            }
        )

    identity_packs.sort(key=_identity_pack_sort_key)
    identity = {
        "schema_version": schema_version,
        "project_id": project_id,
        "source_signature": source_signature,
        "packs": identity_packs,
    }
    canonical_json = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    snapshot_hash = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    snapshot_id = f"snapshot-{snapshot_hash[:24]}"

    declared_hash = payload.get("snapshot_hash")
    if declared_hash is not None and declared_hash != snapshot_hash:
        raise SnapshotManifestError(
            f"declared snapshot_hash {declared_hash!r} does not match computed {snapshot_hash}"
        )
    declared_id = payload.get("snapshot_id")
    if declared_id is not None and declared_id != snapshot_id:
        raise SnapshotManifestError(
            f"declared snapshot_id {declared_id!r} does not match computed {snapshot_id}"
        )

    return ProjectSnapshot(
        schema_version=schema_version,
        project_id=project_id,
        source_signature=source_signature,
        packs=tuple(packs),
        snapshot_hash=snapshot_hash,
        snapshot_id=snapshot_id,
        manifest_path=path,
    )


def resolve_snapshot_packs(
    snapshot: ProjectSnapshot | str | Path,
    role: str | None = None,
) -> tuple[SnapshotPack, ...]:
    """Resolve the deterministic, included pack set, optionally for one role."""

    loaded = _coerce_snapshot(snapshot)
    selected = [
        pack for pack in loaded.packs if pack.included and (role is None or pack.role == role)
    ]
    selected.sort(key=_snapshot_pack_sort_key)
    return tuple(selected)


def verify_snapshot(snapshot: ProjectSnapshot | str | Path) -> SnapshotVerification:
    """Verify included pack existence, hashes, embedded IDs, and shard continuity."""

    loaded = _coerce_snapshot(snapshot)
    included = resolve_snapshot_packs(loaded)
    issues: list[VerificationIssue] = []
    warnings: list[VerificationIssue] = []

    if not included:
        issues.append(VerificationIssue("empty_snapshot", "snapshot has no included packs"))

    seen_paths: dict[Path, str] = {}
    for pack in included:
        previous_pack_id = seen_paths.get(pack.path)
        if previous_pack_id is not None:
            issues.append(
                VerificationIssue(
                    "duplicate_pack_path",
                    f"pack path is shared with {previous_pack_id}: {pack.path}",
                    pack.pack_id,
                )
            )
            continue
        seen_paths[pack.path] = pack.pack_id

        if not pack.path.exists():
            issues.append(
                VerificationIssue("pack_missing", f"pack path does not exist: {pack.path}", pack.pack_id)
            )
            continue

        try:
            actual_hash = sha256_path(pack.path)
        except OSError as exc:
            issues.append(
                VerificationIssue("pack_unreadable", f"cannot hash pack {pack.path}: {exc}", pack.pack_id)
            )
            continue
        if actual_hash != pack.sha256:
            issues.append(
                VerificationIssue(
                    "sha256_mismatch",
                    f"expected sha256 {pack.sha256}, got {actual_hash}",
                    pack.pack_id,
                )
            )

        try:
            embedded_pack_id = _read_embedded_pack_id(pack.path)
        except (OSError, ValueError, BadZipFile, json.JSONDecodeError) as exc:
            issues.append(
                VerificationIssue(
                    "pack_manifest_invalid",
                    f"cannot inspect pack manifest: {exc}",
                    pack.pack_id,
                )
            )
        else:
            if embedded_pack_id is None:
                warnings.append(
                    VerificationIssue(
                        "pack_id_unverifiable",
                        "pack has no readable manifest pack_id",
                        pack.pack_id,
                    )
                )
            elif embedded_pack_id != pack.pack_id:
                issues.append(
                    VerificationIssue(
                        "pack_id_mismatch",
                        f"snapshot pack_id {pack.pack_id!r} does not match embedded {embedded_pack_id!r}",
                        pack.pack_id,
                    )
                )

    _verify_shards(included, issues)
    return SnapshotVerification(
        snapshot_id=loaded.snapshot_id,
        checked_pack_ids=tuple(pack.pack_id for pack in included),
        issues=tuple(issues),
        warnings=tuple(warnings),
    )


def activate(
    snapshot: ProjectSnapshot | str | Path,
    registry_path: str | Path,
    *,
    verify: bool = True,
) -> ProjectSnapshot:
    """Atomically set a project's active snapshot in a JSON registry."""

    loaded = _coerce_snapshot(snapshot)
    if verify:
        verify_snapshot(loaded).require_valid()

    path = Path(registry_path).expanduser().resolve()
    registry = _read_registry(path)
    active = registry.setdefault("active", {})
    if not isinstance(active, dict):
        raise SnapshotRegistryError("registry active field must be an object")

    active[loaded.project_id] = {
        "snapshot_id": loaded.snapshot_id,
        "snapshot_hash": loaded.snapshot_hash,
        "source_signature": loaded.source_signature,
        "manifest_path": _registry_manifest_path(loaded.manifest_path, path.parent),
    }
    _atomic_write_json(path, registry)
    return loaded


def get_active(
    project_id: str,
    registry_path: str | Path,
    *,
    verify: bool = True,
) -> ProjectSnapshot | None:
    """Load a project's active snapshot and reject stale registry entries."""

    normalized_project_id = project_id.strip()
    if not normalized_project_id:
        raise SnapshotRegistryError("project_id must be a non-empty string")

    path = Path(registry_path).expanduser().resolve()
    if not path.exists():
        return None
    registry = _read_registry(path)
    active = registry.get("active")
    if not isinstance(active, Mapping):
        raise SnapshotRegistryError("registry active field must be an object")
    entry = active.get(normalized_project_id)
    if entry is None:
        return None
    if not isinstance(entry, Mapping):
        raise SnapshotRegistryError(f"active entry for {normalized_project_id!r} must be an object")

    manifest_value = entry.get("manifest_path")
    if not isinstance(manifest_value, str) or not manifest_value.strip():
        raise SnapshotRegistryError("active entry has no manifest_path")
    manifest_path = Path(manifest_value)
    if not manifest_path.is_absolute():
        manifest_path = path.parent / manifest_path
    try:
        loaded = load_snapshot(manifest_path)
    except (OSError, SnapshotManifestError) as exc:
        raise SnapshotRegistryError(f"cannot load active snapshot: {exc}") from exc

    expected = {
        "project_id": normalized_project_id,
        "snapshot_id": entry.get("snapshot_id"),
        "snapshot_hash": entry.get("snapshot_hash"),
        "source_signature": entry.get("source_signature"),
    }
    actual = {
        "project_id": loaded.project_id,
        "snapshot_id": loaded.snapshot_id,
        "snapshot_hash": loaded.snapshot_hash,
        "source_signature": loaded.source_signature,
    }
    if actual != expected:
        raise SnapshotRegistryError(
            f"active snapshot registry entry is stale: expected {expected!r}, got {actual!r}"
        )
    if verify:
        try:
            verify_snapshot(loaded).require_valid()
        except SnapshotVerificationError as exc:
            raise SnapshotRegistryError(str(exc)) from exc
    return loaded


def _required_string(payload: Mapping[str, Any], key: str, *, prefix: str = "") -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SnapshotManifestError(f"{prefix}{key} must be a non-empty string")
    return value.strip()


def _normalize_declared_path(path: str) -> str:
    normalized = posixpath.normpath(path.strip().replace("\\", "/"))
    return normalized if normalized != "." else path.strip().replace("\\", "/")


def _identity_pack_sort_key(pack: Mapping[str, Any]) -> tuple[Any, ...]:
    shard_index = pack["shard_index"]
    return (
        str(pack["role"]).casefold(),
        shard_index is None,
        shard_index if shard_index is not None else 0,
        str(pack["pack_id"]).casefold(),
        str(pack["path"]),
    )


def _snapshot_pack_sort_key(pack: SnapshotPack) -> tuple[Any, ...]:
    return (
        pack.role.casefold(),
        pack.shard_index is None,
        pack.shard_index if pack.shard_index is not None else 0,
        pack.pack_id.casefold(),
        pack.declared_path,
    )


def _coerce_snapshot(snapshot: ProjectSnapshot | str | Path) -> ProjectSnapshot:
    return snapshot if isinstance(snapshot, ProjectSnapshot) else load_snapshot(snapshot)


def _read_embedded_pack_id(path: Path) -> str | None:
    manifest_payload: Any | None = None
    if path.is_dir():
        manifest_path = path / "manifest.json"
        if not manifest_path.is_file():
            return None
        manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    elif path.suffix.casefold() == ".zip":
        with ZipFile(path) as archive:
            names = [name for name in archive.namelist() if not name.endswith("/")]
            exact = [name for name in names if name.casefold() == "manifest.json"]
            nested = [name for name in names if name.casefold().endswith("/manifest.json")]
            candidates = exact or nested
            if not candidates:
                return None
            if len(candidates) > 1:
                raise ValueError(f"multiple manifest.json files in archive: {candidates!r}")
            manifest_payload = json.loads(archive.read(candidates[0]).decode("utf-8-sig"))
    else:
        return None

    if not isinstance(manifest_payload, Mapping):
        raise ValueError("pack manifest must be a JSON object")
    embedded = manifest_payload.get("pack_id", manifest_payload.get("id"))
    if embedded is None:
        return None
    if not isinstance(embedded, str) or not embedded.strip():
        raise ValueError("pack manifest pack_id must be a non-empty string")
    return embedded.strip()


def _verify_shards(
    packs: Iterable[SnapshotPack],
    issues: list[VerificationIssue],
) -> None:
    by_role: dict[str, list[SnapshotPack]] = {}
    for pack in packs:
        by_role.setdefault(pack.role, []).append(pack)

    for role, role_packs in sorted(by_role.items()):
        indexed = [pack for pack in role_packs if pack.shard_index is not None]
        if not indexed:
            continue
        unindexed = [pack for pack in role_packs if pack.shard_index is None]
        if unindexed:
            issues.append(
                VerificationIssue(
                    "mixed_shard_indexing",
                    f"role {role!r} mixes indexed and unindexed packs",
                )
            )
            continue

        indices = [pack.shard_index for pack in indexed]
        assert all(index is not None for index in indices)
        concrete_indices = [int(index) for index in indices]
        if len(set(concrete_indices)) != len(concrete_indices):
            issues.append(
                VerificationIssue("duplicate_shard_index", f"role {role!r} has duplicate shard indices")
            )
            continue
        first = min(concrete_indices)
        if first not in (0, 1):
            issues.append(
                VerificationIssue(
                    "invalid_shard_start",
                    f"role {role!r} shard indices must start at 0 or 1, got {first}",
                )
            )
            continue
        expected = list(range(first, max(concrete_indices) + 1))
        if sorted(concrete_indices) != expected:
            missing = sorted(set(expected) - set(concrete_indices))
            issues.append(
                VerificationIssue(
                    "shard_gap",
                    f"role {role!r} has non-contiguous shards; missing {missing}",
                )
            )


def _read_registry(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": REGISTRY_VERSION, "active": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotRegistryError(f"cannot read registry {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SnapshotRegistryError("registry must be a JSON object")
    if payload.get("schema_version") != REGISTRY_VERSION:
        raise SnapshotRegistryError(
            f"unsupported registry schema_version {payload.get('schema_version')!r}"
        )
    return payload


def _registry_manifest_path(manifest_path: Path, registry_directory: Path) -> str:
    try:
        return Path(os.path.relpath(manifest_path, registry_directory)).as_posix()
    except ValueError:
        return str(manifest_path)


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


__all__ = [
    "ProjectSnapshot",
    "SnapshotError",
    "SnapshotManifestError",
    "SnapshotPack",
    "SnapshotRegistryError",
    "SnapshotVerification",
    "SnapshotVerificationError",
    "VerificationIssue",
    "activate",
    "get_active",
    "load_snapshot",
    "resolve_snapshot_packs",
    "sha256_path",
    "verify_snapshot",
]
