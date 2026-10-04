"""Lightmap atlas grouping tests, plus Blender bake/export tests with --blender."""
import ast
import math
import sys
import unittest
from pathlib import Path


ADDON = Path(__file__).resolve().parents[1] / "addons/vectorg_track_exporter/__init__.py"


def addon_functions(*names):
    tree = ast.parse(ADDON.read_text(encoding="utf-8"))
    selected = [
        node for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name in names)
        or (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id.startswith("LIGHTMAP_"))
    ]
    namespace = {"math": math}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(ADDON), "exec"), namespace)
    return namespace


class LightmapAtlasGroupTests(unittest.TestCase):
    def setUp(self):
        self.groups = addon_functions(
            "lightmap_atlas_groups", "lightmap_atlas_capacity", "lightmap_atlas_size_for",
        )["lightmap_atlas_groups"]

    def test_small_track_uses_one_atlas_sized_to_the_texel_size(self):
        # A 1024 px atlas at 0.25 m holds 0.3 * 256² = 19661 m².
        items = [("road", 15000.0, (0, 0, 0)), ("wall", 3000.0, (50, 0, 0))]
        self.assertEqual(self.groups(items, 0.25, 4096), [(["road", "wall"], 1024)])

    def test_atlases_fill_in_order_up_to_capacity(self):
        # A 4096 px atlas at 0.25 m holds 0.3 * 1024² = 314573 m²: two 150000 m² tiles share one.
        items = [(f"tile_{x}", 150000.0, (x * 1000.0, 0.0, 0.0)) for x in (2, 0, 1)]
        self.assertEqual(self.groups(items, 0.25, 4096), [(["tile_0", "tile_1"], 4096), (["tile_2"], 4096)])
        self.assertEqual(self.groups(items, 0.5, 4096), [(["tile_0", "tile_1", "tile_2"], 4096)])

    def test_orders_along_the_wider_horizontal_extent(self):
        items = [(f"tile_{y}", 100000.0, (0.0, y * 1000.0, 0.0)) for y in (4, 1, 5, 0, 3, 2)]
        self.assertEqual(self.groups(items, 0.25, 4096), [
            (["tile_0", "tile_1", "tile_2"], 4096), (["tile_3", "tile_4", "tile_5"], 4096),
        ])

    def test_objects_larger_than_an_atlas_get_their_own(self):
        items = [("terrain", 12000000.0, (0, 0, 0)), ("banner", 1000.0, (5, 0, 0)), ("sign", 500.0, (9, 0, 0))]
        self.assertEqual(self.groups(items, 0.25, 4096), [(["banner", "sign"], 512), (["terrain"], 4096)])

    def test_zero_area_items_are_ignored(self):
        self.assertEqual(self.groups([("empty", 0.0, (0, 0, 0))], 0.25, 4096), [])


def blender_tests():
    """Run with blender --background --factory-startup --python this_file -- --blender."""
    import bpy
    import json
    import struct
    import tempfile
    import zipfile
    import numpy as np
    from mathutils import Euler, Vector, interpolate

    sys.path.insert(0, str(ADDON.parent.parent))
    import vectorg_track_exporter as addon
    addon.register()
    # Factory startup has no compute device; use a GPU for this process when one exists.
    cycles = bpy.context.preferences.addons["cycles"].preferences
    for device_type in ("OPTIX", "CUDA", "HIP", "METAL", "ONEAPI"):
        try:
            cycles.compute_device_type = device_type
        except TypeError:
            continue
        cycles.refresh_devices()
        gpus = [device for device in cycles.devices if device.type == device_type]
        if gpus:
            for device in cycles.devices:
                device.use = device.type == device_type
            break
    else:
        cycles.compute_device_type = "NONE"

    def glb_json(data):
        chunk_length = struct.unpack_from("<I", data, 12)[0]
        return json.loads(data[20:20 + chunk_length])

    class BlenderLightmapTests(unittest.TestCase):
        def setUp(self):
            bpy.ops.object.select_all(action="SELECT")
            bpy.ops.object.delete(use_global=False)
            for image in list(bpy.data.images):
                bpy.data.images.remove(image)
            bpy.ops.track_exporter.create_configuration()
            bpy.ops.track_exporter.add_layout()
            self.settings = bpy.context.scene.track_exporter
            self.layout = self.settings.layouts[0]
            self.layout.route_type = "freeform"
            self.layout.vehicle_classes = {"A"}
            bpy.ops.track_exporter.add_spawn_point()
            self.settings.lightmap_atlas_size = "2048"
            self.settings.lightmap_samples = 16
            self.settings.lightmap_texel_size = 0.25
            shared_visuals = addon.object_with_role(self.settings.shared_root_object, addon.ROLE_VISUALS)
            self.pbr = addon.direct_child_with_role(shared_visuals, addon.ROLE_PBR)
            bpy.ops.mesh.primitive_grid_add(x_subdivisions=41, y_subdivisions=41, size=20)
            self.ground = bpy.context.object
            self.ground.name = "ground"
            self.ground.parent = self.pbr
            bpy.ops.mesh.primitive_cube_add(size=2, location=(0, 0, 3))
            self.cube = bpy.context.object
            self.cube.name = "cube"
            self.cube.parent = self.pbr
            bpy.ops.object.select_all(action="DESELECT")
            bpy.ops.track_exporter.add_preview_camera()
            bpy.ops.track_exporter.setup_preview_scene()
            self.sun = addon.preview_sun(bpy.context)
            # Straight down, so the cube's shadow falls around the ground origin.
            self.sun.rotation_euler = Euler((0.0, 0.0, 0.0))
            bpy.context.view_layer.update()

        def bake(self):
            self.assertEqual(bpy.ops.track_exporter.bake_lightmaps(), {"FINISHED"})

        def sample(self, obj, point):
            """Lightmap texel (ambient occlusion, sun) at the vertex nearest a local point."""
            mesh = obj.data
            index = min(range(len(mesh.vertices)), key=lambda i: (mesh.vertices[i].co - Vector(point)).length)
            loop = next(loop for loop in mesh.loops if loop.vertex_index == index)
            u, v = mesh.uv_layers[addon.LIGHTMAP_UV_NAME].uv[loop.index].vector
            image = addon.baked_lightmap_images()[obj[addon.LIGHTMAP_PROPERTY]]
            size = image.size[0]
            pixels = np.empty(len(image.pixels), dtype=np.float32)
            image.pixels.foreach_get(pixels)
            pixel = pixels.reshape(size, size, 4)[min(size - 1, int(v * size)), min(size - 1, int(u * size))]
            return float(pixel[0]), float(pixel[1])

        def test_bake_writes_shadow_and_occlusion_into_last_uv_slot(self):
            self.cube.data.uv_layers.new(name="Detail")
            self.cube.data.uv_layers.active = self.cube.data.uv_layers["Detail"]
            world, energy = bpy.context.scene.world, self.sun.data.energy
            self.bake()
            self.assertEqual(bpy.context.scene.world, world)
            self.assertEqual(self.sun.data.energy, energy)
            self.assertTrue(self.sun.data.use_shadow)
            self.assertIsNone(bpy.data.worlds.get("vectorg_lightmap_bake"))
            self.assertFalse(any(obj.name.startswith("vectorg_lightmap_bake") for obj in bpy.data.objects))
            self.assertFalse(any(mesh.name.startswith("vectorg_lightmap_bake") for mesh in bpy.data.meshes))
            self.assertFalse(self.ground.hide_render or self.cube.hide_render)
            for obj, slot in ((self.ground, 1), (self.cube, 2)):
                self.assertEqual(obj[addon.LIGHTMAP_PROPERTY], "lightmaps/shared_0.png")
                self.assertEqual(obj.data.uv_layers.find(addon.LIGHTMAP_UV_NAME), slot)
                self.assertTrue(obj.data.uv_layers[0].active_render)
            self.assertEqual(self.cube.data.uv_layers.active.name, "Detail")
            _ao, shadowed = self.sample(self.ground, (0, 0, 0))
            _ao, lit = self.sample(self.ground, (9, 9, 0))
            self.assertLess(shadowed, 0.2)
            self.assertGreater(lit, 0.9)
            # Space between charts is filled from nearby texels, never left at 0 (full shadow and occlusion).
            atlas = addon.baked_lightmap_images()["lightmaps/shared_0.png"]
            pixels = np.empty(len(atlas.pixels), dtype=np.float32)
            atlas.pixels.foreach_get(pixels)
            pixels = pixels.reshape(-1, 4)
            self.assertLess(float(((pixels[:, 0] < 0.01) & (pixels[:, 1] < 0.01)).mean()), 0.01)
            self.assertEqual(
                list(self.settings.lightmap_sun_direction),
                addon.game_vector(addon.sun_toward_direction(self.sun)),
            )
            self.assertEqual(bpy.context.scene.render.engine, addon.preview_eevee_engine())
            self.assertFalse(any(material.name.startswith("vectorg_lightmap") for material in bpy.data.materials))
            self.assertFalse(any(image.name.startswith("vectorg_lightmap_bake") for image in bpy.data.images))

        def test_shadow_off_receivers_cast_nothing_but_take_ambient_occlusion(self):
            # Ground with Shadow off, as tracks set it: it still takes the cube's occlusion and shadow.
            self.ground.visible_shadow = False
            self.bake()
            ambient_occlusion, sun = self.sample(self.ground, (0, 0, 0))
            self.assertLess(ambient_occlusion, 0.95)
            self.assertLess(sun, 0.2)
            self.assertGreater(self.sample(self.ground, (9, 9, 0))[0], 0.95)
            self.assertFalse(self.ground.visible_shadow)
            self.ground.visible_shadow = True
            # A mesh 1 cm above the ground, away from the cube, darkens the ground under it only while it casts.
            bpy.ops.mesh.primitive_plane_add(size=2, location=(6, 6, 0.01))
            mark = bpy.context.object
            mark.name = "skid_mark"
            mark.parent = self.pbr
            self.bake()
            ambient_occlusion, sun = self.sample(self.ground, (6, 6, 0))
            self.assertLess(ambient_occlusion, 0.5)
            self.assertLess(sun, 0.2)
            mark.visible_shadow = False
            self.bake()
            ambient_occlusion, sun = self.sample(self.ground, (6, 6, 0))
            self.assertGreater(ambient_occlusion, 0.95)
            self.assertGreater(sun, 0.9)
            self.assertEqual(mark[addon.LIGHTMAP_PROPERTY], self.ground[addon.LIGHTMAP_PROPERTY])
            self.assertGreater(self.sample(mark, (0, 0, 0))[1], 0.9)
            self.assertFalse(mark.visible_shadow)
            self.assertFalse(any(obj.name.startswith("vectorg_lightmap_bake") for obj in bpy.data.objects))

        def decal(self, name, location, size=2):
            """A subdivided plane under the shared DECALS root."""
            bpy.ops.mesh.primitive_grid_add(x_subdivisions=5, y_subdivisions=5, size=size, location=location)
            obj = bpy.context.object
            obj.name = name
            shared_visuals = addon.object_with_role(self.settings.shared_root_object, addon.ROLE_VISUALS)
            obj.parent = addon.direct_child_with_role(shared_visuals, addon.ROLE_DECALS)
            return obj

        def lightmap_value(self, obj, uv, atlases):
            """Bilinear (ambient occlusion, sun) of the object's atlas at a lightmap UV."""
            file_path = obj[addon.LIGHTMAP_PROPERTY]
            if file_path not in atlases:
                image = addon.baked_lightmap_images()[file_path]
                pixels = np.empty(len(image.pixels), dtype=np.float32)
                image.pixels.foreach_get(pixels)
                atlases[file_path] = pixels.reshape(image.size[1], image.size[0], 4)
            pixels = atlases[file_path]
            return [float(addon.sample_lightmap_channel(pixels[..., channel], np.array([tuple(uv)]))[0]) for channel in (0, 1)]

        def surface_uv(self, obj, world_point):
            """Lightmap UV of a receiver at its surface point nearest a world point."""
            _found, location, _normal, face = obj.closest_point_on_mesh(obj.matrix_world.inverted() @ world_point)
            polygon = obj.data.polygons[face]
            weights = interpolate.poly_3d_calc([obj.data.vertices[i].co for i in polygon.vertices], location)
            layer = obj.data.uv_layers[addon.LIGHTMAP_UV_NAME].uv
            return sum((layer[index].vector * weight for index, weight in zip(polygon.loop_indices, weights)), Vector((0.0, 0.0)))

        def decal_differences(self, decal, surface, points):
            """Per (world point, decal lightmap UV): the largest channel difference from the surface behind it."""
            atlases = {}
            return [
                max(abs(own - behind) for own, behind in zip(
                    self.lightmap_value(decal, uv, atlases),
                    self.lightmap_value(surface, self.surface_uv(surface, point), atlases),
                ))
                for point, uv in points
            ]

        def face_centres(self, decal):
            layer = decal.data.uv_layers[addon.LIGHTMAP_UV_NAME].uv
            return [
                (decal.matrix_world @ polygon.center,
                 sum((layer[index].vector for index in polygon.loop_indices), Vector((0.0, 0.0))) / polygon.loop_total)
                for polygon in decal.data.polygons
            ]

        def test_decals_copy_the_lightmap_of_the_surface_behind_them_and_cast_nothing(self):
            shaded = self.decal("shaded_decal", (0, 0, 0.01))
            lit = self.decal("lit_decal", (6, 6, 0.01))
            # A window on the cube's +X side faces away from the ground: it copies the wall behind it.
            bpy.ops.mesh.primitive_grid_add(
                x_subdivisions=3, y_subdivisions=3, size=0.8, location=(1.01, 0, 3), rotation=(0, math.pi / 2, 0),
            )
            window = bpy.context.object
            window.name = "window_decal"
            window.parent = shaded.parent
            unwraps = []
            original = addon.unwrap_lightmap_atlas

            def counted(context, state, objects, areas, atlas_size, texel_size):
                unwraps.append(sorted(obj.name for obj in objects))
                return original(context, state, objects, areas, atlas_size, texel_size)

            addon.unwrap_lightmap_atlas = counted
            try:
                self.bake()
                self.assertEqual(
                    [names for names in unwraps if "shaded_decal" in names][-1:],
                    [["lit_decal", "shaded_decal", "window_decal"]],
                )
                decal_unwraps = len([names for names in unwraps if "shaded_decal" in names])
                # A rebake reuses the decals' unwrap like the receivers'.
                self.bake()
                self.assertEqual(len([names for names in unwraps if "shaded_decal" in names]), decal_unwraps)
            finally:
                addon.unwrap_lightmap_atlas = original
            for decal, surface in ((shaded, self.ground), (lit, self.ground), (window, self.cube)):
                self.assertEqual(decal[addon.LIGHTMAP_PROPERTY], "lightmaps/shared_decals_0.png")
                layers = decal.data.uv_layers
                self.assertEqual(layers.find(addon.LIGHTMAP_UV_NAME), len(layers) - 1)
                self.assertLess(max(self.decal_differences(decal, surface, self.face_centres(decal))), 0.1)
            atlases = {}
            self.assertLess(self.lightmap_value(shaded, self.face_centres(shaded)[12][1], atlases)[1], 0.2)
            self.assertGreater(self.lightmap_value(lit, self.face_centres(lit)[12][1], atlases)[1], 0.9)
            # The decal 1 cm above the ground casts neither shadow nor occlusion, though its Shadow is on.
            self.assertTrue(lit.visible_shadow)
            ambient_occlusion, sun = self.sample(self.ground, (6, 6, 0))
            self.assertGreater(ambient_occlusion, 0.95)
            self.assertGreater(sun, 0.9)
            self.assertEqual(addon.validate_scene(self.settings, bpy.context)[0], [])
            decals = addon.object_with_role(self.settings.shared_root_object, addon.ROLE_DECALS)
            tagged = addon.apply_shadow_casting_export_flags([decals, shaded, lit, self.cube])
            try:
                self.assertEqual(set(tagged), {shaded, lit})
            finally:
                addon.restore_shadow_casting_export_flags(tagged)
            with tempfile.TemporaryDirectory() as directory:
                output = str(Path(directory) / "track.zip")
                self.assertEqual(bpy.ops.track_exporter.export_track_zip(filepath=output), {"FINISHED"})
                with zipfile.ZipFile(output) as archive:
                    manifest = json.loads(archive.read("manifest.json"))
                    self.assertIn("lightmaps/shared_decals_0.png", manifest["lightmaps"])
                    self.assertTrue(archive.read("lightmaps/shared_decals_0.png").startswith(b"\x89PNG"))

        def test_a_long_decal_face_across_the_ground_charts_copies_the_ground_all_along(self):
            # At 0.05 m texels the ground's charts are cut every 12.8 m, at x = 0 among others. One 18 m
            # quad crosses that cut through the cube's shadow; its corners lie over different charts.
            self.settings.lightmap_texel_size = 0.05
            mesh = bpy.data.meshes.new("strip")
            mesh.from_pydata([(-9, 0.4, 0.01), (9, 0.4, 0.01), (9, 0.6, 0.01), (-9, 0.6, 0.01)], [], [(0, 1, 2, 3)])
            strip = bpy.data.objects.new("strip_decal", mesh)
            bpy.context.scene.collection.objects.link(strip)
            strip.parent = addon.direct_child_with_role(
                addon.object_with_role(self.settings.shared_root_object, addon.ROLE_VISUALS), addon.ROLE_DECALS,
            )
            self.bake()
            layer = mesh.uv_layers[addon.LIGHTMAP_UV_NAME].uv
            corners = [layer[loop].vector.copy() for loop in range(4)]
            points = []
            for step in range(1, 36):
                t = step / 36
                points.append((
                    Vector((-9 + 18 * t, 0.5, 0.01)),
                    ((corners[0] + corners[3]) * (1 - t) + (corners[1] + corners[2]) * t) / 2,
                ))
            self.assertLess(max(self.decal_differences(strip, self.ground, points)), 0.15)
            atlases = {}
            suns = [self.lightmap_value(strip, uv, atlases)[1] for _point, uv in points]
            self.assertLess(suns[17], 0.2)
            self.assertGreater(suns[2], 0.9)
            self.assertGreater(suns[-3], 0.9)

        def test_bake_fails_for_decals_without_a_surface_or_with_shared_mesh_data(self):
            self.decal("floating_decal", (30, 30, 0.01))
            first = self.decal("shared_decal_a", (6, 6, 0.01))
            second = first.copy()
            second.name = "shared_decal_b"
            bpy.context.scene.collection.objects.link(second)
            with self.assertRaises(addon.TrackValidationError) as raised:
                for _step in addon.iter_lightmap_bake(bpy.context, self.settings, lambda *_args: None):
                    pass
            errors = raised.exception.errors
            self.assertTrue(any("floating_decal" in error and "no lightmapped surface" in error for error in errors))
            self.assertTrue(any("shared_decal_a" in error and "shares its mesh data" in error for error in errors))

        def grass(self, name, centres):
            """Upright 0.3 m grass cards at the centres, as one mesh under the shared GRASS group, Shadow off."""
            vertices, faces = [], []
            for x, y in centres:
                base = len(vertices)
                vertices += [(x - 0.15, y, 0.0), (x + 0.15, y, 0.0), (x + 0.15, y, 0.3), (x - 0.15, y, 0.3)]
                faces.append((base, base + 1, base + 2, base + 3))
            mesh = bpy.data.meshes.new(name)
            mesh.from_pydata(vertices, [], faces)
            mesh.uv_layers.new(name="UVMap")
            obj = bpy.data.objects.new(name, mesh)
            obj.visible_shadow = False
            bpy.context.scene.collection.objects.link(obj)
            shared_visuals = addon.object_with_role(self.settings.shared_root_object, addon.ROLE_VISUALS)
            foliage = addon.direct_child_with_role(shared_visuals, addon.ROLE_FOLIAGE_CARDS)
            obj.parent = addon.direct_child_with_role(foliage, addon.ROLE_FOLIAGE_GRASS)
            return obj

        def test_grass_shows_the_lightmap_of_the_ground_under_it(self):
            shaded = self.grass("shaded_grass", [(-0.4, -0.4), (0.4, 0.4)])
            lit = self.grass("lit_grass", [(6, 6), (-6, 6)])
            self.bake()
            for obj in (shaded, lit):
                self.assertEqual(obj[addon.LIGHTMAP_PROPERTY], "lightmaps/shared_grass.png")
                self.assertEqual(obj.data.uv_layers.find(addon.LIGHTMAP_UV_NAME), 1)
                self.assertTrue(obj.data.uv_layers[0].active_render)
            # Card bottoms and tops take the ground straight below them: the cube's shadow and occlusion...
            for point in ((-0.4, -0.4, 0.0), (0.4, 0.4, 0.3)):
                ambient_occlusion, sun = self.sample(shaded, point)
                ground_occlusion, _ground_sun = self.sample(self.ground, (point[0], point[1], 0.0))
                self.assertLess(sun, 0.2)
                self.assertLess(ambient_occlusion, 0.95)
                self.assertAlmostEqual(ambient_occlusion, ground_occlusion, delta=0.1)
            # ...or the sun.
            for point in ((6, 6, 0.3), (-6, 6, 0.0)):
                self.assertGreater(self.sample(lit, point)[1], 0.9)
            self.assertEqual(addon.validate_scene(self.settings, bpy.context)[0], [])
            with tempfile.TemporaryDirectory() as directory:
                output = str(Path(directory) / "track.zip")
                self.assertEqual(bpy.ops.track_exporter.export_track_zip(filepath=output), {"FINISHED"})
                with zipfile.ZipFile(output) as archive:
                    manifest = json.loads(archive.read("manifest.json"))
                    self.assertEqual(manifest["lightmaps"], ["lightmaps/shared_0.png", "lightmaps/shared_grass.png"])
                    self.assertTrue(archive.read("lightmaps/shared_grass.png").startswith(b"\x89PNG"))
                    model = glb_json(archive.read(manifest["model"]))
            node = next(node for node in model["nodes"] if node.get("name") == "lit_grass")
            self.assertEqual(node["extras"][addon.LIGHTMAP_PROPERTY], "lightmaps/shared_grass.png")
            attributes = model["meshes"][node["mesh"]]["primitives"][0]["attributes"]
            self.assertIn("TEXCOORD_1", attributes)
            self.assertNotIn("TEXCOORD_2", attributes)
            # Grass added after the bake needs a new one.
            self.grass("late_grass", [(3, -3)])
            errors, _warnings = addon.validate_scene(self.settings, bpy.context)
            self.assertIn("late_grass has no current lightmap; click Bake Shadows", errors)

        def test_bake_fails_for_grass_without_ground_or_with_shared_mesh_data(self):
            self.grass("floating_grass", [(40, 40)])
            first = self.grass("shared_grass_a", [(3, 3)])
            second = first.copy()
            second.name = "shared_grass_b"
            bpy.context.scene.collection.objects.link(second)
            with self.assertRaises(addon.TrackValidationError) as raised:
                for _step in addon.iter_lightmap_bake(bpy.context, self.settings, lambda *_args: None):
                    pass
            errors = raised.exception.errors
            self.assertTrue(any("floating_grass" in error and "no lightmapped surface under it" in error for error in errors))
            self.assertTrue(any("shared_grass_a" in error and "shares its mesh data" in error for error in errors))

        def test_see_through_receivers_bake_as_solid_and_cast_see_through_shadows(self):
            def panel(location, alpha):
                bpy.ops.mesh.primitive_plane_add(size=2, location=location)
                obj = bpy.context.object
                obj.parent = self.pbr
                material = bpy.data.materials.new(f"fence_{alpha}")
                material.use_nodes = True
                material.node_tree.nodes["Principled BSDF"].inputs["Alpha"].default_value = alpha
                obj.data.materials.append(material)
                return obj

            fence = panel((-6, 6, 1), 0.3)
            hole = panel((6, -6, 1), 0.0)
            self.bake()
            ambient_occlusion, sun = self.sample(fence, (0, 0, 0))
            self.assertGreater(ambient_occlusion, 0.95)
            self.assertGreater(sun, 0.95)
            # The fence still lets most of the sun through onto the ground below it.
            self.assertTrue(0.5 < self.sample(self.ground, (-6, 6, 0))[1] < 0.9)
            # A fully transparent panel receives no light; its texels are filled, never left black.
            self.assertGreater(self.sample(hole, (0, 0, 0))[1], 0.5)

        def test_fence_gaps_take_the_lighting_of_the_fence_around_them(self):
            # Left half opaque, right half fully transparent, in the cube's shadow.
            mask = bpy.data.images.new("fence_mask", 2, 1, alpha=True)
            mask.pixels = [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 0.0]
            bpy.ops.mesh.primitive_plane_add(size=1.5, location=(0, 0, 1))
            fence = bpy.context.object
            fence.parent = self.pbr
            material = bpy.data.materials.new("fence_mask")
            material.use_nodes = True
            nodes = material.node_tree.nodes
            texture = nodes.new("ShaderNodeTexImage")
            texture.image = mask
            texture.interpolation = "Closest"
            material.node_tree.links.new(texture.outputs["Alpha"], nodes["Principled BSDF"].inputs["Alpha"])
            fence.data.materials.append(material)
            self.bake()
            self.assertLess(self.sample(fence, (-0.75, -0.75, 0))[1], 0.2)
            # The gap takes the fence's shadow, not the lit ground's lighting beside it in the atlas.
            self.assertLess(self.sample(fence, (0.75, 0.75, 0))[1], 0.2)

        def shared_texels(self, obj):
            """Texels of obj's lightmap whose centre two or more of its triangles cover."""
            size = addon.baked_lightmap_images()[obj[addon.LIGHTMAP_PROPERTY]].size[0]
            mesh = obj.data
            uv = mesh.uv_layers[addon.LIGHTMAP_UV_NAME].uv
            mesh.calc_loop_triangles()
            count = np.zeros((size, size), dtype=np.int32)
            for triangle in mesh.loop_triangles:
                a, b, c = (np.array(uv[loop].vector) * size for loop in triangle.loops)
                low = np.floor(np.minimum(np.minimum(a, b), c)).astype(int)
                high = np.ceil(np.maximum(np.maximum(a, b), c)).astype(int)
                xs, ys = np.meshgrid(np.arange(low[0], high[0]) + 0.5, np.arange(low[1], high[1]) + 0.5)
                determinant = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
                if abs(determinant) < 1e-12:
                    continue
                w0 = ((b[1] - c[1]) * (xs - c[0]) + (c[0] - b[0]) * (ys - c[1])) / determinant
                w1 = ((c[1] - a[1]) * (xs - c[0]) + (a[0] - c[0]) * (ys - c[1])) / determinant
                inside = (w0 > 1e-9) & (w1 > 1e-9) & (1 - w0 - w1 > 1e-9)
                np.add.at(count, (ys[inside].astype(int), xs[inside].astype(int)), 1)
            return int((count > 1).sum())

        def test_box_projected_props_keep_faces_behind_one_another_off_each_others_texels(self):
            # A wall with a hidden board behind it and many small planks in front: loose parts enough
            # for the box projection, where every face pointing one way shares one flat chart.
            bpy.ops.mesh.primitive_plane_add(size=1, location=(-6, -6, 1.5), rotation=(math.pi / 2, 0, 0))
            prop = bpy.context.object
            prop.scale = (4, 3, 1)
            bpy.ops.object.transform_apply(scale=True)
            parts = [prop]
            bpy.ops.mesh.primitive_plane_add(size=1, location=(-6, -5.8, 1.5), rotation=(math.pi / 2, 0, 0))
            hidden = bpy.context.object
            hidden.scale = (3, 2, 1)
            bpy.ops.object.transform_apply(scale=True)
            parts.append(hidden)
            for index in range(60):
                bpy.ops.mesh.primitive_cube_add(size=0.12, location=(-7.8 + (index % 12) * 0.3, -6.1, 0.4 + (index // 12) * 0.5))
                parts.append(bpy.context.object)
            bpy.ops.object.select_all(action="DESELECT")
            for part in parts:
                part.select_set(True)
            bpy.context.view_layer.objects.active = prop
            bpy.ops.object.join()
            prop.name = "house_prop"
            prop.parent = self.pbr
            self.bake()
            self.assertEqual(self.shared_texels(prop), 0)

        def ambient_occlusion_texels(self, obj, facing=None):
            """Ambient occlusion of every texel whose centre one of obj's lightmap triangles covers,
            of the triangles facing a world direction only when one is given."""
            image = addon.baked_lightmap_images()[obj[addon.LIGHTMAP_PROPERTY]]
            size = image.size[0]
            pixels = np.empty(len(image.pixels), dtype=np.float32)
            image.pixels.foreach_get(pixels)
            red = pixels.reshape(size, size, 4)[..., 0]
            mesh = obj.data
            uv = mesh.uv_layers[addon.LIGHTMAP_UV_NAME].uv
            mesh.calc_loop_triangles()
            values = []
            for triangle in mesh.loop_triangles:
                if facing and (obj.matrix_world.to_3x3() @ triangle.normal).normalized().dot(Vector(facing)) < 0.9:
                    continue
                a, b, c = (np.array(uv[loop].vector) * size for loop in triangle.loops)
                low = np.floor(np.minimum(np.minimum(a, b), c)).astype(int)
                high = np.ceil(np.maximum(np.maximum(a, b), c)).astype(int)
                xs, ys = np.meshgrid(np.arange(low[0], high[0]), np.arange(low[1], high[1]))
                determinant = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
                if abs(determinant) < 1e-12:
                    continue
                w0 = ((b[1] - c[1]) * (xs + 0.5 - c[0]) + (c[0] - b[0]) * (ys + 0.5 - c[1])) / determinant
                w1 = ((c[1] - a[1]) * (xs + 0.5 - c[0]) + (a[0] - c[0]) * (ys + 0.5 - c[1])) / determinant
                inside = (w0 >= 0) & (w1 >= 0) & (1 - w0 - w1 >= 0)
                values.extend(red[ys[inside], xs[inside]].tolist())
            return np.array(values)

        def test_texels_buried_inside_intersecting_parts_take_their_neighbours_light(self):
            # A wall with a plank sunk halfway into it: the wall's texels under the plank lie inside
            # the plank and would bake black, spreading over the visible wall around it.
            bpy.ops.mesh.primitive_cube_add(size=1, location=(-6, -6, 1.5))
            wall = bpy.context.object
            wall.scale = (4, 0.2, 3)
            bpy.ops.mesh.primitive_cube_add(size=1, location=(-6, -6.1, 1.5))
            plank = bpy.context.object
            plank.scale = (3, 0.3, 0.4)
            for obj in (wall, plank):
                bpy.ops.object.select_all(action="DESELECT")
                obj.select_set(True)
                bpy.context.view_layer.objects.active = obj
                bpy.ops.object.transform_apply(scale=True)
                obj.parent = self.pbr
            # A low roof over open ground: real occlusion that must stay dark.
            bpy.ops.mesh.primitive_plane_add(size=4, location=(6, -6, 0.15))
            roof = bpy.context.object
            roof.parent = self.pbr
            self.bake()
            # The wall's face the plank is sunk into, and the plank's faces standing out of the wall.
            for obj, facing in ((wall, (0, -1, 0)), (plank, (0, -1, 0)), (plank, (0, 0, 1)), (plank, (1, 0, 0))):
                self.assertGreater(self.ambient_occlusion_texels(obj, facing).min(), 0.1, f"{obj.name} facing {facing}")
            self.assertLess(self.sample(self.ground, (6, -6, 0))[0], 0.2)

        def test_bake_softens_the_sun_and_restores_it(self):
            scene = bpy.context.scene
            angle, bounces = self.sun.data.angle, scene.cycles.transparent_max_bounces
            state = addon.LightmapBakeState()
            addon.prepare_lightmap_scene(bpy.context, self.settings, state, self.sun)
            try:
                self.assertAlmostEqual(self.sun.data.angle, addon.LIGHTMAP_BAKE_SUN_ANGLE, places=6)
                self.assertEqual(scene.cycles.transparent_max_bounces, addon.LIGHTMAP_TRANSPARENT_BOUNCES)
            finally:
                state.restore()
            self.assertAlmostEqual(self.sun.data.angle, angle, places=6)
            self.assertEqual(scene.cycles.transparent_max_bounces, bounces)

        def test_deep_stacks_of_cut_out_cards_cast_see_through_shadows(self):
            # Twelve stacked cards, each opaque only in its left half: more cut-out surfaces than
            # Cycles' default limit of eight, as along a ray through a tree crown.
            mask = bpy.data.images.new("card_mask", 2, 1, alpha=True)
            mask.pixels = [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 0.0]
            material = bpy.data.materials.new("card_mask")
            material.use_nodes = True
            nodes = material.node_tree.nodes
            texture = nodes.new("ShaderNodeTexImage")
            texture.image = mask
            texture.interpolation = "Closest"
            material.node_tree.links.new(texture.outputs["Alpha"], nodes["Principled BSDF"].inputs["Alpha"])
            foliage = addon.object_with_role(self.settings.shared_root_object, addon.ROLE_FOLIAGE_CARDS)
            trees = addon.direct_child_with_role(foliage, addon.ROLE_FOLIAGE_TREES)
            for index in range(12):
                bpy.ops.mesh.primitive_plane_add(size=4, location=(6, -6, 1 + 0.1 * index))
                card = bpy.context.object
                card.parent = trees
                card.data.materials.append(material)
            bounces = bpy.context.scene.cycles.transparent_max_bounces
            self.bake()
            self.assertEqual(bpy.context.scene.cycles.transparent_max_bounces, bounces)
            self.assertLess(self.sample(self.ground, (5, -6, 0))[1], 0.2)
            self.assertGreater(self.sample(self.ground, (7, -6, 0))[1], 0.9)

        def test_preview_lightmaps_shows_atlases_and_restores_materials(self):
            road = bpy.data.materials.new("road")
            self.ground.data.materials.append(road)
            cube_data, cube_object = bpy.data.materials.new("cube_data"), bpy.data.materials.new("cube_object")
            self.cube.data.materials.append(cube_data)
            self.cube.material_slots[0].link = "OBJECT"
            self.cube.material_slots[0].material = cube_object
            bpy.ops.mesh.primitive_plane_add(size=2, location=(40, 0, 0))
            unbaked = bpy.context.object
            unbaked.data.materials.append(road)
            self.bake()

            self.settings.lightmap_preview = True
            atlas = addon.baked_lightmap_images()["lightmaps/shared_0.png"]
            for obj in (self.ground, self.cube):
                slot = obj.material_slots[0]
                self.assertEqual(slot.link, "OBJECT")
                images = [node.image for node in slot.material.node_tree.nodes if node.bl_idname == "ShaderNodeTexImage"]
                self.assertEqual(images, [atlas])
            self.assertEqual(unbaked.material_slots[0].material.name, "vectorg_lightmap_preview_white")
            self.assertEqual(self.ground.data.materials[0], road)

            self.settings.lightmap_preview = False
            self.assertEqual((self.ground.material_slots[0].link, self.ground.material_slots[0].material), ("DATA", road))
            self.assertEqual((self.cube.material_slots[0].link, self.cube.material_slots[0].material), ("OBJECT", cube_object))
            self.assertEqual(self.cube.data.materials[0], cube_data)
            self.assertEqual(unbaked.material_slots[0].material, road)
            self.assertFalse(any(material.get(addon.LIGHTMAP_PREVIEW_PROPERTY) for material in bpy.data.materials))
            self.assertFalse(any(addon.LIGHTMAP_PREVIEW_PROPERTY in obj for obj in bpy.data.objects))

            # Baking needs the real materials, so it ends the preview first.
            self.settings.lightmap_preview = True
            self.bake()
            self.assertFalse(self.settings.lightmap_preview)
            self.assertEqual(self.ground.material_slots[0].material, road)

        def test_export_writes_atlas_manifest_and_last_texcoord(self):
            self.ground.data.uv_layers.new(name="Detail")
            bare = bpy.data.meshes.new("bare")
            bare.from_pydata([(0, 0, 5), (1, 0, 5), (0, 1, 5)], [], [(0, 1, 2)])
            bare_object = bpy.data.objects.new("bare", bare)
            bpy.context.scene.collection.objects.link(bare_object)
            bare_object.parent = self.pbr
            self.bake()
            self.assertEqual([layer.name for layer in bare.uv_layers], [addon.LIGHTMAP_UV_NAME])
            errors, _warnings = addon.validate_scene(self.settings, bpy.context)
            self.assertEqual(errors, [])
            with tempfile.TemporaryDirectory() as directory:
                output = str(Path(directory) / "track.zip")
                self.assertEqual(bpy.ops.track_exporter.export_track_zip(filepath=output), {"FINISHED"})
                with zipfile.ZipFile(output) as archive:
                    manifest = json.loads(archive.read("manifest.json"))
                    self.assertEqual(manifest["lightmaps"], ["lightmaps/shared_0.png"])
                    self.assertEqual(len(manifest["sun"]["direction"]), 3)
                    self.assertTrue(archive.read("lightmaps/shared_0.png").startswith(b"\x89PNG"))
                    model = glb_json(archive.read(manifest["model"]))
            node = next(node for node in model["nodes"] if node.get("name") == "ground")
            self.assertEqual(node["extras"][addon.LIGHTMAP_PROPERTY], "lightmaps/shared_0.png")
            attributes = model["meshes"][node["mesh"]]["primitives"][0]["attributes"]
            self.assertIn("TEXCOORD_2", attributes)
            self.assertNotIn("TEXCOORD_3", attributes)
            bare_node = next(node for node in model["nodes"] if node.get("name") == "bare")
            bare_attributes = model["meshes"][bare_node["mesh"]]["primitives"][0]["attributes"]
            self.assertEqual(sorted(key for key in bare_attributes if key.startswith("TEXCOORD")), ["TEXCOORD_0"])
            self.assertFalse(any("lightmap" in image.get("name", "").lower() for image in model.get("images", [])))

        def test_moved_sun_and_edited_receivers_need_a_new_bake(self):
            self.bake()
            self.sun.rotation_euler = Euler((0.3, 0.0, 0.0))
            bpy.context.view_layer.update()
            errors, _warnings = addon.validate_scene(self.settings, bpy.context)
            self.assertIn("The preview sun moved since the lightmap bake; click Bake Shadows", errors)
            bpy.ops.mesh.primitive_plane_add(location=(0, 0, 6))
            added = bpy.context.object
            added.parent = self.pbr
            errors, _warnings = addon.validate_scene(self.settings, bpy.context)
            self.assertIn(f"{added.name} has no current lightmap; click Bake Shadows", errors)

        def test_ring_shaped_road_packs_at_the_requested_texel_size(self):
            # A 1 km square loop of 10 m wide road: one coplanar ring island before chart cutting.
            segments = 200
            vertices, faces = [], []
            for index in range(segments):
                angle = math.tau * index / segments
                direction = (math.cos(angle), math.sin(angle))
                # Square loop: scale the direction onto the square's outline.
                reach = 1.0 / max(abs(direction[0]), abs(direction[1]))
                for half in (500.0, 490.0):
                    vertices.append((direction[0] * reach * half, direction[1] * reach * half, 0.0))
            for index in range(segments):
                following = (index + 1) % segments
                faces.append((2 * index, 2 * following, 2 * following + 1, 2 * index + 1))
            ring = bpy.data.meshes.new("ring")
            ring.from_pydata(vertices, [], faces)
            road = bpy.data.objects.new("road_loop", ring)
            bpy.context.scene.collection.objects.link(road)
            road.parent = self.pbr
            self.bake()
            state = json.loads(self.settings.lightmap_unwrap_state)
            texels = [entry["texel"] for entry in state.values() if "road_loop" in entry["objects"]]
            self.assertEqual(len(texels), 1)
            # Without cutting, the ring's outline fills the atlas and the texel size is about 2 m.
            self.assertLess(texels[0], 0.25 * 1.6)

        def test_rebake_reuses_unwrap_until_a_member_changes(self):
            unwraps = []
            original = addon.unwrap_lightmap_atlas

            def counted(context, state, objects, areas, atlas_size, texel_size):
                unwraps.append(sorted(obj.name for obj in objects))
                return original(context, state, objects, areas, atlas_size, texel_size)

            addon.unwrap_lightmap_atlas = counted
            try:
                self.bake()
                self.assertEqual(len(unwraps), 1)
                self.sun.rotation_euler = Euler((0.3, 0.0, 0.0))
                bpy.context.view_layer.update()
                self.bake()
                self.assertEqual(len(unwraps), 1)
                errors, _warnings = addon.validate_scene(self.settings, bpy.context)
                self.assertEqual(errors, [])
                self.ground.scale = (2.0, 2.0, 1.0)
                bpy.context.view_layer.update()
                self.bake()
                self.assertEqual(len(unwraps), 2)
                self.settings.lightmap_texel_size = 0.5
                self.bake()
                self.assertEqual(len(unwraps), 3)
            finally:
                addon.unwrap_lightmap_atlas = original

        def test_bake_survives_save_and_reopen(self):
            self.bake()
            before = addon.baked_lightmap_images()["lightmaps/shared_0.png"]
            size = tuple(before.size)
            pixels = np.empty(len(before.pixels), dtype=np.float32)
            before.pixels.foreach_get(pixels)
            with tempfile.TemporaryDirectory() as directory:
                path = str(Path(directory) / "baked.blend")
                bpy.ops.wm.save_as_mainfile(filepath=path)
                bpy.ops.wm.open_mainfile(filepath=path)
                settings = bpy.context.scene.track_exporter
                after = addon.baked_lightmap_images()["lightmaps/shared_0.png"]
                self.assertTrue(after.packed_file)
                self.assertEqual(tuple(after.size), size)
                reopened = np.empty(len(after.pixels), dtype=np.float32)
                after.pixels.foreach_get(reopened)
                self.assertLess(float(np.abs(reopened - pixels).max()), 0.003)
                errors, _warnings = addon.validate_scene(settings, bpy.context)
                self.assertEqual(errors, [])

        def test_clear_removes_bake_and_unbaked_track_exports_without_lightmaps(self):
            self.bake()
            self.assertEqual(bpy.ops.track_exporter.clear_lightmaps(), {"FINISHED"})
            self.assertEqual(addon.baked_lightmap_images(), {})
            self.assertNotIn(addon.LIGHTMAP_PROPERTY, self.ground)
            self.assertEqual(self.ground.data.uv_layers.find(addon.LIGHTMAP_UV_NAME), -1)
            self.assertEqual(self.settings.lightmap_unwrap_state, "")
            self.assertNotIn("lightmaps", addon.build_manifest(self.settings))
            errors, _warnings = addon.validate_scene(self.settings, bpy.context)
            self.assertEqual(errors, [])

        def test_linked_duplicates_are_skipped_and_generated_geometry_is_rejected(self):
            twin = self.cube.copy()
            bpy.context.scene.collection.objects.link(twin)
            twin.location.x = 5
            self.bake()
            self.assertNotIn(addon.LIGHTMAP_PROPERTY, twin)
            self.assertNotIn(addon.LIGHTMAP_PROPERTY, self.cube)
            for index in range(3):
                self.ground.data.uv_layers.new(name=f"Extra{index}")
            with self.assertRaisesRegex(RuntimeError, "ground has 4 UV maps; remove one"):
                bpy.ops.track_exporter.bake_lightmaps()
            for index in range(3):
                self.ground.data.uv_layers.remove(self.ground.data.uv_layers[f"Extra{index}"])
            self.ground.modifiers.new("array", "ARRAY")
            with self.assertRaisesRegex(RuntimeError, "ground: apply modifiers that add or remove geometry"):
                bpy.ops.track_exporter.bake_lightmaps()
            # Rejected inputs leave the previous bake untouched; validation still flags the mesh.
            self.assertEqual(list(addon.baked_lightmap_images()), ["lightmaps/shared_0.png"])
            errors, _warnings = addon.validate_scene(self.settings, bpy.context)
            self.assertIn("ground: apply modifiers that add or remove geometry before baking", errors)

        def test_layout_scope_bakes_its_own_atlas(self):
            layout_visuals = addon.object_with_role(self.layout.root_object, addon.ROLE_VISUALS)
            bpy.ops.mesh.primitive_plane_add(size=4, location=(30, 0, 0))
            layout_ground = bpy.context.object
            layout_ground.parent = addon.direct_child_with_role(layout_visuals, addon.ROLE_PBR)
            self.bake()
            self.assertEqual(layout_ground[addon.LIGHTMAP_PROPERTY], f"lightmaps/layout_{self.layout.layout_id}_0.png")
            self.assertEqual(
                sorted(addon.baked_lightmap_images()),
                ["lightmaps/layout_" + self.layout.layout_id + "_0.png", "lightmaps/shared_0.png"],
            )

    class LightmapFillTests(unittest.TestCase):
        def test_baked_texels_are_kept_and_empty_texels_take_nearby_values(self):
            size = 16
            values = np.zeros((size, size), dtype=np.float32)
            covered = np.zeros((size, size), dtype=bool)
            values[2:4, 2:6], covered[2:4, 2:6] = 0.9, True
            values[12:14, 10:14], covered[12:14, 10:14] = 0.2, True
            filled = addon.fill_empty_texels(values.ravel(), covered.ravel(), size).reshape(size, size)
            self.assertTrue(np.array_equal(filled[covered], values[covered]))
            self.assertAlmostEqual(float(filled[1, 3]), 0.9, places=5)
            self.assertAlmostEqual(float(filled[14, 12]), 0.2, places=5)
            self.assertTrue(((filled >= 0.2 - 1e-6) & (filled <= 0.9 + 1e-6)).all())

        def test_empty_atlas_stays_zero(self):
            filled = addon.fill_empty_texels(np.zeros(64, dtype=np.float32), np.zeros(64, dtype=bool), 8)
            self.assertEqual(float(np.abs(filled).max()), 0.0)

    suite = unittest.TestSuite([
        unittest.defaultTestLoader.loadTestsFromTestCase(BlenderLightmapTests),
        unittest.defaultTestLoader.loadTestsFromTestCase(LightmapFillTests),
    ])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    addon.unregister()
    if not result.wasSuccessful():
        raise RuntimeError("Blender lightmap tests failed")


if __name__ == "__main__":
    if "--blender" in sys.argv:
        blender_tests()
    else:
        unittest.main()
