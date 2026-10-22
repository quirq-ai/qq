"""Fetch pinned toolchains and dependencies into a content-addressed store, checked by digest.

A pin (quirq-repo/1) is a source and a digest. The source's scheme picks the fetcher:

    https://..., file://...        an artifact; its bytes must hash to the sha256 digest
    oci://REGISTRY/REPO@MANIFEST   an artifact layer in an OCI registry (the toolchains repo
                                   publishes here); the source must name the image manifest
                                   (qqsync refuses one that does not) and the sha256 digest
                                   names the layer blob in it
    any git URL + git:<commit>     a source tree at that commit

Each pin is fetched once per machine into $QQ_HOME/store/<algo>-<hex>, unpacked when it is a
tarball, and never changed after: the entry is made read only, its tree (paths, kinds, sizes,
times, link targets) is recorded in TREE_RECORD inside it, and every reuse checks the tree
again, so a toolchain something wrote into (a build, a package manager) is refused instead of
shared. Remove a refused entry with
`chmod -R u+w ENTRY && rm -rf ENTRY` (on Windows, in cmd: `attrib -r ENTRY\\* /s /d` then
`rmdir /s /q ENTRY`);
the next qq sync fetches it again.

Pins are resolved, fetched (https, file, oci) and verified (every scheme, git checkouts included)
by qqsync.pins, so depot only adds the git checkout and unpacking.
A half-fetched entry is never visible: it is built in a scratch directory and renamed into place.
TODO(expert): the record sits beside the tree it describes, so it catches accidents and stray
writes, not someone who rewrites both with the user's own rights.
"""
from __future__ import annotations

import hashlib
import os
import posixpath
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from qqsync import pins

from qqdepot import compat
from qqdepot.pin import git_env, qq_home


class FetchError(Exception):
    """A pin cannot be fetched or does not match its digest."""


@dataclass(frozen=True)
class Artifact:
    """One thing to fetch: a manifest pin, resolved for this platform by qqsync."""

    pin: pins.Pin

    @classmethod
    def of(cls, name: str, source: str, digest: str, section: str = "deps") -> Artifact:
        return cls(pins.Pin(section, name, None, source, digest))

    name = property(lambda self: self.pin.label)
    source = property(lambda self: self.pin.source)
    digest = property(lambda self: self.pin.digest)
    algo = property(lambda self: self.pin.algorithm)
    value = property(lambda self: self.pin.digest.partition(":")[2])


def resolve(manifest: dict, section: str, name: str) -> Artifact:
    """The artifact `section.name` pins for this machine's platform (qqsync.pins.find_pin)."""
    try:
        return Artifact(pins.find_pin(manifest, section, name))
    except pins.PinError as e:
        raise FetchError(str(e)) from None


def store_dir() -> Path:
    return qq_home() / "store"


def ensure(artifact: Artifact) -> Path:
    """The store entry for `artifact`, fetching it first if this machine does not have it."""
    store = store_dir()
    entry = store / f"{artifact.algo}-{artifact.value}"
    if _usable(entry):   # the common case, without the lock
        return entry
    store.mkdir(parents=True, exist_ok=True)
    # One qq at a time fetches, replaces or removes an entry; the others wait and reuse it.
    with (store / f".{entry.name[:40]}.lock").open("w") as lock:
        compat.lock(lock)
        if _usable(entry):
            return entry
        if entry.is_dir() and not entry.is_symlink():
            # Made by a qq from before entries were recorded: nothing to check it against.
            _remove(entry, store)
        elif entry.is_symlink() or entry.exists():
            raise FetchError(f"{entry} is not a store entry qq made; remove it and run qq sync again")
        with tempfile.TemporaryDirectory(dir=store, prefix=f".{artifact.algo}-{artifact.value[:16]}.") as scratch:
            scratch = Path(scratch)
            staged = scratch / "entry"
            if artifact.algo == "git":
                _fetch_git(artifact, staged)
            else:
                blob = _fetch_blob(artifact, scratch / "blob")
                _unpack(blob, staged, artifact)
            try:
                _seal(staged, artifact)
                # A directory moves to a new parent only while writable (its ".." changes),
                # so the top is made read only once it is in place.
                compat.move(staged, entry)
                entry.chmod(entry.stat().st_mode & ~0o222)
            except OSError as e:
                _unseal(scratch)   # so the scratch directory can be removed without root
                raise FetchError(f"{artifact.name}: cannot store {entry}: {e}") from None
    return entry


TREE_RECORD = ".qq-tree"
FOLD_CASE: bool | None = None   # None: ask the file system the store is on (tests set it)


def _folds_case(directory: Path) -> bool:
    """Whether names that differ only in case are one file in `directory`. macOS and Windows
    usually fold case, but a volume can be made case sensitive; Linux is taken not to."""
    if FOLD_CASE is not None:
        return FOLD_CASE
    if not (compat.WINDOWS or sys.platform == "darwin"):
        return False
    fd, probe = tempfile.mkstemp(dir=directory, prefix=".qq-case-")
    os.close(fd)
    try:
        return (directory / Path(probe).name.upper()).exists()
    finally:
        os.unlink(probe)


def _tree(root: Path, content: bool = False) -> str:
    """One sha256 over every path in `root`: its kind, executable bit and link target, and a
    file's size and modification time, or with `content` its bytes. The stat form is cheap
    enough for every reuse and changes with any ordinary write; the content form settles it
    when only times moved (a cache restore or copy that drops nanoseconds).
    Write bits are left out (qq clears them itself), and so are `__pycache__` directories,
    which the interpreter qq runs on writes into its own install when it runs as root, where write
    bits stop nothing.
    TODO(expert): a same-size write with its time reset passes the stat form."""
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for name in sorted(dirnames + filenames):
            path = Path(dirpath, name)
            rel = path.relative_to(root).as_posix()
            if rel == TREE_RECORD:
                continue
            st = path.lstat()
            if stat.S_ISLNK(st.st_mode):
                kind, body = "l", os.readlink(path)
            elif stat.S_ISDIR(st.st_mode):
                kind, body = "d", ""
            elif stat.S_ISREG(st.st_mode):
                kind = "x" if st.st_mode & 0o111 else "f"
                body = _file_sha256(path) if content else f"{st.st_size}:{st.st_mtime_ns}"
            else:
                kind, body = "?", ""
            h.update(f"{kind} {len(rel)}:{rel} {len(body)}:{body}\n".encode(errors="surrogateescape"))
    return h.hexdigest()


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _seal(staged: Path, artifact: Artifact) -> None:
    """Make every file and directory read only (links have no mode), then record the tree."""
    record = staged / TREE_RECORD
    if record.exists() or record.is_symlink():
        raise FetchError(f"{artifact.name}: the artifact has its own {TREE_RECORD}, which qq reserves")
    record.write_text("")
    for dirpath, dirnames, filenames in os.walk(staged, topdown=False):
        for name in filenames:
            path = Path(dirpath, name)
            if not path.is_symlink() and name != TREE_RECORD:
                path.chmod(path.stat().st_mode & ~0o222)
    record.write_text(f"stat {_tree(staged)}\ncontent {_tree(staged, content=True)}\n")
    record.chmod(0o444)
    for dirpath, dirnames, filenames in os.walk(staged, topdown=False):
        for name in dirnames:
            path = Path(dirpath, name)
            if not path.is_symlink():
                path.chmod(path.stat().st_mode & ~0o222)


def _unseal(root: Path) -> None:
    """Write bits back on every directory under `root`, so it can be deleted."""
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            path = Path(dirpath, name)
            if not path.is_symlink():
                path.chmod(0o700)


def _usable(entry: Path) -> bool:
    """True for an entry that still matches its record, False for none or one with no record (an
    older qq made it). A changed entry is an error, never silently used."""
    if entry.is_symlink() or not entry.is_dir():
        return False
    try:
        record = dict(line.split(" ", 1) for line in (entry / TREE_RECORD).read_text().splitlines())
        same = (_tree(entry) == record.get("stat")
                or _tree(entry, content=True) == record.get("content"))   # only times moved
    except FileNotFoundError:
        return False
    except (OSError, ValueError) as e:
        raise FetchError(f"cannot check {entry}: {e}") from None
    if not same:
        raise FetchError(f"{entry} changed after qq fetched it, so it is not shared any more; "
                         f"remove it ({_remove_command(entry)}) and run qq sync again")
    return True


def _remove_command(entry: Path) -> str:
    """The shell command that deletes a sealed entry on this platform."""
    if compat.WINDOWS:
        return f'in cmd: attrib -r "{entry}\\*" /s /d && rmdir /s /q "{entry}"'
    return f"chmod -R u+w {entry} && rm -rf {entry}"


def _remove(entry: Path, store: Path) -> None:
    """Move an entry aside and delete it, write bits back first (a sealed tree is read only)."""
    aside = Path(tempfile.mkdtemp(dir=store, prefix=f".old-{entry.name[:24]}."))
    try:
        entry.chmod(0o700)   # moving a directory to a new parent rewrites its ".."
        compat.move(entry, aside / "entry")
        _unseal(aside)
    except OSError as e:
        raise FetchError(f"cannot replace {entry}, made by an older qq: {e}; remove it and run qq sync again") from None
    finally:
        compat.rmtree(aside)


def _fetch_blob(artifact: Artifact, path: Path) -> Path:
    """Download the artifact's bytes to `path` with qqsync.pins.fetch, which checks them against
    the pin (https://, file:// and oci:// registry layers)."""
    try:
        pins.fetch(artifact.pin, path)
    except pins.PinError as e:
        scheme = urllib.parse.urlparse(artifact.source).scheme
        hint = "" if scheme in ("https", "file", "oci") else "; a git source needs digest = \"git:<commit>\""
        raise FetchError(f"{e}{hint}") from None
    return path


def _check_members(archive: tarfile.TarFile, artifact: Artifact, fold: bool | None = None) -> None:
    """Containment that does not rest on the interpreter's own tarfile: its "data" filter had
    bypasses until 3.11.13, 3.12.11 and 3.13.4 (symlink chains), and qq runs on older system
    interpreters too.

    Every member is a plain relative path; no member sits under a symlink member, so a link
    cannot move where later members land; a link's target, resolved through the archive's other
    links the way the filesystem will, stays inside the archive; and a hard link names, as
    written, a regular file of the archive.
    """
    def inside(path: str) -> bool:
        norm = posixpath.normpath(path)
        return not (norm == ".." or norm.startswith("../") or posixpath.isabs(norm))

    # Where the file system ignores case (macOS and Windows by default), "Bin/x" lands in "bin/x",
    # so names are compared case-folded there.
    key = str.casefold if (FOLD_CASE if fold is None else fold) else str
    links, files = {}, set()
    for m in archive.getmembers():
        if posixpath.isabs(m.name) or ".." in m.name.split("/") or not inside(m.name):
            raise FetchError(f"{artifact.name}: archive member {m.name!r} leaves the archive")
        name = posixpath.normpath(m.name)
        if key(name) in links or key(name) in files:   # a later member would replace what was checked
            raise FetchError(f"{artifact.name}: archive member {m.name!r} appears twice")
        parts = name.split("/")
        if any(key("/".join(parts[:i])) in links for i in range(1, len(parts))):
            raise FetchError(f"{artifact.name}: archive member {m.name!r} sits under a symlink")
        if m.issym():
            # Both readings must stay inside: the kernel's (resolved through the archive's links,
            # below) and the text's, since some tarfile filters rewrite targets lexically.
            if not inside(posixpath.join(posixpath.dirname(name), m.linkname)):
                raise FetchError(f"{artifact.name}: symlink {m.name!r} points outside the archive")
            links[key(name)] = m.linkname
        elif m.islnk():
            # tarfile links to the target path as written, so ".." through a symlink would count.
            if ".." in m.linkname.split("/") or key(posixpath.normpath(m.linkname)) not in files:
                raise FetchError(f"{artifact.name}: hard link {m.name!r} does not name a file before it")
        elif m.isfile():
            files.add(key(name))
    for name in links:
        if _escapes(name, links, key):
            raise FetchError(f"{artifact.name}: symlink {name!r} points outside the archive")


def _escapes(name: str, links: dict[str, str], key=str) -> bool:
    """Whether the symlink `name` resolves outside the archive root. Resolves like the kernel,
    one component at a time, following the archive's links as it meets them, so a ".." after
    a link to "." climbs from where that link really points, not from its text."""
    at = posixpath.dirname(name).split("/") if posixpath.dirname(name) else []
    todo, hops = links[name].split("/"), 0
    if posixpath.isabs(links[name]):
        return True
    while todo:
        part = todo.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            if not at:
                return True
            at.pop()
            continue
        at.append(part)
        target = links.get(key("/".join(at)))
        if target is not None:
            hops += 1
            if hops > 40 or posixpath.isabs(target):   # a loop, or a link out by an absolute path
                return True
            at.pop()
            todo = target.split("/") + todo
    return False


def _unpack(blob: Path, into: Path, artifact: Artifact) -> None:
    if tarfile.is_tarfile(blob):
        try:
            with tarfile.open(blob) as archive:
                _check_members(archive, artifact, fold=_folds_case(into.parent))
                # "data" refuses absolute paths, links that leave `into` and device files, and keeps
                # executable bits and in-tree symlinks, which is all a toolchain needs.
                archive.extractall(into, filter="data")
        except (tarfile.TarError, OSError) as e:
            raise FetchError(f"{artifact.name}: cannot unpack {artifact.source}: {e}") from None
    else:
        into.mkdir()
        name = Path(urllib.parse.urlparse(artifact.source).path).name or artifact.name
        shutil.copy2(blob, into / name)


def _fetch_git(artifact: Artifact, into: Path) -> None:
    if artifact.source.startswith("-"):
        raise FetchError(f"{artifact.name}: {artifact.source!r} is not a repository URL")
    env = git_env()

    def git(*args: str) -> None:
        try:
            # core.autocrlf=false: the same bytes on every machine (Git for Windows defaults to
            # checking text out with CRLF).
            subprocess.run(["git", "-C", str(into), "-c", "core.autocrlf=false", *args],
                           check=True, capture_output=True, text=True, env=env)
        except subprocess.CalledProcessError as e:
            raise FetchError(f"{artifact.name}: git {' '.join(args)} failed: {e.stderr.strip()}") from None
        except OSError as e:
            raise FetchError(f"{artifact.name}: cannot run git: {e}") from None

    into.mkdir()
    git("init", "-q", f"--object-format={'sha256' if len(artifact.value) == 64 else 'sha1'}")
    git("fetch", "-q", "--depth=1", "--end-of-options", artifact.source, artifact.value)
    git("checkout", "-q", "--detach", "FETCH_HEAD")
    try:
        pins.verify_checkout(artifact.pin, into)
    except pins.PinError as e:
        raise FetchError(str(e)) from None
