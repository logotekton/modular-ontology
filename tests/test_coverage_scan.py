from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from modular_ontology.coverage_scan import (
    CoverageScanError,
    scan_snapshot_nodes,
    scan_snapshot_pack_role,
)


_AUTO_COUNT = object()


def _node(node_id: str, node_type: str = "Thing", **properties: object) -> dict:
    return {
        "id": node_id,
        "node_type": node_type,
        "properties": properties,
    }


def _canonical_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class CoverageScanSyntheticTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self._pack_sequence = 0

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _pack(
        self,
        records: list[object],
        *,
        role: str = "facts",
        pack_id: str | None = None,
        declared_count: object = _AUTO_COUNT,
        include_nodes_member: bool = True,
        embedded_pack_id: str | None = None,
    ) -> dict[str, object]:
        self._pack_sequence += 1
        actual_pack_id = pack_id or f"pack-{self._pack_sequence}"
        path = self.root / f"{actual_pack_id}-{self._pack_sequence}.zip"
        manifest = {
            "pack_id": embedded_pack_id or actual_pack_id,
            "entrypoints": {"nodes": "graph/nodes.jsonl"},
        }
        if declared_count is _AUTO_COUNT:
            manifest["counts"] = {"nodes": len(records)}
        elif declared_count is not None:
            manifest["counts"] = {"nodes": declared_count}
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, sort_keys=True),
            )
            if include_nodes_member:
                payload = "\n".join(
                    json.dumps(record, ensure_ascii=False, sort_keys=True)
                    for record in records
                )
                archive.writestr("graph/nodes.jsonl", payload)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return {
            "pack_id": actual_pack_id,
            "role": role,
            "path": path.name,
            "sha256": digest,
            "included": True,
        }

    def _snapshot(self, *packs: dict[str, object]) -> dict[str, object]:
        return {
            "canonical_id": "SYNTHETIC-CANONICAL-R1",
            "manifest_dir": str(self.root),
            "packs": list(packs),
        }

    def _raw_pack(self, raw_jsonl: str) -> dict[str, object]:
        self._pack_sequence += 1
        pack_id = f"raw-pack-{self._pack_sequence}"
        path = self.root / f"{pack_id}.zip"
        manifest = {
            "pack_id": pack_id,
            "entrypoints": {"nodes": "graph/nodes.jsonl"},
            "counts": {"nodes": 1},
        }
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            archive.writestr("graph/nodes.jsonl", raw_jsonl)
        return {
            "pack_id": pack_id,
            "role": "facts",
            "path": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "included": True,
        }

    @staticmethod
    def _spec(
        *,
        roles: list[str] | None = None,
        node_types: list[str] | None = None,
        fields: list[str] | None = None,
        terms: list[str] | None = None,
        op: str = "contains_any",
        scope_filters: list[dict[str, object]] | None = None,
        nested_fields: dict[str, dict[str, int]] | None = None,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "roles": roles or ["facts"],
            "node_types": node_types or ["Thing"],
            "search_fields": fields or ["text"],
            "search": {"op": op, "terms": terms or ["needle"]},
        }
        if scope_filters is not None:
            result["scope_filters"] = scope_filters
        if nested_fields is not None:
            result["nested_fields"] = nested_fields
        return result

    def test_exact_token_uses_nfkc_casefold_and_unicode_boundaries(self) -> None:
        records = [
            _node("n1", text="Ｄ９-wall"),
            _node("n2", text="AD9"),
            _node("n3", text="D90"),
            _node("n4", text="x d9!"),
            _node("n5", text="D9가"),
        ]
        result = scan_snapshot_nodes(
            self._snapshot(self._pack(records)),
            self._spec(op="exact_token", terms=["d9"]),
            allowed_fields={"text"},
        )

        self.assertTrue(result["coverage_complete"])
        self.assertEqual(result["schema_version"], "mo-coverage-scan/1.0")
        self.assertEqual(result["snapshot_identity"]["canonical_id"], "SYNTHETIC-CANONICAL-R1")
        self.assertEqual(result["scan_spec"]["search"]["op"], "exact_token")
        self.assertEqual(
            _canonical_digest(result["coverage_digest_basis"]),
            result["coverage_digest"],
        )
        self.assertEqual(result["scanned_record_count"], 5)
        self.assertEqual(result["selected_node_type_counts"], {"Thing": 5})
        self.assertEqual(result["search_field_presence_counts"], {"text": 5})
        self.assertEqual(result["match_count"], 2)
        self.assertEqual(result["term_match_counts"], {"d9": 2})
        self.assertEqual(result["matched_id_sample"], ["n1", "n4"])
        self.assertNotIn("matched_rows", result)

    def test_contains_any_requires_explicit_bounded_nested_fields(self) -> None:
        records = [
            _node(
                "lift",
                parameters_sample=[
                    {"name": "정격속도", "value": "90 m/min"},
                    {"name": "capacity", "value": 15},
                ],
            )
        ]
        snapshot = self._snapshot(self._pack(records))
        base = self._spec(fields=["parameters_sample"], terms=["정격속도"])

        with self.assertRaisesRegex(CoverageScanError, "explicitly declared nested"):
            scan_snapshot_nodes(
                snapshot, base, allowed_fields={"parameters_sample"}
            )

        result = scan_snapshot_nodes(
            snapshot,
            self._spec(
                fields=["parameters_sample"],
                terms=["정격속도"],
                nested_fields={
                    "parameters_sample": {"max_depth": 2, "max_leaves": 4}
                },
            ),
            allowed_fields={"parameters_sample"},
        )
        self.assertEqual(result["match_count"], 1)

        with self.assertRaisesRegex(CoverageScanError, "exceeds max_depth"):
            scan_snapshot_nodes(
                snapshot,
                self._spec(
                    fields=["parameters_sample"],
                    terms=["정격속도"],
                    nested_fields={
                        "parameters_sample": {"max_depth": 1, "max_leaves": 4}
                    },
                ),
                allowed_fields={"parameters_sample"},
            )
        with self.assertRaisesRegex(CoverageScanError, "exceeds max_leaves"):
            scan_snapshot_nodes(
                snapshot,
                self._spec(
                    fields=["parameters_sample"],
                    terms=["정격속도"],
                    nested_fields={
                        "parameters_sample": {"max_depth": 2, "max_leaves": 1}
                    },
                ),
                allowed_fields={"parameters_sample"},
            )

    def test_scope_eq_and_in_are_typed_and_normalized(self) -> None:
        records = [
            _node("n1", text="needle", category="ＷＡＬＬ", phase=1),
            _node("n2", text="needle", category="wall", phase=2.0),
            _node("n3", text="needle", category="floor", phase=1),
        ]
        result = scan_snapshot_nodes(
            self._snapshot(self._pack(records)),
            self._spec(
                scope_filters=[
                    {"field": "category", "operator": "eq", "value": "wall"},
                    {"field": "phase", "operator": "in", "value": [1, 2]},
                ]
            ),
            allowed_fields={"text", "category", "phase"},
        )
        self.assertEqual(result["scoped_record_count"], 2)
        self.assertEqual(result["match_count"], 2)

    def test_missing_scope_field_fails_closed(self) -> None:
        snapshot = self._snapshot(self._pack([_node("n1", text="needle")]))
        with self.assertRaisesRegex(CoverageScanError, "missing required scope field"):
            scan_snapshot_nodes(
                snapshot,
                self._spec(
                    scope_filters=[
                        {"field": "category", "operator": "eq", "value": "wall"}
                    ]
                ),
                allowed_fields={"text", "category"},
            )

    def test_node_and_search_field_presence_fail_closed(self) -> None:
        heterogeneous = self._snapshot(
            self._pack(
                [
                    _node("a", "Alpha", field_a="needle"),
                    _node("b", "Beta", field_b="needle"),
                ]
            )
        )
        result = scan_snapshot_nodes(
            heterogeneous,
            self._spec(
                node_types=["Alpha", "Beta"], fields=["field_a", "field_b"]
            ),
            allowed_fields={"field_a", "field_b"},
        )
        self.assertEqual(
            result["search_field_presence_counts"], {"field_a": 1, "field_b": 1}
        )

        with self.assertRaisesRegex(CoverageScanError, "none of the requested"):
            scan_snapshot_nodes(
                self._snapshot(self._pack([_node("empty", "Alpha", other="x")])),
                self._spec(node_types=["Alpha"], fields=["field_a"]),
                allowed_fields={"field_a"},
            )

        with self.assertRaisesRegex(CoverageScanError, "not observed in the scoped"):
            scan_snapshot_nodes(
                self._snapshot(
                    self._pack([_node("only-a", "Alpha", field_a="needle")])
                ),
                self._spec(
                    node_types=["Alpha"], fields=["field_a", "field_b"]
                ),
                allowed_fields={"field_a", "field_b"},
            )

        with self.assertRaisesRegex(CoverageScanError, "node_types were not observed"):
            scan_snapshot_nodes(
                self._snapshot(self._pack([_node("a", "Alpha", field_a="needle")])),
                self._spec(
                    node_types=["Alpha", "Beta"], fields=["field_a"]
                ),
                allowed_fields={"field_a"},
            )

    def test_identical_duplicates_are_deduped_and_conflicts_fail(self) -> None:
        duplicate = _node("same", text="needle")
        result = scan_snapshot_nodes(
            self._snapshot(self._pack([duplicate, duplicate])),
            self._spec(),
            allowed_fields={"text"},
        )
        self.assertEqual(result["scanned_record_count"], 2)
        self.assertEqual(result["scoped_record_count"], 1)
        self.assertEqual(result["duplicate_records_removed"], 1)
        self.assertEqual(result["match_count"], 1)

        with self.assertRaisesRegex(CoverageScanError, "conflicting duplicate"):
            scan_snapshot_nodes(
                self._snapshot(
                    self._pack(
                        [_node("same", text="needle"), _node("same", text="changed")]
                    )
                ),
                self._spec(),
                allowed_fields={"text"},
            )

    def test_cross_pack_auxiliary_redeclarations_do_not_mask_selected_conflicts(self) -> None:
        auxiliary_redeclaration = self._snapshot(
            self._pack(
                [
                    _node("shared-domain", "Domain", text="pack one"),
                    _node("selected-one", text="needle"),
                ]
            ),
            self._pack(
                [
                    _node("shared-domain", "Domain", text="pack two"),
                    _node("selected-two", text="needle"),
                ]
            ),
        )
        result = scan_snapshot_nodes(
            auxiliary_redeclaration,
            self._spec(),
            allowed_fields={"text"},
        )
        self.assertEqual(result["scanned_record_count"], 4)
        self.assertEqual(result["scoped_record_count"], 2)

        selected_conflict = self._snapshot(
            self._pack([_node("shared-selected", text="needle")]),
            self._pack([_node("shared-selected", text="changed")]),
        )
        with self.assertRaisesRegex(
            CoverageScanError, "conflicting duplicate selected node"
        ):
            scan_snapshot_nodes(
                selected_conflict, self._spec(), allowed_fields={"text"}
            )

    def test_malformed_records_members_and_manifest_identity_fail(self) -> None:
        cases = [
            (
                "record must be an object",
                self._pack([["not", "object"]]),
            ),
            (
                "node_type must be non-empty text",
                self._pack([{"id": "bad", "node_type": 7, "properties": {}}]),
            ),
            (
                "exactly one graph/nodes.jsonl",
                self._pack([], include_nodes_member=False),
            ),
            (
                "embedded pack_id mismatch",
                self._pack(
                    [_node("n", text="needle")], embedded_pack_id="different-pack"
                ),
            ),
        ]
        for expected, pack_entry in cases:
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(CoverageScanError, expected):
                    scan_snapshot_nodes(
                        self._snapshot(pack_entry),
                        self._spec(),
                        allowed_fields={"text"},
                    )

    def test_duplicate_json_object_keys_fail_at_any_depth(self) -> None:
        payloads = [
            '{"id":"one","id":"two","node_type":"Thing","properties":{"text":"needle"}}',
            '{"id":"one","node_type":"Thing","properties":{"text":"needle","text":"changed"}}',
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(CoverageScanError, "duplicate JSON object key"):
                    scan_snapshot_nodes(
                        self._snapshot(self._raw_pack(payload)),
                        self._spec(),
                        allowed_fields={"text"},
                    )

    def test_declared_count_mismatch_is_a_digest_bound_diagnostic(self) -> None:
        result = scan_snapshot_nodes(
            self._snapshot(
                self._pack([_node("n", text="needle")], declared_count=2)
            ),
            self._spec(),
            allowed_fields={"text"},
        )
        self.assertTrue(result["coverage_complete"])
        self.assertEqual(result["jsonl_schema_valid_pack_count"], 1)
        self.assertEqual(result["eof_pack_count"], 1)
        self.assertEqual(result["declared_node_count_pack_count"], 1)
        self.assertEqual(result["declared_count_match_pack_count"], 0)
        self.assertEqual(result["declared_count_mismatch_pack_count"], 1)
        self.assertEqual(
            result["declared_count_mismatch_sample"],
            [{"pack_id": "pack-1", "declared": 2, "scanned": 1}],
        )

    def test_hash_mismatch_and_post_scan_drift_fail(self) -> None:
        entry = self._pack([_node("n", text="needle")])
        bad_entry = dict(entry)
        bad_entry["sha256"] = "0" * 64
        with self.assertRaisesRegex(CoverageScanError, "pre-scan SHA-256 mismatch"):
            scan_snapshot_nodes(
                self._snapshot(bad_entry), self._spec(), allowed_fields={"text"}
            )

        expected_hash = str(entry["sha256"])
        with patch(
            "modular_ontology.coverage_scan._sha256_file",
            side_effect=[expected_hash, "1" * 64],
        ):
            with self.assertRaisesRegex(CoverageScanError, "post-scan SHA-256 drift"):
                scan_snapshot_nodes(
                    self._snapshot(entry), self._spec(), allowed_fields={"text"}
                )

    def test_runtime_spec_is_bounded_allowlisted_and_non_regex(self) -> None:
        snapshot = self._snapshot(self._pack([_node("n", text="needle")]))
        invalid_cases = [
            (
                self._spec(fields=["secret"]),
                {"text"},
                "outside the caller allowlist",
            ),
            (self._spec(op="regex"), {"text"}, "regex is not supported"),
            (
                self._spec(roles=[f"role-{index}" for index in range(9)]),
                {"text"},
                "between 1 and 8",
            ),
            (
                self._spec(terms=[f"term-{index}" for index in range(17)]),
                {"text"},
                "between 1 and 16",
            ),
            (
                self._spec(terms=["x" * 101]),
                {"text"},
                "no longer than 100",
            ),
        ]
        for spec, allowed, expected in invalid_cases:
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(CoverageScanError, expected):
                    scan_snapshot_nodes(snapshot, spec, allowed_fields=allowed)

    def test_match_sample_is_bounded_and_raw_content_is_not_returned(self) -> None:
        records = [
            _node(f"node-{index:02d}", text=f"secret payload needle {index}")
            for index in range(25)
        ]
        result = scan_snapshot_nodes(
            self._snapshot(self._pack(records)),
            self._spec(),
            allowed_fields={"text"},
        )
        self.assertEqual(result["match_count"], 25)
        self.assertEqual(result["matched_id_sample_limit"], 20)
        self.assertEqual(len(result["matched_id_sample"]), 20)
        self.assertNotIn("secret payload", json.dumps(result, ensure_ascii=False))
        self.assertRegex(result["coverage_digest"], r"^[0-9a-f]{64}$")
        self.assertRegex(result["match_digest"], r"^[0-9a-f]{64}$")

    def test_pack_role_scan_validates_all_descriptors_and_counts_zero(self) -> None:
        first = self._pack([_node("a", text="needle")], role="alpha")
        second = self._pack([_node("b", text="needle")], role="beta")
        snapshot = self._snapshot(first, second)
        result = scan_snapshot_pack_role(snapshot, "missing-role")
        self.assertEqual(result["schema_version"], "mo-role-coverage/1.0")
        self.assertEqual(result["snapshot_identity"]["canonical_id"], "SYNTHETIC-CANONICAL-R1")
        self.assertEqual(_canonical_digest(result["role_digest_basis"]), result["digest"])
        self.assertEqual(result["role_match_count"], 0)
        self.assertEqual(result["manifest_entry_count"], 2)
        self.assertEqual(result["certificate"]["validated_descriptor_count"], 2)
        self.assertEqual(len(result["source_pack_descriptors"]), 2)

        duplicate = dict(second)
        duplicate["pack_id"] = first["pack_id"]
        with self.assertRaisesRegex(CoverageScanError, "duplicate snapshot pack_id"):
            scan_snapshot_pack_role(self._snapshot(first, duplicate), "alpha")

    def test_pack_role_scan_rejects_an_empty_included_corpus(self) -> None:
        with self.assertRaisesRegex(
            CoverageScanError,
            "no included packs for role coverage",
        ):
            scan_snapshot_pack_role(self._snapshot(), "missing-role")


class CoverageScanYeojuR1Tests(unittest.TestCase):
    DRAWING_ROLES = [
        "drawing_entity",
        "drawing_representation_primary",
        "drawing_representation_topology",
        "drawing_representation_index",
        "drawing_representation_review",
        "drawing_representation_qa",
        "drawing_reference",
    ]
    MANIFEST = (
        Path(__file__).resolve().parents[1]
        / "snapshots"
        / "YEOJU-CANONICAL-20260710-092149-R1.json"
    )

    @classmethod
    def setUpClass(cls) -> None:
        if not cls.MANIFEST.is_file():
            raise unittest.SkipTest("Yeoju R1 canonical manifest is not available")
        cls.snapshot = json.loads(cls.MANIFEST.read_text(encoding="utf-8"))
        cls.snapshot["manifest_path"] = str(cls.MANIFEST)

    def _scan(
        self,
        *,
        role: str | list[str],
        node_types: list[str],
        fields: list[str],
        terms: list[str],
        op: str = "contains_any",
        scope_filters: list[dict[str, object]] | None = None,
        nested_fields: dict[str, dict[str, int]] | None = None,
    ) -> dict[str, object]:
        spec: dict[str, object] = {
            "roles": [role] if isinstance(role, str) else role,
            "node_types": node_types,
            "search_fields": fields,
            "search": {"op": op, "terms": terms},
        }
        if scope_filters is not None:
            spec["scope_filters"] = scope_filters
        if nested_fields is not None:
            spec["nested_fields"] = nested_fields
        return scan_snapshot_nodes(
            self.snapshot,
            spec,
            allowed_fields=set(fields)
            | {str(item["field"]) for item in scope_filters or []},
        )

    def test_r1_manifest_has_173_valid_descriptors_and_missing_roles(self) -> None:
        structural = scan_snapshot_pack_role(
            self.snapshot, "project_structural_calculation"
        )
        contract = scan_snapshot_pack_role(self.snapshot, "contract_document")
        for result in (structural, contract):
            self.assertEqual(result["manifest_entry_count"], 173)
            self.assertEqual(result["certificate"]["included_entry_count"], 173)
            self.assertEqual(result["certificate"]["validated_descriptor_count"], 173)
            self.assertEqual(result["role_match_count"], 0)

    def test_r1_b09_absence_scans_reach_eof(self) -> None:
        boq = self._scan(
            role="boq",
            node_types=["BOQItem", "AggregatedBOQItem"],
            fields=["item_name", "specification"],
            terms=["D9"],
            op="exact_token",
            scope_filters=[
                {"field": "work_category", "operator": "eq", "value": "벽체"}
            ],
        )
        model = self._scan(
            role="model_inventory",
            node_types=["BIMElement"],
            fields=["type_name"],
            terms=["D9"],
            op="exact_token",
            scope_filters=[
                {"field": "category", "operator": "eq", "value": "벽"}
            ],
        )
        self.assertEqual(
            (
                boq["selected_pack_count"],
                boq["scanned_record_count"],
                boq["scoped_record_count"],
                boq["match_count"],
            ),
            (1, 5853, 74, 0),
        )
        self.assertEqual(boq["match_digest"], _canonical_digest([]))
        self.assertEqual(
            boq["scoped_node_type_counts"],
            {"AggregatedBOQItem": 37, "BOQItem": 37},
        )
        self.assertEqual(
            (
                model["selected_pack_count"],
                model["scanned_record_count"],
                model["scoped_record_count"],
                model["match_count"],
            ),
            (6, 16861, 2327, 0),
        )

    def test_r1_e01_to_e03_absence_profiles(self) -> None:
        floor_terms = ["지하 1층", "지하1층", "B1", "주차장"]
        e01_boq = self._scan(
            role="boq",
            node_types=[
                "BOQItem",
                "EstimateItem",
                "SiteWorkItem",
                "AggregatedBOQItem",
                "QuantityTakeoffEvidence",
            ],
            fields=["module_type", "item_name", "specification", "note"],
            terms=floor_terms,
        )
        e01_model = self._scan(
            role="model_inventory",
            node_types=["BIMElement"],
            fields=["level", "category", "type_name"],
            terms=floor_terms,
        )
        e01_drawing = self._scan(
            role=self.DRAWING_ROLES,
            node_types=["DrawingSheet"],
            fields=["sheet_number", "sheet_name"],
            terms=floor_terms,
        )
        self.assertEqual(
            (e01_boq["scoped_record_count"], e01_boq["match_count"]), (4280, 0)
        )
        self.assertEqual(
            (e01_model["scoped_record_count"], e01_model["match_count"]),
            (16861, 0),
        )
        self.assertEqual(
            (e01_drawing["scoped_record_count"], e01_drawing["match_count"]),
            (84, 0),
        )
        self.assertEqual(e01_drawing["selected_pack_count"], 33)

        lift_terms = ["승강기", "엘리베이터", "정격속도", "인승"]
        e02_model = self._scan(
            role="model_inventory",
            node_types=["BIMElement"],
            fields=["category", "family_name", "type_name", "parameters_sample"],
            terms=lift_terms,
            nested_fields={
                "parameters_sample": {"max_depth": 4, "max_leaves": 1024}
            },
        )
        e02_drawing = self._scan(
            role=self.DRAWING_ROLES,
            node_types=["DrawingSchedule", "ScheduleCell"],
            fields=["schedule_name", "text"],
            terms=lift_terms,
        )
        self.assertEqual(
            (e02_model["scoped_record_count"], e02_model["match_count"]),
            (16861, 0),
        )
        self.assertEqual(
            (e02_drawing["scoped_record_count"], e02_drawing["match_count"]),
            (576, 0),
        )
        self.assertEqual(e02_drawing["selected_pack_count"], 33)
        self.assertEqual(
            e02_drawing["scoped_node_type_counts"],
            {"DrawingSchedule": 10, "ScheduleCell": 566},
        )

        e03 = self._scan(
            role="boq",
            node_types=[
                "WorkCategory",
                "BOQItem",
                "EstimateItem",
                "AggregatedBOQItem",
            ],
            fields=["name", "work_category", "item_name", "specification", "note"],
            terms=["소방", "스프링클러"],
        )
        self.assertEqual((e03["scoped_record_count"], e03["match_count"]), (2300, 0))
        self.assertEqual(
            e03["scoped_node_type_counts"],
            {
                "AggregatedBOQItem": 218,
                "BOQItem": 219,
                "EstimateItem": 1844,
                "WorkCategory": 19,
            },
        )

    def test_r1_e04_and_e05_split_profiles(self) -> None:
        drift_terms = ["최대 층간변위비", "층간변위비", "구조계산서"]
        e04_model = self._scan(
            role="model_inventory",
            node_types=["BIMElement"],
            fields=["label", "type_name"],
            terms=drift_terms,
        )
        e04_quantity = self._scan(
            role="model_quantity",
            node_types=["BIMQuantityFact"],
            fields=["label", "type_name"],
            terms=drift_terms,
        )
        e04_spec = self._scan(
            role="spec",
            node_types=["Requirement"],
            fields=["label", "requirement_text"],
            terms=drift_terms,
        )
        self.assertEqual(
            (e04_model["scoped_record_count"], e04_model["match_count"]),
            (16861, 0),
        )
        self.assertEqual(
            (e04_quantity["scoped_record_count"], e04_quantity["match_count"]),
            (50098, 0),
        )
        self.assertEqual(
            (e04_spec["scoped_record_count"], e04_spec["match_count"]),
            (10887, 33),
        )
        self.assertEqual(
            e04_spec["term_match_counts"],
            {"최대 층간변위비": 1, "층간변위비": 2, "구조계산서": 31},
        )
        self.assertEqual(e04_spec["selected_pack_count"], 105)
        self.assertEqual(e04_spec["verified_sha256_pack_count"], 105)
        self.assertEqual(e04_spec["jsonl_schema_valid_pack_count"], 105)
        self.assertEqual(e04_spec["eof_pack_count"], 105)
        self.assertEqual(e04_spec["declared_count_mismatch_pack_count"], 1)
        self.assertEqual(
            e04_spec["declared_count_mismatch_sample"],
            [
                {
                    "pack_id": "spec_architecture_41_kcs_41_30_pack",
                    "declared": 342,
                    "scanned": 349,
                }
            ],
        )

        contract_terms = ["지체상금율", "하자보수보증금율", "하자보수보증금"]
        e05_boq = self._scan(
            role="boq",
            node_types=[
                "BIMReference",
                "BOQItem",
                "EstimateItem",
                "SiteWorkItem",
                "AggregatedBOQItem",
                "QuantityTakeoffEvidence",
                "WorkCategory",
                "EstimateSheet",
                "EstimateWorkbook",
                "Formula",
                "ModuleType",
                "Project",
                "Unit",
            ],
            fields=["label", "name", "item_name", "specification"],
            terms=contract_terms,
        )
        e05_law = self._scan(
            role="law",
            node_types=["Entity"],
            fields=["label", "name"],
            terms=contract_terms,
        )
        self.assertEqual(
            (e05_boq["scoped_record_count"], e05_boq["match_count"]), (5853, 0)
        )
        self.assertEqual(
            (e05_law["scoped_record_count"], e05_law["match_count"]), (84, 0)
        )


if __name__ == "__main__":
    unittest.main()
