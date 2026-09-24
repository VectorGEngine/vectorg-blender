"""Manifest float formatting tests; no Blender required."""
import ast
import json
import math
from pathlib import Path
import struct
import unittest


ROOT = Path(__file__).resolve().parents[1]
ADDONS = {
    "car": ROOT / "addons/vectorg_car_exporter/__init__.py",
    "track": ROOT / "addons/vectorg_track_exporter/__init__.py",
}
FUNCTIONS = {"shortest_float32", "compact_manifest_floats"}


def float32(value):
    return struct.unpack("<f", struct.pack("<f", value))[0]


def load_api(path):
    source = ast.parse(path.read_text(encoding="utf-8"))
    nodes = [node for node in source.body if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS]
    namespace = {"struct": struct}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


class ManifestFloatTests(unittest.TestCase):
    def test_blender_float_properties_use_shortest_decimal(self):
        for name, path in ADDONS.items():
            compact = load_api(path)["compact_manifest_floats"]
            with self.subTest(addon=name):
                manifest = {"grip": float32(0.666), "values": [float32(0.1), float32(-12.34)], "count": 3}
                self.assertEqual(
                    json.dumps(compact(manifest)),
                    '{"grip": 0.666, "values": [0.1, -12.34], "count": 3}',
                )

    def test_float32_value_is_preserved(self):
        for name, path in ADDONS.items():
            shortest = load_api(path)["shortest_float32"]
            with self.subTest(addon=name):
                for bits in range(0, 0x7F800000, 0x7F800000 // 20011):
                    single = struct.unpack("<f", struct.pack("<I", bits))[0]
                    for value in (single, -single):
                        compacted = shortest(value)
                        self.assertEqual(float32(compacted), value)
                        self.assertLessEqual(len(repr(compacted)), len(repr(value)))

    def test_non_float32_values_are_unchanged(self):
        for name, path in ADDONS.items():
            api = load_api(path)
            with self.subTest(addon=name):
                for value in (0.1, 1 / 3, round(math.pi, 6), 1e300, True, 7, "0.6660000085830688", None):
                    result = api["compact_manifest_floats"](value)
                    self.assertEqual(result, value)
                    self.assertIs(type(result), type(value))
                self.assertTrue(math.isnan(api["shortest_float32"](math.nan)))
                self.assertEqual(api["shortest_float32"](math.inf), math.inf)
                self.assertEqual(math.copysign(1, api["shortest_float32"](-0.0)), -1)


if __name__ == "__main__":
    unittest.main()
