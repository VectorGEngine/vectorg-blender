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
    from mathutils import Euler, Vector

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

        def test_shadow_off_receivers_cast_nothing_and_take_no_ambient_occlusion(self):
            # A skid mark 1 cm above the ground, away from the cube.
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
            # A see-through painted line under the cube: no ambient occlusion on it, the cube's shadow still falls on it.
            bpy.ops.mesh.primitive_plane_add(size=0.5, location=(0, 0, 0.01))
            line = bpy.context.object
            line.parent = self.pbr
            line.visible_shadow = False
            paint = bpy.data.materials.new("paint")
            paint.use_nodes = True
            paint.node_tree.nodes["Principled BSDF"].inputs["Alpha"].default_value = 0.3
            line.data.materials.append(paint)
            self.bake()
            ambient_occlusion, sun = self.sample(line, (0, 0, 0))
            self.assertGreater(ambient_occlusion, 0.99)
            self.assertLess(sun, 0.2)
            # Other receivers keep their ambient occlusion and the cube's shadow: the ground under the cube.
            ambient_occlusion, sun = self.sample(self.ground, (0, 0, 0))
            self.assertLess(ambient_occlusion, 0.95)
            self.assertLess(sun, 0.2)
            self.assertFalse(any(obj.name.startswith("vectorg_lightmap_bake") for obj in bpy.data.objects))

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
