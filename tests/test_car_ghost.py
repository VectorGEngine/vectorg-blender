"""Custom ghost contract and export isolation tests; no Blender required."""
import ast
import copy
from pathlib import Path
from types import SimpleNamespace
import unittest


ADDON = Path(__file__).resolve().parents[1] / "addons/vectorg_car_exporter/__init__.py"
FUNCTIONS = {
    "find_object", "object_config_name", "set_object_pointer", "is_object_in_tree",
    "hierarchy_objects", "ghost_wheel_settings", "excluded_ghost_objects",
    "car_export_objects", "build_ghost_config", "parse_ghost_config",
    "import_ghost_config", "validate_ghost_scene", "with_helpers_unlinked",
    "object_with_assigned_material", "material_is_assigned_to_geometry",
}


class Object:
    def __init__(self, name, parent=None, material=None):
        self.name = name
        self.parent = parent
        self.children = []
        self.users_collection = []
        self.type = "MESH" if material else "EMPTY"
        self.data = SimpleNamespace(polygons=[SimpleNamespace(material_index=0)] if material else [])
        self.material_slots = [SimpleNamespace(material=material)] if material else []
        if parent:
            parent.children.append(self)


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


class GhostTests(unittest.TestCase):
    def setUp(self):
        self.objects = {}
        self.scene_objects = []
        self.api = {
            "bpy": SimpleNamespace(
                data=SimpleNamespace(objects=self.objects),
                context=SimpleNamespace(scene=SimpleNamespace(objects=self.scene_objects)),
            ),
            "guide_objects": lambda: [],
            "downforce_helper_objects": lambda: [],
        }
        selected = []
        for node in ast.parse(ADDON.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS:
                selected.append(node)
            elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                if node.targets[0].id in {"WHEEL_KEYS", "WHEEL_LABELS", "GHOST_WHEEL_ROLES", "CAMERA_PREFIXES", "BLENDER_AXIS_TO_GAME", "GAME_AXIS_TO_BLENDER"}:
                    selected.append(node)
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(ADDON), "exec"), self.api)
        self.paint = object()
        self.root = self.obj("car")
        self.normal = self.obj("body", self.root, self.paint)
        self.ghost = self.obj("ghost", self.root)
        self.ghost_body = self.obj("ghost_body", self.ghost, self.paint)
        self.settings = SimpleNamespace(
            car_root_object=self.root, ghost_enabled=True, ghost_root_object=self.ghost,
            center_of_mass_object=None, steering_wheel_object=None, dashboard_screen_object=None,
            colliders=[], down_force_points=[], wheels=[],
            body_colors=[SimpleNamespace(material=self.paint)],
            headlights_material=None, brake_lights_material=None, reverse_lights_material=None,
        )
        for prefix in self.api["CAMERA_PREFIXES"]:
            setattr(self.settings, f"{prefix}_camera_object", None)
        for group, key, _steering in self.api["WHEEL_KEYS"]:
            mount = self.obj(f"mount_{group}_{key}", self.ghost)
            joint = self.obj(f"joint_{group}_{key}", mount)
            spin = self.obj(f"spin_{group}_{key}", joint, self.paint)
            setattr(self.settings, f"ghost_{group}_{key}", SimpleNamespace(
                suspension_ref=mount, hub_ref=joint, wheel_ref=spin,
            ))

    def obj(self, name, parent=None, material=None):
        result = Object(name, parent, material)
        self.objects[name] = result
        self.scene_objects.append(result)
        return result

    def call(self, name, *args):
        return self.api[name](*args)

    def validate(self):
        errors, warnings = [], []
        self.call("validate_ghost_scene", self.settings, errors, warnings)
        return errors, warnings

    def test_enabled_contract_round_trip(self):
        config = self.call("build_ghost_config", self.settings)
        self.assertEqual(config["obj"], "ghost")
        self.assertEqual(config["wheels"]["front"]["l"]["spin"], {"obj": "spin_front_l"})
        self.assertEqual(config["wheels"]["front"]["l"]["joint"], {"obj": "joint_front_l"})
        self.assertEqual(set(config), {"obj", "wheels"})
        parsed = self.call("parse_ghost_config", config)
        self.settings.ghost_enabled = False
        self.settings.ghost_root_object = None
        self.settings.ghost_front_l.wheel_ref = None
        self.call("import_ghost_config", self.settings, parsed)
        self.assertEqual(self.call("build_ghost_config", self.settings), config)
        self.assertEqual(self.validate(), ([], []))

    def test_null_disables_but_preserves_selections(self):
        self.call("import_ghost_config", self.settings, self.call("parse_ghost_config", None))
        self.assertFalse(self.settings.ghost_enabled)
        self.assertIsNone(self.call("build_ghost_config", self.settings))
        self.assertIs(self.settings.ghost_root_object, self.ghost)
        self.assertIsNotNone(self.settings.ghost_front_l.wheel_ref)
        exported = self.call("car_export_objects", self.settings)
        self.assertEqual(exported, [self.root, self.normal])
        self.assertEqual(len(self.scene_objects), 16)

    def test_enabled_keeps_full_glb_hierarchy(self):
        self.assertEqual(self.call("excluded_ghost_objects", self.settings), [])
        self.assertEqual(self.call("car_export_objects", self.settings), self.scene_objects)

    def test_invalid_manifest_shapes_and_references(self):
        config = self.call("build_ghost_config", self.settings)
        invalid = [False, True, "ghost", [], {}, {"obj": " "}, {"obj": "ghost", "wheels": []}]
        missing = copy.deepcopy(config)
        del missing["wheels"]["rear"]["r"]["spin"]
        invalid.append(missing)
        duplicate = copy.deepcopy(config)
        duplicate["wheels"]["rear"]["r"] = duplicate["wheels"]["front"]["l"]
        invalid.append(duplicate)
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.call("parse_ghost_config", value)

    def test_joint_and_spin_can_share_a_node(self):
        self.settings.ghost_front_l.hub_ref = self.settings.ghost_front_l.wheel_ref
        config = self.call("build_ghost_config", self.settings)
        self.assertEqual(self.call("parse_ghost_config", config), config)
        self.assertEqual(self.validate(), ([], []))

    def test_missing_root_required_only_when_enabled(self):
        self.settings.ghost_root_object = None
        self.assertIn("root object is required", " ".join(self.validate()[0]))
        self.settings.ghost_enabled = False
        self.assertEqual(self.validate(), ([], []))

    def test_root_must_be_direct_child(self):
        self.ghost.parent = self.normal
        self.assertIn("direct child", " ".join(self.validate()[0]))

    def test_normal_references_cannot_be_removed_when_disabled(self):
        self.settings.ghost_enabled = False
        self.settings.wheels = [self.settings.ghost_front_l]
        self.assertIn("normal car", " ".join(self.validate()[0]))

    def test_wheel_references_must_belong_to_ghost(self):
        self.settings.ghost_front_l.wheel_ref = self.normal
        self.assertIn("exported ghost hierarchy", " ".join(self.validate()[0]))

    def test_wheel_hierarchies_cannot_overlap(self):
        self.settings.ghost_front_r = self.settings.ghost_front_l
        self.assertIn("overlap", " ".join(self.validate()[0]))

    def test_joint_must_belong_to_its_mount(self):
        self.settings.ghost_front_l.hub_ref.parent = self.ghost
        self.assertIn("joint must be inside mount", " ".join(self.validate()[0]))

    def test_unlinked_wheel_reference_rejected(self):
        self.scene_objects.remove(self.settings.ghost_front_l.wheel_ref)
        self.assertIn("exported ghost hierarchy", " ".join(self.validate()[0]))

    def test_ghost_paint_required_and_lights_warn(self):
        self.settings.body_colors[0].material = object()
        self.settings.headlights_material = object()
        errors, warnings = self.validate()
        self.assertIn("Default body color", " ".join(errors))
        self.assertIn("Headlights", " ".join(warnings))

    def test_authored_materials_are_not_changed(self):
        material = object()
        trim = self.obj("trim", self.ghost, material)
        self.validate()
        self.call("build_ghost_config", self.settings)
        self.assertIs(trim.material_slots[0].material, material)

    def test_export_exclusion_restores_all_collections_on_success_and_failure(self):
        collections = [SimpleNamespace(), SimpleNamespace()]
        for collection in collections:
            collection.objects = CollectionObjects(collection)
            for obj in self.scene_objects:
                collection.objects.link(obj)
        self.settings.ghost_enabled = False
        excluded = self.call("excluded_ghost_objects", self.settings)
        self.api["guide_objects"] = lambda: [self.ghost_body]  # Exclusion lists can overlap.

        def exporting(fail):
            for collection in collections:
                self.assertEqual(set(collection.objects), {"car", "body"})
            if fail:
                raise RuntimeError("export failed")
            return "exported"

        self.assertEqual(self.call("with_helpers_unlinked", lambda: exporting(False), excluded), "exported")
        with self.assertRaisesRegex(RuntimeError, "export failed"):
            self.call("with_helpers_unlinked", lambda: exporting(True), excluded)
        for collection in collections:
            self.assertEqual(set(collection.objects), set(self.objects))
        for obj in self.scene_objects:
            self.assertEqual(obj.users_collection, collections)
        self.assertIs(self.ghost.parent, self.root)


if __name__ == "__main__":
    unittest.main()
