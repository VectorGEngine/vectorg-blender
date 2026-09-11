"""Brake estimate geometry regression tests; no Blender required."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest


ADDON = Path(__file__).resolve().parents[1] / "addons/vectorg_car_exporter/__init__.py"


class BrakeTests(unittest.TestCase):
    def setUp(self):
        selected = []
        for node in ast.parse(ADDON.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.FunctionDef) and node.name in {
                "calculate_max_speed_brake_force_kg", "blender_position_to_game",
                "relative_to_car",
            }:
                selected.append(node)
            elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                if node.targets[0].id in {"WHEEL_KEYS", "NEWTONS_PER_KILOGRAM", "BRAKE_LOCK_MARGIN"}:
                    selected.append(node)
        self.api = {}
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(ADDON), "exec"), self.api)

    def setup_car(self, offset=(0, 0, 0), com_height=0.5):
        def obj(x, y, z):
            position = SimpleNamespace(x=x + offset[0], y=y + offset[1], z=z + offset[2])
            return SimpleNamespace(matrix_world=SimpleNamespace(translation=position))

        wheels = []
        for group, y in (("front", -1.5), ("rear", 1.5)):
            for key, x in (("l", -0.8), ("r", 0.8)):
                wheels.append(SimpleNamespace(
                    group=group, key=key, suspension_ref=obj(x, y, 0.6),
                    wheel_ref=obj(x, y, 0.3), radius=0.3,
                ))
        return SimpleNamespace(
            car_root_object=obj(0, 0, 0), center_of_mass_object=obj(0, 0, com_height),
            colliders=[SimpleNamespace(mass=800)], wheels=wheels,
            down_force_points=[SimpleNamespace(object_ref=obj(0, 0, 0), max_force=1962)],
        )

    def estimate(self, settings):
        return self.api["calculate_max_speed_brake_force_kg"](
            settings, SimpleNamespace(brake_bias=0.6),
        )

    def test_world_geometry_matches_known_axle_loads(self):
        # 800 kg plus 200 kg-equivalent downforce: 1.25 g braking.
        # Static loads 500/500; transfer 800 * 1.25 * 0.5 / 3.
        front, rear, deceleration = self.estimate(self.setup_car())
        self.assertAlmostEqual(deceleration, 1.25)
        self.assertAlmostEqual(front, (500 + 500 / 3) * 0.5 / 0.6 * 1.15)
        self.assertAlmostEqual(rear, (500 - 500 / 3) * 0.5 / 0.4 * 1.15)

    def test_world_translation_does_not_change_estimate(self):
        baseline = self.estimate(self.setup_car())
        translated = self.estimate(self.setup_car(offset=(10, -20, 30)))
        for actual, expected in zip(translated, baseline):
            self.assertAlmostEqual(actual, expected)

    def test_actual_axle_unloading_is_still_rejected(self):
        with self.assertRaisesRegex(ValueError, "Predicted braking unloads an axle"):
            self.estimate(self.setup_car(com_height=2))


if __name__ == "__main__":
    unittest.main()
