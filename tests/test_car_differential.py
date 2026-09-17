"""Differential preset contract tests; no Blender runtime required."""
import ast
import math
from pathlib import Path
from types import SimpleNamespace
import unittest


ADDON = Path(__file__).resolve().parents[1] / "addons/vectorg_car_exporter/__init__.py"


class DifferentialTests(unittest.TestCase):
    def setUp(self):
        self.tree = ast.parse(ADDON.read_text(encoding="utf-8"))
        nodes = [node for node in self.tree.body if (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "DIFFERENTIAL_FIELDS" for target in node.targets)
        ) or (isinstance(node, ast.FunctionDef) and node.name in (
            "preset_differential_config", "build_differential_config", "draw_differential"
        ))]
        self.namespace = {"math": math}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ADDON), "exec"), self.namespace)
        self.fields = self.namespace["DIFFERENTIAL_FIELDS"]
        self.normalize = self.namespace["preset_differential_config"]

    def test_manifest_requires_explicit_valid_settings(self):
        with self.assertRaises(ValueError):
            self.normalize({})
        defaults = {key: default for key, _prop, default in self.fields}
        self.assertEqual(defaults, {
            "frontAccelLock": 0, "frontDecelLock": 0,
            "rearAccelLock": 0, "rearDecelLock": 0, "centerBalance": 0.5,
        })
        for bad in (None, [], {}, {"rearAccelLock": 0.5}, {**defaults, "typo": 0}):
            with self.assertRaises(ValueError):
                self.normalize({"differential": bad})
        for key in defaults:
            for bad in (True, "0.5", None, -0.01, 1.01, math.nan, math.inf):
                with self.assertRaises(ValueError):
                    self.normalize({"differential": {**defaults, key: bad}})

    def test_export_import_preserves_every_setting(self):
        exported = next(
            value for node in ast.walk(self.tree) if isinstance(node, ast.Dict)
            for key, value in zip(node.keys, node.values)
            if isinstance(key, ast.Constant) and key.value == "differential"
        )
        import_loop = next(
            node for node in ast.walk(self.tree) if isinstance(node, ast.For)
            and isinstance(node.iter, ast.Name) and node.iter.id == "DIFFERENTIAL_FIELDS"
            and "setattr(preset, prop, differential[key])" in ast.unparse(node)
        )
        values = (0, 0.01, 0.37, 0.99, 1)
        preset = SimpleNamespace(**{prop: value for (_, prop, _), value in zip(self.fields, values)})
        block = eval(compile(ast.Expression(exported), str(ADDON), "eval"), {
            **self.namespace, "preset": preset, "settings": SimpleNamespace(drive="awd"),
        })
        imported = SimpleNamespace()
        exec(compile(ast.Module(body=[import_loop], type_ignores=[]), str(ADDON), "exec"), {
            **self.namespace, "preset": imported,
            "differential": self.normalize({"differential": block}),
        })
        self.assertEqual(vars(imported), vars(preset))

    def test_single_axle_exports_clear_unused_locks_and_fix_balance(self):
        preset = SimpleNamespace(**{prop: 0.37 for _, prop, _ in self.fields})
        for drive, active, inactive, bias in (
            ("fwd", "front", "rear", 1.0), ("rwd", "rear", "front", 0.0),
        ):
            with self.subTest(drive=drive):
                block = self.namespace["build_differential_config"](preset, drive)
                for mode in ("AccelLock", "DecelLock"):
                    self.assertEqual(block[active + mode], 0.37)
                    self.assertEqual(block[inactive + mode], 0.0)
                self.assertEqual(block["centerBalance"], bias)
                self.assertEqual(self.normalize({"differential": block}), block)

    def test_section_shows_only_driven_axle_controls(self):
        preset = SimpleNamespace(display_name="Race")
        drawn = []
        labels = []
        self.namespace["active_preset"] = lambda settings: preset
        self.namespace["draw_split_prop"] = lambda layout, target, prop, **kwargs: drawn.append(prop)
        layout = SimpleNamespace(label=lambda **kwargs: labels.append(kwargs["text"]))
        for drive, expected in (
            ("fwd", ["front_accel_lock", "front_decel_lock"]),
            ("rwd", ["rear_accel_lock", "rear_decel_lock"]),
            ("awd", [prop for _, prop, _ in self.fields]),
        ):
            with self.subTest(drive=drive):
                drawn.clear()
                labels.clear()
                self.namespace["draw_differential"](layout, SimpleNamespace(drive=drive))
                self.assertEqual(drawn, expected)
                self.assertEqual(labels, ["Differential"])
        self.namespace["active_preset"] = lambda settings: None
        drawn.clear()
        self.namespace["draw_differential"](layout, SimpleNamespace(drive="awd"))
        self.assertEqual(drawn, [])


if __name__ == "__main__":
    unittest.main()
