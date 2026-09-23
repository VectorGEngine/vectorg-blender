"""Suspension arm and travel export tests; no Blender required."""
import ast
import math
from pathlib import Path
from types import SimpleNamespace
import unittest


ADDON = Path(__file__).resolve().parents[1] / "addons/vectorg_car_exporter/__init__.py"
FUNCTIONS = {"object_config_name", "wheel_config", "wheel_preset_config", "suspension_arm_motion_ratio"}
CONSTANTS = {"BLENDER_AXIS_TO_GAME", "MIN_SUSPENSION_ARM_MOTION_RATIO"}


class SuspensionExportTests(unittest.TestCase):
    def setUp(self):
        selected = []
        for node in ast.parse(ADDON.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS:
                selected.append(node)
            elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                if node.targets[0].id in CONSTANTS:
                    selected.append(node)
        self.api = {"math": math}
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(ADDON), "exec"), self.api)

    def wheel(self, pivot=None, tilt=False):
        obj = lambda name: SimpleNamespace(name=name)
        return SimpleNamespace(
            steering=False, suspension_ref=obj("Mount"), hub_ref=obj("Joint"), wheel_ref=obj("Spin"),
            up_local_axis="z", spin_local_axis="x", radius=0.3, width=0.2, section_height=0.1,
            pivot_ref=obj(pivot) if pivot else None, pivot_tilt=tilt,
        )

    def test_pivot_is_exported_only_when_assigned(self):
        self.assertNotIn("pivot", self.api["wheel_config"](self.wheel()))
        self.assertEqual(self.api["wheel_config"](self.wheel("Pivot", True))["pivot"], {"obj": "Pivot", "tilt": True})
        self.assertEqual(self.api["wheel_config"](self.wheel("Pivot"))["pivot"], {"obj": "Pivot", "tilt": False})

    def test_preset_exports_bump_and_droop_travel(self):
        preset = SimpleNamespace(
            tire_type="medium", pressure=2.0, camber=-3.0, caster=6.0, toe=0.0, suspension_offset=-0.02,
            bump_travel=0.08, droop_travel=0.12, suspension_stiffness=80.0, damping_relaxation=2.6,
            damping_compression=2.0, max_brake_force=1000.0, grip_factor=1.0,
        )
        config = self.api["wheel_preset_config"](preset)
        self.assertEqual((config["bumpTravel"], config["droopTravel"]), (0.08, 0.12))

    def test_arm_motion_ratio_matches_the_spring_triangle(self):
        ratio = self.api["suspension_arm_motion_ratio"]
        mount_distance, arm_length = math.hypot(0.4, 0.5), 0.5
        for length in (0.3, 0.4, 0.5):
            cosine = (mount_distance ** 2 + arm_length ** 2 - length ** 2) / (2 * mount_distance * arm_length)
            angle = math.acos(cosine)
            step = 1e-7
            ahead = math.sqrt(mount_distance ** 2 + arm_length ** 2
                              - 2 * mount_distance * arm_length * math.cos(angle + step / arm_length))
            self.assertAlmostEqual(ratio(mount_distance, arm_length, length), (ahead - length) / step, places=5)
        # Outside the arm's reach, and on the spring itself, there is no ratio.
        self.assertIsNone(ratio(mount_distance, arm_length, 1.2))
        self.assertIsNone(ratio(mount_distance, arm_length, 0.1))
        self.assertIsNone(ratio(mount_distance, arm_length, 0.0))
        self.assertLess(ratio(mount_distance, arm_length, 1.138), self.api["MIN_SUSPENSION_ARM_MOTION_RATIO"])


if __name__ == "__main__":
    unittest.main()
