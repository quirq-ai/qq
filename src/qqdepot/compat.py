"""What differs between Linux and macOS on one side and Windows on the other, in one place.

Standard library only. bootstrap.py runs before qqdepot is installed and keeps its own copies of
`lock` and `venv_exe`; keep them in step with these.
"""
from __future__ import annotations

import errno
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import IO, NoReturn

WINDOWS = os.name == "nt"


def lock(file: IO) -> None:
    """Hold an exclusive lock on the open `file` until it is closed, waiting for it if needed."""
    if WINDOWS:
        import msvcrt
        while True:
            try:
                # Locks the first byte, which need not exist. LK_LOCK gives up after 10 tries
                # a second apart, so keep waiting like flock does.
                msvcrt.locking(file.fileno(), msvcrt.LK_LOCK, 1)
                return
            except OSError as e:
                if e.errno != errno.EDEADLOCK:   # held by another qq; anything else is a real error
                    raise
    import fcntl
    fcntl.flock(file, fcntl.LOCK_EX)


def venv_exe(env: Path, name: str) -> Path:
    """A console script or the interpreter of the virtual environment `env`."""
    return env / "Scripts" / f"{name}.exe" if WINDOWS else env / "bin" / name


def site_packages(env: Path) -> list[Path]:
    """The site-packages directories of the virtual environment `env`."""
    return [env / "Lib" / "site-packages"] if WINDOWS else sorted(env.glob("lib/*/site-packages"))


def replace_process(exe: Path, argv: list[str], env: dict[str, str] | None = None) -> NoReturn:
    """Run `exe` in place of this process. Windows has no exec: there os.exec* starts a new
    process and exits at once, so the shell prompt returns while it still runs. Run it as a
    child there instead, and exit with its code."""
    if WINDOWS:
        try:
            code = subprocess.run([str(exe), *argv[1:]], env=env).returncode
        except KeyboardInterrupt:
            code = 130
        sys.exit(code)
    if env is None:
        os.execv(exe, [str(exe), *argv[1:]])
    os.execve(exe, [str(exe), *argv[1:]], env)


def retried(action, *args) -> None:
    """Run action(*args), retried on Windows while antivirus or the search indexer still holds a
    file that was just written (WinError 5 or 32), as package installers do there."""
    for attempt in range(10):
        try:
            action(*args)
            return
        except PermissionError as e:
            if not WINDOWS or getattr(e, "winerror", None) not in (5, 32) or attempt == 9:
                raise
            time.sleep(0.1 * (attempt + 1))


def move(src: Path, dst: Path) -> None:
    """os.replace, retried on Windows (see retried)."""
    retried(os.replace, src, dst)


def is_link(path: Path) -> bool:
    """A symlink, or on Windows also a directory junction (what link_dir makes there)."""
    if path.is_symlink():
        return True
    if not WINDOWS:
        return False
    try:   # Path.is_junction is 3.12+; qq runs on 3.11 too
        return os.lstat(path).st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT
    except OSError:
        return False


def read_link(path: Path) -> Path:
    """Where a link made by link_dir points, without the \\\\?\\ prefix Windows may add."""
    target = os.readlink(path)
    return Path(target.removeprefix("\\\\?\\") if WINDOWS else target)


def link_dir(target: Path, link: Path) -> None:
    """Point the directory link `link` at the directory `target`, replacing a link already there.

    POSIX: a symlink, swapped in with one rename, so a reader never sees it missing. Windows: a
    directory junction, which needs no administrator rights or Developer Mode, unlike a symlink.
    A junction cannot be renamed over another, so the new one is made under a temporary name and
    the old one removed just before the rename: a reader can find it missing only between those
    two calls (qq run then uses PATH for it, and says so)."""
    if not WINDOWS:
        tmp = link.with_name(f".{link.name}.{os.getpid()}.tmp")
        tmp.unlink(missing_ok=True)
        tmp.symlink_to(target, target_is_directory=True)
        os.replace(tmp, link)
        return
    import _winapi
    tmp = link.with_name(f".{link.name}.{os.getpid()}.tmp")
    if is_link(tmp):
        retried(os.unlink, tmp)
    retried(_winapi.CreateJunction, str(target.resolve()), str(tmp))
    if is_link(link):
        retried(os.unlink, link)   # removes the junction only, never what it points at
    retried(os.rename, tmp, link)


def rmtree(path: Path) -> None:
    """shutil.rmtree that also removes read-only files, which Windows refuses to delete (qq marks
    store entries read only). What still cannot be removed is left, as shutil.rmtree's
    ignore_errors did here before: callers remove scratch or half-built trees, and a leftover
    is rebuilt or removed on the next run."""
    def retry(func, failed, _exc_info):
        try:
            os.chmod(failed, 0o700)
            func(failed)
        except OSError:
            pass
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)
