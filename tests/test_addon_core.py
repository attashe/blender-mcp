"""Exercise the installed single-file add-on's script and inspection contracts."""
import importlib.util
import sys
from types import SimpleNamespace

import pytest

from conftest import ROOT_ADDON


@pytest.fixture
def addon(monkeypatch):
    bpy = SimpleNamespace(types=SimpleNamespace(Operator=object, Panel=object))
    for name, module in (("bpy", bpy), ("bmesh", object()), ("mathutils", object())):
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location("addon_under_test", ROOT_ADDON)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_script_exit_returns_partial_output_then_allows_next_command(addon):
    server = addon.BlenderMCPServer()
    code = "import sys\nprint('working')\nprint('detail', file=sys.stderr)\nraise SystemExit(12)"
    result = server.execute_command({"type": "minimal.execute_code", "params": {"code": code}})
    assert result["status"] == "error"
    assert result["error_type"] == "SystemExit"
    assert "raise SystemExit(12)" in result["traceback"]
    assert result["stdout"] == "working\n"
    assert result["stderr"] == "detail\n"
    result = server.execute_command({"type": "minimal.execute_code", "params": {"code": "print('alive')"}})
    assert result["result"] == {"stdout": "alive\n", "stderr": ""}


def test_script_bounds_output_separately_from_errors(addon):
    result = addon.execute_code("print('z' * 100_000)\nraise ValueError('after print')")
    assert result["stdout"] == "z" * addon.OUTPUT_LIMIT + "\n[output truncated]"
    assert result["message"] == "after print"
    assert "after print" in result["traceback"]


def set_scene_objects(addon, names):
    class Objects(list):
        def get(self, name):
            return next((obj for obj in self if obj.name == name), None)

    items = Objects(SimpleNamespace(name=name, type="MESH", dimensions=(1, 2, 3)) for name in names)
    scene = SimpleNamespace(
        name="Scene", objects=items, collection=SimpleNamespace(children_recursive=[]),
        unit_settings=SimpleNamespace(system="METRIC", length_unit="METERS", scale_length=1.0),
    )
    addon.bpy.data = SimpleNamespace(filepath="")
    addon.bpy.app = SimpleNamespace(version_string="5.0")
    addon.bpy.context = SimpleNamespace(
        scene=scene, selected_objects=items, mode="OBJECT",
        view_layer=SimpleNamespace(objects=SimpleNamespace(active=items[1])),
    )


def test_inspect_uses_exact_names_and_reports_limits(addon):
    set_scene_objects(addon, ["A", "B", "C"])
    result = addon.get_scene_info(objects=["B", "miss", "C"], fields=["dimensions"], limit=1)
    assert result["object_count"] == 3
    assert result["objects"] == [{"name": "B", "type": "MESH", "dimensions": [1, 2, 3]}]
    assert result["missing_objects"] == ["miss"]
    assert result["objects_truncated"]
    assert result["selected_objects"] == ["A"]
    assert result["active_object"] == "B"
    assert addon.get_scene_info(objects=[])["objects"] == []
    assert addon.get_scene_info()["objects"][0] == {"name": "A", "type": "MESH"}
    with pytest.raises(ValueError, match="Unknown object fields"):
        addon.get_scene_info(fields=["unknown"])


def test_query_filters_before_limit_and_intersects_exact_names(addon):
    set_scene_objects(addon, [f"Cube.{i}" for i in range(25)] + ["Straße.L", "STRASSE.R"])
    result = addon.get_scene_info(query="STRASSE", limit=1)
    assert result["object_count"] == 27
    assert result["objects"] == [{"name": "Straße.L", "type": "MESH"}]
    assert result["objects_truncated"]

    result = addon.get_scene_info(objects=["STRASSE.R", "Cube.4", "missing"], query="straße", limit=1)
    assert result["objects"] == [{"name": "STRASSE.R", "type": "MESH"}]
    assert result["missing_objects"] == ["missing"]
    assert not result["objects_truncated"]

    empty = addon.get_scene_info(query="nonexistent")
    assert empty["objects"] == [] and not empty["objects_truncated"]
