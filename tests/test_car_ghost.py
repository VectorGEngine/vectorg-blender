"""LOD levels, ghost default level and ghost shadow contract tests; no host application required."""
import ast
import re
from pathlib import Path
from types import SimpleNamespace
import unittest


ADDON = Path(__file__).resolve().parents[1] / "addons/vectorg_car_exporter/__init__.py"
FUNCTIONS = {
    "find_object", "object_config_name", "set_object_pointer", "hierarchy_objects",
    "lod_name_level", "lod_level_row", "car_lod_meshes", "exported_lod_levels", "lod_dropped_objects",
    "lod_object_ranges", "ghost_lod_position", "build_lods_config", "parse_lods_config",
    "import_lods_config", "sync_lod_level_rows", "validate_lods_scene", "build_ghost_config",
    "parse_ghost_config", "import_ghost_config", "validate_ghost_helper", "validate_ghost_scene",
    "is_object_in_tree", "in_car_body",
}


class Object:
    def __init__(self, name, parent=None, mesh=True):
        self.name = name
        self.parent = parent
        self.children = []
        self.type = "MESH" if mesh else "EMPTY"
        self.data = SimpleNamespace(polygons=[object()] if mesh else [])
        if parent:
            parent.children.append(self)


class Rows(list):
    def add(self):
        row = SimpleNamespace(level=0, export=True, screen_height=0.0)
        self.append(row)
        return row


class LodTests(unittest.TestCase):
    def setUp(self):
        self.objects = {}
        self.scene_objects = []
        self.api = {
            "re": re,
            "bpy": SimpleNamespace(
                data=SimpleNamespace(objects=self.objects),
                context=SimpleNamespace(scene=SimpleNamespace(objects=self.scene_objects)),
            ),
            "car_reference_objects": lambda settings: set(),
        }
        selected = []
        for node in ast.parse(ADDON.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS:
                selected.append(node)
            elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) \
                    and node.targets[0].id == "LOD_NAME_PATTERN":
                selected.append(node)
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(ADDON), "exec"), self.api)
        self.root = self.obj("Car", mesh=False)
        self.spin = self.obj("Spin", self.root, mesh=False)
        # Three levels: the body has all of them, the wheel ends at LOD1, the badge starts at LOD1.
        for name, parent in (("Body_LOD0", self.root), ("Body_LOD1", self.root), ("Body_LOD2", self.root),
                             ("Wheel_LOD0", self.spin), ("Wheel_LOD1", self.spin), ("Badge_LOD1", self.root),
                             ("Mirror", self.root)):
            self.obj(name, parent)
        self.shadow = self.obj("GHOST_SHADOW", self.root)
        self.plane = self.obj("GHOST_SHADOW_PLANE", self.root)
        self.settings = SimpleNamespace(
            car_root_object=self.root, lods_enabled=True, lod_levels=Rows(), ghost_lod=1, colliders=[],
            shadow_object=None, ambient_shadow_object=None,
            ghost_shadow_object=self.shadow, ghost_shadow_plane_object=self.plane,
        )
        self.call("sync_lod_level_rows", self.settings)
        for row, height in zip(self.settings.lod_levels, (50.0, 20.0, 5.0)):
            row.screen_height = height

    def obj(self, name, parent=None, mesh=True):
        obj = Object(name, parent, mesh)
        self.objects[name] = obj
        self.scene_objects.append(obj)
        return obj

    def call(self, name, *args):
        return self.api[name](*args)

    def names(self, objects):
        return sorted(obj.name for obj in objects)

    def ranges(self):
        return {obj.name: span for obj, span in self.call("lod_object_ranges", self.settings).items()}

    def validate(self):
        errors, warnings = [], []
        self.call("validate_lods_scene", self.settings, errors, warnings)
        self.call("validate_ghost_scene", self.settings, errors, warnings)
        return errors, warnings

    def test_levels_come_from_name_suffixes(self):
        self.assertEqual(self.call("lod_name_level", "Body_LOD12"), ("Body", 12))
        self.assertEqual(self.call("lod_name_level", "Body_LOD1.001"), ("Body", 1))
        self.assertIsNone(self.call("lod_name_level", "Body_LODx"))
        self.assertIsNone(self.call("lod_name_level", "Mirror"))
        found = self.call("car_lod_meshes", self.settings)
        self.assertEqual(list(found), [0, 1, 2])
        self.assertEqual(self.names(found[1]), ["Badge_LOD1", "Body_LOD1", "Wheel_LOD1"])
        self.assertEqual([row.level for row in self.settings.lod_levels], [0, 1, 2])

    def test_each_level_shows_its_own_meshes_only(self):
        self.assertEqual(self.ranges(), {
            "Body_LOD0": (0, 0), "Body_LOD1": (1, 1), "Body_LOD2": (2, 2),
            "Wheel_LOD0": (0, 0), "Wheel_LOD1": (1, 1), "Badge_LOD1": (1, 1),
        })
        errors, warnings = self.validate()
        self.assertEqual(errors, [])
        self.assertFalse([warning for warning in warnings if "LOD" in warning], "parts may end or start at any level")

    def test_unticked_levels_are_skipped_and_dropped(self):
        self.call("lod_level_row", self.settings, 1).export = False
        self.assertEqual(self.call("exported_lod_levels", self.settings), [0, 2])
        self.assertEqual(self.names(self.call("lod_dropped_objects", self.settings)), ["Badge_LOD1", "Body_LOD1", "Wheel_LOD1"])
        self.assertEqual(self.ranges()["Wheel_LOD0"], (0, 0), "the wheel has no mesh at the next exported level")
        self.assertEqual(self.ranges()["Body_LOD2"], (1, 1))
        # LOD0 always exports.
        self.call("lod_level_row", self.settings, 0).export = False
        self.assertEqual(self.call("exported_lod_levels", self.settings)[0], 0)

    def test_lods_off_export_lod0_only(self):
        self.settings.lods_enabled = False
        self.assertEqual(self.call("exported_lod_levels", self.settings), [0])
        self.assertEqual(self.names(self.call("lod_dropped_objects", self.settings)),
                         ["Badge_LOD1", "Body_LOD1", "Body_LOD2", "Wheel_LOD1"])
        self.assertIsNone(self.call("build_lods_config", self.settings))
        self.assertEqual(self.call("ghost_lod_position", self.settings), 0)

    def test_manifest_round_trip(self):
        config = self.call("build_lods_config", self.settings, {"MERGED_Body_LOD1": (1, 1), "Wheel_LOD1": (1, 2)})
        self.assertEqual(config, {
            "levels": [{"level": 0, "screenHeight": 0.5}, {"level": 1, "screenHeight": 0.2},
                       {"level": 2, "screenHeight": 0.05}],
            "ghost": 1,
            "objects": {"MERGED_Body_LOD1": [1, 1], "Wheel_LOD1": [1, 2]},
        })
        parsed = self.call("parse_lods_config", config)
        self.settings.lod_levels = Rows()
        self.settings.lods_enabled = False
        self.settings.ghost_lod = 0
        self.call("import_lods_config", self.settings, parsed)
        self.assertTrue(self.settings.lods_enabled)
        self.assertEqual([(row.level, row.export, row.screen_height) for row in self.settings.lod_levels],
                         [(0, True, 50.0), (1, True, 20.0), (2, True, 5.0)])
        self.assertEqual(self.settings.ghost_lod, 1)
        ghost = self.call("build_ghost_config", self.settings)
        self.assertEqual(ghost, {"shadow": {"obj": "GHOST_SHADOW"}, "shadowPlane": {"obj": "GHOST_SHADOW_PLANE"}})
        self.settings.ghost_shadow_object = self.settings.ghost_shadow_plane_object = None
        self.call("import_ghost_config", self.settings, self.call("parse_ghost_config", ghost))
        self.assertIs(self.settings.ghost_shadow_object, self.shadow)
        self.assertIs(self.settings.ghost_shadow_plane_object, self.plane)

    def test_invalid_manifest_shapes(self):
        valid = {"levels": [{"level": 0, "screenHeight": 0.5}, {"level": 1, "screenHeight": 0.2}], "ghost": 1, "objects": {}}
        self.assertIsNone(self.call("parse_lods_config", None))
        for change in ({"levels": []}, {"levels": [{"level": 1, "screenHeight": 0.5}]},
                       {"levels": [{"level": 0, "screenHeight": 0.5}, {"level": 1, "screenHeight": 0.6}]},
                       {"levels": [{"level": 0, "screenHeight": 0}]}, {"ghost": 2}, {"ghost": True},
                       {"objects": {"A_LOD0": [1, 0]}}, {"objects": {"A_LOD0": [0, 2]}}, {"objects": []}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.call("parse_lods_config", {**valid, **change})
        for ghost in ([], {"shadow": {"obj": " "}}, {"shadow": {"obj": "S"}, "shadowPlane": {"obj": "S"}}):
            with self.subTest(ghost=ghost), self.assertRaises(ValueError):
                self.call("parse_ghost_config", ghost)

    def test_screen_heights_must_be_set_and_fall(self):
        self.call("lod_level_row", self.settings, 2).screen_height = 0.0
        self.assertIn("LOD2 needs a Show Above Screen Height value", self.validate()[0])
        self.call("lod_level_row", self.settings, 2).screen_height = 30.0
        self.assertIn("LOD2 must show above a smaller screen height than LOD1", self.validate()[0])

    def test_ghost_level_must_export_and_ghost_shadows_sit_on_the_car_root(self):
        self.call("lod_level_row", self.settings, 1).export = False
        self.assertIsNone(self.call("ghost_lod_position", self.settings))
        self.assertIn("Ghost Default LOD1 is not an exported level", self.validate()[0])
        self.call("lod_level_row", self.settings, 1).export = True
        self.shadow.parent = self.spin
        self.api["car_reference_objects"] = lambda settings: {self.spin}
        self.assertIn("Ghost shadow must be under Car Root, outside wheels and other moving nodes", self.validate()[0])
        # A group under Car Root holds the shadows like Car Root itself.
        shadows = self.obj("shadows", self.root, mesh=False)
        self.shadow.parent = shadows
        self.assertEqual([e for e in self.validate()[0] if "Ghost shadow must" in e], [])
        self.shadow.parent = self.root
        self.settings.ghost_shadow_plane_object = None
        self.assertIn("LODs need the ghost Shadow and Shadow Plane; use their refresh buttons", self.validate()[0])

    def test_lods_on_without_lod_meshes_is_an_error(self):
        for name in [name for name in self.objects if "_LOD" in name]:
            self.scene_objects.remove(self.objects[name])
        self.assertIn("LODs is on but no mesh under Car Root is named <part>_LOD<n>", self.validate()[0])


if __name__ == "__main__":
    unittest.main()
