"""Run the fork's MCP client → stdio server → Blender add-on loop on a disposable scene."""

import argparse
import asyncio
import base64
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from blender_mcp.addon_manager import install_addon


MAKE_HORN = """
curve = bpy.data.curves.new("Horn path", "CURVE")
curve.dimensions = "3D"
curve.bevel_depth = 0.32
curve.bevel_resolution = 4
curve.use_fill_caps = True
spline = curve.splines.new("BEZIER")
spline.bezier_points.add(3)
controls = [((0, 0, 0), 1), ((0.05, 0, 0.8), 0.85), ((0.5, 0.15, 1.5), 0.5), ((1.1, 0.1, 1.8), 0.06)]
for point, (position, radius) in zip(spline.bezier_points, controls):
    point.co = position
    point.radius = radius
    point.handle_left_type = point.handle_right_type = "AUTO"
horn = bpy.data.objects.new("Horn", curve)
bpy.context.collection.objects.link(horn)
print(horn.name)
"""


async def check(port, root, images):
    server = StdioServerParameters(command=sys.executable, args=["-m", "blender_mcp.server", "--port", str(port)])
    async with stdio_client(server) as streams, ClientSession(*streams) as client:
        await client.initialize()
        assert {tool.name for tool in (await client.list_tools()).tools} == {"get_scene_info", "execute_blender_code", "look"}

        async def call(tool, **params):
            reply = await client.call_tool(tool, params)
            assert not reply.isError, reply.content
            return reply.content[0]

        async def scene(**params):
            return json.loads((await call("get_scene_info", **params)).text)

        async def picture(label):
            image = await call("look", view="front", target="Horn", shading="solid", max_size=384)
            assert image.type == "image" and image.mimeType == "image/png"
            png = base64.b64decode(image.data, validate=True)
            assert png.startswith(b"\x89PNG\r\n\x1a\n")
            assert max(struct.unpack(">II", png[16:24])) <= 384
            if images:
                images.mkdir(parents=True, exist_ok=True)
                (images / f"{label}.png").write_bytes(png)
            return png

        initial = await scene()
        assert not initial["file"] and initial["mode"] == "OBJECT"
        made = await call("execute_blender_code", code=MAKE_HORN)
        assert json.loads(made.text)["stdout"].strip() == "Horn"
        first = await picture("before")
        first_state = await scene(objects=["Horn"], fields=["dimensions"])
        assert first_state["objects"][0]["type"] == "CURVE"
        saved = root / "edited.blend"
        await call("execute_blender_code", code=(
            'bpy.data.curves["Horn path"].splines[0].bezier_points[-1].co.x += 0.8\n'
            f'bpy.ops.wm.save_as_mainfile(filepath={str(saved)!r})'
        ))
        second = await picture("after")
        assert first != second, "moving the tip must change its silhouette"
        error = await client.call_tool("execute_blender_code", {"code": "print('started')\nraise SystemExit(7)"})
        assert error.isError
        details = json.loads(error.content[0].text)
        assert details["stdout"] == "started\n" and details["error_type"] == "SystemExit"
        assert "raise SystemExit(7)" in details["traceback"]
        final = await scene(objects=["Horn"], fields=["dimensions"])
        assert Path(final["file"]).resolve() == saved.resolve() and saved.is_file()
        before = first_state["objects"][0]["dimensions"][0]
        after = final["objects"][0]["dimensions"][0]
        assert after > before + 0.3
        print(f'Blender {initial["blender_version"]}: three tools, two PNGs, width {before:.3f} to {after:.3f}, saved file, live after SystemExit.')


def run(blender, root, images):
    scripts = root / "scripts"
    install_addon(scripts / "addons")
    startup = root / "startup.py"
    ready = root / "ready"
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    startup.write_text(textwrap.dedent(f"""
        import addon_utils, bpy
        from pathlib import Path
        assert addon_utils.enable("blender_mcp_minimal", default_set=False) is not None
        bpy.context.window_manager.blendermcp_minimal_port = {port}
        assert bpy.ops.blendermcp_minimal.start_server() == {{"FINISHED"}}
        Path({str(ready)!r}).touch()
    """), encoding="utf-8")
    env = dict(os.environ, BLENDER_USER_SCRIPTS=str(scripts), BLENDER_USER_CONFIG=str(root / "config"),
               BLENDER_USER_EXTENSIONS=str(root / "extensions"), BLENDER_USER_RESOURCES=str(root))
    log = root / "blender.log"
    with log.open("wb") as stream:
        proc = subprocess.Popen([str(blender), "--factory-startup", "--disable-autoexec",
                                 "--window-geometry", "0", "0", "800", "600",
                                 "--python-exit-code", "1", "--python", str(startup)],
                                env=env, stdout=stream, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 90
            while not ready.exists():
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError(log.read_text(encoding="utf-8", errors="replace")[-3000:])
                time.sleep(0.2)
            asyncio.run(check(port, root, images))
        finally:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=15)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("blender", type=Path, help="Path to the Blender executable")
    parser.add_argument("--images", type=Path, help="Keep the before/after PNGs in this directory")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="blender-mcp-smoke-") as temp:
        run(args.blender, Path(temp), args.images)
