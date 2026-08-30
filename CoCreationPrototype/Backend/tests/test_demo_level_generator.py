import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from demo_level_generator import generate_demo_level
from level_validation import HEIGHT, WIDTH, validate_and_solve


class DemoLevelGeneratorTests(unittest.TestCase):
    def test_seed_reproduces_a_valid_algorithm_level(self):
        first = generate_demo_level(seed=12345)
        second = generate_demo_level(seed=12345)

        self.assertEqual(first.rows, second.rows)
        self.assertEqual(first.validation, second.validation)
        self.assertEqual(first.generation_summary, second.generation_summary)
        self.assertEqual(first.seed, 12345)
        self.assertEqual(first.validation.rows, first.rows)
        self.assertEqual(validate_and_solve(list(first.rows)).rows, first.rows)
        self.assertEqual(len(first.rows), HEIGHT)
        self.assertTrue(all(len(row) == WIDTH for row in first.rows))
        self.assertEqual(sum(row.count("p") for row in first.rows), 1)
        self.assertEqual(sum(row.count("s") for row in first.rows), 2)
        self.assertEqual(sum(row.count("t") for row in first.rows), 2)
        self.assertGreaterEqual(sum(row.count("@") for row in first.rows), 4)
        self.assertIn(first.generation_summary["mode"], {"algorithm_level", "algorithm_level_relaxed", "fallback"})
        self.assertGreaterEqual(first.generation_summary["reversePulls"], 18)

    def test_different_seeds_produce_different_maps(self):
        first = generate_demo_level(seed=1)
        second = generate_demo_level(seed=2)

        self.assertNotEqual(first.rows, second.rows)

    def test_water_areas_are_disjoint_rectangles_in_algorithm_range(self):
        for seed in (1, 2, 3, 42, 999):
            level = generate_demo_level(seed=seed)
            components = _water_components(level.rows)
            self.assertGreaterEqual(len(components), 1)
            self.assertLessEqual(len(components), 2)
            for component in components:
                columns = {x for x, _ in component}
                rows = {y for _, y in component}
                self.assertIn(len(columns), range(2, 5))
                self.assertIn(len(rows), range(2, 5))
                self.assertEqual(len(component), len(columns) * len(rows))
                self.assertTrue(
                    all((x, y) in component for x in columns for y in rows)
                )

    def test_fallback_remains_two_box_and_solvable(self):
        level = generate_demo_level(seed=7, max_attempts=1)
        self.assertEqual(sum(row.count("s") for row in level.rows), 2)
        self.assertEqual(sum(row.count("t") for row in level.rows), 2)
        self.assertEqual(validate_and_solve(list(level.rows)).rows, level.rows)


def _water_components(rows):
    remaining = {
        (x, y)
        for y, row in enumerate(rows)
        for x, tile in enumerate(row)
        if tile == "@"
    }
    components = []
    while remaining:
        component = {remaining.pop()}
        pending = list(component)
        while pending:
            x, y = pending.pop()
            for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
                neighbor = (x + dx, y + dy)
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    component.add(neighbor)
                    pending.append(neighbor)
        components.append(component)
    return components


if __name__ == "__main__":
    unittest.main()
