"""Vehicle lights: manifest contract and lamp material checks; no host application required."""
import ast
import copy
import math
from pathlib import Path
from types import SimpleNamespace
import unittest


ADDON = Path(__file__).resolve().parents[1] / "addons/vectorg_car_exporter/__init__.py"
FUNCTIONS = {
    "parse_lights_config", "build_lights_config", "lamp_materials", "lamp_emission_strength",
    "validate_lights_scene", "is_object_in_tree",
}
CONSTANTS = {"LIGHT_ROLES", "LIGHT_MARKER_PROP", "LIGHT_MARKERS"}

LIGHTS = {
    "materials": {"lights": "lamps", "brake": "brake", "reverse": "reverse"},
    "emission": {"lights": 8, "brake": 12, "reverse": 6},
    "front": {"position": [0.62, 0.7, 1.9], "direction": [0, -0.1, 1]},
    "rear": {"position": [0.58, 0.8, -1.95]},
}


class Material:
    def __init__(self, name, strength):
        principled = SimpleNamespace(type="BSDF_PRINCIPLED",
                                     inputs={"Emission Strength": SimpleNamespace(default_value=strength)})
        self.name, self.use_nodes, self.node_tree = name, True, SimpleNamespace(nodes=[principled])


def material(name, strength=1.0):
    return Material(name, strength)


class Marker:
    def __init__(self, parent):
        self.name, self.parent = "marker", parent


class LightTests(unittest.TestCase):
    def setUp(self):
        self.root = SimpleNamespace(name="car", parent=None)
        self.marker = Marker(self.root)
        self.settings = SimpleNamespace(
            car_root_object=self.root, front_light_marker=self.marker, rear_light_marker=self.marker,
            lights_material=material("lamps"), brake_material=material("brake"), reverse_material=material("reverse"),
            lights_emissive_intensity=8, brake_emissive_intensity=12, reverse_emissive_intensity=6,
        )
        self.api = {
            "math": math,
            "bpy": SimpleNamespace(context=SimpleNamespace(scene=SimpleNamespace(objects=[self.marker]))),
            "light_marker_frame": lambda root, obj: ([0.62, 0.7, 1.9], [0, -0.1, 1]),
        }
        selected = []
        for node in ast.parse(ADDON.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS:
                selected.append(node)
            elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id in CONSTANTS:
                selected.append(node)
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(ADDON), "exec"), self.api)

    def call(self, name, *args):
        return self.api[name](*args)

    def validate(self, exported=("lamps", "brake", "reverse")):
        errors, warnings = [], []
        self.call("validate_lights_scene", self.settings, set(exported), errors, warnings)
        return errors, warnings

    def test_manifest_round_trip_matches_the_game_contract(self):
        built = self.call("build_lights_config", self.settings)
        self.assertEqual(built["materials"], LIGHTS["materials"])
        self.assertEqual(built["emission"], LIGHTS["emission"])
        self.assertAlmostEqual(math.hypot(*built["front"]["direction"]), 1)
        self.assertEqual(self.call("parse_lights_config", built), built)
        self.assertIsNone(self.call("parse_lights_config", None))

    def test_malformed_manifests_are_rejected(self):
        broken = [{**LIGHTS, "extra": 1}, {**LIGHTS, "materials": {**LIGHTS["materials"], "lights": ""}},
                  {**LIGHTS, "materials": {"lights": None, "brake": None, "reverse": None}},
                  {**LIGHTS, "materials": {**LIGHTS["materials"], "brake": "lamps"}},
                  {**LIGHTS, "materials": {"lights": "lamps", "brake": "brake"}},
                  {**LIGHTS, "emission": {"lights": 1, "brake": 1}},
                  {**LIGHTS, "emission": {**LIGHTS["emission"], "reverse": -1}},
                  {**LIGHTS, "front": {"position": [0, 0, 0]}},
                  {**LIGHTS, "front": {"position": [0, 0, 0], "direction": [0, 0, -1]}},
                  {**LIGHTS, "rear": {"position": [0, 0, math.nan]}},
                  {"material": "lamps", "mask": "lamp_mask.png", "front": LIGHTS["front"], "rear": LIGHTS["rear"],
                   "emission": LIGHTS["emission"]}]
        for config in broken:
            with self.subTest(config=config), self.assertRaises(ValueError):
                self.call("parse_lights_config", copy.deepcopy(config))

    def test_a_car_without_lamp_materials_exports_no_lights(self):
        self.settings.lights_material = self.settings.brake_material = self.settings.reverse_material = None
        self.assertIsNone(self.call("build_lights_config", self.settings))
        self.assertEqual(self.validate(), ([], []))

    def test_a_missing_function_only_warns_and_exports_null(self):
        self.settings.reverse_material = None
        self.assertIsNone(self.call("build_lights_config", self.settings)["materials"]["reverse"])
        errors, warnings = self.validate()
        self.assertEqual(errors, [])
        self.assertIn("No Reverse Lights material: those lamps never light", warnings)

    def test_functions_need_their_own_materials_markers_and_an_emission_to_scale(self):
        self.settings.brake_material = self.settings.lights_material
        self.assertIn("Each light function needs its own lamp material", self.validate()[0])
        self.settings.brake_material = material("brake")
        self.settings.rear_light_marker = None
        self.assertIn("Lights need the Rear Lamp marker", self.validate()[0])
        self.settings.rear_light_marker = self.marker
        self.settings.lights_material = material("lamps", strength=0.0)
        errors, _warnings = self.validate(exported=("lamps", "brake"))
        self.assertIn("Headlights / Tail Lights material lamps needs an Emission Strength above 0; "
                      "the game scales it by Full Emission", errors)
        self.assertIn("Reverse Lights material is not used by an exported mesh: reverse", errors)


if __name__ == "__main__":
    unittest.main()
