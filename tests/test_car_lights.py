"""Vehicle light manifest, transform, authoring and helper isolation tests; no Blender required."""
import ast
import copy
import math
from pathlib import Path
from types import SimpleNamespace
import unittest

from test_car_armature import Matrix, Vector as BaseVector


ADDON = Path(__file__).resolve().parents[1] / "addons/vectorg_car_exporter/__init__.py"
FUNCTIONS = {
    "blender_position_to_game", "game_position_to_blender", "light_object_poll",
    "light_helper_objects", "parse_lights_config", "build_lights_config",
    "remove_owned_light_helper", "clear_light_sources", "create_light_helper",
    "validate_light_import", "import_lights_config", "is_object_in_tree",
    "hierarchy_objects", "excluded_ghost_objects", "car_export_objects",
    "with_helpers_unlinked", "set_material_pointer", "draw_vehicle_lights",
    "next_helper_name",
}


class Vector(BaseVector):
    def to_track_quat(self, track, up):
        return (self.x, self.y, self.z, track, up)


class Sources(list):
    def add(self):
        item = SimpleNamespace(role="headlights", object_ref=None, intensity=100, distance=40)
        self.append(item)
        return item

    def remove(self, index):
        del self[index]


class Objects(dict):
    def __iter__(self):
        return iter(self.values())

    def new(self, name, data):
        obj = Object(name, data=data)
        self[name] = obj
        return obj

    def remove(self, obj, do_unlink=False):
        del self[obj.name]
        if obj.data:
            obj.data.users -= 1
        for collection in list(obj.users_collection):
            collection.objects.unlink(obj)


class Lights(dict):
    def new(self, name, type):
        data = SimpleNamespace(type=type, users=1, color=(1, 1, 1), spot_size=math.pi / 3, spot_blend=0.5)
        self[name] = data
        return data

    def remove(self, data):
        for name, item in list(self.items()):
            if item is data:
                del self[name]


class CollectionObjects(dict):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner

    def link(self, obj):
        self[obj.name] = obj
        obj.users_collection.append(self.owner)

    def unlink(self, obj):
        del self[obj.name]
        obj.users_collection.remove(self.owner)


class Object:
    def __init__(self, name, parent=None, data=None):
        self.name, self.parent, self.data = name, parent, data
        self.type = "LIGHT" if data else "EMPTY"
        self.children, self.users_collection, self.props = [], [], {}
        self.matrix_world = Matrix()
        self.matrix_world.translation = Vector((0, 0, 0))
        self.matrix_parent_inverse = SimpleNamespace(identity=lambda: None)
        self.selected = False

    def select_set(self, selected):
        self.selected = selected

    def get(self, key):
        return self.props.get(key)

    def __setitem__(self, key, value):
        self.props[key] = value


class LightTests(unittest.TestCase):
    def setUp(self):
        self.objects, self.lights = Objects(), Lights()
        self.collection = SimpleNamespace()
        self.collection.objects = CollectionObjects(self.collection)
        self.root = self.objects.new("car", None)
        self.settings = SimpleNamespace(car_root_object=self.root, ghost_root_object=None, ghost_enabled=False,
                                        light_sources=Sources())
        self.scene = SimpleNamespace(objects=[self.root], collection=self.collection, car_exporter=self.settings,
                                     cursor=SimpleNamespace(location=Vector((0, 0, 0))))
        self.context = SimpleNamespace(scene=self.scene, view_layer=SimpleNamespace(objects=SimpleNamespace(active=None)),
                                       active_object=None)
        self.materials = {"red": SimpleNamespace(name="red")}
        self.api = {"math": math, "Vector": Vector,
                    "bpy": SimpleNamespace(context=self.context,
                                           data=SimpleNamespace(objects=self.objects, lights=self.lights, materials=self.materials)),
                    "scene_settings": lambda context: self.settings,
                    "guide_objects": lambda: [], "downforce_helper_objects": lambda: [],
                    "EnumProperty": lambda **kwargs: None, "BoolProperty": lambda **kwargs: None,
                    "IntProperty": lambda **kwargs: None, "Operator": object}
        selected = []
        for node in ast.parse(ADDON.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS:
                selected.append(node)
            elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                if node.targets[0].id in {"LIGHT_ROLES", "LIGHT_ROLE_ITEMS", "LIGHT_HELPER_PROP", "MAX_LIGHT_SOURCES"}:
                    selected.append(node)
            elif isinstance(node, ast.ClassDef) and node.name in {
                "CAR_EXPORTER_OT_add_light_source", "CAR_EXPORTER_OT_remove_light_source",
            }:
                selected.append(node)
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(ADDON), "exec"), self.api)
        for _role, prefix, _label in self.api["LIGHT_ROLES"]:
            setattr(self.settings, f"{prefix}_material", None)
            setattr(self.settings, f"{prefix}_emissive_intensity", 10)

    def call(self, name, *args):
        return self.api[name](*args)

    def source(self, name="lamp", role="headlights", owned=True):
        obj = self.objects.new(name, self.lights.new(name, "SPOT"))
        obj.parent = self.root
        if owned:
            obj[self.api["LIGHT_HELPER_PROP"]] = True
        self.scene.objects.append(obj)
        self.collection.objects.link(obj)
        source = self.settings.light_sources.add()
        source.object_ref, source.role = obj, role
        return obj

    def definition(self):
        return {"name": "lamp", "position": [1, 0.8, -2], "direction": [0, 0, -1],
                "color": [1, 0, 0], "intensity": 5, "distance": 5,
                "angle": math.radians(70), "penumbra": 0.5}

    def config(self):
        return {"headlights": None, "brakeLights": {"material": "red", "emissiveIntensity": 12,
                                                    "sources": [self.definition()]}, "reverseLights": None}

    def test_unconfigured_lights_export_null(self):
        self.assertIsNone(self.call("build_lights_config", self.settings))
        for value in [None, {}, {"headlights": None}, {"brakeLights": {"material": None, "sources": []}}]:
            self.assertIsNone(self.call("parse_lights_config", value))

    def test_material_only_and_source_only_roles_are_independent(self):
        self.settings.brake_lights_material = self.materials["red"]
        self.source()
        config = self.call("build_lights_config", self.settings)
        self.assertIsNone(config["headlights"]["material"])
        self.assertEqual(len(config["headlights"]["sources"]), 1)
        self.assertEqual(config["brakeLights"], {"material": "red", "emissiveIntensity": 10, "sources": None})
        self.assertIsNone(config["reverseLights"])

    def test_existing_material_only_config_imports_with_default_emission(self):
        config = self.call("parse_lights_config", {"headlights": {"material": "red"}})
        self.call("import_lights_config", self.context, self.settings, config)
        self.assertEqual(self.call("build_lights_config", self.settings), config)

    def test_car_local_transform_with_rotated_scaled_root_and_nested_parent(self):
        self.root.matrix_world = Matrix(math.pi / 2, (10, 20, 30), (2, 3, 4))
        obj = self.source()
        obj.parent = Object("nested", self.root)
        obj.matrix_world.rows = [[0, 0, -3, 4], [0, 1, 0, 22], [1, 0, 0, 42], [0, 0, 0, 1]]
        obj.matrix_world.translation = Vector((4, 22, 42))
        source = self.call("build_lights_config", self.settings)["headlights"]["sources"][0]
        for actual, expected in zip(source["position"], (1, 3, -2)):
            self.assertAlmostEqual(actual, expected)
        for actual, expected in zip(source["direction"], (0, 0, 1)):
            self.assertAlmostEqual(actual, expected)
        self.assertAlmostEqual(source["angle"], math.pi / 6)
        self.assertEqual(source["penumbra"], 0.5)

    def test_invalid_authoring_assignments_rejected(self):
        obj = self.source()
        cases = [lambda: setattr(obj, "parent", None),
                 lambda: setattr(obj.data, "type", "POINT"),
                 lambda: setattr(obj, "children", [Object("child")]),
                 lambda: setattr(self.settings, "ghost_root_object", obj),
                 lambda: self.settings.light_sources.append(self.settings.light_sources[0]),
                 lambda: setattr(self.root, "matrix_world", Matrix(scale=(0, 1, 1)))]
        for change in cases:
            with self.subTest(change=change):
                change()
                with self.assertRaises(ValueError):
                    self.call("build_lights_config", self.settings)
                obj.parent, obj.data.type, obj.children = self.root, "SPOT", []
                self.settings.ghost_root_object = None
                self.root.matrix_world = Matrix()
                del self.settings.light_sources[1:]

    def test_rejects_malformed_external_definitions(self):
        invalid = [("direction", [0, 0, 0]), ("position", [1, 2]), ("color", [1, -1, 0]),
                   ("intensity", True), ("intensity", float("nan")), ("distance", 0),
                   ("angle", math.pi), ("angle", 0), ("penumbra", 1.1), ("name", "")]
        for field, value in invalid:
            with self.subTest(field=field, value=value):
                config = self.config()
                config["brakeLights"]["sources"][0][field] = value
                with self.assertRaises(ValueError):
                    self.call("parse_lights_config", config)
        for value in [False, [], {"unknown": None}, {"headlights": {"material": ""}},
                      {"headlights": {"sources": {}}}, {"headlights": {"emissiveIntensity": -1}}]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.call("parse_lights_config", value)

    def test_duplicate_names_and_source_limit_rejected(self):
        config = self.config()
        config["headlights"] = copy.deepcopy(config["brakeLights"])
        with self.assertRaisesRegex(ValueError, "unique"):
            self.call("parse_lights_config", config)
        config = self.config()
        config["brakeLights"]["sources"] = [dict(self.definition(), name=f"lamp_{i}") for i in range(17)]
        with self.assertRaisesRegex(ValueError, "at most"):
            self.call("parse_lights_config", config)

    def test_helper_export_exclusion_and_restoration_on_failure(self):
        owned = self.source()
        assigned = self.source("assigned", owned=False)
        self.assertEqual(self.call("car_export_objects", self.settings), [self.root])

        def exporting():
            self.assertFalse(owned.users_collection)
            self.assertFalse(assigned.users_collection)
            raise RuntimeError("export failed")

        with self.assertRaisesRegex(RuntimeError, "export failed"):
            self.call("with_helpers_unlinked", exporting)
        self.assertIn(owned.name, self.collection.objects)
        self.assertIn(assigned.name, self.collection.objects)

    def test_import_recreates_sources_and_restores_role_and_beam(self):
        self.call("import_lights_config", self.context, self.settings, self.config())
        obj = self.objects["lamp"]
        self.assertIs(obj.parent, self.root)
        self.assertEqual((obj.location.x, obj.location.y, obj.location.z), (1, 2, 0.8))
        self.assertEqual(obj.rotation_quaternion, (0, 1, 0, '-Z', 'Y'))
        self.assertAlmostEqual(obj.data.spot_size, math.radians(140))
        self.assertEqual(obj.data.color, [1, 0, 0])
        self.assertEqual(self.settings.brake_lights_emissive_intensity, 12)
        self.assertEqual(self.settings.light_sources[0].role, "brakeLights")
        self.assertEqual(self.settings.light_sources[0].intensity, 5)
        self.call("import_lights_config", self.context, self.settings, self.config())
        self.assertIs(self.objects["lamp"], obj)
        self.assertEqual(len(self.settings.light_sources), 1)
        self.call("import_lights_config", self.context, self.settings, None)
        self.assertNotIn("lamp", self.objects)
        self.assertFalse(self.lights)
        self.assertIsNone(self.call("build_lights_config", self.settings))

    def test_import_conflicts_fail_before_clearing_existing_sources(self):
        obj = self.source("existing")
        self.objects.new("lamp", None)
        with self.assertRaisesRegex(ValueError, "conflicts"):
            self.call("import_lights_config", self.context, self.settings, self.config())
        self.assertIs(self.settings.light_sources[0].object_ref, obj)
        with self.assertRaisesRegex(ValueError, "root"):
            self.call("validate_light_import", None, self.config())

    def test_clear_does_not_delete_user_assigned_objects(self):
        obj = self.source(owned=False)
        self.call("clear_light_sources", self.settings)
        self.assertIs(self.objects["lamp"], obj)

    def test_add_operator_presets_and_assign_selected(self):
        for role, expected_direction in [("headlights", (0, -0.999390827, -0.034899497)), ("brakeLights", (0, 1, 0)),
                                         ("reverseLights", (0, 1, 0))]:
            operator = self.api["CAR_EXPORTER_OT_add_light_source"]()
            operator.role, operator.use_selected = role, False
            self.assertEqual(operator.execute(self.context), {"FINISHED"})
            obj = self.settings.light_sources[-1].object_ref
            for actual, expected in zip(obj.rotation_quaternion[:3], expected_direction):
                self.assertAlmostEqual(actual, expected)
            self.assertAlmostEqual(math.degrees(obj.data.spot_size), 40 if role == "headlights" else 140)
            self.assertEqual(self.settings.light_sources[-1].distance, 40 if role == "headlights" else 5)
            self.assertEqual(obj.data.color, (1, 0, 0) if role == "brakeLights" else (1, 1, 1))
        user = self.source("user", owned=False)
        self.settings.light_sources.pop()
        self.context.active_object = user
        operator.use_selected = True
        self.assertEqual(operator.execute(self.context), {"FINISHED"})
        self.assertIs(self.settings.light_sources[-1].object_ref, user)

    def test_invalid_add_does_not_leave_orphan_helpers(self):
        operator = self.api["CAR_EXPORTER_OT_add_light_source"]()
        operator.role, operator.use_selected = "headlights", False
        operator.report = lambda *_args: None
        self.root.matrix_world = Matrix(scale=(0, 1, 1))
        self.assertEqual(operator.execute(self.context), {"CANCELLED"})
        self.assertFalse(self.settings.light_sources)
        self.assertFalse(self.lights)

    def test_remove_operator_keeps_user_object_and_deletes_owned_helper(self):
        operator = self.api["CAR_EXPORTER_OT_remove_light_source"]()
        operator.index = 0
        user = self.source("user", owned=False)
        self.assertEqual(operator.execute(self.context), {"FINISHED"})
        self.assertIn(user.name, self.objects)
        self.source("owned")
        self.assertEqual(operator.execute(self.context), {"FINISHED"})
        self.assertNotIn("owned", self.objects)
        self.assertNotIn("owned", self.lights)


if __name__ == "__main__":
    unittest.main()
