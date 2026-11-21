"""Three MCP tools for a live Blender session, served over stdio."""

import argparse
import asyncio
import json
import logging
import os
import socket
import sys
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ImageContent, TextContent
from pydantic import Field

from .addon_manager import run_cli as run_addon_cli


logger = logging.getLogger(__name__)
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9876
CONNECT_TIMEOUT = 10.0
RESPONSE_TIMEOUT = 180.0
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 32 * 1024 * 1024


def parse_connection_args(argv):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    args, unknown = parser.parse_known_args(argv)
    if unknown:
        logger.warning("Ignoring unrecognized command-line arguments: %s", unknown)
    return args.host, args.port


def resolve_connection(cli_host=None, cli_port=None):
    host = cli_host or os.getenv("BLENDER_HOST") or DEFAULT_HOST
    if cli_port is not None:
        return host, cli_port
    raw_port = os.getenv("BLENDER_PORT")
    try:
        return host, int(raw_port) if raw_port else DEFAULT_PORT
    except ValueError:
        logger.warning("Invalid BLENDER_PORT %r; using %d", raw_port, DEFAULT_PORT)
        return host, DEFAULT_PORT


@dataclass
class BlenderConnection:
    host: str
    port: int
    sock: socket.socket | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def disconnect(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def _receive(self, sock: socket.socket) -> dict:
        # The upstream wire format is one UTF-8 JSON object per exchange.
        # A base64 image can span many reads. Only try decoding at a possible
        # object boundary, and keep split Unicode sequences as bytes until then.
        data = bytearray()
        deadline = time.monotonic() + RESPONSE_TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for Blender")
            sock.settimeout(remaining)
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError("Blender closed the socket before returning a complete response")
            data.extend(chunk)
            if len(data) > MAX_RESPONSE_BYTES:
                raise ValueError("Blender response exceeded 32 MiB")
            if not chunk.rstrip().endswith(b"}"):
                continue
            try:
                reply = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(reply, dict) or reply.get("status") not in ("success", "error"):
                raise ValueError("Unexpected response envelope from the Blender add-on")
            return reply

    def send_command(self, command: str, params: dict) -> dict:
        packet = json.dumps({"type": "minimal." + command, "params": params}, ensure_ascii=False).encode("utf-8")
        if len(packet) > MAX_REQUEST_BYTES:
            raise ValueError("Request exceeds the add-on's 4 MiB limit. Send a smaller script or object list.")
        # The add-on matches replies by order. Keep the complete exchange under
        # one lock even if the MCP client cancels a call while Blender is busy.
        with self._lock:
            if self.sock is None:
                try:
                    self.sock = socket.create_connection((self.host, self.port), timeout=CONNECT_TIMEOUT)
                except OSError as exc:
                    raise ConnectionError(
                        f"Could not connect to Blender at {self.host}:{self.port}. "
                        f"Start the add-on from the BlenderMCP sidebar. {exc}"
                    ) from exc
            try:
                self.sock.settimeout(RESPONSE_TIMEOUT)
                self.sock.sendall(packet)
                return self._receive(self.sock)
            except (OSError, ValueError) as exc:
                self.disconnect()
                message = f"Lost the response to {command}: {exc}."
                if command == "execute_code":
                    message += " The script may have run. Inspect the scene before repeating it."
                raise ConnectionError(message) from exc


_connection = BlenderConnection(*resolve_connection())


@asynccontextmanager
async def server_lifespan(server: FastMCP):
    try:
        yield {}
    finally:
        _connection.disconnect()


mcp = FastMCP(
    "BlenderMCP",
    instructions=(
        "Inspect the active Blender scene with get_scene_info, make a focused change "
        "using Python with execute_blender_code, then use look to check the image."
    ),
    lifespan=server_lifespan,
)


def _text_result(data: dict, *, error: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False, default=str))],
        isError=error,
    )


async def _call(command: str, params: dict) -> CallToolResult:
    try:
        reply = await asyncio.to_thread(_connection.send_command, command, params)
        if reply["status"] == "error":
            error = {k: v for k, v in reply.items() if k != "status"}
            if "error_type" not in error and error.get("message") == f"Unknown command type: minimal.{command}":
                error["error_type"] = "IncompatibleAddon"
                error["message"] += (
                    ". Stop the other Blender MCP listener, enable Blender MCP (Minimal) "
                    "and start it on the configured port."
                )
            return _text_result(error, error=True)
        data = reply["result"]
        if command == "look":
            return CallToolResult(content=[
                ImageContent(type="image", mimeType=data["mime_type"], data=data["image"])
            ])
        return _text_result(data)
    except (OSError, KeyError, ValueError, TypeError) as exc:
        return _text_result({"error_type": type(exc).__name__, "message": str(exc)}, error=True)


ObjectField = Literal["location", "rotation", "scale", "dimensions", "world_bounds", "materials", "collections"]


@mcp.tool()
async def get_scene_info(
    objects: list[str] | None = None,
    fields: list[ObjectField] | None = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
    query: str | None = None,
) -> CallToolResult:
    """Inspect the open file, Blender version, mode, units, selection and objects.
    Query finds a case-insensitive substring in object names before applying limit.
    Objects restricts the search to exact names. Request fields for more detail.
    world_bounds returns world-axis min/max of the evaluated bounding box, or null when unavailable.
    """
    params = {"objects": objects, "fields": fields, "limit": limit}
    if query is not None:
        params["query"] = query
    return await _call("get_scene_info", params)


@mcp.tool()
async def execute_blender_code(code: str) -> CallToolResult:
    """Run Python in the current Blender session, with bpy, bmesh and mathutils.
    Use print() for results. Returns stdout and stderr, plus a traceback on failure.
    Changes made before a script error may persist.
    """
    return await _call("execute_code", {"code": code})


@mcp.tool()
async def look(
    view: Literal["viewport", "front", "right", "top", "three_quarter", "camera", "render_result"] = "viewport",
    target: str | None = None,
    shading: Literal["current", "solid", "material"] = "current",
    max_size: Annotated[int, Field(ge=64, le=1600)] = 768,
    image: str | None = None,
) -> CallToolResult:
    """Get a PNG of the working viewport, a named angle, camera view or last render.
    Target isolates and frames a visible object and its children in viewport or angle views.
    Use solid shading to inspect shape. render_result uses current shading and no target.
    Max_size bounds the longest image edge in pixels.
    To read an image already in Blender, pass its exact datablock name in image
    (including "Render Result"), with only max_size as an optional setting.
    """
    params = {"view": view, "target": target, "shading": shading, "max_size": max_size}
    if image is not None:
        params["image"] = image
    return await _call("look", params)


def main():
    global _connection
    argv = sys.argv[1:]
    if argv and argv[0] in {"install-addon", "addon-paths", "-h", "--help"}:
        raise SystemExit(run_addon_cli(argv))
    _connection = BlenderConnection(*resolve_connection(*parse_connection_args(argv)))
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
