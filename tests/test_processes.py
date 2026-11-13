import os
import plistlib
from pathlib import Path

from claude_instances.launchers import Launcher
from claude_instances.processes import (
    MainProcess,
    ProcessLine,
    Running,
    claude_binary,
    main_processes,
    match_instances,
    parse_ps,
    watchdog_alive,
    watchdog_pids,
)

FIXTURE = (Path(__file__).parent / "fixtures/ps-output.txt").read_text()
BIN = "/Applications/Claude.app/Contents/MacOS/Claude"
AS = Path("/Users/u/Library/Application Support")
V1_SCRIPT = Path("/Apps/Claude V1.app/Contents/MacOS/launcher")


def L(name: str, data_dir: Path) -> Launcher:
    app = Path(f"/Apps/{name}.app")
    return Launcher(name, app, app / "Contents/MacOS/launcher", data_dir,
                    Path("/Applications/Claude.app"), True, True)


def test_parse_ps_keeps_spaces_in_command():
    lines = parse_ps(FIXTURE)
    assert lines[0] == ProcessLine(501, f"{BIN} --user-data-dir={AS}/Claude-V1")
    assert lines[6].command.startswith(
        "/Applications/Claude.app/Contents/Frameworks/Claude Helper (Renderer).app/")
    assert [l.pid for l in lines] == list(range(501, 512))


def test_main_processes_excludes_type_helpers_grep_and_non_flag_args():
    procs = main_processes(parse_ps(FIXTURE), BIN)
    assert sorted(p.pid for p in procs) == [501, 502, 504, 505, 506, 511]
    assert MainProcess(504, None) in procs and MainProcess(511, None) in procs


def test_v1_does_not_match_v12():
    procs = main_processes(parse_ps(FIXTURE), BIN)
    assert next(p for p in procs if p.pid == 501).data_dir == AS / "Claude-V1"
    assert next(p for p in procs if p.pid == 502).data_dir == AS / "Claude-V12"
    assert next(p for p in procs if p.pid == 506).data_dir == AS / "Claude-Dup"


def test_match_instances_argless_goes_to_default_launcher_and_duplicates():
    launchers = [L("Claude V1", AS / "Claude-V1"), L("Claude Personal", AS / "Claude"),
                 L("Claude Dup", AS / "Claude-Dup"), L("Claude Idle", AS / "Claude-Idle")]
    by_name, strays = match_instances(
        main_processes(parse_ps(FIXTURE), BIN), launchers, AS / "Claude")
    assert by_name["Claude Personal"] == [Running(504, argless=True), Running(511, argless=True)]
    assert by_name["Claude V1"] == [Running(501, argless=False)]
    assert [r.pid for r in by_name["Claude Dup"]] == [505, 506]
    assert by_name["Claude Idle"] == []
    assert [p.pid for p in strays] == [502]


def test_argless_is_stray_when_no_default_launcher():
    launchers = [L("Claude V1", AS / "Claude-V1"), L("Claude Dup", AS / "Claude-Dup")]
    by_name, strays = match_instances(
        main_processes(parse_ps(FIXTURE), BIN), launchers, AS / "Claude")
    assert [p.pid for p in strays] == [502, 504, 511]
    assert MainProcess(504, None) in strays and MainProcess(511, None) in strays
    assert all(not r.argless for runs in by_name.values() for r in runs)


def test_watchdog_pids_match_script_path_only():
    assert watchdog_pids(parse_ps(FIXTURE), V1_SCRIPT) == [508]


def test_watchdog_pids_need_the_whole_script_path():
    script = Path("/Applications/Claude W.app/Contents/MacOS/launcher")
    lines = [
        ProcessLine(1, f"/bin/zsh {script}"),
        ProcessLine(2, f"/bin/zsh /Users/u{script}"),  # the same name in ~/Applications
        ProcessLine(3, f"/bin/zsh {script}.bak"),
        ProcessLine(4, f"/bin/zsh {script} --flag"),
        ProcessLine(5, f"{script}"),
    ]
    assert watchdog_pids(lines, script) == [1, 4, 5]


def test_watchdog_alive_requires_lock_and_live_cmdline(tmp_path):
    lines = parse_ps(FIXTURE)
    assert not watchdog_alive("Claude V1", V1_SCRIPT, lines, tmp_path)  # no lock
    os.symlink("508", tmp_path / "Claude V1.lock")
    assert watchdog_alive("Claude V1", V1_SCRIPT, lines, tmp_path)
    os.remove(tmp_path / "Claude V1.lock")
    os.symlink("999", tmp_path / "Claude V1.lock")
    assert not watchdog_alive("Claude V1", V1_SCRIPT, lines, tmp_path)
    os.remove(tmp_path / "Claude V1.lock")
    os.symlink("501", tmp_path / "Claude V1.lock")  # live pid, but not the watchdog
    assert not watchdog_alive("Claude V1", V1_SCRIPT, lines, tmp_path)


def test_claude_binary_reads_plist(tmp_path):
    app = tmp_path / "Stub.app"
    assert claude_binary(app) == f"{app}/Contents/MacOS/Claude"  # missing plist
    (app / "Contents").mkdir(parents=True)
    with open(app / "Contents/Info.plist", "wb") as f:
        plistlib.dump({"CFBundleExecutable": "stubclaude"}, f)
    assert claude_binary(app) == f"{app}/Contents/MacOS/stubclaude"
