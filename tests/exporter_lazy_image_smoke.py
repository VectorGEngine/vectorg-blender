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


def exercise_file_image(exporter, directory, prefix, mode):
    source_path = directory / f"{prefix}_source.png"
    source = bpy.data.images.new(f"{prefix}_source", width=8, height=4, alpha=True)
    source.filepath_raw = str(source_path)
    source.file_format = "PNG"
    source.save()
    if mode == "packed":
        source.pack()
        source_path.unlink()
        assert source.packed_file is not None
        assert source.has_data
    elif mode == "missing":
        source_path.unlink()
        assert source.packed_file is None
        assert source.has_data
    else:
        bpy.data.images.remove(source)
        source = bpy.data.images.load(str(source_path), check_existing=False)
        assert not source.has_data

    duplicate = exporter.duplicate_image_with_data(source)
    try:
        assert duplicate.has_data
        if mode == "missing":
            assert duplicate.source == "GENERATED"
    finally:
        bpy.data.images.remove(duplicate)

    replacement = exporter.optimized_export_image(
        source,
        {"color"},
        4,
        True,
        directory,
        0,
        85,
    )
    try:
        assert replacement is not None
        assert tuple(replacement.size) == (4, 2)
        assert replacement.file_format == "JPEG"
    finally:
        temporary_file = replacement.get(exporter.TEMP_IMAGE_FILE_PROPERTY)
        bpy.data.images.remove(replacement)
        bpy.data.images.remove(source)
        if temporary_file:
            Path(temporary_file).unlink(missing_ok=True)


track = load_module(
    "vectorg_track_exporter_lazy_image_smoke",
    "addons/vectorg_track_exporter/__init__.py",
)
car = load_module(
    "vectorg_car_exporter_lazy_image_smoke",
    "addons/vectorg_car_exporter/__init__.py",
)

with tempfile.TemporaryDirectory(prefix="vectorg_lazy_image_smoke_") as directory:
    directory = Path(directory)
    for exporter, prefix in ((car, "car"), (track, "track")):
        for mode in ("lazy", "packed", "missing"):
            exercise_file_image(exporter, directory, f"{prefix}_{mode}", mode)

print("EXPORTER_LAZY_IMAGE_SMOKE_OK")
