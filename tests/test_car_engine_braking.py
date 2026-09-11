"""Engine manifest round-trip regression tests; no Blender required."""
import ast
import math
from pathlib import Path
from types import SimpleNamespace
import unittest


ADDON = Path(__file__).resolve().parents[1] / "addons/vectorg_car_exporter/__init__.py"


class EngineBrakingTests(unittest.TestCase):
    def setUp(self):
        self.tree = ast.parse(ADDON.read_text(encoding="utf-8"))

    def execute(self, nodes, namespace):
        module = ast.Module(body=nodes, type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), str(ADDON), "exec"), namespace)

    def test_export_import_round_trip(self):
        engine_dict = next(
            node for node in ast.walk(self.tree)
            if isinstance(node, ast.Dict)
            and any(isinstance(key, ast.Constant) and key.value == "engineBraking" for key in node.keys)
        )
        import_assignment = next(
            node for node in ast.walk(self.tree)
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Attribute) and target.attr == "engine_braking" for target in node.targets)
            and isinstance(node.value, ast.Subscript)
        )
        attributes = {
            node.attr for node in ast.walk(engine_dict)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "settings"
        }
        for factor in (0, 0.2, 0.65, 1.5):
            settings = SimpleNamespace(**dict.fromkeys(attributes, 1))
            settings.engine_braking = factor
            engine = eval(compile(ast.Expression(engine_dict), str(ADDON), "eval"), {
                "settings": settings, "sample_torque_curve": lambda _: {},
            })
            self.assertEqual(engine["engineBraking"], factor)
            imported = SimpleNamespace(engine_braking=99)
            self.execute([import_assignment], {"settings": imported, "engine": engine})
            self.assertEqual(imported.engine_braking, factor)

    def test_import_rejects_invalid_factors(self):
        assignment = next(
            node for node in ast.walk(self.tree)
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "engine_braking" for target in node.targets)
        )
        validation = next(
            node for node in ast.walk(self.tree)
            if isinstance(node, ast.If)
            and "Manifest engine.engineBraking must" in ast.unparse(node)
            and isinstance(node.test, ast.BoolOp)
        )
        function = ast.FunctionDef(
            name="validate", args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
            body=[assignment, validation], decorator_list=[],
        )
        for factor in (None, True, "0.2", -0.1, float("nan"), float("inf"), 0, 0.65):
            errors = []
            namespace = {"math": math, "engine": {"engineBraking": factor},
                         "self": SimpleNamespace(report=lambda _, message: errors.append(message))}
            self.execute([function], namespace)
            result = namespace["validate"]()
            if factor in (0, 0.65) and not isinstance(factor, bool):
                self.assertIsNone(result)
                self.assertFalse(errors)
            else:
                self.assertEqual(result, {"CANCELLED"})
                self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
