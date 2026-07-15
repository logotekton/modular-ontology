"""Strict, snapshot-bound coverage scans for absence evidence.

This module deliberately has no query-ID or project-specific profiles.  A caller
supplies a small allowlisted scan specification and the exact fields it is
permitted to inspect.  Every selected pack is hash-checked before and after a
full JSONL scan; a result is returned only after all packs reach EOF.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import unicodedata
import zipfile
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


class CoverageScanError(ValueError):
    """Raised when a coverage scan cannot produce a complete certificate."""


_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_MISSING = object()
_MAX_SCOPE_FILTERS = 16
_MAX_SCOPE_IN_VALUES = 64
_MAX_NESTED_DEPTH = 8
_MAX_NESTED_LEAVES = 1024
_MATCHED_ID_SAMPLE_LIMIT = 20
_DECLARED_COUNT_MISMATCH_SAMPLE_LIMIT = 20
_COVERAGE_SCHEMA_VERSION = "mo-coverage-scan/1.0"
_ROLE_SCHEMA_VERSION = "mo-role-coverage/1.0"


@dataclass(frozen=True)
class _Pack:
    pack_id: str
    role: str
    path: Path
    sha256: str

    def descriptor(self) -> dict[str, str]:
        return {"pack_id": self.pack_id, "role": self.role, "sha256": self.sha256}


@dataclass(frozen=True)
class _ScopeFilter:
    field: str
    operator: str
    values: tuple[tuple[str, Any], ...]


@dataclass(frozen=True)
class _NestedField:
    max_depth: int
    max_leaves: int


@dataclass(frozen=True)
class _ScanSpec:
    roles: tuple[str, ...]
    node_types: tuple[str, ...]
    search_fields: tuple[str, ...]
    scope_filters: tuple[_ScopeFilter, ...]
    search_op: str
    terms: tuple[str, ...]
    normalized_terms: tuple[str, ...]
    nested_fields: Mapping[str, _NestedField]

    def digest_payload(self) -> dict[str, Any]:
        scope_filters = []
        for item in self.scope_filters:
            values = [
                {
                    "domain": domain,
                    "value": str(value) if isinstance(value, Decimal) else value,
                }
                for domain, value in item.values
            ]
            scope_filters.append(
                {
                    "field": item.field,
                    "operator": item.operator,
                    "value": values[0] if item.operator == "eq" else values,
                }
            )
        return {
            "roles": list(self.roles),
            "node_types": list(self.node_types),
            "search_fields": list(self.search_fields),
            "scope_filters": scope_filters,
            "search": {"op": self.search_op, "terms": list(self.terms)},
            "nested_fields": {
                field: {
                    "max_depth": value.max_depth,
                    "max_leaves": value.max_leaves,
                }
                for field, value in sorted(self.nested_fields.items())
            },
        }


def scan_snapshot_nodes(
    snapshot: Mapping[str, Any],
    spec: Mapping[str, Any],
    *,
    allowed_fields: Collection[str],
) -> dict[str, Any]:
    """Scan selected snapshot roles to EOF and return an absence-safe certificate.

    ``spec`` contains exactly these JSON fields::

        {
          "roles": ["boq"],
          "node_types": ["BOQItem"],
          "search_fields": ["item_name", "specification"],
          "scope_filters": [
            {"field": "work_category", "operator": "eq", "value": "wall"}
          ],
          "search": {"op": "exact_token", "terms": ["D9"]},
          "nested_fields": {
            "parameters_sample": {"max_depth": 4, "max_leaves": 256}
          }
        }

    ``scope_filters`` and ``nested_fields`` are optional.  Fields outside the
    caller-provided ``allowed_fields`` set are rejected rather than ignored.
    """

    if not isinstance(snapshot, Mapping):
        raise CoverageScanError("snapshot must be a mapping")
    validated = _validate_scan_spec(spec, allowed_fields)
    packs, _ = _snapshot_packs(snapshot)
    requested_roles = set(validated.roles)
    selected = [pack for pack in packs if pack.role in requested_roles]
    role_counts = Counter(pack.role for pack in selected)
    missing_roles = [role for role in validated.roles if role_counts[role] == 0]
    if missing_roles:
        raise CoverageScanError(
            f"snapshot contains no included packs for roles: {sorted(missing_roles)!r}"
        )

    selected.sort(key=lambda item: (item.role, item.pack_id))
    selected_descriptors = [item.descriptor() for item in selected]
    # Pack-local IDs must always be internally coherent.  Across packs, only
    # requested node types share the query result identity domain: auxiliary
    # vocabulary nodes are commonly re-declared with pack-specific metadata.
    seen_pack_nodes: dict[tuple[str, str], str] = {}
    seen_selected_nodes: dict[str, str] = {}
    matched_ids: set[str] = set()
    term_match_counts = {term: 0 for term in validated.terms}
    selected_node_type_counts: Counter[str] = Counter()
    scoped_node_type_counts: Counter[str] = Counter()
    search_field_presence_counts: Counter[str] = Counter()
    scanned_record_count = 0
    scoped_record_count = 0
    duplicate_records_removed = 0
    verified_pack_count = 0
    schema_pack_count = 0
    eof_pack_count = 0
    declared_node_count_pack_count = 0
    declared_count_match_pack_count = 0
    declared_count_mismatch_pack_count = 0
    declared_count_mismatch_sample: list[dict[str, Any]] = []

    for pack in selected:
        pre_scan_hash = _sha256_file(pack.path)
        if pre_scan_hash != pack.sha256:
            raise CoverageScanError(
                f"pre-scan SHA-256 mismatch for {pack.pack_id}: "
                f"expected {pack.sha256}, got {pre_scan_hash}"
            )
        verified_pack_count += 1
        physical_count = 0
        declared_count: int | None = None
        try:
            with zipfile.ZipFile(pack.path) as archive:
                declared_count = _declared_node_count(archive, pack)
                names = archive.namelist()
                if names.count("graph/nodes.jsonl") != 1:
                    raise CoverageScanError(
                        f"{pack.pack_id} must contain exactly one graph/nodes.jsonl"
                    )
                with archive.open("graph/nodes.jsonl") as stream:
                    for line_number, raw_line in enumerate(stream, start=1):
                        if not raw_line.strip():
                            continue
                        physical_count += 1
                        scanned_record_count += 1
                        node = _load_json_object(
                            raw_line,
                            source=f"{pack.pack_id}!graph/nodes.jsonl:{line_number}",
                        )
                        node_id, node_type = _validate_node(node, pack, line_number)
                        canonical_payload = _canonical_json(node)
                        pack_node_key = (pack.pack_id, node_id)
                        previous = seen_pack_nodes.get(pack_node_key)
                        if previous is not None:
                            if previous != canonical_payload:
                                raise CoverageScanError(
                                    f"conflicting duplicate node id {node_id!r} "
                                    f"within pack {pack.pack_id!r}"
                                )
                            duplicate_records_removed += 1
                            continue
                        seen_pack_nodes[pack_node_key] = canonical_payload
                        if node_type not in validated.node_types:
                            continue
                        previous = seen_selected_nodes.get(node_id)
                        if previous is not None:
                            if previous != canonical_payload:
                                raise CoverageScanError(
                                    f"conflicting duplicate selected node id {node_id!r}"
                                )
                            duplicate_records_removed += 1
                            continue
                        seen_selected_nodes[node_id] = canonical_payload
                        selected_node_type_counts[node_type] += 1
                        row = _flatten_node(node, pack.pack_id, line_number)
                        if not _matches_scope(
                            row, validated.scope_filters, source=node_id
                        ):
                            continue
                        scoped_record_count += 1
                        scoped_node_type_counts[node_type] += 1
                        present_search_fields = [
                            field
                            for field in validated.search_fields
                            if row.get(field, _MISSING) is not _MISSING
                            and row.get(field) is not None
                        ]
                        if not present_search_fields:
                            raise CoverageScanError(
                                f"scoped node {node_id!r} has none of the requested "
                                "search_fields"
                            )
                        search_field_presence_counts.update(present_search_fields)
                        searchable_values = _searchable_values(row, validated)
                        matched_term_indexes = _matched_terms(
                            searchable_values, validated
                        )
                        if not matched_term_indexes:
                            continue
                        matched_ids.add(node_id)
                        for index in matched_term_indexes:
                            term_match_counts[validated.terms[index]] += 1
        except zipfile.BadZipFile as exc:
            raise CoverageScanError(f"invalid ZIP pack {pack.pack_id}") from exc
        if declared_count is not None:
            declared_node_count_pack_count += 1
            if physical_count == declared_count:
                declared_count_match_pack_count += 1
            else:
                declared_count_mismatch_pack_count += 1
                if (
                    len(declared_count_mismatch_sample)
                    < _DECLARED_COUNT_MISMATCH_SAMPLE_LIMIT
                ):
                    declared_count_mismatch_sample.append(
                        {
                            "pack_id": pack.pack_id,
                            "declared": declared_count,
                            "scanned": physical_count,
                        }
                    )
        # This count certifies JSONL member/record schema validity through EOF.
        # A stale advisory manifest count is reported separately below.
        schema_pack_count += 1
        eof_pack_count += 1
        post_scan_hash = _sha256_file(pack.path)
        if post_scan_hash != pre_scan_hash or post_scan_hash != pack.sha256:
            raise CoverageScanError(
                f"post-scan SHA-256 drift for {pack.pack_id}: "
                f"before {pre_scan_hash}, after {post_scan_hash}"
            )

    missing_node_types = [
        node_type
        for node_type in validated.node_types
        if selected_node_type_counts[node_type] == 0
    ]
    if missing_node_types:
        raise CoverageScanError(
            "requested node_types were not observed in the selected packs: "
            f"{missing_node_types!r}"
        )
    missing_search_fields = [
        field
        for field in validated.search_fields
        if search_field_presence_counts[field] == 0
    ]
    if missing_search_fields:
        raise CoverageScanError(
            "requested search_fields were not observed in the scoped records: "
            f"{missing_search_fields!r}"
        )

    sorted_matches = sorted(matched_ids)
    match_digest = _sha256_json(sorted_matches)
    snapshot_identity = _snapshot_identity(snapshot)
    scan_spec = validated.digest_payload()
    digest_basis = {
        "snapshot": snapshot_identity,
        "spec": scan_spec,
        "source_packs": selected_descriptors,
        "scanned_record_count": scanned_record_count,
        "declared_node_count_pack_count": declared_node_count_pack_count,
        "declared_count_match_pack_count": declared_count_match_pack_count,
        "declared_count_mismatch_pack_count": declared_count_mismatch_pack_count,
        "declared_count_mismatch_sample": declared_count_mismatch_sample,
        "selected_node_type_counts": dict(sorted(selected_node_type_counts.items())),
        "scoped_record_count": scoped_record_count,
        "scoped_node_type_counts": dict(sorted(scoped_node_type_counts.items())),
        "search_field_presence_counts": dict(
            sorted(search_field_presence_counts.items())
        ),
        "match_count": len(sorted_matches),
        "term_match_counts": term_match_counts,
        "duplicate_records_removed": duplicate_records_removed,
        "match_digest": match_digest,
    }
    coverage_digest = _sha256_json(digest_basis)
    return {
        "schema_version": _COVERAGE_SCHEMA_VERSION,
        "snapshot_identity": snapshot_identity,
        "scan_spec": scan_spec,
        "coverage_digest_basis": digest_basis,
        "coverage_complete": True,
        "selected_pack_count": len(selected),
        "verified_sha256_pack_count": verified_pack_count,
        "schema_valid_pack_count": schema_pack_count,
        "jsonl_schema_valid_pack_count": schema_pack_count,
        "eof_pack_count": eof_pack_count,
        "scanned_record_count": scanned_record_count,
        "declared_node_count_pack_count": declared_node_count_pack_count,
        "declared_count_match_pack_count": declared_count_match_pack_count,
        "declared_count_mismatch_pack_count": declared_count_mismatch_pack_count,
        "declared_count_mismatch_sample": declared_count_mismatch_sample,
        "declared_count_mismatch_sample_limit": _DECLARED_COUNT_MISMATCH_SAMPLE_LIMIT,
        "selected_node_type_counts": dict(sorted(selected_node_type_counts.items())),
        "scoped_record_count": scoped_record_count,
        "scoped_node_type_counts": dict(sorted(scoped_node_type_counts.items())),
        "search_field_presence_counts": dict(
            sorted(search_field_presence_counts.items())
        ),
        "match_count": len(sorted_matches),
        "term_match_counts": term_match_counts,
        "duplicate_records_removed": duplicate_records_removed,
        "coverage_digest": coverage_digest,
        "match_digest": match_digest,
        "matched_id_sample": sorted_matches[:_MATCHED_ID_SAMPLE_LIMIT],
        "matched_id_sample_limit": _MATCHED_ID_SAMPLE_LIMIT,
        "selected_source_packs": selected_descriptors,
    }


def scan_snapshot_pack_role(snapshot: Mapping[str, Any], role: str) -> dict[str, Any]:
    """Validate every included manifest descriptor and count one requested role.

    This is a manifest-coverage operation.  It validates descriptor shape,
    uniqueness, local path existence, and actual pre/post SHA-256 for every
    included entry; it does not claim that unrelated pack JSONL was scanned.
    """

    if not isinstance(snapshot, Mapping):
        raise CoverageScanError("snapshot must be a mapping")
    requested_role = _bounded_text(role, field="role", maximum=100)
    packs, manifest_entry_count = _snapshot_packs(snapshot)
    if not packs:
        raise CoverageScanError(
            "snapshot contains no included packs for role coverage"
        )
    pre_hashes: dict[str, str] = {}
    for pack in packs:
        actual_hash = _sha256_file(pack.path)
        if actual_hash != pack.sha256:
            raise CoverageScanError(
                f"role-scan SHA-256 mismatch for {pack.pack_id}: "
                f"expected {pack.sha256}, got {actual_hash}"
            )
        pre_hashes[pack.pack_id] = actual_hash
    descriptors = [pack.descriptor() for pack in packs]
    matched = [item for item in descriptors if item["role"] == requested_role]
    post_verified_sha256_count = 0
    for pack in packs:
        post_hash = _sha256_file(pack.path)
        if post_hash != pre_hashes[pack.pack_id] or post_hash != pack.sha256:
            raise CoverageScanError(
                f"role-scan post-verification SHA-256 drift for {pack.pack_id}: "
                f"before {pre_hashes[pack.pack_id]}, after {post_hash}"
            )
        post_verified_sha256_count += 1
    snapshot_identity = _snapshot_identity(snapshot)
    digest_basis = {
        "snapshot": snapshot_identity,
        "requested_role": requested_role,
        "manifest_entry_count": manifest_entry_count,
        "included_source_packs": descriptors,
        "verified_sha256_count": len(pre_hashes),
        "post_verified_sha256_count": post_verified_sha256_count,
        "role_match_count": len(matched),
        "matched_source_packs": matched,
    }
    digest = _sha256_json(digest_basis)
    return {
        "schema_version": _ROLE_SCHEMA_VERSION,
        "snapshot_identity": snapshot_identity,
        "role_digest_basis": digest_basis,
        "requested_role": requested_role,
        "role_match_count": len(matched),
        "manifest_entry_count": manifest_entry_count,
        "certificate": {
            "coverage_complete": True,
            "included_entry_count": len(packs),
            "validated_descriptor_count": len(packs),
            "existing_path_count": len(packs),
            "verified_sha256_count": len(pre_hashes),
            "post_verified_sha256_count": post_verified_sha256_count,
        },
        "digest": digest,
        "source_pack_descriptors": descriptors,
        "matched_source_pack_descriptors": matched,
    }


def _validate_scan_spec(
    value: Mapping[str, Any], allowed_fields: Collection[str]
) -> _ScanSpec:
    if not isinstance(value, Mapping):
        raise CoverageScanError("coverage scan spec must be a mapping")
    required = {"roles", "node_types", "search_fields", "search"}
    optional = {"scope_filters", "nested_fields"}
    if set(value) - required - optional or not required.issubset(value):
        raise CoverageScanError(
            f"coverage scan spec must contain {sorted(required)!r} "
            f"with optional {sorted(optional)!r}"
        )
    allowed = _allowed_field_set(allowed_fields)
    roles = _text_array(value["roles"], field="roles", minimum=1, maximum=8)
    node_types = _text_array(
        value["node_types"], field="node_types", minimum=1, maximum=16
    )
    search_fields = _text_array(
        value["search_fields"], field="search_fields", minimum=1, maximum=16
    )
    unknown_search_fields = sorted(set(search_fields) - allowed)
    if unknown_search_fields:
        raise CoverageScanError(
            f"search_fields are outside the caller allowlist: {unknown_search_fields!r}"
        )

    search = value["search"]
    if not isinstance(search, Mapping) or set(search) != {"op", "terms"}:
        raise CoverageScanError("search must contain exactly op and terms")
    operation = search["op"]
    if operation not in {"exact_token", "contains_any"}:
        raise CoverageScanError(
            "search.op must be exact_token or contains_any; regex is not supported"
        )
    terms = _text_array(search["terms"], field="search.terms", minimum=1, maximum=16)
    normalized_terms = tuple(_normalize_text(term) for term in terms)
    if any(not term.strip() for term in normalized_terms):
        raise CoverageScanError("search terms cannot be blank")
    if len(set(normalized_terms)) != len(normalized_terms):
        raise CoverageScanError("search terms must remain unique after normalization")
    if operation == "exact_token":
        for term in normalized_terms:
            if not _is_letter_or_number(term[0]) or not _is_letter_or_number(term[-1]):
                raise CoverageScanError(
                    "exact_token terms must start and end with a Unicode letter or number"
                )

    scope_filters = _validate_scope_filters(
        value.get("scope_filters", []), allowed
    )
    nested_fields = _validate_nested_fields(
        value.get("nested_fields", {}), set(search_fields), allowed
    )
    if nested_fields and operation != "contains_any":
        raise CoverageScanError("nested_fields are supported only by contains_any")
    return _ScanSpec(
        roles=roles,
        node_types=node_types,
        search_fields=search_fields,
        scope_filters=scope_filters,
        search_op=operation,
        terms=terms,
        normalized_terms=normalized_terms,
        nested_fields=nested_fields,
    )


def _allowed_field_set(value: Collection[str]) -> set[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Collection):
        raise CoverageScanError("allowed_fields must be a collection of field names")
    result = {
        _bounded_text(item, field="allowed_fields item", maximum=100) for item in value
    }
    if not result:
        raise CoverageScanError("allowed_fields cannot be empty")
    return result


def _text_array(
    value: Any, *, field: str, minimum: int, maximum: int
) -> tuple[str, ...]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise CoverageScanError(
            f"{field} must be an array containing between {minimum} and {maximum} items"
        )
    items = tuple(
        _bounded_text(item, field=f"{field} item", maximum=100) for item in value
    )
    if len(set(items)) != len(items):
        raise CoverageScanError(f"{field} items must be unique")
    return items


def _bounded_text(value: Any, *, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise CoverageScanError(
            f"{field} must be non-empty text no longer than {maximum} characters"
        )
    return value


def _validate_scope_filters(
    value: Any, allowed_fields: set[str]
) -> tuple[_ScopeFilter, ...]:
    if not isinstance(value, list) or len(value) > _MAX_SCOPE_FILTERS:
        raise CoverageScanError("scope_filters must be an array of at most 16 filters")
    filters: list[_ScopeFilter] = []
    seen_fields: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, Mapping) or set(item) != {"field", "operator", "value"}:
            raise CoverageScanError(
                f"scope_filters[{index}] must contain field, operator, and value"
            )
        field = _bounded_text(
            item["field"], field=f"scope_filters[{index}].field", maximum=100
        )
        if field not in allowed_fields:
            raise CoverageScanError(f"scope field {field!r} is outside the caller allowlist")
        if field in seen_fields:
            raise CoverageScanError(f"duplicate scope filter field {field!r}")
        seen_fields.add(field)
        operator = item["operator"]
        if operator not in {"eq", "in"}:
            raise CoverageScanError("scope filter operator must be eq or in")
        raw_values = item["value"] if operator == "in" else [item["value"]]
        if operator == "in" and (
            not isinstance(raw_values, list)
            or not 1 <= len(raw_values) <= _MAX_SCOPE_IN_VALUES
        ):
            raise CoverageScanError("scope in value must contain between 1 and 64 scalars")
        values = tuple(_scope_token(raw, source="scope filter value") for raw in raw_values)
        if len(set(values)) != len(values):
            raise CoverageScanError("scope filter values must be unique")
        filters.append(_ScopeFilter(field=field, operator=operator, values=values))
    return tuple(filters)


def _validate_nested_fields(
    value: Any, search_fields: set[str], allowed_fields: set[str]
) -> dict[str, _NestedField]:
    if not isinstance(value, Mapping):
        raise CoverageScanError("nested_fields must be a mapping")
    result: dict[str, _NestedField] = {}
    for raw_field, config in value.items():
        field = _bounded_text(raw_field, field="nested_fields key", maximum=100)
        if field not in search_fields or field not in allowed_fields:
            raise CoverageScanError(
                f"nested field {field!r} must be an allowlisted search field"
            )
        if not isinstance(config, Mapping) or set(config) != {"max_depth", "max_leaves"}:
            raise CoverageScanError(
                f"nested_fields[{field!r}] must contain max_depth and max_leaves"
            )
        depth = config["max_depth"]
        leaves = config["max_leaves"]
        if isinstance(depth, bool) or not isinstance(depth, int) or not 1 <= depth <= _MAX_NESTED_DEPTH:
            raise CoverageScanError("nested max_depth must be an integer from 1 to 8")
        if isinstance(leaves, bool) or not isinstance(leaves, int) or not 1 <= leaves <= _MAX_NESTED_LEAVES:
            raise CoverageScanError("nested max_leaves must be an integer from 1 to 1024")
        result[field] = _NestedField(max_depth=depth, max_leaves=leaves)
    return result


def _snapshot_packs(snapshot: Mapping[str, Any]) -> tuple[list[_Pack], int]:
    raw_entries = snapshot.get("packs", snapshot.get("pack_entries"))
    if not isinstance(raw_entries, Sequence) or isinstance(raw_entries, (str, bytes)):
        raise CoverageScanError("snapshot packs must be an array")
    base = _snapshot_base(snapshot)
    packs: list[_Pack] = []
    pack_ids: set[str] = set()
    paths: set[Path] = set()
    for index, raw in enumerate(raw_entries):
        if not isinstance(raw, Mapping):
            raise CoverageScanError(f"snapshot packs[{index}] must be a mapping")
        included = raw.get("included", True)
        if not isinstance(included, bool):
            raise CoverageScanError(f"snapshot packs[{index}].included must be boolean")
        if not included:
            continue
        pack_id = _bounded_text(
            raw.get("pack_id"), field=f"snapshot packs[{index}].pack_id", maximum=200
        )
        role = _bounded_text(
            raw.get("role"), field=f"snapshot packs[{index}].role", maximum=100
        )
        sha256 = _validate_sha256(raw.get("sha256"), index=index)
        path = _resolve_pack_path(raw.get("path", raw.get("zip_path")), base, index)
        if pack_id in pack_ids:
            raise CoverageScanError(f"duplicate snapshot pack_id {pack_id!r}")
        if path in paths:
            raise CoverageScanError(f"duplicate snapshot pack path {str(path)!r}")
        pack_ids.add(pack_id)
        paths.add(path)
        packs.append(_Pack(pack_id=pack_id, role=role, path=path, sha256=sha256))
    packs.sort(key=lambda item: (item.role, item.pack_id))
    return packs, len(raw_entries)


def _snapshot_base(snapshot: Mapping[str, Any]) -> Path:
    if snapshot.get("manifest_dir") is not None:
        return Path(os.fspath(snapshot["manifest_dir"])).expanduser().resolve()
    if snapshot.get("base_path") is not None:
        return Path(os.fspath(snapshot["base_path"])).expanduser().resolve()
    if snapshot.get("manifest_path") is not None:
        return Path(os.fspath(snapshot["manifest_path"])).expanduser().resolve().parent
    return Path.cwd().resolve()


def _resolve_pack_path(value: Any, base: Path, index: int) -> Path:
    try:
        raw = os.fspath(value)
    except TypeError:
        raise CoverageScanError(f"snapshot packs[{index}].path must be path text") from None
    if not isinstance(raw, str) or not raw:
        raise CoverageScanError(f"snapshot packs[{index}].path must be path text")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = base / path
    path = path.resolve()
    if not path.is_file():
        raise CoverageScanError(f"snapshot pack path is missing or not a file: {path}")
    return path


def _validate_sha256(value: Any, *, index: int) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _HEX_DIGITS for character in value)
    ):
        raise CoverageScanError(
            f"snapshot packs[{index}].sha256 must be 64 hexadecimal characters"
        )
    return value.casefold()


def _declared_node_count(archive: zipfile.ZipFile, pack: _Pack) -> int | None:
    names = archive.namelist()
    if names.count("manifest.json") > 1:
        raise CoverageScanError(f"{pack.pack_id} contains duplicate manifest.json members")
    if "manifest.json" not in names:
        return None
    manifest = _load_json_object(
        archive.read("manifest.json"), source=f"{pack.pack_id}!manifest.json"
    )
    embedded_pack_id = manifest.get("pack_id", manifest.get("id"))
    if embedded_pack_id is not None and embedded_pack_id != pack.pack_id:
        raise CoverageScanError(
            f"embedded pack_id mismatch for {pack.pack_id}: {embedded_pack_id!r}"
        )
    entrypoints = manifest.get("entrypoints")
    if entrypoints is not None:
        if not isinstance(entrypoints, Mapping):
            raise CoverageScanError(f"{pack.pack_id} manifest entrypoints must be a mapping")
        declared_member = entrypoints.get("nodes")
        if declared_member is not None and declared_member != "graph/nodes.jsonl":
            raise CoverageScanError(
                f"{pack.pack_id} declares a non-canonical nodes entrypoint {declared_member!r}"
            )
    counts = manifest.get("counts")
    if counts is None:
        return None
    if not isinstance(counts, Mapping):
        raise CoverageScanError(f"{pack.pack_id} manifest counts must be a mapping")
    count = counts.get("nodes")
    if count is None:
        return None
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise CoverageScanError(f"{pack.pack_id} declared nodes count must be non-negative")
    return count


def _load_json_object(raw: bytes, *, source: str) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8-sig")
        value = json.loads(
            text,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_object_pairs,
        )
    except CoverageScanError as exc:
        raise CoverageScanError(f"invalid JSON at {source}: {exc}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CoverageScanError(f"invalid JSON at {source}") from exc
    if not isinstance(value, dict):
        raise CoverageScanError(f"JSON record must be an object at {source}")
    return value


def _reject_json_constant(value: str) -> None:
    raise CoverageScanError(f"non-standard JSON numeric constant {value!r}")


def _reject_duplicate_object_pairs(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CoverageScanError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _validate_node(
    node: Mapping[str, Any], pack: _Pack, line_number: int
) -> tuple[str, str]:
    source = f"{pack.pack_id}!graph/nodes.jsonl:{line_number}"
    node_id = node.get("id")
    if not isinstance(node_id, str) or not node_id:
        raise CoverageScanError(f"node id must be non-empty text at {source}")
    node_type = node.get("node_type")
    if not isinstance(node_type, str) or not node_type:
        raise CoverageScanError(f"node_type must be non-empty text at {source}")
    properties = node.get("properties")
    if not isinstance(properties, Mapping):
        raise CoverageScanError(f"node properties must be an object at {source}")
    return node_id, node_type


def _flatten_node(
    node: Mapping[str, Any], pack_id: str, line_number: int
) -> dict[str, Any]:
    row = dict(node["properties"])
    for key, value in node.items():
        if key == "properties":
            continue
        if key in row and row[key] != value:
            raise CoverageScanError(
                f"conflicting top-level/property field {key!r} at "
                f"{pack_id}!graph/nodes.jsonl:{line_number}"
            )
        row.setdefault(key, value)
    return row


def _matches_scope(
    row: Mapping[str, Any],
    filters: Sequence[_ScopeFilter],
    *,
    source: str,
) -> bool:
    for item in filters:
        actual = row.get(item.field, _MISSING)
        if actual is _MISSING or actual is None:
            raise CoverageScanError(
                f"selected node {source!r} is missing required scope field "
                f"{item.field!r}"
            )
        token = _scope_token(actual, source=f"scope field {item.field!r}")
        if token not in item.values:
            return False
    return True


def _scope_token(value: Any, *, source: str) -> tuple[str, Any]:
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, str):
        return ("text", _normalize_text(value))
    if isinstance(value, int):
        return ("number", Decimal(value))
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CoverageScanError(f"{source} must be a finite scalar")
        try:
            return ("number", Decimal(str(value)))
        except InvalidOperation:
            raise CoverageScanError(f"{source} must be a finite scalar") from None
    raise CoverageScanError(f"{source} must be scalar text, number, or boolean")


def _searchable_values(row: Mapping[str, Any], spec: _ScanSpec) -> list[str]:
    values: list[str] = []
    for field in spec.search_fields:
        value = row.get(field, _MISSING)
        if value is _MISSING or value is None:
            continue
        nested = spec.nested_fields.get(field)
        if nested is None:
            values.append(_search_scalar(value, field=field))
            continue
        leaves: list[str] = []
        _nested_scalar_leaves(
            value,
            field=field,
            depth=0,
            limits=nested,
            output=leaves,
        )
        values.extend(leaves)
    return values


def _nested_scalar_leaves(
    value: Any,
    *,
    field: str,
    depth: int,
    limits: _NestedField,
    output: list[str],
) -> None:
    if value is None:
        return
    if isinstance(value, Mapping):
        if depth >= limits.max_depth:
            raise CoverageScanError(f"nested search field {field!r} exceeds max_depth")
        if not all(isinstance(key, str) for key in value):
            raise CoverageScanError(f"nested search field {field!r} has a non-text key")
        for key in sorted(value):
            _nested_scalar_leaves(
                value[key],
                field=field,
                depth=depth + 1,
                limits=limits,
                output=output,
            )
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if depth >= limits.max_depth:
            raise CoverageScanError(f"nested search field {field!r} exceeds max_depth")
        for item in value:
            _nested_scalar_leaves(
                item,
                field=field,
                depth=depth + 1,
                limits=limits,
                output=output,
            )
        return
    output.append(_search_scalar(value, field=field))
    if len(output) > limits.max_leaves:
        raise CoverageScanError(f"nested search field {field!r} exceeds max_leaves")


def _search_scalar(value: Any, *, field: str) -> str:
    if isinstance(value, str):
        return _normalize_text(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CoverageScanError(f"search field {field!r} contains a non-finite number")
        return str(value).casefold()
    raise CoverageScanError(
        f"search field {field!r} must be scalar or explicitly declared nested"
    )


def _matched_terms(values: Sequence[str], spec: _ScanSpec) -> list[int]:
    matched: list[int] = []
    for index, term in enumerate(spec.normalized_terms):
        if spec.search_op == "contains_any":
            found = any(term in value for value in values)
        else:
            found = any(_contains_exact_token(value, term) for value in values)
        if found:
            matched.append(index)
    return matched


def _contains_exact_token(value: str, term: str) -> bool:
    start = 0
    while True:
        index = value.find(term, start)
        if index < 0:
            return False
        end = index + len(term)
        left_boundary = index == 0 or not _is_letter_or_number(value[index - 1])
        right_boundary = end == len(value) or not _is_letter_or_number(value[end])
        if left_boundary and right_boundary:
            return True
        start = index + 1


def _is_letter_or_number(value: str) -> bool:
    return bool(value) and unicodedata.category(value)[0] in {"L", "N"}


def _normalize_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _snapshot_identity(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "canonical_id",
        "snapshot_id",
        "snapshot_hash",
        "source_signature",
        "source_composite_sha256",
        "project_id",
    )
    return {key: snapshot[key] for key in keys if snapshot.get(key) is not None}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise CoverageScanError("coverage source contains a non-canonical JSON value") from exc


__all__ = [
    "CoverageScanError",
    "scan_snapshot_nodes",
    "scan_snapshot_pack_role",
]
