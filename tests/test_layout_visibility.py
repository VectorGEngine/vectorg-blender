"""Layout show/hide in the track exporter; run with blender --background --factory-startup --python this_file -- --blender."""
from pathlib import Path
import sys
import unittest


ADDON = Path(__file__).resolve().parents[1] / "addons/vectorg_track_exporter/__init__.py"


def blender_tests():
    import bpy

    sys.path.insert(0, str(ADDON.parent.parent))
    import vectorg_track_exporter as addon
    addon.register()

    class LayoutVisibilityTests(unittest.TestCase):
        def test_show_hide_changes_viewport_visibility_only(self):
            bpy.ops.track_exporter.create_configuration()
            bpy.ops.track_exporter.add_layout()
            layout = bpy.context.scene.track_exporter.layouts[0]
            visuals = addon.object_with_role(layout.root_object, addon.ROLE_VISUALS)
            bpy.ops.mesh.primitive_cube_add()
            rendered = bpy.context.object
            rendered.parent = visuals
            bpy.ops.mesh.primitive_cube_add()
            not_rendered = bpy.context.object
            not_rendered.parent = visuals
            not_rendered.hide_render = True
            objects = [layout.root_object, *addon.descendants(layout.root_object)]

            layout.visible = False
            self.assertTrue(all(obj.hide_get() for obj in objects))
            layout.visible = True
            self.assertFalse(any(obj.hide_get() for obj in objects))
            self.assertFalse(rendered.hide_render)
            self.assertTrue(not_rendered.hide_render)
            self.assertFalse(any(obj.hide_render for obj in objects if obj != not_rendered))

    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(LayoutVisibilityTests)
    )
    if not result.wasSuccessful():
        raise RuntimeError("Blender layout visibility tests failed")


if __name__ == "__main__":
    if "--blender" in sys.argv:
        blender_tests()
