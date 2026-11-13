"""Eyes kept through the car export; run with blender --background --factory-startup --python this_file -- --blender."""
from pathlib import Path
import sys
import unittest


ADDON = Path(__file__).resolve().parents[1] / "addons/vectorg_car_exporter/__init__.py"


def blender_tests():
    import bpy

    sys.path.insert(0, str(ADDON.parent.parent))
    import vectorg_car_exporter as addon

    class ExportVisibilityTests(unittest.TestCase):
        def test_objects_kept_out_of_an_export_keep_their_eye(self):
            parent = bpy.data.objects.new("Parent", None)
            bpy.context.scene.collection.objects.link(parent)
            objects = []
            for name in ("Hidden_LOD1", "Shown_LOD0"):
                obj = bpy.data.objects.new(name, bpy.data.meshes.new(name))
                bpy.context.scene.collection.objects.link(obj)
                obj.parent = parent
                objects.append(obj)
            hidden, shown = objects
            hidden.hide_set(True)

            states = addon.export_exclusions(objects)
            addon.unlink_export_helpers(states)
            bpy.context.view_layer.update()
            self.assertNotIn(hidden.name, bpy.context.view_layer.objects)
            addon.relink_export_helpers(states)

            self.assertTrue(hidden.hide_get())
            self.assertFalse(shown.hide_get())
            self.assertEqual(hidden.parent, parent)

    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(ExportVisibilityTests)
    )
    if not result.wasSuccessful():
        raise RuntimeError("Car export visibility tests failed")


if __name__ == "__main__":
    if "--blender" in sys.argv:
        blender_tests()
