bl_info = {
    "name": "VectorG Track Exporter",
    "author": "VectorG",
    "version": (0, 4, 0),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar > VectorG",
    "description": "Create and export VectorG track packages as <track_id>.glb + manifest.json zip",
    "category": "Import-Export",
}

import json
import math
import re
import shutil
import struct
import tempfile
import zipfile
from pathlib import Path

import bpy
from bpy.app.handlers import persistent
from bpy_extras.io_utils import ExportHelper
from mathutils import Matrix, Vector
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import Operator, Panel, PropertyGroup, UIList


ROLE_PROPERTY = "vectorg_role"
SURFACE_PROPERTY = "vectorg_surface"
SHAPE_PROPERTY = "vectorg_shape"
EVENT_PROPERTY = "vectorg_event"
ORDER_PROPERTY = "vectorg_order"

ROLE_TRACK = "track"
ROLE_SHARED = "shared"
ROLE_LAYOUTS = "layouts"
ROLE_VISUALS = "visuals"
ROLE_PBR = "pbr"
ROLE_FOLIAGE_CARDS = "foliage_cards"
ROLE_COLLISIONS = "collisions"
ROLE_OBSTACLES = "obstacles"
ROLE_SPAWN_POINTS = "spawn_points"
ROLE_EVENTS = "events"
ROLE_MAP = "map"
ROLE_SPAWN_POINT = "spawn_point"
ROLE_SURFACE = "surface"

ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
PACKAGE_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9._+-]{1,64}$")
DEFAULT_EVENT_SCALE = (5.0, 1.0, 2.0)
DEFAULT_DYNAMIC_MASS = 10.0
DYNAMIC_COLLIDER_DENSITY = 10.0
ROUTE_MAX_SPACING = 5.0
ROUTE_MAX_ANGLE_DEGREES = 5.0
ROUTE_MAX_CURVE_ERROR = 0.05
ROUTE_EVENT_MAX_DISTANCE = 30.0
ROUTE_ENDPOINT_MAX_DISTANCE = 10.0
ROUTE_EPSILON = 1e-6
MAP_ALIGNMENT_MIN_AXIS_RATIO = 1.1
DEFAULT_MAX_TEXTURE_SIZE = 4096
DEFAULT_JPEG_QUALITY = 85
TEMP_IMAGE_FILE_PROPERTY = "vectorg_temp_file"
TEXTURE_SIZE_ITEMS = (
    ("2048", "2048", "Cap exported track textures to 2048 px on their longest side"),
    ("4096", "4096", "Cap exported track textures to 4096 px on their longest side"),
    ("8192", "8192", "Cap exported track textures to 8192 px on their longest side"),
)
SURFACE_IDS = (
    "tarmac",
    "concrete",
    "curb",
    "grass",
    "gravel",
    "dirt",
    "mud",
    "sand",
    "snow",
    "ice",
)
def scene_settings(context):
    return context.scene.track_exporter


def object_name(obj):
    return obj.name if obj else ""


def abspath(path):
    return bpy.path.abspath(path) if path else ""


def image_source_path(image):
    return Path(bpy.path.abspath(image.filepath, library=image.library)) if image else None


def image_source_extension(image):
    return Path(image.filepath).suffix.lower() if image else ""


def hdr_image_poll(_settings, image):
    return image.source == "FILE" and image_source_extension(image) in {".hdr", ".exr"}


def curve_object_poll(_settings, obj):
    return obj.type == "CURVE"


def is_in_tree(root, obj):
    current = obj
    while current:
        if current == root:
            return True
        current = current.parent
    return False


def descendants(root):
    if not root:
        return []
    result = []
    stack = list(root.children)
    while stack:
        obj = stack.pop()
        result.append(obj)
        stack.extend(obj.children)
    return result


def hierarchy_descendants(root):
    """Descendants in the order they appear in Blender's object hierarchy."""
    if not root:
        return []
    result = []

    def visit(parent):
        for child in parent.children:
            result.append(child)
            visit(child)

    visit(root)
    return result


def direct_child_with_role(root, role):
    if not root:
        return None
    return next((child for child in root.children if child.get(ROLE_PROPERTY) == role), None)


def descendants_with_role(root, role):
    return [obj for obj in descendants(root) if obj.get(ROLE_PROPERTY) == role]


def object_with_role(root, role):
    if not root:
        return None
    if root.get(ROLE_PROPERTY) == role:
        return root
    return next((obj for obj in descendants(root) if obj.get(ROLE_PROPERTY) == role), None)


def create_empty(context, name, parent=None, role=None, display_type="PLAIN_AXES"):
    obj = bpy.data.objects.new(name, None)
    context.scene.collection.objects.link(obj)
    obj.empty_display_type = display_type
    obj.empty_display_size = 1.0
    if display_type == "CUBE":
        size_driver = obj.driver_add("empty_display_size").driver
        size_driver.type = "SCRIPTED"
        size_driver.expression = "1.0"
    if role:
        obj[ROLE_PROPERTY] = role
    if parent:
        obj.parent = parent
    return obj


def set_world_location(obj, location):
    matrix = obj.matrix_world.copy()
    matrix.translation = location
    obj.matrix_world = matrix


def select_only(context, obj):
    for selected in list(context.selected_objects):
        selected.select_set(False)
    obj.select_set(True)
    context.view_layer.objects.active = obj


def create_collision_hierarchy(context, parent, name_prefix):
    collisions = create_empty(context, f"{name_prefix}_COLLISIONS", parent, ROLE_COLLISIONS)
    for surface_id in SURFACE_IDS:
        ensure_surface_group(context, collisions, surface_id)
    create_empty(context, f"{name_prefix}_OBSTACLES", collisions, ROLE_OBSTACLES)
    return collisions


def create_visual_hierarchy(context, parent, name_prefix):
    visuals = create_empty(context, f"{name_prefix}_VISUALS", parent, ROLE_VISUALS)
    create_empty(context, f"{name_prefix}_PBR", visuals, ROLE_PBR)
    create_empty(context, f"{name_prefix}_FOLIAGE_CARDS", visuals, ROLE_FOLIAGE_CARDS)
    return visuals


def layout_root_name(layout_id):
    return layout_id if layout_id.startswith("layout_") else f"layout_{layout_id}"


def indexed_name_value(obj, object_type):
    match = re.search(rf"_{re.escape(object_type)}_(\d+)(?:\.\d+)?$", obj.name)
    return int(match.group(1)) if match else 0


def ordered_objects(objects, object_type):
    """Stable ordering for copied objects before assigning consecutive names."""
    return sorted(
        objects,
        key=lambda obj: (indexed_name_value(obj, object_type) or float("inf"), obj.name),
    )


def layout_named_objects(layout):
    """Find layout objects from their semantic hierarchy and properties."""
    nodes = layout_nodes(layout)
    spawn_root = nodes["spawnPoints"]
    event_root = nodes["events"]
    obstacles_root = nodes["obstacles"]

    spawns = descendants_with_role(spawn_root, ROLE_SPAWN_POINT)
    checkpoints = [
        obj for obj in hierarchy_descendants(event_root)
        if obj.get(EVENT_PROPERTY) == "checkpoint"
    ] if event_root else []
    box_colliders = [
        obj for obj in descendants(obstacles_root)
        if obj.get(SHAPE_PROPERTY) == "box"
    ] if obstacles_root else []
    static_boxes = [obj for obj in box_colliders if obj.get("vectorg_body") != "dynamic"]
    dynamic_boxes = [obj for obj in box_colliders if obj.get("vectorg_body") == "dynamic"]

    return {
        "spawns": spawns,
        "checkpoints": checkpoints,
        "static_boxes": static_boxes,
        "dynamic_boxes": dynamic_boxes,
    }


def apply_generated_names(name_map):
    """Rename a group atomically enough to avoid accidental Blender suffixes."""
    name_map = {obj: name for obj, name in name_map.items() if obj and obj.name != name}
    sources = set(name_map)
    duplicate_names = {
        name for name in name_map.values()
        if sum(candidate == name for candidate in name_map.values()) > 1
    }
    conflicts = [
        name for name in name_map.values()
        if (existing := bpy.data.objects.get(name)) and existing not in sources
    ]
    if conflicts or duplicate_names:
        return False, sorted(set(conflicts) | duplicate_names)

    for index, obj in enumerate(name_map):
        temporary_name = f"__vectorg_rename_{index}__"
        while bpy.data.objects.get(temporary_name):
            temporary_name += "_"
        obj.name = temporary_name
    for obj, name in name_map.items():
        obj.name = name
    return True, []


def sync_layout_node_names(layout):
    root = layout.root_object
    if not root or not layout.layout_id:
        return

    layout_id = layout.layout_id
    named = layout_named_objects(layout)
    name_map = {root: layout_root_name(layout_id)}
    root["vectorg_layout_id"] = layout_id

    role_names = {
        ROLE_VISUALS: f"{layout_id}_VISUALS",
        ROLE_COLLISIONS: f"{layout_id}_COLLISIONS",
        ROLE_SPAWN_POINTS: f"{layout_id}_SPAWN_POINTS",
        ROLE_EVENTS: f"{layout_id}_EVENTS",
        ROLE_MAP: f"{layout_id}_MAP",
    }
    for child in root.children:
        generated_name = role_names.get(child.get(ROLE_PROPERTY))
        if generated_name:
            name_map[child] = generated_name

    visuals = direct_child_with_role(root, ROLE_VISUALS)
    if visuals:
        for child in visuals.children:
            if child.get(ROLE_PROPERTY) == ROLE_PBR:
                name_map[child] = f"{layout_id}_PBR"
            elif child.get(ROLE_PROPERTY) == ROLE_FOLIAGE_CARDS:
                name_map[child] = f"{layout_id}_FOLIAGE_CARDS"

    collisions = direct_child_with_role(root, ROLE_COLLISIONS)
    if collisions:
        for child in collisions.children:
            if child.get(ROLE_PROPERTY) == ROLE_SURFACE:
                name_map[child] = f"{layout_id}_COLLISIONS_{child.get(SURFACE_PROPERTY)}"
            elif child.get(ROLE_PROPERTY) == ROLE_OBSTACLES:
                name_map[child] = f"{layout_id}_OBSTACLES"

    for index, obj in enumerate(ordered_objects(named["spawns"], "spawn"), start=1):
        name_map[obj] = f"{layout_id}_spawn_{index:02d}"
    for index, obj in enumerate(named["checkpoints"], start=1):
        obj[ORDER_PROPERTY] = index
        name_map[obj] = f"{layout_id}_checkpoint_{index:02d}"
    events = object_with_role(root, ROLE_EVENTS)
    for event_type in ("start_finish", "start", "finish"):
        for obj in descendants(events):
            if obj.get(EVENT_PROPERTY) == event_type:
                name_map[obj] = f"{layout_id}_{event_type}"
    for index, obj in enumerate(ordered_objects(named["static_boxes"], "static_box"), start=1):
        name_map[obj] = f"{layout_id}_static_box_{index:02d}"
    for index, obj in enumerate(ordered_objects(named["dynamic_boxes"], "dynamic_box"), start=1):
        name_map[obj] = f"{layout_id}_dynamic_box_{index:02d}"
    if layout.map_curve:
        name_map[layout.map_curve] = f"{layout_id}_map_curve"

    for obj in [root, *descendants(root)]:
        obj.pop("vectorg_generated_kind", None)
        obj.pop("vectorg_generated_index", None)

    success, conflicts = apply_generated_names(name_map)
    if conflicts:
        layout["vectorg_name_conflicts"] = ", ".join(conflicts)
    elif "vectorg_name_conflicts" in layout:
        del layout["vectorg_name_conflicts"]
    return success


def update_layout_id(layout, _context):
    sync_layout_node_names(layout)


def update_map_curve(layout, context):
    curve = layout.map_curve
    if not curve or not layout.root_object:
        return
    settings = scene_settings(context)
    for other_layout in settings.layouts:
        if other_layout.as_pointer() != layout.as_pointer() and other_layout.map_curve == curve:
            other_layout.map_curve = None
    map_root = direct_child_with_role(layout.root_object, ROLE_MAP)
    if not map_root:
        map_root = create_empty(context, f"{layout.layout_id}_MAP", layout.root_object, ROLE_MAP)
    world_matrix = curve.matrix_world.copy()
    curve.parent = map_root
    curve.matrix_world = world_matrix
    sync_layout_node_names(layout)
    update_layout_length(layout)
    select_only(context, curve)


@persistent
def update_curve_lengths(_scene, depsgraph):
    updated_ids = set()
    for update in depsgraph.updates:
        updated_ids.add(update.id)
        updated_ids.add(getattr(update.id, "original", update.id))
    for scene in bpy.data.scenes:
        settings = getattr(scene, "track_exporter", None)
        if not settings or not settings.is_configured:
            continue
        for layout in settings.layouts:
            curve = layout.map_curve
            if curve and (curve in updated_ids or curve.data in updated_ids):
                update_layout_length(layout)


def update_layout_visibility(layout, _context):
    if layout.root_object:
        hidden = not layout.visible
        for obj in [layout.root_object, *descendants(layout.root_object)]:
            obj.hide_set(hidden)
            obj.hide_render = hidden


def create_layout_hierarchy(context, track_root, layout_id):
    root = create_empty(context, layout_root_name(layout_id), track_root, "layout")
    root["vectorg_layout_id"] = layout_id
    visuals = create_visual_hierarchy(context, root, layout_id)
    collisions = create_collision_hierarchy(context, root, layout_id)
    spawn_points = create_empty(context, f"{layout_id}_SPAWN_POINTS", root, ROLE_SPAWN_POINTS)
    events = create_empty(context, f"{layout_id}_EVENTS", root, ROLE_EVENTS)
    map_root = create_empty(context, f"{layout_id}_MAP", root, ROLE_MAP)
    return root, visuals, collisions, spawn_points, events, map_root


def layout_generated_node_names(layout_id):
    names = {
        layout_root_name(layout_id),
        f"{layout_id}_VISUALS",
        f"{layout_id}_PBR",
        f"{layout_id}_FOLIAGE_CARDS",
        f"{layout_id}_COLLISIONS",
        f"{layout_id}_SPAWN_POINTS",
        f"{layout_id}_EVENTS",
        f"{layout_id}_MAP",
        f"{layout_id}_OBSTACLES",
    }
    names.update(f"{layout_id}_COLLISIONS_{surface_id}" for surface_id in SURFACE_IDS)
    return names


def next_layout_id(settings):
    reserved_ids = {layout.layout_id for layout in settings.layouts}
    index = 1
    while True:
        candidate = f"layout_{index}"
        names_available = all(bpy.data.objects.get(name) is None for name in layout_generated_node_names(candidate))
        if candidate not in reserved_ids and names_available:
            return candidate
        index += 1


def active_layout(settings):
    if 0 <= settings.active_layout_index < len(settings.layouts):
        return settings.layouts[settings.active_layout_index]
    return None


def collider_scope_root(settings, scope):
    if scope == "SHARED":
        return settings.shared_root_object
    layout = active_layout(settings)
    return layout.root_object if layout else None


def collider_obstacles_root(settings, scope):
    scope_root = collider_scope_root(settings, scope)
    collisions = object_with_role(scope_root, ROLE_COLLISIONS)
    return object_with_role(collisions, ROLE_OBSTACLES)


def selected_mesh(context):
    selected = [obj for obj in context.selected_objects if obj.type == "MESH"]
    return selected[0] if len(selected) == 1 and len(context.selected_objects) == 1 else None


def selected_meshes(context):
    return [obj for obj in context.selected_objects if obj.type == "MESH"]


def bounds_collider_matrix(context, target):
    evaluated = target.evaluated_get(context.evaluated_depsgraph_get())
    corners = [Vector(corner) for corner in evaluated.bound_box]
    minimum = Vector(tuple(min(corner[axis] for corner in corners) for axis in range(3)))
    maximum = Vector(tuple(max(corner[axis] for corner in corners) for axis in range(3)))
    center = (minimum + maximum) * 0.5
    half_extents = (maximum - minimum) * 0.5
    target_world = evaluated.matrix_world
    world_scale = target_world.to_scale()
    scaled_half_extents = Vector((
        half_extents.x * abs(world_scale.x),
        half_extents.y * abs(world_scale.y),
        half_extents.z * abs(world_scale.z),
    ))
    world_center = target_world @ center
    return Matrix.LocRotScale(world_center, target_world.to_quaternion(), scaled_half_extents)


def combined_bounds_collider_matrix(context, targets):
    depsgraph = context.evaluated_depsgraph_get()
    world_corners = []
    for target in targets:
        evaluated = target.evaluated_get(depsgraph)
        world_corners.extend(evaluated.matrix_world @ Vector(corner) for corner in evaluated.bound_box)
    minimum = Vector(tuple(min(corner[axis] for corner in world_corners) for axis in range(3)))
    maximum = Vector(tuple(max(corner[axis] for corner in world_corners) for axis in range(3)))
    center = (minimum + maximum) * 0.5
    half_extents = (maximum - minimum) * 0.5
    matrix = Matrix.Diagonal((half_extents.x, half_extents.y, half_extents.z, 1.0))
    matrix.translation = center
    return matrix


def box_mass_from_matrix(matrix):
    half_extents = matrix.to_scale()
    volume = 8.0 * abs(half_extents.x * half_extents.y * half_extents.z)
    return round(max(1.0, volume * DYNAMIC_COLLIDER_DENSITY), 2)


def sync_dynamic_collider_metadata(settings):
    for link in settings.dynamic_colliders:
        collider = link.collider_object
        target = link.target_object
        if not collider:
            continue
        collider[SHAPE_PROPERTY] = "box"
        collider["vectorg_body"] = "dynamic"
        collider["vectorg_mass"] = float(link.mass)
        if target:
            collider["vectorg_target"] = target.name
        elif "vectorg_target" in collider:
            del collider["vectorg_target"]


def find_surface_group(collision_root, surface_id):
    if not collision_root:
        return None
    for child in collision_root.children:
        configured_surface = child.get(SURFACE_PROPERTY)
        if child.get(ROLE_PROPERTY) == ROLE_SURFACE and configured_surface == surface_id:
            return child
    return None


def ensure_surface_group(context, collision_root, surface_id):
    group = find_surface_group(collision_root, surface_id)
    if group:
        return group
    group = create_empty(context, f"{collision_root.name}_{surface_id}", collision_root, ROLE_SURFACE)
    group[SURFACE_PROPERTY] = surface_id
    return group


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


def track_export_objects(track_root, excluded_objects=()):
    excluded_objects = set(excluded_objects)
    return [
        obj
        for obj in [track_root, *descendants(track_root)]
        if obj not in excluded_objects
    ]


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


def image_source_exists(image):
    if image.packed_file or image.source != "FILE":
        return True
    source = image_source_path(image)
    return bool(source and source.is_file())


def texture_validation(objects, max_size, hdr_image=None):
    errors = []
    warnings = []
    usage_by_image = classify_texture_usage(objects)
    for image, usages in usage_by_image.items():
        if image == hdr_image:
            errors.append("HDR environment image must not be used by an exported track material")
            continue
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
    copy = image.copy()
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
    if max_size <= 0:
        return [], []
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


def gltf_mesh_instance_export_options():
    properties = {
        prop.identifier
        for prop in bpy.ops.export_scene.gltf.get_rna_type().properties
    }
    if "export_gpu_instances" not in properties:
        return {}
    return {"export_gpu_instances": True}


def glb_json(filepath):
    with Path(filepath).open("rb") as source:
        header = source.read(12)
        if len(header) != 12:
            raise RuntimeError("Exported GLB has an invalid header")
        magic, version, total_length = struct.unpack("<4sII", header)
        if magic != b"glTF" or version != 2:
            raise RuntimeError("Exported model is not a glTF 2.0 binary")
        if total_length != Path(filepath).stat().st_size:
            raise RuntimeError("Exported GLB length is invalid")
        while source.tell() < total_length:
            chunk_header = source.read(8)
            if len(chunk_header) != 8:
                break
            chunk_length, chunk_type = struct.unpack("<II", chunk_header)
            chunk = source.read(chunk_length)
            if chunk_type == 0x4E4F534A:
                return json.loads(chunk.rstrip(b" \x00").decode("utf-8"))
    raise RuntimeError("Exported GLB has no JSON chunk")


def validate_hdr_not_embedded(filepath, hdr_image):
    if not hdr_image:
        return
    identifiers = {
        hdr_image.name.lower(),
        Path(hdr_image.filepath or "").name.lower(),
        Path(hdr_image.filepath or "").stem.lower(),
    }
    identifiers.discard("")
    for image in glb_json(filepath).get("images", []):
        name = str(image.get("name", "")).lower()
        uri = str(image.get("uri", "")).lower()
        if name in identifiers or Path(uri).name in identifiers or Path(uri).stem in identifiers:
            raise RuntimeError("HDR environment image was embedded in the GLB")


def resolved_surface(collision_root, obj):
    current = obj
    while current and current != collision_root:
        value = current.get(SURFACE_PROPERTY)
        if isinstance(value, str) and value.strip():
            return value.strip()
        current = current.parent
    return None


def valid_id(value):
    return bool(value and ID_PATTERN.fullmatch(value))


def layout_nodes(layout):
    root = layout.root_object
    collisions = object_with_role(root, ROLE_COLLISIONS)
    return {
        "root": root,
        "visuals": object_with_role(root, ROLE_VISUALS),
        "collisions": collisions,
        "obstacles": object_with_role(collisions, ROLE_OBSTACLES),
        "spawnPoints": object_with_role(root, ROLE_SPAWN_POINTS),
        "events": object_with_role(root, ROLE_EVENTS),
    }


def layout_event_config(layout):
    nodes = layout_nodes(layout)
    events_root = nodes["events"]
    event_objects = [
        obj for obj in descendants(events_root)
        if obj.get(EVENT_PROPERTY) in {"start_finish", "start", "finish", "checkpoint", "reset_zone", "track_limit"}
    ] if events_root else []
    event_objects.sort(key=lambda obj: (
        {"start_finish": 0, "start": 0, "checkpoint": 1, "finish": 2}.get(obj.get(EVENT_PROPERTY), 3),
        int(obj.get(ORDER_PROPERTY, 0)),
        obj.name,
    ))

    result = []
    for obj in event_objects:
        entry = {
            "object": obj.name,
            "type": obj[EVENT_PROPERTY],
        }
        if obj.get(EVENT_PROPERTY) == "checkpoint":
            entry["order"] = int(obj.get(ORDER_PROPERTY, 0))
        result.append(entry)
    return result


def split_bezier(points):
    p0, p1, p2, p3 = points
    p01 = (p0 + p1) * 0.5
    p12 = (p1 + p2) * 0.5
    p23 = (p2 + p3) * 0.5
    p012 = (p01 + p12) * 0.5
    p123 = (p12 + p23) * 0.5
    midpoint = (p012 + p123) * 0.5
    return (p0, p01, p012, midpoint), (midpoint, p123, p23, p3)


def bezier_needs_subdivision(points):
    p0, p1, p2, p3 = points
    chord = (p3 - p0).length
    control_length = (p1 - p0).length + (p2 - p1).length + (p3 - p2).length
    start_tangent = p1 - p0
    end_tangent = p3 - p2
    tangent_angle = 0.0
    if start_tangent.length_squared > 1e-12 and end_tangent.length_squared > 1e-12:
        tangent_angle = start_tangent.angle(end_tangent)
    return (
        chord > ROUTE_MAX_SPACING
        or control_length - chord > ROUTE_MAX_CURVE_ERROR
        or tangent_angle > math.radians(ROUTE_MAX_ANGLE_DEGREES)
    )


def sample_bezier_segment(points, start_parameter=0.0, end_parameter=1.0, depth=0):
    if depth >= 18 or not bezier_needs_subdivision(points):
        return [(points[0], start_parameter), (points[3], end_parameter)]
    left, right = split_bezier(points)
    midpoint = (start_parameter + end_parameter) * 0.5
    left_points = sample_bezier_segment(left, start_parameter, midpoint, depth + 1)
    right_points = sample_bezier_segment(right, midpoint, end_parameter, depth + 1)
    return left_points[:-1] + right_points


def subdivide_line(start, end):
    distance = (end - start).length
    segments = max(1, int(math.ceil(distance / ROUTE_MAX_SPACING)))
    return [
        (start.lerp(end, index / segments), index / segments)
        for index in range(segments + 1)
    ]


def adaptive_route_samples(curve_object):
    if not curve_object or len(curve_object.data.splines) != 1:
        return [], False
    spline = curve_object.data.splines[0]
    matrix = curve_object.matrix_world
    route = []

    if spline.type == "BEZIER":
        points = list(spline.bezier_points)
        segment_count = len(points) if spline.use_cyclic_u else len(points) - 1
        for index in range(max(0, segment_count)):
            next_index = (index + 1) % len(points)
            segment = tuple(matrix @ value for value in (
                points[index].co,
                points[index].handle_right,
                points[next_index].handle_left,
                points[next_index].co,
            ))
            sampled = sample_bezier_segment(segment)
            samples = [
                {
                    "position": position,
                    "tilt": points[index].tilt
                    + (points[next_index].tilt - points[index].tilt) * parameter,
                }
                for position, parameter in sampled
            ]
            route.extend(samples if not route else samples[1:])
    elif spline.type == "POLY":
        control_points = list(spline.points)
        points = [matrix @ Vector(point.co[:3]) for point in control_points]
        segment_count = len(points) if spline.use_cyclic_u else len(points) - 1
        for index in range(max(0, segment_count)):
            next_index = (index + 1) % len(points)
            sampled = subdivide_line(points[index], points[next_index])
            samples = [
                {
                    "position": position,
                    "tilt": control_points[index].tilt
                    + (control_points[next_index].tilt - control_points[index].tilt) * parameter,
                }
                for position, parameter in sampled
            ]
            route.extend(samples if not route else samples[1:])

    closed = bool(spline.use_cyclic_u)
    if closed and len(route) > 1 and (
        route[-1]["position"] - route[0]["position"]
    ).length < 1e-5:
        route.pop()
    return route, closed


def adaptive_route_points(curve_object):
    samples, closed = adaptive_route_samples(curve_object)
    return [sample["position"] for sample in samples], closed


def route_length(samples, closed):
    if len(samples) < 2:
        return 0.0
    positions = [
        sample["position"] if isinstance(sample, dict) else sample
        for sample in samples
    ]
    length = sum(
        (positions[index] - positions[index - 1]).length
        for index in range(1, len(positions))
    )
    if closed:
        length += (positions[0] - positions[-1]).length
    return length


def update_layout_length(layout):
    samples, closed = adaptive_route_samples(layout.map_curve)
    if len(samples) >= 2:
        layout.length = route_length(samples, closed) / 1000.0
    return [sample["position"] for sample in samples], closed


def route_event_objects(layout):
    events_root = layout_nodes(layout)["events"]
    events = [
        obj for obj in descendants(events_root)
        if obj.get(EVENT_PROPERTY) in {"start_finish", "start", "finish", "checkpoint"}
    ] if events_root else []
    return sorted(events, key=lambda obj: (
        {"start_finish": 0, "start": 0, "checkpoint": 1, "finish": 2}.get(
            obj.get(EVENT_PROPERTY),
            3,
        ),
        int(obj.get(ORDER_PROPERTY, 0)),
        obj.name,
    ))


def closest_route_position(samples, closed, position):
    segment_count = len(samples) if closed else len(samples) - 1
    best = None
    for index in range(max(0, segment_count)):
        start = samples[index]["position"]
        end = samples[(index + 1) % len(samples)]["position"]
        delta = end - start
        length_squared = delta.length_squared
        if length_squared <= ROUTE_EPSILON:
            continue
        amount = max(0.0, min(1.0, (position - start).dot(delta) / length_squared))
        point = start + delta * amount
        distance_squared = (point - position).length_squared
        if best is None or distance_squared < best["distanceSquared"]:
            best = {
                "segment": index,
                "amount": amount,
                "point": point,
                "distanceSquared": distance_squared,
            }
    return best


def rebase_closed_route(samples, position):
    projection = closest_route_position(samples, True, position)
    if not projection:
        raise ValueError("cannot project the start/finish event onto the route")
    index = projection["segment"]
    next_index = (index + 1) % len(samples)
    amount = projection["amount"]
    if amount <= ROUTE_EPSILON:
        start_index = index
        return samples[start_index:] + samples[:start_index]
    if amount >= 1.0 - ROUTE_EPSILON:
        start_index = next_index
        return samples[start_index:] + samples[:start_index]
    start = samples[index]
    end = samples[next_index]
    seam = {
        "position": projection["point"],
        "tilt": start["tilt"] + (end["tilt"] - start["tilt"]) * amount,
    }
    return [seam] + samples[next_index:] + samples[:next_index]


def route_distances(samples, closed):
    cumulative = [0.0]
    for index in range(1, len(samples)):
        distance = (samples[index]["position"] - samples[index - 1]["position"]).length
        if distance <= ROUTE_EPSILON:
            raise ValueError("contains duplicate consecutive samples")
        cumulative.append(cumulative[-1] + distance)
    total = cumulative[-1]
    if closed:
        closing_distance = (samples[0]["position"] - samples[-1]["position"]).length
        if closing_distance <= ROUTE_EPSILON:
            raise ValueError("contains a duplicate closing sample")
        total += closing_distance
    if total <= ROUTE_EPSILON:
        raise ValueError("has zero length")
    return cumulative, total


def route_tangents(samples, closed):
    tangents = []
    for index in range(len(samples)):
        if closed:
            previous = samples[(index - 1) % len(samples)]["position"]
            following = samples[(index + 1) % len(samples)]["position"]
        elif index == 0:
            previous = samples[0]["position"]
            following = samples[1]["position"]
        elif index == len(samples) - 1:
            previous = samples[-2]["position"]
            following = samples[-1]["position"]
        else:
            previous = samples[index - 1]["position"]
            following = samples[index + 1]["position"]
        tangent = following - previous
        if tangent.length_squared <= ROUTE_EPSILON:
            raise ValueError(f"has an undefined tangent at sample {index}")
        tangents.append(tangent.normalized())
    return tangents


def projected_up(reference, tangent):
    up = reference - tangent * reference.dot(tangent)
    if up.length_squared <= ROUTE_EPSILON:
        fallback = min(
            (Vector((1.0, 0.0, 0.0)), Vector((0.0, 1.0, 0.0)), Vector((0.0, 0.0, 1.0))),
            key=lambda axis: abs(axis.dot(tangent)),
        )
        up = fallback - tangent * fallback.dot(tangent)
    if up.length_squared <= ROUTE_EPSILON:
        raise ValueError("cannot construct a stable route frame")
    return up.normalized()


def transport_up(up, previous_tangent, tangent):
    axis = previous_tangent.cross(tangent)
    dot = max(-1.0, min(1.0, previous_tangent.dot(tangent)))
    if axis.length_squared <= ROUTE_EPSILON:
        if dot < 0.0:
            raise ValueError("contains a 180-degree tangent reversal")
        return projected_up(up, tangent)
    transported = Matrix.Rotation(
        math.acos(dot),
        3,
        axis.normalized(),
    ) @ up
    return projected_up(transported, tangent)


def signed_angle_around_axis(start, end, axis):
    return math.atan2(axis.dot(start.cross(end)), max(-1.0, min(1.0, start.dot(end))))


def route_frames(samples, closed, cumulative, total, reference_up):
    tangents = route_tangents(samples, closed)
    base_ups = [projected_up(reference_up, tangents[0])]
    for index in range(1, len(samples)):
        base_ups.append(transport_up(base_ups[-1], tangents[index - 1], tangents[index]))

    seam_correction = 0.0
    if closed:
        transported = transport_up(base_ups[-1], tangents[-1], tangents[0])
        seam_correction = signed_angle_around_axis(transported, base_ups[0], tangents[0])

    frames = []
    for index, sample in enumerate(samples):
        tangent = tangents[index]
        distributed_roll = seam_correction * cumulative[index] / total if closed else 0.0
        up = Matrix.Rotation(distributed_roll + sample["tilt"], 3, tangent) @ base_ups[index]
        up = projected_up(up, tangent)
        frames.append({"forward": tangent, "up": up})
    return frames


def game_vector(value):
    return [round(value.x, 6), round(value.z, 6), round(-value.y, 6)]


def route_event_data(layout, samples, closed, cumulative, total):
    result = []
    for obj in route_event_objects(layout):
        projection = closest_route_position(samples, closed, obj.matrix_world.translation)
        if not projection:
            raise ValueError(f"cannot project event {obj.name} onto the route")
        distance = math.sqrt(projection["distanceSquared"])
        if distance > ROUTE_EVENT_MAX_DISTANCE:
            raise ValueError(
                f"event {obj.name} is {distance:.2f} m from the route; "
                f"maximum is {ROUTE_EVENT_MAX_DISTANCE:.0f} m"
            )
        segment = projection["segment"]
        next_index = (segment + 1) % len(samples)
        segment_length = (
            samples[next_index]["position"] - samples[segment]["position"]
        ).length
        route_distance = cumulative[segment] + segment_length * projection["amount"]
        if closed and obj.get(EVENT_PROPERTY) == "start_finish":
            route_distance = 0.0
        if closed and total - route_distance <= ROUTE_EPSILON:
            route_distance = 0.0
        entry = {
            "object": obj.name,
            "type": obj[EVENT_PROPERTY],
            "s": round(route_distance, 6),
        }
        if obj.get(EVENT_PROPERTY) == "checkpoint":
            entry["order"] = int(obj.get(ORDER_PROPERTY, 0))
        result.append(entry)
    return result


def validate_route_events(layout, events, total):
    by_type = {}
    for event in events:
        by_type.setdefault(event["type"], []).append(event)

    if layout.route_type == "point_to_point":
        starts = by_type.get("start", [])
        finishes = by_type.get("finish", [])
        if starts and starts[0]["s"] > ROUTE_ENDPOINT_MAX_DISTANCE:
            raise ValueError(
                f"start event projects to {starts[0]['s']:.2f} m; "
                f"it must be within {ROUTE_ENDPOINT_MAX_DISTANCE:.0f} m of the route start"
            )
        if finishes and total - finishes[0]["s"] > ROUTE_ENDPOINT_MAX_DISTANCE:
            raise ValueError(
                f"finish event projects to {finishes[0]['s']:.2f} m; "
                f"it must be within {ROUTE_ENDPOINT_MAX_DISTANCE:.0f} m of the route end"
            )

    checkpoints = sorted(by_type.get("checkpoint", []), key=lambda event: event["order"])
    for previous, current in zip(checkpoints, checkpoints[1:]):
        if current["s"] <= previous["s"] + ROUTE_EPSILON:
            raise ValueError(
                f"checkpoint {current['order']} does not follow checkpoint "
                f"{previous['order']} along the route"
            )


def layout_route_data(layout):
    samples, closed = adaptive_route_samples(layout.map_curve)
    if len(samples) < 2:
        raise ValueError("must contain at least two route samples")

    if closed:
        start_finish = next(
            (
                obj for obj in route_event_objects(layout)
                if obj.get(EVENT_PROPERTY) == "start_finish"
            ),
            None,
        )
        if not start_finish:
            raise ValueError("needs a start/finish event to define route distance zero")
        samples = rebase_closed_route(samples, start_finish.matrix_world.translation)

    cumulative, total = route_distances(samples, closed)
    reference_up = layout.map_curve.matrix_world.to_3x3() @ Vector((0.0, 0.0, 1.0))
    if reference_up.length_squared <= ROUTE_EPSILON:
        raise ValueError("map curve has an invalid world up axis")
    frames = route_frames(samples, closed, cumulative, total, reference_up.normalized())
    events = route_event_data(layout, samples, closed, cumulative, total)
    validate_route_events(layout, events, total)
    layout.length = total / 1000.0

    return {
        "version": 2,
        "closed": closed,
        "length": round(total, 6),
        "maxSpacing": ROUTE_MAX_SPACING,
        "frame": "parallel_transport",
        "samples": [
            {
                "s": round(cumulative[index], 6),
                "position": game_vector(sample["position"]),
                "forward": game_vector(frames[index]["forward"]),
                "up": game_vector(frames[index]["up"]),
            }
            for index, sample in enumerate(samples)
        ],
        "events": events,
    }


def build_manifest(settings):
    shared_collisions = object_with_role(settings.shared_root_object, ROLE_COLLISIONS)
    shared_obstacles = object_with_role(shared_collisions, ROLE_OBSTACLES)
    shared_visuals = object_with_role(settings.shared_root_object, ROLE_VISUALS)
    layouts = []

    for layout in settings.layouts:
        if layout.map_curve:
            update_layout_length(layout)
        nodes = layout_nodes(layout)
        track_types = [
            value for value, enabled in (
                ("tarmac", layout.track_type_tarmac),
                ("offroad", layout.track_type_offroad),
            ) if enabled
        ]
        layout_config = {
            "id": layout.layout_id,
            "name": layout.display_name,
            "description": layout.description,
            "discipline": layout.discipline,
            "routeType": layout.route_type,
            "length": layout.length,
            "trackTypes": track_types,
            "nodes": {key: object_name(value) for key, value in nodes.items()},
            "spawnPoints": sorted(
                obj.name for obj in descendants_with_role(nodes["spawnPoints"], ROLE_SPAWN_POINT)
            ),
            "timeAttack": {
                "events": layout_event_config(layout),
            },
        }
        if layout.map_curve:
            layout_config["map"] = f"maps/{layout.layout_id}.svg"
            layout_config["route"] = f"routes/{layout.layout_id}.json"
        layouts.append(layout_config)

    config = {
        "version": 1,
        "id": settings.track_id,
        "packageVersion": settings.package_version,
        "displayName": settings.display_name,
        "model": f"{settings.track_id}.glb",
        "root": object_name(settings.track_root_object),
        "shared": {
            "root": object_name(settings.shared_root_object),
            "visuals": object_name(shared_visuals),
            "collisions": object_name(shared_collisions),
            "obstacles": object_name(shared_obstacles),
        },
        "layouts": layouts,
    }
    if settings.hdr_image:
        config["hdr"] = "hdr/env" + image_source_extension(settings.hdr_image)
    return config


def curve_svg_commands(curve_object):
    """Convert supported curve splines to SVG commands in projected world XY coordinates."""
    matrix = curve_object.matrix_world
    paths = []
    coordinates = []

    def projected(point):
        world = matrix @ Vector(point[:3])
        result = (float(world.x), float(world.y))
        coordinates.append(result)
        return result

    for spline in curve_object.data.splines:
        commands = []
        if spline.type == "BEZIER":
            points = list(spline.bezier_points)
            if len(points) < 2:
                continue
            projected_points = [projected(point.co) for point in points]
            right_handles = [projected(point.handle_right) for point in points]
            left_handles = [projected(point.handle_left) for point in points]
            commands.append(("M", projected_points[0]))
            segment_count = len(points) if spline.use_cyclic_u else len(points) - 1
            for index in range(segment_count):
                next_index = (index + 1) % len(points)
                commands.append((
                    "C",
                    right_handles[index],
                    left_handles[next_index],
                    projected_points[next_index],
                ))
            if spline.use_cyclic_u:
                commands.append(("Z",))
        elif spline.type == "POLY":
            points = [projected(point.co) for point in spline.points]
            if len(points) < 2:
                continue
            commands.append(("M", points[0]))
            commands.extend(("L", point) for point in points[1:])
            if spline.use_cyclic_u:
                commands.append(("Z",))
        if commands:
            paths.append(commands)
    return paths, coordinates


def write_layout_map_svg(layout, filepath):
    paths, coordinates = curve_svg_commands(layout.map_curve)
    if not paths or not coordinates:
        raise RuntimeError(f"{layout.display_name or layout.layout_id} map curve has no exportable splines")

    route_points, route_closed = adaptive_route_points(layout.map_curve)
    alignment_samples = []
    segment_count = len(route_points) if route_closed else len(route_points) - 1
    for index in range(max(0, segment_count)):
        start = route_points[index]
        end = route_points[(index + 1) % len(route_points)]
        weight = (end - start).length
        if weight > 1e-9:
            alignment_samples.append((((start.x + end.x) * 0.5, (start.y + end.y) * 0.5), weight))
    if not alignment_samples:
        alignment_samples = [(point, 1.0) for point in coordinates]
    total_weight = sum(weight for _point, weight in alignment_samples)
    center_x = sum(point[0] * weight for point, weight in alignment_samples) / total_weight
    center_y = sum(point[1] * weight for point, weight in alignment_samples) / total_weight
    covariance_xx = sum(
        (point[0] - center_x) ** 2 * weight for point, weight in alignment_samples
    ) / total_weight
    covariance_yy = sum(
        (point[1] - center_y) ** 2 * weight for point, weight in alignment_samples
    ) / total_weight
    covariance_xy = sum(
        (point[0] - center_x) * (point[1] - center_y) * weight
        for point, weight in alignment_samples
    ) / total_weight
    discriminant = math.sqrt(
        max(0.0, (covariance_xx - covariance_yy) ** 2 + 4.0 * covariance_xy ** 2)
    )
    major_variance = (covariance_xx + covariance_yy + discriminant) * 0.5
    minor_variance = max(0.0, (covariance_xx + covariance_yy - discriminant) * 0.5)
    axis_ratio = math.sqrt(major_variance / max(minor_variance, 1e-12))
    alignment_angle = 0.0
    if axis_ratio >= MAP_ALIGNMENT_MIN_AXIS_RATIO:
        alignment_angle = 0.5 * math.atan2(
            2.0 * covariance_xy,
            covariance_xx - covariance_yy,
        )

    cos_angle = math.cos(alignment_angle)
    sin_angle = math.sin(alignment_angle)

    def aligned(point):
        offset_x = point[0] - center_x
        offset_y = point[1] - center_y
        return (
            offset_x * cos_angle + offset_y * sin_angle,
            -offset_x * sin_angle + offset_y * cos_angle,
        )

    aligned_coordinates = [aligned(point) for point in coordinates]
    min_x = min(point[0] for point in aligned_coordinates)
    max_x = max(point[0] for point in aligned_coordinates)
    min_y = min(point[1] for point in aligned_coordinates)
    max_y = max(point[1] for point in aligned_coordinates)
    span_x = max_x - min_x
    span_y = max_y - min_y
    scale = 1000.0 / max(span_x, span_y, 0.001)
    padding = 50.0
    width = span_x * scale + padding * 2.0
    height = span_y * scale + padding * 2.0

    def svg_point(point):
        aligned_x, aligned_y = aligned(point)
        x = (aligned_x - min_x) * scale + padding
        y = (max_y - aligned_y) * scale + padding
        return f"{x:.3f} {y:.3f}"

    path_elements = []
    for commands in paths:
        parts = []
        for command in commands:
            if command[0] == "Z":
                parts.append("Z")
            else:
                parts.append(f"{command[0]} {' '.join(svg_point(point) for point in command[1:])}")
        path_elements.append(f'  <path d="{" ".join(parts)}"/>')

    svg = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.3f} {height:.3f}" '
        'fill="none" preserveAspectRatio="xMidYMid meet">\n'
        '  <g stroke="#ffffff" stroke-width="10" stroke-linecap="round" stroke-linejoin="round">\n'
        + "\n".join(path_elements)
        + '\n  </g>\n</svg>\n'
    )
    filepath.write_text(svg, encoding="utf-8")


def validate_visual_root(errors, label, visuals):
    if not visuals:
        return

    pbr_roots = [
        child for child in visuals.children
        if child.get(ROLE_PROPERTY) == ROLE_PBR
    ]
    foliage_roots = [
        child for child in visuals.children
        if child.get(ROLE_PROPERTY) == ROLE_FOLIAGE_CARDS
    ]
    if len(pbr_roots) != 1:
        errors.append(f"{label} visuals need exactly one PBR root")
    if len(foliage_roots) != 1:
        errors.append(f"{label} visuals need exactly one FOLIAGE_CARDS root")

    behavior_roots = set(pbr_roots + foliage_roots)
    for child in visuals.children:
        if child not in behavior_roots:
            errors.append(f"{child.name} must be inside the {label} PBR or FOLIAGE_CARDS root")


def validate_collision_root(errors, warnings, label, collision_root):
    if not collision_root:
        errors.append(f"Missing {label} collision root")
        return

    obstacle_roots = [
        child for child in collision_root.children
        if child.get(ROLE_PROPERTY) == ROLE_OBSTACLES
    ]
    obstacles_root = obstacle_roots[0] if obstacle_roots else None
    if len(obstacle_roots) != 1:
        errors.append(f"{label} collisions need exactly one OBSTACLES root")
    if not obstacles_root:
        return

    surface_ids = set()
    for group in collision_root.children:
        role = group.get(ROLE_PROPERTY)
        if role == ROLE_OBSTACLES:
            continue
        if role != ROLE_SURFACE:
            errors.append(f"{group.name} is not a surface group or the OBSTACLES root")
            continue
        surface_id = group.get(SURFACE_PROPERTY)
        if not isinstance(surface_id, str) or not valid_id(surface_id):
            errors.append(f"{group.name} has an invalid surface ID")
        elif surface_id in surface_ids:
            errors.append(f"Duplicate surface group '{surface_id}' in {label}")
        else:
            surface_ids.add(surface_id)

    missing_surfaces = set(SURFACE_IDS) - surface_ids
    unknown_surfaces = surface_ids - set(SURFACE_IDS)
    for surface_id in sorted(missing_surfaces):
        errors.append(f"{label} is missing drivable surface group '{surface_id}'")
    for surface_id in sorted(unknown_surfaces):
        errors.append(f"{label} has unsupported drivable surface group '{surface_id}'")

    collider_count = 0
    for obj in descendants(collision_root):
        is_collider = obj.type == "MESH" or SHAPE_PROPERTY in obj
        if not is_collider:
            continue
        collider_count += 1
        if is_in_tree(obstacles_root, obj):
            validate_collision_object(errors, warnings, obj)
            continue
        if not resolved_surface(collision_root, obj):
            errors.append(f"Collider {obj.name} is not inside a configured surface group")
        validate_collision_object(errors, warnings, obj)
    if collider_count == 0:
        warnings.append(f"{label} has no colliders")


def validate_collision_object(errors, warnings, obj):
    shape = obj.get(SHAPE_PROPERTY, "trimesh" if obj.type == "MESH" else None)
    if shape == "box":
        validate_box_empty(errors, obj, "Box collider")
        return
    if obj.type != "MESH":
        errors.append(f"Collider {obj.name} must be a mesh or a box Empty")
        return
    if shape != "trimesh":
        errors.append(f"Collision mesh {obj.name} has unsupported shape '{shape}'")
        return
    if any(component < 0 for component in obj.scale):
        errors.append(f"Collision mesh {obj.name} has negative scale")
    if any(abs(component - 1.0) > 0.0001 for component in obj.scale):
        warnings.append(f"Collision mesh {obj.name} has unapplied scale")
    if obj.data:
        triangle_count = sum(max(0, len(poly.vertices) - 2) for poly in obj.data.polygons)
        non_zero_axes = sum(abs(component) > 0.0001 for component in obj.dimensions)
        if non_zero_axes < 2:
            errors.append(f"Collision mesh {obj.name} needs non-zero extent on at least two axes")
        if triangle_count == 0:
            errors.append(f"Collision mesh {obj.name} has no triangles")
        if triangle_count > 100000:
            warnings.append(f"Collision mesh {obj.name} has {triangle_count} triangles")


def validate_box_empty(errors, obj, label):
    if obj.type != "EMPTY" or obj.empty_display_type != "CUBE":
        errors.append(f"{label} {obj.name} must be a cube Empty")
        return
    if abs(float(obj.empty_display_size) - 1.0) > 0.0001:
        errors.append(f"{label} {obj.name} must use Empty display size 1")
    world_scale = obj.matrix_world.to_scale()
    if min(abs(component) for component in world_scale) <= 0.0001:
        errors.append(f"{label} {obj.name} has invalid scale")
    validate_box_parent_transform(errors, obj, label)


def validate_box_parent_transform(errors, obj, label):
    current = obj.parent
    while current:
        scale = current.matrix_world.to_scale()
        absolute = [abs(component) for component in scale]
        if max(absolute) - min(absolute) > 0.0001:
            errors.append(f"{label} {obj.name} has a non-uniformly scaled parent: {current.name}")
            return
        basis = current.matrix_world.to_3x3()
        axes = [basis.col[index].normalized() for index in range(3)]
        if any(abs(axes[a].dot(axes[b])) > 0.0001 for a, b in ((0, 1), (0, 2), (1, 2))):
            errors.append(f"{label} {obj.name} has a sheared parent transform: {current.name}")
            return
        current = current.parent


def validate_scene(settings):
    errors = []
    warnings = []
    track_root = settings.track_root_object
    sync_dynamic_collider_metadata(settings)

    if not valid_id(settings.track_id):
        errors.append("Track ID may only contain letters, numbers, underscore, and dash")
    if not PACKAGE_VERSION_PATTERN.fullmatch(settings.package_version):
        errors.append("Package version may only contain letters, numbers, dot, underscore, plus, and dash")
    if not settings.display_name.strip():
        errors.append("Display name is required")
    if not track_root:
        errors.append("Track root object is required")
        return errors, warnings
    if track_root.get(ROLE_PROPERTY) != ROLE_TRACK:
        warnings.append("Track root is not marked with the track role")
    if not settings.shared_root_object or not is_in_tree(track_root, settings.shared_root_object):
        errors.append("Shared root must be inside the track hierarchy")
    if len(settings.layouts) == 0:
        errors.append("At least one layout is required")

    scope_roots = [root for root in [settings.shared_root_object] if root]
    scope_roots.extend(layout.root_object for layout in settings.layouts if layout.root_object)
    collision_roots = [object_with_role(root, ROLE_COLLISIONS) for root in scope_roots]
    linked_colliders = set()
    for index, link in enumerate(settings.dynamic_colliders, start=1):
        collider = link.collider_object
        target = link.target_object
        if not collider:
            errors.append(f"Dynamic collider link {index} is missing its collider")
            continue
        if collider in linked_colliders:
            errors.append(f"Dynamic collider {collider.name} is linked more than once")
        linked_colliders.add(collider)
        if not target:
            errors.append(f"Dynamic collider {collider.name} is missing its target object")
            continue
        if target.type != "MESH":
            errors.append(f"Dynamic collider target {target.name} must be a mesh")
        collider_scope = next((root for root in scope_roots if is_in_tree(root, collider)), None)
        target_scope = next((root for root in scope_roots if is_in_tree(root, target)), None)
        if not collider_scope:
            errors.append(f"Dynamic collider {collider.name} is outside a configured scope")
        if target_scope != collider_scope:
            errors.append(f"Dynamic collider {collider.name} and target {target.name} must share a scope")
        if any(root and is_in_tree(root, target) for root in collision_roots):
            errors.append(f"Dynamic collider target {target.name} must not be inside COLLISIONS")
        if link.mass <= 0:
            errors.append(f"Dynamic collider {collider.name} must have positive mass")

    if track_root:
        for obj in descendants(track_root):
            if obj.get("vectorg_body") == "dynamic" and obj not in linked_colliders:
                errors.append(f"Dynamic collider {obj.name} has no target link")

    layout_ids = set()
    layout_roots = set()
    layout_map_curves = set()
    for index, layout in enumerate(settings.layouts, start=1):
        layout_name = layout.display_name.strip() or layout.layout_id or f"Layout {index}"
        label = f"{layout_name} layout"
        if not valid_id(layout.layout_id):
            errors.append(f"{label} has an invalid ID")
        elif layout.layout_id in layout_ids:
            errors.append(f"Duplicate layout ID '{layout.layout_id}'")
        layout_ids.add(layout.layout_id)
        if not layout.display_name.strip():
            errors.append(f"{label} display name is required")
        if not layout.root_object:
            errors.append(f"{label} root object is required")
            continue
        if layout.root_object in layout_roots:
            errors.append(f"{label} uses a duplicate root object")
        layout_roots.add(layout.root_object)
        if not is_in_tree(track_root, layout.root_object):
            errors.append(f"{label} root must be inside the track hierarchy")

        map_root = direct_child_with_role(layout.root_object, ROLE_MAP)
        if not map_root:
            errors.append(f"{label} is missing its MAP node")
        if not layout.map_curve:
            warnings.append(f"{label} has no map curve")
        else:
            if layout.map_curve in layout_map_curves:
                errors.append(f"{label} shares its map curve with another layout")
            layout_map_curves.add(layout.map_curve)
            splines = list(layout.map_curve.data.splines)
            supported_splines = [spline for spline in splines if spline.type in {"BEZIER", "POLY"}]
            if not splines:
                errors.append(f"{label} map curve has no splines")
            elif len(splines) != 1:
                errors.append(f"{label} map curve must contain exactly one spline")
            elif len(supported_splines) != len(splines):
                errors.append(f"{label} map curve may only contain Bezier or Poly splines")
            elif any(
                len(spline.bezier_points if spline.type == "BEZIER" else spline.points) < 2
                for spline in supported_splines
            ):
                errors.append(f"{label} map curve splines need at least two points")
            if layout.route_type == "circular" and supported_splines and not any(
                spline.use_cyclic_u for spline in supported_splines
            ):
                errors.append(f"{label} map curve needs a cyclic spline for a circular route")
            if layout.route_type == "point_to_point" and supported_splines and any(
                spline.use_cyclic_u for spline in supported_splines
            ):
                errors.append(f"{label} map curve must be open for a point-to-point route")
            if map_root and layout.map_curve.parent != map_root:
                errors.append(f"{label} map curve must be directly under its MAP node")
            if len(splines) == 1 and len(supported_splines) == 1:
                update_layout_length(layout)

        nodes = layout_nodes(layout)
        for node_label, role in (
            ("visuals", ROLE_VISUALS),
            ("collisions", ROLE_COLLISIONS),
            ("obstacles", ROLE_OBSTACLES),
            ("spawnPoints", ROLE_SPAWN_POINTS),
            ("events", ROLE_EVENTS),
        ):
            matches = descendants_with_role(layout.root_object, role)
            if not nodes[node_label]:
                errors.append(f"{label} is missing its {node_label} node")
            elif len(matches) > 1:
                errors.append(f"{label} has multiple {node_label} nodes")
        validate_visual_root(errors, label, nodes["visuals"])
        validate_collision_root(errors, warnings, label, nodes["collisions"])

        spawn_points = descendants_with_role(nodes["spawnPoints"], ROLE_SPAWN_POINT)
        if not spawn_points:
            errors.append(f"{label} needs at least one spawn point")

        event_objects = [obj for obj in descendants(nodes["events"]) if obj.get(EVENT_PROPERTY)] if nodes["events"] else []
        supported_events = {"start_finish", "start", "finish", "checkpoint", "reset_zone", "track_limit"}
        for event in event_objects:
            if event.get(EVENT_PROPERTY) not in supported_events:
                errors.append(f"Event {event.name} has an unsupported event type")
        start_finish = [obj for obj in event_objects if obj.get(EVENT_PROPERTY) == "start_finish"]
        starts = [obj for obj in event_objects if obj.get(EVENT_PROPERTY) == "start"]
        finishes = [obj for obj in event_objects if obj.get(EVENT_PROPERTY) == "finish"]
        checkpoints = [obj for obj in event_objects if obj.get(EVENT_PROPERTY) == "checkpoint"]
        if layout.route_type == "circular":
            if len(start_finish) != 1:
                errors.append(f"{label} needs exactly one start/finish event")
            if starts or finishes:
                errors.append(f"{label} cannot use separate start or finish events for a circular route")
            if not checkpoints:
                errors.append(f"{label} needs at least one checkpoint")
        else:
            if start_finish:
                errors.append(f"{label} cannot use a start/finish event for a point-to-point route")
            if len(starts) != 1:
                errors.append(f"{label} needs exactly one start event")
            if len(finishes) != 1:
                errors.append(f"{label} needs exactly one finish event")
        orders = sorted(int(obj.get(ORDER_PROPERTY, 0)) for obj in checkpoints)
        if orders != list(range(1, len(checkpoints) + 1)):
            errors.append(f"{label} checkpoint orders must be contiguous from 1")
        for event in event_objects:
            validate_box_empty(errors, event, "Event")
        if layout.map_curve:
            try:
                layout_route_data(layout)
            except ValueError as error:
                errors.append(f"{label} route {error}")

    if settings.shared_root_object:
        shared_visuals = object_with_role(settings.shared_root_object, ROLE_VISUALS)
        validate_visual_root(errors, "Shared", shared_visuals)
        shared_collision_root = object_with_role(settings.shared_root_object, ROLE_COLLISIONS)
        validate_collision_root(errors, warnings, "Shared", shared_collision_root)

    if settings.hdr_image:
        extension = image_source_extension(settings.hdr_image)
        if extension not in {".hdr", ".exr"}:
            errors.append("HDR texture must use .hdr or .exr")
        elif not settings.hdr_image.packed_file and not image_source_path(settings.hdr_image).is_file():
            errors.append("HDR texture source file does not exist")

    if track_root:
        map_roots = [
            direct_child_with_role(layout.root_object, ROLE_MAP)
            for layout in settings.layouts
            if layout.root_object
        ]
        excluded_map_objects = {
            obj
            for root in map_roots
            if root
            for obj in [root, *descendants(root)]
        }
        texture_errors, texture_warnings = texture_validation(
            track_export_objects(track_root, excluded_map_objects),
            int(settings.max_texture_size),
            settings.hdr_image,
        )
        errors.extend(texture_errors)
        warnings.extend(texture_warnings)

    return errors, warnings


def show_validation_popup(context, errors, warnings):
    title = "Track Validation Failed" if errors else "Track Validation Passed"
    icon = "ERROR" if errors or warnings else "CHECKMARK"

    def draw_popup(self, _context):
        column = self.layout.column()
        for message in errors:
            column.label(text=message, icon="ERROR")
        for message in warnings:
            column.label(text=message, icon="INFO")
        if not errors and not warnings:
            column.label(text="No errors or warnings", icon="CHECKMARK")

    context.window_manager.popup_menu(draw_popup, title=title, icon=icon)


def reset_settings(settings):
    settings.is_configured = False
    settings.track_id = ""
    settings.package_version = "1"
    settings.display_name = ""
    settings.max_texture_size = str(DEFAULT_MAX_TEXTURE_SIZE)
    settings.optimize_color_textures = True
    settings.jpeg_quality = DEFAULT_JPEG_QUALITY
    settings.track_root_object = None
    settings.shared_root_object = None
    settings.layouts.clear()
    settings.dynamic_colliders.clear()
    settings.active_layout_index = 0
    settings.hdr_image = None


class TrackLayoutSettings(PropertyGroup):
    layout_id: StringProperty(name="ID", description="Export identifier and generated-object name prefix", default="layout", update=update_layout_id)
    visible: BoolProperty(name="Visible", description="Show or hide this complete layout hierarchy", default=True, update=update_layout_visibility)
    display_name: StringProperty(name="Name", description="Player-facing layout name", default="Layout")
    description: StringProperty(name="Description", description="Player-facing layout description", default="")
    discipline: EnumProperty(
        name="Discipline",
        items=(
            ("circuit", "Circuit", "Closed paved-road race circuit"),
            ("oval", "Oval", "Oval circuit"),
            ("rally", "Rally", "Point-to-point rally stage"),
            ("rallycross", "Rallycross", "Mixed-surface rallycross circuit"),
            ("offroad", "Offroad", "Off-road race course"),
            ("drift", "Drift", "Drift course"),
            ("drag", "Drag", "Straight-line drag strip"),
            ("hill_climb", "Hill Climb", "Point-to-point hill-climb course"),
            ("autocross", "Autocross", "Short technical autocross course"),
            ("test", "Test", "Test or development layout"),
        ),
        default="circuit",
    )
    route_type: EnumProperty(
        name="Route Type",
        items=(
            ("circular", "Circular", "Start and finish use one shared event"),
            ("point_to_point", "Point to Point", "Start and finish use separate events"),
        ),
        default="circular",
    )
    length: FloatProperty(name="Length (km)", description="Calculated from the configured map curve", default=0.0, min=0.0)
    track_type_tarmac: BoolProperty(name="Tarmac", description="Classify this layout as tarmac", default=True)
    track_type_offroad: BoolProperty(name="Offroad", description="Classify this layout as off-road", default=False)
    root_object: PointerProperty(name="Root", description="Generated root object for this layout", type=bpy.types.Object)
    map_curve: PointerProperty(
        name="Map Curve",
        description="Curve moved under the layout MAP node and used for its SVG, length, and reset route",
        type=bpy.types.Object,
        poll=curve_object_poll,
        update=update_map_curve,
    )


class DynamicColliderLink(PropertyGroup):
    collider_object: PointerProperty(name="Collider", description="Dynamic collider Empty", type=bpy.types.Object)
    target_object: PointerProperty(name="Target", description="Visual object followed by the dynamic collider", type=bpy.types.Object)
    mass: FloatProperty(name="Mass", description="Dynamic collider mass in kilograms", default=DEFAULT_DYNAMIC_MASS, min=0.001)


class TrackExporterSettings(PropertyGroup):
    is_configured: BoolProperty(name="Configured", default=False)
    track_id: StringProperty(name="Track ID", description="Export identifier for the whole track package", default="my_track")
    package_version: StringProperty(
        name="Package Version",
        description="Explicit asset revision; increment when package contents change",
        default="1",
    )
    display_name: StringProperty(name="Display Name", description="Player-facing track name", default="My Track")
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
    track_root_object: PointerProperty(name="Track Root", description="Root of all track content", type=bpy.types.Object)
    shared_root_object: PointerProperty(name="Shared Root", description="Content shared by every layout", type=bpy.types.Object)
    hdr_image: PointerProperty(name="HDR", description="Loaded HDR or EXR image exported as the track environment", type=bpy.types.Image, poll=hdr_image_poll)
    layouts: CollectionProperty(type=TrackLayoutSettings)
    dynamic_colliders: CollectionProperty(type=DynamicColliderLink)
    active_layout_index: IntProperty(name="Active Layout", default=0)


class TRACK_EXPORTER_UL_layouts(UIList):
    def draw_item(self, _context, layout, _data, item, _icon, _active_data, _active_propname, index):
        row = layout.row(align=True)
        row.label(text=item.display_name or item.layout_id, icon="OUTLINER_COLLECTION")
        row.label(text=item.layout_id)
        row.prop(item, "visible", text="", emboss=False, icon="HIDE_OFF" if item.visible else "HIDE_ON")


class TRACK_EXPORTER_OT_create_configuration(Operator):
    bl_idname = "track_exporter.create_configuration"
    bl_label = "Create Track Structure"
    bl_description = "Create the VectorG track hierarchy and collision surface groups"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = scene_settings(context)
        reset_settings(settings)
        settings.is_configured = True
        settings.track_id = "my_track"
        settings.package_version = "1"
        settings.display_name = "My Track"
        settings.max_texture_size = str(DEFAULT_MAX_TEXTURE_SIZE)
        settings.optimize_color_textures = True
        settings.jpeg_quality = DEFAULT_JPEG_QUALITY

        track_root = create_empty(context, "TRACK_ROOT", role=ROLE_TRACK)
        shared = create_empty(context, "SHARED", track_root, ROLE_SHARED)
        create_visual_hierarchy(context, shared, "SHARED")
        create_collision_hierarchy(context, shared, "SHARED")
        create_empty(context, "LAYOUTS", track_root, ROLE_LAYOUTS)

        settings.track_root_object = track_root
        settings.shared_root_object = shared
        select_only(context, track_root)
        return {"FINISHED"}


class TRACK_EXPORTER_OT_remove_configuration(Operator):
    bl_idname = "track_exporter.remove_configuration"
    bl_label = "Remove Configuration"
    bl_description = "Remove the VectorG exporter configuration from this scene"
    bl_options = {"REGISTER", "UNDO"}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        reset_settings(scene_settings(context))
        return {"FINISHED"}


class TRACK_EXPORTER_OT_add_layout(Operator):
    bl_idname = "track_exporter.add_layout"
    bl_label = "Add Layout"
    bl_description = "Create a new layout hierarchy with visuals, collisions, spawns, and events"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = scene_settings(context)
        if not settings.track_root_object:
            self.report({"ERROR"}, "Create or assign a track root first")
            return {"CANCELLED"}
        layouts_root = direct_child_with_role(settings.track_root_object, ROLE_LAYOUTS)
        if not layouts_root:
            layouts_root = create_empty(context, "LAYOUTS", settings.track_root_object, ROLE_LAYOUTS)
        layout_id = next_layout_id(settings)
        index = int(layout_id.removeprefix("layout_"))
        root, _visuals, _collisions, _spawns, _events, _map = create_layout_hierarchy(
            context, layouts_root, layout_id
        )

        layout = settings.layouts.add()
        layout.layout_id = layout_id
        layout.display_name = f"Layout {index}"
        layout.root_object = root
        sync_layout_node_names(layout)
        update_layout_visibility(layout, context)
        settings.active_layout_index = len(settings.layouts) - 1
        select_only(context, root)
        return {"FINISHED"}


class TRACK_EXPORTER_OT_remove_layout(Operator):
    bl_idname = "track_exporter.remove_layout"
    bl_label = "Remove Layout Entry"
    bl_description = "Remove the selected layout from the exporter configuration"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = scene_settings(context)
        index = settings.active_layout_index
        if not (0 <= index < len(settings.layouts)):
            return {"CANCELLED"}
        settings.layouts.remove(index)
        settings.active_layout_index = min(index, max(0, len(settings.layouts) - 1))
        return {"FINISHED"}


class TRACK_EXPORTER_OT_move_layout(Operator):
    bl_idname = "track_exporter.move_layout"
    bl_label = "Move Layout"
    bl_options = {"REGISTER", "UNDO"}

    direction: EnumProperty(items=(("UP", "Up", ""), ("DOWN", "Down", "")))

    def execute(self, context):
        settings = scene_settings(context)
        index = settings.active_layout_index
        target = index - 1 if self.direction == "UP" else index + 1
        if not (0 <= index < len(settings.layouts) and 0 <= target < len(settings.layouts)):
            return {"CANCELLED"}
        settings.layouts.move(index, target)
        settings.active_layout_index = target
        return {"FINISHED"}


class TRACK_EXPORTER_OT_refresh_layout_names(Operator):
    bl_idname = "track_exporter.refresh_layout_names"
    bl_label = "Refresh Layout Names"
    bl_description = "Apply the current layout ID to its generated object names"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        layout = active_layout(scene_settings(context))
        if not layout or not layout.root_object:
            self.report({"ERROR"}, "Active layout root is missing")
            return {"CANCELLED"}
        if not sync_layout_node_names(layout):
            conflicts = layout.get("vectorg_name_conflicts", "")
            self.report({"ERROR"}, f"Cannot rename objects; names already in use: {conflicts}")
            return {"CANCELLED"}
        self.report({"INFO"}, "Layout object names refreshed")
        return {"FINISHED"}


class TRACK_EXPORTER_OT_add_static_box_collider(Operator):
    bl_idname = "track_exporter.add_static_box_collider"
    bl_label = "Add Static Box Collider"
    bl_description = "Create a static box collider from selected mesh bounds, or at the 3D cursor"
    bl_options = {"REGISTER", "UNDO"}

    scope: EnumProperty(items=(("SHARED", "Shared", "Add under shared obstacles"), ("LAYOUT", "Layout", "Add under active layout obstacles")))

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT"

    def execute(self, context):
        settings = scene_settings(context)
        scope_root = collider_scope_root(settings, self.scope)
        obstacles_root = collider_obstacles_root(settings, self.scope)
        if not scope_root or not obstacles_root:
            self.report({"ERROR"}, "Target OBSTACLES root is missing")
            return {"CANCELLED"}
        targets = selected_meshes(context)
        if any(not is_in_tree(scope_root, target) for target in targets):
            self.report({"ERROR"}, "Selected meshes must be inside the target scope")
            return {"CANCELLED"}
        matrix = None
        if targets:
            matrix = combined_bounds_collider_matrix(context, targets)
            if min(abs(component) for component in matrix.to_scale()) <= 0.0001:
                self.report({"ERROR"}, "Selected meshes have invalid bounds")
                return {"CANCELLED"}
        obj = create_empty(context, "static_box_collider", obstacles_root, display_type="CUBE")
        if matrix is not None:
            obj.matrix_world = matrix
        else:
            set_world_location(obj, context.scene.cursor.location)
        obj[SHAPE_PROPERTY] = "box"
        obj.show_name = True
        if self.scope == "LAYOUT":
            layout = active_layout(settings)
            sync_layout_node_names(layout)
        select_only(context, obj)
        return {"FINISHED"}


class TRACK_EXPORTER_OT_add_dynamic_box_collider(Operator):
    bl_idname = "track_exporter.add_dynamic_box_collider"
    bl_label = "Add Dynamic Box Collider"
    bl_description = "Create a dynamic box collider matching the selected mesh bounds"
    bl_options = {"REGISTER", "UNDO"}

    scope: EnumProperty(items=(("SHARED", "Shared", "Add under shared obstacles"), ("LAYOUT", "Layout", "Add under active layout obstacles")))

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT"

    def execute(self, context):
        settings = scene_settings(context)
        target = selected_mesh(context)
        if not target:
            self.report({"ERROR"}, "Select exactly one mesh object")
            return {"CANCELLED"}
        scope_root = collider_scope_root(settings, self.scope)
        obstacles_root = collider_obstacles_root(settings, self.scope)
        if not scope_root or not obstacles_root:
            self.report({"ERROR"}, "Target scope or OBSTACLES root is missing")
            return {"CANCELLED"}
        if not is_in_tree(scope_root, target):
            self.report({"ERROR"}, "Selected mesh must be inside the target scope")
            return {"CANCELLED"}
        collisions_root = object_with_role(scope_root, ROLE_COLLISIONS)
        if collisions_root and is_in_tree(collisions_root, target):
            self.report({"ERROR"}, "Dynamic target must not be inside COLLISIONS")
            return {"CANCELLED"}

        collider_matrix = bounds_collider_matrix(context, target)
        if min(abs(component) for component in collider_matrix.to_scale()) <= 0.0001:
            self.report({"ERROR"}, "Selected mesh has invalid bounds")
            return {"CANCELLED"}
        mass = box_mass_from_matrix(collider_matrix)
        collider = create_empty(
            context,
            f"{target.name}_dynamic_collider",
            obstacles_root,
            display_type="CUBE",
        )
        collider.matrix_world = collider_matrix
        collider[SHAPE_PROPERTY] = "box"
        collider["vectorg_body"] = "dynamic"
        collider["vectorg_mass"] = mass
        collider["vectorg_target"] = target.name
        collider.show_name = True

        if self.scope == "LAYOUT":
            layout = active_layout(settings)
            sync_layout_node_names(layout)

        link = settings.dynamic_colliders.add()
        link.collider_object = collider
        link.target_object = target
        link.mass = mass
        select_only(context, collider)
        return {"FINISHED"}


class TRACK_EXPORTER_OT_add_spawn_point(Operator):
    bl_idname = "track_exporter.add_spawn_point"
    bl_label = "Add Spawn Point"
    bl_description = "Add a vehicle spawn point at the 3D cursor"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        layout = active_layout(scene_settings(context))
        root = object_with_role(layout.root_object, ROLE_SPAWN_POINTS) if layout else None
        if not root:
            self.report({"ERROR"}, "Active layout spawn root is missing")
            return {"CANCELLED"}
        count = len(descendants_with_role(root, ROLE_SPAWN_POINT)) + 1
        obj = create_empty(context, f"{layout.layout_id}_spawn_{count:02d}", root, ROLE_SPAWN_POINT)
        obj.show_name = True
        set_world_location(obj, context.scene.cursor.location)
        sync_layout_node_names(layout)
        select_only(context, obj)
        return {"FINISHED"}


def add_event_object(context, layout, event_type, name, order=0):
    events_root = object_with_role(layout.root_object, ROLE_EVENTS)
    obj = create_empty(context, name, events_root, display_type="CUBE")
    obj.show_name = True
    obj[EVENT_PROPERTY] = event_type
    obj.scale = DEFAULT_EVENT_SCALE
    set_world_location(obj, context.scene.cursor.location)
    if order:
        obj[ORDER_PROPERTY] = order
    select_only(context, obj)
    return obj


class TRACK_EXPORTER_OT_add_start_finish(Operator):
    bl_idname = "track_exporter.add_start_finish"
    bl_label = "Add Start/Finish"
    bl_description = "Add the circular-route start and finish event at the 3D cursor"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        layout = active_layout(scene_settings(context))
        if not layout or not object_with_role(layout.root_object, ROLE_EVENTS):
            self.report({"ERROR"}, "Active layout event root is missing")
            return {"CANCELLED"}
        if layout.route_type != "circular":
            self.report({"ERROR"}, "Start/finish is only valid for circular routes")
            return {"CANCELLED"}
        existing = [obj for obj in descendants(object_with_role(layout.root_object, ROLE_EVENTS)) if obj.get(EVENT_PROPERTY) == "start_finish"]
        if existing:
            self.report({"ERROR"}, "Active layout already has a start/finish event")
            return {"CANCELLED"}
        add_event_object(context, layout, "start_finish", f"{layout.layout_id}_start_finish")
        sync_layout_node_names(layout)
        return {"FINISHED"}


class TRACK_EXPORTER_OT_add_route_endpoint(Operator):
    bl_idname = "track_exporter.add_route_endpoint"
    bl_label = "Add Route Endpoint"
    bl_description = "Add a point-to-point start or finish event at the 3D cursor"
    bl_options = {"REGISTER", "UNDO"}

    event_type: EnumProperty(items=(("start", "Start", "Point-to-point route start"), ("finish", "Finish", "Point-to-point route finish")))

    def execute(self, context):
        layout = active_layout(scene_settings(context))
        if not layout or not object_with_role(layout.root_object, ROLE_EVENTS):
            self.report({"ERROR"}, "Active layout event root is missing")
            return {"CANCELLED"}
        if layout.route_type != "point_to_point":
            self.report({"ERROR"}, "Start and finish are only valid for point-to-point routes")
            return {"CANCELLED"}
        events_root = object_with_role(layout.root_object, ROLE_EVENTS)
        existing = [obj for obj in descendants(events_root) if obj.get(EVENT_PROPERTY) == self.event_type]
        if existing:
            self.report({"ERROR"}, f"Active layout already has a {self.event_type} event")
            return {"CANCELLED"}
        add_event_object(context, layout, self.event_type, f"{layout.layout_id}_{self.event_type}")
        sync_layout_node_names(layout)
        return {"FINISHED"}


class TRACK_EXPORTER_OT_add_checkpoint(Operator):
    bl_idname = "track_exporter.add_checkpoint"
    bl_label = "Add Checkpoint"
    bl_description = "Add the next ordered checkpoint at the 3D cursor"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = scene_settings(context)
        layout = active_layout(settings)
        events_root = object_with_role(layout.root_object, ROLE_EVENTS) if layout else None
        if not events_root:
            self.report({"ERROR"}, "Active layout event root is missing")
            return {"CANCELLED"}
        orders = [int(obj.get(ORDER_PROPERTY, 0)) for obj in descendants(events_root) if obj.get(EVENT_PROPERTY) == "checkpoint"]
        order = max(orders, default=0) + 1
        checkpoint = add_event_object(
            context,
            layout,
            "checkpoint",
            f"{layout.layout_id}_checkpoint_{order:02d}",
            order,
        )
        sync_layout_node_names(layout)
        return {"FINISHED"}


class TRACK_EXPORTER_OT_validate_track(Operator):
    bl_idname = "track_exporter.validate_track"
    bl_label = "Validate Track"
    bl_description = "Check the track hierarchy and export requirements"

    def execute(self, context):
        errors, warnings = validate_scene(scene_settings(context))
        show_validation_popup(context, errors, warnings)
        return {"CANCELLED"} if errors else {"FINISHED"}


def export_track_glb(
    context,
    track_root,
    filepath,
    max_texture_size,
    optimize_color_textures,
    jpeg_quality,
    hdr_image=None,
    excluded_objects=(),
):
    selected_before = list(context.selected_objects)
    active_before = context.view_layer.objects.active
    export_objects = track_export_objects(track_root, excluded_objects)
    visibility_before = [
        (obj, obj.hide_get(), obj.hide_render)
        for obj in export_objects
    ]
    restored_nodes, temp_images = apply_export_texture_optimization(
        export_objects,
        max_texture_size,
        optimize_color_textures,
        Path(filepath).parent,
        jpeg_quality,
    )
    try:
        for obj, _hidden, _hide_render in visibility_before:
            obj.hide_set(False)
            obj.hide_render = False
        bpy.ops.object.select_all(action="DESELECT")
        for obj in export_objects:
            obj.select_set(True)
        context.view_layer.objects.active = track_root
        result = bpy.ops.export_scene.gltf(
            filepath=str(filepath),
            export_format="GLB",
            use_selection=True,
            export_apply=True,
            export_extras=True,
            export_cameras=False,
            **gltf_image_export_options(jpeg_quality),
            **gltf_mesh_instance_export_options(),
        )
        if "FINISHED" not in result:
            raise RuntimeError("Blender glTF export did not finish")
    finally:
        restore_export_textures(restored_nodes, temp_images)
        bpy.ops.object.select_all(action="DESELECT")
        for obj, hidden, hide_render in visibility_before:
            obj.hide_set(hidden)
            obj.hide_render = hide_render
        for obj in selected_before:
            if obj.name in bpy.data.objects:
                obj.select_set(True)
        if active_before and active_before.name in bpy.data.objects:
            context.view_layer.objects.active = active_before
    validate_hdr_not_embedded(filepath, hdr_image)


class TRACK_EXPORTER_OT_export_track_zip(Operator, ExportHelper):
    bl_idname = "track_exporter.export_track_zip"
    bl_label = "Export Track Zip"
    bl_description = "Export <track_id>.glb, manifest.json, and the optional HDR into a track zip"
    bl_options = {"REGISTER"}
    filename_ext = ".zip"

    filepath: StringProperty(name="Export Zip", description="Destination for the exported track package", subtype="FILE_PATH")
    filter_glob: StringProperty(default="*.zip", options={"HIDDEN"})

    def execute(self, context):
        settings = scene_settings(context)
        errors, warnings = validate_scene(settings)
        for message in warnings:
            self.report({"WARNING"}, message)
        if errors:
            for message in errors:
                self.report({"ERROR"}, message)
            return {"CANCELLED"}

        export_zip = Path(abspath(self.filepath))
        if export_zip.suffix.lower() != ".zip":
            export_zip = export_zip.with_suffix(".zip")
        export_zip.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="track_exporter_") as temp_dir:
            temp_path = Path(temp_dir)
            model_filename = f"{settings.track_id}.glb"
            map_roots = [
                direct_child_with_role(layout.root_object, ROLE_MAP)
                for layout in settings.layouts
                if layout.root_object
            ]
            map_roots = [root for root in map_roots if root]
            excluded_map_objects = {
                obj for root in map_roots for obj in [root, *descendants(root)]
            }
            export_track_glb(
                context,
                settings.track_root_object,
                temp_path / model_filename,
                int(settings.max_texture_size),
                settings.optimize_color_textures,
                settings.jpeg_quality,
                settings.hdr_image,
                excluded_objects=excluded_map_objects,
            )
            manifest = build_manifest(settings)
            (temp_path / "manifest.json").write_text(json.dumps(manifest, indent=4), encoding="utf-8")

            map_curves = [layout.map_curve for layout in settings.layouts if layout.map_curve]
            if map_curves:
                maps_path = temp_path / "maps"
                maps_path.mkdir()
                routes_path = temp_path / "routes"
                routes_path.mkdir()
                for layout in settings.layouts:
                    if layout.map_curve:
                        write_layout_map_svg(layout, maps_path / f"{layout.layout_id}.svg")
                        route_data = layout_route_data(layout)
                        (routes_path / f"{layout.layout_id}.json").write_text(
                            json.dumps(route_data, separators=(",", ":")),
                            encoding="utf-8",
                        )

            if settings.hdr_image:
                source = image_source_path(settings.hdr_image)
                extension = image_source_extension(settings.hdr_image)
                hdr_path = temp_path / "hdr"
                hdr_path.mkdir()
                destination = hdr_path / f"env{extension}"
                if settings.hdr_image.packed_file:
                    destination.write_bytes(bytes(settings.hdr_image.packed_file.data))
                else:
                    shutil.copy2(source, destination)

            with zipfile.ZipFile(export_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in temp_path.rglob("*"):
                    if path.is_file():
                        archive.write(path, path.relative_to(temp_path).as_posix())

        self.report({"INFO"}, f"Exported {export_zip}")
        return {"FINISHED"}

    def invoke(self, context, event):
        settings = scene_settings(context)
        if not self.filepath:
            self.filepath = f"//{settings.track_id or 'track'}.zip"
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


def draw_split_prop(layout, data, prop_name, label=None, **kwargs):
    split = layout.split(factor=0.4, align=True)
    split.label(text=label or data.bl_rna.properties[prop_name].name)
    split.prop(data, prop_name, text="", **kwargs)


class TRACK_EXPORTER_PT_track_export(Panel):
    bl_label = "VectorG Track Exporter"
    bl_idname = "TRACK_EXPORTER_PT_track_export"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "VectorG"

    def draw(self, context):
        layout = self.layout
        settings = scene_settings(context)
        if not settings.is_configured:
            layout.operator("track_exporter.create_configuration", icon="ADD")
            return

        box = layout.box()
        box.label(text="Package")
        draw_split_prop(box, settings, "track_id")
        draw_split_prop(box, settings, "package_version")
        draw_split_prop(box, settings, "display_name")
        draw_split_prop(box, settings, "max_texture_size")
        draw_split_prop(box, settings, "optimize_color_textures")
        color_quality = box.row()
        color_quality.enabled = settings.optimize_color_textures
        draw_split_prop(color_quality, settings, "jpeg_quality")
        draw_split_prop(box, settings, "track_root_object")
        draw_split_prop(box, settings, "shared_root_object")
        draw_split_prop(box, settings, "hdr_image")

        box = layout.box()
        box.label(text="Shared")
        static_op = box.operator(
            "track_exporter.add_static_box_collider",
            text="Create Static Box Collider",
            icon="MESH_CUBE",
        )
        static_op.scope = "SHARED"
        dynamic_row = box.row()
        dynamic_row.enabled = selected_mesh(context) is not None
        dynamic_op = dynamic_row.operator(
            "track_exporter.add_dynamic_box_collider",
            text="Create Dynamic Box Collider",
            icon="MESH_CUBE",
        )
        dynamic_op.scope = "SHARED"

        box = layout.box()
        row = box.row(align=True)
        row.label(text="Layouts")
        row.operator("track_exporter.add_layout", text="", icon="ADD")
        row.operator("track_exporter.remove_layout", text="", icon="REMOVE")
        move_up = row.operator("track_exporter.move_layout", text="", icon="TRIA_UP")
        move_up.direction = "UP"
        move_down = row.operator("track_exporter.move_layout", text="", icon="TRIA_DOWN")
        move_down.direction = "DOWN"
        box.template_list(
            "TRACK_EXPORTER_UL_layouts", "", settings, "layouts", settings, "active_layout_index", rows=3
        )
        current = active_layout(settings)
        if current:
            id_row = box.row(align=True)
            id_split = id_row.split(factor=0.4, align=True)
            id_split.label(text="ID")
            id_value = id_split.row(align=True)
            id_value.prop(current, "layout_id", text="")
            id_value.operator("track_exporter.refresh_layout_names", text="", icon="FILE_REFRESH")
            draw_split_prop(box, current, "display_name")
            draw_split_prop(box, current, "description")
            draw_split_prop(box, current, "discipline")
            draw_split_prop(box, current, "route_type")
            length_row = box.row()
            length_row.enabled = False
            draw_split_prop(length_row, current, "length")
            draw_split_prop(box, current, "root_object")
            draw_split_prop(box, current, "map_curve")
            tags = box.row(align=True)
            tags.label(text="Track Types")
            tags.prop(current, "track_type_tarmac", text="Tarmac", toggle=True)
            tags.prop(current, "track_type_offroad", text="Offroad", toggle=True)
            box.separator()
            box.label(text="Layout Objects")
            box.operator("track_exporter.add_spawn_point", icon="EMPTY_AXIS")
            if current.route_type == "circular":
                box.operator("track_exporter.add_start_finish", icon="MESH_CUBE")
            else:
                start_op = box.operator("track_exporter.add_route_endpoint", text="Add Start", icon="MESH_CUBE")
                start_op.event_type = "start"
                finish_op = box.operator("track_exporter.add_route_endpoint", text="Add Finish", icon="MESH_CUBE")
                finish_op.event_type = "finish"
            box.operator("track_exporter.add_checkpoint", icon="MESH_CUBE")
            box.separator()
            box.label(text="Layout Colliders")
            static_op = box.operator(
                "track_exporter.add_static_box_collider",
                text="Create Static Box Collider",
                icon="MESH_CUBE",
            )
            static_op.scope = "LAYOUT"
            dynamic_row = box.row()
            dynamic_row.enabled = selected_mesh(context) is not None
            dynamic_op = dynamic_row.operator(
                "track_exporter.add_dynamic_box_collider",
                text="Create Dynamic Box Collider",
                icon="MESH_CUBE",
            )
            dynamic_op.scope = "LAYOUT"

        box = layout.box()
        box.operator("track_exporter.remove_configuration", icon="TRASH")

        box = layout.box()
        row = box.row(align=True)
        row.operator("track_exporter.validate_track", icon="CHECKMARK")
        row.operator("track_exporter.export_track_zip", icon="EXPORT")


classes = (
    TrackLayoutSettings,
    DynamicColliderLink,
    TrackExporterSettings,
    TRACK_EXPORTER_UL_layouts,
    TRACK_EXPORTER_OT_create_configuration,
    TRACK_EXPORTER_OT_remove_configuration,
    TRACK_EXPORTER_OT_add_layout,
    TRACK_EXPORTER_OT_remove_layout,
    TRACK_EXPORTER_OT_move_layout,
    TRACK_EXPORTER_OT_refresh_layout_names,
    TRACK_EXPORTER_OT_add_static_box_collider,
    TRACK_EXPORTER_OT_add_dynamic_box_collider,
    TRACK_EXPORTER_OT_add_spawn_point,
    TRACK_EXPORTER_OT_add_start_finish,
    TRACK_EXPORTER_OT_add_route_endpoint,
    TRACK_EXPORTER_OT_add_checkpoint,
    TRACK_EXPORTER_OT_validate_track,
    TRACK_EXPORTER_OT_export_track_zip,
    TRACK_EXPORTER_PT_track_export,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.track_exporter = PointerProperty(type=TrackExporterSettings)
    if update_curve_lengths not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(update_curve_lengths)


def unregister():
    if update_curve_lengths in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(update_curve_lengths)
    del bpy.types.Scene.track_exporter
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
