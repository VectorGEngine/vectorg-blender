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
            and node.targets[0].id == "CAST_SHADOW_PROPERTY")
    ]
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(ADDON), "exec"), namespace)
    return namespace


class FakeObject(dict):
    def __init__(self, name, type_, visible_shadow):
        super().__init__()
        self.name, self.type, self.visible_shadow = name, type_, visible_shadow


class ShadowCastingExportTests(unittest.TestCase):
    def test_meshes_with_shadow_visibility_off_are_tagged_only_during_export(self):
        road = FakeObject("road", "MESH", False)
        wall = FakeObject("wall", "MESH", True)
        empty = FakeObject("visuals", "EMPTY", False)
        bpy = types.SimpleNamespace(data=types.SimpleNamespace(objects={"road": road, "wall": wall, "visuals": empty}))
        ns = addon_functions({"bpy": bpy}, "apply_shadow_casting_export_flags", "restore_shadow_casting_export_flags")

        tagged = ns["apply_shadow_casting_export_flags"]([road, wall, empty])
        self.assertEqual(tagged, [road])
        self.assertIs(road["vectorg_cast_shadow"], False)
        self.assertNotIn("vectorg_cast_shadow", wall)
        self.assertNotIn("vectorg_cast_shadow", empty)

        ns["restore_shadow_casting_export_flags"](tagged)
        self.assertNotIn("vectorg_cast_shadow", road)


if __name__ == "__main__":
    unittest.main()
