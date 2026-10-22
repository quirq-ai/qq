"""The gate's verdict on a change, from quirq-ai/gate's own `qqgate`, used by pinned commit.

qq never decides what must pass: `qqgate required` and `qqgate verdict --sha` do, from
infra-config at the commit the gate pins. The pinned gate gets its own environment under
$QQ_HOME/gate, apart from qq's, so its dependency pins never have to match qq's.

A repo infra-config does not list is "ungated": qq then reports the backend's own checks and
says so, because nothing defines what must pass there.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from qqdepot import compat
from qqdepot.backends import ChangeError
from qqdepot.pin import git_env, pip_install, qq_home

# TODO(expert): let rollers move this pin, with the qqsync and qqrecipes pins in pyproject.toml.
GATE_SOURCE = "https://github.com/quirq-ai/gate"
GATE_COMMIT = "96c85d438b35cd239e9ddca87a42ed2a076a8d98"   # main after #30: pins infra-config 41a8cb0, which gates website
# Override for development and tests: a qqgate executable and an infra-config checkout.
GATE_ENV, CONFIG_ENV = "QQ_GATE", "QQ_GATE_CONFIG"


def _timeout() -> float:
    """A hung gate must not hold `qq land` or the watcher open: past this it is an error, never a pass."""
    try:
        return float(os.environ.get("QQ_GATE_TIMEOUT") or 300)
    except ValueError:
        raise ChangeError(f"QQ_GATE_TIMEOUT={os.environ['QQ_GATE_TIMEOUT']!r} is not a number of seconds") from None


# Check states that are not a result yet. Any other state that is not a pass is a refusal.
RUNNING = frozenset({"queued", "in_progress", "waiting", "requested", "pending", "expected"})
# qqgate's exit code for a repo infra-config does not list (gate #7). Only this structured answer
# means "ungated": an error's text never does, so a gate failure can never open the weaker rule.
UNGATED_EXIT = 3


def _git(cwd: Path, *args: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                              text=True, env=git_env()).stdout.strip()
    except subprocess.CalledProcessError as e:
        raise ChangeError(f"git {' '.join(args)} failed: {e.stderr.strip()}") from None


def _checkout(source: str, commit: str, into: Path) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or source.startswith("-"):
        raise ChangeError(f"bad pin {source}@{commit}: want a URL and a full commit")
    into.mkdir(parents=True)
    _git(into, "init", "-q")
    _git(into, "fetch", "-q", "--depth=1", "--end-of-options", source, commit)
    _git(into, "checkout", "-q", "--detach", "FETCH_HEAD")
    if (got := _git(into, "rev-parse", "HEAD")) != commit:
        raise ChangeError(f"{source} gave commit {got}, not the pinned {commit}")


def ensure() -> tuple[Path, Path]:
    """(qqgate executable, infra-config checkout), installing the pinned gate once."""
    if os.environ.get(GATE_ENV):
        config = os.environ.get(CONFIG_ENV)
        if not config:
            raise ChangeError(f"{GATE_ENV} is set, so set {CONFIG_ENV} to an infra-config checkout too")
        return Path(os.environ[GATE_ENV]), Path(config)
    base = qq_home() / "gate"
    target = base / GATE_COMMIT[:16]
    exe, config, done = compat.venv_exe(target / "venv", "qqgate"), target / "infra-config", target / ".qq-installed"
    if done.is_file():
        return exe, config
    base.mkdir(parents=True, exist_ok=True)
    with (base / f".{GATE_COMMIT[:16]}.lock").open("w") as lock:
        compat.lock(lock)
        if done.is_file():
            return exe, config
        print(f"qq: installing the gate at {GATE_COMMIT[:12]}", file=sys.stderr)
        shutil.rmtree(target, ignore_errors=True)
        try:
            _checkout(GATE_SOURCE, GATE_COMMIT, target / "gate")
            try:
                cfg_pin = tomllib.loads((target / "gate" / "pins.toml").read_text())["infra-config"]
            except (OSError, tomllib.TOMLDecodeError, KeyError) as e:
                raise ChangeError(f"gate {GATE_COMMIT[:12]} has no usable pins.toml [infra-config]: {e}") from None
            # Policy is read at the commit the gate pins, so qq and CI judge by the same config.
            _checkout(cfg_pin["source"], cfg_pin["commit"], config)
            for cmd in ([sys.executable, "-m", "venv", str(target / "venv")],
                        *pip_install(compat.venv_exe(target / "venv", "python"), str(target / "gate"))):
                try:
                    subprocess.run(cmd, check=True, capture_output=True, text=True)
                except subprocess.CalledProcessError as e:
                    raise ChangeError(f"installing the gate failed: {' '.join(cmd[:4])}\n"
                                      + "\n".join(e.stderr.strip().splitlines()[-5:])) from None
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise
        done.write_text(f"{GATE_SOURCE}@{GATE_COMMIT}\n")
    return exe, config


def _qqgate(args: list[str], token: str | None = None) -> tuple[int, dict | None, str]:
    exe, config = ensure()
    env = dict(os.environ)
    if token:
        env["QQ_GITHUB_TOKEN"] = token
    try:
        p = subprocess.run([str(exe), args[0], "--config", str(config), *args[1:], "--json"],
                           capture_output=True, text=True, env=env, timeout=_timeout())
    except subprocess.TimeoutExpired:
        raise ChangeError(f"the gate did not answer within {_timeout():g}s ({exe} {args[0]})") from None
    except OSError as e:
        raise ChangeError(f"cannot run the gate ({exe}): {e}") from None
    data = None
    if p.returncode in (0, 1, UNGATED_EXIT):
        try:
            data = json.loads(p.stdout)
        except json.JSONDecodeError as e:
            raise ChangeError(f"the gate printed non-JSON: {e}") from None
        if not isinstance(data, dict):
            raise ChangeError(f"the gate printed JSON that is not an object: {p.stdout.strip()[:200]}")
    return p.returncode, data, p.stderr.strip()


def gated_name(repo: str) -> str:
    """The gate names repos as infra-config repos.toml does: the repository's own name. `repo` is
    the backend's canonical OWNER/NAME (backend.repo), never the spelling a user typed.
    TODO(expert): match on repos.toml `source` once two owners share a repo name."""
    return repo.rsplit("/", 1)[-1]


def required(repo: str) -> dict | None:
    """`qqgate required --json` for the repo, or None when infra-config does not gate it."""
    rc, data, err = _qqgate(["required", "--repo", gated_name(repo)])
    if rc == 0:
        return data
    if rc == UNGATED_EXIT and data == {"repo": gated_name(repo), "onboarded": False}:
        return None
    raise ChangeError(f"the gate could not compute {repo}'s required checks: {err}")


def classify_gated(v: dict) -> str:
    if v["verdict"] == "pass":
        return "pass"
    if any(f["state"] not in RUNNING for f in v["failing"]):
        return "refused"
    return "pending"   # only checks still running or not reported yet


def classify_ungated(checks: dict[str, str], runs: dict) -> str:
    """No policy covers the repo, so be strict: every check finished with success, at least one
    check, and no run of the commit still going. Skipped and neutral are refusals here."""
    states = [*checks.values(), *runs.values()]   # a run that did not succeed (action_required) refuses
    if any(s not in RUNNING and s != "success" for s in states):
        return "refused"
    if not checks or any(s in RUNNING for s in states):
        return "pending"
    return "pass"


def verdict(repo: str, sha: str, backend) -> dict:
    """{"result": pass|refused|pending, "gated": bool, ...} for one commit."""
    token = backend.token()
    rc, data, err = _qqgate(["verdict", "--repo", gated_name(repo), "--sha", sha], token)
    if rc in (0, 1):
        # The exit code and the JSON must agree: a "pass" counts only with exit 0.
        if data.get("verdict") != ("pass" if rc == 0 else "refused") or not isinstance(data.get("failing"), list):
            raise ChangeError(f"the gate gave {repo}@{sha[:12]} an inconsistent verdict "
                              f"(exit {rc}, verdict {data.get('verdict')!r})")
        try:
            result = classify_gated(data)
        except (KeyError, TypeError) as e:
            raise ChangeError(f"the gate's verdict for {repo}@{sha[:12]} is malformed: {e}") from None
        return {"result": result, "gated": True, "sha": sha, **data}
    if rc == UNGATED_EXIT and data == {"repo": gated_name(repo), "onboarded": False}:
        checks, runs = backend.checks(repo, sha), backend.runs(repo, sha)
        return {"result": classify_ungated(checks, runs), "gated": False, "sha": sha, "checks": checks}
    raise ChangeError(f"the gate could not decide {repo}@{sha[:12]}: {err}")
