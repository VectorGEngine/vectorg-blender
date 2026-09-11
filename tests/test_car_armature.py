"""Armature joint mapping and rest-attachment export tests; no Blender required."""
import ast
import copy
import math
from pathlib import Path
from types import SimpleNamespace
import unittest


ADDON = Path(__file__).resolve().parents[1] / "addons/vectorg_car_exporter/__init__.py"
FUNCTIONS = {
    "find_object", "object_config_name", "set_object_pointer", "is_object_in_tree",
    "hierarchy_objects", "excluded_ghost_objects", "car_export_objects",
    "armature_object_poll", "armature_attachment_object", "armature_attachment_offset",
    "parse_armature_config", "blender_position_to_game",
    "build_armature_config", "import_armature_config", "validate_armature_scene",
    "gltf_armature_export_options", "export_car_glb", "build_manifest",
    "draw_armature",
}


class Vector:
    def __init__(self, values):
        self.x, self.y, self.z = values

    def __sub__(self, other):
        return Vector((self.x - other.x, self.y - other.y, self.z - other.z))

    @property
    def length(self):
        return math.sqrt(self.x ** 2 + self.y ** 2 + self.z ** 2)


class Matrix:
    """Small affine test double; exercises exporter calls without Blender's mathutils."""
    def __init__(self, angle=0, position=(0, 0, 0), scale=(1, 1, 1)):
        c, s = math.cos(angle), math.sin(angle)
        self.rows = [[c * scale[0], -s * scale[1], 0, position[0]],
                     [s * scale[0], c * scale[1], 0, position[1]],
                     [0, 0, scale[2], position[2]], [0, 0, 0, 1]]

    def __matmul__(self, point):
        vector = (point.x, point.y, point.z, 1)
        return Vector(tuple(sum(row[i] * vector[i] for i in range(4)) for row in self.rows[:3]))

    def inverted(self):
        rows = [row[:] + [int(i == j) for j in range(4)] for i, row in enumerate(self.rows)]
        for column in range(4):
            pivot = max(range(column, 4), key=lambda i: abs(rows[i][column]))
            if abs(rows[pivot][column]) < 1e-12:
                raise ValueError("singular")
            rows[column], rows[pivot] = rows[pivot], rows[column]
            divisor = rows[column][column]
            rows[column] = [v / divisor for v in rows[column]]
            for i in range(4):
                if i != column:
                    factor = rows[i][column]
                    rows[i] = [v - factor * p for v, p in zip(rows[i], rows[column])]
        result = Matrix()
        result.rows = [row[4:] for row in rows]
        return result


class JointCollection(list):
    def add(self):
        joint = SimpleNamespace(bone="", base_attachment="root", tip_attachment="none", stretch=False)
        self.append(joint)
        return joint

    def remove(self, index):
        del self[index]


class Object:
    def __init__(self, name, parent=None, kind="EMPTY"):
        self.name, self.parent, self.type = name, parent, kind
        self.children = []
        self.matrix_world = Matrix()
        self.data = SimpleNamespace(bones={})
        if parent:
            parent.children.append(self)


class ArmatureTests(unittest.TestCase):
    def setUp(self):
        self.objects, self.scene_objects = {}, []
        self.api = {
            "Path": Path, "math": math, "Operator": object, "IntProperty": lambda: None,
            "scene_settings": lambda context: self.settings,
            "bpy": SimpleNamespace(
                data=SimpleNamespace(objects=self.objects),
                context=SimpleNamespace(scene=SimpleNamespace(objects=self.scene_objects)),
            ),
            "guide_objects": lambda: [], "downforce_helper_objects": lambda: [],
        }
        selected = []
        for node in ast.parse(ADDON.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS:
                selected.append(node)
            elif isinstance(node, ast.ClassDef) and node.name in {
                "CAR_EXPORTER_OT_add_armature_joint", "CAR_EXPORTER_OT_remove_armature_joint",
            }:
                selected.append(node)
            elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                if node.targets[0].id in {
                    "WHEEL_KEYS", "WHEEL_LABELS", "GHOST_WHEEL_ROLES", "BLENDER_AXIS_TO_GAME",
                    "ARMATURE_ATTACHMENT_ROLES", "ARMATURE_ATTACHMENT_ITEMS", "ARMATURE_TIP_ITEMS",
                }:
                    selected.append(node)
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(ADDON), "exec"), self.api)
        self.root = self.obj("CarRoot")
        self.rig = self.obj("SuspensionRig", self.root, "ARMATURE")
        self.geometry = self.obj("SuspensionGeometry", self.rig, "MESH")
        self.settings = SimpleNamespace(
            car_root_object=self.root, armature_enabled=True, armature_object=self.rig,
            ghost_enabled=False, ghost_root_object=None, wheels=[], armature_joints=JointCollection(),
        )
        for group, key, _steering in self.api["WHEEL_KEYS"]:
            mount = self.obj(f"mount_{group}_{key}", self.root)
            joint = self.obj(f"joint_{group}_{key}", mount)
            spin = self.obj(f"spin_{group}_{key}", joint)
            self.settings.wheels.append(SimpleNamespace(
                group=group, key=key, suspension_ref=mount, hub_ref=joint, wheel_ref=spin,
            ))
            bone_name = f"hub_{group}_{key}"
            self.rig.data.bones[bone_name] = SimpleNamespace(
                name=bone_name, use_connect=False, head_local=Vector((.3, .1, .8)),
                tail_local=Vector((.8, .1, .5)),
            )
            mapping = self.settings.armature_joints.add()
            mapping.bone = bone_name
            mapping.base_attachment = f"joint_{group[0]}{key}"

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

    def test_round_trip_and_follow_only_contract(self):
        config = self.config()
        self.assertEqual(config["obj"], "SuspensionRig")
        self.assertEqual(config["joints"][0], {
            "bone": "hub_front_l", "base": "joint_fl", "tip": None, "stretch": False,
            "tipOffset": None,
        })
        self.assertEqual(set(config), {"obj", "joints"})
        parsed = self.call("parse_armature_config", config)
        self.settings.armature_object = None
        self.call("import_armature_config", self.settings, parsed)
        self.assertEqual(self.config(), config)
        self.assertEqual(self.validate(), [])

    def test_attachment_enums_cover_root_and_every_existing_wheel_role(self):
        expected = ["root"] + [f"{role}_{corner}" for role in ("mount", "joint", "spin")
                               for corner in ("fl", "fr", "rl", "rr")]
        self.assertEqual([item[0] for item in self.api["ARMATURE_ATTACHMENT_ITEMS"]], expected)
        self.assertEqual([item[0] for item in self.api["ARMATURE_TIP_ITEMS"]], ["none"] + expected)
        for role in expected:
            for tip in (False, True):
                mapping = self.settings.armature_joints[0]
                mapping.base_attachment = "root" if tip else role
                mapping.tip_attachment = role if tip else "none"
                self.assertEqual(self.config()["joints"][0]["tip" if tip else "base"], role)

    def test_aim_and_stretch_preserve_head_tail_offsets_under_parent_transforms(self):
        self.root.matrix_world = Matrix(.3, (2, 3, 4), (2, 2, 2))
        self.rig.matrix_world = Matrix(-.5, (-2, 1, 3), (1.5, 1.5, 1.5))
        tip = self.settings.wheels[0].hub_ref
        tip.matrix_world = Matrix(.8, (5, 6, -2), (.5, 1.2, 2))
        mapping = self.settings.armature_joints[0]
        mapping.base_attachment, mapping.tip_attachment = "root", "joint_fl"
        bone = self.rig.data.bones[mapping.bone]
        for stretch in (False, True):
            mapping.stretch = stretch
            exported = self.config()["joints"][0]
            self.assertEqual(exported["stretch"], stretch)
            # Convert exported GLB-local positions back to Blender local points.
            def world_point(obj, offset):
                return obj.matrix_world @ Vector((offset[0], -offset[2], offset[1]))
            self.assertNotIn("baseOffset", exported)
            tail = world_point(tip, exported["tipOffset"])
            self.assertLess((tail - self.rig.matrix_world @ bone.tail_local).length, 1e-10)
        config = self.config()
        self.call("import_armature_config", self.settings, self.call("parse_armature_config", config))
        self.assertEqual(self.config(), config)

    def test_disabled_tip_suppresses_stretch_without_losing_ui_selection(self):
        mapping = self.settings.armature_joints[0]
        mapping.stretch = True
        self.assertFalse(self.config()["joints"][0]["stretch"])
        self.assertTrue(mapping.stretch)
        mapping.tip_attachment = "spin_fl"
        self.assertTrue(self.config()["joints"][0]["stretch"])

    def test_partial_import_replaces_list_and_disable_preserves_it(self):
        config = self.config()
        config["joints"] = config["joints"][:1]
        self.call("import_armature_config", self.settings, self.call("parse_armature_config", config))
        self.assertEqual(len(self.settings.armature_joints), 1)
        self.assertEqual(self.config(), config)
        self.call("import_armature_config", self.settings, None)
        self.assertIsNone(self.config())
        self.assertEqual(len(self.settings.armature_joints), 1)
        self.assertIn(self.rig, self.call("car_export_objects", self.settings))
        self.assertIn(self.geometry, self.call("car_export_objects", self.settings))
        self.settings.armature_enabled = True
        self.assertEqual(self.config(), config)

    def test_empty_mapping_is_rejected_only_when_enabled(self):
        self.settings.armature_joints.clear()
        self.assertIn("at least one", " ".join(self.validate()))
        self.settings.armature_enabled = False
        self.assertEqual(self.validate(), [])

    def test_malformed_manifest_and_old_wheel_schema_rejected(self):
        invalid = [False, True, [], "rig", {}, {"obj": "rig", "wheels": {}},
                   {"obj": "rig", "joints": []}, {"obj": "rig", "joints": {}},
                   {"obj": " ", "joints": self.config()["joints"]}]
        for field, values in {
            "bone": ["", None, 5], "base": ["none", "joint_xx", [], None],
            "tip": ["none", False, {}, ""], "stretch": [None, 1, "true"],
            "tipOffset": [[0, 0, 0]],
        }.items():
            for value in values:
                config = self.config()
                config["joints"][0][field] = value
                invalid.append(config)
        config = self.config()
        config["joints"][0]["stretch"] = True
        invalid.append(config)
        config = self.config()
        config["joints"][0]["tip"] = "joint_fl"
        invalid.append(config)
        config = self.config()
        config["extra"] = True
        invalid.append(config)
        config = self.config()
        config["joints"][0]["baseOffset"] = [0, 0, 0]
        invalid.append(config)
        for offset in (None, [], [0, 0], [0, False, 0], [0, float("nan"), 0]):
            config = self.config()
            config["joints"][0]["tip"] = "joint_fl"
            config["joints"][0]["tipOffset"] = offset
            invalid.append(config)
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.call("parse_armature_config", value)

    def test_duplicate_missing_connected_and_zero_length_bones_rejected(self):
        mapping = self.settings.armature_joints[0]
        self.settings.armature_joints[1].bone = mapping.bone
        self.assertIn("more than once", " ".join(self.validate()))
        self.settings.armature_joints[1].bone = "hub_front_r"
        mapping.bone = "missing"
        self.assertIn("missing from", " ".join(self.validate()))
        mapping.bone = "hub_front_l"
        bone = self.rig.data.bones[mapping.bone]
        bone.use_connect = True
        self.assertIn("Connected disabled", " ".join(self.validate()))
        bone.use_connect = False
        bone.tail_local = bone.head_local
        self.assertIn("rest length", " ".join(self.validate()))

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
        for enabled in (False, True):
            self.settings.ghost_enabled = enabled
            self.assertIn("outside the custom ghost", " ".join(self.validate()))

    def test_rig_and_wheel_parenting_cannot_feed_back(self):
        joint = self.settings.wheels[0].hub_ref
        self.rig.parent = joint
        self.assertIn("independent hierarchies", " ".join(self.validate()))
        self.rig.parent = self.root
        joint.parent = self.rig
        self.assertIn("independent hierarchies", " ".join(self.validate()))

    def test_missing_attachment_and_singular_transform_rejected(self):
        self.scene_objects.remove(self.settings.wheels[0].hub_ref)
        self.assertIn("exported car object", " ".join(self.validate()))
        self.scene_objects.append(self.settings.wheels[0].hub_ref)
        self.settings.wheels[0].hub_ref.matrix_world = Matrix(scale=(0, 1, 1))
        self.assertIn("singular", " ".join(self.validate()))
        self.settings.armature_joints[0].base_attachment = "invalid"
        self.assertIn("Unknown armature attachment", " ".join(self.validate()))

    def test_add_remove_operators_preserve_other_entries(self):
        added = self.api["CAR_EXPORTER_OT_add_armature_joint"]()
        removed = self.api["CAR_EXPORTER_OT_remove_armature_joint"]()
        context = SimpleNamespace()
        before = self.config()
        self.assertEqual(added.execute(context), {"FINISHED"})
        self.assertEqual(len(self.settings.armature_joints), 5)
        self.assertEqual(self.settings.armature_joints[-1].tip_attachment, "none")
        removed.index = 4
        self.assertEqual(removed.execute(context), {"FINISHED"})
        self.assertEqual(self.config(), before)
        removed.index = 99
        self.assertEqual(removed.execute(context), {"CANCELLED"})

    def test_stretch_control_is_only_drawn_with_a_tip(self):
        drawn = []
        class Layout:
            def row(self, **kwargs): return self
            def column(self, **kwargs): return self
            def box(self): return self
            def label(self, **kwargs): pass
            def separator(self, **kwargs): pass
            def prop(self, obj, prop, **kwargs): drawn.append(prop)
            def prop_search(self, obj, prop, *args): drawn.append(prop)
            def operator(self, *args, **kwargs): return SimpleNamespace()
        self.api["draw_split_prop"] = lambda layout, obj, prop: drawn.append(prop)
        self.call("draw_armature", Layout(), self.settings)
        self.assertNotIn("stretch", drawn)
        self.settings.armature_joints[0].tip_attachment = "joint_fl"
        drawn.clear()
        self.call("draw_armature", Layout(), self.settings)
        self.assertEqual(drawn.count("stretch"), 1)

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
