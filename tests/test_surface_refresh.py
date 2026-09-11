"""Hierarchy/geometry unit tests, plus Blender export tests with --blender."""
import ast
import math
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


ADDON = Path(__file__).resolve().parents[1] / "addons/vectorg_track_exporter/__init__.py"
FUNCTIONS = {
    "scene_settings", "descendants", "hierarchy_descendants", "direct_child_with_role",
    "descendants_with_role", "object_with_role", "create_empty", "create_collision_hierarchy",
    "create_visual_hierarchy", "layout_root_name", "indexed_name_value", "ordered_objects",
    "layout_named_objects", "layout_nodes", "apply_generated_names", "sync_layout_node_names",
    "create_layout_hierarchy", "find_surface_group", "ensure_surface_group",
    "refresh_track_structure", "valid_id",
}


class Object(dict):
    __hash__ = object.__hash__
    __eq__ = object.__eq__

    def __init__(self, name):
        super().__init__()
        self.name = name
        self.children = []
        self._parent = None

    def __bool__(self):
        return True

    @property
    def parent(self):
        return self._parent

    @parent.setter
    def parent(self, value):
        if self._parent:
            self._parent.children.remove(self)
        self._parent = value
        if value:
            value.children.append(self)


class Objects(list):
    def new(self, name, _data):
        result = Object(name)
        self.append(result)
        return result

    def get(self, name):
        return next((obj for obj in self if obj.name == name), None)


class SurfaceRefreshTests(unittest.TestCase):
    def setUp(self):
        self.objects = Objects()
        namespace = {"re": re, "bpy": SimpleNamespace(data=SimpleNamespace(objects=self.objects))}
        tree = ast.parse(ADDON.read_text(encoding="utf-8"))
        selected = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS:
                selected.append(node)
            elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
                if name.startswith("ROLE_") or name in {
                    "SURFACE_IDS", "SURFACE_PROPERTY", "SHAPE_PROPERTY", "EVENT_PROPERTY",
                    "ORDER_PROPERTY", "ID_PATTERN",
                }:
                    selected.append(node)
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(ADDON), "exec"), namespace)
        self.api = SimpleNamespace(**namespace)
        self.namespace = namespace
        self.settings = SimpleNamespace(layouts=[])
        self.context = SimpleNamespace(scene=SimpleNamespace(
            track_exporter=self.settings,
            collection=SimpleNamespace(objects=SimpleNamespace(link=lambda _obj: None)),
        ))
        self.surface_ids = self.api.SURFACE_IDS
        namespace["SURFACE_IDS"] = tuple(surface for surface in self.surface_ids if not surface.startswith("wet_"))
        shared = self.api.create_empty(self.context, "SHARED", role="shared")
        self.settings.shared_root_object = shared
        self.shared_collisions = self.api.create_collision_hierarchy(self.context, shared, "SHARED")
        for layout_id in ["gp", "sprint"]:
            root, *_rest = self.api.create_layout_hierarchy(self.context, None, layout_id)
            layout = Object(layout_id)
            layout.layout_id = layout_id
            layout.root_object = root
            layout.map_curve = None
            layout.ideal_line = None
            self.settings.layouts.append(layout)
        namespace["SURFACE_IDS"] = self.surface_ids

    def collision_roots(self):
        return [self.shared_collisions, *[
            self.api.direct_child_with_role(layout.root_object, "collisions")
            for layout in self.settings.layouts
        ]]

    def test_refresh_adds_surfaces_to_shared_and_every_layout_without_duplicates(self):
        original = list(self.objects)
        tarmac = self.api.find_surface_group(self.shared_collisions, "tarmac")
        geometry = self.objects.new("existing_geometry", None)
        geometry.parent = tarmac
        geometry.location = (1, 2, 3)
        self.assertTrue(self.api.refresh_track_structure(self.context)[0])
        self.assertEqual(len(self.objects), len(original) + 1 + 9)
        for collisions in self.collision_roots():
            groups = [child for child in collisions.children if child.get("vectorg_role") == "surface"]
            self.assertEqual(sorted(group["vectorg_surface"] for group in groups), sorted(self.surface_ids))
            for group in groups:
                self.assertEqual(group.name, f"{collisions.name}_{group['vectorg_surface']}")
        first_refresh = list(self.objects)
        self.assertTrue(self.api.refresh_track_structure(self.context)[0])
        self.assertEqual(self.objects, first_refresh)
        self.assertTrue(all(obj in self.objects for obj in original))
        self.assertIs(geometry.parent, tarmac)
        self.assertEqual(geometry.location, (1, 2, 3))

    def test_refresh_synchronizes_existing_and_new_layout_surface_names(self):
        layout = self.settings.layouts[1]
        layout.layout_id = "renamed"
        self.assertTrue(self.api.refresh_track_structure(self.context)[0])
        self.assertEqual(layout.root_object.name, "layout_renamed")
        collisions = self.collision_roots()[2]
        self.assertEqual(collisions.name, "renamed_COLLISIONS")
        self.assertEqual(self.api.find_surface_group(collisions, "wet_tarmac").name, "renamed_COLLISIONS_wet_tarmac")

    def test_missing_roots_and_name_conflicts_fail_before_creating_groups(self):
        self.settings.layouts[1].root_object = None
        before = list(self.objects)
        self.assertFalse(self.api.refresh_track_structure(self.context)[0])
        self.assertEqual(self.objects, before)

    def test_conflicting_surface_name_is_not_overwritten(self):
        conflict = self.objects.new("SHARED_COLLISIONS_wet_tarmac", None)
        before = list(self.objects)
        success, message = self.api.refresh_track_structure(self.context)
        self.assertFalse(success)
        self.assertIn(conflict.name, message)
        self.assertEqual(self.objects, before)

    def test_duplicate_layout_ids_fail_before_changing_structure(self):
        self.settings.layouts[1].layout_id = self.settings.layouts[0].layout_id
        before = list(self.objects)
        self.assertFalse(self.api.refresh_track_structure(self.context)[0])
        self.assertEqual(self.objects, before)

    def test_ideal_line_is_renamed_with_layout_and_stays_under_map(self):
        layout = self.settings.layouts[0]
        line = self.objects.new("edited_line", None)
        line.parent = self.api.direct_child_with_role(layout.root_object, "map")
        layout.ideal_line = line
        layout.layout_id = "renamed"
        self.assertTrue(self.api.sync_layout_node_names(layout))
        self.assertEqual(line.name, "renamed_ideal_line")
        self.assertEqual(line.parent.name, "renamed_MAP")


class IdealLineGeometryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse(ADDON.read_text(encoding="utf-8"))
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name.startswith("ideal_")]
        namespace = {"math": math}
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(ADDON), "exec"), namespace)
        cls.api = SimpleNamespace(**namespace)

    def test_straight_is_unchanged_and_open_endpoints_are_pinned(self):
        centers = [(i * 6.0, 0.0, 0.0) for i in range(20)]
        points, offsets, converged = self.api.ideal_optimize_offsets(centers, [(0, 1, 0)] * 20, False, [3.5] * 20)
        self.assertEqual(points, centers)
        self.assertEqual(offsets, [0.0] * 20)
        self.assertTrue(converged)

    def test_circle_uses_wider_radius_instead_of_shorter_tighter_path(self):
        rights = [(math.cos(i * math.tau / 60), math.sin(i * math.tau / 60), 0) for i in range(60)]
        centers = [tuple(v * 50 for v in right) for right in rights]
        points, offsets, converged = self.api.ideal_optimize_offsets(centers, rights, True, [3.5] * len(centers))
        self.assertTrue(converged)
        self.assertTrue(all(abs(offset - 3.5) < 0.001 for offset in offsets))
        self.assertLess(self.api.ideal_curvature_energy_gradient(points, rights, True)[0],
                        self.api.ideal_curvature_energy_gradient(centers, rights, True)[0])

    def test_gradient_matches_finite_difference(self):
        points = [(0, 0, 0), (5, 1, 0.2), (9, 5, 1), (15, 4, 2), (20, 0, 2)]
        rights = [(0, 1, 0)] * len(points)
        _energy, gradient = self.api.ideal_curvature_energy_gradient(points, rights, False)
        for i in range(len(points)):
            plus, minus = list(points), list(points)
            plus[i] = tuple(a + b * 1e-5 for a, b in zip(points[i], rights[i]))
            minus[i] = tuple(a - b * 1e-5 for a, b in zip(points[i], rights[i]))
            expected = (self.api.ideal_curvature_energy_gradient(plus, rights, False)[0]
                        - self.api.ideal_curvature_energy_gradient(minus, rights, False)[0]) / 2e-5
            self.assertAlmostEqual(gradient[i], expected, places=7)

    def test_s_bend_improves_with_bounded_offsets_and_repeatable_results(self):
        points = [(i * 6, 10 * math.sin(i * math.pi / 15), 0) for i in range(61)]
        rights = []
        for i in range(len(points)):
            slope = (10 * math.pi / 90) * math.cos(i * math.pi / 15)
            norm = math.hypot(1, slope)
            rights.append((-slope / norm, 1 / norm, 0))
        result = self.api.ideal_optimize_offsets(points, rights, False, [3.5] * len(points))
        self.assertEqual(result, self.api.ideal_optimize_offsets(points, rights, False, [3.5] * len(points)))
        optimized, offsets, _converged = result
        self.assertEqual(offsets[0], 0)
        self.assertEqual(offsets[-1], 0)
        self.assertLessEqual(max(map(abs, offsets)), 3.5)
        self.assertLess(self.api.ideal_curvature_energy_gradient(optimized, rights, False)[0],
                        self.api.ideal_curvature_energy_gradient(points, rights, False)[0] * 0.9)

    def test_resampling_preserves_open_endpoints_elevation_and_tilt(self):
        points, tilts = self.api.ideal_resample([(0, 0, 0), (10, 0, 10)], [0, 1], False, 3)
        self.assertEqual(points[0], (0, 0, 0))
        self.assertEqual(points[-1], (10, 0, 10))
        self.assertEqual(tilts[-1], 1)
        self.assertTrue(all(math.dist(a, b) <= 3.00001 for a, b in zip(points, points[1:])))

    def test_invalid_geometry_and_empty_corridor_are_rejected(self):
        with self.assertRaises(ValueError):
            self.api.ideal_resample([(0, 0, 0)] * 2, [0, 0], False)
        with self.assertRaises(ValueError):
            self.api.ideal_line_limit(10, 5)
        with self.assertRaises(ValueError):
            self.api.ideal_resample([(0, 0, 0), (math.nan, 0, 0)], [0, 0], False)

    def test_closed_handles_have_matching_tangents_at_seam(self):
        points = [(math.cos(i * math.tau / 12), math.sin(i * math.tau / 12), 0) for i in range(12)]
        handles = self.api.ideal_bezier_handles(points, True)
        left, right = handles[0]
        a, b = self.api.ideal_sub(points[0], left), self.api.ideal_sub(right, points[0])
        self.assertAlmostEqual(self.api.ideal_dot(a, b) / (math.dist(a, (0, 0, 0)) * math.dist(b, (0, 0, 0))), 1)

    def test_variable_width_optimizer_respects_each_local_bound(self):
        count = 40
        centers = [(20 * math.cos(i * math.tau / count), 20 * math.sin(i * math.tau / count), 0)
                   for i in range(count)]
        rights = [(x / 20, y / 20, 0) for x, y, _z in centers]
        limits = [0.1 if 10 <= i <= 20 else 3.5 for i in range(count)]
        _points, offsets, _converged = self.api.ideal_optimize_offsets(centers, rights, True, limits)
        self.assertTrue(all(abs(offset) <= limit + 1e-9 for offset, limit in zip(offsets, limits)))
        self.assertGreater(max(offsets[:10]), 0.5)

    def test_planning_resamples_widths_at_the_same_positions(self):
        points, tilts, widths = self.api.ideal_resample(
            [(0, 0, 0), (10, 0, 0), (20, 0, 0)], [0, 1, 0], False, 5, widths=[10, 4, 8])
        self.assertEqual([p[0] for p in points], [0, 5, 10, 15, 20])
        self.assertEqual(widths, [10, 7, 4, 6, 8])
        self.assertEqual(tilts, [0, 0.5, 1, 0.5, 0])

    def test_short_narrowing_is_retained_between_regular_planning_points(self):
        points, _tilts, widths = self.api.ideal_resample(
            [(0, 0, 0), (2, 0, 0), (3, 0, 0), (4, 0, 0), (12, 0, 0)],
            [0] * 5, False, 6, widths=[10, 10, 4, 10, 10])
        by_position = {point[0]: width for point, width in zip(points, widths)}
        self.assertEqual(by_position[3], 4)
        self.assertEqual(by_position[2], 10)
        self.assertEqual(by_position[4], 10)

    def assert_fitted_shape(self, points, closed, indices, handles):
        dense_handles = self.api.ideal_bezier_handles(points, closed)
        cumulative = [0.0]
        for i in range(len(points) if closed else len(points) - 1):
            cumulative.append(cumulative[-1] + math.dist(points[i], points[(i + 1) % len(points)]))
        ends = indices[1:] + [len(points)] if closed else indices[1:]
        for fitted_index, (start, end) in enumerate(zip(indices, ends)):
            curve = (points[start], handles[fitted_index][1],
                     handles[(fitted_index + 1) % len(indices)][0], points[end % len(points)])
            for i in range(start, end):
                following = (i + 1) % len(points)
                dense = (points[i], dense_handles[i][1], dense_handles[following][0], points[following])
                for step in range(21):
                    t = step / 20
                    fitted_t = (cumulative[i] - cumulative[start]
                                + t * (cumulative[i + 1] - cumulative[i])) / (cumulative[end] - cumulative[start])
                    self.assertLessEqual(math.dist(self.api.ideal_bezier_value(curve, fitted_t),
                                                   self.api.ideal_bezier_value(dense, t)), 0.100001)
        for i in range(len(indices)) if closed else range(1, len(indices) - 1):
            point = points[indices[i]]
            left, right = handles[i]
            a, b = self.api.ideal_sub(point, left), self.api.ideal_sub(right, point)
            self.assertAlmostEqual(self.api.ideal_dot(a, b) / (math.dist(a, (0, 0, 0)) * math.dist(b, (0, 0, 0))), 1)

    def test_editable_straight_reduces_to_endpoints(self):
        points = [(i * 6, 0, i * 0.5) for i in range(101)]
        indices, handles = self.api.ideal_fit_editable_curve(points, False)
        self.assertEqual(indices, [0, 100])
        self.assert_fitted_shape(points, False, indices, handles)

    def test_editable_circle_reduces_controls_and_preserves_seam_and_shape(self):
        points = [(60 * math.cos(i * math.tau / 120), 60 * math.sin(i * math.tau / 120), 0) for i in range(120)]
        indices, handles = self.api.ideal_fit_editable_curve(points, True)
        self.assertLessEqual(len(indices), 12)
        self.assertGreaterEqual(len(indices), 3)
        self.assert_fitted_shape(points, True, indices, handles)

    def test_editable_s_bends_preserve_elevation_and_shape(self):
        points = [(i * 6, 10 * math.sin(i * math.pi / 30), 2 * math.sin(i * math.pi / 20)) for i in range(121)]
        indices, handles = self.api.ideal_fit_editable_curve(points, False)
        self.assertLess(len(indices), len(points) // 3)
        self.assertEqual((indices[0], indices[-1]), (0, len(points) - 1))
        self.assert_fitted_shape(points, False, indices, handles)

    def test_editable_short_and_tight_curves_keep_valid_controls(self):
        for points, closed in (([(0, 0, 0), (1, 0, 0)], False),
                               ([(0, 0, 0), (1, 0, 0), (0, 1, 0)], True),
                               ([(0, 0, 0), (6, 0, 0), (6, 6, 2), (0, 6, 0), (0, 12, 0)], False)):
            indices, handles = self.api.ideal_fit_editable_curve(points, closed)
            self.assertGreaterEqual(len(indices), 3 if closed else 2)
            self.assert_fitted_shape(points, closed, indices, handles)


def blender_tests():
    """Run with blender --background --factory-startup --python this_file -- --blender."""
    import bpy
    import json
    import struct
    import tempfile
    import zipfile
    from mathutils import Matrix, Vector

    sys.path.insert(0, str(ADDON.parent.parent))
    import vectorg_track_exporter as addon
    addon.register()

    class BlenderIdealLineTests(unittest.TestCase):
        def setUp(self):
            bpy.ops.object.select_all(action="SELECT")
            bpy.ops.object.delete(use_global=False)
            bpy.ops.track_exporter.create_configuration()
            bpy.ops.track_exporter.add_layout()
            self.settings = bpy.context.scene.track_exporter
            self.layout = self.settings.layouts[0]
            self.layout.route_type = "freeform"
            data = bpy.data.meshes.new("road")
            data.from_pydata([(-200, -200, 0), (200, -200, 0), (200, 200, 0), (-200, 200, 0)], [], [(0, 1, 2, 3)])
            road = bpy.data.objects.new("road", data)
            bpy.context.scene.collection.objects.link(road)
            collisions = addon.object_with_role(self.settings.shared_root_object, addon.ROLE_COLLISIONS)
            road.parent = addon.find_surface_group(collisions, "tarmac")
            self.road = road
            self.make_route(False)
            bpy.context.view_layer.update()

        def make_route(self, closed):
            data = bpy.data.curves.new("route", "CURVE")
            data.dimensions = "3D"
            data.bevel_depth = 5
            data.use_radius = True
            spline = data.splines.new("BEZIER")
            points = [(50 * math.cos(i * math.tau / 8), 50 * math.sin(i * math.tau / 8), 0.4)
                      for i in range(8)] if closed else [(-40, 0, 0.4), (-10, 8, 0.4), (10, -8, 0.4), (40, 0, 0.4)]
            spline.bezier_points.add(len(points) - 1)
            for bp, point in zip(spline.bezier_points, points):
                bp.co = point
                bp.handle_left_type = bp.handle_right_type = "AUTO"
            spline.use_cyclic_u = closed
            obj = bpy.data.objects.new("route", data)
            bpy.context.scene.collection.objects.link(obj)
            self.layout.map_curve = obj
            return obj

        def generate(self):
            result = bpy.ops.track_exporter.generate_ideal_line()
            self.assertEqual(result, {"FINISHED"})
            bpy.context.view_layer.update()
            return self.layout.ideal_line

        def test_generation_parent_names_and_editable_curve(self):
            line = self.generate()
            self.assertEqual(line.parent, addon.direct_child_with_role(self.layout.root_object, addon.ROLE_MAP))
            self.assertEqual(line.name, f"{self.layout.layout_id}_ideal_line")
            self.assertEqual(line.data.splines[0].type, "BEZIER")
            self.assertFalse(line.data.splines[0].use_cyclic_u)
            self.assertEqual(bpy.context.view_layer.objects.active, line)
            self.layout.layout_id = "renamed"
            self.assertEqual(line.name, "renamed_ideal_line")

        def test_generated_straight_has_two_editable_controls_but_dense_export(self):
            spline = self.layout.map_curve.data.splines[0]
            for index, bp in enumerate(spline.bezier_points):
                bp.co = (-150 + index * 100, 0, 0.4)
            bpy.context.view_layer.update()
            line = self.generate()
            self.assertEqual(len(line.data.splines[0].bezier_points), 2)
            data, _warnings = addon.layout_ideal_line_data(self.layout, bpy.context)
            self.assertGreaterEqual(len(data["samples"]), 61)

        def test_export_projects_above_and_below_without_editing_curve_or_route_length(self):
            line = self.generate()
            for height in (0.7, -0.7):
                line.location.z = height
                bpy.context.view_layer.update()
                before = line.matrix_world.copy()
                route_length = self.layout.length
                data, _warnings = addon.layout_ideal_line_data(self.layout, bpy.context)
                self.assertTrue(all(abs(p["position"][1]) < 1e-5 for p in data["samples"]))
                self.assertEqual(line.matrix_world, before)
                self.assertEqual(self.layout.length, route_length)
                self.assertEqual(data["frame"], "surface_normal")
                self.assertTrue(all(p["up"][1] > 0.99 for p in data["samples"]))
                self.assertAlmostEqual(data["samples"][-1]["s"], data["length"], places=5)
                self.assertIn("idealLine", addon.build_manifest(self.settings)["layouts"][0])

        def test_assignment_preserves_world_transform_and_regeneration_leaves_shared_data_unchanged(self):
            line = self.generate()
            clone = bpy.data.objects.new("handmade", line.data)
            bpy.context.scene.collection.objects.link(clone)
            clone.matrix_world = Matrix.Translation((1, 2, 0.3))
            bpy.context.view_layer.update()
            before = clone.matrix_world.copy()
            original_data = line.data
            line.name = "previous_line_backup"
            self.layout.ideal_line = clone
            bpy.context.view_layer.update()
            self.assertEqual(clone.matrix_world, before)
            self.generate()
            self.assertIs(line.data, original_data)
            self.assertIsNot(clone.data, original_data)

        def test_surface_projection_uses_nearest_level_and_rejects_missing_hit(self):
            line = self.generate()
            raised = self.road.copy()
            raised.data = self.road.data.copy()
            bpy.context.scene.collection.objects.link(raised)
            raised.location.z = 1.5
            line.location.z = 0.3
            bpy.context.view_layer.update()
            data, _warnings = addon.layout_ideal_line_data(self.layout, bpy.context)
            self.assertTrue(all(abs(p["position"][1]) < 1e-5 for p in data["samples"]))
            line.location.z = 1.1
            bpy.context.view_layer.update()
            data, _warnings = addon.layout_ideal_line_data(self.layout, bpy.context)
            self.assertTrue(all(abs(p["position"][1] - 1.5) < 1e-5 for p in data["samples"]))
            line.location.z = 10
            bpy.context.view_layer.update()
            with self.assertRaisesRegex(ValueError, "no road collision surface"):
                addon.layout_ideal_line_data(self.layout, bpy.context)

        def test_circular_export_has_surface_projected_seam_and_rejects_open_mismatch(self):
            self.make_route(True)
            self.layout.route_type = "circular"
            events = addon.object_with_role(self.layout.root_object, addon.ROLE_EVENTS)
            start = addon.create_empty(bpy.context, "start", events)
            start[addon.EVENT_PROPERTY] = "start_finish"
            start.location = (48, 8, 0)
            checkpoint = addon.create_empty(bpy.context, "checkpoint", events)
            checkpoint[addon.EVENT_PROPERTY] = "checkpoint"
            checkpoint[addon.ORDER_PROPERTY] = 1
            checkpoint.location = (-50, 0, 0)
            bpy.context.view_layer.update()
            line = self.generate()
            self.assertTrue(line.data.splines[0].use_cyclic_u)
            data, _warnings = addon.layout_ideal_line_data(self.layout, bpy.context)
            self.assertTrue(data["closed"])
            self.assertEqual(data["samples"][0]["s"], 0)
            self.assertEqual(data["frame"], "surface_normal")
            self.assertTrue(all(abs(p["position"][1]) < 1e-5 for p in data["samples"]))
            self.assertLess(data["samples"][-1]["s"], data["length"])
            line.data.splines[0].use_cyclic_u = False
            with self.assertRaisesRegex(ValueError, "cyclic"):
                addon.layout_ideal_line_data(self.layout, bpy.context)

        def test_point_to_point_exports_open_line_and_endpoint_events(self):
            self.layout.route_type = "point_to_point"
            events = addon.object_with_role(self.layout.root_object, addon.ROLE_EVENTS)
            for event_type, point in (("start", (-40, 0, 0)), ("finish", (40, 0, 0))):
                event = addon.create_empty(bpy.context, event_type, events)
                event[addon.EVENT_PROPERTY] = event_type
                event.location = point
            bpy.context.view_layer.update()
            line = self.generate()
            data, _warnings = addon.layout_ideal_line_data(self.layout, bpy.context)
            self.assertFalse(data["closed"])
            self.assertEqual(data["samples"][0]["s"], 0)
            self.assertEqual(data["samples"][-1]["s"], data["length"])
            self.assertEqual(data["events"][0]["type"], "start")
            self.assertEqual(data["events"][-1]["type"], "finish")
            line.data.splines[0].use_cyclic_u = True
            with self.assertRaisesRegex(ValueError, "open"):
                addon.layout_ideal_line_data(self.layout, bpy.context)

        def test_distant_checkpoint_exports_on_route_and_ideal_line(self):
            self.generate()
            events = addon.object_with_role(self.layout.root_object, addon.ROLE_EVENTS)
            checkpoint = addon.create_empty(bpy.context, "distant_checkpoint", events)
            checkpoint[addon.EVENT_PROPERTY] = "checkpoint"
            checkpoint[addon.ORDER_PROPERTY] = 1
            checkpoint.location = (0, 0, 100)
            bpy.context.view_layer.update()
            route = addon.layout_route_data(self.layout)
            ideal_line, _warnings = addon.layout_ideal_line_data(self.layout, bpy.context)
            for data in (route, ideal_line):
                event = next(event for event in data["events"] if event["object"] == checkpoint.name)
                self.assertEqual(event["type"], "checkpoint")
                self.assertGreaterEqual(event["s"], 0)
                self.assertLessEqual(event["s"], data["length"])

        def test_width_change_and_export_preserve_manual_control_point_edit(self):
            line = self.generate()
            bp = line.data.splines[0].bezier_points[3]
            bp.co.y += 0.5
            before = bp.co.copy()
            self.layout.map_curve.data.bevel_depth = 6
            bpy.context.view_layer.update()
            addon.layout_ideal_line_data(self.layout, bpy.context)
            self.assertEqual(bp.co, before)

        def test_route_exports_variable_width_and_ideal_line_uses_it(self):
            spline = self.layout.map_curve.data.splines[0]
            spline.radius_interpolation = "LINEAR"
            for point, radius in zip(spline.bezier_points, (1, 0.5, 0.75, 1.2)):
                point.radius = radius
            bpy.context.view_layer.update()
            route = addon.layout_route_data(self.layout)
            self.assertEqual(route["version"], 3)
            self.assertEqual(route["samples"][0]["width"], 10)
            self.assertAlmostEqual(route["samples"][-1]["width"], 12, places=5)
            self.assertAlmostEqual(min(s["width"] for s in route["samples"]), 5, places=5)
            self.generate()
            data, warnings = addon.layout_ideal_line_data(self.layout, bpy.context)
            self.assertNotIn("roadWidth", data)
            self.assertNotIn("roadWidth", data["generation"])
            self.assertFalse(any("width/clearance" in warning for warning in warnings), warnings)

        def test_narrow_road_rejects_clearance_even_without_a_planning_point_there(self):
            spline = self.layout.map_curve.data.splines[0]
            spline.bezier_points[1].radius = 0.1
            bpy.context.view_layer.update()
            with self.assertRaisesRegex(ValueError, "Edge clearance"):
                addon.generate_ideal_line(bpy.context, self.layout)

        def test_legacy_generation_metadata_does_not_export_removed_road_width(self):
            line = self.generate()
            metadata = json.loads(line["vectorg_ideal_line_generation"])
            metadata["roadWidth"] = 999
            line["vectorg_ideal_line_generation"] = json.dumps(metadata)
            data, _warnings = addon.layout_ideal_line_data(self.layout, bpy.context)
            self.assertNotIn("roadWidth", data["generation"])

        def test_route_projection_handles_dense_reference_samples(self):
            route = {
                "closed": False, "length": 20.0,
                "samples": [{"s": i / 100, "position": [i / 100, 0, 0], "up": [0, 1, 0]}
                            for i in range(2001)],
            }
            samples = [{"position": [x, 0, 2]} for x in (0, 5, 10, 15, 20)]
            projections = addon.project_ideal_samples(route, samples)
            for sample, projection in zip(samples, projections):
                self.assertAlmostEqual(projection[0], sample["position"][0], places=5)

        def test_sloped_surface_exports_normals_and_recalculates_length(self):
            line = self.generate()
            for vertex in self.road.data.vertices:
                vertex.co.z = vertex.co.x * 0.02
            bpy.context.view_layer.update()
            data, _warnings = addon.layout_ideal_line_data(self.layout, bpy.context)
            positions = [Vector(sample["position"]) for sample in data["samples"]]
            measured_length = sum((b - a).length for a, b in zip(positions, positions[1:]))
            self.assertAlmostEqual(data["length"], measured_length, places=4)
            for sample in data["samples"]:
                self.assertAlmostEqual(sample["position"][1], sample["position"][0] * 0.02, places=5)
                self.assertAlmostEqual(Vector(sample["up"]).length, 1, places=5)
                self.assertAlmostEqual(Vector(sample["up"]).dot(Vector(sample["forward"])), 0, places=5)

        def test_zip_contains_edited_projected_line_and_glb_excludes_map_hierarchy(self):
            line = self.generate()
            bp = line.data.splines[0].bezier_points[3]
            bp.co.y += 0.4
            line.location.z = -0.5
            bpy.ops.track_exporter.add_spawn_point()
            bpy.context.view_layer.update()
            expected, _warnings = addon.layout_ideal_line_data(self.layout, bpy.context)
            curve_points = [point.co.copy() for point in line.data.splines[0].bezier_points]
            errors, _warnings = addon.validate_scene(self.settings, bpy.context)
            self.assertEqual(errors, [])
            with tempfile.TemporaryDirectory() as directory:
                output = str(Path(directory) / "track.zip")
                result = bpy.ops.track_exporter.export_track_zip(filepath=output)
                self.assertEqual(result, {"FINISHED"})
                with zipfile.ZipFile(output) as archive:
                    manifest = json.loads(archive.read("manifest.json"))
                    layout_manifest = manifest["layouts"][0]
                    self.assertEqual(json.loads(archive.read(layout_manifest["idealLine"])), expected)
                    route = json.loads(archive.read(layout_manifest["route"]))
                    self.assertEqual(route["version"], 3)
                    self.assertTrue(all(sample["width"] == 10 for sample in route["samples"]))
                    self.assertNotIn("roadWidth", expected)
                    self.assertNotIn("roadWidth", expected["generation"])
                    glb = archive.read(manifest["model"])
                    chunk_length = struct.unpack_from("<I", glb, 12)[0]
                    model = json.loads(glb[20:20 + chunk_length])
                    names = [node.get("name", "") for node in model.get("nodes", [])]
                    self.assertNotIn(line.name, names)
                    self.assertNotIn(line.parent.name, names)
            self.assertEqual([point.co.copy() for point in line.data.splines[0].bezier_points], curve_points)

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(BlenderIdealLineTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    addon.unregister()
    if not result.wasSuccessful():
        raise RuntimeError("Blender ideal-line integration tests failed")


if __name__ == "__main__":
    if "--blender" in sys.argv:
        blender_tests()
    else:
        unittest.main()
