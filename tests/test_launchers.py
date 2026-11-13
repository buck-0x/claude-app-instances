from dataclasses import replace
from pathlib import Path

from claude_instances.launchers import (
    Launcher,
    discover_launchers,
    scan_launchers,
    parse_launcher_script,
    rebuild_command,
)
from helpers import make_launcher_app


def make_and_discover(apps_dir: Path, name: str, data_dir: Path, **kwargs) -> Launcher:
    make_launcher_app(apps_dir, name, data_dir, **kwargs)
    (launcher,) = discover_launchers([apps_dir])
    return launcher


def test_discovers_all_formats(tmp_path):
    make_launcher_app(tmp_path, "Claude Alpha", tmp_path / "AS/Claude-Alpha")
    make_launcher_app(tmp_path, "Claude Mid", tmp_path / "AS/Claude-Mid", fmt="watchdog")
    make_launcher_app(tmp_path, "Claude Old", tmp_path / "AS/Claude-Old", fmt="oneshot")
    a, m, o = discover_launchers([tmp_path])
    assert (a.is_watchdog, a.has_argless_rule) == (True, True)
    assert (m.is_watchdog, m.has_argless_rule) == (True, False)
    assert (o.is_watchdog, o.has_argless_rule, o.data_dir) == (False, False, tmp_path / "AS/Claude-Old")
    assert a.script_path == tmp_path / "Claude Alpha.app/Contents/MacOS/launcher"
    assert (a.name, a.app_path, a.data_dir) == (
        "Claude Alpha", tmp_path / "Claude Alpha.app", tmp_path / "AS/Claude-Alpha")
    assert o.claude_app == Path("/Applications/Claude.app")


def test_ignores_chatgpt_and_foreign_apps(tmp_path):
    make_launcher_app(tmp_path, "ChatGPT X", tmp_path / "d", bundle_id="local.launcher-chatgpt.x")
    make_launcher_app(tmp_path, "Other", tmp_path / "d", bundle_id="com.example.other")
    assert discover_launchers([tmp_path]) == []


def test_rebuild_command_keeps_data_folder_and_apps_dir(tmp_path):
    support = tmp_path / "AS"
    l = make_and_discover(tmp_path / "Apps", "Claude Personal", support / "Claude")
    assert rebuild_command(l, support) == f"new-claude 'Claude Personal' --dir Claude --apps {tmp_path / 'Apps'}"
    assert rebuild_command(replace(l, data_dir=tmp_path / "elsewhere/X"), support) is None


def test_rebuild_command_omits_apps_for_system_applications(tmp_path):
    support = tmp_path / "AS"
    l = make_and_discover(tmp_path, "Claude Work", support / "Claude-Work")
    l = replace(l, app_path=Path("/Applications/Claude Work.app"))
    assert rebuild_command(l, support) == "new-claude 'Claude Work' --dir Claude-Work"


def test_data_dir_with_spaces(tmp_path):
    data = tmp_path / "Library/Application Support/Claude-Work"
    for fmt in ("argless", "watchdog", "oneshot"):
        apps = tmp_path / fmt
        assert make_and_discover(apps, "Claude Work", data, fmt=fmt).data_dir == data


def test_unreadable_plist_skipped(tmp_path):
    app = make_launcher_app(tmp_path, "Claude Bad", tmp_path / "AS/Claude-Bad")
    (app / "Contents/Info.plist").write_bytes(b"garbage")
    app = make_launcher_app(tmp_path, "Claude Cut", tmp_path / "AS/Claude-Cut")
    (app / "Contents/Info.plist").write_bytes(b'<?xml version="1.0"?><plist><dict><key>a')
    make_launcher_app(tmp_path, "Claude Good", tmp_path / "AS/Claude-Good")
    assert [l.name for l in discover_launchers([tmp_path])] == ["Claude Good"]


def test_claude_app_override_line(tmp_path):
    fake = Path("/tmp/FakeClaude.app")
    for fmt in ("argless", "oneshot"):
        l = make_and_discover(tmp_path / fmt, "Claude X", tmp_path / "AS/Claude-X",
                              fmt=fmt, claude_app=fake)
        assert l.claude_app == fake


def test_scans_multiple_dirs_and_missing_dir(tmp_path):
    make_launcher_app(tmp_path / "a", "Claude Two", tmp_path / "AS/Claude-Two")
    make_launcher_app(tmp_path / "a", "Claude One", tmp_path / "AS/Claude-One")
    found = discover_launchers([tmp_path / "a", tmp_path / "none"])
    assert [l.name for l in found] == ["Claude One", "Claude Two"]


def test_parse_script_without_data_dir_uses_default_app():
    default = Path("/Applications/Claude.app")
    assert parse_launcher_script("#!/bin/zsh\necho hi\n", default) == (None, default, False, False)


def test_same_name_in_two_dirs_first_dir_wins(tmp_path):
    system, user = tmp_path / "Applications", tmp_path / "home/Applications"
    make_launcher_app(system, "Claude Work", tmp_path / "AS/Claude-Work")
    make_launcher_app(user, "Claude Work", tmp_path / "AS/Claude-Work-2")
    make_launcher_app(user, "Claude Solo", tmp_path / "AS/Claude-Solo")
    kept, shadowed = scan_launchers([system, user])
    assert [(l.name, l.app_path.parent) for l in kept] == [
        ("Claude Solo", user), ("Claude Work", system)]
    assert [l.app_path for l in shadowed] == [user / "Claude Work.app"]
    assert discover_launchers([system, user]) == kept
    # The scan order decides, not the path order.
    kept, shadowed = scan_launchers([user, system])
    assert kept[1].app_path.parent == user and shadowed[0].app_path.parent == system
