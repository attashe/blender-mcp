# Derived from the BlenderMCP add-on by Siddharth Ahuja. See LICENSE.
import io
import json
import linecache
import queue
import select
import socket
import threading
import traceback
from contextlib import redirect_stderr, redirect_stdout, suppress
from itertools import islice

import bmesh
import bpy
import mathutils


bl_info = {
    "name": "Blender MCP (Minimal)",
    "author": "Siddharth Ahuja, attashe",
    "version": (2, 3, 1, 3),
    "blender": (4, 3, 0),
    "location": "View3D > Sidebar > BlenderMCP",
    "description": "Execute Python, inspect the scene and capture a view over localhost",
    "category": "Interface",
}

OBJECT_FIELDS = {"name", "type", "location", "rotation", "scale", "dimensions", "world_bounds", "materials", "collections"}
OUTPUT_LIMIT = 16_384


class _Output(io.StringIO):
    """A print stream bounded during writes, including a single very large print."""

    truncated = False

    def write(self, text):
        remaining = max(0, OUTPUT_LIMIT - self.tell())
        self.truncated |= len(text) > remaining
        super().write(text[:remaining])
        return len(text)

    def captured(self):
        return self.getvalue() + ("\n[output truncated]" if self.truncated else "")


def _error(exc):
    return {
        "status": "error",
        "error_type": type(exc).__name__,
        "message": str(exc)[:OUTPUT_LIMIT],
        "traceback": traceback.format_exc()[-OUTPUT_LIMIT:],
    }


def execute_code(code):
    stdout, stderr = _Output(), _Output()
    namespace = {"__name__": "__main__", "bpy": bpy, "bmesh": bmesh, "mathutils": mathutils}
    filename = "<blender-mcp>"
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            linecache.cache[filename] = (len(code), None, code.splitlines(True), filename)
            exec(compile(code, filename, "exec"), namespace)
    except BaseException as exc:
        # SystemExit from argparse/sys.exit must not tear down Blender's event loop.
        reply = _error(exc)
        reply.update(stdout=stdout.captured(), stderr=stderr.captured())
        return reply
    finally:
        linecache.cache.pop(filename, None)

    return {"status": "success", "result": {"stdout": stdout.captured(), "stderr": stderr.captured()}}


def _world_bounds(obj, depsgraph):
    if obj.data is None or obj.type in {"CAMERA", "LIGHT", "LIGHT_PROBE", "SPEAKER"}:
        return None
    evaluated = obj.evaluated_get(depsgraph)
    corners = [tuple(c) for c in evaluated.bound_box]
    if not corners:
        return None
    if corners[0] in {(0, 0, 0), (-1, -1, -1)} and all(c == corners[0] for c in corners):
        # Empty geometry and a real point at this coordinate can have identical boxes.
        elements = {"MESH": "vertices", "CURVE": "splines", "SURFACE": "splines", "CURVES": "points",
                    "FONT": "body", "META": "elements", "POINTCLOUD": "points", "ARMATURE": "bones"}
        if not getattr(evaluated.data, elements.get(evaluated.type, ""), None):
            return None
    points = [evaluated.matrix_world @ mathutils.Vector(c) for c in corners]
    return {"min": [min(axis) for axis in zip(*points)], "max": [max(axis) for axis in zip(*points)]}


def _object_info(obj, fields, depsgraph):
    data = {"name": obj.name, "type": obj.type}
    for field in fields & {"location", "scale", "dimensions"}:
        data[field] = list(getattr(obj, field))
    if "rotation" in fields:
        attr = {"AXIS_ANGLE": "rotation_axis_angle", "QUATERNION": "rotation_quaternion"}
        values = getattr(obj, attr.get(obj.rotation_mode, "rotation_euler"))
        data["rotation"] = {"mode": obj.rotation_mode, "value": list(values)}
    if "materials" in fields:
        data["materials"] = [slot.material.name if slot.material else None for slot in obj.material_slots]
    if "collections" in fields:
        data["collections"] = [coll.name for coll in obj.users_collection]
    if "world_bounds" in fields:
        data["world_bounds"] = _world_bounds(obj, depsgraph)
    return data


def get_scene_info(objects=None, fields=None, limit=20, query=None):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer between 1 and 100")
    fields = set(fields or ())
    unknown = fields - OBJECT_FIELDS
    if unknown:
        raise ValueError("Unknown object fields: " + ", ".join(sorted(unknown)))

    context = bpy.context
    scene = context.scene
    missing = []
    if objects is None:
        matching = scene.objects
    else:
        if not isinstance(objects, list) or len(objects) > 100:
            raise ValueError("objects must be a list of at most 100 exact names")
        matching = []
        for name in dict.fromkeys(objects):
            obj = scene.objects.get(name)
            if obj is None:
                missing.append(name)
            else:
                matching.append(obj)

    if query:
        substring = query.strip().casefold()
        matching = (obj for obj in matching if substring in obj.name.casefold())
    # One extra match tells the caller whether there's more, even in a large scene.
    page = list(islice(matching, limit + 1))
    depsgraph = context.evaluated_depsgraph_get() if page and "world_bounds" in fields else None
    selected = context.selected_objects
    collections = scene.collection.children_recursive
    active = context.view_layer.objects.active
    units = scene.unit_settings
    return {
        "file": bpy.data.filepath,
        "blender_version": bpy.app.version_string,
        "scene": scene.name,
        "mode": context.mode,
        "units": {"system": units.system, "length_unit": units.length_unit, "scale_length": units.scale_length},
        "active_object": active.name if active else None,
        "selected_objects": [obj.name for obj in islice(selected, limit)],
        "selected_count": len(selected),
        "collections": [coll.name for coll in islice(collections, limit)],
        "collection_count": len(collections),
        "object_count": len(scene.objects),
        "objects": [_object_info(obj, fields, depsgraph) for obj in page[:limit]],
        "objects_truncated": len(page) > limit,
        "missing_objects": missing,
    }


def _preview_size(width, height, limit):
    if width <= 0 or height <= 0:
        raise ValueError("The requested view has no pixels")
    scale = min(1.0, limit / max(width, height))
    return max(1, round(width * scale)), max(1, round(height * scale))


def _preview_png(width, height, rgba):
    """Encode the offscreen's display-referred bytes without a second color transform."""
    import struct
    import zlib

    def chunk(kind, payload):
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload)))

    stride = width * 4
    # GPU rows start at the bottom. Each PNG scanline begins with filter type zero.
    rows = b"".join(b"\0" + rgba[y * stride:(y + 1) * stride]
                    for y in reversed(range(height)))
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(rows, 3)) + chunk(b"IEND", b""))


def _preview_image(name, max_size):
    """Encode the current pixel buffer, including unsaved edits to packed images."""
    from contextlib import ExitStack
    from pathlib import Path
    from tempfile import TemporaryDirectory

    source = bpy.data.images.get(name)
    if source is None:
        if name == "Render Result":
            raise ValueError("No Render Result is available. Render a frame first.")
        raise ValueError(f"No Blender image datablock named {name!r}")
    if source.is_multiview or (source.type != "RENDER_RESULT" and
                               (source.source not in {"GENERATED", "FILE"} or
                                source.type not in {"IMAGE", "UV_TEST"})):
        raise ValueError(f"{name!r} needs a single still image; got {source.source}/{source.type}")
    if source.type != "RENDER_RESULT" and min(source.size) < 1:
        raise ValueError(f"No pixels available for image {name!r}")

    scene = bpy.context.scene
    with TemporaryDirectory(prefix="blendermcp-look-") as directory, ExitStack() as cleanup:
        if source.type != "RENDER_RESULT" and not (source.is_float or source.use_view_as_render):
            # Byte textures use standard display conversion independently of shot exposure.
            # save_render also respects non-color inputs such as baked normal maps.
            scene = bpy.data.scenes.new("BlenderMCP image encoding")
            cleanup.callback(bpy.data.scenes.remove, scene)
            scene.view_settings.view_transform = "Standard"
            scene.render.dither_intensity = 0.0
        settings = scene.render.image_settings
        options = (settings.file_format, settings.color_mode, settings.color_depth)
        path = Path(directory) / "frame.png"
        try:
            settings.file_format = "PNG"
            settings.color_mode = "RGBA"
            settings.color_depth = "8"
            # Image.copy() may reconstruct the old file instead of copying dirty pixels.
            source.save_render(str(path), scene=scene)
        finally:
            # Restore format before its dependent enum values.
            settings.file_format, settings.color_mode, settings.color_depth = options
        image = bpy.data.images.load(str(path), check_existing=False)
        try:
            width, height = _preview_size(*image.size, max_size)
            if (width, height) != tuple(image.size):
                image.scale(width, height)
                image.save()
            return width, height, path.read_bytes()
        finally:
            bpy.data.images.remove(image)


def _preview_frame(objects, rotation, aspect, depsgraph):
    from mathutils import Matrix, Vector

    # Measure the evaluated boxes in camera space so even a tall, flat profile fits.
    inverse_rotation = rotation.inverted()
    points = []
    for obj in objects:
        evaluated = obj.evaluated_get(depsgraph)
        box = evaluated.bound_box
        if box is None or all(tuple(p) == (-1.0, -1.0, -1.0) for p in box):
            points.append(inverse_rotation @ evaluated.matrix_world.translation)
        else:
            transform = inverse_rotation.to_matrix().to_4x4() @ evaluated.matrix_world
            points.extend(transform @ Vector(corner) for corner in box)
    if not points:
        raise ValueError("No visible objects to frame")
    low = Vector(tuple(min(p[i] for p in points) for i in range(3)))
    high = Vector(tuple(max(p[i] for p in points) for i in range(3)))
    center = (low + high) * 0.5
    span = high - low
    half_height = max(span.y, span.x / aspect, 0.01) * 0.56
    padding = max(half_height, span.z * 0.01, 0.1)
    center.z = high.z + padding
    view = (Matrix.Translation(rotation @ center) @ rotation.to_matrix().to_4x4()).inverted()
    near, far = padding * 0.1, span.z + 2 * padding
    projection = Matrix(((1 / (half_height * aspect), 0, 0, 0),
                         (0, 1 / half_height, 0, 0),
                         (0, 0, -2 / (far - near), -(far + near) / (far - near)),
                         (0, 0, 0, 1)))
    return view, projection


def _preview_viewport(view, target, shading, max_size):
    import gpu
    from contextlib import ExitStack
    from mathutils import Vector

    context = bpy.context
    if bpy.app.background or not context.screen:
        raise RuntimeError("Interactive views require a Blender window with a 3D Viewport. "
                           "Use render_result in background Blender after rendering a frame.")
    area = next((area for area in context.screen.areas if area.type == "VIEW_3D"), None)
    region = next((r for r in area.regions if r.type == "WINDOW"), None) if area else None
    if not region:
        raise RuntimeError("Open a 3D Viewport in the current Blender window")
    scene, layer, space = context.scene, context.view_layer, area.spaces.active
    depsgraph = context.evaluated_depsgraph_get()
    size = _preview_size(region.width, region.height, max_size)
    visible = []
    if target or view not in {"viewport", "camera"}:
        visible = [obj for obj in layer.objects if obj.visible_get(view_layer=layer, viewport=space)
                   and obj.type not in {"CAMERA", "LIGHT", "LIGHT_PROBE", "SPEAKER"}]
    if target:
        obj = layer.objects.get(target)
        if obj is None or obj not in visible:
            raise ValueError(f"No visible target named {target!r} in the current view layer")
        family = {obj, *obj.children_recursive}
        subjects = [child for child in visible if child in family]
    else:
        subjects = visible
    if view == "camera":
        if not scene.camera:
            raise ValueError("The scene has no active camera")
        cam = scene.camera.evaluated_get(depsgraph)
        render = scene.render
        w, h = render.resolution_x, render.resolution_y
        size = _preview_size(w * render.pixel_aspect_x, h * render.pixel_aspect_y, max_size)
        view_matrix = cam.matrix_world.normalized().inverted()
        projection = cam.calc_matrix_camera(depsgraph, x=w, y=h, scale_x=render.pixel_aspect_x,
                                            scale_y=render.pixel_aspect_y)
    elif view == "viewport" and not target:
        view_matrix = space.region_3d.view_matrix.copy()
        projection = space.region_3d.window_matrix.copy()
    else:
        if view == "viewport":
            rotation = space.region_3d.view_matrix.inverted().to_quaternion()
        else:
            directions = {"front": (0, 1, 0), "right": (-1, 0, 0), "top": (0, 0, -1),
                          "three_quarter": (-1, 1, -0.7)}
            rotation = Vector(directions[view]).to_track_quat("-Z", "Y")
        view_matrix, projection = _preview_frame(subjects, rotation, size[0] / size[1], depsgraph)

    with ExitStack() as cleanup, context.temp_override(area=area, region=region):
        def change(owner, key, value):
            previous = getattr(owner, key)
            if previous != value:
                cleanup.callback(setattr, owner, key, previous)
                setattr(owner, key, value)

        if shading == "solid":
            change(space.shading, "type", "SOLID")
            change(space.shading, "light", "STUDIO")
            change(space.shading, "color_type", "SINGLE")
        elif shading == "material":
            change(space.shading, "type", "MATERIAL")
        if space.shading.type == "RENDERED" and scene.render.engine == "CYCLES":
            raise ValueError("Use material or solid shading for a Cycles viewport, "
                             "or render_result for the finished frame")
        if target or view != "viewport" or shading != "current":
            change(space.overlay, "show_overlays", False)
        if target:
            for obj in visible:
                if obj not in family:
                    if obj.select_get(view_layer=layer):
                        cleanup.callback(obj.select_set, True, view_layer=layer)
                    cleanup.callback(obj.hide_set, False, view_layer=layer)
                    obj.hide_set(True, view_layer=layer)
            layer.update()
        offscreen = gpu.types.GPUOffScreen(*size)
        try:
            offscreen.draw_view3d(scene, layer, space, region, view_matrix, projection,
                                  do_color_management=True)
            buffer = offscreen.texture_color.read()
            buffer.dimensions = size[0] * size[1] * 4
            rgba = bytes(buffer)
        finally:
            offscreen.free()
    return size[0], size[1], _preview_png(*size, rgba)


def look(view="viewport", target=None, shading="current", max_size=768, image=None):
    import base64

    if view not in {"viewport", "front", "right", "top", "three_quarter", "camera", "render_result"}:
        raise ValueError(f"Unknown view {view!r}")
    if shading not in {"current", "solid", "material"}:
        raise ValueError(f"Unknown shading {shading!r}")
    if type(max_size) is not int or not 64 <= max_size <= 1600:
        raise ValueError("max_size must be an integer between 64 and 1600")
    if target is not None and (not isinstance(target, str) or not target.strip()):
        raise ValueError("target must be one exact object name")
    if target and view in {"camera", "render_result"}:
        raise ValueError(f"The {view} view has fixed framing. Omit target or choose an angle.")
    if image is not None:
        if not isinstance(image, str) or not image.strip():
            raise ValueError("image must be one exact Blender image datablock name")
        if view != "viewport" or target is not None or shading != "current":
            raise ValueError("Use image with max_size only; omit view, shading and target")
        width, height, png = _preview_image(image, max_size)
    elif view == "render_result":
        if shading != "current":
            raise ValueError("render_result uses the shading of the rendered frame")
        width, height, png = _preview_image("Render Result", max_size)
    else:
        width, height, png = _preview_viewport(view, target, shading, max_size)
    return {"image": base64.b64encode(png).decode("ascii"), "mime_type": "image/png",
            "width": width, "height": height}


class BlenderMCPServer:
    """Socket threads receive commands. One persistent Blender timer executes them."""

    def __init__(self, port=9876):
        self.host = "127.0.0.1"
        self.port = port
        self.running = False
        self.socket = None
        self.server_thread = None
        self.command_queue = queue.Queue()
        self._clients = set()
        self._clients_lock = threading.Lock()
        # bpy.app.timers uses callable identity, so retain this bound method.
        self._timer = self._drain_command_queue

    def start(self):
        if self.running:
            return
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind((self.host, self.port))
            self.port = self.socket.getsockname()[1]
            self.socket.listen(5)
            self.socket.settimeout(0.2)
            self.running = True
            bpy.app.timers.register(self._timer, persistent=True)
            self.server_thread = threading.Thread(target=self._server_loop, daemon=True)
            self.server_thread.start()
        except Exception:
            self.stop()
            raise

    def stop(self):
        self.running = False
        if bpy.app.timers.is_registered(self._timer):
            bpy.app.timers.unregister(self._timer)
        if self.socket:
            self.socket.close()
        if self.server_thread and self.server_thread.is_alive():
            self.server_thread.join()
        self.socket = None
        with self._clients_lock:
            for client in self._clients:
                with suppress(OSError):
                    client.shutdown(socket.SHUT_RDWR)
                client.close()
            self._clients.clear()
        while not self.command_queue.empty():
            self.command_queue.get_nowait()

    def _server_loop(self):
        while self.running:
            try:
                client, _address = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._clients_lock:
                self._clients.add(client)
            threading.Thread(target=self._handle_client, args=(client,), daemon=True).start()

    def _handle_client(self, client):
        buffer = bytearray()
        try:
            client.settimeout(1)
            while self.running:
                try:
                    chunk = client.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buffer.extend(chunk)
                if len(buffer) > 4 * 1024 * 1024:
                    break
                # Both peers exchange a single JSON object per turn. Test the tail
                # before parsing, so long strings are not decoded for each chunk.
                if not chunk.rstrip().endswith(b"}"):
                    continue
                try:
                    command = json.loads(buffer)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                self.command_queue.put((command, client))
                buffer.clear()
        except OSError:
            pass
        finally:
            with self._clients_lock:
                self._clients.discard(client)
            client.close()

    def _client_gone(self, client):
        with self._clients_lock:
            if client not in self._clients:
                return True
        try:
            readable, _, _ = select.select([client], [], [], 0)
            return bool(readable) and client.recv(1, socket.MSG_PEEK) == b""
        except socket.timeout:
            return False
        except (OSError, ValueError):
            return True

    def _drain_command_queue(self):
        if not self.running:
            return None
        while True:
            try:
                command, client = self.command_queue.get_nowait()
            except queue.Empty:
                return 0.05
            if not self._client_gone(client):
                break
        reply = self.execute_command(command)
        try:
            client.sendall(json.dumps(reply).encode("utf-8"))
        except OSError:
            # The command already ran. Drop the reply and release any reader.
            with suppress(OSError):
                client.shutdown(socket.SHUT_RDWR)
        return 0.0

    def execute_command(self, command):
        try:
            kind = command["type"]
            params = command.get("params", {})
            if kind == "minimal.execute_code":
                return execute_code(**params)
            if kind == "minimal.get_scene_info":
                result = get_scene_info(**params)
            elif kind == "minimal.look":
                result = look(**params)
            else:
                raise ValueError(f"Unknown command: {kind}")
            return {"status": "success", "result": result}
        except Exception as exc:
            return _error(exc)


_server = None


class BLENDERMCP_MINIMAL_OT_StartServer(bpy.types.Operator):
    bl_idname = "blendermcp_minimal.start_server"
    bl_label = "Start MCP Server"

    def execute(self, context):
        global _server
        if _server is None:
            server = BlenderMCPServer(port=context.window_manager.blendermcp_minimal_port)
            try:
                server.start()
            except OSError as exc:
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}
            _server = server
        return {"FINISHED"}


class BLENDERMCP_MINIMAL_OT_StopServer(bpy.types.Operator):
    bl_idname = "blendermcp_minimal.stop_server"
    bl_label = "Stop MCP Server"

    def execute(self, context):
        global _server
        if _server is not None:
            _server.stop()
            _server = None
        return {"FINISHED"}


class BLENDERMCP_MINIMAL_PT_Panel(bpy.types.Panel):
    bl_label = "BlenderMCP"
    bl_idname = "BLENDERMCP_MINIMAL_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BlenderMCP"

    def draw(self, context):
        if _server is None:
            self.layout.prop(context.window_manager, "blendermcp_minimal_port")
            self.layout.operator("blendermcp_minimal.start_server")
        else:
            self.layout.label(text=f"Listening on {_server.host}:{_server.port}")
            self.layout.operator("blendermcp_minimal.stop_server")


_CLASSES = (BLENDERMCP_MINIMAL_OT_StartServer, BLENDERMCP_MINIMAL_OT_StopServer, BLENDERMCP_MINIMAL_PT_Panel)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.blendermcp_minimal_port = bpy.props.IntProperty(
        name="Port", default=9876, min=1, max=65535, options={"SKIP_SAVE"}
    )


def unregister():
    global _server
    if _server is not None:
        _server.stop()
        _server = None
    del bpy.types.WindowManager.blendermcp_minimal_port
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
