"""Exercise exporter hierarchy logic without Blender; not a GLB runtime test."""
import ast
import re
import unittest
from pathlib import Path
from types import SimpleNamespace


ADDON = Path(__file__).resolve().parents[1] / "addons/vectorg_track_exporter/__init__.py"
FUNCTIONS = {
    "scene_settings", "descendants", "hierarchy_descendants", "direct_child_with_role",
    "descendants_with_role", "object_with_role", "create_empty", "create_collision_hierarchy",
    "create_visual_hierarchy", "layout_root_name", "indexed_name_value", "ordered_objects",
    "layout_named_objects", "layout_nodes", "apply_generated_names", "sync_layout_node_names",
    "create_layout_hierarchy", "find_surface_group", "ensure_surface_group",
    "refresh_track_structure", "valid_id",
}


class Object(dict):
    __hash__ = object.__hash__
    __eq__ = object.__eq__

    def __init__(self, name):
        super().__init__()
        self.name = name
        self.children = []
        self._parent = None

    def __bool__(self):
        return True

    @property
    def parent(self):
        return self._parent

    @parent.setter
    def parent(self, value):
        if self._parent:
            self._parent.children.remove(self)
        self._parent = value
        if value:
            value.children.append(self)


class Objects(list):
    def new(self, name, _data):
        result = Object(name)
        self.append(result)
        return result

    def get(self, name):
        return next((obj for obj in self if obj.name == name), None)


class SurfaceRefreshTests(unittest.TestCase):
    def setUp(self):
        self.objects = Objects()
        namespace = {"re": re, "bpy": SimpleNamespace(data=SimpleNamespace(objects=self.objects))}
        tree = ast.parse(ADDON.read_text(encoding="utf-8"))
        selected = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS:
                selected.append(node)
            elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
                if name.startswith("ROLE_") or name in {
                    "SURFACE_IDS", "SURFACE_PROPERTY", "SHAPE_PROPERTY", "EVENT_PROPERTY",
                    "ORDER_PROPERTY", "ID_PATTERN",
                }:
                    selected.append(node)
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(ADDON), "exec"), namespace)
        self.api = SimpleNamespace(**namespace)
        self.namespace = namespace
        self.settings = SimpleNamespace(layouts=[])
        self.context = SimpleNamespace(scene=SimpleNamespace(
            track_exporter=self.settings,
            collection=SimpleNamespace(objects=SimpleNamespace(link=lambda _obj: None)),
        ))
        self.surface_ids = self.api.SURFACE_IDS
        namespace["SURFACE_IDS"] = tuple(surface for surface in self.surface_ids if not surface.startswith("wet_"))
        shared = self.api.create_empty(self.context, "SHARED", role="shared")
        self.settings.shared_root_object = shared
        self.shared_collisions = self.api.create_collision_hierarchy(self.context, shared, "SHARED")
        for layout_id in ["gp", "sprint"]:
            root, *_rest = self.api.create_layout_hierarchy(self.context, None, layout_id)
            layout = Object(layout_id)
            layout.layout_id = layout_id
            layout.root_object = root
            layout.map_curve = None
            self.settings.layouts.append(layout)
        namespace["SURFACE_IDS"] = self.surface_ids

    def collision_roots(self):
        return [self.shared_collisions, *[
            self.api.direct_child_with_role(layout.root_object, "collisions")
            for layout in self.settings.layouts
        ]]

    def test_refresh_adds_surfaces_to_shared_and_every_layout_without_duplicates(self):
        original = list(self.objects)
        tarmac = self.api.find_surface_group(self.shared_collisions, "tarmac")
        geometry = self.objects.new("existing_geometry", None)
        geometry.parent = tarmac
        geometry.location = (1, 2, 3)
        self.assertTrue(self.api.refresh_track_structure(self.context)[0])
        self.assertEqual(len(self.objects), len(original) + 1 + 9)
        for collisions in self.collision_roots():
            groups = [child for child in collisions.children if child.get("vectorg_role") == "surface"]
            self.assertEqual(sorted(group["vectorg_surface"] for group in groups), sorted(self.surface_ids))
            for group in groups:
                self.assertEqual(group.name, f"{collisions.name}_{group['vectorg_surface']}")
        first_refresh = list(self.objects)
        self.assertTrue(self.api.refresh_track_structure(self.context)[0])
        self.assertEqual(self.objects, first_refresh)
        self.assertTrue(all(obj in self.objects for obj in original))
        self.assertIs(geometry.parent, tarmac)
        self.assertEqual(geometry.location, (1, 2, 3))

    def test_refresh_synchronizes_existing_and_new_layout_surface_names(self):
        layout = self.settings.layouts[1]
        layout.layout_id = "renamed"
        self.assertTrue(self.api.refresh_track_structure(self.context)[0])
        self.assertEqual(layout.root_object.name, "layout_renamed")
        collisions = self.collision_roots()[2]
        self.assertEqual(collisions.name, "renamed_COLLISIONS")
        self.assertEqual(self.api.find_surface_group(collisions, "wet_tarmac").name, "renamed_COLLISIONS_wet_tarmac")

    def test_missing_roots_and_name_conflicts_fail_before_creating_groups(self):
        self.settings.layouts[1].root_object = None
        before = list(self.objects)
        self.assertFalse(self.api.refresh_track_structure(self.context)[0])
        self.assertEqual(self.objects, before)

    def test_conflicting_surface_name_is_not_overwritten(self):
        conflict = self.objects.new("SHARED_COLLISIONS_wet_tarmac", None)
        before = list(self.objects)
        success, message = self.api.refresh_track_structure(self.context)
        self.assertFalse(success)
        self.assertIn(conflict.name, message)
        self.assertEqual(self.objects, before)

    def test_duplicate_layout_ids_fail_before_changing_structure(self):
        self.settings.layouts[1].layout_id = self.settings.layouts[0].layout_id
        before = list(self.objects)
        self.assertFalse(self.api.refresh_track_structure(self.context)[0])
        self.assertEqual(self.objects, before)


if __name__ == "__main__":
    unittest.main()
