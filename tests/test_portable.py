"""qq sync on every platform qq supports: Linux, macOS and Windows.

Standard library and qqsync only, so the presubmit's macOS and Windows jobs run this file on
their own runners (test_sync.py needs recipes and a POSIX shell). On Windows the store links are
directory junctions and store entries are read only through the read-only attribute.
"""
import hashlib
import io
import os
import sys
import tarfile
from pathlib import Path

import pytest

from qqdepot import cli, compat, store


def payload(text: str = "hello from the toolchain\n") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = text.encode()
        info = tarfile.TarInfo("share/hello.txt")
        info.size, info.mode = len(data), 0o644
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def write_repo(root: Path, pins: dict[str, bytes]) -> Path:
    """A repo whose manifest pins each blob as a file:// dependency."""
    (root / "infra").mkdir(parents=True, exist_ok=True)
    lines = ['schema = "quirq-repo/1"', ""]
    for name, blob in pins.items():
        path = root.parent / f"{name}.tar.gz"
        path.write_bytes(blob)
        lines += [f"[deps.{name}]", f'source = "{path.resolve().as_uri()}"',
                  f'digest = "sha256:{hashlib.sha256(blob).hexdigest()}"', ""]
    lines += ["[[targets]]", 'name = "app"', 'kind = "pytest"', ""]
    (root / "infra" / "repo.toml").write_text("\n".join(lines))
    return root


def test_link_dir_points_at_and_replaces_a_directory(tmp_path):
    a, b, link = tmp_path / "a", tmp_path / "b", tmp_path / "link"
    a.mkdir()
    b.mkdir()
    (a / "f").write_text("a")
    (b / "f").write_text("b")
    compat.link_dir(a, link)
    assert compat.is_link(link) and (link / "f").read_text() == "a"
    assert compat.read_link(link).resolve() == a.resolve()
    compat.link_dir(b, link)
    assert (link / "f").read_text() == "b"
    os.unlink(link)
    assert not link.exists() and (b / "f").read_text() == "b"   # the target is untouched


def test_is_link_is_false_for_plain_paths(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "f").write_text("")
    assert not compat.is_link(tmp_path / "d")
    assert not compat.is_link(tmp_path / "f")
    assert not compat.is_link(tmp_path / "missing")


def test_rmtree_removes_read_only_files(tmp_path):
    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    f = tree / "sub" / "f"
    f.write_text("x")
    f.chmod(0o444)
    compat.rmtree(tree)
    assert not tree.exists()


def test_lock_can_be_taken_again_after_release(tmp_path):
    for _ in range(2):
        with (tmp_path / ".lock").open("w") as f:
            compat.lock(f)


def test_venv_paths_match_the_platform(tmp_path):
    exe = compat.venv_exe(tmp_path, "qq")
    assert exe == (tmp_path / "Scripts" / "qq.exe" if compat.WINDOWS else tmp_path / "bin" / "qq")


def test_sync_links_pins_reuses_them_and_drops_removed_ones(tmp_path, monkeypatch, capsys):
    repo = write_repo(tmp_path / "product", {"tool": payload(), "other": payload("other\n")})
    monkeypatch.chdir(repo)
    assert cli.main(["sync"]) == 0
    link = repo / ".qq" / "deps" / "tool"
    assert compat.is_link(link)
    assert (link / "share" / "hello.txt").read_text() == "hello from the toolchain\n"
    entry = compat.read_link(link)
    assert entry.parent == store.store_dir()

    # Sealed: a write into the shared entry is refused (root ignores write bits on POSIX).
    if compat.WINDOWS or os.geteuid() != 0:
        with pytest.raises(PermissionError):
            (entry / "share" / "hello.txt").write_text("changed")

    assert cli.main(["sync"]) == 0   # reuses the checked entry
    assert (link / "share" / "hello.txt").read_text() == "hello from the toolchain\n"

    write_repo(repo, {"tool": payload()})   # drop `other`
    assert cli.main(["sync"]) == 0
    assert not os.path.lexists(repo / ".qq" / "deps" / "other")
    assert compat.is_link(link)


def test_sync_refuses_bytes_that_do_not_match_the_pin(tmp_path, monkeypatch, capsys):
    repo = write_repo(tmp_path / "product", {"tool": payload()})
    (tmp_path / "tool.tar.gz").write_bytes(payload("tampered\n"))
    monkeypatch.chdir(repo)
    assert cli.main(["sync"]) == 1
    assert "refusing to use it" in capsys.readouterr().err
    assert not os.path.lexists(repo / ".qq" / "deps" / "tool")


def test_commands_that_need_a_posix_shell_say_so_on_windows(monkeypatch, capsys):
    monkeypatch.setattr(compat, "WINDOWS", True)
    assert cli.main(["build"]) == 2
    assert "does not run on Windows yet" in capsys.readouterr().err


@pytest.mark.skipif(not compat.WINDOWS, reason="Windows only: junctions")
def test_windows_store_links_are_junctions(tmp_path, monkeypatch):
    repo = write_repo(tmp_path / "product", {"tool": payload()})
    monkeypatch.chdir(repo)
    assert cli.main(["sync"]) == 0
    link = repo / ".qq" / "deps" / "tool"
    assert not link.is_symlink() and compat.is_link(link)


def test_manifest_text_is_valid(tmp_path):
    # Guards the helper above: the manifest it writes validates on every platform.
    from qqsync.manifest import load
    repo = write_repo(tmp_path / "product", {"tool": payload()})
    assert load(repo / "infra" / "repo.toml")["deps"]["tool"]["source"].startswith("file:")


def test_installed_gate_is_found_without_qq_gate(monkeypatch):
    # The real path, not the QQ_GATE override every change test uses: an installed gate under
    # QQ_HOME is reused, with the venv layout of this platform.
    from qqdepot import gate
    monkeypatch.delenv(gate.GATE_ENV, raising=False)
    target = store.qq_home() / "gate" / gate.GATE_COMMIT[:16]
    target.mkdir(parents=True)
    (target / ".qq-installed").write_text("")
    exe, config = gate.ensure()
    assert exe == compat.venv_exe(target / "venv", "qqgate")
    assert config == target / "infra-config"


@pytest.mark.parametrize("names", [["Bin/x", "bin/x"], ["a/B", "a/b"]])
def test_archive_names_that_differ_only_in_case_are_refused_where_case_is_folded(tmp_path, monkeypatch, names):
    monkeypatch.setattr(store, "FOLD_CASE", True)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for n in names:
            info = tarfile.TarInfo(n)
            tar.addfile(info, io.BytesIO(b""))
    buf.seek(0)
    with tarfile.open(fileobj=buf) as tar, pytest.raises(store.FetchError, match="appears twice"):
        store._check_members(tar, store.Artifact.of("t", "file:///x", "sha256:" + "0" * 64))


def test_a_link_spelled_in_another_case_still_counts_where_case_is_folded(monkeypatch):
    monkeypatch.setattr(store, "FOLD_CASE", True)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        link = tarfile.TarInfo("Out")
        link.type, link.linkname = tarfile.SYMTYPE, "."
        tar.addfile(link)
        tar.addfile(tarfile.TarInfo("out/x"), io.BytesIO(b""))
    buf.seek(0)
    with tarfile.open(fileobj=buf) as tar, pytest.raises(store.FetchError, match="sits under a symlink"):
        store._check_members(tar, store.Artifact.of("t", "file:///x", "sha256:" + "0" * 64))


def test_bootstrap_lock_without_wait_fails_while_another_holds_it(tmp_path):
    # Auto-update's lock: a second qq must not wait for an update another qq is running.
    from qqdepot import bootstrap
    path = tmp_path / "auto-update.lock"
    with path.open("a") as first:
        bootstrap._lock(first, wait=False)
        with path.open("a") as second, pytest.raises(OSError):
            bootstrap._lock(second, wait=False)
    with path.open("a") as again:
        bootstrap._lock(again, wait=False)   # free once the first is closed


def test_case_folding_is_read_from_the_file_system(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "FOLD_CASE", None)
    (tmp_path / "probe").write_text("")
    folds = (tmp_path / "PROBE").exists()
    assert store._folds_case(tmp_path) == (folds and (compat.WINDOWS or sys.platform == "darwin"))
    assert [p.name for p in tmp_path.iterdir()] == ["probe"]   # the probe file is gone


def test_auto_update_finds_qqsync_in_this_platforms_venv_layout(tmp_path, monkeypatch):
    from qqdepot import bootstrap
    monkeypatch.setattr(bootstrap, "tools_dir", lambda: tmp_path)
    commit = "a" * 40
    site = (tmp_path / "qqsync" / "Lib" / "site-packages" if compat.WINDOWS
            else tmp_path / "qqsync" / "lib" / "python3.14" / "site-packages")
    info = site / "qqsync-0.1.dist-info"
    info.mkdir(parents=True)
    (info / "direct_url.json").write_text(
        '{"url": "https://github.com/quirq-ai/sync", "vcs_info": {"vcs": "git", "commit_id": "%s"}}' % commit)
    assert bootstrap._sync_commit() == commit
