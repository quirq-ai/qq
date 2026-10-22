"""qq: the quirq infra command line.

Run inside a repo, qq runs the version that repo pins in infra/repo.toml.
Other qq repos add subcommands through the `qq.commands` entry point group, and a repo adds its
own with `qq create NAME COMMAND` (saved in infra/commands; then `qq NAME` runs it).
"""
from __future__ import annotations

import argparse
import sys
from importlib.metadata import entry_points

from qqdepot import __version__, compat
from qqdepot.commands import build, change, create, run, sync
from qqdepot.pin import PinError, dispatch

# An entry point in this group names a function register(subparsers) that adds one
# subcommand whose parser sets `run`, a function of the parsed args returning the exit code.
COMMANDS_GROUP = "qq.commands"
BUILTIN = (sync, build, change, run, create)
# What runs on Windows so far: fetching pins and keeping clones current. build, test, run and a
# repo's own commands need /bin/sh, which Windows lacks; upload, try, land and status are not
# ported yet.
WINDOWS_COMMANDS = frozenset({"sync", "fetch"})


def build_parser(epilog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qq", description=__doc__, epilog=epilog,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"qq {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    for module in BUILTIN:
        module.register(sub)
    for ep in sorted(entry_points(group=COMMANDS_GROUP), key=lambda ep: ep.name):
        try:
            ep.load()(sub)
        except Exception as e:  # one broken plugin must not take down every command
            print(f"qq: skipping subcommand {ep.name!r}: {e}", file=sys.stderr)
    parser.qq_commands = sub.choices   # the names qq answers to; a repo's command never shadows one
    return parser


def repo_epilog(qq_commands: set[str]) -> str | None:
    """This repo's own commands (qq create), listed under qq's in qq --help."""
    commands = run.repo_commands(qq_commands)
    if not commands:
        return None
    width = max(len(name) for name, _ in commands)
    return "this repo's commands (qq create NAME COMMAND adds one):\n" + "\n".join(
        f"  {name:<{width}}  {line}" for name, line in commands)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        dispatch(argv)
    except PinError as e:
        print(f"qq: {e}", file=sys.stderr)
        # qq run and a repo's commands pass the command's exit code through, so their own failures are 125.
        route = argv[1:] if argv[:1] == ["--"] else argv
        passthrough = route[:1] == ["run"] or (bool(route) and not route[0].startswith("-")
                                               and route[0] not in build_parser().qq_commands)
        return run.QQ_FAILED if passthrough else 2
    route = argv[1:] if argv[:1] == ["--"] else argv   # qq -- NAME routes like qq -- sync
    if compat.WINDOWS and route and not route[0].startswith("-") and route[0] not in WINDOWS_COMMANDS:
        print(f"qq: `qq {route[0]}` does not run on Windows yet; only qq sync, qq fetch and qq sync "
              "project do. Run it in WSL.", file=sys.stderr)
        return 2
    if route[:1] == ["run"]:   # parsed on its own: its usage errors must not exit 2 like a command's
        return run.main(route[1:])
    parser = build_parser()
    commands = set(parser.qq_commands)
    if route and not route[0].startswith("-") and route[0] not in commands:
        code = run.run_saved(route[0], route[1:], commands)   # qq NAME: this repo's command
        if code is not None:
            return code
    if not argv or argv[0] in ("-h", "--help"):   # read the repo's commands only to print help
        parser.epilog = repo_epilog(commands)
    args = parser.parse_args(argv)
    if getattr(args, "run", None) is None:
        parser.print_help()
        return 0
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
