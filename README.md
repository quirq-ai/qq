# qq

`qq` is the quirq infra command line (this repo was `quirq-ai/depot` until 2026-10-09; the old
URL redirects here). Every repo that quirq infra builds pins the `qq` version it runs in its
manifest, `infra/repo.toml`, and `qq` installs and runs exactly that version.

## Chromium counterpart

`depot_tools`: the one checkout on your `PATH` that bootstraps itself and wraps the tools a
contributor needs (`gclient`, `git cl upload`, `git cl try`, `git cl land`).

## Shape

- Thin. `qq` wraps git and `gh` (behind a `github` backend; `launchpad`, quirq's own cloud, later)
  and calls other qq repos' libraries by pinned commit: manifests through `sync`, toolchains
  through `toolchains`, builds through `recipes`.
- Pluggable. Other repos add subcommands; `release` defines `qq channel rollback`. qq's own
  environment does not install release's package yet, so `qq --help` does not list it.
- Acknowledge, then push. Commands that start long work return a run ID at once and the verdict
  arrives later, so no agent holds a tool call open during a build.

## Install

Clone this repo once and put its `bin` directory on `PATH`, as with depot_tools. You need
`git` and `python3` 3.11.4 or newer with its `venv` module.

```sh
git clone https://github.com/quirq-ai/qq ~/qq
export PATH="$HOME/qq/bin:$PATH"
qq --version
```

`qq --version` prints `qq 0.1.0`. The full install, with `qqsync` and `PATH` set up, is in the qq
guide: https://docs.quirq.dev/docs/qq. Toolchains are published for Linux x86_64 only: on a Mac,
`qq sync` stops with `no pin for platform macos-arm64`, so use `git clone` instead of `qq fetch`,
and give `qq build` and `qq test` toolchains you installed yourself (`--toolchain python=ROOT`).

## Version pinning

Every repo pins the `qq` it runs in its manifest, read only through `sync`:

```toml
[qq]
version = "0.1.0"          # installs qq tag v0.1.0
# source = "https://github.com/quirq-ai/qq"      # optional: pin the bytes too,
# digest = "git:<commit>"                         # by commit, or sha256:<archive digest>
```

Inside such a repo, `qq` runs exactly the pinned version. The first run on a machine installs it
into its own environment under `$QQ_HOME` (default `~/.cache/qq/versions/<version>`) and checks
that it reports the pinned version; later runs reuse it. When a roll moves the pin, the next `qq`
installs the new version by itself: that is how `qq` updates. qq has no release tag yet, so no
`v0.1.0` tag exists to install; a pin of version `0.1.0` with no `source` or `digest` runs your qq
checkout, which reports `0.1.0`. Outside a repo, or in one without a `[qq]` table, `qq` runs the
version of your qq checkout.

Trust: a pin installs qq only from the depot (`$QQ_DEPOT_URL`, default quirq-ai/qq, and release
archives under it) or from a source you list in `$QQ_TRUSTED_SOURCES`, so a pull request that edits
the manifest cannot make `qq status` run code from a host it picked. A `source` needs a digest. A
`git:` digest fixes the exact commit, and qq checks pip installed that commit; a `sha256:` digest
fixes the archive; a version alone trusts the qq tag `v<version>`.

Every qq environment (the launcher, each pinned version, the gate) installs PyPI packages only
by hash from `src/qqdepot/locks/pypi.txt`, and everything else (qqsync, qqrecipes, the gate) by
pinned commit with no index, so nothing unpinned runs next to your `gh` token. A pinned repo
that adds a PyPI dependency needs it added to `locks/pypi.in` and the lock regenerated (the
command is in that file); otherwise the install fails at the no-index step or `pip check`.
Locked packages install from wheels only, since building one would fetch unhashed build
requirements. pip still reads your own `PIP_*` variables and pip config (proxy, CA,
`find-links`): those come from you, not from a repo, but keep them pointed at sources you trust.

| Variable | Meaning |
|---|---|
| `QQ_HOME` | Where launchers and pinned versions live |
| `QQ_PYTHON` | The interpreter the bootstrap uses (default `python3`) |
| `QQ_TRUSTED_SOURCES` | Space-separated sources a `[qq]` pin may install from, besides the qq repo |
| `QQ_DEPOT_URL` | Where version tags are fetched from (default this repo; a mirror works) |
| `QQ_PINNED=1` | Run this `qq` as is, without reading the pin (applies to that one process) |

TODO(expert): once qq publishes releases (release repo), pin by `sha256` archive by default and
let the bootstrap update its own checkout.

## Get a repo's toolchains and dependencies

```sh
qq fetch https://github.com/quirq-ai/xo-space   # clone, then qq sync in it
qq sync                                        # inside a repo: fetch every pin, check, link
```

`qq sync` reads the manifest's `[toolchains]` and `[deps]` through `sync` and fetches each pin
once per machine into `$QQ_HOME/store`, checked against its digest. A tarball is unpacked. The
entry is then read only, and each `qq sync` checks it is unchanged before linking it again; a
changed entry is refused (remove it with `chmod -R u+w ENTRY && rm -rf ENTRY` and sync again). Git
fetches run over https, ssh and file only. The
pin is then linked at `<repo>/.qq/toolchains/<name>` or `<repo>/.qq/deps/<name>`. The source's
scheme picks the fetcher:

| Source | Digest | Fetched as |
|---|---|---|
| `https://…`, `file://…` | `sha256:` | the bytes at that URL |
| `oci://REGISTRY/REPO@sha256:<manifest>` | `sha256:<layer>` | that layer of that manifest, which is how `toolchains` publishes |
| any git URL | `git:<commit>` | the tree at that commit |

A pin with `platforms` uses this machine's entry (`linux-x86_64`, `macos-arm64`, …). A pin
dropped from the manifest loses its link at the next sync. Registry packages must be public:
`qq sync` asks for an anonymous token and never sends credentials.

## Keep working clones up to date

```sh
qq sync project                  # fetch, then bring each clone up to date with its strategy
qq sync project --fetch-only     # download new commits; leave the working files alone
qq sync project check            # is each clone behind its remote? changes nothing
qq sync project add URL [NAME]   # clone a repo; it needs no infra/repo.toml
qq sync project members          # who has access to this clone's repo, and who is invited
qq sync project invite USER      # give USER access
qq sync project revoke USER      # remove USER's access, or cancel their invitation
```

A project is a clone someone edits, which follows a branch; the pins above are read-only copies at
fixed commits. These commands are `qqsync project` from `sync`, under qq's `sync` word: see that
repo's README for `--root`, `--json`, the exit codes and strategies.

## Build and test like CI

```sh
qq build [TARGET ...]     # fetch and build the targets (default all)
qq test [TARGET ...]      # fetch, build and test them
```

Both hand the manifest to `recipes`, qq's planner and runner, which leave JUnit XML, logs and
`results.json` under `<repo>/.qq/out` (or `--out DIR`). Product CI does not run recipes yet: the
workflows infra-config generates (`qq-<repo>-presubmit.yml` and `-postsubmit.yml`) still run the
stand-in `interim` commands from infra-config's `kinds.toml`, on toolchains from GitHub's setup
actions. A local run and a CI run of one commit can therefore differ until those steps switch to
recipes (V0-DEP-03). Toolchains come from `<repo>/.qq/toolchains/<name>`, where `qq sync` unpacks
them, or `--toolchain NAME=ROOT`; any other toolchain is the one on `PATH`.

## Run commands with the repo's toolchains, and add your own

```sh
qq create check ./scripts/check --all   # make `qq check` a command of this repo (infra/commands/check.sh)
qq check --fast                         # run it like any qq command: ./scripts/check --all --fast
qq --help                               # lists the repo's commands under qq's own
qq run "./scripts/check --fast"         # run a command once without keeping it (with /bin/sh)
qq run PROGRAM [ARG ...]                # several words: PROGRAM runs directly, no shell
```

A repo's commands and `qq run` run from the repo root. The `bin` directory of each toolchain `qq
sync` linked under `<repo>/.qq/toolchains` comes first on `PATH`, so the command uses the versions
the manifest pins rather than whatever is installed. A toolchain counts only when its link points
at the store entry for the digest pinned now: after a pin moves, the old one is not used, and a
toolchain directory committed to the repo never lands on `PATH`. A pinned toolchain that is not
synced at its pin is named on stderr and taken from `PATH`; run `qq sync`. Toolchains are
published for linux-x86_64 only so far, so elsewhere (a Mac, an arm64 machine) the command runs
with your own tools and qq names the pins it could not use. CI does not use these pins yet: it
installs Node and Python with GitHub's setup actions, so the patch release can differ, and it
installs pnpm 10 (`npm install --global pnpm@10`) where the pinned Node toolchain bundles pnpm
11.28.2. Exact local and CI parity is v1.

qq then becomes the command (exec), so its exit code, signals and terminal are the command's own.
When qq itself fails (bad usage, no repo, a broken or unreadable command file, a broken qq pin) it
exits 125, so its errors never look like the command's; a command that cannot start gives 127 (not
found) or 126 (not runnable), as a shell does. It runs in the foreground, like `qq build` and `qq
test`; it is for local commands, not for handing long work to CI. `qq create` passes nothing
through, so it exits like the other qq commands: 2 on bad usage (a bad name, no command) or outside
a repo, 1 when it cannot create the command (the name is taken, the file exists).

`qq create NAME COMMAND` writes a POSIX shell script at `infra/commands/NAME.sh`; commit it so
teammates and agents get `qq NAME` too (`qq create --force NAME COMMAND` replaces one; you can also
write or edit one by hand). Created from several words, each stays one word and the arguments given
to `qq NAME` are passed on after them (`"$@"`). Created from one argument, the command line is kept
as typed; add `"$@"` where arguments should go, since qq refuses to run a command with arguments
when the script never mentions them rather than drop them. It looks outside single quotes, comments
and heredocs with a quoted end word (`<<'EOF'`), also inside `"$(...)"`, and checks for a mention
only: a function's own `$1` or `set --` counts too. For a script that never mentions them, `qq NAME
--help` prints its path and first line; a script that takes arguments gets `--help` like any other.
`qq -- NAME` runs it as `qq -- sync` runs sync. A name is lowercase letters, digits, `-` and `_`, so
it can never be a path or shell syntax, and arguments are never read as shell code. qq's own
commands always win: a name qq already answers to (`sync`, `fetch`, `build`, `land`, a plugin's
command, ...) cannot be created, and a script committed under such a name is never run. Nor can a
name one typo away from one (`snyc`, `tets`, `lands`): a mistyped `qq sync` must never run the
repo's code, so such a script is refused rather than run. Only the exact file name counts, also on a
case-insensitive file system (`qq space` never runs `Space.sh`).
`qq --help` lists each command's first line (from its first 4 KiB, cut at 120 characters) with
anything a terminal would act on written as an escape: `\xNN` for ASCII control characters and
bytes that are not UTF-8, `\uNNNN` or `\UNNNNNNNN` for other characters that do not print, and a
backslash as `\\`. Under a broken qq pin, help is not available until the pin is fixed.

A repo's command is code in the repo, like any script there. qq runs one only when you name it,
never from `qq sync`, `qq build` or `qq test`; read `infra/commands/NAME.sh` with `less` or `cat -v`
(plain `cat` lets terminal escapes in it act) before running one from a repo you do not trust. CI
does not run them yet.

A repo that pins `[qq] version = "0.1.0"` (xo-space and innernet do) runs the `qq` of your qq
checkout, since that is the version it reports, so it has these commands once your checkout
includes #23. A repo that pins qq by digest gets them when its pin moves to a commit that
has them.

## Send a change through the gate

```sh
qq upload [--title T] [--draft]   # push this branch, open or update its change
qq try [CHANGE] [--notify CMD]    # upload, print a run ID and exit; the verdict is pushed later
qq land [CHANGE] [--notify CMD]   # land once the gate passes (merge queue); returns at once
qq status [CHANGE]                # the gate's verdict now: exit 0 pass, 1 refused, 2 error, 3 pending
```

Acknowledge, then push: `qq try` and `qq land` return a run ID at once, so an agent never holds a
tool call open while CI runs. A watcher follows the change and delivers the verdict (pass,
refused, landed, dequeued by the merge queue, superseded by a newer push, closed, timed-out or
error) to `$QQ_HOME/verdicts/<run ID>.json` and to the `--notify` command (default `$QQ_NOTIFY`),
which gets the verdict JSON on stdin and `QQ_VERDICT` in its environment. An agent harness plugs
its own channel in there. The command line shows in `ps` and in the receipt, so pass secrets
through the environment. If the watcher dies (a reboot), `qq status` still gives the verdict.

`qq land` queues nothing until the gate passes: without a merge queue, GitHub would merge at
once. A refused change is not queued, and a change the queue drops is reported as dequeued.

qq does not decide what must pass. It runs [gate](https://github.com/quirq-ai/gate)'s `qqgate`
at the commit `src/qqdepot/gate.py` pins, in its own environment under `$QQ_HOME/gate`, with
infra-config at the commit the gate pins. A repo infra-config does not list (qqgate's exit 3) is
reported as ungated and judged strictly from the backend's own checks: at least one check, every
check a success (skipped and neutral are refusals), and no run of the commit still going; `qq land` then squash-merges unless `--method` says otherwise. On GitHub the commands are thin wrappers over `gh`
(signed in with `gh auth login`); backend code sits in `src/qqdepot/backends/<backend>.py`,
picked by `--backend` or `$QQ_BACKEND`.

## Subcommands from other repos

A package adds a `qq` subcommand with an entry point in the `qq.commands` group naming a function
`register(subparsers)`. It adds one parser and sets `run`, a function of the parsed arguments that
returns the exit code.

## Develop

```sh
python -m pip install -e ".[test]"
python -m pytest -q
```

Python 3.14 in CI (the org pin in infra-config); `qq` itself needs 3.11.4 or newer.

## v0 status

| Item | What | PR | State |
|---|---|---|---|
| V0-DEP-01 | `qq` skeleton with version pinning | #2 | merged; done-when shown by `tests/test_fresh_machine.py` in presubmit |
| V0-DEP-02 | `qq fetch` and `qq sync` | #4 | merged; a fresh clone builds after `qq sync` alone in `tests/test_sync.py`. The `e2e-sync` workflow (#17) runs `qq fetch` (clone at main, then `qq sync`) on xo-space and innernet nightly and on demand, from public ghcr with no credentials |
| V0-DEP-03 | `qq build` and `qq test` | #3, #5 | merged; presubmit `parity` runs `qq test` and `qqrecipes execute` on xo-space and innernet on separate runners and compares their JUnit. Parity still reads sync's fixture manifests, not the repos' own `infra/repo.toml`. Not done: product CI does not run recipes yet (see Build and test like CI) |
| V0-DEP-04 | `qq upload`, `try`, `land`, `status` | #7 | merged; `tests/test_change.py` shows try returning a run ID and the verdict pushed later (fake gh); presubmit `live` runs the pinned gate and `qq status` through the real `gh`. gate's repo rulesets are applied as of gate 6610664, which covered the 13 infra repos plus xo-space and innernet; gate's later settings (website onboarding, gate #25, #26) are not applied yet. A live `qq try` or `qq land` on xo-space or innernet is not shown here yet |

Plan and every v0 item: `quirq-ai/infra-config`, `docs/plan.md` and `docs/v0.md`.

## License

Apache-2.0; see [LICENSE](LICENSE).
