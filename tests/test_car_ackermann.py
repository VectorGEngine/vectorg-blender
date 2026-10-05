"""Ackermann preset contract tests; no Blender runtime required."""
import ast
from pathlib import Path
import unittest


ADDON = Path(__file__).resolve().parents[1] / "addons/vectorg_car_exporter/__init__.py"


class AckermannTests(unittest.TestCase):
    def setUp(self):
        self.tree = ast.parse(ADDON.read_text(encoding="utf-8"))

    def function(self, name):
        return next(node for node in self.tree.body if isinstance(node, ast.FunctionDef) and node.name == name)

    def test_preset_property_spans_anti_to_full_ackermann(self):
        properties = [
            node for node in ast.walk(self.tree)
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "ackermann"
        ]
        self.assertEqual(len(properties), 1)
        keywords = {keyword.arg: keyword.value for keyword in properties[0].annotation.keywords}
        self.assertEqual(ast.literal_eval(keywords["default"]), 1.0)
        self.assertEqual(ast.literal_eval(keywords["min"]), -1.0)
        self.assertEqual(ast.literal_eval(keywords["max"]), 1.0)

    def test_manifest_export_and_defaults_include_ackermann(self):
        exported = [
            node for node in ast.walk(self.function("build_presets_config"))
            if isinstance(node, ast.Dict)
            and any(isinstance(key, ast.Constant) and key.value == "ackermann" for key in node.keys)
        ]
        self.assertEqual(len(exported), 1)
        value = exported[0].values[[key.value for key in exported[0].keys if isinstance(key, ast.Constant)].index("ackermann")]
        self.assertEqual(ast.unparse(value), "preset.ackermann")

        defaults = next(
            node for node in ast.walk(self.function("default_preset_values")) if isinstance(node, ast.Dict)
        )
        entries = {key.value: value for key, value in zip(defaults.keys, defaults.values) if isinstance(key, ast.Constant)}
        self.assertEqual(ast.literal_eval(entries["ackermann"]), 1.0)
        self.assertIn("'ackermann'", ast.unparse(self.function("apply_preset_values")))

    def test_manifest_import_requires_ackermann(self):
        source = ADDON.read_text(encoding="utf-8")
        self.assertIn('ackermann = preset_data.get("ackermann")', source)
        self.assertIn('preset.ackermann = preset_data["ackermann"]', source)
        self.assertIn("ackermann must be between -1 and 1", source)


if __name__ == "__main__":
    unittest.main()
