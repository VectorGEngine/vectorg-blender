bl_info = {
    "name": "VectorG Car Exporter",
    "author": "VectorG",
    "version": (0, 8, 2),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar > VectorG",
    "description": "Export VectorG vehicle packages as <car_id>.glb + manifest.json + audio zip",
    "category": "Import-Export",
}

import json
import math
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

import bpy
from bpy_extras.io_utils import ExportHelper
from bpy.app.handlers import persistent
from mathutils import Vector
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import Operator, Panel, PropertyGroup


WHEEL_KEYS = (
    ("front", "l", True),
    ("front", "r", True),
    ("rear", "l", False),
    ("rear", "r", False),
)

WHEEL_LABELS = {
    ("front", "l"): "Front Left Wheel",
    ("front", "r"): "Front Right Wheel",
    ("rear", "l"): "Rear Left Wheel",
    ("rear", "r"): "Rear Right Wheel",
}

ARMATURE_ATTACHMENT_ROLES = {
    "root": None,
    **{f"{role}_{group[0]}{key}": (group, key, prop)
       for role, prop in (("mount", "suspension_ref"), ("joint", "hub_ref"), ("spin", "wheel_ref"))
       for group, key, _steering in WHEEL_KEYS},
}
ARMATURE_ATTACHMENT_ITEMS = tuple(
    (role, role, "Use the existing car object assignment") for role in ARMATURE_ATTACHMENT_ROLES
)
ARMATURE_TIP_ITEMS = (("none", "None", "Follow Base Attachment without aiming"),) + ARMATURE_ATTACHMENT_ITEMS

TIRE_TYPE_ITEMS = (
    ("soft", "Soft", "Highest configured dry-road tire grip"),
    ("medium", "Medium", "Balanced configured tire grip"),
    ("hard", "Hard", "Lowest configured dry-road tire grip"),
    ("wet", "Wet", "Tire grip optimized for wet roads"),
    ("snow", "Snow", "Studless winter tire grip optimized for snow and ice"),
)
TIRE_TYPES = frozenset(item[0] for item in TIRE_TYPE_ITEMS)

SOUND_SLOTS = {
    "tranny_on": {"label": "Transmission On", "default": "trany_power_high.wav", "rpm": 0, "loop": True, "volume": 0.6},
    "tranny_off": {"label": "Transmission Off", "default": "tw_offlow_4.wav", "rpm": 0, "loop": True, "volume": 0.1},
    "on_high": {"label": "On High", "default": "BAC_Mono_onhigh.wav", "rpm": 1000, "loop": True, "volume": 0.5},
    "on_low": {"label": "On Low", "default": "BAC_Mono_onlow.wav", "rpm": 1000, "loop": True, "volume": 0.4},
    "off_high": {"label": "Off High", "default": "BAC_Mono_offveryhigh.wav", "rpm": 1000, "loop": True, "volume": 0.3},
    "off_low": {"label": "Off Low", "default": "BAC_Mono_offlow.wav", "rpm": 1000, "loop": True, "volume": 0.3},
    "limiter": {"label": "Limiter", "default": "limiter.wav", "rpm": 8000, "loop": True, "volume": 0.4},
    "turbo": {"label": "Turbo", "default": "turbo_flutter.wav", "rpm": 8000, "loop": False, "volume": 0.6},
}
SOUND_RPM_SLOTS = {"on_high", "on_low", "off_high", "off_low"}

ORIENTATION_DOT_THRESHOLD = math.cos(math.radians(1.0))
STEERING_WHEEL_DOT_THRESHOLD = math.cos(math.radians(45.0))
TORQUE_CURVE_NODE_GROUP = "_CarExporterTorqueCurve"
TORQUE_CURVE_NODE = "Torque Curve"
CAMERA_PREFIXES = ("chase", "cockpit", "hood", "roof")
GUIDE_PREFIX = "CAR_EXPORTER_GUIDE_"
GUIDE_PROP = "car_exporter_helper"
DOWNFORCE_HELPER_PROP = "vectorg_downforce_helper"
NEWTONS_PER_KILOGRAM = 9.81
BRAKE_LOCK_MARGIN = 1.15
CENTER_OF_MASS_HELPER_PROP = "vectorg_center_of_mass_helper"
PACKAGE_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9._+-]{1,64}$")
DEFAULT_MAX_TEXTURE_SIZE = 4096
DEFAULT_JPEG_QUALITY = 85
TEMP_IMAGE_FILE_PROPERTY = "vectorg_temp_file"
TEXTURE_SIZE_ITEMS = (
    ("1024", "1024", "Cap exported car textures to 1024 px on their longest side"),
    ("2048", "2048", "Cap exported car textures to 2048 px on their longest side"),
    ("4096", "4096", "Cap exported car textures to 4096 px on their longest side"),
)
AXIS_ITEMS = (
    ("x", "X", ""),
    ("y", "Y", ""),
    ("z", "Z", ""),
    ("-x", "-X", ""),
    ("-y", "-Y", ""),
    ("-z", "-Z", ""),
)
BLENDER_AXIS_TO_GAME = {
    "x": [1, 0, 0],
    "-x": [-1, 0, 0],
    "y": [0, 0, 1],
    "-y": [0, 0, -1],
    "z": [0, 1, 0],
    "-z": [0, -1, 0],
}
GAME_AXIS_TO_BLENDER = {tuple(value): key for key, value in BLENDER_AXIS_TO_GAME.items()}
BLENDER_AXIS_LOCAL = {
    "x": (1, 0, 0),
    "-x": (-1, 0, 0),
    "y": (0, 1, 0),
    "-y": (0, -1, 0),
    "z": (0, 0, 1),
    "-z": (0, 0, -1),
}

def scene_settings(context):
    return context.scene.car_exporter


def find_object(name):
    return bpy.data.objects.get(name) if name else None


def object_config_name(obj):
    return obj.name if obj else ""


def set_object_pointer(data, prop_name, object_name):
    setattr(data, prop_name, find_object(object_name))


def set_material_pointer(data, prop_name, material_name):
    setattr(data, prop_name, bpy.data.materials.get(material_name) if material_name else None)


def object_axis(obj, local_axis):
    return (obj.matrix_world.to_quaternion() @ Vector(local_axis)).normalized()


def dot_axis(obj, local_axis, world_axis):
    return object_axis(obj, local_axis).dot(Vector(world_axis).normalized())


def abspath(path):
    return bpy.path.abspath(path) if path else ""


def relative_to_car(car_obj, obj):
    if not car_obj or not obj:
        return None
    return car_obj.matrix_world.inverted() @ obj.matrix_world.translation


def blender_position_to_game(position):
    return [position.x, position.z, -position.y]


def game_position_to_blender(position):
    return Vector((position[0], -position[2], position[1]))


def next_helper_name(prefix):
    index = 1
    while bpy.data.objects.get(f"{prefix}_{index:02d}"):
        index += 1
    return f"{prefix}_{index:02d}"


def downforce_point_display_name(point, index):
    return point.display_name.strip() or f"Downforce {index + 1}"


def next_downforce_point_display_name(settings):
    existing_names = {
        downforce_point_display_name(point, index).casefold()
        for index, point in enumerate(settings.down_force_points)
    }
    index = 1
    while f"downforce {index}" in existing_names:
        index += 1
    return f"Downforce {index}"


def calculate_max_speed_brake_force_kg(settings, preset):
    total_mass = sum(collider.mass for collider in settings.colliders)
    if total_mass <= 0.0:
        raise ValueError("Collider mass must be greater than zero")
    if not settings.car_root_object or not settings.center_of_mass_object:
        raise ValueError("Car Root and Center of Mass are required")

    # Use Blender world axes (Z up, -Y forward), including parent transforms.
    # Root-local axes can differ when the car root has unapplied rotation/scale.
    shared_wheels = {(wheel.group, wheel.key): wheel for wheel in settings.wheels}
    wheel_positions = {}
    contact_heights = []
    for group, key, _steering in WHEEL_KEYS:
        wheel = shared_wheels.get((group, key))
        if not wheel or not wheel.suspension_ref or not wheel.wheel_ref:
            raise ValueError("All wheel mounts and spin objects are required")
        mount_position = blender_position_to_game(
            wheel.suspension_ref.matrix_world.translation
        )
        spin_position = blender_position_to_game(
            wheel.wheel_ref.matrix_world.translation
        )
        wheel_positions[(group, key)] = mount_position
        contact_heights.append(spin_position[1] - wheel.radius)

    front_z = sum(wheel_positions[("front", key)][2] for key in ("l", "r")) / 2.0
    rear_z = sum(wheel_positions[("rear", key)][2] for key in ("l", "r")) / 2.0
    axle_span = front_z - rear_z
    wheelbase = abs(axle_span)
    if wheelbase <= 1.0e-4:
        raise ValueError("Front and rear wheel mounts must define a wheelbase")

    center_of_mass = blender_position_to_game(
        settings.center_of_mass_object.matrix_world.translation
    )
    front_static_fraction = min(max((center_of_mass[2] - rear_z) / axle_span, 0.0), 1.0)
    center_of_mass_height = max(center_of_mass[1] - sum(contact_heights) / len(contact_heights), 0.0)

    front_load = total_mass * front_static_fraction
    rear_load = total_mass * (1.0 - front_static_fraction)
    for point in settings.down_force_points:
        if not point.object_ref:
            raise ValueError("Every downforce point must have a helper object")
        position = blender_position_to_game(
            point.object_ref.matrix_world.translation
        )
        front_fraction = (position[2] - rear_z) / axle_span
        force_kg = point.max_force / NEWTONS_PER_KILOGRAM
        front_load += force_kg * front_fraction
        rear_load += force_kg * (1.0 - front_fraction)

    front_grip = 1.0
    rear_grip = 1.0
    denominator = total_mass - total_mass * center_of_mass_height / wheelbase * (front_grip - rear_grip)
    if denominator <= 1.0e-4:
        raise ValueError("Vehicle geometry and tire grip produce an invalid brake-force estimate")
    deceleration_g = (front_grip * front_load + rear_grip * rear_load) / denominator
    load_transfer = total_mass * deceleration_g * center_of_mass_height / wheelbase
    front_load += load_transfer
    rear_load -= load_transfer
    if front_load <= 0.0 or rear_load <= 0.0:
        raise ValueError("Predicted braking unloads an axle; adjust the vehicle setup")
    if preset.brake_bias <= 0.0 or preset.brake_bias >= 1.0:
        raise ValueError("Brake Bias must be greater than 0 and less than 1")

    front_force_kg = front_load * 0.5 * front_grip / preset.brake_bias * BRAKE_LOCK_MARGIN
    rear_force_kg = rear_load * 0.5 * rear_grip / (1.0 - preset.brake_bias) * BRAKE_LOCK_MARGIN
    return front_force_kg, rear_force_kg, deceleration_g


def create_car_helper(context, settings, name, display_type, display_size, helper_prop):
    car_obj = settings.car_root_object
    if not car_obj:
        return None
    helper = bpy.data.objects.new(name, None)
    helper.empty_display_type = display_type
    helper.empty_display_size = display_size
    helper.show_in_front = True
    helper.hide_render = True
    helper[helper_prop] = True
    link_collection = car_obj.users_collection[0] if car_obj.users_collection else context.scene.collection
    link_collection.objects.link(helper)
    helper.parent = car_obj
    helper.matrix_parent_inverse.identity()
    helper.location = car_obj.matrix_world.inverted() @ context.scene.cursor.location
    helper.lock_rotation = (True, True, True)
    helper.lock_scale = (True, True, True)
    return helper


def object_world_bounds_size(obj):
    if not obj:
        return None
    if obj.type == "MESH" and obj.data and obj.data.vertices:
        points = [vertex.co for vertex in obj.data.vertices]
    else:
        points = [Vector(corner) for corner in obj.bound_box]
    min_corner = Vector((
        min(point.x for point in points),
        min(point.y for point in points),
        min(point.z for point in points),
    ))
    max_corner = Vector((
        max(point.x for point in points),
        max(point.y for point in points),
        max(point.z for point in points),
    ))
    local_size = max_corner - min_corner
    scale = obj.matrix_world.to_scale()
    return Vector((
        abs(local_size.x * scale.x),
        abs(local_size.y * scale.y),
        abs(local_size.z * scale.z),
    ))


def default_collider_mass(obj):
    size = object_world_bounds_size(obj)
    if not size:
        return 0.0
    volume = max(size.x, 0.01) * max(size.y, 0.01) * max(size.z, 0.01)
    return round(max(1.0, volume * 150.0), 2)


def update_collider_object(self, _context):
    if self.object_ref:
        self.mass = default_collider_mass(self.object_ref)
    else:
        self.collider_type = "trimesh"
        self.mass = 0.0


def is_object_in_tree(root_obj, obj):
    if not root_obj or not obj:
        return False
    current = obj
    while current:
        if current == root_obj:
            return True
        current = current.parent
    return False


def hierarchy_objects(root_obj):
    objects = []
    pending = [root_obj] if root_obj else []
    while pending:
        obj = pending.pop()
        objects.append(obj)
        pending.extend(reversed(obj.children))
    return objects


GHOST_WHEEL_ROLES = (("mount", "suspension_ref"), ("joint", "hub_ref"), ("spin", "wheel_ref"))


def ghost_wheel_settings(settings, group, key):
    return getattr(settings, f"ghost_{group}_{key}")


def excluded_ghost_objects(settings):
    return hierarchy_objects(settings.ghost_root_object) if not settings.ghost_enabled else []


def car_export_objects(settings):
    excluded = set(guide_objects() + downforce_helper_objects() + excluded_ghost_objects(settings))
    return [obj for obj in bpy.context.scene.objects if obj not in excluded]


def armature_object_poll(_settings, obj):
    return obj.type == "ARMATURE"


def armature_attachment_object(settings, role):
    if role not in ARMATURE_ATTACHMENT_ROLES:
        raise ValueError(f"Unknown armature attachment: {role}")
    assignment = ARMATURE_ATTACHMENT_ROLES[role]
    if assignment is None:
        obj = settings.car_root_object
    else:
        group, key, prop = assignment
        wheel = next((wheel for wheel in settings.wheels if (wheel.group, wheel.key) == (group, key)), None)
        obj = getattr(wheel, prop) if wheel else None
    if not obj or obj not in car_export_objects(settings) or not is_object_in_tree(settings.car_root_object, obj):
        raise ValueError(f"Armature attachment {role} requires an exported car object assignment")
    if is_object_in_tree(settings.ghost_root_object, obj):
        raise ValueError(f"Armature attachment {role} must be outside the custom ghost hierarchy")
    return obj


def armature_attachment_offset(obj, world_position):
    try:
        position = obj.matrix_world.inverted() @ world_position
    except ValueError as error:
        raise ValueError(f"Armature attachment {obj.name} has a singular transform") from error
    # Position conversion matches GLB's coordinate basis, not the axis-selector labels.
    return blender_position_to_game(position)


def parse_armature_config(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"obj", "joints"}:
        raise ValueError("Manifest armature must contain obj and joints; recreate old wheel mappings with Add Joint")
    name = value.get("obj")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Manifest armature.obj must be a non-empty string")
    joints = value["joints"]
    if not isinstance(joints, list) or not joints:
        raise ValueError("Armature requires at least one joint mapping")
    result = {"obj": name, "joints": []}
    used_bones = set()
    for index, mapping in enumerate(joints):
        path = f"armature.joints[{index}]"
        if not isinstance(mapping, dict) or set(mapping) != {"bone", "base", "tip", "stretch", "tipOffset"}:
            raise ValueError(f"Manifest {path} must contain bone, base, tip, stretch and tipOffset")
        bone = mapping["bone"]
        if not isinstance(bone, str) or not bone.strip():
            raise ValueError(f"Manifest {path}.bone must be a non-empty string")
        if bone in used_bones:
            raise ValueError(f"Armature bone is assigned more than once: {bone}")
        used_bones.add(bone)
        for field in ("base", "tip"):
            role = mapping[field]
            if field == "tip" and role is None:
                continue
            if not isinstance(role, str) or role not in ARMATURE_ATTACHMENT_ROLES:
                raise ValueError(f"Manifest {path}.{field} must select an attachment role")
        if not isinstance(mapping["stretch"], bool):
            raise ValueError(f"Manifest {path}.stretch must be a boolean")
        if mapping["tip"] is None and (mapping["stretch"] or mapping["tipOffset"] is not None):
            raise ValueError(f"Manifest {path} without a tip must have stretch false and tipOffset null")
        if mapping["tip"] is not None:
            vector = mapping["tipOffset"]
            if (not isinstance(vector, list) or len(vector) != 3
                    or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector)):
                raise ValueError(f"Manifest {path}.tipOffset must be a finite three-component position")
        result["joints"].append({**mapping, "tipOffset": list(mapping["tipOffset"]) if mapping["tip"] is not None else None})
    return result


def build_armature_config(settings):
    if not settings.armature_enabled:
        return None
    rig = settings.armature_object
    if not rig or rig.type != "ARMATURE":
        raise ValueError("Armature object is required and must be an Armature")
    joints = []
    for mapping in settings.armature_joints:
        bone = rig.data.bones.get(mapping.bone)
        if bone is None:
            raise ValueError(f"Joint bone is missing from {rig.name}: {mapping.bone or '(unassigned)'}")
        if bone.use_connect:
            raise ValueError(f"Joint bone {bone.name} must have Connected disabled so it can translate")
        # Bone rest coordinates are independent of pose-mode animation and constraints.
        head = rig.matrix_world @ bone.head_local
        tail = rig.matrix_world @ bone.tail_local
        length = (tail - head).length
        if not math.isfinite(length) or length <= 1e-6:
            raise ValueError(f"Joint bone {bone.name} must have a non-zero finite rest length")
        base = armature_attachment_object(settings, mapping.base_attachment)
        # Validate the base transform; its bone-relative offset is derived from GLB rest transforms at runtime.
        armature_attachment_offset(base, head)
        tip_role = mapping.tip_attachment if mapping.tip_attachment != "none" else None
        tip = armature_attachment_object(settings, tip_role) if tip_role else None
        joints.append({
            "bone": bone.name, "base": mapping.base_attachment, "tip": tip_role,
            "stretch": bool(mapping.stretch) if tip else False,
            "tipOffset": armature_attachment_offset(tip, tail) if tip else None,
        })
    return parse_armature_config({"obj": object_config_name(rig), "joints": joints})


def import_armature_config(settings, config):
    settings.armature_enabled = config is not None
    # Disabling retains authoring selections; importing an enabled mapping replaces them.
    if config is None:
        return
    set_object_pointer(settings, "armature_object", config["obj"])
    settings.armature_joints.clear()
    for mapping in config["joints"]:
        joint = settings.armature_joints.add()
        joint.bone = mapping["bone"]
        joint.base_attachment = mapping["base"]
        joint.tip_attachment = mapping["tip"] or "none"
        joint.stretch = mapping["stretch"]


def validate_armature_scene(settings, errors, warnings):
    if not settings.armature_enabled:
        return
    rig = settings.armature_object
    if not rig or rig.type != "ARMATURE":
        errors.append("Armature object is required and must be an Armature")
        return
    exported_objects = set(car_export_objects(settings))
    if rig not in exported_objects or rig == settings.car_root_object or not is_object_in_tree(settings.car_root_object, rig):
        errors.append("Armature must be inside the exported car hierarchy below Car Root")
    if is_object_in_tree(settings.ghost_root_object, rig):
        errors.append("Armature must be outside the custom ghost hierarchy")
    # Sources and rig must not drive each other through object/bone parenting.
    for wheel in settings.wheels:
        for _role, prop in GHOST_WHEEL_ROLES:
            obj = getattr(wheel, prop)
            if obj and (is_object_in_tree(obj, rig) or is_object_in_tree(rig, obj)):
                errors.append("Armature and normal wheel objects must use independent hierarchies")
                break
    try:
        build_armature_config(settings)
    except ValueError as error:
        errors.append(str(error))
        return


def gltf_armature_export_options(settings):
    if not settings.armature_enabled:
        return {}
    return {
        "export_skins": True,
        "export_rest_position_armature": True,
        "export_def_bones": False,
        "export_armature_object_remove": False,
        "export_hierarchy_flatten_bones": False,
        "export_hierarchy_flatten_objs": False,
        # Runtime wheel poses drive this rig; do not export Blender actions/drivers.
        "export_animations": False,
    }


def build_ghost_config(settings):
    if not settings.ghost_enabled:
        return None
    wheels = {"front": {}, "rear": {}}
    for group, key, _steering in WHEEL_KEYS:
        wheel = ghost_wheel_settings(settings, group, key)
        wheels[group][key] = {
            role: {"obj": object_config_name(getattr(wheel, prop))}
            for role, prop in GHOST_WHEEL_ROLES
        }
    return {"obj": object_config_name(settings.ghost_root_object), "wheels": wheels}


def parse_ghost_config(value):
    if value is None:
        return None

    def node_name(record, path):
        name = record.get("obj") if isinstance(record, dict) else None
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"Manifest {path}.obj must be a non-empty string")
        return name

    root_name = node_name(value, "ghost")
    wheels = value.get("wheels")
    if not isinstance(wheels, dict):
        raise ValueError("Manifest ghost.wheels must be an object")
    result = {"obj": root_name, "wheels": {"front": {}, "rear": {}}}
    used_names = {root_name}
    for group, key, _steering in WHEEL_KEYS:
        axle = wheels.get(group)
        wheel = axle.get(key) if isinstance(axle, dict) else None
        if not isinstance(wheel, dict):
            raise ValueError(f"Manifest ghost.wheels.{group}.{key} must be an object")
        nodes = {}
        for role, _prop in GHOST_WHEEL_ROLES:
            name = node_name(wheel.get(role), f"ghost.wheels.{group}.{key}.{role}")
            nodes[role] = {"obj": name}
        # A wheel may share its own joint/spin node, as normal wheels can.
        names = {node["obj"] for node in nodes.values()}
        if names & used_names:
            raise ValueError("Manifest ghost wheel nodes must not overlap the root or another wheel")
        used_names.update(names)
        result["wheels"][group][key] = nodes
    return result


def import_ghost_config(settings, config):
    settings.ghost_enabled = config is not None
    # Preserve disabled authoring selections so the subtree can still be excluded.
    if config is None:
        return
    set_object_pointer(settings, "ghost_root_object", config["obj"])
    for group, key, _steering in WHEEL_KEYS:
        wheel = ghost_wheel_settings(settings, group, key)
        for role, prop in GHOST_WHEEL_ROLES:
            set_object_pointer(wheel, prop, config["wheels"][group][key][role]["obj"])


def validate_ghost_scene(settings, errors, warnings):
    root = settings.ghost_root_object
    if not root:
        if settings.ghost_enabled:
            errors.append("Custom ghost root object is required")
        return
    if not settings.car_root_object or root.parent != settings.car_root_object:
        errors.append("Ghost root must be a direct child of the car root")
    ghost_objects = set(hierarchy_objects(root))
    scene_objects = set(bpy.context.scene.objects)
    if root not in scene_objects:
        errors.append("Ghost root must belong to the current scene")
    normal_refs = [settings.center_of_mass_object, settings.steering_wheel_object,
                   settings.dashboard_screen_object]
    normal_refs.extend(getattr(settings, f"{prefix}_camera_object") for prefix in CAMERA_PREFIXES)
    normal_refs.extend(collider.object_ref for collider in settings.colliders)
    normal_refs.extend(point.object_ref for point in settings.down_force_points)
    normal_refs.extend(getattr(wheel, prop) for wheel in settings.wheels for _role, prop in GHOST_WHEEL_ROLES)
    if any(obj in ghost_objects for obj in normal_refs if obj):
        errors.append("Ghost hierarchy must not contain normal car wheel, camera, dashboard or physics references")
    if not settings.ghost_enabled:
        return
    if not any(obj.type == "MESH" and obj.data.polygons for obj in ghost_objects):
        errors.append("Ghost hierarchy must contain mesh geometry")
    used_nodes = {root}
    mounts = []
    for group, key, _steering in WHEEL_KEYS:
        label = f"Ghost {WHEEL_LABELS[(group, key)]}"
        wheel = ghost_wheel_settings(settings, group, key)
        nodes = [getattr(wheel, prop) for _role, prop in GHOST_WHEEL_ROLES]
        for (role, _prop), obj in zip(GHOST_WHEEL_ROLES, nodes):
            if not obj or obj not in ghost_objects or obj not in scene_objects:
                errors.append(f"{label} {role} must be inside the exported ghost hierarchy")
        unique_nodes = {obj for obj in nodes if obj}
        if unique_nodes & used_nodes:
            errors.append(f"{label} nodes must not overlap the ghost root or another wheel")
        used_nodes.update(unique_nodes)
        mount, joint, spin = nodes
        if mount and joint and not is_object_in_tree(mount, joint):
            errors.append(f"{label} joint must be inside mount hierarchy")
        if joint and spin and not is_object_in_tree(joint, spin):
            errors.append(f"{label} spin must be inside joint hierarchy")
        if mount:
            if any(is_object_in_tree(other, mount) or is_object_in_tree(mount, other) for other in mounts):
                errors.append("Ghost wheel mount hierarchies must not overlap")
            mounts.append(mount)
        if spin and not any(obj.type == "MESH" and obj.data.polygons for obj in hierarchy_objects(spin)):
            errors.append(f"{label} spin hierarchy must contain mesh geometry")
    if settings.body_colors:
        material = settings.body_colors[0].material
        if material and not material_is_assigned_to_geometry(material, ghost_objects):
            errors.append("Default body color material must be assigned to ghost geometry")
    for label, prop in (("Headlights", "headlights_material"), ("Brake lights", "brake_lights_material"),
                        ("Reverse lights", "reverse_lights_material")):
        material = getattr(settings, prop)
        if material and not material_is_assigned_to_geometry(material, ghost_objects):
            warnings.append(f"{label} material is not assigned to ghost geometry; custom ghost will not display this light")


def objects_with_unapplied_scale(root_obj):
    return [
        obj
        for obj in hierarchy_objects(root_obj)
        if any(abs(component - 1.0) > 1e-6 for component in obj.scale)
    ]


def apply_car_hierarchy_scales(context, root_obj):
    objects = hierarchy_objects(root_obj)
    if not objects_with_unapplied_scale(root_obj):
        return

    unavailable = [obj.name for obj in objects if obj.name not in context.view_layer.objects]
    if unavailable:
        raise RuntimeError(
            "Cannot apply scale because car hierarchy objects are excluded from the active view layer: "
            + ", ".join(unavailable)
        )

    previous_active = context.view_layer.objects.active
    previous_mode = previous_active.mode if previous_active else "OBJECT"
    try:
        if previous_active and previous_mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        with context.temp_override(
            object=root_obj,
            active_object=root_obj,
            selected_objects=objects,
            selected_editable_objects=objects,
        ):
            result = bpy.ops.object.transform_apply(
                location=False,
                rotation=False,
                scale=True,
                isolate_users=True,
            )
        if "FINISHED" not in result:
            raise RuntimeError("Blender could not apply scale to the car hierarchy")
    finally:
        context.view_layer.objects.active = previous_active
        if previous_active and previous_mode != "OBJECT":
            with context.temp_override(
                object=previous_active,
                active_object=previous_active,
            ):
                bpy.ops.object.mode_set(mode=previous_mode)

    unscaled = [
        obj.name
        for obj in objects
        if any(abs(component - 1.0) > 1e-6 for component in obj.scale)
    ]
    if unscaled:
        raise RuntimeError("Scale was not applied to car hierarchy objects: " + ", ".join(unscaled))


def validate_object_in_car_tree(errors, car_obj, label, obj):
    if not car_obj:
        return
    if obj and obj != car_obj and not is_object_in_tree(car_obj, obj):
        errors.append(f"{label} must be inside car root hierarchy")


def camera_object_poll(_self, obj):
    return obj.type == "CAMERA"


def camera_fov(settings, prefix):
    camera_obj = getattr(settings, f"{prefix}_camera_object")
    if camera_obj and camera_obj.type == "CAMERA" and camera_obj.data:
        camera = camera_obj.data
        render = bpy.context.scene.render
        render_width = render.resolution_x * render.pixel_aspect_x
        render_height = render.resolution_y * render.pixel_aspect_y
        aspect_ratio = render_width / render_height
        vertical_fit = camera.sensor_fit == "VERTICAL" or (
            camera.sensor_fit == "AUTO" and aspect_ratio < 1.0
        )
        vertical_angle = camera.angle if vertical_fit else 2.0 * math.atan(
            math.tan(camera.angle * 0.5) / aspect_ratio
        )
        return math.degrees(vertical_angle)
    return getattr(settings, f"{prefix}_fov")


def camera_target_name(camera_obj):
    return f"{camera_obj.name}_target"


def camera_target_child(camera_obj):
    if not camera_obj:
        return None
    target_name = camera_target_name(camera_obj)
    for child in camera_obj.children:
        if child.type == "EMPTY" and child.name == target_name:
            return child
    return None


def position_camera_target(settings, prefix, target_obj):
    distance = getattr(settings, f"{prefix}_target_distance")
    target_obj.location = (0.0, 0.0, -distance)
    target_obj.rotation_euler = (0.0, 0.0, 0.0)
    target_obj.scale = (1.0, 1.0, 1.0)


def create_camera_target_on_selection(settings, prefix):
    camera_obj = getattr(settings, f"{prefix}_camera_object")
    if not camera_obj or camera_obj.type != "CAMERA":
        return

    target_obj = camera_target_child(camera_obj)
    if target_obj is None:
        target_obj = bpy.data.objects.new(camera_target_name(camera_obj), None)
        target_obj.empty_display_type = "PLAIN_AXES"
        target_obj.empty_display_size = 0.25
        link_collection = camera_obj.users_collection[0] if camera_obj.users_collection else bpy.context.scene.collection
        link_collection.objects.link(target_obj)
        target_obj.parent = camera_obj
        target_obj.matrix_parent_inverse.identity()

    position_camera_target(settings, prefix, target_obj)


def update_existing_camera_target(settings, prefix):
    camera_obj = getattr(settings, f"{prefix}_camera_object")
    target_obj = camera_target_child(camera_obj)
    if target_obj:
        position_camera_target(settings, prefix, target_obj)


def update_chase_camera_object(settings, _context):
    create_camera_target_on_selection(settings, "chase")


def update_cockpit_camera_object(settings, _context):
    create_camera_target_on_selection(settings, "cockpit")


def update_hood_camera_object(settings, _context):
    create_camera_target_on_selection(settings, "hood")


def update_roof_camera_object(settings, _context):
    create_camera_target_on_selection(settings, "roof")


def update_chase_target_distance(settings, _context):
    update_existing_camera_target(settings, "chase")


def update_cockpit_target_distance(settings, _context):
    update_existing_camera_target(settings, "cockpit")


def update_hood_target_distance(settings, _context):
    update_existing_camera_target(settings, "hood")


def update_roof_target_distance(settings, _context):
    update_existing_camera_target(settings, "roof")


def update_driver_assist_max_levels(settings, _context):
    for preset in settings.presets:
        preset.abs_level = min(preset.abs_level, settings.abs_max_level)
        preset.esc_level = min(preset.esc_level, settings.esc_max_level)
        preset.traction_control_level = min(
            preset.traction_control_level,
            settings.traction_control_max_level,
        )


def ensure_camera_targets(settings):
    for prefix in CAMERA_PREFIXES:
        create_camera_target_on_selection(settings, prefix)


def guide_objects():
    return [
        obj
        for obj in bpy.data.objects
        if obj.get(GUIDE_PROP) or obj.name.startswith(GUIDE_PREFIX)
    ]


def downforce_helper_objects():
    return [obj for obj in bpy.data.objects if obj.get(DOWNFORCE_HELPER_PROP)]


def remove_size_guide():
    for obj in guide_objects():
        data = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if data and data.users == 0:
            if isinstance(data, bpy.types.Curve):
                bpy.data.curves.remove(data)
            elif isinstance(data, bpy.types.Mesh):
                bpy.data.meshes.remove(data)


def guide_material(name, color):
    material = bpy.data.materials.get(name)
    if material is None:
        material = bpy.data.materials.new(name)
    material.diffuse_color = color
    return material


def create_guide_curve(name, splines, material, bevel_depth=0.015):
    curve = bpy.data.curves.new(name, "CURVE")
    curve.dimensions = "3D"
    curve.resolution_u = 1
    curve.bevel_depth = bevel_depth
    curve.bevel_resolution = 2
    for points in splines:
        spline = curve.splines.new("POLY")
        spline.points.add(len(points) - 1)
        for point, co in zip(spline.points, points):
            point.co = (co[0], co[1], co[2], 1.0)

    obj = bpy.data.objects.new(name, curve)
    obj[GUIDE_PROP] = True
    obj.show_in_front = True
    obj.hide_render = True
    obj.data.materials.append(material)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def create_size_guide(settings):
    remove_size_guide()
    line_material = guide_material(f"{GUIDE_PREFIX}Lines", (0.0, 0.85, 1.0, 1.0))

    length = settings.guide_length
    width = settings.guide_width
    wheelbase = settings.guide_wheelbase
    track_width = settings.guide_track_width
    z = 0.02
    half_l = length * 0.5
    half_w = width * 0.5
    front_y = -half_l
    rear_y = half_l
    front_axle_y = -wheelbase * 0.5
    rear_axle_y = wheelbase * 0.5
    half_track = track_width * 0.5
    wheel_size = 0.35

    splines = [
        [(-half_w, front_y, z), (half_w, front_y, z), (half_w, rear_y, z), (-half_w, rear_y, z), (-half_w, front_y, z)],
        [(0.0, front_y - 0.45, z), (-0.35, front_y, z), (0.35, front_y, z), (0.0, front_y - 0.45, z)],
        [(0.0, front_y, z), (0.0, rear_y, z)],
    ]

    for _name, x, y in (
        ("Wheel_FL", half_track, front_axle_y),
        ("Wheel_FR", -half_track, front_axle_y),
        ("Wheel_RL", half_track, rear_axle_y),
        ("Wheel_RR", -half_track, rear_axle_y),
    ):
        half = wheel_size * 0.5
        splines.append([
            (x - half, y - half, z),
            (x + half, y - half, z),
            (x + half, y + half, z),
            (x - half, y + half, z),
            (x - half, y - half, z),
        ])

    create_guide_curve(
        f"{GUIDE_PREFIX}SizeGuide",
        splines,
        line_material,
    )


def with_helpers_unlinked(callback, excluded_objects=()):
    helpers = list(dict.fromkeys(guide_objects() + downforce_helper_objects() + list(excluded_objects)))
    states = [(obj, list(obj.users_collection)) for obj in helpers]
    try:
        for obj, collections in states:
            for collection in collections:
                collection.objects.unlink(obj)
        return callback()
    finally:
        for obj, collections in states:
            if obj.name not in bpy.data.objects:
                continue
            for collection in collections:
                if obj.name not in collection.objects.keys():
                    collection.objects.link(obj)


def node_trees(root_tree):
    trees = []
    seen = set()
    pending = [root_tree] if root_tree else []
    while pending:
        tree = pending.pop()
        if tree in seen:
            continue
        seen.add(tree)
        trees.append(tree)
        for node in tree.nodes:
            if node.bl_idname == "ShaderNodeGroup" and node.node_tree:
                pending.append(node.node_tree)
    return trees


def material_texture_nodes(material):
    if not material or not material.use_nodes or not material.node_tree:
        return []
    return [
        node
        for tree in node_trees(material.node_tree)
        for node in tree.nodes
        if node.bl_idname == "ShaderNodeTexImage" and node.image
    ]


def export_materials(objects, additional_materials=()):
    materials = []
    seen = set()
    for obj in objects:
        if obj.type != "MESH":
            continue
        for slot in obj.material_slots:
            material = slot.material
            if material and material.name not in seen:
                seen.add(material.name)
                materials.append(material)
    for material in additional_materials:
        if material and material.name not in seen:
            seen.add(material.name)
            materials.append(material)
    return materials


def object_with_assigned_material(material, objects):
    if not material:
        return None
    for obj in objects:
        if obj.type != "MESH":
            continue
        for polygon in obj.data.polygons:
            if polygon.material_index >= len(obj.material_slots):
                continue
            if obj.material_slots[polygon.material_index].material == material:
                return obj
    return None


def material_is_assigned_to_geometry(material, objects):
    return object_with_assigned_material(material, objects) is not None


def upstream_texture_nodes(socket, visited=None):
    if not socket or not socket.is_linked:
        return set()
    if visited is None:
        visited = set()
    result = set()
    for link in socket.links:
        node = link.from_node
        if node in visited:
            continue
        visited.add(node)
        if node.bl_idname == "ShaderNodeTexImage" and node.image:
            result.add(node)
            continue
        for input_socket in node.inputs:
            result.update(upstream_texture_nodes(input_socket, visited))
    return result


def classify_texture_usage(objects, additional_materials=()):
    usage_by_image = {}
    for material in export_materials(objects, additional_materials):
        texture_nodes = material_texture_nodes(material)
        classified_nodes = set()
        for node in texture_nodes:
            usage_by_image.setdefault(node.image, set())
        for tree in node_trees(material.node_tree):
            for node in tree.nodes:
                if node.bl_idname != "ShaderNodeBsdfPrincipled":
                    continue
                for socket in node.inputs:
                    if not socket.is_linked:
                        continue
                    if socket.name in {"Base Color", "Emission", "Emission Color"}:
                        usage = "color"
                    elif socket.name == "Alpha":
                        usage = "alpha"
                    else:
                        usage = "data"
                    for texture_node in upstream_texture_nodes(socket):
                        usage_by_image.setdefault(texture_node.image, set()).add(usage)
                        classified_nodes.add(texture_node)
        for node in texture_nodes:
            if node not in classified_nodes:
                usage_by_image[node.image].add("ambiguous")
    return usage_by_image


def alpha_material_warnings(objects, additional_materials=()):
    warnings = []
    for material in export_materials(objects, additional_materials):
        has_linked_alpha = any(
            socket.name == "Alpha" and socket.is_linked
            for tree in node_trees(material.node_tree)
            for node in tree.nodes
            if node.bl_idname == "ShaderNodeBsdfPrincipled"
            for socket in node.inputs
        )
        if has_linked_alpha and getattr(material, "blend_method", None) == "OPAQUE":
            warnings.append(
                f"Material {material.name} has a linked Alpha input but uses Opaque blend mode"
            )
    return warnings


def image_source_path(image):
    return Path(bpy.path.abspath(image.filepath, library=image.library)) if image else None


def image_source_exists(image):
    if image.packed_file or image.source != "FILE":
        return True
    source = image_source_path(image)
    return bool(source and source.is_file())


def texture_validation(objects, max_size, additional_materials=()):
    errors = []
    warnings = []
    for image, usages in classify_texture_usage(objects, additional_materials).items():
        width, height = image.size
        if width <= 0 or height <= 0:
            errors.append(f"Texture {image.name} has no pixel data")
        elif max(width, height) > max_size:
            warnings.append(f"Texture {image.name} will be scaled to a maximum of {max_size}px")
        if not image_source_exists(image):
            errors.append(f"Texture source does not exist: {image.name}")
        if "color" in usages and "data" in usages:
            warnings.append(f"Texture {image.name} is used as both color and data; its format will be preserved")
        if "ambiguous" in usages:
            warnings.append(f"Texture {image.name} has an unsupported or ambiguous node path; its format will be preserved")
    warnings.extend(alpha_material_warnings(objects, additional_materials))
    return errors, warnings


def image_extension(image):
    return Path(image.filepath_raw or image.filepath).suffix.lower()


def image_is_data(image):
    color_settings = image.colorspace_settings
    return bool(
        getattr(color_settings, "is_data", False)
        or color_settings.name.lower() in {"non-color", "raw"}
    )


def ensure_image_data_loaded(image):
    if image.has_data:
        return
    try:
        image.pixels[0]
    except Exception as error:
        raise RuntimeError(f"Texture {image.name} has no readable pixel data") from error
    if not image.has_data:
        raise RuntimeError(f"Texture {image.name} has no readable pixel data")


def duplicate_image_with_data(image):
    ensure_image_data_loaded(image)
    copy = image.copy()
    try:
        ensure_image_data_loaded(copy)
        return copy
    except RuntimeError:
        bpy.data.images.remove(copy)

    width, height = image.size
    copy = bpy.data.images.new(
        name=f"{image.name}_vectorg_export_source",
        width=width,
        height=height,
        alpha=image.channels == 4,
        float_buffer=image.is_float,
    )
    try:
        copy.colorspace_settings.name = image.colorspace_settings.name
        copy.alpha_mode = image.alpha_mode
        copy.pixels.foreach_set(image.pixels)
        copy.update()
        return copy
    except Exception as error:
        bpy.data.images.remove(copy)
        raise RuntimeError(f"Texture {image.name} pixel data could not be copied") from error


def optimized_export_image(
    image,
    usages,
    max_size,
    optimize_color_textures,
    temporary_directory,
    temporary_index,
    jpeg_quality,
):
    width, height = image.size
    if width <= 0 or height <= 0:
        return None
    longest_side = max(width, height)
    should_resize = longest_side > max_size
    source_extension = image_extension(image)
    should_use_jpeg = (
        optimize_color_textures
        and usages == {"color"}
        and not image_is_data(image)
        and source_extension not in {".jpg", ".jpeg", ".jpe", ".webp"}
    )
    if not should_resize and not should_use_jpeg:
        return None
    if should_resize:
        scale = max_size / longest_side
        target_width = max(1, round(width * scale))
        target_height = max(1, round(height * scale))
    else:
        target_width, target_height = width, height
    copy = duplicate_image_with_data(image)
    copy.name = f"{image.name}_vectorg_export_{target_width}x{target_height}"
    if should_resize:
        copy.scale(target_width, target_height)
    if should_use_jpeg:
        jpeg_path = Path(temporary_directory) / f"texture_{temporary_index}.jpg"
        copy.filepath_raw = str(jpeg_path)
        copy.file_format = "JPEG"
        replacement = None
        try:
            copy.save(quality=jpeg_quality)
            replacement = bpy.data.images.load(str(jpeg_path), check_existing=False)
            replacement.name = copy.name
            replacement[TEMP_IMAGE_FILE_PROPERTY] = str(jpeg_path)
            return replacement
        except Exception:
            if replacement and replacement.name in bpy.data.images:
                bpy.data.images.remove(replacement)
            jpeg_path.unlink(missing_ok=True)
            raise
        finally:
            bpy.data.images.remove(copy)
    return copy


def apply_export_texture_optimization(
    objects,
    max_size,
    optimize_color_textures,
    temporary_directory,
    jpeg_quality,
):
    usage_by_image = classify_texture_usage(objects)
    replacements = {}
    restored_nodes = []
    temp_images = []
    try:
        for material in export_materials(objects):
            for node in material_texture_nodes(material):
                source = node.image
                if source not in replacements:
                    replacement = optimized_export_image(
                        source,
                        usage_by_image.get(source, {"ambiguous"}),
                        max_size,
                        optimize_color_textures,
                        temporary_directory,
                        len(replacements),
                        jpeg_quality,
                    )
                    replacements[source] = replacement or source
                    if replacement:
                        temp_images.append(replacement)
                replacement = replacements[source]
                if replacement is not source:
                    restored_nodes.append((node, source))
                    node.image = replacement
    except Exception:
        restore_export_textures(restored_nodes, temp_images)
        raise
    return restored_nodes, temp_images


def restore_export_textures(restored_nodes, temp_images):
    for node, source in restored_nodes:
        node.image = source
    for image in temp_images:
        temporary_file = image.get(TEMP_IMAGE_FILE_PROPERTY)
        try:
            if image.name in bpy.data.images:
                bpy.data.images.remove(image)
        finally:
            if temporary_file:
                Path(temporary_file).unlink(missing_ok=True)


def gltf_image_export_options(jpeg_quality):
    properties = {
        prop.identifier
        for prop in bpy.ops.export_scene.gltf.get_rna_type().properties
    }
    options = {}
    for name, value in (
        ("export_image_format", "AUTO"),
        ("export_image_quality", jpeg_quality),
        ("export_jpeg_quality", jpeg_quality),
        ("export_unused_images", False),
        ("export_unused_textures", False),
    ):
        if name in properties:
            options[name] = value
    return options


def validate_scene(settings):
    errors = []
    warnings = []

    if not settings.is_configured:
        errors.append("Create configuration first")
        return errors, warnings
    if not PACKAGE_VERSION_PATTERN.fullmatch(settings.package_version):
        errors.append("Package version may only contain letters, numbers, dot, underscore, plus, and dash")

    car_obj = settings.car_root_object
    required = [
        ("car root", settings.car_root_object),
        ("center of mass", settings.center_of_mass_object),
        ("steering wheel", settings.steering_wheel_object),
        ("chase camera", settings.chase_camera_object),
        ("cockpit camera", settings.cockpit_camera_object),
        ("hood camera", settings.hood_camera_object),
        ("roof camera", settings.roof_camera_object),
    ]

    for label, obj in required:
        if not obj:
            errors.append(f"Missing {label} object")

    if car_obj:
        for label, obj in required[1:]:
            validate_object_in_car_tree(errors, car_obj, label, obj)

    downforce_objects = set()
    downforce_names = set()
    for index, point in enumerate(settings.down_force_points, start=1):
        display_name = downforce_point_display_name(point, index - 1)
        normalized_name = display_name.casefold()
        if normalized_name in downforce_names:
            errors.append(f"Duplicate downforce point name: {display_name}")
        downforce_names.add(normalized_name)
        point_obj = point.object_ref
        if not point_obj:
            errors.append(f"Downforce point {index} object is required")
            continue
        if point_obj.type != "EMPTY":
            errors.append(f"Downforce point {index} must be an Empty object")
        if point_obj in downforce_objects:
            errors.append(f"Downforce point object is used more than once: {object_config_name(point_obj)}")
        downforce_objects.add(point_obj)
        validate_object_in_car_tree(errors, car_obj, f"Downforce point {index}", point_obj)
        position = relative_to_car(car_obj, point_obj)
        if position is None or not all(math.isfinite(value) for value in position):
            errors.append(f"Downforce point {index} position must be finite")
        if not math.isfinite(point.max_force) or point.max_force < 0:
            errors.append(f"Downforce point {index} max force must be non-negative")

    for label, prefix in (
        ("Chase camera", "chase"),
        ("Cockpit camera", "cockpit"),
        ("Hood camera", "hood"),
        ("Roof camera", "roof"),
    ):
        camera_obj = getattr(settings, f"{prefix}_camera_object")
        if not camera_obj:
            continue
        if camera_obj.type != "CAMERA":
            errors.append(f"{label} must be a Camera object")
            continue
        target_obj = camera_target_child(camera_obj)
        if target_obj and target_obj.parent != camera_obj:
            errors.append(f"{label} target must be a camera child")

    if len(settings.colliders) == 0:
        errors.append("At least one collider is required")

    collider_names = set()
    for index, collider in enumerate(settings.colliders, start=1):
        collider_name = object_config_name(collider.object_ref)
        if not collider.object_ref:
            errors.append(f"Collider {index} object is required")
            continue
        if collider_name in collider_names:
            warnings.append(f"Collider object is used more than once: {collider_name}")
        collider_names.add(collider_name)
        validate_object_in_car_tree(errors, car_obj, f"Collider {index}", collider.object_ref)

    ensure_default_wheels(settings)
    ensure_default_presets(settings)
    validate_ghost_scene(settings, errors, warnings)
    validate_armature_scene(settings, errors, warnings)

    wheel_positions = {}
    wheel_rest_lengths = {}
    for index, wheel in enumerate(settings.wheels, start=1):
        mount_obj = wheel.suspension_ref
        joint_obj = wheel.hub_ref
        wheel_obj = wheel.wheel_ref
        if not mount_obj:
            errors.append(f"Wheel {index} mount object is required")
        if not joint_obj:
            errors.append(f"Wheel {index} joint object is required")
        if not wheel_obj:
            errors.append(f"Wheel {index} spin object is required")
        validate_object_in_car_tree(errors, car_obj, f"Wheel {index} mount", mount_obj)
        validate_object_in_car_tree(errors, car_obj, f"Wheel {index} joint", joint_obj)
        validate_object_in_car_tree(errors, car_obj, f"Wheel {index} spin", wheel_obj)
        if mount_obj and joint_obj and not is_object_in_tree(mount_obj, joint_obj):
            errors.append(f"Wheel {index} joint must be inside mount hierarchy")
        if joint_obj and wheel_obj and not is_object_in_tree(joint_obj, wheel_obj):
            errors.append(f"Wheel {index} spin must be inside joint hierarchy")
        if wheel_obj:
            wheel_positions[(wheel.group, wheel.key)] = wheel_obj.matrix_world.translation.copy()
            up_axis_world = object_axis(wheel_obj, BLENDER_AXIS_LOCAL[wheel.up_local_axis]).normalized()
            up_alignment = up_axis_world.dot(Vector((0, 0, 1)))
            if up_alignment < ORIENTATION_DOT_THRESHOLD:
                warnings.append(f"{object_config_name(wheel_obj)} configured up axis should point world +Z")
            axle_alignment = abs(object_axis(wheel_obj, BLENDER_AXIS_LOCAL[wheel.spin_local_axis]).dot(Vector((1, 0, 0))))
            if axle_alignment < ORIENTATION_DOT_THRESHOLD:
                warnings.append(f"{object_config_name(wheel_obj)} configured spin axis should align with world X left/right")
            if mount_obj and joint_obj:
                mount_to_joint = joint_obj.matrix_world.translation - mount_obj.matrix_world.translation
                wheel_rest_lengths[(wheel.group, wheel.key)] = mount_to_joint.length
                if mount_to_joint.length <= 1e-6:
                    errors.append(f"Wheel {index} Mount and Joint must be at different positions")
            if joint_obj:
                steering_axis = object_axis(joint_obj, BLENDER_AXIS_LOCAL[wheel.up_local_axis]).normalized()
                if steering_axis.dot(Vector((0, 0, 1))) <= 0.1:
                    errors.append(f"Wheel {index} selected Up Local Axis on Joint must point upward along the kingpin")

    for group in ("front", "rear"):
        left_pos = wheel_positions.get((group, "l"))
        right_pos = wheel_positions.get((group, "r"))
        if left_pos is not None and right_pos is not None and left_pos.x <= right_pos.x:
            warnings.append(f"{group.title()} left wheel should be on world +X side of right wheel")

    for key in ("l", "r"):
        front_pos = wheel_positions.get(("front", key))
        rear_pos = wheel_positions.get(("rear", key))
        if front_pos is not None and rear_pos is not None and front_pos.y >= rear_pos.y:
            warnings.append(f"Front {key.upper()} wheel should be forward of rear {key.upper()} wheel on world -Y")

    assist_max_levels = {
        "ABS": settings.abs_max_level,
        "ESC": settings.esc_max_level,
        "Traction Control": settings.traction_control_max_level,
    }
    for assist_name, max_level in assist_max_levels.items():
        if not isinstance(max_level, int) or max_level < 1:
            errors.append(f"{assist_name} max level must be a positive integer")

    preset_ids = set()
    for index, preset in enumerate(settings.presets, start=1):
        label = preset.display_name.strip() or preset.preset_id or f"Preset {index}"
        if not preset.preset_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", preset.preset_id):
            errors.append(f"{label} preset has an invalid ID")
        elif preset.preset_id in preset_ids:
            errors.append(f"Duplicate preset ID: {preset.preset_id}")
        preset_ids.add(preset.preset_id)
        if not preset.display_name.strip():
            errors.append(f"{label} preset name is required")
        if not math.isfinite(preset.max_steering_angle) or not 1.0 <= preset.max_steering_angle <= 90.0:
            errors.append(f"{label} max steering angle must be between 1 and 90 degrees")
        if not math.isfinite(preset.road_wheel_curve) or not 0.0 <= preset.road_wheel_curve <= 1.0:
            errors.append(f"{label} steering response curve must be between 0 and 1")
        if not math.isfinite(preset.max_degrees_of_rotation) or not 90.0 <= preset.max_degrees_of_rotation <= 2160.0:
            errors.append(f"{label} steering wheel rotation must be between 90 and 2160 degrees")
        if not math.isfinite(preset.brake_bias) or not 0.0 <= preset.brake_bias <= 1.0:
            errors.append(f"{label} brake bias must be between 0 and 1")
        if not math.isfinite(preset.final_drive_ratio) or preset.final_drive_ratio <= 0:
            errors.append(f"{label} final drive ratio must be positive")
        if not math.isfinite(preset.reverse_ratio) or preset.reverse_ratio >= 0:
            errors.append(f"{label} reverse ratio must be negative")
        for gear_index in range(1, preset.forward_gear_count + 1):
            gear_ratio = getattr(preset, f"gear_{gear_index}")
            if not math.isfinite(gear_ratio) or gear_ratio <= 0:
                errors.append(f"{label} gear {gear_index} ratio must be positive")
        for axle, stiffness in (
            ("front", preset.front_anti_roll_bar_stiffness),
            ("rear", preset.rear_anti_roll_bar_stiffness),
        ):
            if not math.isfinite(stiffness) or not 1.0 <= stiffness <= 40.0:
                errors.append(f"{label} {axle} anti-roll bar stiffness must be between 1 and 40")
        for assist_name, level, max_level in (
            ("ABS", preset.abs_level, settings.abs_max_level),
            ("ESC", preset.esc_level, settings.esc_max_level),
            ("Traction Control", preset.traction_control_level, settings.traction_control_max_level),
        ):
            if not isinstance(level, int) or not 0 <= level <= max_level:
                errors.append(f"{label} {assist_name} level must be between 0 and {max_level}")
        for group in ("front", "rear"):
            wheel = getattr(preset, group)
            if wheel.tire_type not in TIRE_TYPES:
                errors.append(f"{label} {group} tire type is invalid")
            if not all(math.isfinite(value) for value in (
                wheel.pressure,
                wheel.camber,
                wheel.caster,
                wheel.toe,
                wheel.suspension_offset,
                wheel.suspension_stiffness,
                wheel.damping_relaxation,
                wheel.damping_compression,
                wheel.max_brake_force,
                wheel.grip_factor,
            )):
                errors.append(f"{label} {group} adjustments must be finite")
            if not -15.0 <= wheel.caster <= 15.0:
                errors.append(f"{label} {group} caster must be between -15 and 15 degrees")
            if any(value < 0 for value in (
                wheel.suspension_stiffness,
                wheel.damping_relaxation,
                wheel.damping_compression,
                wheel.max_brake_force,
            )):
                errors.append(f"{label} {group} handling values must be non-negative")
            if wheel.grip_factor <= 0:
                errors.append(f"{label} {group} grip factor must be a positive number")
            for key in ("l", "r"):
                rest_length = wheel_rest_lengths.get((group, key))
                if rest_length is not None and rest_length + wheel.suspension_offset <= 0:
                    errors.append(
                        f"{label} {group} suspension offset collapses the {key.upper()} wheel rest length"
                    )

    steering_obj = settings.steering_wheel_object
    if steering_obj:
        steering_spin_alignment = abs(object_axis(steering_obj, BLENDER_AXIS_LOCAL[settings.steering_wheel_spin_axis]).dot(Vector((0, 1, 0))))
        if steering_spin_alignment < STEERING_WHEEL_DOT_THRESHOLD:
            warnings.append("Steering wheel configured spin axis should align with world Y forward/back")

    dashboard_screen = settings.dashboard_screen_object
    if dashboard_screen:
        validate_object_in_car_tree(errors, car_obj, "Dashboard screen", dashboard_screen)
        if dashboard_screen.type != "MESH":
            errors.append("Dashboard screen must be a mesh object")
        else:
            uv_layer = dashboard_screen.data.uv_layers.active
            if not uv_layer or not uv_layer.data:
                errors.append("Dashboard screen must have an active UV map")
            else:
                u_values = [loop.uv.x for loop in uv_layer.data]
                v_values = [loop.uv.y for loop in uv_layer.data]
                if min(u_values) > 0.01 or max(u_values) < 0.99 or min(v_values) > 0.01 or max(v_values) < 0.99:
                    errors.append("Dashboard screen UV map must cover the full 0-1 texture area")

            screen_materials = {
                slot.material
                for slot in dashboard_screen.material_slots
                if slot.material
            }
            if len(screen_materials) != 1:
                errors.append("Dashboard screen must use exactly one material")
            else:
                screen_material = next(iter(screen_materials))
                material_users = [
                    obj.name
                    for obj in bpy.context.scene.objects
                    if obj != dashboard_screen
                    and obj.type == "MESH"
                    and any(slot.material == screen_material for slot in obj.material_slots)
                    and obj not in guide_objects()
                ]
                if material_users:
                    errors.append(
                        f"Dashboard screen material must not be shared with other meshes: {', '.join(material_users)}"
                    )

            screen_size = object_world_bounds_size(dashboard_screen)
            if not screen_size or screen_size.x <= 1e-6 or screen_size.z <= 1e-6:
                errors.append(
                    "Dashboard screen local X width and local Z length must be greater than zero"
                )

    exported_material_names = {
        material.name
        for material in export_materials(
            car_export_objects(settings)
        )
    }
    ghost_objects = set(hierarchy_objects(settings.ghost_root_object))
    car_objects = [obj for obj in hierarchy_objects(car_obj) if obj not in ghost_objects]
    body_color_names = set()
    body_color_material_names = set()
    for index, body_color in enumerate(settings.body_colors):
        label = f"Body color {index + 1}"
        display_name = body_color.display_name.strip()
        normalized_name = display_name.casefold()
        if not display_name:
            errors.append(f"{label} name is required")
        elif normalized_name in body_color_names:
            errors.append(f"Duplicate body color name: {display_name}")
        body_color_names.add(normalized_name)

        material = body_color.material
        if not material:
            errors.append(f"{label} material is required")
            continue
        if material.name in body_color_material_names:
            errors.append(f"Body color material is used more than once: {material.name}")
        body_color_material_names.add(material.name)
        if index == 0 and not material_is_assigned_to_geometry(material, car_objects):
            errors.append(f"Default body color material is not assigned to car geometry: {material.name}")

    for label, prop_name in (
        ("Headlights", "headlights_material"),
        ("Brake lights", "brake_lights_material"),
        ("Reverse lights", "reverse_lights_material"),
    ):
        material = getattr(settings, prop_name)
        if material and material.name not in exported_material_names:
            errors.append(f"{label} material is not used by an exported mesh: {material.name}")

    if settings.use_custom_sounds:
        for slot in SOUND_SLOTS:
            if not getattr(settings, f"sound_{slot}_enabled"):
                continue
            path = getattr(settings, f"sound_{slot}")
            if path and not os.path.isfile(abspath(path)):
                errors.append(f"Sound file for {slot} does not exist: {path}")

    if not settings.car_id:
        errors.append("Car ID is required")
    elif not settings.car_id.replace("_", "").replace("-", "").isalnum():
        errors.append("Car ID may only contain letters, numbers, underscore, and dash")

    if not (
        settings.idle_rpm
        < settings.redline_rpm
        <= settings.rev_limit
        <= settings.max_rpm
    ):
        errors.append("Engine RPM values must satisfy idleRPM < redlineRPM <= revLimit <= maxRPM")
    if not math.isfinite(settings.engine_braking) or settings.engine_braking < 0:
        errors.append("Engine Braking Factor must be a finite nonnegative number")

    if car_obj:
        body_color_materials = [body_color.material for body_color in settings.body_colors if body_color.material]
        texture_errors, texture_warnings = texture_validation(
            car_export_objects(settings),
            int(settings.max_texture_size),
            body_color_materials,
        )
        errors.extend(texture_errors)
        warnings.extend(texture_warnings)

    return errors, warnings


class CarBodyColorSettings(PropertyGroup):
    display_name: StringProperty(
        name="Name",
        description="Player-facing name for this selectable body color",
        default="",
    )
    material: PointerProperty(
        name="Material",
        description="Body material associated with this selectable color",
        type=bpy.types.Material,
    )


class CarColliderSettings(PropertyGroup):
    object_ref: PointerProperty(
        name="Object",
        description="Mesh object used as this physics collider",
        type=bpy.types.Object,
        update=update_collider_object,
    )
    collider_type: EnumProperty(
        name="Type",
        description="Collision shape generated from the selected object",
        items=(("trimesh", "Trimesh", ""), ("box", "Box", "")),
        default="trimesh",
    )
    mass: FloatProperty(
        name="Mass (kg)",
        description="Collider mass in kilograms",
        default=1230.0,
        min=0.0,
    )


class CarDownForcePointSettings(PropertyGroup):
    def get_max_force_kg(self):
        return self.max_force / NEWTONS_PER_KILOGRAM

    def set_max_force_kg(self, value):
        self.max_force = value * NEWTONS_PER_KILOGRAM

    display_name: StringProperty(
        name="Name",
        description="Name used to identify this aerodynamic load point",
        default="",
    )
    object_ref: PointerProperty(
        name="Point",
        description="Helper object defining where this downforce is applied",
        type=bpy.types.Object,
    )
    max_force: FloatProperty(name="Max Force", default=3000.0, min=0.0)
    max_force_kg: FloatProperty(
        name="Max Downforce (kg)",
        description="Equivalent weight added by this downforce point at maximum speed",
        min=0.0,
        get=get_max_force_kg,
        set=set_max_force_kg,
    )


class CarWheelSettings(PropertyGroup):
    group: StringProperty(name="Group", default="front")
    key: StringProperty(name="Key", default="l")
    steering: BoolProperty(
        name="Steering",
        description="Allow this wheel to turn with steering input",
        default=False,
    )
    suspension_ref: PointerProperty(
        name="Mount",
        description="Object marking where the suspension attaches to the chassis",
        type=bpy.types.Object,
    )
    hub_ref: PointerProperty(
        name="Joint",
        description="Steering pivot and kingpin orientation; also receives the neutral toe rotation",
        type=bpy.types.Object,
    )
    wheel_ref: PointerProperty(
        name="Spin",
        description="Object that visually rotates with wheel speed",
        type=bpy.types.Object,
    )
    up_local_axis: EnumProperty(
        name="Up Local Axis",
        description="Shared local up axis: Joint defines the kingpin orientation and Spin defines the tire orientation",
        items=AXIS_ITEMS,
        default="z",
    )
    spin_local_axis: EnumProperty(
        name="Spin Local Axis",
        description="Wheel object's local axis around which the wheel rotates",
        items=AXIS_ITEMS,
        default="x",
    )
    radius: FloatProperty(
        name="Radius (m)",
        description="Wheel radius in metres, measured from the wheel centre to the tire contact surface",
        default=0.3,
        min=0.01,
    )
    # Retained as hidden migration sources for blend files saved with preset schema 3.
    suspension_stiffness: FloatProperty(default=80.0, options={"HIDDEN"})
    damping_relaxation: FloatProperty(default=2.6, options={"HIDDEN"})
    damping_compression: FloatProperty(default=2.0, options={"HIDDEN"})
    max_brake_force: FloatProperty(default=1000.0, min=0.0, options={"HIDDEN"})
    pressure: FloatProperty(default=2.0, min=1.3, max=2.7, options={"HIDDEN"})
    camber: FloatProperty(default=-4.0, options={"HIDDEN"})
    toe: FloatProperty(default=-0.15, options={"HIDDEN"})
    grip_factor: FloatProperty(
        default=1.0,
        min=0.01,
        options={"HIDDEN"},
    )


class CarArmatureJointSettings(PropertyGroup):
    bone: StringProperty(name="Bone", description="Existing bone driven by the selected attachments")
    base_attachment: EnumProperty(
        name="Base Attachment", items=ARMATURE_ATTACHMENT_ITEMS, default="root",
        description="Existing car role followed by the bone head, preserving its authored offset",
    )
    tip_attachment: EnumProperty(
        name="Tip Attachment", items=ARMATURE_TIP_ITEMS, default="none",
        description="Existing car role followed by the bone tail; None follows only the base",
    )
    stretch: BoolProperty(
        name="Stretch", default=False,
        description="Scale the bone along its length to reach the tip, preserving thickness",
    )


class CarGhostWheelSettings(PropertyGroup):
    suspension_ref: PointerProperty(
        name="Mount", description="Ghost suspension attachment inside Ghost Root", type=bpy.types.Object,
    )
    hub_ref: PointerProperty(
        name="Joint", description="Ghost steering and alignment node inside the mount", type=bpy.types.Object,
    )
    wheel_ref: PointerProperty(
        name="Spin", description="Ghost wheel geometry or parent that rotates inside the joint", type=bpy.types.Object,
    )


class CarWheelPresetSettings(PropertyGroup):
    def get_max_brake_force_kg(self):
        return self.max_brake_force / NEWTONS_PER_KILOGRAM

    def set_max_brake_force_kg(self, value):
        self.max_brake_force = value * NEWTONS_PER_KILOGRAM

    group: StringProperty(name="Group", default="front")
    key: StringProperty(name="Key", default="l")
    tire_type: EnumProperty(
        name="Tire Compound",
        description="Tire compound used to determine available grip",
        items=TIRE_TYPE_ITEMS,
        default="medium",
    )
    pressure: FloatProperty(
        name="Tire Pressure (bar)",
        description="Tire inflation pressure in bar; higher pressure reduces available grip",
        default=2.0,
        min=1.3,
        max=2.7,
    )
    camber: FloatProperty(
        name="Camber (°)",
        description="Wheel tilt viewed from the front; negative tilts the top inward and positive tilts it outward",
        default=-4.0,
    )
    caster: FloatProperty(
        name="Caster (°)",
        description="Steering-axis tilt viewed from the side; positive tilts the top toward the rear and negative toward the front",
        default=0.0,
        min=-15.0,
        max=15.0,
    )
    toe: FloatProperty(
        name="Toe (°)",
        description="Wheel direction viewed from above; negative points the fronts inward and positive points them outward",
        default=-0.15,
    )
    suspension_offset: FloatProperty(
        name="Suspension Offset (m)",
        description="Change to suspension rest length in metres; positive moves the wheel farther from the mount and negative moves it closer",
        default=0.0,
        min=-0.25,
        max=0.25,
        unit="LENGTH",
    )
    suspension_stiffness: FloatProperty(
        name="Suspension Stiffness",
        description="Spring strength based on compression distance; higher values make the suspension firmer and reduce compression",
        default=80.0,
        min=0.0,
    )
    damping_relaxation: FloatProperty(
        name="Damping Relaxation",
        description="Resistance while the suspension extends; higher values slow rebound and reduce bouncing",
        default=2.6,
        min=0.0,
    )
    damping_compression: FloatProperty(
        name="Damping Compression",
        description="Resistance while the suspension compresses; higher values resist rapid compression over bumps",
        default=2.0,
        min=0.0,
    )
    max_brake_force: FloatProperty(name="Max Brake Force (N)", default=1000.0, min=0.0)
    max_brake_force_kg: FloatProperty(
        name="Max Brake Force (kg)",
        description="Equivalent braking force available at each wheel; exported in newtons",
        min=0.0,
        get=get_max_brake_force_kg,
        set=set_max_brake_force_kg,
    )
    grip_factor: FloatProperty(
        name="Grip Factor",
        description="Multiplier for this wheel's pressure-derived grip",
        default=1.0,
        min=0.01,
    )


class CarPresetSettings(PropertyGroup):
    preset_id: StringProperty(
        name="ID",
        description="Stable unique identifier stored with this preset",
        default="default",
    )
    display_name: StringProperty(
        name="Name",
        description="Player-facing name for this vehicle preset",
        default="Default",
    )
    max_steering_angle: FloatProperty(
        name="Maximum Steering Angle (°)",
        description="Maximum angle the road wheels can turn from straight ahead",
        default=50.0,
        min=1.0,
        max=90.0,
    )
    road_wheel_curve: FloatProperty(
        name="Steering Response Curve",
        description="Blend between linear (0) and cubic (1) road-wheel response",
        default=0.5,
        min=0.0,
        max=1.0,
        step=5,
        precision=2,
    )
    max_degrees_of_rotation: FloatProperty(
        name="Steering Wheel Rotation (°)",
        description="Total steering wheel rotation from full left lock to full right lock",
        default=540.0,
        min=90.0,
        max=2160.0,
    )
    front_anti_roll_bar_stiffness: FloatProperty(
        name="Front Anti-Roll Bar",
        description="Linear front-axle anti-roll coupling stiffness",
        default=15.0,
        min=1.0,
        max=40.0,
        step=25,
        precision=2,
    )
    rear_anti_roll_bar_stiffness: FloatProperty(
        name="Rear Anti-Roll Bar",
        description="Linear rear-axle anti-roll coupling stiffness",
        default=15.0,
        min=1.0,
        max=40.0,
        step=25,
        precision=2,
    )
    abs_level: IntProperty(
        name="ABS Level",
        description="Default anti-lock braking level for this preset; zero disables ABS",
        default=5,
        min=0,
    )
    esc_level: IntProperty(
        name="ESC Level",
        description="Default electronic stability control level for this preset; zero disables ESC",
        default=0,
        min=0,
    )
    traction_control_level: IntProperty(
        name="Traction Control Level",
        description="Default traction control level for this preset; zero disables traction control",
        default=5,
        min=0,
    )
    brake_bias: FloatProperty(
        name="Brake Bias",
        description="Front brake force proportion",
        default=0.6,
        min=0.0,
        max=1.0,
        subtype="FACTOR",
    )
    final_drive_ratio: FloatProperty(
        name="Final Drive Ratio",
        description="Multiplier applied to every selected gear ratio before torque reaches the wheels",
        default=5.0,
        min=0.01,
    )
    reverse_ratio: FloatProperty(
        name="Reverse",
        description="Reverse gear ratio; negative values produce reverse wheel rotation",
        default=-3.57,
    )
    forward_gear_count: IntProperty(
        name="Forward Gears",
        description="Number of forward gear ratios exported for this preset",
        default=6,
        min=1,
        max=15,
    )
    gear_1: FloatProperty(name="Gear 1", description="First-gear ratio", default=4.08, min=0.01)
    gear_2: FloatProperty(name="Gear 2", description="Second-gear ratio", default=2.7, min=0.01)
    gear_3: FloatProperty(name="Gear 3", description="Third-gear ratio", default=1.9, min=0.01)
    gear_4: FloatProperty(name="Gear 4", description="Fourth-gear ratio", default=1.4, min=0.01)
    gear_5: FloatProperty(name="Gear 5", description="Fifth-gear ratio", default=1.06, min=0.01)
    gear_6: FloatProperty(name="Gear 6", description="Sixth-gear ratio", default=0.85, min=0.01)
    gear_7: FloatProperty(name="Gear 7", description="Seventh-gear ratio", default=0.70, min=0.01)
    gear_8: FloatProperty(name="Gear 8", description="Eighth-gear ratio", default=0.58, min=0.01)
    gear_9: FloatProperty(name="Gear 9", description="Ninth-gear ratio", default=0.50, min=0.01)
    gear_10: FloatProperty(name="Gear 10", description="Tenth-gear ratio", default=0.44, min=0.01)
    gear_11: FloatProperty(name="Gear 11", description="Eleventh-gear ratio", default=0.40, min=0.01)
    gear_12: FloatProperty(name="Gear 12", description="Twelfth-gear ratio", default=0.36, min=0.01)
    gear_13: FloatProperty(name="Gear 13", description="Thirteenth-gear ratio", default=0.33, min=0.01)
    gear_14: FloatProperty(name="Gear 14", description="Fourteenth-gear ratio", default=0.30, min=0.01)
    gear_15: FloatProperty(name="Gear 15", description="Fifteenth-gear ratio", default=0.28, min=0.01)
    front: PointerProperty(type=CarWheelPresetSettings)
    rear: PointerProperty(type=CarWheelPresetSettings)
    wheels: CollectionProperty(type=CarWheelPresetSettings)


class CarExporterSettings(PropertyGroup):
    is_configured: BoolProperty(name="Configured", default=False)
    car_id: StringProperty(
        name="Car ID",
        description="Stable package identifier used in filenames and game data",
        default="my_car",
    )
    package_version: StringProperty(
        name="Package Version",
        description="Explicit asset revision; increment when package contents change",
        default="1",
    )
    display_name: StringProperty(
        name="Display Name",
        description="Player-facing vehicle name shown in the game",
        default="My Car",
    )
    max_texture_size: EnumProperty(
        name="Maximum Texture Size",
        description="Maximum exported material-texture dimension",
        items=TEXTURE_SIZE_ITEMS,
        default=str(DEFAULT_MAX_TEXTURE_SIZE),
    )
    optimize_color_textures: BoolProperty(
        name="Compress Opaque Color Textures",
        description="Export unambiguous opaque color textures as JPEG while preserving alpha and data textures",
        default=True,
    )
    jpeg_quality: IntProperty(
        name="JPEG Quality",
        description="Quality used for optimized opaque color textures",
        default=DEFAULT_JPEG_QUALITY,
        min=1,
        max=100,
    )
    car_class: StringProperty(
        name="Class",
        description="Player-facing vehicle class used for grouping and display",
        default="GT",
    )
    vehicle_tag_tarmac: BoolProperty(
        name="Tarmac",
        description="Mark this vehicle as suitable for tarmac tracks",
        default=True,
    )
    vehicle_tag_offroad: BoolProperty(
        name="Offroad",
        description="Mark this vehicle as suitable for off-road tracks",
        default=True,
    )
    abs_max_level: IntProperty(
        name="ABS Max Level",
        description="Highest player-selectable ABS level; preset levels are measured against this maximum",
        default=5,
        min=1,
        update=update_driver_assist_max_levels,
    )
    esc_max_level: IntProperty(
        name="ESC Max Level",
        description="Highest player-selectable ESC level; preset levels are measured against this maximum",
        default=5,
        min=1,
        update=update_driver_assist_max_levels,
    )
    traction_control_max_level: IntProperty(
        name="Traction Control Max Level",
        description="Highest player-selectable traction control level; preset levels are measured against this maximum",
        default=5,
        min=1,
        update=update_driver_assist_max_levels,
    )
    car_root_object: PointerProperty(
        name="Car Root",
        description="Root object containing the complete vehicle hierarchy",
        type=bpy.types.Object,
    )
    ghost_enabled: BoolProperty(
        name="Enable Custom Ghost",
        description="Export a dedicated ghost subtree in the car GLB; otherwise export ghost as null",
        default=False,
    )
    ghost_root_object: PointerProperty(
        name="Ghost Root",
        description="Direct child of Car Root containing all custom ghost geometry and wheel nodes",
        type=bpy.types.Object,
    )
    ghost_front_l: PointerProperty(type=CarGhostWheelSettings)
    ghost_front_r: PointerProperty(type=CarGhostWheelSettings)
    ghost_rear_l: PointerProperty(type=CarGhostWheelSettings)
    ghost_rear_r: PointerProperty(type=CarGhostWheelSettings)
    armature_enabled: BoolProperty(
        name="Enable Armature",
        description="Export bone mappings that follow, aim and stretch between existing car attachments",
        default=False,
    )
    armature_object: PointerProperty(
        name="Object",
        description="Independent suspension armature inside Car Root",
        type=bpy.types.Object,
        poll=armature_object_poll,
    )
    armature_joints: CollectionProperty(type=CarArmatureJointSettings)
    center_of_mass_object: PointerProperty(
        name="Center of Mass",
        description="Helper object defining the vehicle's center of mass",
        type=bpy.types.Object,
    )
    steering_wheel_object: PointerProperty(
        name="Steering Wheel",
        description="Object visually rotated by player steering input",
        type=bpy.types.Object,
    )
    steering_wheel_spin_axis: EnumProperty(
        name="Steering Wheel Spin Axis",
        description="Steering wheel's local rotation axis",
        items=AXIS_ITEMS,
        default="y",
    )
    # Retained as hidden migration sources for blend files saved with preset schema 4.
    max_degrees_of_rotation: FloatProperty(default=540.0, min=90.0, max=2160.0, options={"HIDDEN"})
    headlights_material: PointerProperty(
        name="Headlights",
        description="Emissive material controlled by the vehicle headlights",
        type=bpy.types.Material,
    )
    brake_lights_material: PointerProperty(
        name="Brake Lights",
        description="Emissive material illuminated while braking",
        type=bpy.types.Material,
    )
    reverse_lights_material: PointerProperty(
        name="Reverse Lights",
        description="Emissive material illuminated while reversing",
        type=bpy.types.Material,
    )
    dashboard_screen_object: PointerProperty(
        name="Screen",
        description="Mesh object used as the in-game dashboard display",
        type=bpy.types.Object,
    )
    # Retained as a hidden migration source for vehicle manifest version 6.
    down_force: FloatProperty(name="Downforce", default=3000.0)
    air_drag: FloatProperty(
        name="Air Drag",
        description="Aerodynamic drag coefficient; higher values create more resistance as speed increases",
        default=0.5,
        min=0.0,
        max=1.0,
    )
    abs: FloatProperty(default=1.0, min=0.0, max=1.0, options={"HIDDEN"})
    esc: FloatProperty(default=0.0, min=0.0, max=1.0, options={"HIDDEN"})
    traction_control: FloatProperty(default=1.0, min=0.0, max=1.0, options={"HIDDEN"})
    # Retained as a hidden migration source for blend files saved with preset schema 3.
    max_steering_angle: FloatProperty(default=50.0, min=1.0, max=90.0, options={"HIDDEN"})
    use_custom_sounds: BoolProperty(
        name="Use Custom Sounds",
        description="Configure each vehicle sound to inherit the default, use a custom file, or be disabled",
        default=False,
    )
    sound_pitch_offset: IntProperty(
        name="Pitch Offset (cents)",
        description="Vehicle-wide engine sample pitch offset; 100 cents equals one semitone",
        default=0,
        min=-2400,
        max=2400,
    )
    body_colors: CollectionProperty(type=CarBodyColorSettings)
    active_body_color_index: IntProperty(name="Active Body Color", default=0)
    colliders: CollectionProperty(type=CarColliderSettings)
    down_force_points: CollectionProperty(type=CarDownForcePointSettings)
    wheels: CollectionProperty(type=CarWheelSettings)
    presets: CollectionProperty(type=CarPresetSettings)
    active_preset_index: IntProperty(name="Active Preset", default=0)
    preset_schema_version: IntProperty(default=0, options={"HIDDEN"})
    guide_length: FloatProperty(name="Guide Length", default=4.5, min=0.1, unit="LENGTH")
    guide_width: FloatProperty(name="Guide Width", default=2.0, min=0.1, unit="LENGTH")
    guide_wheelbase: FloatProperty(name="Wheelbase", default=2.7, min=0.1, unit="LENGTH")
    guide_track_width: FloatProperty(name="Track Width", default=1.65, min=0.1, unit="LENGTH")

    drive: EnumProperty(
        name="Drive",
        description="Wheels powered by the engine: all, front, or rear",
        items=(("awd", "AWD", ""), ("fwd", "FWD", ""), ("rwd", "RWD", "")),
        default="awd",
    )
    hp: FloatProperty(
        name="Power (hp) — Display Only",
        description="Rated engine power shown to players; the torque curve controls vehicle physics",
        default=590.0,
        min=1.0,
    )
    max_rpm: IntProperty(
        name="Max RPM",
        description="Maximum engine speed represented by the torque curve",
        default=8000,
        min=1,
    )
    idle_rpm: IntProperty(
        name="Idle RPM",
        description="Minimum running engine speed when the engine is idling",
        default=1000,
        min=1,
    )
    redline_rpm: IntProperty(
        name="Redline RPM",
        description="Engine speed marking the start of the redline range",
        default=7000,
        min=1,
    )
    rev_limit: IntProperty(
        name="Rev Limit",
        description="Engine speed where combustion torque is cut to prevent further revving",
        default=7900,
        min=1,
    )
    engine_inertia: FloatProperty(
        name="Engine Inertia (kg·m²)",
        description="Resistance to RPM changes; higher values make the engine rev more slowly",
        default=0.2,
        min=0.01,
    )
    engine_braking: FloatProperty(
        name="Engine Braking Factor",
        description="Unitless fraction of peak engine torque used for engine braking; 0.2 gives 20% at maximum RPM with zero throttle, decreasing toward idle or as throttle increases",
        default=0.2,
        min=0.0,
    )
    engine_friction_torque: FloatProperty(
        name="Friction Torque (N·m)",
        description="Internal engine drag; higher values make RPM fall faster when the throttle is released",
        default=70.0,
        min=0.0,
    )
    clutch_response: FloatProperty(
        name="Clutch Response (s⁻¹)",
        description="Rate at which the clutch synchronizes engine and wheel RPM; higher values engage faster and more sharply",
        default=12.0,
        min=0.0,
    )
    shift_cooldown: FloatProperty(
        name="Gear Change Cooldown (s)",
        description="Minimum time before another gear change is allowed; zero disables the delay",
        default=0.0,
        min=0.0,
        unit="TIME",
    )
    auto_blip: BoolProperty(
        name="Auto Blip",
        description="Allow the game auto-blip setting to operate for this vehicle",
        default=True,
    )
    auto_blip_duration: FloatProperty(
        name="Auto Blip Duration (s)",
        description="Maximum time allowed for clutch-open RPM matching during an automatic downshift blip",
        default=0.2,
        min=0.0,
        max=1.0,
        unit="TIME",
    )
    turbo_enabled: BoolProperty(
        name="Turbo Enabled",
        description="Enable turbo boost for this engine",
        default=True,
    )
    turbo_boost: FloatProperty(
        name="Turbo Boost",
        description="Maximum turbo torque multiplier at full spool; 1.0 adds no boost",
        default=1.35,
        min=1.0,
        max=2.0,
    )
    max_torque: FloatProperty(
        name="Maximum Torque (N·m)",
        description="Peak torque used to scale the editable torque curve",
        default=590.0,
        min=1.0,
    )
    torque_factor: FloatProperty(
        name="Torque Factor",
        description="Multiplier applied to drive and engine-braking torque before tire-force limits",
        default=1.0,
        min=0.01,
    )

    torque_1000: FloatProperty(name="1000 RPM", default=422)
    torque_2000: FloatProperty(name="2000 RPM", default=506)
    torque_3000: FloatProperty(name="3000 RPM", default=565)
    torque_4000: FloatProperty(name="4000 RPM", default=590)
    torque_5000: FloatProperty(name="5000 RPM", default=586)
    torque_6000: FloatProperty(name="6000 RPM", default=564)
    torque_7000: FloatProperty(name="7000 RPM", default=523)
    torque_8000: FloatProperty(name="8000 RPM", default=460)

    chase_camera_object: PointerProperty(
        name="Chase",
        description="Camera object used for the chase view",
        type=bpy.types.Object,
        poll=camera_object_poll,
        update=update_chase_camera_object,
    )
    cockpit_camera_object: PointerProperty(
        name="Cockpit",
        description="Camera object used for the cockpit view",
        type=bpy.types.Object,
        poll=camera_object_poll,
        update=update_cockpit_camera_object,
    )
    hood_camera_object: PointerProperty(
        name="Hood",
        description="Camera object used for the hood view",
        type=bpy.types.Object,
        poll=camera_object_poll,
        update=update_hood_camera_object,
    )
    roof_camera_object: PointerProperty(
        name="Roof",
        description="Camera object used for the roof view",
        type=bpy.types.Object,
        poll=camera_object_poll,
        update=update_roof_camera_object,
    )
    chase_fov: FloatProperty(name="Chase FOV", default=39.5)
    cockpit_fov: FloatProperty(name="Cockpit FOV", default=32.3)
    hood_fov: FloatProperty(name="Hood FOV", default=44.1)
    roof_fov: FloatProperty(name="Roof FOV", default=44.1)
    chase_target_distance: FloatProperty(
        name="Target Distance",
        description="Distance in metres from the chase camera to its viewing target",
        default=5.0,
        min=0.01,
        update=update_chase_target_distance,
    )
    cockpit_target_distance: FloatProperty(
        name="Target Distance",
        description="Distance in metres from the cockpit camera to its viewing target",
        default=1.0,
        min=0.01,
        update=update_cockpit_target_distance,
    )
    hood_target_distance: FloatProperty(
        name="Target Distance",
        description="Distance in metres from the hood camera to its viewing target",
        default=2.0,
        min=0.01,
        update=update_hood_target_distance,
    )
    roof_target_distance: FloatProperty(
        name="Target Distance",
        description="Distance in metres from the roof camera to its viewing target",
        default=2.0,
        min=0.01,
        update=update_roof_target_distance,
    )
    sound_tranny_on: StringProperty(
        name="Transmission On",
        description="Transmission sound file played while engine torque is applied",
        subtype="FILE_PATH",
        default="",
    )
    sound_tranny_on_enabled: BoolProperty(
        name="Transmission On Enabled",
        description="Use the default sound when no file is assigned, use the assigned custom sound, or turn off to disable it",
        default=True,
    )
    sound_tranny_off: StringProperty(
        name="Transmission Off",
        description="Transmission sound file played while coasting or using engine braking",
        subtype="FILE_PATH",
        default="",
    )
    sound_tranny_off_enabled: BoolProperty(
        name="Transmission Off Enabled",
        description="Use the default sound when no file is assigned, use the assigned custom sound, or turn off to disable it",
        default=True,
    )
    sound_on_high: StringProperty(
        name="On High",
        description="High-RPM engine sound file used while throttle is applied",
        subtype="FILE_PATH",
        default="",
    )
    sound_on_high_enabled: BoolProperty(
        name="On High Enabled",
        description="Use the default sound when no file is assigned, use the assigned custom sound, or turn off to disable it",
        default=True,
    )
    sound_on_low: StringProperty(
        name="On Low",
        description="Low-RPM engine sound file used while throttle is applied",
        subtype="FILE_PATH",
        default="",
    )
    sound_on_low_enabled: BoolProperty(
        name="On Low Enabled",
        description="Use the default sound when no file is assigned, use the assigned custom sound, or turn off to disable it",
        default=True,
    )
    sound_off_high: StringProperty(
        name="Off High",
        description="High-RPM engine sound file used while the throttle is released",
        subtype="FILE_PATH",
        default="",
    )
    sound_off_high_enabled: BoolProperty(
        name="Off High Enabled",
        description="Use the default sound when no file is assigned, use the assigned custom sound, or turn off to disable it",
        default=True,
    )
    sound_off_low: StringProperty(
        name="Off Low",
        description="Low-RPM engine sound file used while the throttle is released",
        subtype="FILE_PATH",
        default="",
    )
    sound_off_low_enabled: BoolProperty(
        name="Off Low Enabled",
        description="Use the default sound when no file is assigned, use the assigned custom sound, or turn off to disable it",
        default=True,
    )
    sound_limiter: StringProperty(
        name="Limiter",
        description="Sound file played while the engine is touching the rev limiter",
        subtype="FILE_PATH",
        default="",
    )
    sound_limiter_enabled: BoolProperty(
        name="Limiter Enabled",
        description="Use the default sound when no file is assigned, use the assigned custom sound, or turn off to disable it",
        default=True,
    )
    sound_turbo: StringProperty(
        name="Turbo",
        description="Turbo sound file played after boost is released",
        subtype="FILE_PATH",
        default="",
    )
    sound_turbo_enabled: BoolProperty(
        name="Turbo Enabled",
        description="Use the default sound when no file is assigned, use the assigned custom sound, or turn off to disable it",
        default=True,
    )
    sound_tranny_on_volume: FloatProperty(
        name="Volume",
        description="Playback volume multiplier for the transmission-on sound",
        default=0.6,
        min=0.0,
        soft_max=1.0,
    )
    sound_tranny_off_volume: FloatProperty(
        name="Volume",
        description="Playback volume multiplier for the transmission-off sound",
        default=0.1,
        min=0.0,
        soft_max=1.0,
    )
    sound_on_high_rpm: IntProperty(
        name="RPM",
        description="Reference engine speed for pitching the high-RPM throttle-on sample",
        default=1000,
        min=0,
    )
    sound_on_high_volume: FloatProperty(
        name="Volume",
        description="Playback volume multiplier for the high-RPM throttle-on sound",
        default=0.5,
        min=0.0,
        soft_max=1.0,
    )
    sound_on_low_rpm: IntProperty(
        name="RPM",
        description="Reference engine speed for pitching the low-RPM throttle-on sample",
        default=1000,
        min=0,
    )
    sound_on_low_volume: FloatProperty(
        name="Volume",
        description="Playback volume multiplier for the low-RPM throttle-on sound",
        default=0.4,
        min=0.0,
        soft_max=1.0,
    )
    sound_off_high_rpm: IntProperty(
        name="RPM",
        description="Reference engine speed for pitching the high-RPM throttle-off sample",
        default=1000,
        min=0,
    )
    sound_off_high_volume: FloatProperty(
        name="Volume",
        description="Playback volume multiplier for the high-RPM throttle-off sound",
        default=0.3,
        min=0.0,
        soft_max=1.0,
    )
    sound_off_low_rpm: IntProperty(
        name="RPM",
        description="Reference engine speed for pitching the low-RPM throttle-off sample",
        default=1000,
        min=0,
    )
    sound_off_low_volume: FloatProperty(
        name="Volume",
        description="Playback volume multiplier for the low-RPM throttle-off sound",
        default=0.3,
        min=0.0,
        soft_max=1.0,
    )
    sound_limiter_volume: FloatProperty(
        name="Volume",
        description="Playback volume multiplier for the rev-limiter sound",
        default=0.4,
        min=0.0,
        soft_max=1.0,
    )
    sound_turbo_volume: FloatProperty(
        name="Volume",
        description="Playback volume multiplier for the turbo sound",
        default=0.6,
        min=0.0,
        soft_max=1.0,
    )


def clear_configuration_settings(settings):
    center_of_mass = settings.center_of_mass_object
    if center_of_mass and center_of_mass.get(CENTER_OF_MASS_HELPER_PROP):
        bpy.data.objects.remove(center_of_mass, do_unlink=True)
    for point in settings.down_force_points:
        helper = point.object_ref
        if helper and helper.get(DOWNFORCE_HELPER_PROP):
            bpy.data.objects.remove(helper, do_unlink=True)
    settings.is_configured = False
    settings.car_id = ""
    settings.package_version = "1"
    settings.display_name = ""
    settings.max_texture_size = str(DEFAULT_MAX_TEXTURE_SIZE)
    settings.optimize_color_textures = True
    settings.jpeg_quality = DEFAULT_JPEG_QUALITY
    settings.car_class = ""
    settings.vehicle_tag_tarmac = False
    settings.vehicle_tag_offroad = False
    settings.abs_max_level = 5
    settings.esc_max_level = 5
    settings.traction_control_max_level = 5
    settings.car_root_object = None
    settings.ghost_enabled = False
    settings.ghost_root_object = None
    settings.armature_enabled = False
    settings.armature_object = None
    settings.armature_joints.clear()
    for group, key, _steering in WHEEL_KEYS:
        wheel = ghost_wheel_settings(settings, group, key)
        for _role, prop in GHOST_WHEEL_ROLES:
            setattr(wheel, prop, None)
    settings.center_of_mass_object = None
    settings.steering_wheel_object = None
    settings.steering_wheel_spin_axis = "y"
    settings.max_degrees_of_rotation = 540.0
    settings.body_colors.clear()
    settings.active_body_color_index = 0
    settings.headlights_material = None
    settings.brake_lights_material = None
    settings.reverse_lights_material = None
    settings.dashboard_screen_object = None
    settings.colliders.clear()
    settings.down_force_points.clear()
    settings.wheels.clear()
    settings.presets.clear()
    settings.active_preset_index = 0
    settings.preset_schema_version = 0
    settings.down_force = 0.0
    settings.air_drag = 0.0
    settings.abs = 0.0
    settings.esc = 0.0
    settings.traction_control = 0.0
    settings.max_steering_angle = 1.0
    settings.use_custom_sounds = False
    settings.sound_pitch_offset = 0
    settings.drive = "awd"
    settings.hp = 1.0
    settings.max_rpm = 1
    settings.idle_rpm = 1
    settings.redline_rpm = 1
    settings.rev_limit = 1
    settings.engine_inertia = 0.01
    settings.engine_braking = 0.2
    settings.engine_friction_torque = 0.0
    settings.clutch_response = 0.0
    settings.shift_cooldown = 0.0
    settings.auto_blip = False
    settings.auto_blip_duration = 0.0
    settings.turbo_enabled = False
    settings.turbo_boost = 1.0
    settings.max_torque = 1.0
    settings.torque_factor = 0.01
    for rpm in (1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000):
        setattr(settings, f"torque_{rpm}", 0.0)
    for prefix in CAMERA_PREFIXES:
        setattr(settings, f"{prefix}_camera_object", None)
        setattr(settings, f"{prefix}_fov", 0.0)
        setattr(settings, f"{prefix}_target_distance", 0.01)
    for slot, meta in SOUND_SLOTS.items():
        setattr(settings, f"sound_{slot}_enabled", True)
        setattr(settings, f"sound_{slot}", "")
        setattr(settings, f"sound_{slot}_volume", meta["volume"])
        if slot in SOUND_RPM_SLOTS:
            setattr(settings, f"sound_{slot}_rpm", meta["rpm"])
    settings.guide_length = 4.5
    settings.guide_width = 2.0
    settings.guide_wheelbase = 2.7
    settings.guide_track_width = 1.65
    remove_size_guide()


def initialize_configuration_settings(settings):
    clear_configuration_settings(settings)
    settings.is_configured = True
    settings.car_id = "my_car"
    settings.package_version = "1"
    settings.display_name = "My Car"
    settings.max_texture_size = str(DEFAULT_MAX_TEXTURE_SIZE)
    settings.optimize_color_textures = True
    settings.jpeg_quality = DEFAULT_JPEG_QUALITY
    settings.car_class = "GT"
    settings.vehicle_tag_tarmac = True
    settings.vehicle_tag_offroad = True
    settings.abs_max_level = 5
    settings.esc_max_level = 5
    settings.traction_control_max_level = 5
    settings.down_force = 3000.0
    settings.air_drag = 0.5
    settings.abs = 1.0
    settings.esc = 0.0
    settings.traction_control = 1.0
    settings.max_steering_angle = 50.0
    settings.max_degrees_of_rotation = 540.0
    settings.drive = "awd"
    settings.hp = 590.0
    settings.max_rpm = 8000
    settings.idle_rpm = 1000
    settings.redline_rpm = 7000
    settings.rev_limit = 7900
    settings.engine_inertia = 0.2
    settings.engine_braking = 0.2
    settings.engine_friction_torque = 70.0
    settings.clutch_response = 12.0
    settings.shift_cooldown = 0.0
    settings.auto_blip = True
    settings.auto_blip_duration = 0.2
    settings.turbo_enabled = True
    settings.turbo_boost = 1.35
    settings.max_torque = 590.0
    settings.torque_factor = 1.0
    for rpm, value in {
        1000: 422.292,
        2000: 506.974,
        3000: 565.453,
        4000: 590.0,
        5000: 586.53,
        6000: 564.822,
        7000: 523.597,
        8000: 460.2,
    }.items():
        setattr(settings, f"torque_{rpm}", value)
    settings.chase_fov = 39.500591632003015
    settings.cockpit_fov = 32.268804142808847
    settings.hood_fov = 44.095897188516894
    settings.roof_fov = 44.095897188516894
    settings.chase_target_distance = 5.0
    settings.cockpit_target_distance = 1.0
    settings.hood_target_distance = 2.0
    settings.roof_target_distance = 2.0
    reset_torque_curve_node()
    ensure_default_wheels(settings)
    ensure_default_presets(settings)
    preset = active_preset(settings)
    if preset:
        preset.front.caster = 6.0
        preset.rear.caster = 0.0
        settings.preset_schema_version = 8
    create_size_guide(settings)


def wheel_config(wheel):
    return {
        "steering": bool(wheel.steering),
        "mount": {
            "obj": object_config_name(wheel.suspension_ref),
        },
        "joint": {
            "obj": object_config_name(wheel.hub_ref),
        },
        "spin": {
            "obj": object_config_name(wheel.wheel_ref),
            "upLocalAxis": BLENDER_AXIS_TO_GAME[wheel.up_local_axis],
            "spinLocalAxis": BLENDER_AXIS_TO_GAME[wheel.spin_local_axis],
            "radius": wheel.radius,
        },
    }


def build_wheels_config(settings):
    ensure_default_wheels(settings)
    wheels = {}
    for wheel in settings.wheels:
        wheels.setdefault(wheel.group, {})[wheel.key] = wheel_config(wheel)
    return wheels


def wheel_preset_config(wheel):
    return {
        "tireType": wheel.tire_type,
        "pressure": wheel.pressure,
        "camber": wheel.camber,
        "caster": wheel.caster,
        "toe": wheel.toe,
        "suspensionOffset": wheel.suspension_offset,
        "suspensionStiffness": wheel.suspension_stiffness,
        "dampingRelaxation": wheel.damping_relaxation,
        "dampingCompression": wheel.damping_compression,
        "maxBrakeForce": wheel.max_brake_force,
        "gripFactor": wheel.grip_factor,
    }


def build_presets_config(settings):
    ensure_default_presets(settings)
    return [
        {
            "id": preset.preset_id,
            "name": preset.display_name,
            "maxSteeringAngle": preset.max_steering_angle,
            "roadWheelCurve": preset.road_wheel_curve,
            "maxDegreesOfRotation": preset.max_degrees_of_rotation,
            "antiRollBars": {
                "front": preset.front_anti_roll_bar_stiffness,
                "rear": preset.rear_anti_roll_bar_stiffness,
            },
            "absLevel": preset.abs_level,
            "escLevel": preset.esc_level,
            "tractionControlLevel": preset.traction_control_level,
            "brakeBias": preset.brake_bias,
            "gearing": {
                "finalDriveRatio": preset.final_drive_ratio,
                "gearRatios": {
                    **{"0": 0, "-1": preset.reverse_ratio},
                    **{
                        str(index): getattr(preset, f"gear_{index}")
                        for index in range(1, preset.forward_gear_count + 1)
                    },
                },
            },
            "wheels": {
                group: {
                    key: wheel_preset_config(getattr(preset, group))
                    for key in ("l", "r")
                }
                for group in ("front", "rear")
            },
        }
        for preset in settings.presets
    ]


def sample_torque_curve(settings):
    max_rpm = max(1000, settings.max_rpm)
    sample_step = 1000
    torque_curve = {}

    sample_rpms = list(range(sample_step, max_rpm + 1, sample_step))
    if sample_rpms[-1] != max_rpm:
        sample_rpms.append(max_rpm)
    for rpm in sample_rpms:
        torque_curve[str(rpm)] = round(max(0, evaluate_torque_curve(rpm / max_rpm) * settings.max_torque), 3)

    return torque_curve


def default_torque_points():
    return [
        (0.125, 422.0 / 590.0),
        (0.250, 506.0 / 590.0),
        (0.375, 565.0 / 590.0),
        (0.500, 590.0 / 590.0),
        (0.625, 586.0 / 590.0),
        (0.750, 564.0 / 590.0),
        (0.875, 523.0 / 590.0),
        (1.000, 460.0 / 590.0),
    ]


def get_torque_curve_node(create=True):
    tree = bpy.data.node_groups.get(TORQUE_CURVE_NODE_GROUP)
    if tree is None:
        if not create:
            return None
        tree = bpy.data.node_groups.new(name=TORQUE_CURVE_NODE_GROUP, type="ShaderNodeTree")

    if create:
        # The curve node group is an internal data store and is not linked to a
        # material. Keep it when saving the blend file despite having no users.
        tree.use_fake_user = True

    node = tree.nodes.get(TORQUE_CURVE_NODE)
    if node is None:
        if not create:
            return None
        node = tree.nodes.new("ShaderNodeFloatCurve")
        node.name = TORQUE_CURVE_NODE
        node.label = TORQUE_CURVE_NODE
        reset_torque_curve_node(node)

    return node


def reset_torque_curve_node(node=None):
    node = node or get_torque_curve_node()
    mapping = node.mapping
    mapping.initialize()
    mapping.use_clip = True
    mapping.clip_min_x = 0.0
    mapping.clip_max_x = 1.0
    mapping.clip_min_y = 0.0
    mapping.clip_max_y = 1.0

    curve = mapping.curves[0]
    while len(curve.points) > 2:
        curve.points.remove(curve.points[-2])

    points = default_torque_points()
    curve.points[0].location = points[0]
    curve.points[-1].location = points[-1]
    for x, y in points[1:-1]:
        curve.points.new(x, y)

    mapping.update()


def evaluate_torque_curve(rpm_ratio):
    node = get_torque_curve_node()
    mapping = node.mapping
    curve = mapping.curves[0]
    rpm_ratio = max(0.0, min(1.0, rpm_ratio))
    result = mapping.evaluate(curve, rpm_ratio)
    return max(0.0, min(1.0, result))


def initialize_car_exporter_defaults():
    try:
        get_torque_curve_node()
        for scene in bpy.data.scenes:
            if hasattr(scene, "car_exporter"):
                ensure_default_wheels(scene.car_exporter)
                ensure_default_presets(scene.car_exporter)
    except AttributeError:
        return 0.2
    return None


def apply_imported_torque_curve(settings, torque_curve):
    node = get_torque_curve_node()
    mapping = node.mapping
    mapping.initialize()
    mapping.use_clip = True
    mapping.clip_min_x = 0.0
    mapping.clip_max_x = 1.0
    mapping.clip_min_y = 0.0
    mapping.clip_max_y = 1.0

    curve = mapping.curves[0]
    while len(curve.points) > 2:
        curve.points.remove(curve.points[-2])

    max_rpm = max(1000, settings.max_rpm)
    max_torque = max(settings.max_torque, 1.0)
    points = [
        (min(max(int(rpm) / max_rpm, 0.0), 1.0), min(max(float(torque) / max_torque, 0.0), 1.0))
        for rpm, torque in sorted(torque_curve.items(), key=lambda item: int(item[0]))
    ]
    if len(points) < 2:
        points = default_torque_points()

    curve.points[0].location = points[0]
    curve.points[-1].location = points[-1]
    for x, y in points[1:-1]:
        curve.points.new(x, y)

    mapping.update()


def build_manifest(settings):
    sounds = {"pitchOffset": settings.sound_pitch_offset}
    if settings.use_custom_sounds:
        for slot, meta in SOUND_SLOTS.items():
            if not getattr(settings, f"sound_{slot}_enabled"):
                sounds[slot] = None
                continue
            source_path = getattr(settings, f"sound_{slot}")
            if not source_path:
                continue
            source_name = Path(abspath(source_path)).name
            sounds[slot] = {
                "source": source_name,
                "rpm": getattr(settings, f"sound_{slot}_rpm") if slot in SOUND_RPM_SLOTS else meta["rpm"],
                "loop": meta["loop"],
                "volume": getattr(settings, f"sound_{slot}_volume"),
            }

    lights = {
        key: {"material": material.name}
        for key, material in (
            ("headlights", settings.headlights_material),
            ("brakeLights", settings.brake_lights_material),
            ("reverseLights", settings.reverse_lights_material),
        )
        if material
    }

    dashboard = None
    if settings.dashboard_screen_object:
        dashboard = {
            "screen": {
                "obj": object_config_name(settings.dashboard_screen_object),
            },
        }

    body = {
        "obj": object_config_name(settings.car_root_object),
        "centerOfMass": object_config_name(settings.center_of_mass_object),
        "colliders": [
            {
                "obj": object_config_name(collider.object_ref),
                "type": collider.collider_type,
                "mass": collider.mass,
            }
            for collider in settings.colliders
        ],
        "downForcePoints": [
            {
                "name": downforce_point_display_name(point, index),
                "position": blender_position_to_game(
                    relative_to_car(settings.car_root_object, point.object_ref)
                ),
                "maxForce": point.max_force,
            }
            for index, point in enumerate(settings.down_force_points)
        ],
        "airDrag": settings.air_drag,
    }
    if settings.body_colors:
        body["colors"] = [
            {
                "name": body_color.display_name.strip(),
                "material": body_color.material.name,
            }
            for body_color in settings.body_colors
        ]

    manifest = {
        "version": 8,
        "id": settings.car_id,
        "packageVersion": settings.package_version,
        "model": f"{settings.car_id}.glb",
        "ghost": build_ghost_config(settings),
        "displayName": settings.display_name,
        "class": settings.car_class,
        "trackTypes": [
            tag
            for tag, enabled in (
                ("tarmac", settings.vehicle_tag_tarmac),
                ("offroad", settings.vehicle_tag_offroad),
            )
            if enabled
        ],
        "type": "car",
        "engine": {
            "hp": settings.hp,
            "drive": settings.drive,
            "maxRPM": settings.max_rpm,
            "idleRPM": settings.idle_rpm,
            "redlineRPM": settings.redline_rpm,
            "revLimit": settings.rev_limit,
            "inertia": settings.engine_inertia,
            "engineBraking": settings.engine_braking,
            "frictionTorque": settings.engine_friction_torque,
            "clutchResponse": settings.clutch_response,
            "shiftCooldown": settings.shift_cooldown,
            "autoBlip": settings.auto_blip,
            "autoBlipDuration": settings.auto_blip_duration,
            "torqueFactor": settings.torque_factor,
            "torqueCurve": sample_torque_curve(settings),
            "turbo": {
                "enabled": settings.turbo_enabled,
                "boost": settings.turbo_boost,
                "load": 0.0,
            },
        },
        "body": body,
        "wheels": build_wheels_config(settings),
        "driverAssists": {
            "abs": {"maxLevel": settings.abs_max_level},
            "esc": {"maxLevel": settings.esc_max_level},
            "tractionControl": {"maxLevel": settings.traction_control_max_level},
        },
        "presets": build_presets_config(settings),
        "steeringWheel": {
            "obj": object_config_name(settings.steering_wheel_object),
            "spinLocalAxis": BLENDER_AXIS_TO_GAME[settings.steering_wheel_spin_axis],
        },
        "lights": lights,
        "cameras": {
            "chase_cam": {
                "obj": object_config_name(settings.chase_camera_object),
                "fov": camera_fov(settings, "chase"),
            },
            "cockpit_cam": {
                "obj": object_config_name(settings.cockpit_camera_object),
                "fov": camera_fov(settings, "cockpit"),
            },
            "hood_cam": {
                "obj": object_config_name(settings.hood_camera_object),
                "fov": camera_fov(settings, "hood"),
            },
            "roof_cam": {
                "obj": object_config_name(settings.roof_camera_object),
                "fov": camera_fov(settings, "roof"),
            },
        },
        "sounds": sounds,
    }
    if dashboard:
        manifest["dashboard"] = dashboard
    armature = build_armature_config(settings)
    if armature is not None:
        manifest["armature"] = armature
    return manifest


def show_validation_popup(context, errors, warnings):
    title = "Car Validation Failed" if errors else "Car Validation Passed"
    icon = "ERROR" if errors else ("ERROR" if warnings else "CHECKMARK")

    def draw_popup(self, _context):
        layout = self.layout
        if errors:
            layout.label(text=f"Errors: {len(errors)}")
            for message in errors:
                layout.label(text=message, icon="ERROR")
        else:
            layout.label(text="No errors", icon="CHECKMARK")

        if warnings:
            layout.separator()
            layout.label(text=f"Warnings: {len(warnings)}")
            for message in warnings:
                layout.label(text=message, icon="ERROR")

    context.window_manager.popup_menu(draw_popup, title=title, icon=icon)


class CAR_EXPORTER_OT_validate_car(Operator):
    bl_idname = "car_exporter.validate_car"
    bl_label = "Validate Car"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = scene_settings(context)
        errors, warnings = validate_scene(settings)
        for msg in warnings:
            self.report({"WARNING"}, msg)
        for msg in errors:
            self.report({"ERROR"}, msg)
        if errors:
            show_validation_popup(context, errors, warnings)
            return {"CANCELLED"}
        show_validation_popup(context, errors, warnings)
        self.report({"INFO"}, f"Car validation passed with {len(warnings)} warning(s)")
        return {"FINISHED"}


class CAR_EXPORTER_OT_estimate_brake_force(Operator):
    bl_idname = "car_exporter.estimate_brake_force"
    bl_label = "Estimate Brake Force"
    bl_description = "Estimate wheel-lock brake force at maximum speed on dry tarmac with ABS off"
    bl_options = {"REGISTER", "UNDO"}

    axle: EnumProperty(
        items=(("front", "Front", ""), ("rear", "Rear", "")),
        options={"HIDDEN"},
    )

    def execute(self, context):
        settings = scene_settings(context)
        ensure_default_wheels(settings)
        ensure_default_presets(settings)
        preset = active_preset(settings)
        if not preset:
            self.report({"ERROR"}, "Add a car preset before estimating brake force")
            return {"CANCELLED"}
        try:
            front_kg, rear_kg, deceleration_g = calculate_max_speed_brake_force_kg(settings, preset)
        except ValueError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        force_kg = front_kg if self.axle == "front" else rear_kg
        getattr(preset, self.axle).max_brake_force_kg = force_kg
        self.report(
            {"INFO"},
            f"{self.axle.title()} brake force set to {force_kg:.0f} kg ({deceleration_g:.2f} g estimate)",
        )
        return {"FINISHED"}


class CAR_EXPORTER_OT_add_armature_joint(Operator):
    bl_idname = "car_exporter.add_armature_joint"
    bl_label = "Add Joint"
    bl_description = "Add a mapping for an existing armature bone"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = scene_settings(context)
        rig = settings.armature_object
        if not settings.armature_enabled or not rig or rig.type != "ARMATURE":
            self.report({"ERROR"}, "Enable Armature and select an armature first")
            return {"CANCELLED"}
        settings.armature_joints.add()
        return {"FINISHED"}


class CAR_EXPORTER_OT_remove_armature_joint(Operator):
    bl_idname = "car_exporter.remove_armature_joint"
    bl_label = "Remove Joint"
    bl_options = {"REGISTER", "UNDO"}

    index: IntProperty()

    def execute(self, context):
        joints = scene_settings(context).armature_joints
        if not 0 <= self.index < len(joints):
            return {"CANCELLED"}
        joints.remove(self.index)
        return {"FINISHED"}


class CAR_EXPORTER_OT_add_collider(Operator):
    bl_idname = "car_exporter.add_collider"
    bl_label = "Add Collider"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = scene_settings(context)
        collider = settings.colliders.add()
        collider.object_ref = None
        collider.collider_type = "trimesh"
        collider.mass = 0.0
        return {"FINISHED"}


class CAR_EXPORTER_OT_remove_collider(Operator):
    bl_idname = "car_exporter.remove_collider"
    bl_label = "Remove Collider"
    bl_options = {"REGISTER", "UNDO"}

    index: IntProperty()

    def execute(self, context):
        settings = scene_settings(context)
        if 0 <= self.index < len(settings.colliders):
            collider = settings.colliders[self.index]
            collider.object_ref = None
            collider.collider_type = "trimesh"
            collider.mass = 0.0
            settings.colliders.remove(self.index)
        return {"FINISHED"}


class CAR_EXPORTER_OT_add_center_of_mass(Operator):
    bl_idname = "car_exporter.add_center_of_mass"
    bl_label = "Add Center of Mass"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = scene_settings(context)
        if not settings.car_root_object:
            self.report({"ERROR"}, "Select Car Root before adding Center of Mass")
            return {"CANCELLED"}
        if settings.center_of_mass_object:
            self.report({"ERROR"}, "Center of Mass already exists")
            return {"CANCELLED"}
        helper = create_car_helper(
            context,
            settings,
            "centerOfMass" if not bpy.data.objects.get("centerOfMass") else next_helper_name("centerOfMass"),
            "SPHERE",
            0.18,
            CENTER_OF_MASS_HELPER_PROP,
        )
        helper.hide_render = False
        settings.center_of_mass_object = helper
        context.view_layer.objects.active = helper
        helper.select_set(True)
        return {"FINISHED"}


class CAR_EXPORTER_OT_remove_center_of_mass(Operator):
    bl_idname = "car_exporter.remove_center_of_mass"
    bl_label = "Remove Center of Mass"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = scene_settings(context)
        helper = settings.center_of_mass_object
        settings.center_of_mass_object = None
        if helper and helper.get(CENTER_OF_MASS_HELPER_PROP):
            bpy.data.objects.remove(helper, do_unlink=True)
        return {"FINISHED"}


class CAR_EXPORTER_OT_add_downforce_point(Operator):
    bl_idname = "car_exporter.add_downforce_point"
    bl_label = "Add Downforce Point"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = scene_settings(context)
        if not settings.car_root_object:
            self.report({"ERROR"}, "Select Car Root before adding a downforce point")
            return {"CANCELLED"}
        helper = create_car_helper(
            context,
            settings,
            next_helper_name("downforce"),
            "SINGLE_ARROW",
            0.4,
            DOWNFORCE_HELPER_PROP,
        )
        helper.rotation_euler = (math.pi, 0.0, 0.0)
        display_name = next_downforce_point_display_name(settings)
        point = settings.down_force_points.add()
        point.display_name = display_name
        point.object_ref = helper
        point.max_force = 3000.0
        context.view_layer.objects.active = helper
        helper.select_set(True)
        return {"FINISHED"}


class CAR_EXPORTER_OT_remove_downforce_point(Operator):
    bl_idname = "car_exporter.remove_downforce_point"
    bl_label = "Remove Downforce Point"
    bl_options = {"REGISTER", "UNDO"}

    index: IntProperty()

    def execute(self, context):
        settings = scene_settings(context)
        if 0 <= self.index < len(settings.down_force_points):
            helper = settings.down_force_points[self.index].object_ref
            settings.down_force_points.remove(self.index)
            if helper and helper.get(DOWNFORCE_HELPER_PROP):
                bpy.data.objects.remove(helper, do_unlink=True)
        return {"FINISHED"}


class CAR_EXPORTER_UL_body_colors(bpy.types.UIList):
    def draw_item(self, _context, layout, _data, item, _icon, _active_data, _active_propname, index):
        row = layout.row(align=True)
        row.label(
            text=item.display_name or f"Color {index + 1}",
            icon="CHECKMARK" if index == 0 else "MATERIAL",
        )
        row.label(text=item.material.name if item.material else "No material")


class CAR_EXPORTER_OT_add_body_color(Operator):
    bl_idname = "car_exporter.add_body_color"
    bl_label = "Add Body Color"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = scene_settings(context)
        existing_names = {body_color.display_name.strip().casefold() for body_color in settings.body_colors}
        if not settings.body_colors:
            display_name = "Default"
        else:
            number = 2
            while f"color {number}" in existing_names:
                number += 1
            display_name = f"Color {number}"
        body_color = settings.body_colors.add()
        body_color.display_name = display_name
        body_color.material = None
        settings.active_body_color_index = len(settings.body_colors) - 1
        return {"FINISHED"}


class CAR_EXPORTER_OT_remove_body_color(Operator):
    bl_idname = "car_exporter.remove_body_color"
    bl_label = "Remove Body Color"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = scene_settings(context)
        index = settings.active_body_color_index
        if not (0 <= index < len(settings.body_colors)):
            return {"CANCELLED"}
        settings.body_colors[index].material = None
        settings.body_colors.remove(index)
        settings.active_body_color_index = min(index, max(len(settings.body_colors) - 1, 0))
        return {"FINISHED"}


class CAR_EXPORTER_OT_move_body_color(Operator):
    bl_idname = "car_exporter.move_body_color"
    bl_label = "Move Body Color"
    bl_description = "Reorder body colors; index 0 is the default"
    bl_options = {"REGISTER", "UNDO"}

    direction: EnumProperty(items=(("UP", "Up", ""), ("DOWN", "Down", "")))

    def execute(self, context):
        settings = scene_settings(context)
        index = settings.active_body_color_index
        target = index - 1 if self.direction == "UP" else index + 1
        if not (0 <= index < len(settings.body_colors) and 0 <= target < len(settings.body_colors)):
            return {"CANCELLED"}
        settings.body_colors.move(index, target)
        settings.active_body_color_index = target
        return {"FINISHED"}


class CAR_EXPORTER_UL_presets(bpy.types.UIList):
    def draw_item(self, _context, layout, _data, item, _icon, _active_data, _active_propname, index):
        row = layout.row(align=True)
        row.label(
            text=item.display_name or item.preset_id,
            icon="CHECKMARK" if index == 0 else "OUTLINER_COLLECTION",
        )
        row.label(text=item.preset_id)


class CAR_EXPORTER_OT_add_preset(Operator):
    bl_idname = "car_exporter.add_preset"
    bl_label = "Add Car Preset"
    bl_description = "Add a car preset with default handling values"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = scene_settings(context)
        if len(settings.presets) > 0:
            ensure_default_presets(settings)
        first_preset = len(settings.presets) == 0
        preset_id = "default" if first_preset else next_preset_id(settings)
        preset = settings.presets.add()
        preset.preset_id = preset_id
        preset.display_name = "Default" if first_preset else f"Preset {len(settings.presets)}"
        ensure_preset_wheels(preset)
        apply_preset_values(default_preset_values(settings), preset)
        settings.preset_schema_version = 8
        settings.active_preset_index = len(settings.presets) - 1
        return {"FINISHED"}


class CAR_EXPORTER_OT_remove_preset(Operator):
    bl_idname = "car_exporter.remove_preset"
    bl_label = "Remove Car Preset"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = scene_settings(context)
        if len(settings.presets) <= 1:
            self.report({"ERROR"}, "At least one car preset is required")
            return {"CANCELLED"}
        index = settings.active_preset_index
        if not (0 <= index < len(settings.presets)):
            return {"CANCELLED"}
        settings.presets.remove(index)
        settings.active_preset_index = min(index, len(settings.presets) - 1)
        return {"FINISHED"}


class CAR_EXPORTER_OT_move_preset(Operator):
    bl_idname = "car_exporter.move_preset"
    bl_label = "Move Car Preset"
    bl_options = {"REGISTER", "UNDO"}

    direction: EnumProperty(items=(("UP", "Up", ""), ("DOWN", "Down", "")))

    def execute(self, context):
        settings = scene_settings(context)
        index = settings.active_preset_index
        target = index - 1 if self.direction == "UP" else index + 1
        if not (0 <= index < len(settings.presets) and 0 <= target < len(settings.presets)):
            return {"CANCELLED"}
        settings.presets.move(index, target)
        settings.active_preset_index = target
        return {"FINISHED"}


class CAR_EXPORTER_OT_tooltip_label(Operator):
    bl_idname = "car_exporter.tooltip_label"
    bl_label = ""
    bl_options = {"INTERNAL"}

    tooltip: StringProperty()

    @classmethod
    def description(cls, _context, properties):
        return properties.tooltip

    def execute(self, _context):
        return {"FINISHED"}


def add_wheel_from_config(settings, group, key, data=None):
    data = data or {}
    mount = data.get("mount", {})
    joint_data = data.get("joint", {})
    spin_data = data.get("spin", {})
    wheel = settings.wheels.add()
    wheel.group = group
    wheel.key = key
    wheel.steering = bool(data.get("steering", group == "front"))
    set_object_pointer(wheel, "suspension_ref", mount.get("obj", ""))
    set_object_pointer(wheel, "hub_ref", joint_data.get("obj", ""))
    set_object_pointer(wheel, "wheel_ref", spin_data.get("obj", ""))
    wheel.up_local_axis = GAME_AXIS_TO_BLENDER.get(tuple(spin_data.get("upLocalAxis", [0, 1, 0])), "z")
    wheel.spin_local_axis = GAME_AXIS_TO_BLENDER.get(tuple(spin_data.get("spinLocalAxis", [1, 0, 0])), "x")
    # Populate legacy storage so schema migration can preserve version 2/3 blend data.
    wheel.suspension_stiffness = mount.get("stiffness", wheel.suspension_stiffness)
    wheel.damping_relaxation = mount.get("dampingRelaxation", wheel.damping_relaxation)
    wheel.damping_compression = mount.get("dampingCompression", wheel.damping_compression)
    wheel.radius = spin_data.get("radius", wheel.radius)
    wheel.max_brake_force = spin_data.get("maxBrakeForce", wheel.max_brake_force)
    wheel.pressure = spin_data.get("pressure", wheel.pressure)
    wheel.camber = spin_data.get("camber", wheel.camber)
    wheel.toe = spin_data.get("toe", wheel.toe)
    wheel.grip_factor = spin_data.get("gripFactor", wheel.grip_factor)
    return wheel


def ensure_default_wheels(settings):
    expected = [(group, key) for group, key, _steering in WHEEL_KEYS]
    current = [(wheel.group, wheel.key) for wheel in settings.wheels]
    if current == expected:
        return

    existing = {
        (wheel.group, wheel.key): {
            "steering": wheel.steering,
            "suspension_ref": wheel.suspension_ref,
            "hub_ref": wheel.hub_ref,
            "wheel_ref": wheel.wheel_ref,
            "up_local_axis": wheel.up_local_axis,
            "spin_local_axis": wheel.spin_local_axis,
            "suspension_stiffness": wheel.suspension_stiffness,
            "damping_relaxation": wheel.damping_relaxation,
            "damping_compression": wheel.damping_compression,
            "radius": wheel.radius,
            "max_brake_force": wheel.max_brake_force,
            "pressure": wheel.pressure,
            "camber": wheel.camber,
            "toe": wheel.toe,
            "grip_factor": wheel.grip_factor,
        }
        for wheel in settings.wheels
    }
    settings.wheels.clear()
    for group, key, steering in WHEEL_KEYS:
        imported = existing.get((group, key))
        if imported:
            wheel = settings.wheels.add()
            wheel.group = group
            wheel.key = key
            wheel.steering = imported["steering"]
            wheel.suspension_ref = imported["suspension_ref"]
            wheel.hub_ref = imported["hub_ref"]
            wheel.wheel_ref = imported["wheel_ref"]
            wheel.up_local_axis = imported["up_local_axis"]
            wheel.spin_local_axis = imported["spin_local_axis"]
            wheel.suspension_stiffness = imported["suspension_stiffness"]
            wheel.damping_relaxation = imported["damping_relaxation"]
            wheel.damping_compression = imported["damping_compression"]
            wheel.radius = imported["radius"]
            wheel.max_brake_force = imported["max_brake_force"]
            wheel.pressure = imported["pressure"]
            wheel.camber = imported["camber"]
            wheel.toe = imported["toe"]
            wheel.grip_factor = imported["grip_factor"]
            continue

        front_wheel = group == "front"
        add_wheel_from_config(settings, group, key, {
            "steering": steering,
            "mount": {
                "stiffness": 80,
                "dampingRelaxation": 2.6,
                "dampingCompression": 2.0,
            },
            "joint": {},
            "spin": {
                "upLocalAxis": [0, 1, 0],
                "spinLocalAxis": [1, 0, 0],
                "radius": 0.3,
                "maxBrakeForce": 1000,
                "pressure": 2.0,
                "camber": -4.0 if front_wheel else -3.0,
                "toe": -0.15 if front_wheel else 0.2,
                "gripFactor": 1.0,
            },
        })


def default_wheel_preset_values(group):
    front_wheel = group == "front"
    return {
        "tire_type": "medium",
        "pressure": 2.0,
        "camber": -4.0 if front_wheel else -3.0,
        "caster": 6.0 if front_wheel else 0.0,
        "toe": -0.15 if front_wheel else 0.2,
        "suspension_offset": 0.0,
        "suspension_stiffness": 80.0,
        "damping_relaxation": 2.6,
        "damping_compression": 2.0,
        "max_brake_force": 1000.0,
        "grip_factor": 1.0,
    }


def default_preset_values(settings):
    return {
        "max_steering_angle": 50.0,
        "road_wheel_curve": 0.5,
        "max_degrees_of_rotation": 540.0,
        "front_anti_roll_bar_stiffness": 15.0,
        "rear_anti_roll_bar_stiffness": 15.0,
        "abs_level": settings.abs_max_level,
        "esc_level": 0,
        "traction_control_level": settings.traction_control_max_level,
        "brake_bias": 0.6,
        "final_drive_ratio": 5.0,
        "reverse_ratio": -3.57,
        "forward_gear_count": 6,
        "gear_ratios": {
            1: 4.08,
            2: 2.7,
            3: 1.9,
            4: 1.4,
            5: 1.06,
            6: 0.85,
            7: 0.70,
            8: 0.58,
            9: 0.50,
            10: 0.44,
            11: 0.40,
            12: 0.36,
            13: 0.33,
            14: 0.30,
            15: 0.28,
        },
        "front": default_wheel_preset_values("front"),
        "rear": default_wheel_preset_values("rear"),
    }


def ensure_preset_wheels(preset, source_wheels=None):
    expected = [(group, key) for group, key, _steering in WHEEL_KEYS]
    current = [(wheel.group, wheel.key) for wheel in preset.wheels]
    if current == expected:
        return

    existing = {
        (wheel.group, wheel.key): {
            "tire_type": wheel.tire_type,
            "pressure": wheel.pressure,
            "camber": wheel.camber,
            "caster": wheel.caster,
            "toe": wheel.toe,
            "suspension_offset": wheel.suspension_offset,
            "suspension_stiffness": wheel.suspension_stiffness,
            "damping_relaxation": wheel.damping_relaxation,
            "damping_compression": wheel.damping_compression,
            "max_brake_force": wheel.max_brake_force,
            "grip_factor": wheel.grip_factor,
        }
        for wheel in preset.wheels
    }
    legacy = {
        (wheel.group, wheel.key): {
            "tire_type": "medium",
            "pressure": wheel.pressure,
            "camber": wheel.camber,
            "caster": 0.0,
            "toe": wheel.toe,
            "suspension_offset": 0.0,
            "suspension_stiffness": wheel.suspension_stiffness,
            "damping_relaxation": wheel.damping_relaxation,
            "damping_compression": wheel.damping_compression,
            "max_brake_force": wheel.max_brake_force,
            "grip_factor": wheel.grip_factor,
        }
        for wheel in (source_wheels or [])
    }
    preset.wheels.clear()
    for group, key, _steering in WHEEL_KEYS:
        values = existing.get((group, key)) or legacy.get((group, key)) or default_wheel_preset_values(group)
        wheel = preset.wheels.add()
        wheel.group = group
        wheel.key = key
        wheel.tire_type = values["tire_type"]
        wheel.pressure = values["pressure"]
        wheel.camber = values["camber"]
        wheel.caster = values["caster"]
        wheel.toe = values["toe"]
        wheel.suspension_offset = values["suspension_offset"]
        wheel.suspension_stiffness = values["suspension_stiffness"]
        wheel.damping_relaxation = values["damping_relaxation"]
        wheel.damping_compression = values["damping_compression"]
        wheel.max_brake_force = values["max_brake_force"]
        wheel.grip_factor = values["grip_factor"]


def wheel_preset_values(source):
    return {
        field: getattr(source, field)
        for field in (
            "tire_type",
            "pressure",
            "camber",
            "caster",
            "toe",
            "suspension_offset",
            "suspension_stiffness",
            "damping_relaxation",
            "damping_compression",
            "max_brake_force",
            "grip_factor",
        )
    }


def apply_wheel_preset_values(values, target):
    for field, value in values.items():
        setattr(target, field, value)


def copy_wheel_preset_values(source, target):
    apply_wheel_preset_values(wheel_preset_values(source), target)


def ensure_default_presets(settings):
    if len(settings.presets) == 0:
        preset = settings.presets.add()
        preset.preset_id = "default"
        preset.display_name = "Default"
        ensure_preset_wheels(preset, settings.wheels)
        settings.active_preset_index = 0
    for preset in settings.presets:
        ensure_preset_wheels(preset)
    if settings.preset_schema_version < 2:
        shared_wheels = {(wheel.group, wheel.key): wheel for wheel in settings.wheels}
        for preset in settings.presets:
            for wheel in preset.wheels:
                shared = shared_wheels.get((wheel.group, wheel.key))
                if not shared:
                    continue
                wheel.suspension_stiffness = shared.suspension_stiffness
                wheel.damping_relaxation = shared.damping_relaxation
                wheel.damping_compression = shared.damping_compression
    if settings.preset_schema_version < 3:
        for preset in settings.presets:
            for group in ("front", "rear"):
                source = next(
                    (wheel for wheel in preset.wheels if wheel.group == group and wheel.key == "l"),
                    None,
                )
                if source:
                    copy_wheel_preset_values(source, getattr(preset, group))
        settings.preset_schema_version = 3
    if settings.preset_schema_version < 4:
        shared_wheels = {(wheel.group, wheel.key): wheel for wheel in settings.wheels}
        for preset in settings.presets:
            preset.max_steering_angle = settings.max_steering_angle
            for group in ("front", "rear"):
                source = shared_wheels.get((group, "l")) or shared_wheels.get((group, "r"))
                if not source:
                    continue
                target = getattr(preset, group)
                target.max_brake_force = source.max_brake_force
                target.grip_factor = source.grip_factor
        settings.preset_schema_version = 4
    if settings.preset_schema_version < 5:
        for preset in settings.presets:
            preset.max_degrees_of_rotation = settings.max_degrees_of_rotation
            preset.abs_level = settings.abs_max_level
            preset.esc_level = 0
            preset.traction_control_level = settings.traction_control_max_level
            preset.brake_bias = 0.6
        settings.preset_schema_version = 5
    if settings.preset_schema_version < 6:
        for preset in settings.presets:
            preset.front.caster = 0.0
            preset.rear.caster = 0.0
        settings.preset_schema_version = 6
    if settings.preset_schema_version < 7:
        for preset in settings.presets:
            preset.abs_level = settings.abs_max_level
            preset.esc_level = 0
            preset.traction_control_level = settings.traction_control_max_level
        settings.preset_schema_version = 7
    if settings.preset_schema_version < 8:
        for preset in settings.presets:
            preset.front_anti_roll_bar_stiffness = 15.0
            preset.rear_anti_roll_bar_stiffness = 15.0
        settings.preset_schema_version = 8
    settings.active_preset_index = min(
        max(settings.active_preset_index, 0),
        len(settings.presets) - 1,
    )


def active_preset(settings):
    if 0 <= settings.active_preset_index < len(settings.presets):
        return settings.presets[settings.active_preset_index]
    return None


def next_preset_id(settings):
    existing = {preset.preset_id for preset in settings.presets}
    index = 1
    while f"preset_{index}" in existing:
        index += 1
    return f"preset_{index}"


def apply_preset_values(values, target):
    for field in (
        "max_steering_angle",
        "road_wheel_curve",
        "max_degrees_of_rotation",
        "front_anti_roll_bar_stiffness",
        "rear_anti_roll_bar_stiffness",
        "abs_level",
        "esc_level",
        "traction_control_level",
        "brake_bias",
        "final_drive_ratio",
        "reverse_ratio",
        "forward_gear_count",
    ):
        setattr(target, field, values[field])
    for index, ratio in values["gear_ratios"].items():
        setattr(target, f"gear_{index}", ratio)
    apply_wheel_preset_values(values["front"], target.front)
    apply_wheel_preset_values(values["rear"], target.rear)


def schedule_defaults_initialization():
    if not bpy.app.timers.is_registered(initialize_car_exporter_defaults):
        bpy.app.timers.register(initialize_car_exporter_defaults, first_interval=0.1)


@persistent
def initialize_car_exporter_defaults_after_load(_dummy):
    schedule_defaults_initialization()



class CAR_EXPORTER_OT_reset_torque_curve(Operator):
    bl_idname = "car_exporter.reset_torque_curve"
    bl_label = "Reset Torque Curve"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        reset_torque_curve_node()
        return {"FINISHED"}


class CAR_EXPORTER_OT_create_configuration(Operator):
    bl_idname = "car_exporter.create_configuration"
    bl_label = "Create Configuration"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = scene_settings(context)
        initialize_configuration_settings(settings)
        return {"FINISHED"}


class CAR_EXPORTER_OT_remove_configuration(Operator):
    bl_idname = "car_exporter.remove_configuration"
    bl_label = "Remove Configuration"
    bl_options = {"REGISTER", "UNDO"}

    def invoke(self, context, _event):
        return context.window_manager.invoke_confirm(self, _event)

    def execute(self, context):
        settings = scene_settings(context)
        clear_configuration_settings(settings)
        return {"FINISHED"}


def create_body_material_export_carrier(context):
    settings = scene_settings(context)
    excluded = set(excluded_ghost_objects(settings))
    car_objects = [obj for obj in hierarchy_objects(settings.car_root_object) if obj not in excluded]
    carrier_materials = [
        body_color.material
        for body_color in settings.body_colors
        if body_color.material and not material_is_assigned_to_geometry(body_color.material, car_objects)
    ]
    if not carrier_materials:
        return None

    vertices = []
    faces = []
    for _material in carrier_materials:
        vertex_index = len(vertices)
        vertices.extend(((0.0, 0.0, 0.0),) * 3)
        faces.append((vertex_index, vertex_index + 1, vertex_index + 2))

    mesh = bpy.data.meshes.new("VECTORG_BODY_MATERIALS")
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    for material in carrier_materials:
        mesh.materials.append(material)
    for index, polygon in enumerate(mesh.polygons):
        polygon.material_index = index

    carrier = bpy.data.objects.new("VECTORG_BODY_MATERIALS", mesh)
    context.scene.collection.objects.link(carrier)
    default_material = settings.body_colors[0].material if settings.body_colors else None
    carrier_parent = object_with_assigned_material(default_material, car_objects)
    if carrier_parent:
        carrier.parent = carrier_parent
        carrier.location = carrier_parent.data.vertices[0].co
    elif settings.car_root_object:
        carrier.parent = settings.car_root_object
        carrier.location = (0.0, 0.0, 0.0)
    return carrier


def remove_body_material_export_carrier(carrier):
    if not carrier:
        return
    mesh = carrier.data
    bpy.data.objects.remove(carrier, do_unlink=True)
    if mesh and mesh.users == 0:
        bpy.data.meshes.remove(mesh)


def export_car_glb(context, filepath, max_texture_size, optimize_color_textures, jpeg_quality):
    carrier = create_body_material_export_carrier(context)
    restored_nodes = []
    temp_images = []
    try:
        export_objects = list(context.scene.objects)
        restored_nodes, temp_images = apply_export_texture_optimization(
            export_objects,
            max_texture_size,
            optimize_color_textures,
            Path(filepath).parent,
            jpeg_quality,
        )
        result = bpy.ops.export_scene.gltf(
            filepath=str(filepath),
            export_format="GLB",
            use_selection=False,
            export_apply=True,
            export_cameras=True,
            **gltf_image_export_options(jpeg_quality),
            **gltf_armature_export_options(scene_settings(context)),
        )
        if "FINISHED" not in result:
            raise RuntimeError("Blender glTF export did not finish")
    finally:
        restore_export_textures(restored_nodes, temp_images)
        remove_body_material_export_carrier(carrier)


class CAR_EXPORTER_OT_export_car_zip(Operator, ExportHelper):
    bl_idname = "car_exporter.export_car_zip"
    bl_label = "Export Car Zip"
    bl_options = {"REGISTER"}
    filename_ext = ".zip"

    filepath: StringProperty(
        name="Export Zip",
        description="Destination path for the exported vehicle ZIP package",
        subtype="FILE_PATH",
    )
    filter_glob: StringProperty(default="*.zip", options={"HIDDEN"})
    apply_scales_before_export: BoolProperty(
        name="Apply scales",
        description="Apply scale to the car root and all descendants before export",
        default=True,
    )
    file_selector_opened: BoolProperty(default=False, options={"HIDDEN", "SKIP_SAVE"})

    def open_file_selector(self, context):
        settings = scene_settings(context)
        if not self.filepath:
            self.filepath = bpy.path.abspath(f"//{settings.car_id or 'car'}.zip")
        self.file_selector_opened = True
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def draw(self, _context):
        if self.file_selector_opened:
            return
        layout = self.layout
        layout.label(text="One or more car hierarchy objects have unapplied scale.")
        layout.label(text="This may cause unexpected behaviour in game.")
        layout.separator()
        layout.prop(self, "apply_scales_before_export")

    def execute(self, context):
        if not self.file_selector_opened:
            return self.open_file_selector(context)

        settings = scene_settings(context)
        errors, warnings = validate_scene(settings)
        for msg in warnings:
            self.report({"WARNING"}, msg)
        if errors:
            for msg in errors:
                self.report({"ERROR"}, msg)
            return {"CANCELLED"}

        export_zip = Path(abspath(self.filepath))
        if not export_zip.name.lower().endswith(".zip"):
            export_zip = export_zip.with_suffix(".zip")
        export_zip.parent.mkdir(parents=True, exist_ok=True)
        ensure_camera_targets(settings)
        if self.apply_scales_before_export:
            try:
                apply_car_hierarchy_scales(context, settings.car_root_object)
            except RuntimeError as error:
                self.report({"ERROR"}, str(error))
                return {"CANCELLED"}

        with tempfile.TemporaryDirectory(prefix="car_exporter_") as temp_dir:
            temp_path = Path(temp_dir)
            sounds_path = temp_path / "sounds"
            if settings.use_custom_sounds:
                sounds_path.mkdir()

            model_filename = f"{settings.car_id}.glb"
            with_helpers_unlinked(lambda: export_car_glb(
                context,
                temp_path / model_filename,
                int(settings.max_texture_size),
                settings.optimize_color_textures,
                settings.jpeg_quality,
            ), excluded_objects=excluded_ghost_objects(settings))

            manifest = build_manifest(settings)
            (temp_path / "manifest.json").write_text(json.dumps(manifest, indent=4), encoding="utf-8")

            copied = set()
            if settings.use_custom_sounds:
                for slot in SOUND_SLOTS:
                    if not getattr(settings, f"sound_{slot}_enabled"):
                        continue
                    source = getattr(settings, f"sound_{slot}")
                    if not source:
                        continue
                    source_path = Path(abspath(source))
                    if source_path.is_file() and source_path.name not in copied:
                        shutil.copy2(source_path, sounds_path / source_path.name)
                        copied.add(source_path.name)

            with zipfile.ZipFile(export_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                if settings.use_custom_sounds:
                    archive.writestr("sounds/", "")
                for path in temp_path.rglob("*"):
                    if path.is_file():
                        archive.write(path, path.relative_to(temp_path).as_posix())

        self.report({"INFO"}, f"Exported {export_zip}")
        return {"FINISHED"}

    def invoke(self, context, _event):
        settings = scene_settings(context)
        if objects_with_unapplied_scale(settings.car_root_object):
            self.file_selector_opened = False
            return context.window_manager.invoke_props_dialog(
                self,
                width=420,
                title="Unapplied Scale",
                confirm_text="Export",
            )
        return self.open_file_selector(context)


class CAR_EXPORTER_OT_import_manifest(Operator):
    bl_idname = "car_exporter.import_car_manifest"
    bl_label = "Import Car Manifest"
    bl_options = {"REGISTER"}

    filepath: StringProperty(
        name="Manifest JSON",
        description="Vehicle manifest JSON file to import into the current scene",
        subtype="FILE_PATH",
    )

    def execute(self, context):
        settings = scene_settings(context)
        manifest_path = Path(abspath(self.filepath))
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            self.report({"ERROR"}, "Vehicle manifest must be an object")
            return {"CANCELLED"}
        try:
            ghost_config = parse_ghost_config(data.get("ghost"))
            armature_config = parse_armature_config(data.get("armature"))
        except ValueError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        removed_wheel_fields = {"sideFrictionStiffness", "brakeFactor", "sideFactor", "forwardFactor", "contactDamping"}
        pending_values = [data]
        while pending_values:
            value = pending_values.pop()
            if isinstance(value, dict):
                removed = removed_wheel_fields.intersection(value)
                if removed:
                    self.report({"ERROR"}, f"Vehicle manifest field {sorted(removed)[0]} is no longer supported")
                    return {"CANCELLED"}
                pending_values.extend(value.values())
            elif isinstance(value, list):
                pending_values.extend(value)
        manifest_version = data.get("version")
        if manifest_version != 8:
            self.report({"ERROR"}, "Only vehicle manifest version 8 can be imported")
            return {"CANCELLED"}
        engine = data.get("engine", {})
        if not isinstance(engine, dict) or "redlineRPM" not in engine:
            self.report({"ERROR"}, "Manifest engine.redlineRPM is required")
            return {"CANCELLED"}
        engine_braking = engine.get("engineBraking")
        if (
            isinstance(engine_braking, bool)
            or not isinstance(engine_braking, (int, float))
            or not math.isfinite(engine_braking)
            or engine_braking < 0
        ):
            self.report({"ERROR"}, "Manifest engine.engineBraking must be a finite nonnegative number")
            return {"CANCELLED"}
        if "finalDriveRatio" in engine or "gearRatios" in engine:
            self.report({"ERROR"}, "Manifest version 8 gearing must be configured by presets")
            return {"CANCELLED"}
        sounds = data.get("sounds", {})
        if not isinstance(sounds, dict):
            self.report({"ERROR"}, "Manifest sounds must be an object")
            return {"CANCELLED"}
        sound_pitch_offset = sounds.get("pitchOffset", 0)
        if (
            isinstance(sound_pitch_offset, bool)
            or not isinstance(sound_pitch_offset, (int, float))
            or not math.isfinite(sound_pitch_offset)
            or not -2400 <= sound_pitch_offset <= 2400
        ):
            self.report({"ERROR"}, "Manifest sounds.pitchOffset must be between -2400 and 2400 cents")
            return {"CANCELLED"}
        for slot in SOUND_SLOTS:
            if slot not in sounds or sounds[slot] is None:
                continue
            sound = sounds[slot]
            if not isinstance(sound, dict):
                self.report({"ERROR"}, f"Manifest sounds.{slot} must be an object or null")
                return {"CANCELLED"}
            if not isinstance(sound.get("source"), str) or not sound["source"].strip():
                self.report({"ERROR"}, f"Manifest sounds.{slot}.source must be a non-empty string")
                return {"CANCELLED"}
            for field, default in (("rpm", 1000), ("volume", 1)):
                value = sound.get(field, default)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                    self.report({"ERROR"}, f"Manifest sounds.{slot}.{field} must be a finite non-negative number")
                    return {"CANCELLED"}
            if "loop" in sound and not isinstance(sound["loop"], bool):
                self.report({"ERROR"}, f"Manifest sounds.{slot}.loop must be a boolean")
                return {"CANCELLED"}
        body = data.get("body")
        if not isinstance(body, dict):
            self.report({"ERROR"}, "Manifest body must be an object")
            return {"CANCELLED"}
        downforce_points_data = body.get("downForcePoints", [])
        if manifest_version == 8:
            if not isinstance(downforce_points_data, list):
                self.report({"ERROR"}, "Manifest body.downForcePoints must be an array")
                return {"CANCELLED"}
            downforce_point_names = set()
            for point_index, point_data in enumerate(downforce_points_data):
                if not isinstance(point_data, dict):
                    self.report({"ERROR"}, f"Manifest downforce point {point_index} must be an object")
                    return {"CANCELLED"}
                display_name = point_data.get("name")
                if not isinstance(display_name, str) or not display_name.strip():
                    self.report({"ERROR"}, f"Manifest downforce point {point_index} name is invalid")
                    return {"CANCELLED"}
                normalized_name = display_name.strip().casefold()
                if normalized_name in downforce_point_names:
                    self.report({"ERROR"}, f"Duplicate manifest downforce point name: {display_name.strip()}")
                    return {"CANCELLED"}
                downforce_point_names.add(normalized_name)
                position = point_data.get("position")
                if (
                    not isinstance(position, list)
                    or len(position) != 3
                    or any(
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(value)
                        for value in position
                    )
                ):
                    self.report({"ERROR"}, f"Manifest downforce point {point_index} position is invalid")
                    return {"CANCELLED"}
                max_force = point_data.get("maxForce")
                if (
                    isinstance(max_force, bool)
                    or not isinstance(max_force, (int, float))
                    or not math.isfinite(max_force)
                    or max_force < 0
                ):
                    self.report({"ERROR"}, f"Manifest downforce point {point_index} maxForce is invalid")
                    return {"CANCELLED"}
            if "downForce" in body:
                self.report({"ERROR"}, "Manifest version 8 must use body.downForcePoints")
                return {"CANCELLED"}
        body_colors_data = []
        if "colors" in body:
            body_colors_data = body["colors"]
            if not isinstance(body_colors_data, list) or not body_colors_data:
                self.report({"ERROR"}, "Manifest body.colors must contain at least one color")
                return {"CANCELLED"}
            color_names = set()
            material_names = set()
            for color_index, body_color in enumerate(body_colors_data):
                if not isinstance(body_color, dict):
                    self.report({"ERROR"}, f"Manifest body.colors[{color_index}] must be an object")
                    return {"CANCELLED"}
                display_name = body_color.get("name")
                material_name = body_color.get("material")
                if not isinstance(display_name, str) or not display_name.strip():
                    self.report({"ERROR"}, f"Manifest body.colors[{color_index}].name is invalid")
                    return {"CANCELLED"}
                normalized_name = display_name.strip().casefold()
                if normalized_name in color_names:
                    self.report({"ERROR"}, f"Duplicate manifest body color name: {display_name.strip()}")
                    return {"CANCELLED"}
                color_names.add(normalized_name)
                if not isinstance(material_name, str) or not material_name.strip():
                    self.report({"ERROR"}, f"Manifest body.colors[{color_index}].material is invalid")
                    return {"CANCELLED"}
                if material_name in material_names:
                    self.report({"ERROR"}, f"Duplicate manifest body color material: {material_name}")
                    return {"CANCELLED"}
                material_names.add(material_name)
        steering_wheel = data.get("steeringWheel")
        if not isinstance(steering_wheel, dict):
            self.report({"ERROR"}, "Manifest steeringWheel must be an object")
            return {"CANCELLED"}
        driver_assists = data.get("driverAssists")
        if not isinstance(driver_assists, dict):
            self.report({"ERROR"}, "Manifest driverAssists must be an object")
            return {"CANCELLED"}
        assist_max_levels = {}
        for field in ("abs", "esc", "tractionControl"):
            config = driver_assists.get(field)
            max_level = config.get("maxLevel") if isinstance(config, dict) else None
            if isinstance(max_level, bool) or not isinstance(max_level, int) or max_level < 1:
                self.report({"ERROR"}, f"Manifest driverAssists.{field}.maxLevel must be a positive integer")
                return {"CANCELLED"}
            assist_max_levels[field] = max_level
        presets_data = data.get("presets")
        if not isinstance(presets_data, list) or not presets_data:
            self.report({"ERROR"}, "Manifest presets must contain at least one preset")
            return {"CANCELLED"}
        for preset_index, preset_data in enumerate(presets_data):
            if not isinstance(preset_data, dict):
                self.report({"ERROR"}, f"Manifest preset {preset_index} must be an object")
                return {"CANCELLED"}
            gearing = preset_data.get("gearing")
            if not isinstance(gearing, dict):
                self.report({"ERROR"}, f"Manifest preset {preset_index}.gearing must be an object")
                return {"CANCELLED"}
            final_drive_ratio = gearing.get("finalDriveRatio")
            if (
                not isinstance(final_drive_ratio, (int, float))
                or not math.isfinite(final_drive_ratio)
                or final_drive_ratio <= 0
            ):
                self.report({"ERROR"}, f"Manifest preset {preset_index}.gearing.finalDriveRatio must be positive")
                return {"CANCELLED"}
            gear_ratios = gearing.get("gearRatios")
            if not isinstance(gear_ratios, dict) or gear_ratios.get("0") != 0:
                self.report({"ERROR"}, f"Manifest preset {preset_index}.gearing.gearRatios is invalid")
                return {"CANCELLED"}
            reverse_ratio = gear_ratios.get("-1")
            if (
                not isinstance(reverse_ratio, (int, float))
                or not math.isfinite(reverse_ratio)
                or reverse_ratio >= 0
            ):
                self.report({"ERROR"}, f"Manifest preset {preset_index} reverse ratio must be negative")
                return {"CANCELLED"}
            positive_gears = sorted(
                int(key) for key in gear_ratios
                if isinstance(key, str) and key.isdigit() and int(key) > 0
            )
            if not positive_gears or positive_gears != list(range(1, len(positive_gears) + 1)) or len(positive_gears) > 15:
                self.report({"ERROR"}, f"Manifest preset {preset_index} forward gears must be contiguous from 1 to 15")
                return {"CANCELLED"}
            if any(
                not isinstance(gear_ratios[str(index)], (int, float))
                or not math.isfinite(gear_ratios[str(index)])
                or gear_ratios[str(index)] <= 0
                for index in positive_gears
            ):
                self.report({"ERROR"}, f"Manifest preset {preset_index} forward gear ratios must be positive")
                return {"CANCELLED"}
            if set(gear_ratios) != {"-1", "0", *(str(index) for index in positive_gears)}:
                self.report({"ERROR"}, f"Manifest preset {preset_index}.gearing.gearRatios contains unsupported gears")
                return {"CANCELLED"}
            steering_angle = preset_data.get("maxSteeringAngle")
            if not isinstance(steering_angle, (int, float)) or not math.isfinite(steering_angle) or not 1 <= steering_angle <= 90:
                self.report({"ERROR"}, f"Manifest preset {preset_index}.maxSteeringAngle is invalid")
                return {"CANCELLED"}
            road_wheel_curve = preset_data.get("roadWheelCurve", 0.5)
            if (
                isinstance(road_wheel_curve, bool)
                or not isinstance(road_wheel_curve, (int, float))
                or not math.isfinite(road_wheel_curve)
                or not 0 <= road_wheel_curve <= 1
            ):
                self.report({"ERROR"}, f"Manifest preset {preset_index}.roadWheelCurve must be between 0 and 1")
                return {"CANCELLED"}
            rotation = preset_data.get("maxDegreesOfRotation")
            if not isinstance(rotation, (int, float)) or not math.isfinite(rotation) or not 90 <= rotation <= 2160:
                self.report({"ERROR"}, f"Manifest preset {preset_index}.maxDegreesOfRotation is invalid")
                return {"CANCELLED"}
            brake_bias = preset_data.get("brakeBias")
            if (
                not isinstance(brake_bias, (int, float))
                or not math.isfinite(brake_bias)
                or not 0 <= brake_bias <= 1
            ):
                self.report({"ERROR"}, f"Manifest preset {preset_index}.brakeBias must be between 0 and 1")
                return {"CANCELLED"}
            anti_roll_bars = preset_data.get("antiRollBars")
            if not isinstance(anti_roll_bars, dict):
                self.report({"ERROR"}, f"Manifest preset {preset_index}.antiRollBars must be an object")
                return {"CANCELLED"}
            for axle in ("front", "rear"):
                value = anti_roll_bars.get(axle)
                if (
                    not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or not 1 <= value <= 40
                ):
                    self.report({"ERROR"}, f"Manifest preset {preset_index}.antiRollBars.{axle} must be between 1 and 40")
                    return {"CANCELLED"}
            for field, max_level in (
                ("absLevel", assist_max_levels["abs"]),
                ("escLevel", assist_max_levels["esc"]),
                ("tractionControlLevel", assist_max_levels["tractionControl"]),
            ):
                value = preset_data.get(field)
                if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= max_level:
                    self.report(
                        {"ERROR"},
                        f"Manifest preset {preset_index}.{field} must be an integer between 0 and {max_level}",
                    )
                    return {"CANCELLED"}
            preset_wheels = preset_data.get("wheels") or {}
            if not isinstance(preset_wheels, dict):
                self.report({"ERROR"}, f"Manifest preset {preset_index}.wheels must be an object")
                return {"CANCELLED"}
            for group, key, _steering in WHEEL_KEYS:
                group_wheels = preset_wheels.get(group)
                if not isinstance(group_wheels, dict) or not isinstance(group_wheels.get(key), dict):
                    self.report({"ERROR"}, f"Manifest preset {preset_index} {group} {key.upper()} must be an object")
                    return {"CANCELLED"}
                wheel_data = group_wheels[key]
                finite_fields = (
                    "pressure", "camber", "toe", "suspensionOffset",
                    "suspensionStiffness", "dampingRelaxation", "dampingCompression",
                    "maxBrakeForce", "gripFactor",
                )
                finite_fields += ("caster",)
                if any(
                    not isinstance(wheel_data.get(field), (int, float))
                    or not math.isfinite(wheel_data[field])
                    for field in finite_fields
                ):
                    self.report({"ERROR"}, f"Manifest preset {preset_index} {group} {key.upper()} values must be finite")
                    return {"CANCELLED"}
                if wheel_data.get("tireType") not in TIRE_TYPES:
                    self.report({"ERROR"}, f"Manifest preset {preset_index} {group} {key.upper()} tireType is invalid")
                    return {"CANCELLED"}
                if not 1.3 <= wheel_data["pressure"] <= 2.7:
                    self.report({"ERROR"}, f"Manifest preset {preset_index} {group} {key.upper()} pressure is invalid")
                    return {"CANCELLED"}
                caster = wheel_data.get("caster", 0.0)
                if not -15.0 <= caster <= 15.0:
                    self.report({"ERROR"}, f"Manifest preset {preset_index} {group} {key.upper()} caster is invalid")
                    return {"CANCELLED"}
                if not -0.25 <= wheel_data["suspensionOffset"] <= 0.25:
                    self.report({"ERROR"}, f"Manifest preset {preset_index} {group} {key.upper()} suspensionOffset is invalid")
                    return {"CANCELLED"}
                non_negative_fields = (
                    "suspensionStiffness", "dampingRelaxation", "dampingCompression",
                    "maxBrakeForce",
                )
                if any(wheel_data[field] < 0 for field in non_negative_fields):
                    self.report({"ERROR"}, f"Manifest preset {preset_index} {group} {key.upper()} handling values must be non-negative")
                    return {"CANCELLED"}
                if wheel_data["gripFactor"] <= 0:
                    self.report({"ERROR"}, f"Manifest preset {preset_index} {group} {key.upper()} gripFactor must be positive")
                    return {"CANCELLED"}

        settings.is_configured = True
        import_ghost_config(settings, ghost_config)
        import_armature_config(settings, armature_config)
        settings.car_id = data.get("id", data.get("name", settings.car_id))
        settings.package_version = str(data.get("packageVersion", settings.package_version))
        settings.display_name = data.get("displayName", data.get("name", settings.display_name))
        settings.car_class = data.get("class", settings.car_class)
        settings.sound_pitch_offset = round(sound_pitch_offset)
        settings.use_custom_sounds = any(slot in sounds for slot in SOUND_SLOTS)
        for slot, meta in SOUND_SLOTS.items():
            sound = sounds.get(slot)
            setattr(settings, f"sound_{slot}_enabled", slot not in sounds or sound is not None)
            setattr(settings, f"sound_{slot}", "")
            if not isinstance(sound, dict):
                continue
            setattr(
                settings,
                f"sound_{slot}",
                str(manifest_path.parent / "sounds" / sound["source"].strip()),
            )
            setattr(settings, f"sound_{slot}_volume", sound.get("volume", meta["volume"]))
            if slot in SOUND_RPM_SLOTS:
                setattr(settings, f"sound_{slot}_rpm", sound.get("rpm", meta["rpm"]))
        track_types = data.get("trackTypes")
        if isinstance(track_types, list):
            tags = set(track_types)
            settings.vehicle_tag_tarmac = "tarmac" in tags
            settings.vehicle_tag_offroad = "offroad" in tags
        settings.drive = engine.get("drive", settings.drive)
        settings.hp = engine.get("hp", settings.hp)
        settings.max_rpm = engine.get("maxRPM", settings.max_rpm)
        settings.idle_rpm = engine.get("idleRPM", settings.idle_rpm)
        settings.redline_rpm = engine["redlineRPM"]
        settings.rev_limit = engine.get("revLimit", settings.rev_limit)
        settings.engine_inertia = engine.get("inertia", settings.engine_inertia)
        settings.engine_braking = engine["engineBraking"]
        settings.engine_friction_torque = engine.get("frictionTorque", settings.engine_friction_torque)
        settings.clutch_response = engine.get("clutchResponse", settings.clutch_response)
        settings.shift_cooldown = engine.get("shiftCooldown", settings.shift_cooldown)
        settings.auto_blip = engine.get("autoBlip") is True
        settings.auto_blip_duration = engine.get("autoBlipDuration", settings.auto_blip_duration)
        settings.torque_factor = engine.get("torqueFactor", settings.torque_factor)
        set_object_pointer(settings, "car_root_object", body.get("obj", ""))
        if not settings.car_root_object and downforce_points_data:
            self.report({"ERROR"}, "Manifest car root object is required to import downforce points")
            return {"CANCELLED"}
        set_object_pointer(settings, "center_of_mass_object", body.get("centerOfMass", ""))
        settings.air_drag = body.get("airDrag", settings.air_drag)
        for point in settings.down_force_points:
            helper = point.object_ref
            if helper and helper.get(DOWNFORCE_HELPER_PROP):
                bpy.data.objects.remove(helper, do_unlink=True)
        settings.down_force_points.clear()
        if manifest_version == 8:
            for point_index, point_data in enumerate(downforce_points_data):
                helper = create_car_helper(
                    context,
                    settings,
                    next_helper_name("downforce"),
                    "SINGLE_ARROW",
                    0.4,
                    DOWNFORCE_HELPER_PROP,
                )
                helper.location = game_position_to_blender(point_data["position"])
                helper.rotation_euler = (math.pi, 0.0, 0.0)
                point = settings.down_force_points.add()
                point.display_name = point_data["name"].strip()
                point.object_ref = helper
                point.max_force = point_data["maxForce"]
        settings.abs_max_level = assist_max_levels["abs"]
        settings.esc_max_level = assist_max_levels["esc"]
        settings.traction_control_max_level = assist_max_levels["tractionControl"]
        settings.body_colors.clear()
        for body_color_data in body_colors_data:
            body_color = settings.body_colors.add()
            body_color.display_name = body_color_data["name"].strip()
            set_material_pointer(body_color, "material", body_color_data["material"])
        settings.active_body_color_index = 0

        colliders = body.get("colliders") or []
        settings.colliders.clear()
        for collider_data in colliders:
            collider = settings.colliders.add()
            set_object_pointer(collider, "object_ref", collider_data.get("obj", ""))
            collider.collider_type = collider_data.get("type", "trimesh")
            collider.mass = collider_data.get("mass", 0.0)

        wheels = data.get("wheels") or {}
        settings.wheels.clear()
        if isinstance(wheels, dict):
            for group, group_wheels in wheels.items():
                if isinstance(group_wheels, dict):
                    for key, wheel_data in group_wheels.items():
                        add_wheel_from_config(settings, group, key, wheel_data)
        ensure_default_wheels(settings)
        settings.presets.clear()
        settings.preset_schema_version = 8
        for preset_data in presets_data:
            if not isinstance(preset_data, dict):
                continue
            preset = settings.presets.add()
            preset.preset_id = str(preset_data.get("id", next_preset_id(settings)))
            preset.display_name = str(preset_data.get("name", preset.preset_id))
            preset.max_steering_angle = preset_data["maxSteeringAngle"]
            preset.road_wheel_curve = preset_data.get("roadWheelCurve", 0.5)
            preset.max_degrees_of_rotation = preset_data["maxDegreesOfRotation"]
            preset.front_anti_roll_bar_stiffness = preset_data["antiRollBars"]["front"]
            preset.rear_anti_roll_bar_stiffness = preset_data["antiRollBars"]["rear"]
            preset.abs_level = preset_data["absLevel"]
            preset.esc_level = preset_data["escLevel"]
            preset.traction_control_level = preset_data["tractionControlLevel"]
            preset.brake_bias = preset_data["brakeBias"]
            gearing = preset_data["gearing"]
            ratios = gearing["gearRatios"]
            preset.final_drive_ratio = gearing["finalDriveRatio"]
            preset.reverse_ratio = ratios["-1"]
            positive_gears = sorted(int(key) for key in ratios if key.isdigit() and int(key) > 0)
            preset.forward_gear_count = len(positive_gears)
            for gear_index in positive_gears:
                setattr(preset, f"gear_{gear_index}", ratios[str(gear_index)])
            preset_wheels = preset_data.get("wheels") or {}
            for group in ("front", "rear"):
                group_wheels = preset_wheels.get(group) or {}
                wheel_data = group_wheels.get("l") or group_wheels.get("r") or {}
                wheel = getattr(preset, group)
                wheel.tire_type = wheel_data.get("tireType", "medium")
                wheel.pressure = wheel_data.get("pressure", 2.0)
                wheel.camber = wheel_data.get("camber", -4.0 if group == "front" else -3.0)
                wheel.caster = wheel_data.get("caster", 0.0)
                wheel.toe = wheel_data.get("toe", -0.15 if group == "front" else 0.2)
                wheel.suspension_offset = wheel_data.get("suspensionOffset", 0.0)
                wheel.suspension_stiffness = wheel_data.get("suspensionStiffness", 80.0)
                wheel.damping_relaxation = wheel_data.get("dampingRelaxation", 2.6)
                wheel.damping_compression = wheel_data.get("dampingCompression", 2.0)
                wheel.max_brake_force = wheel_data.get("maxBrakeForce", 1000.0)
                wheel.grip_factor = wheel_data.get("gripFactor", 1.0)
        ensure_default_presets(settings)
        settings.active_preset_index = 0

        torque = engine.get("torqueCurve", {})
        if torque:
            settings.max_torque = max(float(value) for value in torque.values())
            apply_imported_torque_curve(settings, torque)
        for rpm in (1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000):
            setattr(settings, f"torque_{rpm}", torque.get(str(rpm), getattr(settings, f"torque_{rpm}")))

        turbo = engine.get("turbo", {})
        settings.turbo_enabled = turbo.get("enabled", settings.turbo_enabled)
        settings.turbo_boost = turbo.get("boost", settings.turbo_boost)
        set_object_pointer(settings, "steering_wheel_object", steering_wheel.get("obj", ""))
        settings.steering_wheel_spin_axis = GAME_AXIS_TO_BLENDER.get(
            tuple(steering_wheel.get("spinLocalAxis", [0, 0, -1])),
            "y",
        )

        lights = data.get("lights", {})
        for key, prop_name in (
            ("headlights", "headlights_material"),
            ("brakeLights", "brake_lights_material"),
            ("reverseLights", "reverse_lights_material"),
        ):
            light = lights.get(key, {}) if isinstance(lights, dict) else {}
            material_name = light.get("material", "") if isinstance(light, dict) else ""
            set_material_pointer(settings, prop_name, material_name)

        dashboard = data.get("dashboard", {})
        screen = dashboard.get("screen", {}) if isinstance(dashboard, dict) else {}
        if isinstance(screen, dict):
            set_object_pointer(settings, "dashboard_screen_object", screen.get("obj", ""))
        else:
            settings.dashboard_screen_object = None

        cameras = data.get("cameras", {})
        for name, attr in (
            ("chase_cam", "chase"),
            ("cockpit_cam", "cockpit"),
            ("hood_cam", "hood"),
            ("roof_cam", "roof"),
        ):
            camera = cameras.get(name, {})
            set_object_pointer(settings, f"{attr}_camera_object", camera.get("obj", ""))
            setattr(settings, f"{attr}_fov", camera.get("fov", getattr(settings, f"{attr}_fov")))

        create_size_guide(settings)
        self.report({"INFO"}, "Imported config values into scene settings")
        return {"FINISHED"}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


def draw_split_prop(layout, data, prop_name, label=None, **kwargs):
    split = layout.split(factor=0.4, align=True)
    property_label = label or data.bl_rna.properties[prop_name].name
    split.label(text=property_label)
    split.prop(data, prop_name, text="", **kwargs)


def draw_split_label(layout, label, value, tooltip=""):
    split = layout.split(factor=0.4, align=True)
    split.label(text=label)
    value_row = split.row(align=True)
    value_row.label(text=value)
    if tooltip:
        help_op = value_row.operator("car_exporter.tooltip_label", text="", icon="HELP", emboss=False)
        help_op.tooltip = tooltip


def draw_body_colors(layout, settings):
    header = layout.row(align=True)
    header.label(text="Body Colors")
    header.operator("car_exporter.add_body_color", text="", icon="ADD")
    header.operator("car_exporter.remove_body_color", text="", icon="REMOVE")
    move_up = header.operator("car_exporter.move_body_color", text="", icon="TRIA_UP")
    move_up.direction = "UP"
    move_down = header.operator("car_exporter.move_body_color", text="", icon="TRIA_DOWN")
    move_down.direction = "DOWN"
    layout.template_list(
        "CAR_EXPORTER_UL_body_colors",
        "",
        settings,
        "body_colors",
        settings,
        "active_body_color_index",
        rows=3,
    )
    index = settings.active_body_color_index
    if not (0 <= index < len(settings.body_colors)):
        layout.label(text="Add body colors to export selectable material names", icon="INFO")
        return
    body_color = settings.body_colors[index]
    draw_split_prop(layout, body_color, "display_name")
    draw_split_prop(layout, body_color, "material")
    if index == 0:
        layout.label(text="Index 0 is the default body color", icon="CHECKMARK")


def draw_vehicle_tags(layout, settings):
    split = layout.split(factor=0.4, align=True)
    split.label(text="Track Types")
    tag_buttons = split.grid_flow(row_major=True, columns=2, even_columns=True, even_rows=True, align=True)
    tag_buttons.prop(settings, "vehicle_tag_tarmac", text="Tarmac", toggle=True)
    tag_buttons.prop(settings, "vehicle_tag_offroad", text="Offroad", toggle=True)


def draw_torque_curve(layout, settings):
    draw_split_prop(layout, settings, "max_torque")
    draw_split_prop(layout, settings, "torque_factor")
    layout.operator("car_exporter.reset_torque_curve", text="Reset Curve")
    node = get_torque_curve_node(create=False)
    if node:
        layout.template_curve_mapping(node, "mapping", type="NONE")
    else:
        layout.label(text="Torque curve initializing")


def draw_colliders(layout, settings):
    header = layout.row(align=True)
    header.label(text="")
    header.operator("car_exporter.add_collider", text="Add", icon="ADD")

    if len(settings.colliders) == 0:
        layout.label(text="No colliders configured")
        return

    for index, collider in enumerate(settings.colliders):
        row = layout.row(align=True)
        row.label(text=f"Collider {index + 1}")
        remove = row.operator("car_exporter.remove_collider", text="", icon="REMOVE")
        remove.index = index
        draw_split_prop(layout, collider, "object_ref", label="Object")
        draw_split_prop(layout, collider, "collider_type", label="Type")
        draw_split_prop(layout, collider, "mass")


def draw_wheels(layout, settings):
    if len(settings.wheels) == 0:
        ensure_default_wheels(settings)

    for index, wheel in enumerate(settings.wheels):
        if index > 0:
            layout.separator()
        row = layout.row(align=True)
        row.label(text=WHEEL_LABELS.get((wheel.group, wheel.key), f"Wheel {index + 1}"))
        draw_split_prop(layout, wheel, "steering")
        draw_split_prop(layout, wheel, "suspension_ref")
        draw_split_prop(layout, wheel, "hub_ref")
        draw_split_prop(layout, wheel, "wheel_ref", label="Spin")
        draw_split_prop(layout, wheel, "up_local_axis")
        draw_split_prop(layout, wheel, "spin_local_axis")
        draw_split_prop(layout, wheel, "radius")


def draw_armature(layout, settings):
    header = layout.row(align=True)
    header.alignment = "LEFT"
    header.use_property_split = False
    header.use_property_decorate = False
    header.label(text="Armature")
    header.separator(factor=0.5)
    header.prop(settings, "armature_enabled", text="")
    if not settings.armature_enabled:
        return
    layout.separator()
    draw_split_prop(layout, settings, "armature_object")
    rig = settings.armature_object
    inputs = layout.column()
    inputs.enabled = rig is not None and rig.type == "ARMATURE"
    inputs.operator("car_exporter.add_armature_joint", icon="ADD")
    for index, joint in enumerate(settings.armature_joints):
        box = inputs.box()
        row = box.row(align=True)
        row.label(text=joint.bone or f"Joint {index + 1}")
        row.operator("car_exporter.remove_armature_joint", text="", icon="REMOVE").index = index
        if rig and rig.type == "ARMATURE":
            box.prop_search(joint, "bone", rig.data, "bones")
        else:
            box.prop(joint, "bone")
        draw_split_prop(box, joint, "base_attachment")
        draw_split_prop(box, joint, "tip_attachment")
        if joint.tip_attachment != "none":
            draw_split_prop(box, joint, "stretch")
    layout.label(text="Bone head and tail define the attachment offsets", icon="INFO")


def draw_custom_ghost(layout, settings):
    header = layout.row(align=True)
    header.alignment = "LEFT"
    header.use_property_split = False
    header.use_property_decorate = False
    header.label(text="Custom Ghost")
    header.separator(factor=0.5)
    header.prop(settings, "ghost_enabled", text="")
    if not settings.ghost_enabled:
        return
    layout.separator()
    inputs = layout.column()
    draw_split_prop(inputs, settings, "ghost_root_object")
    for group, key, _steering in WHEEL_KEYS:
        box = inputs.box()
        box.label(text=WHEEL_LABELS[(group, key)])
        wheel = ghost_wheel_settings(settings, group, key)
        for _role, prop in GHOST_WHEEL_ROLES:
            draw_split_prop(box, wheel, prop)
    layout.label(text="Uses existing wheel axes, body colors and light materials", icon="INFO")


def draw_presets(layout, settings):
    header = layout.row(align=True)
    header.label(text="Car Presets")
    header.operator("car_exporter.add_preset", text="", icon="ADD")
    header.operator("car_exporter.remove_preset", text="", icon="REMOVE")
    move_up = header.operator("car_exporter.move_preset", text="", icon="TRIA_UP")
    move_up.direction = "UP"
    move_down = header.operator("car_exporter.move_preset", text="", icon="TRIA_DOWN")
    move_down.direction = "DOWN"
    layout.template_list(
        "CAR_EXPORTER_UL_presets",
        "",
        settings,
        "presets",
        settings,
        "active_preset_index",
        rows=3,
    )
    preset = active_preset(settings)
    if not preset:
        layout.label(text="Add a car preset to configure adjustments", icon="INFO")
        return
    draw_split_prop(layout, preset, "preset_id")
    draw_split_prop(layout, preset, "display_name")
    draw_split_prop(layout, preset, "max_steering_angle")
    draw_split_prop(layout, preset, "road_wheel_curve")
    draw_split_prop(layout, preset, "max_degrees_of_rotation")
    draw_split_prop(layout, preset, "front_anti_roll_bar_stiffness")
    draw_split_prop(layout, preset, "rear_anti_roll_bar_stiffness")
    draw_split_prop(layout, preset, "abs_level")
    draw_split_prop(layout, preset, "esc_level")
    draw_split_prop(layout, preset, "traction_control_level")
    draw_split_prop(layout, preset, "brake_bias")

    gearing_box = layout.box()
    gearing_box.label(text="Gearing")
    draw_split_prop(gearing_box, preset, "reverse_ratio")
    draw_split_prop(gearing_box, preset, "forward_gear_count")
    for index in range(1, preset.forward_gear_count + 1):
        draw_split_prop(gearing_box, preset, f"gear_{index}")
    draw_split_prop(gearing_box, preset, "final_drive_ratio")

    for group in ("front", "rear"):
        axle_box = layout.box()
        axle_box.label(text=f"{group.title()} Wheels")
        wheel = getattr(preset, group)
        draw_split_prop(axle_box, wheel, "tire_type")
        draw_split_prop(axle_box, wheel, "pressure")
        draw_split_prop(axle_box, wheel, "camber")
        draw_split_prop(axle_box, wheel, "caster")
        draw_split_prop(axle_box, wheel, "toe")
        draw_split_prop(axle_box, wheel, "suspension_offset")
        draw_split_prop(axle_box, wheel, "suspension_stiffness")
        draw_split_prop(axle_box, wheel, "damping_relaxation")
        draw_split_prop(axle_box, wheel, "damping_compression")
        brake_row = axle_box.row(align=True)
        brake_split = brake_row.split(factor=0.4, align=True)
        brake_split.label(text=wheel.bl_rna.properties["max_brake_force_kg"].name)
        brake_value = brake_split.row(align=True)
        brake_value.prop(wheel, "max_brake_force_kg", text="")
        estimate = brake_value.operator("car_exporter.estimate_brake_force", text="", icon="FILE_REFRESH")
        estimate.axle = group
        draw_split_prop(axle_box, wheel, "grip_factor")


def draw_cameras(layout, settings):
    cameras = (
        ("Chase Cam", "chase"),
        ("Cockpit Cam", "cockpit"),
        ("Hood Cam", "hood"),
        ("Roof Cam", "roof"),
    )

    for index, (label, prefix) in enumerate(cameras):
        if index > 0:
            layout.separator()
        row = layout.row(align=True)
        row.label(text=label)
        draw_split_prop(layout, settings, f"{prefix}_camera_object", label="Object")
        if not getattr(settings, f"{prefix}_camera_object"):
            continue
        draw_split_prop(layout, settings, f"{prefix}_target_distance", label="Target Distance")
        draw_split_label(layout, "Vertical FOV", f"{camera_fov(settings, prefix):.1f}", tooltip="Adjust FOV from Camera Properties")


def draw_body_physics(layout, settings):
    layout.label(text="Center of Mass")
    if settings.center_of_mass_object:
        row = layout.row(align=True)
        row.prop(settings, "center_of_mass_object", text="")
        row.operator("car_exporter.remove_center_of_mass", text="", icon="X")
    else:
        layout.operator("car_exporter.add_center_of_mass", icon="ADD")

    layout.separator(type="LINE")
    layout.label(text="Downforce Points")
    for index, point in enumerate(settings.down_force_points):
        point_box = layout.box()
        header = point_box.row(align=True)
        header.label(text=downforce_point_display_name(point, index), icon="TRIA_DOWN")
        remove = header.operator("car_exporter.remove_downforce_point", text="", icon="X")
        remove.index = index
        draw_split_prop(point_box, point, "display_name")
        draw_split_prop(point_box, point, "object_ref", label="Helper")
        draw_split_prop(point_box, point, "max_force_kg")
    layout.operator("car_exporter.add_downforce_point", icon="ADD")
    layout.separator(type="LINE")
    draw_split_prop(layout, settings, "air_drag")


class CAR_EXPORTER_PT_car_export(Panel):
    bl_label = "VectorG Car Exporter"
    bl_idname = "CAR_EXPORTER_PT_car_export"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "VectorG"

    def draw(self, context):
        layout = self.layout
        settings = scene_settings(context)

        if not settings.is_configured:
            layout.operator("car_exporter.create_configuration", icon="ADD")
            return

        box = layout.box()
        box.label(text="Package")
        draw_split_prop(box, settings, "car_id")
        draw_split_prop(box, settings, "package_version")
        draw_split_prop(box, settings, "display_name")
        draw_split_prop(box, settings, "max_texture_size")
        draw_split_prop(box, settings, "optimize_color_textures")
        color_quality = box.row()
        color_quality.enabled = settings.optimize_color_textures
        draw_split_prop(color_quality, settings, "jpeg_quality")
        draw_split_prop(box, settings, "car_class")
        draw_vehicle_tags(box, settings)

        box = layout.box()
        box.label(text="Body")
        draw_split_prop(box, settings, "car_root_object")
        box.separator(type="LINE")
        draw_body_colors(box, settings)

        draw_custom_ghost(layout.box(), settings)

        box = layout.box()
        box.label(text="Steering Wheel")
        draw_split_prop(box, settings, "steering_wheel_object")
        draw_split_prop(box, settings, "steering_wheel_spin_axis")

        box = layout.box()
        box.label(text="Lights")
        draw_split_prop(box, settings, "headlights_material")
        draw_split_prop(box, settings, "brake_lights_material")
        draw_split_prop(box, settings, "reverse_lights_material")

        box = layout.box()
        box.label(text="Dashboard")
        draw_split_prop(box, settings, "dashboard_screen_object")

        box = layout.box()
        box.label(text="Engine")
        for prop in (
            "drive",
            "hp",
            "idle_rpm",
            "redline_rpm",
            "rev_limit",
            "max_rpm",
            "engine_inertia",
            "engine_braking",
            "engine_friction_torque",
            "clutch_response",
        ):
            draw_split_prop(box, settings, prop)
        draw_split_prop(box, settings, "turbo_enabled")
        turbo_boost = box.column()
        turbo_boost.enabled = settings.turbo_enabled
        draw_split_prop(turbo_boost, settings, "turbo_boost")

        box = layout.box()
        box.label(text="Torque Curve")
        draw_torque_curve(box, settings)

        box = layout.box()
        box.label(text="Gears")
        draw_split_prop(box, settings, "shift_cooldown")
        draw_split_prop(box, settings, "auto_blip")
        auto_blip_duration_row = box.row()
        auto_blip_duration_row.enabled = settings.auto_blip
        draw_split_prop(auto_blip_duration_row, settings, "auto_blip_duration")

        box = layout.box()
        box.label(text="Body Physics")
        draw_body_physics(box, settings)

        box = layout.box()
        box.label(text="Colliders")
        draw_colliders(box, settings)

        box = layout.box()
        box.label(text="Wheel Setup")
        draw_wheels(box, settings)

        draw_armature(layout.box(), settings)

        box = layout.box()
        box.label(text="Driver Assists")
        draw_split_prop(box, settings, "abs_max_level")
        draw_split_prop(box, settings, "esc_max_level")
        draw_split_prop(box, settings, "traction_control_max_level")

        box = layout.box()
        draw_presets(box, settings)

        box = layout.box()
        box.label(text="Cameras")
        draw_cameras(box, settings)

        box = layout.box()
        box.label(text="Audio")
        draw_split_prop(box, settings, "sound_pitch_offset")
        draw_split_prop(box, settings, "use_custom_sounds")
        if settings.use_custom_sounds:
            box.separator(type="LINE")
            for index, (slot, meta) in enumerate(SOUND_SLOTS.items()):
                if index:
                    box.separator(type="LINE")
                sound_section = box.column()
                draw_split_prop(
                    sound_section,
                    settings,
                    f"sound_{slot}_enabled",
                    label=meta["label"],
                )
                controls = sound_section.column()
                controls.enabled = getattr(settings, f"sound_{slot}_enabled")
                draw_split_prop(controls, settings, f"sound_{slot}", label="File")
                if slot in SOUND_RPM_SLOTS:
                    draw_split_prop(controls, settings, f"sound_{slot}_rpm")
                draw_split_prop(controls, settings, f"sound_{slot}_volume")

        box = layout.box()
        box.operator("car_exporter.remove_configuration", icon="TRASH")

        box = layout.box()
        row = box.row()
        row.operator("car_exporter.validate_car", icon="CHECKMARK")
        row.operator("car_exporter.import_car_manifest", icon="IMPORT")
        box.operator("car_exporter.export_car_zip", icon="EXPORT")


classes = (
    CarBodyColorSettings,
    CarColliderSettings,
    CarDownForcePointSettings,
    CarWheelSettings,
    CarArmatureJointSettings,
    CarGhostWheelSettings,
    CarWheelPresetSettings,
    CarPresetSettings,
    CarExporterSettings,
    CAR_EXPORTER_UL_body_colors,
    CAR_EXPORTER_UL_presets,
    CAR_EXPORTER_OT_validate_car,
    CAR_EXPORTER_OT_estimate_brake_force,
    CAR_EXPORTER_OT_add_armature_joint,
    CAR_EXPORTER_OT_remove_armature_joint,
    CAR_EXPORTER_OT_add_collider,
    CAR_EXPORTER_OT_remove_collider,
    CAR_EXPORTER_OT_add_center_of_mass,
    CAR_EXPORTER_OT_remove_center_of_mass,
    CAR_EXPORTER_OT_add_downforce_point,
    CAR_EXPORTER_OT_remove_downforce_point,
    CAR_EXPORTER_OT_add_body_color,
    CAR_EXPORTER_OT_remove_body_color,
    CAR_EXPORTER_OT_move_body_color,
    CAR_EXPORTER_OT_add_preset,
    CAR_EXPORTER_OT_remove_preset,
    CAR_EXPORTER_OT_move_preset,
    CAR_EXPORTER_OT_tooltip_label,
    CAR_EXPORTER_OT_reset_torque_curve,
    CAR_EXPORTER_OT_create_configuration,
    CAR_EXPORTER_OT_remove_configuration,
    CAR_EXPORTER_OT_export_car_zip,
    CAR_EXPORTER_OT_import_manifest,
    CAR_EXPORTER_PT_car_export,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.car_exporter = PointerProperty(type=CarExporterSettings)
    if initialize_car_exporter_defaults_after_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(initialize_car_exporter_defaults_after_load)
    schedule_defaults_initialization()


def unregister():
    if bpy.app.timers.is_registered(initialize_car_exporter_defaults):
        bpy.app.timers.unregister(initialize_car_exporter_defaults)
    if initialize_car_exporter_defaults_after_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(initialize_car_exporter_defaults_after_load)
    del bpy.types.Scene.car_exporter
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
