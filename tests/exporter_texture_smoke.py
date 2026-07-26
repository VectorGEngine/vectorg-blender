import importlib.util
import tempfile
from pathlib import Path

import bpy


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_material(name, image, usage):
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    principled = next(node for node in nodes if node.bl_idname == "ShaderNodeBsdfPrincipled")
    texture = nodes.new("ShaderNodeTexImage")
    texture.image = image
    links.new(texture.outputs["Color"], principled.inputs["Base Color"])
    if usage == "alpha":
        links.new(texture.outputs["Alpha"], principled.inputs["Alpha"])
    elif usage == "data":
        links.remove(principled.inputs["Base Color"].links[0])
        normal = nodes.new("ShaderNodeNormalMap")
        links.new(texture.outputs["Color"], normal.inputs["Color"])
        links.new(normal.outputs["Normal"], principled.inputs["Normal"])
    return material, texture


def make_cube(name, parent, material, x):
    bpy.ops.mesh.primitive_cube_add(location=(x, 0, 0))
    cube = bpy.context.object
    cube.name = name
    cube.parent = parent
    cube.data.materials.append(material)


track = load_module(
    "vectorg_track_exporter_smoke",
    "addons/vectorg_track_exporter/__init__.py",
)
car = load_module(
    "vectorg_car_exporter_smoke",
    "addons/vectorg_car_exporter/__init__.py",
)

track.register()
track.unregister()
car.register()
car.unregister()

bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)

root = bpy.data.objects.new("TRACK_ROOT", None)
bpy.context.scene.collection.objects.link(root)

color_image = bpy.data.images.new("opaque_color", width=8, height=4, alpha=True)
alpha_image = bpy.data.images.new("alpha_color", width=8, height=4, alpha=True)
normal_image = bpy.data.images.new("normal_data", width=8, height=4, alpha=False)
normal_image.colorspace_settings.is_data = True
hdr_image = bpy.data.images.new("environment.hdr", width=4, height=2, alpha=False)
hdr_image.filepath_raw = "/tmp/environment.hdr"

color_material, color_node = make_material("opaque_material", color_image, "color")
alpha_material, alpha_node = make_material("alpha_material", alpha_image, "alpha")
normal_material, normal_node = make_material("normal_material", normal_image, "data")

make_cube("opaque_cube", root, color_material, -3)
make_cube("alpha_cube", root, alpha_material, 0)
make_cube("normal_cube", root, normal_material, 3)

export_objects = [root, *track.descendants(root)]
usage = track.classify_texture_usage(export_objects)
assert usage[color_image] == {"color"}
assert usage[alpha_image] == {"color", "alpha"}
assert usage[normal_image] == {"data"}

with tempfile.TemporaryDirectory(prefix="vectorg_exporter_smoke_") as directory:
    directory = Path(directory)
    restored, temporary_images = track.apply_export_texture_optimization(
        export_objects,
        4,
        True,
        directory,
        85,
    )
    assert all(max(image.size) <= 4 for image in temporary_images)
    assert color_node.image.file_format == "JPEG"
    assert alpha_node.image.file_format != "JPEG"
    assert normal_node.image.file_format != "JPEG"
    track.restore_export_textures(restored, temporary_images)
    assert not list(directory.glob("texture_*.jpg"))

    track_path = directory / "track.glb"
    track.export_track_glb(
        bpy.context,
        root,
        track_path,
        4,
        True,
        85,
        hdr_image,
    )
    track_json = track.glb_json(track_path)
    track_mime_types = sorted(image["mimeType"] for image in track_json["images"])
    assert track_mime_types == ["image/jpeg", "image/png", "image/png"]
    assert all(image.get("name") != hdr_image.name for image in track_json["images"])
    assert not list(directory.glob("texture_*.jpg"))

    car_path = directory / "car.glb"
    car.export_car_glb(bpy.context, car_path, 4, True, 85)
    car_json = track.glb_json(car_path)
    car_mime_types = sorted(image["mimeType"] for image in car_json["images"])
    assert car_mime_types == ["image/jpeg", "image/png", "image/png"]
    assert not list(directory.glob("texture_*.jpg"))

assert color_node.image == color_image
assert alpha_node.image == alpha_image
assert normal_node.image == normal_image
assert not any("_vectorg_export_" in image.name for image in bpy.data.images)

print("EXPORTER_TEXTURE_SMOKE_OK")
