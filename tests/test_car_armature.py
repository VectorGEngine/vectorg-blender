"""Suspension armature manifest and export contract tests; no Blender required."""
import ast
import copy
from pathlib import Path
from types import SimpleNamespace
import unittest


ADDON = Path(__file__).resolve().parents[1] / "addons/vectorg_car_exporter/__init__.py"
FUNCTIONS = {
    "find_object", "object_config_name", "set_object_pointer", "is_object_in_tree",
    "hierarchy_objects", "excluded_ghost_objects", "car_export_objects",
    "armature_object_poll", "armature_bone_property", "parse_armature_config",
    "build_armature_config", "import_armature_config", "validate_armature_scene",
    "gltf_armature_export_options", "export_car_glb", "build_manifest",
}


class Object:
    def __init__(self, name, parent=None, kind="EMPTY"):
        self.name = name
        self.parent = parent
        self.type = kind
        self.children = []
        self.data = SimpleNamespace(bones={})
        if parent:
            parent.children.append(self)


class ArmatureTests(unittest.TestCase):
    def setUp(self):
        self.objects = {}
        self.scene_objects = []
        self.api = {
            "Path": Path,
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
                if node.targets[0].id in {"WHEEL_KEYS", "WHEEL_LABELS", "GHOST_WHEEL_ROLES", "BLENDER_AXIS_TO_GAME"}:
                    selected.append(node)
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(ADDON), "exec"), self.api)
        self.root = self.obj("CarRoot")
        self.rig = self.obj("SuspensionRig", self.root, "ARMATURE")
        self.geometry = self.obj("SuspensionGeometry", self.rig, "MESH")
        self.settings = SimpleNamespace(
            car_root_object=self.root, armature_enabled=True, armature_object=self.rig,
            ghost_enabled=False, ghost_root_object=None, wheels=[],
        )
        for group, key, _steering in self.api["WHEEL_KEYS"]:
            mount = self.obj(f"mount_{group}_{key}", self.root)
            joint = self.obj(f"joint_{group}_{key}", mount)
            spin = self.obj(f"spin_{group}_{key}", joint)
            self.settings.wheels.append(SimpleNamespace(
                group=group, key=key, suspension_ref=mount, hub_ref=joint, wheel_ref=spin,
            ))
            bone_name = f"hub_{group}_{key}"
            self.rig.data.bones[bone_name] = SimpleNamespace(name=bone_name, use_connect=False)
            setattr(self.settings, self.call("armature_bone_property", group, key), bone_name)

    def obj(self, *args):
        obj = Object(*args)
        self.objects[obj.name] = obj
        self.scene_objects.append(obj)
        return obj

    def call(self, name, *args):
        return self.api[name](*args)

    def config(self):
        return self.call("build_armature_config", self.settings)

    def validate(self):
        errors, warnings = [], []
        self.call("validate_armature_scene", self.settings, errors, warnings)
        return errors

    def test_round_trip_has_no_duplicate_joint_references(self):
        config = self.config()
        self.assertEqual(config, {
            "obj": "SuspensionRig",
            "wheels": {
                "front": {"l": {"bone": "hub_front_l"}, "r": {"bone": "hub_front_r"}},
                "rear": {"l": {"bone": "hub_rear_l"}, "r": {"bone": "hub_rear_r"}},
            },
        })
        parsed = self.call("parse_armature_config", config)
        self.settings.armature_object = None
        self.settings.armature_enabled = False
        self.call("import_armature_config", self.settings, parsed)
        self.assertEqual(self.config(), config)
        self.assertEqual(self.validate(), [])

    def test_partial_import_clears_old_mappings(self):
        config = {"obj": "SuspensionRig", "wheels": {"front": {"l": {"bone": "hub_front_l"}}}}
        self.call("import_armature_config", self.settings, self.call("parse_armature_config", config))
        self.assertEqual(self.config(), config)
        self.assertEqual(self.settings.armature_rear_r_bone, "")
        self.assertEqual(self.validate(), [])

    def test_disable_preserves_selections_and_exported_geometry(self):
        before = self.config()
        self.call("import_armature_config", self.settings, None)
        self.assertFalse(self.settings.armature_enabled)
        self.assertIsNone(self.config())
        self.assertIn(self.rig, self.call("car_export_objects", self.settings))
        self.assertIn(self.geometry, self.call("car_export_objects", self.settings))
        self.assertEqual(self.validate(), [])
        self.settings.armature_enabled = True
        self.assertEqual(self.config(), before)

    def test_empty_mapping_is_rejected_only_when_enabled(self):
        for group, key, _steering in self.api["WHEEL_KEYS"]:
            setattr(self.settings, self.call("armature_bone_property", group, key), "")
        self.assertIn("at least one", " ".join(self.validate()))
        self.settings.armature_enabled = False
        self.assertEqual(self.validate(), [])

    def test_malformed_and_unknown_manifest_fields_are_rejected(self):
        invalid = [False, True, [], "rig", {}, {"obj": " "},
                   {"obj": "rig", "wheels": []}, {"obj": "rig", "wheels": {}},
                   {"obj": "rig", "wheels": {"left": {}}}]
        for mapping in [None, [], "bone", {}, {"bone": ""}, {"bone": " "},
                        {"bone": 5}, {"bone": "hub", "obj": "joint"}]:
            invalid.append({"obj": "rig", "wheels": {"front": {"l": mapping}}})
        for axle in [None, [], "front", {"left": {"bone": "hub"}}]:
            invalid.append({"obj": "rig", "wheels": {"front": axle}})
        unknown = self.config()
        unknown["enabled"] = True
        invalid.append(unknown)
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.call("parse_armature_config", value)

    def test_duplicate_and_missing_bones_are_rejected(self):
        self.settings.armature_rear_r_bone = self.settings.armature_front_l_bone
        self.assertIn("multiple wheels", " ".join(self.validate()))
        self.settings.armature_rear_r_bone = "missing"
        self.assertIn("missing from", " ".join(self.validate()))

    def test_missing_wrong_type_and_external_armature(self):
        self.settings.armature_object = None
        self.assertIn("required", " ".join(self.validate()))
        self.settings.armature_object = self.geometry
        self.assertIn("must be an Armature", " ".join(self.validate()))
        self.settings.armature_object = self.rig
        self.rig.parent = None
        self.assertIn("exported car hierarchy", " ".join(self.validate()))
        self.rig.parent = self.root
        self.scene_objects.remove(self.rig)
        self.assertIn("exported car hierarchy", " ".join(self.validate()))

    def test_rig_must_not_be_car_root(self):
        self.settings.car_root_object = self.rig
        self.assertIn("below Car Root", " ".join(self.validate()))

    def test_ghost_rig_rejected_whether_ghost_enabled_or_disabled(self):
        ghost = self.obj("Ghost", self.root)
        self.settings.ghost_root_object = ghost
        self.rig.parent = ghost
        ghost.children.append(self.rig)
        for enabled in [False, True]:
            self.settings.ghost_enabled = enabled
            self.assertIn("outside the custom ghost", " ".join(self.validate()))

    def test_rig_and_wheel_parenting_cannot_feed_back(self):
        joint = self.settings.wheels[0].hub_ref
        self.rig.parent = joint
        self.assertIn("independent hierarchies", " ".join(self.validate()))
        self.rig.parent = self.root
        joint.parent = self.rig
        self.assertIn("independent hierarchies", " ".join(self.validate()))

    def test_connected_bone_and_missing_joint_rejected(self):
        self.rig.data.bones["hub_front_l"].use_connect = True
        self.assertIn("Connected disabled", " ".join(self.validate()))
        self.rig.data.bones["hub_front_l"].use_connect = False
        self.scene_objects.remove(self.settings.wheels[0].hub_ref)
        self.assertIn("exported wheel Joint", " ".join(self.validate()))

    def test_armature_picker_filters_objects(self):
        self.assertTrue(self.call("armature_object_poll", self.settings, self.rig))
        self.assertFalse(self.call("armature_object_poll", self.settings, self.geometry))

    def test_glb_export_passes_skin_options_and_restores_resources_on_failure(self):
        calls, cleaned = [], []
        self.api.update({
            "scene_settings": lambda context: self.settings,
            "create_body_material_export_carrier": lambda context: "carrier",
            "remove_body_material_export_carrier": lambda carrier: cleaned.append(carrier),
            "apply_export_texture_optimization": lambda *args: (["node"], ["image"]),
            "restore_export_textures": lambda nodes, images: cleaned.append((nodes, images)),
            "gltf_image_export_options": lambda quality: {"export_image_quality": quality},
        })
        def exporting(**options):
            calls.append(options)
            return {"CANCELLED"} if len(calls) == 2 else {"FINISHED"}
        self.api["bpy"].ops = SimpleNamespace(export_scene=SimpleNamespace(gltf=exporting))
        context = SimpleNamespace(scene=SimpleNamespace(objects=self.scene_objects))
        self.call("export_car_glb", context, Path("car.glb"), 2048, True, 90)
        self.assertTrue(calls[0]["export_skins"])
        self.assertTrue(calls[0]["export_rest_position_armature"])
        for key in ["export_def_bones", "export_armature_object_remove", "export_animations",
                    "export_hierarchy_flatten_bones", "export_hierarchy_flatten_objs"]:
            self.assertFalse(calls[0][key])
        self.assertTrue(calls[0]["export_apply"])
        self.assertEqual(calls[0]["export_image_quality"], 90)
        with self.assertRaisesRegex(RuntimeError, "did not finish"):
            self.call("export_car_glb", context, Path("car.glb"), 2048, True, 90)
        self.assertEqual(cleaned, [(["node"], ["image"]), "carrier"] * 2)
        self.settings.armature_enabled = False
        self.call("export_car_glb", context, Path("car.glb"), 2048, True, 90)
        self.assertNotIn("export_animations", calls[-1])

    def test_manifest_omits_disabled_section_and_keeps_existing_wheels(self):
        settings = self.settings
        defaults = dict(
            sound_pitch_offset=0, use_custom_sounds=False, headlights_material=None,
            brake_lights_material=None, reverse_lights_material=None,
            dashboard_screen_object=None, center_of_mass_object=None, colliders=[],
            down_force_points=[], air_drag=1, body_colors=[], car_id="test",
            package_version="test-rig", display_name="Test", car_class="test",
            vehicle_tag_tarmac=True, vehicle_tag_offroad=False, hp=100, drive="rwd",
            max_rpm=8000, idle_rpm=800, redline_rpm=7000, rev_limit=7500,
            engine_inertia=1, engine_braking=0.2, engine_friction_torque=1, clutch_response=1,
            shift_cooldown=0.1, auto_blip=False, auto_blip_duration=0.1,
            torque_factor=1, turbo_enabled=False, turbo_boost=0, abs_max_level=5,
            esc_max_level=5, traction_control_max_level=5,
            steering_wheel_object=None, steering_wheel_spin_axis="y",
            chase_camera_object=None, cockpit_camera_object=None,
            hood_camera_object=None, roof_camera_object=None,
        )
        for key, value in defaults.items():
            setattr(settings, key, value)
        existing_wheels = {"front": {"l": {"joint": {"obj": "joint_front_l"}}}}
        self.api.update({
            "build_ghost_config": lambda settings: None,
            "sample_torque_curve": lambda settings: [],
            "build_wheels_config": lambda settings: copy.deepcopy(existing_wheels),
            "build_presets_config": lambda settings: [],
            "camera_fov": lambda *args: 60,
        })
        enabled = self.call("build_manifest", settings)
        self.assertEqual(enabled["armature"], self.config())
        self.assertEqual(enabled["version"], 8)
        self.assertEqual(enabled["packageVersion"], "test-rig")
        self.assertEqual(enabled["wheels"], existing_wheels)
        settings.armature_enabled = False
        disabled = self.call("build_manifest", settings)
        self.assertNotIn("armature", disabled)
        del enabled["armature"]
        self.assertEqual(enabled, disabled)


if __name__ == "__main__":
    unittest.main()
