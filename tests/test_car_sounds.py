"""Sound-slot manifest round trips using exporter code; no Blender required."""
import ast
import math
import re
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest


ADDON = Path(__file__).resolve().parents[1] / "addons/vectorg_car_exporter/__init__.py"
NEW_SLOTS = ("idle", "on_mid", "off_mid", "engine_start", "gear_grinding", "brake_squeal")
SOUND_HELPERS = {
    "sound_sample_path", "sound_export_name", "validate_sound_sample",
    "export_sound_samples", "load_manifest_sound_samples",
    "sound_reference_rpm",
}


def sound_datablock(filepath, packed=None, library=None):
    return SimpleNamespace(
        name=Path(filepath).name, filepath=str(filepath), library=library,
        packed_file=None if packed is None else SimpleNamespace(data=packed, size=len(packed)),
    )


class CarSoundTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(ADDON.read_text(encoding="utf-8"))
        cls.constants = {}
        for node in cls.tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                if node.targets[0].id in ("SOUND_SLOTS", "SOUND_RPM_SLOTS"):
                    cls.constants[node.targets[0].id] = ast.literal_eval(node.value)
        settings_class = next(node for node in cls.tree.body
                              if isinstance(node, ast.ClassDef) and node.name == "CarExporterSettings")
        cls.defaults = {}
        cls.pointers = []
        for node in settings_class.body:
            if isinstance(node, ast.AnnAssign) and node.target.id.startswith("sound_"):
                if node.annotation.func.id == "PointerProperty":
                    cls.pointers.append(node)
                    cls.defaults[node.target.id] = None
                    continue
                default = next(keyword.value for keyword in node.annotation.keywords
                               if keyword.arg == "default")
                cls.defaults[node.target.id] = ast.literal_eval(default)
        build = next(node for node in cls.tree.body
                     if isinstance(node, ast.FunctionDef) and node.name == "build_manifest")
        # Execute the actual sound export block without unrelated Blender geometry work.
        cls.export_nodes = build.body[:2]
        cls.import_loop = next(node for node in ast.walk(cls.tree)
                               if isinstance(node, ast.For)
                               and ast.unparse(node.iter) == "SOUND_SLOTS.items()"
                               and "sounds.get(slot)" in ast.unparse(node))
        cls.import_validation = next(node for node in ast.walk(cls.tree)
                                     if isinstance(node, ast.For) and ast.unparse(node.iter) == "SOUND_SLOTS"
                                     and "Manifest sounds." in ast.unparse(node))
        cls.helpers = [node for node in cls.tree.body
                       if isinstance(node, ast.FunctionDef) and node.name in SOUND_HELPERS]

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.sound_dir = self.root / "sounds"
        self.sound_dir.mkdir()
        self.loaded = {}
        self.bpy = SimpleNamespace(
            path=SimpleNamespace(abspath=self.abspath),
            data=SimpleNamespace(sounds=SimpleNamespace(load=self.load_sound)),
        )

    def abspath(self, filepath, library=None):
        if filepath.startswith("//"):
            root = Path(library.filepath).parent if library else self.root
            return str(root / filepath[2:])
        return filepath

    def load_sound(self, filepath, check_existing=False):
        self.assertTrue(check_existing)
        self.assertTrue(Path(filepath).is_file())
        if filepath not in self.loaded:
            self.loaded[filepath] = sound_datablock(filepath)
        return self.loaded[filepath]

    def execute(self, nodes, **values):
        namespace = {**self.constants, "Path": Path, "math": math, "re": re, "shutil": shutil, "bpy": self.bpy, **values}
        module = ast.Module(body=self.helpers + nodes, type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), str(ADDON), "exec"), namespace)
        return namespace

    def settings(self):
        return SimpleNamespace(**self.defaults, use_custom_sounds=True)

    def export(self, settings):
        return self.execute(self.export_nodes, settings=settings)["sounds"]

    def validate_import(self, sounds):
        function = ast.FunctionDef(
            name="validate", args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
            body=[self.import_validation], decorator_list=[],
        )
        errors = []
        namespace = self.execute([function], sounds=sounds)
        namespace["self"] = SimpleNamespace(report=lambda _, message: errors.append(message))
        return namespace["validate"](), errors

    def test_unassigned_optional_slots_are_omitted(self):
        self.assertEqual(self.export(self.settings()), {"pitchOffset": 0})

    def test_engine_reference_rpm_must_be_positive(self):
        for slot in self.constants["SOUND_RPM_SLOTS"]:
            for rpm in [0, -1, float("nan"), float("inf"), True]:
                with self.subTest(slot=slot, rpm=rpm):
                    settings = self.settings()
                    setattr(settings, f"sound_{slot}", sound_datablock("sample.wav", packed=b"audio"))
                    setattr(settings, f"sound_{slot}_rpm", rpm)
                    with self.assertRaisesRegex(ValueError, "reference RPM must be a finite positive number"):
                        self.export(settings)

    def test_import_rejects_zero_reference_rpm_for_every_engine_slot(self):
        for slot in self.constants["SOUND_SLOTS"]:
            result, errors = self.validate_import({slot: {"source": "sample.wav", "rpm": 0}})
            if slot in self.constants["SOUND_RPM_SLOTS"]:
                self.assertEqual(result, {"CANCELLED"})
                self.assertIn("must be positive", errors[0])
            else:
                self.assertIsNone(result)

    def test_import_ignores_legacy_loop_flags(self):
        for slot in self.constants["SOUND_SLOTS"]:
            for loop in [True, False, "invalid"]:
                with self.subTest(slot=slot, loop=loop):
                    self.assertEqual(
                        self.validate_import({slot: {"source": "sample.wav", "rpm": 1000, "loop": loop}}),
                        (None, []),
                    )

    def test_slot_order_and_sound_datablock_inputs(self):
        self.assertEqual(list(self.constants["SOUND_SLOTS"]), [
            "idle", "off_low", "off_mid", "off_high", "on_low", "on_mid", "on_high",
            "tranny_off", "tranny_on", "limiter", "turbo",
            "engine_start", "gear_grinding", "brake_squeal",
        ])
        self.assertEqual({node.target.id for node in self.pointers},
                         {f"sound_{slot}" for slot in self.constants["SOUND_SLOTS"]})
        for node in self.pointers:
            pointer_type = next(keyword.value for keyword in node.annotation.keywords if keyword.arg == "type")
            self.assertEqual(ast.unparse(pointer_type), "bpy.types.Sound")
        for slot, meta in self.constants["SOUND_SLOTS"].items():
            self.assertNotIn("loop", meta)
            self.assertEqual(self.defaults[f"sound_{slot}_enabled"], True)
            self.assertEqual(self.defaults[f"sound_{slot}_volume"], meta["volume"])
            self.assertEqual(f"sound_{slot}_rpm" in self.defaults, slot in self.constants["SOUND_RPM_SLOTS"])

    def test_new_slots_round_trip_custom_omitted_and_disabled(self):
        for slot in NEW_SLOTS:
            for mode in ("custom", "omitted", "disabled"):
                with self.subTest(slot=slot, mode=mode):
                    settings = self.settings()
                    if mode != "omitted":
                        setattr(settings, f"sound_{slot}", sound_datablock(f"//{slot}.wav", packed=b"audio"))
                    setattr(settings, f"sound_{slot}_enabled", mode != "disabled")
                    if slot in self.constants["SOUND_RPM_SLOTS"]:
                        setattr(settings, f"sound_{slot}_rpm", 3200)
                    setattr(settings, f"sound_{slot}_volume", 0.65)
                    sounds = self.export(settings)
                    if mode == "custom":
                        rpm = 3200 if slot in self.constants["SOUND_RPM_SLOTS"] else self.constants["SOUND_SLOTS"][slot]["rpm"]
                        self.assertEqual(sounds[slot], {"source": f"{slot}.wav", "rpm": rpm, "volume": 0.65})
                    elif mode == "disabled":
                        self.assertIsNone(sounds[slot])
                    else:
                        self.assertNotIn(slot, sounds)
                    imported = self.settings()
                    setattr(imported, f"sound_{slot}", sound_datablock("stale.wav"))
                    namespace = self.execute([])
                    namespace["export_sound_samples"](settings, self.sound_dir)
                    loaded = namespace["load_manifest_sound_samples"](sounds, self.root / "manifest.json")
                    self.execute([self.import_loop], settings=imported, sounds=sounds,
                                 loaded_sounds=loaded)
                    self.assertEqual(getattr(imported, f"sound_{slot}_enabled"), mode != "disabled")
                    self.assertIs(getattr(imported, f"sound_{slot}"), loaded.get(slot))
                    self.assertEqual(self.export(imported), sounds)

    def test_custom_sound_switch_still_omits_all_slots_when_disabled(self):
        settings = self.settings()
        settings.use_custom_sounds = False
        for slot in NEW_SLOTS:
            setattr(settings, f"sound_{slot}", sound_datablock(f"{slot}.wav"))
        self.assertEqual(self.export(settings), {"pitchOffset": 0})

    def test_reset_restores_new_slot_defaults(self):
        reset = next(node for node in self.tree.body
                     if isinstance(node, ast.FunctionDef) and node.name == "clear_configuration_settings")
        reset_loop = next(node for node in reset.body if isinstance(node, ast.For)
                          and ast.unparse(node.iter) == "SOUND_SLOTS.items()")
        settings = self.settings()
        for slot in NEW_SLOTS:
            setattr(settings, f"sound_{slot}", sound_datablock("custom.wav"))
            setattr(settings, f"sound_{slot}_enabled", False)
            if slot in self.constants["SOUND_RPM_SLOTS"]:
                setattr(settings, f"sound_{slot}_rpm", 5000)
            setattr(settings, f"sound_{slot}_volume", 0.9)
        self.execute([reset_loop], settings=settings)
        for slot in NEW_SLOTS:
            suffixes = ("", "_enabled", "_rpm", "_volume") if slot in self.constants["SOUND_RPM_SLOTS"] \
                else ("", "_enabled", "_volume")
            for suffix in suffixes:
                key = f"sound_{slot}{suffix}"
                self.assertEqual(getattr(settings, key), self.defaults[key])

    def test_packed_audio_exports_without_its_original_file(self):
        settings = self.settings()
        settings.sound_idle = sound_datablock("//missing/engine.wav", packed=b"packed\x00audio")
        self.execute([])["export_sound_samples"](settings, self.sound_dir)
        self.assertEqual((self.sound_dir / self.export(settings)["idle"]["source"]).read_bytes(), b"packed\x00audio")

    def test_extensionless_packed_audio_exports_with_its_container_extension(self):
        for header, extension in [
            (b"RIFF\x00\x00\x00\x00WAVEfmt ", ".wav"),
            (b"RF64\xff\xff\xff\xffWAVEfmt ", ".wav"),
            (b"OggS", ".ogg"), (b"fLaC", ".flac"),
            (b"FORM\x00\x00\x00\x00AIFF", ".aiff"), (b"ID3", ".mp3"),
        ]:
            settings = self.settings()
            settings.sound_on_high = sound_datablock("//on_high", packed=header)
            self.execute([])["export_sound_samples"](settings, self.sound_dir)
            filename = self.export(settings)["on_high"]["source"]
            self.assertEqual(filename, "on_high" + extension)
            self.assertEqual((self.sound_dir / filename).read_bytes(), header)

    def test_extensionless_unpacked_wav_and_datablock_names_keep_extensions(self):
        source = self.root / "on_high"
        source.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")
        filename = self.execute([])["sound_export_name"]
        self.assertEqual(filename(sound_datablock("//engine.wav"), "idle"), "idle.wav")
        self.assertEqual(filename(sound_datablock(str(source)), "on_high"), "on_high.wav")
        sound = sound_datablock("", packed=b"audio")
        sound.name = "Engine.m4a.001"
        self.assertEqual(filename(sound, "idle"), "idle.m4a")

    def test_unknown_extensionless_audio_is_not_exported_under_a_guessed_extension(self):
        with self.assertRaisesRegex(ValueError, "Cannot determine audio extension"):
            self.execute([])["sound_export_name"](sound_datablock("", packed=b"unknown"), "idle")

    def test_unpacked_and_linked_sounds_resolve_their_own_paths(self):
        (self.root / "engine.wav").write_bytes(b"local")
        library_dir = self.root / "library"
        library_dir.mkdir()
        (library_dir / "engine.wav").write_bytes(b"linked")
        settings = self.settings()
        settings.sound_on_low = sound_datablock("//engine.wav")
        settings.sound_off_low = sound_datablock("//engine.wav", library=SimpleNamespace(filepath=str(library_dir / "car.blend")))
        self.execute([])["export_sound_samples"](settings, self.sound_dir)
        # Same source filename must not overwrite a different slot's recording.
        self.assertEqual((self.sound_dir / "on_low.wav").read_bytes(), b"local")
        self.assertEqual((self.sound_dir / "off_low.wav").read_bytes(), b"linked")

    def test_missing_unpacked_and_empty_packed_sounds_are_rejected(self):
        validate = self.execute([])["validate_sound_sample"]
        for sound in [sound_datablock("//missing.wav"), sound_datablock(""), sound_datablock("//empty.wav", packed=b"")]:
            with self.assertRaises(ValueError):
                validate(sound)

    def test_disabled_sound_is_not_validated_or_exported(self):
        settings = self.settings()
        settings.sound_idle = sound_datablock("//missing.wav")
        settings.sound_idle_enabled = False
        self.execute([])["export_sound_samples"](settings, self.sound_dir)
        self.assertEqual(list(self.sound_dir.iterdir()), [])
        self.assertIsNone(self.export(settings)["idle"])

    def test_all_slot_files_match_the_exported_manifest(self):
        settings = self.settings()
        for slot in self.constants["SOUND_SLOTS"]:
            setattr(settings, f"sound_{slot}", sound_datablock("//same-name.ogg", packed=slot.encode()))
        self.execute([])["export_sound_samples"](settings, self.sound_dir)
        sounds = self.export(settings)
        for slot in self.constants["SOUND_SLOTS"]:
            self.assertEqual((self.sound_dir / sounds[slot]["source"]).read_bytes(), slot.encode())

    def test_import_preflights_paths_before_loading_any_datablocks(self):
        (self.sound_dir / "valid.wav").write_bytes(b"audio")
        (self.root / "outside.wav").write_bytes(b"outside")
        load = self.execute([])["load_manifest_sound_samples"]
        for invalid in ["../outside.wav", "missing.wav", str(self.root / "outside.wav")]:
            with self.assertRaises(ValueError):
                load({"tranny_off": {"source": "valid.wav"}, "idle": {"source": invalid}}, self.root / "manifest.json")
            self.assertEqual(self.loaded, {})

    def test_import_reports_audio_decode_failure(self):
        (self.sound_dir / "invalid.wav").write_bytes(b"invalid")

        def fail_load(*args, **kwargs):
            raise RuntimeError("Unsupported audio")

        self.bpy.data.sounds.load = fail_load
        with self.assertRaisesRegex(ValueError, "Unable to load manifest sound idle"):
            self.execute([])["load_manifest_sound_samples"]({"idle": {"source": "invalid.wav"}}, self.root / "manifest.json")


if __name__ == "__main__":
    unittest.main()
