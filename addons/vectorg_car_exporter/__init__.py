bl_info = {
    "name": "VectorG Car Exporter",
    "author": "VectorG",
    "version": (0, 5, 2),
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

TIRE_TYPE_ITEMS = (
    ("soft", "Soft", "Highest configured tire grip"),
    ("medium", "Medium", "Balanced configured tire grip"),
    ("hard", "Hard", "Lowest configured tire grip"),
)

SOUND_SLOTS = {
    "tranny_on": {"label": "Transmission On", "default": "trany_power_high.wav", "rpm": 0, "loop": True, "volume": 0.6},
    "tranny_off": {"label": "Transmission Off", "default": "tw_offlow_4.wav", "rpm": 0, "loop": True, "volume": 0.1},
    "on_high": {"label": "On High", "default": "BAC_Mono_onhigh.wav", "rpm": 1000, "loop": True, "volume": 0.5},
    "on_mid": {"label": "On Mid", "default": "BAC_Mono_onmid.wav", "rpm": 1000, "loop": True, "volume": 0.45},
    "on_low": {"label": "On Low", "default": "BAC_Mono_onlow.wav", "rpm": 1000, "loop": True, "volume": 0.4},
    "off_high": {"label": "Off High", "default": "BAC_Mono_offveryhigh.wav", "rpm": 1000, "loop": True, "volume": 0.3},
    "off_mid": {"label": "Off Mid", "default": "BAC_Mono_offmid.wav", "rpm": 1000, "loop": True, "volume": 0.35},
    "off_low": {"label": "Off Low", "default": "BAC_Mono_offlow.wav", "rpm": 1000, "loop": True, "volume": 0.3},
    "limiter": {"label": "Limiter", "default": "limiter.wav", "rpm": 8000, "loop": True, "volume": 0.4},
    "turbo_flutter": {"label": "Turbo Flutter", "default": "turbo_flutter.wav", "rpm": 8000, "loop": False, "volume": 0.6},
}

ORIENTATION_DOT_THRESHOLD = math.cos(math.radians(1.0))
STEERING_WHEEL_DOT_THRESHOLD = math.cos(math.radians(45.0))
TORQUE_CURVE_NODE_GROUP = "_CarExporterTorqueCurve"
TORQUE_CURVE_NODE = "Torque Curve"
CAMERA_PREFIXES = ("chase", "cockpit", "hood", "roof")
GUIDE_PREFIX = "CAR_EXPORTER_GUIDE_"
GUIDE_PROP = "car_exporter_helper"
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
        return math.degrees(camera_obj.data.angle)
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


def ensure_camera_targets(settings):
    for prefix in CAMERA_PREFIXES:
        create_camera_target_on_selection(settings, prefix)


def guide_objects():
    return [
        obj
        for obj in bpy.data.objects
        if obj.get(GUIDE_PROP) or obj.name.startswith(GUIDE_PREFIX)
    ]


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


def with_helpers_unlinked(callback):
    helpers = guide_objects()
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


def export_materials(objects):
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
    return materials


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


def classify_texture_usage(objects):
    usage_by_image = {}
    for material in export_materials(objects):
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


def alpha_material_warnings(objects):
    warnings = []
    for material in export_materials(objects):
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


def texture_validation(objects, max_size):
    errors = []
    warnings = []
    for image, usages in classify_texture_usage(objects).items():
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
    warnings.extend(alpha_material_warnings(objects))
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
        if not math.isfinite(wheel.grip_factor) or wheel.grip_factor <= 0:
            errors.append(f"Wheel {index} grip factor must be a positive number")
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
                wheel_rest_lengths[(wheel.group, wheel.key)] = abs(mount_to_joint.dot(up_axis_world))

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
        for group in ("front", "rear"):
            wheel = getattr(preset, group)
            if wheel.tire_type not in {"soft", "medium", "hard"}:
                errors.append(f"{label} {group} tire type is invalid")
            if not all(math.isfinite(value) for value in (
                wheel.pressure,
                wheel.camber,
                wheel.toe,
                wheel.suspension_offset,
                wheel.suspension_stiffness,
                wheel.damping_relaxation,
                wheel.damping_compression,
            )):
                errors.append(f"{label} {group} adjustments must be finite")
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
            if not screen_size or screen_size.x <= 1e-6 or screen_size.y <= 1e-6:
                errors.append("Dashboard screen local X and Y dimensions must be greater than zero")

    exported_material_names = {
        material.name
        for material in export_materials(
            [obj for obj in bpy.context.scene.objects if obj not in guide_objects()]
        )
    }
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
            path = getattr(settings, f"sound_{slot}")
            if not path:
                errors.append(f"Sound slot is not assigned: {slot}")
            elif not os.path.isfile(abspath(path)):
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

    if car_obj:
        texture_errors, texture_warnings = texture_validation(
            [obj for obj in bpy.context.scene.objects if obj not in guide_objects()],
            int(settings.max_texture_size),
        )
        errors.extend(texture_errors)
        warnings.extend(texture_warnings)

    return errors, warnings


class CarColliderSettings(PropertyGroup):
    object_ref: PointerProperty(name="Object", type=bpy.types.Object, update=update_collider_object)
    collider_type: EnumProperty(name="Type", items=(("trimesh", "Trimesh", ""), ("box", "Box", "")), default="trimesh")
    mass: FloatProperty(name="Mass", default=1230.0, min=0.0)


class CarWheelSettings(PropertyGroup):
    group: StringProperty(name="Group", default="front")
    key: StringProperty(name="Key", default="l")
    steering: BoolProperty(name="Steering", default=False)
    suspension_ref: PointerProperty(name="Mount", type=bpy.types.Object)
    hub_ref: PointerProperty(name="Joint", type=bpy.types.Object)
    wheel_ref: PointerProperty(name="Spin", type=bpy.types.Object)
    up_local_axis: EnumProperty(name="Up Local Axis", items=AXIS_ITEMS, default="z")
    spin_local_axis: EnumProperty(name="Spin Local Axis", items=AXIS_ITEMS, default="x")
    suspension_stiffness: FloatProperty(name="Suspension Stiffness", default=80.0)
    damping_relaxation: FloatProperty(name="Damping Relaxation", default=2.6)
    damping_compression: FloatProperty(name="Damping Compression", default=2.0)
    radius: FloatProperty(name="Radius", default=0.3, min=0.01)
    max_brake_force: FloatProperty(name="Max Brake Force", default=5000.0, min=0.0)
    pressure: FloatProperty(name="Pressure", default=2.0, min=1.3, max=2.7)
    camber: FloatProperty(name="Camber", default=-4.0)
    toe: FloatProperty(name="Toe", default=-0.15)
    side_friction_stiffness: FloatProperty(name="Side Friction", default=1.0)
    side_factor: FloatProperty(name="Side Factor", default=1.0)
    forward_factor: FloatProperty(name="Forward Factor", default=1.6)
    brake_factor: FloatProperty(name="Brake Factor", default=1.5)
    contact_damping: FloatProperty(name="Contact Damping", default=0.15)
    grip_factor: FloatProperty(
        name="Grip Factor",
        description="Multiplier for this wheel's pressure-derived grip",
        default=1.0,
        min=0.01,
    )


class CarWheelPresetSettings(PropertyGroup):
    group: StringProperty(name="Group", default="front")
    key: StringProperty(name="Key", default="l")
    tire_type: EnumProperty(name="Tire Type", items=TIRE_TYPE_ITEMS, default="medium")
    pressure: FloatProperty(name="Pressure", default=2.0, min=1.3, max=2.7)
    camber: FloatProperty(name="Camber", default=-4.0)
    toe: FloatProperty(name="Toe", default=-0.15)
    suspension_offset: FloatProperty(
        name="Suspension Offset",
        description="Signed change to suspension rest length; positive pushes the wheel farther from the mount",
        default=0.0,
        min=-0.25,
        max=0.25,
        unit="LENGTH",
    )
    suspension_stiffness: FloatProperty(name="Suspension Stiffness", default=80.0, min=0.0)
    damping_relaxation: FloatProperty(name="Damping Relaxation", default=2.6, min=0.0)
    damping_compression: FloatProperty(name="Damping Compression", default=2.0, min=0.0)


class CarPresetSettings(PropertyGroup):
    preset_id: StringProperty(name="ID", default="default")
    display_name: StringProperty(name="Name", default="Default")
    front: PointerProperty(type=CarWheelPresetSettings)
    rear: PointerProperty(type=CarWheelPresetSettings)
    wheels: CollectionProperty(type=CarWheelPresetSettings)


class CarExporterSettings(PropertyGroup):
    is_configured: BoolProperty(name="Configured", default=False)
    car_id: StringProperty(name="Car ID", default="my_car")
    package_version: StringProperty(
        name="Package Version",
        description="Explicit asset revision; increment when package contents change",
        default="1",
    )
    display_name: StringProperty(name="Display Name", default="My Car")
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
    car_class: StringProperty(name="Class", default="GT")
    vehicle_tag_tarmac: BoolProperty(name="Tarmac", default=True)
    vehicle_tag_offroad: BoolProperty(name="Offroad", default=True)
    car_root_object: PointerProperty(name="Car Root", type=bpy.types.Object)
    center_of_mass_object: PointerProperty(name="Center of Mass", type=bpy.types.Object)
    steering_wheel_object: PointerProperty(name="Steering Wheel", type=bpy.types.Object)
    steering_wheel_spin_axis: EnumProperty(name="Steering Wheel Spin Axis", items=AXIS_ITEMS, default="y")
    headlights_material: PointerProperty(name="Headlights", type=bpy.types.Material)
    brake_lights_material: PointerProperty(name="Brake Lights", type=bpy.types.Material)
    reverse_lights_material: PointerProperty(name="Reverse Lights", type=bpy.types.Material)
    dashboard_screen_object: PointerProperty(name="Screen", type=bpy.types.Object)
    down_force: FloatProperty(name="Downforce", default=3000.0)
    air_drag: FloatProperty(name="Air Drag", default=0.5, min=0.0, max=1.0)
    anti_roll: FloatProperty(name="Anti-roll", default=0.4)
    abs: FloatProperty(name="ABS", default=1.0, min=0.0, max=1.0)
    esc: FloatProperty(name="ESC", default=0.0, min=0.0, max=1.0)
    traction_control: FloatProperty(name="Traction Control", default=1.0, min=0.0, max=1.0)
    max_steering_angle: FloatProperty(name="Max Steering Angle", default=50.0, min=1.0, max=90.0)
    use_custom_sounds: BoolProperty(name="Use Custom Sounds", default=False)
    colliders: CollectionProperty(type=CarColliderSettings)
    wheels: CollectionProperty(type=CarWheelSettings)
    presets: CollectionProperty(type=CarPresetSettings)
    active_preset_index: IntProperty(name="Active Preset", default=0)
    preset_schema_version: IntProperty(default=0, options={"HIDDEN"})
    guide_length: FloatProperty(name="Guide Length", default=4.5, min=0.1, unit="LENGTH")
    guide_width: FloatProperty(name="Guide Width", default=2.0, min=0.1, unit="LENGTH")
    guide_wheelbase: FloatProperty(name="Wheelbase", default=2.7, min=0.1, unit="LENGTH")
    guide_track_width: FloatProperty(name="Track Width", default=1.65, min=0.1, unit="LENGTH")

    drive: EnumProperty(name="Drive", items=(("awd", "AWD", ""), ("fwd", "FWD", ""), ("rwd", "RWD", "")), default="awd")
    hp: FloatProperty(name="HP", default=590.0, min=1.0)
    final_drive_ratio: FloatProperty(name="Final Drive Ratio", default=5.0, min=0.01)
    max_rpm: IntProperty(name="Max RPM", default=8000, min=1)
    idle_rpm: IntProperty(name="Idle RPM", default=1000, min=1)
    redline_rpm: IntProperty(name="Redline RPM", default=7000, min=1)
    rev_limit: IntProperty(name="Rev Limit", default=7900, min=1)
    engine_inertia: FloatProperty(name="Engine Inertia", default=0.2, min=0.01)
    engine_friction_torque: FloatProperty(name="Friction Torque", default=70.0, min=0.0)
    clutch_response: FloatProperty(name="Clutch Response", default=12.0, min=0.0)
    shift_cooldown: FloatProperty(name="Gear Change Cooldown", default=0.0, min=0.0, unit="TIME")
    auto_blip: BoolProperty(
        name="Auto Blip",
        description="Allow the game auto-blip setting to operate for this vehicle",
        default=True,
    )
    auto_blip_duration: FloatProperty(name="Auto Blip Duration", default=0.2, min=0.0, max=1.0, unit="TIME")
    turbo_enabled: BoolProperty(name="Turbo Enabled", default=True)
    turbo_boost: FloatProperty(name="Turbo Boost", default=1.35, min=1.0)
    turbo_valve: BoolProperty(name="Turbo Valve", default=False)
    max_torque: FloatProperty(name="Max Torque", default=590.0, min=1.0)
    torque_factor: FloatProperty(
        name="Torque Factor",
        description="Multiplier applied to drive and engine-braking torque before tire-force limits",
        default=1.0,
        min=0.01,
    )

    reverse_ratio: FloatProperty(name="Reverse", default=-3.57)
    forward_gear_count: IntProperty(name="Forward Gears", default=6, min=1, max=15)
    gear_1: FloatProperty(name="Gear 1", default=4.08)
    gear_2: FloatProperty(name="Gear 2", default=2.7)
    gear_3: FloatProperty(name="Gear 3", default=1.9)
    gear_4: FloatProperty(name="Gear 4", default=1.4)
    gear_5: FloatProperty(name="Gear 5", default=1.06)
    gear_6: FloatProperty(name="Gear 6", default=0.85)
    gear_7: FloatProperty(name="Gear 7", default=0.70)
    gear_8: FloatProperty(name="Gear 8", default=0.58)
    gear_9: FloatProperty(name="Gear 9", default=0.50)
    gear_10: FloatProperty(name="Gear 10", default=0.44)
    gear_11: FloatProperty(name="Gear 11", default=0.40)
    gear_12: FloatProperty(name="Gear 12", default=0.36)
    gear_13: FloatProperty(name="Gear 13", default=0.33)
    gear_14: FloatProperty(name="Gear 14", default=0.30)
    gear_15: FloatProperty(name="Gear 15", default=0.28)

    torque_1000: FloatProperty(name="1000 RPM", default=422)
    torque_2000: FloatProperty(name="2000 RPM", default=506)
    torque_3000: FloatProperty(name="3000 RPM", default=565)
    torque_4000: FloatProperty(name="4000 RPM", default=590)
    torque_5000: FloatProperty(name="5000 RPM", default=586)
    torque_6000: FloatProperty(name="6000 RPM", default=564)
    torque_7000: FloatProperty(name="7000 RPM", default=523)
    torque_8000: FloatProperty(name="8000 RPM", default=460)

    chase_camera_object: PointerProperty(name="Chase", type=bpy.types.Object, poll=camera_object_poll, update=update_chase_camera_object)
    cockpit_camera_object: PointerProperty(name="Cockpit", type=bpy.types.Object, poll=camera_object_poll, update=update_cockpit_camera_object)
    hood_camera_object: PointerProperty(name="Hood", type=bpy.types.Object, poll=camera_object_poll, update=update_hood_camera_object)
    roof_camera_object: PointerProperty(name="Roof", type=bpy.types.Object, poll=camera_object_poll, update=update_roof_camera_object)
    chase_fov: FloatProperty(name="Chase FOV", default=65)
    cockpit_fov: FloatProperty(name="Cockpit FOV", default=54)
    hood_fov: FloatProperty(name="Hood FOV", default=71)
    roof_fov: FloatProperty(name="Roof FOV", default=71)
    chase_target_distance: FloatProperty(name="Target Distance", default=5.0, min=0.01, update=update_chase_target_distance)
    cockpit_target_distance: FloatProperty(name="Target Distance", default=1.0, min=0.01, update=update_cockpit_target_distance)
    hood_target_distance: FloatProperty(name="Target Distance", default=2.0, min=0.01, update=update_hood_target_distance)
    roof_target_distance: FloatProperty(name="Target Distance", default=2.0, min=0.01, update=update_roof_target_distance)
    chase_shake: FloatProperty(name="Shake Intensity", default=16.0)
    cockpit_shake: FloatProperty(name="Shake Intensity", default=1.0)
    hood_shake: FloatProperty(name="Shake Intensity", default=1.1)
    roof_shake: FloatProperty(name="Shake Intensity", default=1.1)

    sound_tranny_on: StringProperty(name="Transmission On", subtype="FILE_PATH", default="")
    sound_tranny_off: StringProperty(name="Transmission Off", subtype="FILE_PATH", default="")
    sound_on_high: StringProperty(name="On High", subtype="FILE_PATH", default="")
    sound_on_mid: StringProperty(name="On Mid", subtype="FILE_PATH", default="")
    sound_on_low: StringProperty(name="On Low", subtype="FILE_PATH", default="")
    sound_off_high: StringProperty(name="Off High", subtype="FILE_PATH", default="")
    sound_off_mid: StringProperty(name="Off Mid", subtype="FILE_PATH", default="")
    sound_off_low: StringProperty(name="Off Low", subtype="FILE_PATH", default="")
    sound_limiter: StringProperty(name="Limiter", subtype="FILE_PATH", default="")
    sound_turbo_flutter: StringProperty(name="Turbo Flutter", subtype="FILE_PATH", default="")


def clear_configuration_settings(settings):
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
    settings.car_root_object = None
    settings.center_of_mass_object = None
    settings.steering_wheel_object = None
    settings.steering_wheel_spin_axis = "y"
    settings.headlights_material = None
    settings.brake_lights_material = None
    settings.reverse_lights_material = None
    settings.dashboard_screen_object = None
    settings.colliders.clear()
    settings.wheels.clear()
    settings.presets.clear()
    settings.active_preset_index = 0
    settings.preset_schema_version = 0
    settings.down_force = 0.0
    settings.air_drag = 0.0
    settings.anti_roll = 0.0
    settings.abs = 0.0
    settings.esc = 0.0
    settings.traction_control = 0.0
    settings.max_steering_angle = 1.0
    settings.use_custom_sounds = False
    settings.drive = "awd"
    settings.hp = 1.0
    settings.final_drive_ratio = 0.01
    settings.max_rpm = 1
    settings.idle_rpm = 1
    settings.redline_rpm = 1
    settings.rev_limit = 1
    settings.engine_inertia = 0.01
    settings.engine_friction_torque = 0.0
    settings.clutch_response = 0.0
    settings.shift_cooldown = 0.0
    settings.auto_blip = False
    settings.auto_blip_duration = 0.0
    settings.turbo_enabled = False
    settings.turbo_boost = 1.0
    settings.turbo_valve = False
    settings.max_torque = 1.0
    settings.torque_factor = 0.01
    settings.reverse_ratio = 0.0
    settings.forward_gear_count = 1
    for index in range(1, 16):
        setattr(settings, f"gear_{index}", 0.0)
    for rpm in (1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000):
        setattr(settings, f"torque_{rpm}", 0.0)
    for prefix in CAMERA_PREFIXES:
        setattr(settings, f"{prefix}_camera_object", None)
        setattr(settings, f"{prefix}_fov", 0.0)
        setattr(settings, f"{prefix}_target_distance", 0.01)
        setattr(settings, f"{prefix}_shake", 0.0)
    for slot in SOUND_SLOTS:
        setattr(settings, f"sound_{slot}", "")
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
    settings.down_force = 3000.0
    settings.air_drag = 0.5
    settings.anti_roll = 0.4
    settings.abs = 1.0
    settings.esc = 0.0
    settings.traction_control = 1.0
    settings.max_steering_angle = 50.0
    settings.drive = "awd"
    settings.hp = 590.0
    settings.final_drive_ratio = 5.0
    settings.max_rpm = 8000
    settings.idle_rpm = 1000
    settings.redline_rpm = 7000
    settings.rev_limit = 7900
    settings.engine_inertia = 0.2
    settings.engine_friction_torque = 70.0
    settings.clutch_response = 12.0
    settings.shift_cooldown = 0.0
    settings.auto_blip = True
    settings.auto_blip_duration = 0.2
    settings.turbo_enabled = True
    settings.turbo_boost = 1.35
    settings.turbo_valve = False
    settings.max_torque = 590.0
    settings.torque_factor = 1.0
    settings.reverse_ratio = -3.57
    settings.forward_gear_count = 6
    for index, value in {
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
    }.items():
        setattr(settings, f"gear_{index}", value)
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
    settings.chase_fov = 65.10000147006308
    settings.cockpit_fov = 54.43222611864906
    settings.hood_fov = 71.50777759085639
    settings.roof_fov = 71.50777759085639
    settings.chase_target_distance = 5.0
    settings.cockpit_target_distance = 1.0
    settings.hood_target_distance = 2.0
    settings.roof_target_distance = 2.0
    settings.chase_shake = 16.0
    settings.cockpit_shake = 1.0
    settings.hood_shake = 1.1
    settings.roof_shake = 1.1
    reset_torque_curve_node()
    ensure_default_wheels(settings)
    ensure_default_presets(settings)
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
            "maxBrakeForce": wheel.max_brake_force,
            "sideFrictionStiffness": wheel.side_friction_stiffness,
            "sideFactor": wheel.side_factor,
            "forwardFactor": wheel.forward_factor,
            "brakeFactor": wheel.brake_factor,
            "contactDamping": wheel.contact_damping,
            "gripFactor": wheel.grip_factor,
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
        "toe": wheel.toe,
        "suspensionOffset": wheel.suspension_offset,
        "suspensionStiffness": wheel.suspension_stiffness,
        "dampingRelaxation": wheel.damping_relaxation,
        "dampingCompression": wheel.damping_compression,
    }


def build_presets_config(settings):
    ensure_default_presets(settings)
    return [
        {
            "id": preset.preset_id,
            "name": preset.display_name,
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
    sounds = {}
    if settings.use_custom_sounds:
        for slot, meta in SOUND_SLOTS.items():
            source_path = getattr(settings, f"sound_{slot}")
            if not source_path:
                continue
            source_name = Path(abspath(source_path)).name
            sounds[slot] = {
                "source": source_name,
                "rpm": meta["rpm"],
                "loop": meta["loop"],
                "volume": meta["volume"],
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

    manifest = {
        "version": 2,
        "id": settings.car_id,
        "packageVersion": settings.package_version,
        "model": f"{settings.car_id}.glb",
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
            "finalDriveRatio": settings.final_drive_ratio,
            "maxRPM": settings.max_rpm,
            "idleRPM": settings.idle_rpm,
            "redlineRPM": settings.redline_rpm,
            "revLimit": settings.rev_limit,
            "inertia": settings.engine_inertia,
            "frictionTorque": settings.engine_friction_torque,
            "clutchResponse": settings.clutch_response,
            "shiftCooldown": settings.shift_cooldown,
            "autoBlip": settings.auto_blip,
            "autoBlipDuration": settings.auto_blip_duration,
            "torqueFactor": settings.torque_factor,
            "gearRatios": {
                **{"0": 0, "-1": settings.reverse_ratio},
                **{
                    str(index): getattr(settings, f"gear_{index}")
                    for index in range(1, settings.forward_gear_count + 1)
                },
            },
            "torqueCurve": sample_torque_curve(settings),
            "turbo": {
                "enabled": settings.turbo_enabled,
                "boost": settings.turbo_boost,
                "valve": settings.turbo_valve,
                "load": 0.0,
            },
        },
        "body": {
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
            "downForce": settings.down_force,
            "airDrag": settings.air_drag,
            "antiRoll": settings.anti_roll,
            "abs": settings.abs,
            "esc": settings.esc,
            "tractionControl": settings.traction_control,
            "maxSteeringAngle": settings.max_steering_angle,
        },
        "wheels": build_wheels_config(settings),
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
                "shake": settings.chase_shake,
            },
            "cockpit_cam": {
                "obj": object_config_name(settings.cockpit_camera_object),
                "fov": camera_fov(settings, "cockpit"),
                "shake": settings.cockpit_shake,
            },
            "hood_cam": {
                "obj": object_config_name(settings.hood_camera_object),
                "fov": camera_fov(settings, "hood"),
                "shake": settings.hood_shake,
            },
            "roof_cam": {
                "obj": object_config_name(settings.roof_camera_object),
                "fov": camera_fov(settings, "roof"),
                "shake": settings.roof_shake,
            },
        },
        "sounds": sounds,
    }
    if dashboard:
        manifest["dashboard"] = dashboard
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
    bl_label = "Add Wheel Preset"
    bl_description = "Add a wheel preset by copying the active preset"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = scene_settings(context)
        source = active_preset(settings)
        first_preset = len(settings.presets) == 0
        preset_id = "default" if first_preset else next_preset_id(settings)
        preset = settings.presets.add()
        preset.preset_id = preset_id
        preset.display_name = "Default" if first_preset else f"Preset {len(settings.presets)}"
        if source:
            copy_preset_wheels(source, preset)
        else:
            ensure_preset_wheels(preset, settings.wheels)
            for group in ("front", "rear"):
                source_wheel = next(
                    wheel for wheel in preset.wheels
                    if wheel.group == group and wheel.key == "l"
                )
                copy_wheel_preset_values(source_wheel, getattr(preset, group))
            settings.preset_schema_version = 3
        settings.active_preset_index = len(settings.presets) - 1
        return {"FINISHED"}


class CAR_EXPORTER_OT_remove_preset(Operator):
    bl_idname = "car_exporter.remove_preset"
    bl_label = "Remove Wheel Preset"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = scene_settings(context)
        if len(settings.presets) <= 1:
            self.report({"ERROR"}, "At least one wheel preset is required")
            return {"CANCELLED"}
        index = settings.active_preset_index
        if not (0 <= index < len(settings.presets)):
            return {"CANCELLED"}
        settings.presets.remove(index)
        settings.active_preset_index = min(index, len(settings.presets) - 1)
        return {"FINISHED"}


class CAR_EXPORTER_OT_move_preset(Operator):
    bl_idname = "car_exporter.move_preset"
    bl_label = "Move Wheel Preset"
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
    wheel.suspension_stiffness = mount.get("stiffness", wheel.suspension_stiffness)
    wheel.damping_relaxation = mount.get("dampingRelaxation", wheel.damping_relaxation)
    wheel.damping_compression = mount.get("dampingCompression", wheel.damping_compression)
    wheel.radius = spin_data.get("radius", wheel.radius)
    wheel.max_brake_force = spin_data.get("maxBrakeForce", wheel.max_brake_force)
    wheel.pressure = spin_data.get("pressure", wheel.pressure)
    wheel.camber = spin_data.get("camber", wheel.camber)
    wheel.toe = spin_data.get("toe", wheel.toe)
    wheel.side_friction_stiffness = spin_data.get("sideFrictionStiffness", wheel.side_friction_stiffness)
    wheel.side_factor = spin_data.get("sideFactor", wheel.side_factor)
    wheel.forward_factor = spin_data.get("forwardFactor", wheel.forward_factor)
    wheel.brake_factor = spin_data.get("brakeFactor", wheel.brake_factor)
    wheel.contact_damping = spin_data.get("contactDamping", wheel.contact_damping)
    wheel.grip_factor = spin_data["gripFactor"]
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
            "side_friction_stiffness": wheel.side_friction_stiffness,
            "side_factor": wheel.side_factor,
            "forward_factor": wheel.forward_factor,
            "brake_factor": wheel.brake_factor,
            "contact_damping": wheel.contact_damping,
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
            wheel.side_friction_stiffness = imported["side_friction_stiffness"]
            wheel.side_factor = imported["side_factor"]
            wheel.forward_factor = imported["forward_factor"]
            wheel.brake_factor = imported["brake_factor"]
            wheel.contact_damping = imported["contact_damping"]
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
                "maxBrakeForce": 5000,
                "pressure": 2.0,
                "camber": -4.0 if front_wheel else -3.0,
                "toe": -0.15 if front_wheel else 0.2,
                "sideFrictionStiffness": 1.0,
                "sideFactor": 1.0,
                "forwardFactor": 1.6,
                "brakeFactor": 1.5,
                "contactDamping": 0.15,
                "gripFactor": 1.0,
            },
        })


def default_wheel_preset_values(group):
    front_wheel = group == "front"
    return {
        "tire_type": "medium",
        "pressure": 2.0,
        "camber": -4.0 if front_wheel else -3.0,
        "toe": -0.15 if front_wheel else 0.2,
        "suspension_offset": 0.0,
        "suspension_stiffness": 80.0,
        "damping_relaxation": 2.6,
        "damping_compression": 2.0,
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
            "toe": wheel.toe,
            "suspension_offset": wheel.suspension_offset,
            "suspension_stiffness": wheel.suspension_stiffness,
            "damping_relaxation": wheel.damping_relaxation,
            "damping_compression": wheel.damping_compression,
        }
        for wheel in preset.wheels
    }
    legacy = {
        (wheel.group, wheel.key): {
            "tire_type": "medium",
            "pressure": wheel.pressure,
            "camber": wheel.camber,
            "toe": wheel.toe,
            "suspension_offset": 0.0,
            "suspension_stiffness": wheel.suspension_stiffness,
            "damping_relaxation": wheel.damping_relaxation,
            "damping_compression": wheel.damping_compression,
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
        wheel.toe = values["toe"]
        wheel.suspension_offset = values["suspension_offset"]
        wheel.suspension_stiffness = values["suspension_stiffness"]
        wheel.damping_relaxation = values["damping_relaxation"]
        wheel.damping_compression = values["damping_compression"]


def copy_wheel_preset_values(source, target):
    target.tire_type = source.tire_type
    target.pressure = source.pressure
    target.camber = source.camber
    target.toe = source.toe
    target.suspension_offset = source.suspension_offset
    target.suspension_stiffness = source.suspension_stiffness
    target.damping_relaxation = source.damping_relaxation
    target.damping_compression = source.damping_compression


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


def copy_preset_wheels(source, target):
    copy_wheel_preset_values(source.front, target.front)
    copy_wheel_preset_values(source.rear, target.rear)


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


def export_car_glb(context, filepath, max_texture_size, optimize_color_textures, jpeg_quality):
    export_objects = list(context.scene.objects)
    restored_nodes, temp_images = apply_export_texture_optimization(
        export_objects,
        max_texture_size,
        optimize_color_textures,
        Path(filepath).parent,
        jpeg_quality,
    )
    try:
        result = bpy.ops.export_scene.gltf(
            filepath=str(filepath),
            export_format="GLB",
            use_selection=False,
            export_apply=True,
            export_cameras=True,
            **gltf_image_export_options(jpeg_quality),
        )
        if "FINISHED" not in result:
            raise RuntimeError("Blender glTF export did not finish")
    finally:
        restore_export_textures(restored_nodes, temp_images)


class CAR_EXPORTER_OT_export_car_zip(Operator, ExportHelper):
    bl_idname = "car_exporter.export_car_zip"
    bl_label = "Export Car Zip"
    bl_options = {"REGISTER"}
    filename_ext = ".zip"

    filepath: StringProperty(name="Export Zip", subtype="FILE_PATH")
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
            ))

            manifest = build_manifest(settings)
            (temp_path / "manifest.json").write_text(json.dumps(manifest, indent=4), encoding="utf-8")

            copied = set()
            if settings.use_custom_sounds:
                for slot in SOUND_SLOTS:
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

    filepath: StringProperty(name="Manifest JSON", subtype="FILE_PATH")

    def execute(self, context):
        settings = scene_settings(context)
        data = json.loads(Path(abspath(self.filepath)).read_text(encoding="utf-8"))
        engine = data.get("engine", {})
        if "redlineRPM" not in engine:
            self.report({"ERROR"}, "Manifest engine.redlineRPM is required")
            return {"CANCELLED"}
        wheels_data = data.get("wheels") or {}
        for group, key, _steering in WHEEL_KEYS:
            spin_data = ((wheels_data.get(group) or {}).get(key) or {}).get("spin") or {}
            grip_factor = spin_data.get("gripFactor")
            if not isinstance(grip_factor, (int, float)) or not math.isfinite(grip_factor) or grip_factor <= 0:
                self.report(
                    {"ERROR"},
                    f"Manifest wheels.{group}.{key}.spin.gripFactor must be a positive number",
                )
                return {"CANCELLED"}

        settings.is_configured = True
        body = data.get("body", {})
        settings.car_id = data.get("id", data.get("name", settings.car_id))
        settings.package_version = str(data.get("packageVersion", settings.package_version))
        settings.display_name = data.get("displayName", data.get("name", settings.display_name))
        settings.car_class = data.get("class", settings.car_class)
        track_types = data.get("trackTypes")
        if isinstance(track_types, list):
            tags = set(track_types)
            settings.vehicle_tag_tarmac = "tarmac" in tags
            settings.vehicle_tag_offroad = "offroad" in tags
        settings.drive = engine.get("drive", settings.drive)
        settings.hp = engine.get("hp", settings.hp)
        settings.final_drive_ratio = engine.get("finalDriveRatio", settings.final_drive_ratio)
        settings.max_rpm = engine.get("maxRPM", settings.max_rpm)
        settings.idle_rpm = engine.get("idleRPM", settings.idle_rpm)
        settings.redline_rpm = engine["redlineRPM"]
        settings.rev_limit = engine.get("revLimit", settings.rev_limit)
        settings.engine_inertia = engine.get("inertia", settings.engine_inertia)
        settings.engine_friction_torque = engine.get("frictionTorque", settings.engine_friction_torque)
        settings.clutch_response = engine.get("clutchResponse", settings.clutch_response)
        settings.shift_cooldown = engine.get("shiftCooldown", settings.shift_cooldown)
        settings.auto_blip = engine.get("autoBlip") is True
        settings.auto_blip_duration = engine.get("autoBlipDuration", settings.auto_blip_duration)
        settings.torque_factor = engine.get("torqueFactor", settings.torque_factor)
        set_object_pointer(settings, "car_root_object", body.get("obj", ""))
        set_object_pointer(settings, "center_of_mass_object", body.get("centerOfMass", ""))
        settings.down_force = body.get("downForce", settings.down_force)
        settings.air_drag = body.get("airDrag", settings.air_drag)
        settings.anti_roll = body.get("antiRoll", settings.anti_roll)
        settings.abs = body.get("abs", settings.abs)
        settings.esc = body.get("esc", settings.esc)
        settings.traction_control = body.get("tractionControl", settings.traction_control)
        settings.max_steering_angle = body.get("maxSteeringAngle", settings.max_steering_angle)

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
        settings.preset_schema_version = 3
        for preset_data in data.get("presets") or []:
            if not isinstance(preset_data, dict):
                continue
            preset = settings.presets.add()
            preset.preset_id = str(preset_data.get("id", next_preset_id(settings)))
            preset.display_name = str(preset_data.get("name", preset.preset_id))
            preset_wheels = preset_data.get("wheels") or {}
            for group in ("front", "rear"):
                group_wheels = preset_wheels.get(group) or {}
                wheel_data = group_wheels.get("l") or group_wheels.get("r") or {}
                wheel = getattr(preset, group)
                wheel.tire_type = wheel_data.get("tireType", "medium")
                wheel.pressure = wheel_data.get("pressure", 2.0)
                wheel.camber = wheel_data.get("camber", -4.0 if group == "front" else -3.0)
                wheel.toe = wheel_data.get("toe", -0.15 if group == "front" else 0.2)
                wheel.suspension_offset = wheel_data.get("suspensionOffset", 0.0)
                wheel.suspension_stiffness = wheel_data.get("suspensionStiffness", 80.0)
                wheel.damping_relaxation = wheel_data.get("dampingRelaxation", 2.6)
                wheel.damping_compression = wheel_data.get("dampingCompression", 2.0)
        ensure_default_presets(settings)
        settings.active_preset_index = 0

        ratios = engine.get("gearRatios", {})
        settings.reverse_ratio = ratios.get("-1", settings.reverse_ratio)
        positive_gears = sorted(int(key) for key in ratios.keys() if key.isdigit() and int(key) > 0)
        if positive_gears:
            settings.forward_gear_count = min(max(positive_gears), 15)
        for index in range(1, 16):
            setattr(settings, f"gear_{index}", ratios.get(str(index), getattr(settings, f"gear_{index}")))

        torque = engine.get("torqueCurve", {})
        if torque:
            settings.max_torque = max(float(value) for value in torque.values())
            apply_imported_torque_curve(settings, torque)
        for rpm in (1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000):
            setattr(settings, f"torque_{rpm}", torque.get(str(rpm), getattr(settings, f"torque_{rpm}")))

        turbo = engine.get("turbo", {})
        settings.turbo_enabled = turbo.get("enabled", settings.turbo_enabled)
        settings.turbo_boost = turbo.get("boost", settings.turbo_boost)
        settings.turbo_valve = turbo.get("valve", settings.turbo_valve)
        steering_wheel = data.get("steeringWheel", "")
        if isinstance(steering_wheel, dict):
            set_object_pointer(settings, "steering_wheel_object", steering_wheel.get("obj", ""))
            settings.steering_wheel_spin_axis = GAME_AXIS_TO_BLENDER.get(
                tuple(steering_wheel.get("spinLocalAxis", [0, 0, -1])),
                "y",
            )
        else:
            set_object_pointer(settings, "steering_wheel_object", steering_wheel)
            settings.steering_wheel_spin_axis = "y"

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
            setattr(settings, f"{attr}_shake", camera.get("shake", getattr(settings, f"{attr}_shake")))

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
        row = layout.row(align=True)
        row.label(text="Sim")
        draw_split_prop(layout, wheel, "max_brake_force")
        draw_split_prop(layout, wheel, "side_friction_stiffness")
        draw_split_prop(layout, wheel, "side_factor")
        draw_split_prop(layout, wheel, "forward_factor")
        draw_split_prop(layout, wheel, "brake_factor")
        draw_split_prop(layout, wheel, "contact_damping")
        draw_split_prop(layout, wheel, "grip_factor")


def draw_presets(layout, settings):
    header = layout.row(align=True)
    header.label(text="Wheel Presets")
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
        layout.label(text="Add a wheel preset to configure adjustments", icon="INFO")
        return
    draw_split_prop(layout, preset, "preset_id")
    draw_split_prop(layout, preset, "display_name")

    for group in ("front", "rear"):
        axle_box = layout.box()
        axle_box.label(text=f"{group.title()} Wheels")
        wheel = getattr(preset, group)
        draw_split_prop(axle_box, wheel, "tire_type")
        draw_split_prop(axle_box, wheel, "pressure")
        draw_split_prop(axle_box, wheel, "camber")
        draw_split_prop(axle_box, wheel, "toe")
        draw_split_prop(axle_box, wheel, "suspension_offset")
        draw_split_prop(axle_box, wheel, "suspension_stiffness")
        draw_split_prop(axle_box, wheel, "damping_relaxation")
        draw_split_prop(axle_box, wheel, "damping_compression")


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
        draw_split_prop(layout, settings, f"{prefix}_shake", label="Shake Intensity")
        draw_split_label(layout, "FOV", f"{camera_fov(settings, prefix):.1f}", tooltip="Adjust FOV from Camera Properties")


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
            "engine_friction_torque",
            "clutch_response",
        ):
            draw_split_prop(box, settings, prop)
        draw_split_prop(box, settings, "turbo_enabled")
        draw_split_prop(box, settings, "turbo_boost")
        draw_split_prop(box, settings, "turbo_valve")

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
        draw_split_prop(box, settings, "reverse_ratio")
        draw_split_prop(box, settings, "forward_gear_count")
        for index in range(1, settings.forward_gear_count + 1):
            draw_split_prop(box, settings, f"gear_{index}")
        draw_split_prop(box, settings, "final_drive_ratio")

        box = layout.box()
        box.label(text="Body Physics")
        draw_split_prop(box, settings, "center_of_mass_object")
        for prop in ("down_force", "air_drag", "anti_roll", "abs", "esc", "traction_control", "max_steering_angle"):
            draw_split_prop(box, settings, prop)

        box = layout.box()
        box.label(text="Colliders")
        draw_colliders(box, settings)

        box = layout.box()
        box.label(text="Wheel Setup")
        draw_wheels(box, settings)

        box = layout.box()
        draw_presets(box, settings)

        box = layout.box()
        box.label(text="Cameras")
        draw_cameras(box, settings)

        box = layout.box()
        box.label(text="Audio")
        draw_split_prop(box, settings, "use_custom_sounds")
        if settings.use_custom_sounds:
            for slot, meta in SOUND_SLOTS.items():
                draw_split_prop(box, settings, f"sound_{slot}", label=meta["label"])

        box = layout.box()
        box.operator("car_exporter.remove_configuration", icon="TRASH")

        box = layout.box()
        row = box.row()
        row.operator("car_exporter.validate_car", icon="CHECKMARK")
        row.operator("car_exporter.import_car_manifest", icon="IMPORT")
        box.operator("car_exporter.export_car_zip", icon="EXPORT")


classes = (
    CarColliderSettings,
    CarWheelSettings,
    CarWheelPresetSettings,
    CarPresetSettings,
    CarExporterSettings,
    CAR_EXPORTER_UL_presets,
    CAR_EXPORTER_OT_validate_car,
    CAR_EXPORTER_OT_add_collider,
    CAR_EXPORTER_OT_remove_collider,
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
