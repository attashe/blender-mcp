"""Run with blender --background --factory-startup --python-exit-code 1 --python THIS_FILE."""

import base64
import runpy
from pathlib import Path
from tempfile import TemporaryDirectory

import bpy


look = runpy.run_path(str(Path(__file__).resolve().parents[1] / "addon.py"))["look"]


def image_state(image):
    return (list(image.size), image.is_dirty, image.filepath, image.file_format,
            image.colorspace_settings.name, image.pixels[:4],
            image.packed_file.data if image.packed_file else None)


def check_capture(image, directory):
    source = image_state(image)
    images, scenes = bpy.data.images.keys(), bpy.data.scenes.keys()
    encoded = look(image=image.name, max_size=64)
    assert (encoded["width"], encoded["height"], encoded["mime_type"]) == (64, 32, "image/png")
    png = base64.b64decode(encoded["image"], validate=True)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert image_state(image) == source
    assert bpy.data.images.keys() == images and bpy.data.scenes.keys() == scenes

    path = directory / "received.png"
    path.write_bytes(png)
    received = bpy.data.images.load(str(path), check_existing=False)
    try:
        assert tuple(received.size) == (64, 32)
        assert all(abs(a - b) <= 2 / 255 for a, b in zip(received.pixels[:4], source[5]))
    finally:
        bpy.data.images.remove(received)


def expect_error(**params):
    try:
        look(**params)
    except ValueError:
        return
    raise AssertionError(f"Expected an image selection error for {params}")


def check():
    scene = bpy.context.scene
    scene.view_settings.exposure = 2.0
    settings = scene.render.image_settings
    settings.file_format = "OPEN_EXR"
    settings.color_management = "OVERRIDE"
    settings.view_settings.view_transform = "AgX"
    settings.view_settings.exposure = 1.0

    def output_settings():
        return (settings.file_format, settings.color_mode, settings.color_depth, settings.exr_codec,
                settings.color_management, settings.view_settings.view_transform, settings.view_settings.exposure)

    opts = output_settings()
    with TemporaryDirectory(prefix="blender-mcp-image-test-") as tmp:
        directory = Path(tmp)
        generated = bpy.data.images.new("Unsaved baked color", 128, 64, alpha=True)
        generated.pixels[:] = [0.25, 0.5, 0.75, 1.0] * (128 * 64)
        generated.update()
        assert generated.is_dirty
        check_capture(generated, directory)

        srcpath = directory / "pristine.png"
        generated.file_format = "PNG"
        generated.save(filepath=str(srcpath))
        original = srcpath.read_bytes()
        packed = bpy.data.images.load(str(srcpath), check_existing=False)
        packed.pack()
        packed.colorspace_settings.name = "Non-Color"
        packed.pixels[:] = [0.8, 0.2, 0.1, 1.0] * (128 * 64)
        packed.update()
        assert packed.is_dirty and packed.packed_file
        check_capture(packed, directory)
        assert srcpath.read_bytes() == original
        data = bpy.data.images.new("Baked float data", 128, 64, alpha=True, float_buffer=True)
        data.colorspace_settings.name = "Non-Color"
        data.pixels[:] = [0.125, 0.5, 0.875, 1.0] * (128 * 64)
        data.update()
        check_capture(data, directory)
        assert output_settings() == opts
        assert scene.view_settings.exposure == 2.0
        for kw in ({"view": "camera"}, {"target": "Cube"}, {"shading": "solid"}):
            expect_error(image=packed.name, **kw)
        expect_error(image=str(srcpath))
        expect_error(image=bpy.data.images.new("UDIM", 64, 64, tiled=True).name)

        scene.render.engine = "CYCLES"
        scene.cycles.device = "CPU"
        scene.cycles.samples = 1
        scene.render.resolution_x, scene.render.resolution_y = 96, 48
        scene.render.resolution_percentage = 100
        bpy.ops.render.render()
        results = [look(view="render_result", max_size=64), look(image="Render Result", max_size=64)]
        assert all((r["width"], r["height"]) == (64, 32) for r in results)
        pixels = []
        for r in results:
            path = directory / "rendered.png"
            path.write_bytes(base64.b64decode(r["image"], validate=True))
            restored = bpy.data.images.load(str(path), check_existing=False)
            try:
                pixels.append(restored.pixels[:])
            finally:
                bpy.data.images.remove(restored)
        assert pixels[0] == pixels[1]
        assert output_settings() == opts
    print(f"Blender {bpy.app.version_string}: verified generated, packed, float, Render Result and preserved color settings")


if __name__ == "__main__":
    check()
