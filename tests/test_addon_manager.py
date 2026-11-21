from pathlib import Path

from blender_mcp import addon_manager as manager
from conftest import ROOT_ADDON


def test_wheel_addon_matches_standalone():
    assert manager.get_bundled_addon_path().read_bytes() == ROOT_ADDON.read_bytes()


def test_install_and_upgrade_only_the_minimal_addon(tmp_path):
    existing = tmp_path / "blender_mcp.py"
    existing.write_text("another installed add-on")
    target = manager.install_addon(tmp_path)
    assert target.name == "blender_mcp_minimal.py"
    assert target.read_bytes() == ROOT_ADDON.read_bytes()
    target.write_text("previous version")
    manager.install_addon(tmp_path)
    manager.install_addon(tmp_path)
    assert target.with_suffix(".py.bak").read_text() == "previous version"
    assert existing.read_text() == "another installed add-on"


def test_explicit_location_has_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("BLENDERMCP_ADDONS_DIR", str(tmp_path))
    assert manager.discover_blender_addon_dirs() == [tmp_path]
    assert manager.run_cli(["install-addon"]) == 0
    assert (tmp_path / "blender_mcp_minimal.py").is_file()


def test_default_discovery_orders_native_and_flatpak_versions(tmp_path, monkeypatch):
    native = tmp_path / ".config" / "blender" / "5.9"
    flatpak = tmp_path / ".var" / "app" / "org.blender.Blender" / "config" / "blender" / "5.10"
    for directory in (native, flatpak):
        directory.mkdir(parents=True)
    for name in ("XDG_CONFIG_HOME", "BLENDERMCP_ADDONS_DIR", "BLENDER_USER_ADDONS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(manager.sys, "platform", "linux")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert manager.discover_blender_addon_dirs() == [flatpak / "scripts" / "addons", native / "scripts" / "addons"]
