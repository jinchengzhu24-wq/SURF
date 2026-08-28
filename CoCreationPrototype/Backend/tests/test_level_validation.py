import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from level_validation import LevelValidationError, validate_and_solve, validate_rows


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
