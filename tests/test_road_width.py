"""Road-width sampling tests that run without Blender."""
import ast
import math
import unittest
from pathlib import Path
from types import SimpleNamespace


ADDON = Path(__file__).resolve().parents[1] / "addons/vectorg_track_exporter/__init__.py"


class Vector(tuple):
    def __add__(self, other):
        return Vector(a + b for a, b in zip(self, other))

    def __sub__(self, other):
        return Vector(a - b for a, b in zip(self, other))

    def __mul__(self, value):
        return Vector(a * value for a in self)

    def dot(self, other):
        return sum(a * b for a, b in zip(self, other))

    @property
    def length_squared(self):
        return self.dot(self)

    @property
    def length(self):
        return math.sqrt(self.length_squared)

    def angle(self, other):
        return math.acos(max(-1, min(1, self.dot(other) / (self.length * other.length))))

    def lerp(self, other, amount):
        return self + (other - self) * amount


class IdentityMatrix:
    def __init__(self):
        self.col = [Vector((1, 0, 0)), Vector((0, 1, 0)), Vector((0, 0, 1))]

    def __matmul__(self, point):
        return Vector(point[:3])

    def to_3x3(self):
        return self

    def determinant(self):
        return 1


class RoadWidthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        names = {
            "split_bezier", "bezier_needs_subdivision", "sample_bezier_segment", "subdivide_line",
            "curve_point_radius", "road_curve_depth", "adaptive_route_samples", "closest_route_position",
            "rebase_closed_route", "route_width_at",
        }
        source = ast.parse(ADDON.read_text(encoding="utf-8"))
        nodes = [node for node in source.body if
                 (isinstance(node, ast.FunctionDef) and node.name in names)
                 or (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                     and node.targets[0].id.startswith("ROUTE_"))]
        namespace = {"math": math, "Vector": Vector}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ADDON), "exec"), namespace)
        cls.api = SimpleNamespace(**namespace)

    def curve(self, radii=(1, 2), kind="BEZIER", interpolation="LINEAR", spacing=10, closed=False):
        points = []
        for i, radius in enumerate(radii):
            co = Vector((spacing * i, 0, 0))
            points.append(SimpleNamespace(co=co, radius=radius, tilt=i * 0.2,
                                          handle_left=co - Vector((spacing / 3, 0, 0)),
                                          handle_right=co + Vector((spacing / 3, 0, 0))))
        spline = SimpleNamespace(type=kind, use_cyclic_u=closed, bezier_points=points, points=points,
                                 radius_interpolation=interpolation)
        data = SimpleNamespace(splines=[spline], bevel_depth=5, use_radius=True, dimensions="3D",
                               bevel_mode="ROUND", taper_object=None, extrude=0, offset=0,
                               bevel_factor_start=0, bevel_factor_end=1)
        return SimpleNamespace(data=data, matrix_world=IdentityMatrix())

    def test_bezier_and_poly_widths_share_position_samples_and_tilt(self):
        for kind in ("BEZIER", "POLY"):
            with self.subTest(kind=kind):
                curve = self.curve(kind=kind)
                positions, _ = self.api.adaptive_route_samples(curve)
                road, _ = self.api.adaptive_route_samples(curve, with_width=True)
                self.assertEqual([s["position"] for s in road], [s["position"] for s in positions])
                for sample in road:
                    self.assertAlmostEqual(sample["width"], 10 + sample["position"][0])
                    self.assertAlmostEqual(sample["tilt"], sample["position"][0] / 50)
                self.assertNotIn("width", positions[0])

    def test_disabled_point_radius_uses_bevel_diameter(self):
        curve = self.curve()
        curve.data.use_radius = False
        samples, _ = self.api.adaptive_route_samples(curve, True)
        self.assertTrue(all(s["width"] == 10 for s in samples))

    def test_ease_width_adds_samples_on_a_short_straight(self):
        samples, _ = self.api.adaptive_route_samples(self.curve(spacing=2, interpolation="EASE"), True)
        self.assertGreater(len(samples), 2)
        for a, b in zip(samples, samples[1:]):
            for amount in (0.25, 0.5, 0.75):
                t = (a["position"][0] + (b["position"][0] - a["position"][0]) * amount) / 2
                width = a["width"] + (b["width"] - a["width"]) * amount
                self.assertAlmostEqual(width, 10 + 10 * t * t * (3 - 2 * t), delta=0.010001)

    def test_interpolation_matches_blender_basis_and_closed_neighbors(self):
        points = [SimpleNamespace(radius=value) for value in (1, 2, 4, 8)]
        radius = self.api.curve_point_radius
        self.assertEqual(radius(points, 1, 0, False, "CARDINAL"), 2)
        self.assertEqual(radius(points, 1, 1, False, "CARDINAL"), 4)
        self.assertAlmostEqual(radius(points, 1, 0.5, False, "CARDINAL"), 2.73375)
        self.assertAlmostEqual(radius(points, 1, 0, False, "BSPLINE"), 13 / 6)
        self.assertAlmostEqual(radius(points, 3, 0.5, True, "LINEAR"), 4.5)

    def test_rebased_seam_retains_interpolated_width(self):
        samples = [{"position": Vector(p), "width": width, "tilt": 0}
                   for p, width in [((0, 0, 0), 4), ((10, 0, 0), 8), ((10, 10, 0), 12)]]
        rebased = self.api.rebase_closed_route(samples, Vector((5, 0, 0)))
        self.assertEqual(rebased[0]["width"], 6)
        self.assertEqual(rebased[0]["position"], (5, 0, 0))
        self.assertEqual([s["width"] for s in rebased[1:]], [8, 12, 4])

    def test_route_width_query_interpolates_open_endpoints_and_closed_seam(self):
        route = {"closed": False, "length": 10,
                 "samples": [{"s": 0, "width": 4}, {"s": 10, "width": 8}]}
        self.assertEqual(self.api.route_width_at(route, 5), 6)
        self.assertEqual(self.api.route_width_at(route, 10), 8)
        route.update(closed=True, length=20)
        self.assertEqual(self.api.route_width_at(route, 15), 6)
        self.assertEqual(self.api.route_width_at(route, 20), 4)

    def test_invalid_widths_scale_and_custom_profiles_are_rejected(self):
        for value in (0, -1, math.nan, math.inf):
            curve = self.curve()
            curve.data.bevel_depth = value
            with self.subTest(depth=value), self.assertRaises(ValueError):
                self.api.adaptive_route_samples(curve, True)
        for property_name, value in (("bevel_mode", "OBJECT"), ("taper_object", object()),
                                     ("extrude", 1), ("bevel_factor_end", 0.5)):
            curve = self.curve()
            setattr(curve.data, property_name, value)
            with self.subTest(property=property_name), self.assertRaises(ValueError):
                self.api.adaptive_route_samples(curve, True)
        curve = self.curve()
        curve.matrix_world.col[0] = Vector((2, 0, 0))
        with self.assertRaisesRegex(ValueError, "scale 1"):
            self.api.adaptive_route_samples(curve, True)
        with self.assertRaisesRegex(ValueError, "positive finite width"):
            self.api.adaptive_route_samples(self.curve((20, 0.1, 0.1, 20), interpolation="CARDINAL", spacing=1), True)


if __name__ == "__main__":
    unittest.main()
