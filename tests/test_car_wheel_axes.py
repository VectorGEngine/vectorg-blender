"""Wheel-axis manifest round trips without requiring Blender."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest


ADDON = Path(__file__).resolve().parents[1] / "addons/vectorg_car_exporter/__init__.py"


class WheelAxisTests(unittest.TestCase):
    def setUp(self):
        selected = []
        functions = {"wheel_config", "object_config_name", "add_wheel_from_config"}
        for node in ast.parse(ADDON.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.FunctionDef) and node.name in functions:
                selected.append(node)
            elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                if node.targets[0].id in {"BLENDER_AXIS_TO_GAME", "GAME_AXIS_TO_BLENDER"}:
                    selected.append(node)
        self.api = {"set_object_pointer": lambda obj, prop, name: setattr(obj, prop, SimpleNamespace(name=name))}
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(ADDON), "exec"), self.api)

    def wheel(self):
        return SimpleNamespace(
            steering=True, suspension_ref=SimpleNamespace(name="mount"), hub_ref=SimpleNamespace(name="joint"),
            wheel_ref=SimpleNamespace(name="spin"), up_local_axis="z", spin_local_axis="x",
            radius=.3, suspension_stiffness=80, damping_relaxation=2.6, damping_compression=2,
            max_brake_force=1000, pressure=2, camber=0, toe=0, grip_factor=1,
        )

    def test_existing_up_axis_round_trip_keeps_joint_mapping_object_only(self):
        for axis in ("x", "-x", "y", "-y", "z", "-z"):
            wheel = self.wheel()
            wheel.up_local_axis = axis
            exported = self.api["wheel_config"](wheel)
            restored = self.wheel()
            settings = SimpleNamespace(wheels=SimpleNamespace(add=lambda: restored))
            self.api["add_wheel_from_config"](settings, "front", "l", exported)
            self.assertEqual(restored.up_local_axis, axis)
            self.assertEqual(exported["joint"], {"obj": "joint"})
            self.assertEqual(self.api["wheel_config"](restored), exported)

    def test_f2021_axis_import_uses_existing_spin_field(self):
        exported = self.api["wheel_config"](self.wheel())
        exported["spin"]["upLocalAxis"] = [0, 0, -1]
        restored = self.wheel()
        settings = SimpleNamespace(wheels=SimpleNamespace(add=lambda: restored))
        self.api["add_wheel_from_config"](settings, "front", "l", exported)
        self.assertEqual(restored.up_local_axis, "-y")
        self.assertEqual(self.api["wheel_config"](restored), exported)


if __name__ == "__main__":
    unittest.main()
