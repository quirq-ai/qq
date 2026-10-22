"""Version pinning: run the qq version the current repo pins in infra/repo.toml.

The pin is the manifest's `[qq]` table (schema quirq-repo/1, read only through qqsync):

    [qq]
    version = "0.1.0"                  # installs the qq tag v0.1.0
    source = "https://..."             # optional: where to get exactly these bytes
    digest = "git:<commit>"            # or sha256:<archive digest>

Each pinned version gets its own environment under $QQ_HOME/versions, made once and
reused. Before first use the installed qq must report the pinned version (and, for a
git: digest, have been installed from that commit), so a tag or archive that holds
something else is an error, not a silent mismatch.

A pin installs only from the depot ($QQ_DEPOT_URL, default quirq-ai/qq) or from a
source the user lists in $QQ_TRUSTED_SOURCES, and a git: commit must be on main or a v*
tag there: a pull request that edits the manifest cannot make `qq status` run code from
a host, or a fork, of its choosing.
TODO(suraj): protect v* tags in quirq-ai/qq (a version-only pin trusts the tag), and
bump __version__ with each release so a version names one commit.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from qqsync.errors import ManifestError
from qqsync.manifest import load

from qqdepot import __version__, compat

MANIFEST = Path("infra/repo.toml")
# Set in the environment of a pinned qq, so it runs instead of dispatching again.
PINNED_ENV = "QQ_PINNED"
DEFAULT_DEPOT_URL = "https://github.com/quirq-ai/qq"


class PinError(Exception):
    """The pinned qq cannot be found, installed or verified."""


@dataclass(frozen=True)
class Pin:
    version: str
    source: str | None = None
    digest: str | None = None
    manifest: Path | None = None

    @property
    def key(self) -> str:
        """The directory name of this pin's environment under $QQ_HOME/versions."""
        if not self.digest:
            return self.version
        algo, _, value = self.digest.partition(":")
        return f"{self.version}-{algo}-{value[:16]}"


def qq_home() -> Path:
    if home := os.environ.get("QQ_HOME"):
        return Path(home).resolve()   # links into the store must not depend on the cwd
    if compat.WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "qq"
    cache = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(cache) / "qq"


# The caller's GIT_* that only say how to reach a remote (keys, proxies, CAs, prompts).
# Any other GIT_* is dropped: GIT_DIR and its kin would point git at another repository.
GIT_KEEP = frozenset({"GIT_SSH", "GIT_SSH_COMMAND", "GIT_SSH_VARIANT", "GIT_ASKPASS", "GIT_PROXY_COMMAND",
                      "GIT_SSL_CAINFO", "GIT_SSL_CAPATH", "GIT_TERMINAL_PROMPT", "GIT_HTTP_USER_AGENT"})


def git_env() -> dict[str, str]:
    """The environment qq fetches with git in: only the https, ssh and file transports, for
    submodules and redirects too."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") or k in GIT_KEEP}
    env["GIT_ALLOW_PROTOCOL"] = "https:ssh:file"
    return env


def find_manifest(start: Path) -> Path | None:
    """The nearest infra/repo.toml at or above `start`."""
    for directory in (start, *start.parents):
        candidate = directory / MANIFEST
        if candidate.is_file():
            return candidate
    return None


def read_pin(start: Path) -> Pin | None:
    """The pin of the repo containing `start`, or None outside a repo or without a [qq] table."""
    manifest = find_manifest(start)
    if manifest is None:
        return None
    try:
        data = load(manifest)
    except ManifestError as e:
        raise PinError(f"{e}\nFix the manifest, or set {PINNED_ENV}=1 to run qq {__version__} as is.") from None
    table = data.get("qq")
    if not table:
        return None
    return Pin(table["version"], table.get("source"), table.get("digest"), manifest)


TRUSTED_ENV = "QQ_TRUSTED_SOURCES"


def _base(url: str) -> str:
    return url.rstrip("/").removesuffix(".git")


def _plain(url: str) -> bool:
    """A URL whose text is what git and urllib will fetch: no "." or ".." segments, escapes,
    user info, query, fragment or backslashes, which could make a prefix match lie."""
    parts = urllib.parse.urlsplit(url)
    segments = parts.path.split("/")
    return (bool(parts.scheme) and not parts.query and not parts.fragment and "@" not in parts.netloc
            and "%" not in url and "\\" not in url and "." not in segments and ".." not in segments)


def trusted(source: str) -> bool:
    """Whether a pin may install qq from `source`: the depot qq uses ($QQ_DEPOT_URL, default
    quirq-ai/qq) or what the user trusts in $QQ_TRUSTED_SOURCES (space separated), and anything
    under those (release archives). A repo's manifest alone never picks a new host."""
    if not _plain(source):
        return False
    bases = [DEFAULT_DEPOT_URL, os.environ.get("QQ_DEPOT_URL", ""),
             *os.environ.get(TRUSTED_ENV, "").split()]
    src = _base(source)
    return any(b and (src == _base(b) or src.startswith(_base(b) + "/")) for b in bases)


def check(pin: Pin) -> None:
    """Refuse a pin that would install from an untrusted place or without fixed bytes.
    (The quirq-repo/1 schema already makes source and digest come together.)"""
    if pin.source is None:
        return
    if not pin.digest:
        raise PinError(f"{pin.manifest}: [qq] source needs a digest (git:<commit> or sha256:<archive>)")
    if not trusted(pin.source):
        raise PinError(f"{pin.manifest} pins qq from {pin.source}, which qq does not trust. Running it would "
                       f"run whatever that repo's author chose. To trust it, set {TRUSTED_ENV}={pin.source}")


def is_self(pin: Pin) -> bool:
    """True when this process already is the pinned qq."""
    return pin.source is None and pin.digest is None and pin.version == __version__


PYPI_LOCK = Path(__file__).parent / "locks" / "pypi.txt"


def pip_install(python: Path, *targets: str) -> list[list[str]]:
    """The pip commands that install `targets` into the environment of `python` with nothing
    unpinned: PyPI packages only from the hashed lock, then the targets and their git
    dependencies with no index (a PyPI dependency missing from the lock fails here), then a
    consistency check. bootstrap.py repeats this, since it runs before qq is installed."""
    pip = [str(python), "-m", "pip", "--disable-pip-version-check", "--quiet"]
    return [[*pip, "install", "--require-hashes", "--no-deps", "--only-binary=:all:", "-r", str(PYPI_LOCK)],
            [*pip, "install", "--no-index", "--no-build-isolation", *targets],
            [*pip, "check"]]


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, check=True, text=True, capture_output=True, **kwargs)
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or e.stdout or "").strip().splitlines()[-5:]
        raise PinError(f"command failed: {' '.join(cmd)}\n" + "\n".join(detail)) from None


def _fetch_archive(pin: Pin, into: Path) -> Path:
    """Download `pin.source` and check it against the sha256 digest."""
    name = Path(urllib.parse.urlparse(pin.source).path).name or "qqdepot.tar.gz"
    path = into / name
    try:
        with urllib.request.urlopen(pin.source, timeout=60) as response, path.open("wb") as out:
            shutil.copyfileobj(response, out)
    except (OSError, ValueError) as e:
        raise PinError(f"cannot download {pin.source}: {e}") from None
    actual = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != pin.digest:
        raise PinError(f"{pin.source} has digest {actual}, but {pin.manifest} pins {pin.digest}")
    return path


def requirement(pin: Pin, scratch: Path) -> str:
    """What pip installs for this pin."""
    if pin.source is None:
        return f"git+{os.environ.get('QQ_DEPOT_URL') or DEFAULT_DEPOT_URL}@v{pin.version}"
    algo, _, value = pin.digest.partition(":")
    if algo == "git":
        _published(pin.source, value)
        return f"git+{pin.source}@{value}"
    return str(_fetch_archive(pin, scratch))


def _published(source: str, commit: str) -> None:
    """Refuse a commit that neither `main` nor a release tag (v*) of `source` contains. GitHub
    serves any commit of a repository's fork network by its id, so a trusted URL alone would
    let a fork's code in; and anyone with write access can push a branch at any commit, while
    main and v* tags are protected (gate's rulesets, qq-release-tags)."""
    with tempfile.TemporaryDirectory(prefix="qq-pin-") as tmp:
        def git(*args: str) -> str:
            return subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True, text=True,
                                  env=git_env()).stdout
        try:
            git("init", "-q", "--bare")
            git("fetch", "-q", "--filter=blob:none", "--no-tags", "--end-of-options", source,
                "+refs/heads/main:refs/heads/main", "+refs/tags/v*:refs/tags/v*")
            holders = git("for-each-ref", f"--contains={commit}", "--format=%(refname)")
        except subprocess.CalledProcessError:
            holders = ""   # also when the commit is not there at all
        except OSError as e:
            raise PinError(f"cannot run git to check {source}@{commit[:12]}: {e}") from None
    if not holders.strip():
        raise PinError(f"{commit} is on neither main nor a v* tag of {source}, so it is not that repo's "
                       "released code; pin a commit merged to main")


def installed_commit(target: Path) -> str | None:
    """The commit pip installed qqdepot from, from its PEP 610 direct_url.json, if a git one."""
    for record in (r for site in compat.site_packages(target) for r in site.glob("qqdepot-*.dist-info/direct_url.json")):
        try:
            return json.loads(record.read_text()).get("vcs_info", {}).get("commit_id")
        except (OSError, ValueError, AttributeError):
            return None
    return None


def installed_version(qq: Path) -> str:
    env = {**os.environ, PINNED_ENV: "1"}
    env.pop("PYTHONPATH", None)
    out = _run([str(qq), "--version"], env=env).stdout.strip()
    return out.removeprefix("qq ")


def ensure(pin: Pin) -> Path:
    """The qq executable for `pin`, installing it into $QQ_HOME/versions first if needed."""
    check(pin)
    versions = qq_home() / "versions"
    target = versions / pin.key
    qq = compat.venv_exe(target, "qq")
    done = target / ".qq-installed"  # written last: a half-made environment is never used
    if done.is_file():
        return qq
    versions.mkdir(parents=True, exist_ok=True)
    # Environments cannot move once made, so build in place under a lock that keeps
    # concurrent qq runs from building the same version twice.
    with (versions / f".{pin.key}.lock").open("w") as lock:
        compat.lock(lock)
        if done.is_file():
            return qq
        print(f"qq: installing qq {pin.version} pinned by {pin.manifest}", file=sys.stderr)
        compat.rmtree(target)
        try:
            with tempfile.TemporaryDirectory(dir=versions, prefix=f".{pin.key}.") as scratch:
                _run([sys.executable, "-m", "venv", str(target)])
                for cmd in pip_install(compat.venv_exe(target, "python"), requirement(pin, Path(scratch))):
                    _run(cmd)
            got = installed_version(qq)
            if got != pin.version:
                raise PinError(f"{pin.manifest} pins qq {pin.version}, but what it names installs qq {got}")
            algo, _, value = (pin.digest or "").partition(":")
            if algo == "git" and (commit := installed_commit(target)) != value:
                raise PinError(f"{pin.manifest} pins qq at {value}, but pip installed {commit or 'an unknown commit'}")
        except BaseException:
            compat.rmtree(target)
            raise
        done.write_text(f"{pin.version} {pin.source or ''} {pin.digest or ''}\n")
    return qq


def dispatch(argv: list[str]) -> None:
    """Replace this process with the pinned qq, unless this already is it."""
    if os.environ.pop(PINNED_ENV, None) == "1":
        # Only this process skips the pin; a qq it runs, maybe in another repo, pins again.
        return
    pin = read_pin(Path.cwd())
    if pin is None or is_self(pin):
        return
    check(pin)
    qq = ensure(pin)
    env = {**os.environ, PINNED_ENV: "1"}
    env.pop("PYTHONPATH", None)  # the pinned qq runs its own code, never code shadowing it
    compat.replace_process(qq, [str(qq), *argv], env)
