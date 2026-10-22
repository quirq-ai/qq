"""The qq bootstrap, run by bin/qq straight from a depot checkout, before anything is installed.

Standard library only. It builds a launcher environment from the checkout's committed
tree, once per checkout and commit, and runs its qq. The launcher then runs the version
the current repo pins (see pin.py).

Each launcher is built in place under a lock with a completion marker, so concurrent
first runs build it once and nobody uses or deletes a half-built one. It is built from
an export of the commit, not from the checkout itself, so the checkout stays untouched
(it may be read only) and no build leftovers leak into the launcher.

Auto-update (off unless qq-setup turned it on): qq-setup's option writes ~/qq-tools/auto-update,
naming the qq and qqsync commits it installed. With it, and only for setup's own checkout
(~/qq-tools/depot) while it is still at that commit with no local changes, at most once a day a
detached check reads qq-setup's pins.json (its reviewed commits) and records them; the command never
waits for it, so qq works offline. When a check found other commits, the next qq first moves the
checkout to the reviewed one (git fetch, then reset --keep, which refuses to touch local changes),
reinstalls qqsync if its commit moved and qqsync is still at the marker's, and runs the command on
the new qq. A checkout or qqsync installed any other way is left alone; nothing runs when CI or
QQ_PINNED is set. "Newest" is whatever setup's main says: if setup moves a pin back, qq follows it.
One update at a time (auto-update.lock); every interpreter it starts runs isolated (-I) from
~/qq-tools, so nothing in the folder qq was run from is imported.
"""
from __future__ import annotations

import errno
import hashlib
import io
import os
import shutil
import subprocess
import sys
import json
import re
import secrets
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

_SKIP = shutil.ignore_patterns(".git", "build", "*.egg-info", "__pycache__")


WINDOWS = os.name == "nt"


def qq_home() -> Path:
    # As pin.qq_home.
    if home := os.environ.get("QQ_HOME"):
        return Path(home)
    if WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "qq"
    cache = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(cache) / "qq"


# Copies of compat.lock (plus wait=False, for auto-update) and compat.venv_exe: this file runs
# before qqdepot is installed.
def _lock(file, wait: bool = True) -> None:
    """Hold an exclusive lock on the open `file` until it is closed. With wait=False, raise
    OSError at once when another process holds it."""
    if WINDOWS:
        import msvcrt
        while True:
            try:
                msvcrt.locking(file.fileno(), msvcrt.LK_LOCK if wait else msvcrt.LK_NBLCK, 1)
                return
            except OSError as e:
                if not wait or e.errno != errno.EDEADLOCK:
                    raise
    import fcntl
    fcntl.flock(file, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)


def _venv_exe(env: Path, name: str) -> Path:
    return env / "Scripts" / f"{name}.exe" if WINDOWS else env / "bin" / name


def head(depot: Path) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(depot), "rev-parse", "HEAD"],
                             check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip()


def export(depot: Path, commit: str | None, into: Path) -> Path:
    """A copy of the depot tree to install from: the commit if git knows it, else the files."""
    src = into / "depot"
    if commit:
        tar = subprocess.run(["git", "-C", str(depot), "archive", "--format=tar", commit],
                             check=True, capture_output=True).stdout
        with tarfile.open(fileobj=io.BytesIO(tar)) as archive:
            archive.extractall(src, filter="data")
    else:
        shutil.copytree(depot, src, ignore=_SKIP)
    return src


def ensure_launcher(depot: Path) -> Path:
    commit = head(depot)
    # TODO(expert): without git the key cannot see edits; a content hash would.
    key = hashlib.sha256(f"{depot}\0{commit or 'no-git'}".encode()).hexdigest()[:16]
    launchers = qq_home() / "launchers"
    target = launchers / key
    qq = _venv_exe(target, "qq")
    done = target / ".qq-installed"
    if done.is_file():
        return qq
    launchers.mkdir(parents=True, exist_ok=True)
    with (launchers / f".{key}.lock").open("w") as lock:
        _lock(lock)
        if done.is_file():
            return qq
        print(f"qq: setting up the launcher for {depot} in {target}", file=sys.stderr)
        shutil.rmtree(target, ignore_errors=True)
        try:
            with tempfile.TemporaryDirectory(dir=launchers, prefix=f".{key}.") as scratch:
                src = export(depot, commit, Path(scratch))
                subprocess.run([sys.executable, "-I", "-m", "venv", str(target)], check=True, cwd=launchers)
                # As pin.pip_install: PyPI only by hash from the lock, the rest with no index.
                pip = [str(_venv_exe(target, "python")), "-I", "-m", "pip", "--disable-pip-version-check", "--quiet"]
                lock = src / "src" / "qqdepot" / "locks" / "pypi.txt"
                for cmd in ([*pip, "install", "--require-hashes", "--no-deps", "--only-binary=:all:", "-r", str(lock)],
                            [*pip, "install", "--no-index", "--no-build-isolation", str(src)],
                            [*pip, "check"]):
                    subprocess.run(cmd, check=True, capture_output=True, cwd=launchers)
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise
        done.write_text(f"{depot} {commit or 'no-git'}\n")
    # TODO(expert): remove launchers of commits no checkout is at any more.
    return qq


PINS_URL = "https://raw.githubusercontent.com/quirq-ai/setup/refs/heads/main/pins.json"
PINS_SCHEMA = "qq-setup-pins/1"
QQ_REPO = "https://github.com/quirq-ai/qq"
SYNC_REPO = "https://github.com/quirq-ai/sync"
COMMIT = re.compile(r"[0-9a-f]{40}")
DAY = 24 * 3600
RETRY = 3600  # after a check or an update that failed
MAX_PINS = 4096
MAX_LOG = 64 * 1024
RAN_ENV = "QQ_AUTO_UPDATE_RAN"  # set on the re-run after an update, so it never loops
CHECK = "--auto-update-check"


def tools_dir() -> Path:
    return Path.home() / "qq-tools"


def _marker() -> tuple[dict, float] | None:
    """What qq-setup installed ({"qq": commit, "qqsync": commit}) and when it wrote that, or None."""
    path = tools_dir() / "auto-update"
    try:
        data = json.loads(path.read_text(encoding="utf-8")[:1000])
        written = path.stat().st_mtime
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not all(isinstance(data.get(k), str) and COMMIT.fullmatch(data[k])
                                             for k in ("qq", "qqsync")):
        return None
    return {"qq": data["qq"], "qqsync": data["qqsync"]}, written


def _write(path: Path, text: str) -> None:
    """Swap `text` into `path` whole, so a concurrent reader never sees half of it."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _state() -> dict:
    try:
        data = json.loads((tools_dir() / "auto-update.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(state: dict) -> None:
    try:
        _write(tools_dir() / "auto-update.json", json.dumps(state) + "\n")
    except OSError:
        pass


def _log(line: str) -> None:
    path = tools_dir() / "auto-update.log"
    try:
        if path.stat().st_size > MAX_LOG:
            path.unlink()
    except OSError:
        pass
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {line}\n")
    except OSError:
        pass


def _number(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


def get_pins(url: str = PINS_URL, timeout: float = 10.0) -> object:
    """qq-setup's pins.json: https only, no redirect, at most MAX_PINS bytes. Raises ValueError."""
    if not url.startswith("https://"):
        raise ValueError("the pins address is not https")
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "qq"})
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout) as resp:
            raw = resp.read(MAX_PINS + 1)
    except urllib.error.HTTPError as e:
        raise ValueError(f"pins.json answered HTTP {e.code}") from None
    except (urllib.error.URLError, OSError) as e:
        raise ValueError(f"pins.json could not be reached ({e})") from None
    if len(raw) > MAX_PINS:
        raise ValueError(f"pins.json is over {MAX_PINS} bytes")
    try:
        return json.loads(raw)
    except ValueError:
        raise ValueError("pins.json is not JSON") from None


def pins_from(data: object) -> dict:
    """qq's and qqsync's commits in pins.json. Only those fields are checked: the file may gain other
    tools' pins, and an installed qq must keep working when it does."""
    if not isinstance(data, dict) or data.get("schema") != PINS_SCHEMA:
        raise ValueError(f"pins.json is not {PINS_SCHEMA}")
    for k in ("qq", "qqsync"):
        if not isinstance(data.get(k), str) or not COMMIT.fullmatch(data[k]):
            raise ValueError(f"pins.json has no full commit for {k}")
    return {"qq": data["qq"], "qqsync": data["qqsync"]}


def check() -> int:
    """The background check: record qq-setup's reviewed commits. Never raises."""
    state = _state()
    try:
        state["pins"] = pins_from(get_pins())
        state["checked"] = state["pins_at"] = time.time()
        _log(f"check: setup's reviewed qq is {state['pins']['qq']}, qqsync {state['pins']['qqsync']}")
    except ValueError as e:
        state["checked"] = time.time() - DAY + RETRY  # try again in an hour
        _log(f"check failed: {e}")
    _save_state(state)
    return 0


def _spawn(cmd: list[str]) -> None:
    """Start `cmd` detached from this terminal, with no input or output."""
    # start_new_session has no effect on Windows: there the check gets its own process group and
    # no console, so a Ctrl+C in this terminal does not stop it.
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if WINDOWS else 0
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True, start_new_session=True, cwd=tools_dir(), creationflags=flags)


def _start_check(state: dict, depot: Path) -> None:
    """Record the check as started (so other commands do not start one too), then run it detached."""
    state["checked"] = time.time()
    _save_state(state)
    try:
        _spawn([sys.executable, "-I", str(Path(__file__).resolve()), CHECK, str(depot)])
    except OSError as e:
        _log(f"could not start the check: {e}")


ALLOW_PROTOCOL = "https"  # the only transport auto-update's git may use (tests use file)


def _git(depot: Path, *args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    """git on setup's checkout and nothing else: no GIT_* from the caller's environment (GIT_DIR,
    GIT_WORK_TREE, GIT_CONFIG_PARAMETERS... would point it elsewhere or change its config), https
    only, never a prompt, run from ~/qq-tools."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_ALLOW_PROTOCOL=ALLOW_PROTOCOL, GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1")
    return subprocess.run(["git", "-C", str(depot), *args], stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, errors="replace", timeout=timeout, env=env, cwd=tools_dir())


def _head(depot: Path) -> str | None:
    try:
        out = _git(depot, "rev-parse", "--verify", "HEAD")
    except (OSError, subprocess.TimeoutExpired):
        return None
    commit = out.stdout.strip()
    return commit if out.returncode == 0 and COMMIT.fullmatch(commit) else None


def _clean(depot: Path) -> bool:
    try:
        out = _git(depot, "status", "--porcelain", "--untracked-files=no", timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0 and not out.stdout.strip()


def _sync_commit() -> str | None:
    """The commit pip installed qqsync from, when it came from quirq-ai/sync by git; else None."""
    pattern = "Lib/site-packages" if WINDOWS else "lib/python*/site-packages"
    for record in (tools_dir() / "qqsync").glob(f"{pattern}/qqsync-*.dist-info/direct_url.json"):
        try:
            data = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        vcs = data.get("vcs_info") if isinstance(data, dict) else None
        if (isinstance(vcs, dict) and vcs.get("vcs") == "git"
                and str(data.get("url", "")).removesuffix(".git") == SYNC_REPO):
            commit = vcs.get("commit_id")
            return commit if isinstance(commit, str) and COMMIT.fullmatch(commit) else None
    return None


def update(depot: Path, pins: dict, marker: dict) -> bool:
    """Move setup's checkout to the reviewed qq commit (which must be on quirq-ai/qq's main), and
    qqsync to its commit when it is still setup's. Logged. False when a step failed: the checkout is
    then back at the marker's commit, or, if even that failed, the marker records where it is."""
    moved = False
    try:
        if pins["qq"] != marker["qq"]:
            for args in (("fetch", "--quiet", "--no-tags", QQ_REPO, "refs/heads/main"),
                         ("merge-base", "--is-ancestor", pins["qq"], "FETCH_HEAD"),
                         ("reset", "--quiet", "--keep", pins["qq"])):
                done = _git(depot, *args, timeout=60)
                if done.returncode:
                    why = ("is not on quirq-ai/qq's main" if args[0] == "merge-base"
                           else f"git {args[0]} failed\n{done.stderr.strip()[-2000:]}")
                    _log(f"update to qq {pins['qq']}: {why}")
                    return False
            moved = True
        if pins["qqsync"] != marker["qqsync"] and _sync_commit() == marker["qqsync"]:
            py = _venv_exe(tools_dir() / "qqsync", "python")
            done = subprocess.run([str(py), "-I", "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                                   "--force-reinstall", "--no-deps", f"qqsync @ git+{SYNC_REPO}@{pins['qqsync']}"],
                                  stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
                                  timeout=120, cwd=tools_dir())
            if done.returncode:
                _log(f"update to qqsync {pins['qqsync']}: failed\n{done.stderr.strip()[-2000:]}")
                _undo(depot, marker, pins, moved)
                return False
    except (OSError, subprocess.TimeoutExpired) as e:
        _log(f"update failed: {e}")
        _undo(depot, marker, pins, moved)
        return False
    _log(f"update to qq {pins['qq']}, qqsync {pins['qqsync']}: done")
    return True


def _undo(depot: Path, marker: dict, pins: dict, moved: bool) -> None:
    """After a failed qqsync step: put the checkout back at the marker's commit, so auto-update still
    owns it and tries again; if that fails too, record in the marker where the checkout is."""
    if not moved:
        return
    try:
        back = _git(depot, "reset", "--quiet", "--keep", marker["qq"]).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        back = False
    if back:
        _log(f"put qq back at {marker['qq']}")
        return
    try:
        _write(tools_dir() / "auto-update", json.dumps({"qq": pins["qq"], "qqsync": marker["qqsync"]}) + "\n")
        _log(f"qq stays at {pins['qq']}; qqsync is still {marker['qqsync']}")
    except OSError as e:
        _log(f"could not record qq at {pins['qq']} in the marker: {e}")


def auto_update(depot: Path, argv: list[str]) -> None:
    """Before a command: update first when a check found newer reviewed commits (then run the command
    again on the new qq), and start the daily check. Never raises; returns to go on with this qq."""
    if os.environ.pop(RAN_ENV, None) or os.environ.get("CI") or os.environ.get("QQ_PINNED"):
        return
    found = _marker()
    if found is None:
        return
    marker, written = found
    try:
        if depot.resolve() != (tools_dir() / "depot").resolve() or _head(depot) != marker["qq"] or not _clean(depot):
            return  # not setup's checkout, or changed since setup: leave it until setup's Update
        state = _state()
        pins = state.get("pins")
        try:
            pins = pins_from({"schema": PINS_SCHEMA, **pins}) if isinstance(pins, dict) else None
        except ValueError:
            pins = None
        # Only pins read after the marker was written: older ones may be behind what setup installed since.
        if (pins and pins != marker and _number(state.get("pins_at")) > written
                and not (state.get("failed") == pins and time.time() - _number(state.get("failed_at")) < RETRY)):
            with (tools_dir() / "auto-update.lock").open("a") as lock:
                try:
                    _lock(lock, wait=False)
                    now = _head(depot)
                except OSError:
                    now = None  # another qq is updating: run this one
                if now == pins["qq"] and pins["qq"] != marker["qq"]:
                    ok = True  # another qq updated it since this one started
                elif now == marker["qq"] and _clean(depot):
                    print(f"qq: updating to {pins['qq'][:7]}, setup's newest reviewed qq (auto-update is on; "
                          "this can take a minute)", file=sys.stderr, flush=True)
                    ok = update(depot, pins, marker)
                    state = _state()
                    if ok:
                        _write(tools_dir() / "auto-update", json.dumps(pins) + "\n")
                        state.pop("failed", None)
                        state.pop("failed_at", None)
                    else:
                        state["failed"], state["failed_at"] = pins, time.time()
                        print(f"qq: could not update (details in {tools_dir() / 'auto-update.log'}); "
                              "running this qq, will try again in an hour", file=sys.stderr)
                    _save_state(state)
                else:
                    ok = False
            if ok:
                sys.stdout.flush()
                sys.stderr.flush()
                env = {**os.environ, RAN_ENV: "1"}
                if WINDOWS:
                    # No exec (see main), and a .cmd run from here would need cmd's quoting: run the
                    # new checkout's bootstrap as qq.cmd does, with this interpreter, and pass its code on.
                    bootstrap = depot / "src" / "qqdepot" / "bootstrap.py"
                    sys.exit(subprocess.run([sys.executable, "-I", str(bootstrap), str(depot), *argv], env=env).returncode)
                qq = depot / "bin" / "qq"
                os.execve(qq, [str(qq), *argv], env)
        if time.time() - _number(state.get("checked")) >= DAY:
            _start_check(state, depot)
    except Exception as e:  # noqa: BLE001 - auto-update never stops a command
        _log(f"skipped: {type(e).__name__}: {e}")


def main(argv: list[str]) -> int:
    if argv[:1] == [CHECK]:
        return check()
    depot = Path(argv[0])
    auto_update(depot, argv[1:])
    try:
        qq = ensure_launcher(depot)
    except (OSError, subprocess.CalledProcessError) as e:
        detail = getattr(e, "stderr", None)
        detail = detail.decode(errors="replace") if isinstance(detail, bytes) else detail or ""
        print(f"qq: cannot set up the launcher from {depot}: {e}\n{detail.strip()}".rstrip(), file=sys.stderr)
        return 1
    if WINDOWS:   # no exec on Windows (see compat.replace_process): run it and pass its code on
        try:
            return subprocess.run([str(qq), *argv[1:]]).returncode
        except KeyboardInterrupt:
            return 130
    os.execv(qq, [str(qq), *argv[1:]])
    return 1  # not reached


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
