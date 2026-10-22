"""Auto-update in the bootstrap (src/qqdepot/bootstrap.py): off without qq-setup's marker, a background
check at most once a day, the update first on the next run, and never a changed checkout."""
import json
import shutil
import subprocess
import time

import pytest

from qqdepot import bootstrap

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
S = "c" * 40


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


class Ran(BaseException):
    pass


@pytest.fixture
def setup_install(tmp_path, monkeypatch):
    """~/qq-tools/depot cloned at commit A of an upstream that has B on top; the marker names A."""
    for name in ("CI", "QQ_PINNED", bootstrap.RAN_ENV):
        monkeypatch.delenv(name, raising=False)
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git(upstream, "init", "-q", "-b", "main")
    (upstream / "f").write_text("a\n")
    git(upstream, "add", "f")
    git(upstream, "commit", "-q", "-m", "a")
    a = git(upstream, "rev-parse", "HEAD")
    tools = tmp_path / "qq-tools"
    tools.mkdir()
    subprocess.run(["git", "clone", "-q", str(upstream), str(tools / "depot")], check=True)
    (upstream / "f").write_text("b\n")
    git(upstream, "commit", "-q", "-am", "b")
    b = git(upstream, "rev-parse", "HEAD")
    monkeypatch.setattr(bootstrap, "tools_dir", lambda: tools)
    monkeypatch.setattr(bootstrap, "QQ_REPO", str(upstream))
    monkeypatch.setattr(bootstrap, "ALLOW_PROTOCOL", "file")
    (tools / "auto-update").write_text(json.dumps({"qq": a, "qqsync": S}))
    spawned, ran = [], []
    monkeypatch.setattr(bootstrap, "_spawn", spawned.append)

    def execve(path, argv, env):
        ran.append((argv[1:], env.get(bootstrap.RAN_ENV)))
        raise Ran
    monkeypatch.setattr(bootstrap.os, "execve", execve)
    return {"tools": tools, "depot": tools / "depot", "a": a, "b": b, "spawned": spawned, "ran": ran,
            "upstream": upstream}


def state(t, **kw):
    (t["tools"] / "auto-update.json").write_text(json.dumps(kw))


def found(t):
    state(t, pins={"qq": t["b"], "qqsync": S}, pins_at=time.time() + 5, checked=time.time())


def test_off_without_the_marker(setup_install):
    (setup_install["tools"] / "auto-update").unlink()
    found(setup_install)
    bootstrap.auto_update(setup_install["depot"], ["status"])
    assert bootstrap.head(setup_install["depot"]) == setup_install["a"] and not setup_install["spawned"]


def test_the_check_runs_in_the_background_once_a_day(setup_install):
    bootstrap.auto_update(setup_install["depot"], ["status"])
    bootstrap.auto_update(setup_install["depot"], ["status"])
    assert len(setup_install["spawned"]) == 1
    assert setup_install["spawned"][0][1] == "-I", "isolated from the folder qq was run in"
    assert setup_install["spawned"][0][3:] == [bootstrap.CHECK, str(setup_install["depot"])]


def test_newer_pins_update_first_then_run_the_command(setup_install, capsys):
    found(setup_install)
    with pytest.raises(Ran):
        bootstrap.auto_update(setup_install["depot"], ["sync", "--dry-run"])
    assert bootstrap.head(setup_install["depot"]) == setup_install["b"]
    assert git(setup_install["depot"], "symbolic-ref", "--short", "HEAD") == "main", "still on main"
    assert setup_install["ran"] == [(["sync", "--dry-run"], "1")]
    assert json.loads((setup_install["tools"] / "auto-update").read_text())["qq"] == setup_install["b"]
    assert "updating to" in capsys.readouterr().err


@pytest.mark.parametrize("change", ["edit", "commit", "elsewhere", "ci", "pinned", "stale"])
def test_left_alone(setup_install, monkeypatch, change):
    t = setup_install
    found(t)
    depot = t["depot"]
    if change == "edit":
        (depot / "f").write_text("mine\n")
    elif change == "commit":
        (depot / "g").write_text("mine\n")
        git(depot, "add", "g")
        git(depot, "commit", "-q", "-m", "mine")
    elif change == "elsewhere":
        depot = t["tools"] / "other"
        subprocess.run(["git", "clone", "-q", str(t["depot"]), str(depot)], check=True)
    elif change == "ci":
        monkeypatch.setenv("CI", "true")
    elif change == "pinned":
        monkeypatch.setenv("QQ_PINNED", "1")
    elif change == "stale":
        state(t, pins={"qq": t["b"], "qqsync": S}, pins_at=time.time() - 60, checked=time.time())
    before = bootstrap.head(depot)
    bootstrap.auto_update(depot, ["status"])
    assert bootstrap.head(depot) == before and not t["ran"]


def test_a_failed_update_runs_this_qq_and_waits_an_hour(setup_install, capsys):
    t = setup_install
    state(t, pins={"qq": "d" * 40, "qqsync": S}, pins_at=time.time() + 5, checked=time.time())
    bootstrap.auto_update(t["depot"], ["status"])
    assert "could not update" in capsys.readouterr().err
    bootstrap.auto_update(t["depot"], ["status"])
    assert capsys.readouterr().err == "", "no second try within the hour"
    assert bootstrap.head(t["depot"]) == t["a"] and not t["ran"]
    assert "is not on quirq-ai/qq's main" in (t["tools"] / "auto-update.log").read_text()


def test_a_broken_state_file_never_stops_a_command(setup_install):
    (setup_install["tools"] / "auto-update.json").write_text("{not json")
    bootstrap.auto_update(setup_install["depot"], ["status"])
    state(setup_install, pins="nope", pins_at="x", checked=None)
    bootstrap.auto_update(setup_install["depot"], ["status"])


def test_pins_check_only_qqs_own_fields():
    good = {"schema": "qq-setup-pins/1", "qq": "a" * 40, "qqsync": S, "xo": "e" * 40, "later": [1]}
    assert bootstrap.pins_from(good) == {"qq": "a" * 40, "qqsync": S}
    for bad in (None, [], {**good, "schema": "qq-setup-pins/2"}, {**good, "qq": "a" * 7},
                {**good, "qqsync": None}, {k: v for k, v in good.items() if k != "qq"}):
        with pytest.raises(ValueError):
            bootstrap.pins_from(bad)


def test_the_check_records_pins_or_retries_in_an_hour(setup_install, monkeypatch):
    good = {"schema": "qq-setup-pins/1", "qq": "a" * 40, "qqsync": S, "xo": "e" * 40}
    monkeypatch.setattr(bootstrap, "get_pins", lambda: good)
    bootstrap.check()
    s = json.loads((setup_install["tools"] / "auto-update.json").read_text())
    assert s["pins"] == {"qq": "a" * 40, "qqsync": S} and s["pins_at"] == s["checked"]

    def down():
        raise ValueError("pins.json could not be reached")
    monkeypatch.setattr(bootstrap, "get_pins", down)
    bootstrap.check()
    s = json.loads((setup_install["tools"] / "auto-update.json").read_text())
    assert s["pins"]["qq"] == "a" * 40
    assert time.time() - s["checked"] == pytest.approx(bootstrap.DAY - bootstrap.RETRY, abs=60)


def test_pins_are_read_over_https_from_setups_main():
    assert bootstrap.PINS_URL.startswith("https://raw.githubusercontent.com/quirq-ai/setup/refs/heads/main/")
    with pytest.raises(ValueError, match="not https"):
        bootstrap.get_pins("http://127.0.0.1:1/pins.json", 1)


def test_qqsync_moves_only_while_it_is_setups(setup_install, monkeypatch):
    t = setup_install
    calls = []
    real = subprocess.run
    monkeypatch.setattr(bootstrap.subprocess, "run", lambda cmd, **kw: real(cmd, **kw) if cmd[0] == "git" else
                        calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    marker = {"qq": t["a"], "qqsync": S}
    pins = {"qq": t["a"], "qqsync": "f" * 40}
    monkeypatch.setattr(bootstrap, "_sync_commit", lambda: "9" * 40)
    assert bootstrap.update(t["depot"], pins, marker) and calls == []
    monkeypatch.setattr(bootstrap, "_sync_commit", lambda: S)
    assert bootstrap.update(t["depot"], pins, marker)
    assert calls[0][1:] == ["-I", "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--force-reinstall",
                            "--no-deps", f"qqsync @ git+https://github.com/quirq-ai/sync@{'f' * 40}"]


def test_one_update_at_a_time(setup_install):
    import fcntl
    found(setup_install)
    with (setup_install["tools"] / "auto-update.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        bootstrap.auto_update(setup_install["depot"], ["status"])
        assert bootstrap.head(setup_install["depot"]) == setup_install["a"] and not setup_install["ran"]
    with pytest.raises(Ran):
        bootstrap.auto_update(setup_install["depot"], ["status"])
    assert bootstrap.head(setup_install["depot"]) == setup_install["b"]


def test_git_ignores_the_callers_git_environment(setup_install, monkeypatch, tmp_path):
    """GIT_DIR and friends from the caller's shell must not point the update at another repo."""
    t = setup_install
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    (other / "x").write_text("x\n")
    git(other, "add", "x")
    git(other, "commit", "-q", "-m", "x")
    mine = git(other, "rev-parse", "HEAD")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.hooksPath'='/nonexistent'")
    found(t)
    with pytest.raises(Ran):
        bootstrap.auto_update(t["depot"], ["status"])
    monkeypatch.delenv("GIT_DIR")
    monkeypatch.delenv("GIT_WORK_TREE")
    assert git(t["depot"], "rev-parse", "HEAD") == t["b"]
    assert git(other, "rev-parse", "HEAD") == mine and (other / "x").read_text() == "x\n"


def test_a_pin_not_on_main_is_refused(setup_install):
    t = setup_install
    git(t["upstream"], "checkout", "-q", "-b", "side", t["a"])
    (t["upstream"] / "f").write_text("side\n")
    git(t["upstream"], "commit", "-q", "-am", "side")
    side = git(t["upstream"], "rev-parse", "HEAD")
    git(t["upstream"], "checkout", "-q", "main")
    state(t, pins={"qq": side, "qqsync": S}, pins_at=time.time() + 5, checked=time.time())
    bootstrap.auto_update(t["depot"], ["status"])
    assert git(t["depot"], "rev-parse", "HEAD") == t["a"] and not t["ran"]
    assert "is not on quirq-ai/qq's main" in (t["tools"] / "auto-update.log").read_text()


def test_a_failed_qqsync_step_puts_qq_back(setup_install, monkeypatch):
    t = setup_install
    real = subprocess.run
    monkeypatch.setattr(bootstrap.subprocess, "run", lambda cmd, **kw: real(cmd, **kw) if cmd[0] == "git" else
                        subprocess.CompletedProcess(cmd, 1, "", "no network"))
    monkeypatch.setattr(bootstrap, "_sync_commit", lambda: S)
    state(t, pins={"qq": t["b"], "qqsync": "f" * 40}, pins_at=time.time() + 5, checked=time.time())
    bootstrap.auto_update(t["depot"], ["status"])
    assert git(t["depot"], "rev-parse", "HEAD") == t["a"], "back at the marker's commit"
    assert json.loads((t["tools"] / "auto-update").read_text())["qq"] == t["a"]
    assert not t["ran"] and "put qq back" in (t["tools"] / "auto-update.log").read_text()
