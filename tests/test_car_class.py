"""Vehicle class registry parity with the game; no Blender required."""
import ast
from pathlib import Path
import re
import unittest


ADDON = Path(__file__).resolve().parents[1] / "addons/vectorg_car_exporter/__init__.py"
GAME_REGISTRY = Path(__file__).resolve().parents[2] / "vectorg/vehicleClasses.js"


def addon_constant(name):
    tree = ast.parse(ADDON.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} is not defined")


class CarClassTests(unittest.TestCase):
    def test_class_items_match_game_registry(self):
        items = addon_constant("VEHICLE_CLASS_ITEMS")
        game_classes = re.findall(
            r"\{ code: '([A-Z]+)', name: '([^']+)' \}",
            GAME_REGISTRY.read_text(encoding="utf-8"),
        )
        self.assertEqual(
            [(code, label) for code, label, _description in items],
            [(code, f"{code} {name}") for code, name in game_classes],
        )
        self.assertIn(addon_constant("DEFAULT_VEHICLE_CLASS"), {code for code, _label, _description in items})

    def test_class_is_an_enum_property(self):
        source = ADDON.read_text(encoding="utf-8")
        self.assertRegex(source, r"car_class: EnumProperty\(\s*name=\"Class\",[^)]*items=VEHICLE_CLASS_ITEMS")
        self.assertIn('"class": settings.car_class,', source)


if __name__ == "__main__":
    unittest.main()
