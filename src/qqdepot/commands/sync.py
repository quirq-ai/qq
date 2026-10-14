"""qq sync and qq fetch: get a repo's pinned toolchains and dependencies.

    qq sync           in a repo: fetch every pin in infra/repo.toml and link it under .qq/
    qq fetch URL [DIR]  clone a repo, then qq sync in it
    qq sync project ... keep working clones up to date and manage who has access (qqsync.project)

Toolchains land at <repo>/.qq/toolchains/<name> and dependencies at <repo>/.qq/deps/<name>,
each a link into the shared store (see store.py), so `qq build` and `qq test` find them.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from qqsync.errors import ManifestError
from qqsync.manifest import load
from qqsync.project import cli as project

from qqdepot import store
from qqdepot.commands.build import TOOLCHAINS, repo_root
from qqdepot.pin import MANIFEST, git_env

DEPS = Path(".qq/deps")
RECORD = Path(".qq/sync.json")
SECTIONS = (("toolchains", TOOLCHAINS), ("deps", DEPS))


def _real_dir(root: Path, rel: Path) -> Path:
    """<root>/<rel> as a real directory inside the repo, made if missing.

    A repo can commit `.qq` or `.qq/deps` as a symlink. Followed, it would make qq sync write links,
    delete links and write sync.json wherever it points, so any symlink on the way is refused.
    TODO(expert): open each level with O_NOFOLLOW (dir fds) to close the check-then-use race.
    """
    path = root
    for part in rel.parts:
        path = path / part
        if path.is_symlink():
            raise store.FetchError(f"{path} is a symlink; qq sync writes only inside the repo. "
                                   "Remove it (and any committed .qq) and run qq sync again")
        if not path.exists():
            path.mkdir()
        elif not path.is_dir():
            raise store.FetchError(f"{path} is not a directory; move it away and run qq sync again")
    if not path.resolve().is_relative_to(root.resolve()):
        raise store.FetchError(f"{path} resolves outside {root}; refusing to write there")
    return path


STORE_ENTRY = re.compile(r"[a-z0-9]+-[0-9a-f]{16,}")   # store.py's <algo>-<hex> entry names


def _ours(link: Path) -> bool:
    """True for a link qq sync made: one into a store entry, under any QQ_HOME (it may change)."""
    if not link.is_symlink():
        return False
    target = Path(os.readlink(link))
    return target.parent.name == "store" and STORE_ENTRY.fullmatch(target.name) is not None


def _link(target: Path, link: Path) -> None:
    """Point `link` at `target`, replacing what was there in one step. `link.parent` must be a
    directory _real_dir returned."""
    if link.exists() and not link.is_symlink():
        raise store.FetchError(f"{link} is not a link qq made; move it away and run qq sync again")
    tmp = link.with_name(f".{link.name}.{os.getpid()}.tmp")
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(target, target_is_directory=True)
    os.replace(tmp, link)


def sync(root: Path) -> list[dict]:
    """Fetch and link every pin of the repo at `root`. Returns what was synced."""
    manifest = load(root / MANIFEST)
    synced = []
    for section, base in SECTIONS:
        pins = manifest.get(section, {})
        for name in pins:
            artifact = store.resolve(manifest, section, name)
            entry = store.ensure(artifact)
            _link(entry, _real_dir(root, base) / name)
            synced.append({"section": section, "name": name, "digest": artifact.digest, "path": str(entry)})
            print(f"{base / name}: {artifact.digest[:19]}…")
        # A pin dropped from the manifest must not linger and be built with.
        # Only links qq made (into the store) are removed, so nothing else is ever deleted.
        if (root / base).exists() or (root / base).is_symlink():
            for stale in _real_dir(root, base).iterdir():
                if stale.name not in pins and not stale.name.startswith(".") and _ours(stale):
                    stale.unlink()
    record = _real_dir(root, RECORD.parent) / RECORD.name
    if record.is_symlink():
        raise store.FetchError(f"{record} is a symlink; remove it and run qq sync again")
    # A new file renamed over the record: writing in place would go through a hard link
    # (a tarball or a local tool can make one) to a file outside the repo.
    fd, tmp = tempfile.mkstemp(dir=record.parent, prefix=f".{RECORD.name}.")
    try:
        with os.fdopen(fd, "w") as out:
            out.write(json.dumps(synced, indent=2) + "\n")
        os.chmod(tmp, 0o666 & ~_umask())
        os.replace(tmp, record)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return synced


def _umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


def run_sync(args: argparse.Namespace) -> int:
    root = repo_root(Path.cwd())
    if root is None:
        print(f"qq: no {MANIFEST} here or above {Path.cwd()}; run qq sync inside a repo", file=sys.stderr)
        return 2
    try:
        sync(root)
    except (ManifestError, store.FetchError, OSError) as e:
        print(f"qq: {e}", file=sys.stderr)
        return 1
    return 0


def run_fetch(args: argparse.Namespace) -> int:
    directory = Path(args.directory or Path(args.url.rstrip("/")).name.removesuffix(".git"))
    if directory.exists():
        print(f"qq: {directory} already exists; run qq sync inside it instead", file=sys.stderr)
        return 1
    try:
        subprocess.run(["git", "clone", "--quiet", "--", args.url, str(directory)], check=True, env=git_env())
    except (OSError, subprocess.CalledProcessError) as e:
        print(f"qq: cannot clone {args.url}: {e}", file=sys.stderr)
        return 1
    if not (directory / MANIFEST).is_file():
        print(f"qq: cloned {args.url}, which has no {MANIFEST}; nothing to sync", file=sys.stderr)
        return 1
    try:
        sync(directory.resolve())
    except (ManifestError, store.FetchError, OSError) as e:
        print(f"qq: {e}\nqq: cloned into {directory}; fix the problem and run qq sync there", file=sys.stderr)
        return 1
    return 0


def register(sub) -> None:
    s = sub.add_parser("sync", help="fetch the repo's pinned toolchains and dependencies",
                       description="Fetch every pin in infra/repo.toml, check it against its digest,"
                                   " and link it under <repo>/.qq.")
    s.set_defaults(run=run_sync)
    # The other kind of sync: clones someone edits, which follow a branch. Bare `qq sync` stays the pins.
    project.register(s.add_subparsers(dest="sync_command", metavar="COMMAND"), prefix="qq")
    f = sub.add_parser("fetch", help="clone a repo, then qq sync in it")
    f.add_argument("url", help="the repo to clone")
    f.add_argument("directory", nargs="?", help="where to clone it (default: its name)")
    f.set_defaults(run=run_fetch)
