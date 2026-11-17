# Skills

Run `skills sync` after changing a workspace or project overlay. It updates the
shared clone, calls its builder and publishes a stable directory for the agents.
`chezmoi apply` runs the same command. A failed sync is reported without failing
the apply; run the command directly to get a nonzero status for automation.

## Source and published views

| Path | Purpose |
| --- | --- |
| `~/.local/share/skills/base/` | Writable clone of [wyrd-company/skills](https://github.com/wyrd-company/skills), kept on clean `main`; make skill PRs from worktrees of this clone. |
| `~/.local/share/skills/generated/skills` | Builder-managed output symlink and sibling generation directories; used only to prepare the published copy. |
| `~/.agents/skills/` | Real directory of copied skill files, read by Codex and Pi. No symlinks within the view. |
| `~/.claude/skills/<name>` | Per-skill links to the absolute `~/.agents/skills/<name>` path, read by Claude Code. |

The shared view carries `.skills-sync-view`, a marker recording its format
version and whether it is a fallback. Each successful sync copies the build into
`~/.agents/.skills.stage-*`, moves the old view aside, renames the stage into
place and removes the old copy. If the second rename fails, the old view is
restored. Canonical skill paths stay rooted at `~/.agents/skills` across builds;
existing sessions can read those paths after the builder removes an obsolete
generation. Start a new session to refresh the catalogue of skill names and
descriptions when those change.

Claude's ordinary directory stays in place. Sync only replaces or prunes links
into the managed roots; Claude-only directories, files and unrelated links are
left where they are. A private skill with the same name as a published skill is
refused before the build. The original Claude view is copied once to
`~/.claude/skills.before-sync` before any migration changes. Later syncs retain
that backup and preserve private edits in the current directory.

## Building

The command passes absolute paths to the clone's Taskfile, in this order:

1. The base clone.
2. `/skills`, when that directory exists.
3. Each `/workspace/*/.skills` directory, ordered by its project path.

Projects without `.skills/` contribute no arguments. A candidate containing
`.git` (file or directory) or `<folder-name>.git` is reported on stderr and
skipped as a repository clone. `main/` is a valid skill name. The project folder
name is the command's single `PROJECT_OVERLAY` constant. Its interim value
`.skills` awaits the operator's decision on
[skills#27](https://github.com/wyrd-company/skills/issues/27); merge is held until
that decision matches the constant.

Merge rules, validation and the build interface belong to the skills
repository's [CONTRIBUTING.md](https://github.com/wyrd-company/skills/blob/main/CONTRIBUTING.md#building-overlays-the-interface-for-skills-sync).
The builder's contract is unchanged; none of the harnesses reads its output.
Task and uv are in the Brewfile; Python 3.11 or newer is provided by the workspace.
The first clone needs network, and the first build or a changed `uv.lock` needs
network unless dependencies are cached. `uv sync --frozen` creates the ignored
`.venv/` in the writable clone. Prewarm it while online:

```sh
task --dir "$HOME/.local/share/skills/base" tools
```

Only a clean `main` updates, by fast-forward. Uncommitted changes, another branch
or detached HEAD stop the sync with the clean-main message. A failed pull warns
and builds the current checkout. `git worktree repair` runs on the source clone
on every sync, including before a build that fails, so an interrupted migration
can repair its worktree backlinks on retry.

`~/.local/share/skills/sync.lock` serializes source updates, building and
publication for that HOME. A second sync waits for the lock before touching the
clone or views. The builder also holds its own output lock. Do not remove lock
files while a sync could be running.

A build failure leaves an existing managed view byte for byte unchanged. If
there is no managed view yet, including after a container rebuild that retained
Claude's links but lost the source and shared view, sync copies the available
base clone's top-level skill folders directly without calling the builder again.
The marker records `fallback: true`, Claude's managed links are refreshed,
stderr reports that the base skills were published unbuilt, and sync still
exits nonzero. Fix the build error and sync again to replace the fallback with
the checked, layered build. An ordinary unmarked directory or unrelated symlink
at `~/.agents/skills` is refused rather than overwritten.

## Migrating an existing installation

Migration happens by applying this revision (`chezmoi update` after the merge);
never install `skills` or run `skills sync` from a branch while the applied
chezmoi source still declares the `~/.agents/skills` git-repo external. A rollback
lasts only until the next apply.

The hook adopts the old real clone at `~/.agents/skills` as
`~/.local/share/skills/base`, preserving all Git objects, branches and linked
worktrees. It builds or prepares a base fallback before moving the clone. The
view replaces the old clone at its original path; Claude's existing absolute
links already name that path. Worktrees stay where they are and their backlinks
are repaired. A migrated `.venv` is reinstalled once at the final path because
console-script shebangs contain the old absolute path. A pending marker retries
that environment repair after an interrupted migration. The first fallback can
be published even if Task or uv cannot build.

Migrate while no agent is editing the source. The old clone and new source path
must be on the same filesystem; a failed cross-filesystem rename leaves the old
clone in place. If both clone paths exist, sync refuses to choose between them.
Inspect and reconcile them before retrying. The original clone is retained at
`base`; `~/.agents/skills.before-sync` is an alias to it after a completed legacy
migration, and Claude's one-time backup remains available for inspection.

If interrupted, rerun sync from the applied dotfiles revision. A missing view
with an intact source can be rebuilt or receive a base fallback. A published
view with stale worktree backlinks is repaired even if the next build fails.
Never delete `.git/worktrees` or the linked worktrees to recover.

## Rollback

Stop syncs and agents editing the source, then run this as a script. The checks
stop it from replacing an unrelated path. It retains the generated view for
inspection and puts the original clone back at its old path:

```sh
set -e
test -d "$HOME/.agents/skills"
test ! -L "$HOME/.agents/skills"
test -f "$HOME/.agents/skills/.skills-sync-view"
test -d "$HOME/.local/share/skills/base/.git"
test ! -e "$HOME/.agents/skills.after-sync"
test ! -L "$HOME/.agents/skills.after-sync"
mv "$HOME/.agents/skills" "$HOME/.agents/skills.after-sync"
mv "$HOME/.local/share/skills/base" "$HOME/.agents/skills"
git -C "$HOME/.agents/skills" worktree repair
uv sync --directory "$HOME/.agents/skills" --frozen --reinstall
```

Claude's links now read the restored clone; keep its current directory and any
private edits there. Prune links to skills introduced only by an overlay:

```sh
for entry in "$HOME/.claude/skills"/*; do
    [ -L "$entry" ] || continue
    case "$(readlink "$entry")" in
        "$HOME/.agents/skills/"*) [ -f "$entry/SKILL.md" ] || rm "$entry" ;;
    esac
done
```

The original Claude link layout is also in `~/.claude/skills.before-sync`; inspect
it without replacing private entries in the current directory. The source alias
`~/.agents/skills.before-sync` can be removed if it still points to the now-moved
`base`. Keep builder output and `skills.after-sync` until the failure is
understood. The next apply or direct sync attempts migration again.

## Verification

Run orchestration fixtures, the real locked builder and guard mutations:

```sh
python3 -m unittest discover -s tests -p test_skills.py -v
SKILLS_TEST_BASE=/absolute/path/to/skills-main \
  python3 -m unittest discover -s tests -p test_skills.py -v
python3 tests/mutate_skills.py
```

Fixtures use local Git remotes and fake Task/uv commands. The real builder test
uses a scratch HOME and checks append order, project headings, base-only byte
equality, stable canonical paths, and last-good output after validation failures.
The rebuild regression retains Claude's links, removes source/view state and
proves an invalid overlay still leaves a base fallback and resolving links.

With all three harnesses installed, list their discovered skills and verify an
appended token without calling a model:

```sh
python3 tests/prove_skills_harnesses.py \
  --home /path/to/synced-scratch-home --cwd /path/to/scratch-project \
  --skill standards --token UNIQUE_OVERLAY_TOKEN
```

This uses Claude's normal initialization command list, Codex's `skills/list` API
and Pi's installed default resource loader. All built skills must be listed.
Claude starts with a temporary `CLAUDE_CONFIG_DIR` whose `skills` symlink points
to the selected HOME's Claude view; the directory and its startup state are
deleted afterwards. Codex and Pi use the selected HOME. Prove branch changes
only in a scratch HOME. Live acceptance follows the merged `chezmoi update` and
is coordinated with the workspace's update queue.
