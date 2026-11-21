import asyncio
import base64
import json
import socket
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from blender_mcp import server


def test_tools_via_mcp_client(monkeypatch):
    errors = {
        "error_type": "SystemExit", "message": "2", "stdout": "начало\n",
        "stderr": "diagnostic\n", "traceback": "Traceback ... SystemExit: 2",
    }
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode("ascii")
    requests = []

    def dispatch(command, params):
        requests.append((command, params))
        if command == "execute_code":
            return {"status": "error", **errors}
        if command == "look":
            return {"status": "success", "result": {"image": png, "mime_type": "image/png"}}
        return {"status": "success", "result": {"mode": "OBJECT"}}

    monkeypatch.setattr(server._connection, "send_command", dispatch)

    async def exercise():
        async with create_connected_server_and_client_session(server.mcp) as client:
            listed = {t.name: t for t in (await client.list_tools()).tools}
            assert set(listed) == {"execute_blender_code", "get_scene_info", "look"}
            assert listed["execute_blender_code"].inputSchema["required"] == ["code"]
            assert not listed["get_scene_info"].inputSchema.get("required")

            failed = await client.call_tool("execute_blender_code", {"code": "raise SystemExit(2)"})
            assert failed.isError
            assert json.loads(failed.content[0].text) == errors
            state = await client.call_tool("get_scene_info", {})
            assert not state.isError and json.loads(state.content[0].text)["mode"] == "OBJECT"
            picture = await client.call_tool("look", {"target": "кривая", "view": "front"})
            assert not picture.isError
            assert picture.content[0].type == "image"
            assert (picture.content[0].data, picture.content[0].mimeType) == (png, "image/png")
            invalid = await client.call_tool("look", {"view": "unrecognised"})
            assert invalid.isError

    asyncio.run(exercise())
    assert [c for c, _ in requests] == ["execute_code", "get_scene_info", "look"]
    assert requests[1][1] == {"fields": None, "objects": None, "limit": 20}
    assert requests[2][1]["target"] == "кривая"


def test_lost_script_response_is_not_replayed_and_next_inspection_reconnects():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(3)
        conn = server.BlenderConnection(*listener.getsockname())
        commands = []

        def serve():
            for _ in range(2):
                client, _ = listener.accept()
                with client:
                    client.settimeout(3)
                    command = json.loads(client.recv(4096))
                    commands.append(command["type"])
                    if command["type"] == "minimal.get_scene_info":
                        client.sendall(b'{"status":"success","result":{"count":1}}')

        with ThreadPoolExecutor(max_workers=1) as pool:
            job = pool.submit(serve)
            try:
                with pytest.raises(ConnectionError, match="script may have run"):
                    conn.send_command("execute_code", {"code": "pass"})
                assert conn.sock is None
                assert conn.send_command("get_scene_info", {})["result"]["count"] == 1
                job.result(timeout=3)
            finally:
                conn.disconnect()
        assert commands == ["minimal.execute_code", "minimal.get_scene_info"]


def test_concurrent_commands_keep_their_own_replies():
    client, peer = socket.socketpair()
    conn = server.BlenderConnection("127.0.0.1", 0, sock=client)
    commands_started = threading.Barrier(3, timeout=3)

    def send(index):
        commands_started.wait()
        return conn.send_command("get_scene_info", {"number": index})["result"]

    def serve():
        peer.settimeout(3)
        commands_started.wait()
        for _ in range(2):
            req = json.loads(peer.recv(4096))
            payload = {"label": "日本語", "number": req["params"]["number"]}
            data = json.dumps({"status": "success", "result": payload}, ensure_ascii=False).encode()
            split = data.index("日".encode()) + 1
            peer.sendall(data[:split])
            peer.sendall(data[split:])

    with peer, client, ThreadPoolExecutor(max_workers=3) as pool:
        responder = pool.submit(serve)
        futures = [pool.submit(send, n) for n in range(2)]
        for n, task in enumerate(futures):
            assert task.result(timeout=5) == {"label": "日本語", "number": n}
        responder.result(timeout=3)


def test_timeout_disconnects_without_retry(monkeypatch):
    monkeypatch.setattr(server, "RESPONSE_TIMEOUT", 0.05)
    client, busy_peer = socket.socketpair()
    conn = server.BlenderConnection("127.0.0.1", 0, sock=client)
    with busy_peer, client:
        with pytest.raises(ConnectionError, match="script may have run"):
            conn.send_command("execute_code", {"code": "pass"})
        assert conn.sock is None and client.fileno() == -1
        assert json.loads(busy_peer.recv(4096))["type"] == "minimal.execute_code"


def test_oversize_script_rejected_before_connecting(monkeypatch):
    monkeypatch.setattr(socket, "create_connection", lambda *a, **kw: pytest.fail("unexpected connection"))
    # Character count fits; the encoded bytes cross the cap.
    code = "#" + "é" * (server.MAX_REQUEST_BYTES // 2)
    conn = server.BlenderConnection("127.0.0.1", 0)
    with pytest.raises(ValueError, match="4 MiB"):
        conn.send_command("execute_code", {"code": code})


def test_upstream_unknown_command_has_matching_addon_hint(monkeypatch):
    def upstream(command, params):
        return {"status": "error", "message": f"Unknown command type: minimal.{command}"}

    monkeypatch.setattr(server._connection, "send_command", upstream)
    result = asyncio.run(server.execute_blender_code("pass"))
    message = json.loads(result.content[0].text)
    assert result.isError and message["error_type"] == "IncompatibleAddon"
    assert "start" in message["message"] and "Blender MCP (Minimal)" in message["message"]
