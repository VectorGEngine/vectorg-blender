"""Shadow-casting export flag tests (no Blender needed)."""
import ast
import types
import unittest
from pathlib import Path


ADDON = Path(__file__).resolve().parents[1] / "addons/vectorg_track_exporter/__init__.py"


def addon_functions(namespace, *names):
    tree = ast.parse(ADDON.read_text(encoding="utf-8"))
    selected = [
        node for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name in names)
        or (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
            and (node.targets[0].id in ("CAST_SHADOW_PROPERTY", "ROLE_PROPERTY", "ROLE_DECALS")))
    ]
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(ADDON), "exec"), namespace)
    return namespace


class FakeObject(dict):
    __hash__ = object.__hash__
    __eq__ = object.__eq__

    def __init__(self, name, type_, visible_shadow, children=()):
        super().__init__()
        self.name, self.type, self.visible_shadow = name, type_, visible_shadow
        self.children = list(children)


class ShadowCastingExportTests(unittest.TestCase):
    def test_meshes_with_shadow_visibility_off_and_decals_are_tagged_only_during_export(self):
        road = FakeObject("road", "MESH", False)
        wall = FakeObject("wall", "MESH", True)
        empty = FakeObject("visuals", "EMPTY", False)
        skid_mark = FakeObject("skid_mark", "MESH", True)
        decals = FakeObject("DECALS", "EMPTY", True, [skid_mark])
        decals["vectorg_role"] = "decals"
        objects = [road, wall, empty, decals, skid_mark]
        bpy = types.SimpleNamespace(data=types.SimpleNamespace(objects={obj.name: obj for obj in objects}))
        ns = addon_functions(
            {"bpy": bpy}, "apply_shadow_casting_export_flags", "restore_shadow_casting_export_flags", "descendants",
        )

        tagged = ns["apply_shadow_casting_export_flags"](objects)
        self.assertEqual(tagged, [road, skid_mark], "a decal casts nothing even with its Shadow on")
        self.assertIs(road["vectorg_cast_shadow"], False)
        self.assertIs(skid_mark["vectorg_cast_shadow"], False)
        self.assertNotIn("vectorg_cast_shadow", wall)
        self.assertNotIn("vectorg_cast_shadow", empty)

        ns["restore_shadow_casting_export_flags"](tagged)
        self.assertNotIn("vectorg_cast_shadow", road)
        self.assertNotIn("vectorg_cast_shadow", skid_mark)


if __name__ == "__main__":
    unittest.main()
