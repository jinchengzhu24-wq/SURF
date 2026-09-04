import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from level_validation import (
    LevelValidationError,
    build_entity_bindings,
    build_map_facts,
    build_stage_snapshot,
    analyze_user_map_claims,
    format_map_claim_correction,
    entity_binding_fingerprint,
    validate_and_solve,
    validate_rows,
)


CLOSED_ROWS = [
    "############",
    "#..........#",
    "#..........#",
    "#..........#",
    "#...p......#",
    "#...s.t....#",
    "#..........#",
    "#..........#",
    "#..........#",
    "############",
]

IRREGULAR_CLOSED_ROWS = [
    "            ",
    "   ######   ",
    "   #....#   ",
    "   #....#   ",
    "   #.pst#   ",
    "   #....#   ",
    "   ######   ",
    "            ",
    "            ",
    "            ",
]

IDENTITY_ROWS = [
    "############",
    "#..t.......#",
    "#..........#",
    "#..........#",
    "#...p......#",
    "#..s..t....#",
    "#..........#",
    "#......s...#",
    "#..........#",
    "############",
]


class OuterWallValidationTests(unittest.TestCase):
    def test_closed_rectangular_map_passes(self):
        self.assertEqual(validate_rows(CLOSED_ROWS), tuple(CLOSED_ROWS))

    def test_irregular_closed_map_with_exterior_void_passes(self):
        self.assertEqual(validate_rows(IRREGULAR_CLOSED_ROWS), tuple(IRREGULAR_CLOSED_ROWS))

    def test_breach_from_exterior_void_to_floor_is_rejected(self):
        rows = CLOSED_ROWS.copy()
        rows[0] = "####.#######"

        error = self.assert_open_outer_wall(rows)

        self.assertEqual(error.details, {"row": 1, "column": 5, "tile": "."})

    def test_water_cannot_close_an_outer_wall(self):
        rows = CLOSED_ROWS.copy()
        rows[0] = "####@#######"

        error = self.assert_open_outer_wall(rows)

        self.assertEqual(error.details, {"row": 1, "column": 5, "tile": "@"})

    def test_breach_to_player_is_rejected_after_entity_counts_pass(self):
        rows = CLOSED_ROWS.copy()
        rows[0] = "####p#######"
        rows[4] = "#..........#"

        error = self.assert_open_outer_wall(rows)

        self.assertEqual(error.details, {"row": 1, "column": 5, "tile": "p"})

    def test_solvable_map_with_outer_wall_breach_is_rejected_before_solving(self):
        rows = CLOSED_ROWS.copy()
        rows[0] = "####.#######"

        with self.assertRaises(LevelValidationError) as raised:
            validate_and_solve(rows)

        self.assertEqual(raised.exception.code, "OPEN_OUTER_WALL")

    def assert_open_outer_wall(self, rows):
        with self.assertRaises(LevelValidationError) as raised:
            validate_rows(rows)

        self.assertEqual(raised.exception.code, "OPEN_OUTER_WALL")
        self.assertIn("water cannot close", str(raised.exception))
        return raised.exception


class EntityBindingTests(unittest.TestCase):
    def test_stage_snapshot_is_one_authoritative_current_map(self):
        bindings = build_entity_bindings(IDENTITY_ROWS)
        snapshot = build_stage_snapshot(
            IDENTITY_ROWS,
            version_id="version-current",
            stage_number=3,
            entity_bindings=bindings,
        )

        self.assertEqual(snapshot["versionId"], "version-current")
        self.assertEqual(snapshot["stageNumber"], 3)
        self.assertEqual(snapshot["dimensions"], {"rows": 10, "columns": 12})
        self.assertEqual(snapshot["rows"], IDENTITY_ROWS)
        self.assertEqual(snapshot["mapFingerprint"], bindings["mapFingerprint"])
        self.assertEqual(
            snapshot["entityBindingFingerprint"], bindings["bindingFingerprint"]
        )

    def test_user_wrong_current_entity_claim_is_corrected_but_future_route_is_not(self):
        bindings = build_entity_bindings(IDENTITY_ROWS)
        snapshot = build_stage_snapshot(IDENTITY_ROWS, entity_bindings=bindings)
        current_claim = "B1" + chr(0x5728) + chr(0xFF08) + "4" + chr(0xFF0C) + "4" + chr(0xFF09)
        result = analyze_user_map_claims(current_claim, snapshot)

        self.assertEqual(result["conflicts"][0]["entity"], "B1")
        self.assertEqual(result["conflicts"][0]["expected"], {"row": 6, "column": 4})
        correction = format_map_claim_correction(result["conflicts"], "zh-CN")
        self.assertIn("B1", correction)
        self.assertIn("6", correction)
        self.assertIn("4", correction)

        future = "if B1 moves to (4,4)"
        self.assertEqual(analyze_user_map_claims(future, snapshot)["conflicts"], [])

    def test_coordinate_tile_claim_uses_current_tile_not_any_other_cell(self):
        bindings = build_entity_bindings(IDENTITY_ROWS)
        snapshot = build_stage_snapshot(IDENTITY_ROWS, entity_bindings=bindings)
        result = analyze_user_map_claims("(6,4)" + chr(0x662F) + chr(0x6C34) + chr(0x57DF), snapshot)

        self.assertEqual(len(result["conflicts"]), 1)
        self.assertEqual(result["conflicts"][0]["actualTile"], "s")

    def test_user_map_claim_parser_supports_row_column_and_reverse_forms(self):
        snapshot = build_stage_snapshot(
            IDENTITY_ROWS,
            entity_bindings=build_entity_bindings(IDENTITY_ROWS),
        )
        claims = [
            "B1 is located in row 6, column 4",
            "B1 sits on row 6, column 4",
            "B1" + chr(0x5728) + chr(0x7B2C) + "6" + chr(0x884C) + chr(0x7B2C) + "4" + chr(0x5217),
            chr(0x7B2C) + "6" + chr(0x884C) + chr(0x7B2C) + "4" + chr(0x5217) + chr(0x662F) + "B1",
        ]
        for claim in claims:
            with self.subTest(claim=claim):
                self.assertEqual(analyze_user_map_claims(claim, snapshot)["conflicts"], [])

    def test_user_map_claim_parser_skips_future_and_hypothetical_row_claims(self):
        snapshot = build_stage_snapshot(
            IDENTITY_ROWS,
            entity_bindings=build_entity_bindings(IDENTITY_ROWS),
        )
        claims = [
            "B1 will be at row 4, column 4",
            "If B1 sits on row 4, column 4, the detour changes.",
            "\u5982\u679cB1\u5728\u7b2c4\u884c\u7b2c4\u5217\uff0c\u7ed5\u884c\u4f1a\u66f4\u660e\u663e\u3002",
            "\u7b2c4\u884c\u7b2c4\u5217\u5c06\u662f\u6c34\u57df\u3002",
        ]
        for claim in claims:
            with self.subTest(claim=claim):
                self.assertEqual(analyze_user_map_claims(claim, snapshot)["conflicts"], [])

    def test_initial_bindings_and_map_facts_are_authoritative(self):
        bindings = build_entity_bindings(IDENTITY_ROWS)

        self.assertEqual(bindings["identityStatus"], "exact")
        self.assertEqual(
            [(item["label"], item["row"], item["column"])
             for item in bindings["entities"] if item["kind"] == "box"],
            [("B1", 6, 4), ("B2", 8, 8)],
        )
        self.assertEqual(
            bindings["bindingFingerprint"],
            entity_binding_fingerprint(bindings),
        )
        facts = build_map_facts(IDENTITY_ROWS, entity_bindings=bindings)
        self.assertEqual(facts["dimensions"], {"rows": 10, "columns": 12})
        self.assertEqual(facts["entityBindingFingerprint"], bindings["bindingFingerprint"])
        self.assertEqual(
            [(item["id"], item["row"], item["column"])
             for item in facts["entities"] if item["kind"] == "box"],
            [("B1", 6, 4), ("B2", 8, 8)],
        )

    def test_single_box_move_inherits_identity_without_reordering(self):
        child = list(IDENTITY_ROWS)
        child[5] = "#...s.t....#"
        parent = build_entity_bindings(IDENTITY_ROWS)
        child_bindings = build_entity_bindings(
            child,
            parent_bindings=parent,
            entity_transitions=[
                {"row": 6, "column": 4, "from": "s", "to": ".", "anchorEntity": "B1"},
                {"row": 6, "column": 5, "from": ".", "to": "s", "anchorEntity": "B1"},
            ],
        )

        moved = next(item for item in child_bindings["entities"] if item["label"] == "B1")
        unchanged = next(item for item in child_bindings["entities"] if item["label"] == "B2")
        self.assertEqual((moved["row"], moved["column"]), (6, 5))
        self.assertEqual(moved["entityId"], next(
            item["entityId"] for item in parent["entities"] if item["label"] == "B1"
        ))
        self.assertEqual(unchanged["identityConfidence"], "exact")

    def test_ambiguous_multi_box_operation_marks_box_identity_unknown(self):
        parent = build_entity_bindings(IDENTITY_ROWS)
        # A box swap has the same final grid as the parent.  The untagged
        # operation metadata is the only signal that identities crossed.
        child = list(IDENTITY_ROWS)
        child_bindings = build_entity_bindings(
            child,
            parent_bindings=parent,
            entity_transitions=[
                {"row": 6, "column": 4, "from": "s", "to": "."},
                {"row": 6, "column": 8, "from": ".", "to": "s"},
                {"row": 8, "column": 8, "from": "s", "to": "."},
                {"row": 8, "column": 4, "from": ".", "to": "s"},
            ],
        )

        boxes = [item for item in child_bindings["entities"] if item["kind"] == "box"]
        self.assertEqual(child_bindings["identityStatus"], "partial")
        self.assertTrue(all(item["identityConfidence"] == "unknown" for item in boxes))

    def test_unknown_identity_is_not_upgraded_by_a_later_unchanged_stage(self):
        parent = build_entity_bindings(IDENTITY_ROWS)
        ambiguous = build_entity_bindings(
            IDENTITY_ROWS,
            parent_bindings=parent,
            entity_transitions=[
                {"row": 6, "column": 4, "from": "s", "to": "."},
                {"row": 6, "column": 8, "from": ".", "to": "s"},
                {"row": 8, "column": 8, "from": "s", "to": "."},
                {"row": 8, "column": 4, "from": ".", "to": "s"},
            ],
        )
        later = build_entity_bindings(IDENTITY_ROWS, parent_bindings=ambiguous)

        boxes = [item for item in later["entities"] if item["kind"] == "box"]
        self.assertTrue(all(item["identityConfidence"] == "unknown" for item in boxes))

    def test_invalid_binding_is_rendered_as_untrusted_facts(self):
        bindings = build_entity_bindings(IDENTITY_ROWS)
        bindings["mapFingerprint"] = "stale"
        facts = build_map_facts(IDENTITY_ROWS, entity_bindings=bindings)

        self.assertEqual(facts["identityStatus"], "unknown")
        self.assertTrue(
            all(item["identityConfidence"] == "unknown" for item in facts["entities"])
        )

    def test_unknown_entity_label_requires_neutral_coordinate_clarification(self):
        parent = build_entity_bindings(IDENTITY_ROWS)
        ambiguous = build_entity_bindings(
            IDENTITY_ROWS,
            parent_bindings=parent,
            entity_transitions=[
                {"row": 6, "column": 4, "from": "s", "to": "."},
                {"row": 6, "column": 8, "from": ".", "to": "s"},
                {"row": 8, "column": 8, "from": "s", "to": "."},
                {"row": 8, "column": 4, "from": ".", "to": "s"},
            ],
        )
        snapshot = build_stage_snapshot(IDENTITY_ROWS, entity_bindings=ambiguous)

        result = analyze_user_map_claims("B1 is at (6,4)", snapshot)

        self.assertEqual(
            result["conflicts"][0]["reason"],
            "entity_identity_unknown",
        )
        correction = format_map_claim_correction(result["conflicts"], "zh-CN")
        self.assertIn("B1", correction)
        self.assertIn("行列位置", correction)

    def test_legacy_semantic_breach_still_produces_safe_grounding_facts(self):
        rows = CLOSED_ROWS.copy()
        rows[0] = "####@#######"

        facts = build_map_facts(rows)

        self.assertFalse(facts["semanticValidation"]["valid"])
        self.assertEqual(facts["semanticValidation"]["code"], "OPEN_OUTER_WALL")
        self.assertEqual(facts["identityStatus"], "unknown")
