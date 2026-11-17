# Skills

Run `skills sync` after changing a workspace or project overlay. Start a new agent
session afterwards so it discovers the current skills. `chezmoi apply` runs the
same sync after installing the command. A failed sync is reported without failing
the apply; run it directly to get a nonzero exit status for automation.

The command updates the writable clone of
[wyrd-company/skills](https://github.com/wyrd-company/skills) at
`~/.local/share/skills/base`, only on a clean `main` and only by fast-forward.
Uncommitted changes, a different branch or a detached HEAD stop the sync; make
skill changes in worktrees. A failed pull reports a warning and builds the
current checkout, allowing a cached installation to work offline.

## Building

The command passes absolute paths to the clone's Taskfile, in this order:

1. The base clone.
2. `/skills`, when that directory exists.
3. Each `/workspace/*/.skills` directory, ordered by its project path.

Projects without `.skills/` contribute no arguments. A candidate containing
`.git` (file or directory) or `<folder-name>.git` is a repository clone; it is
skipped with a warning on stderr. `main/` is a valid skill name and is included.
The project folder name is the `PROJECT_OVERLAY` constant in the command. Its
interim value `.skills` awaits the operator's decision on
[skills#27](https://github.com/wyrd-company/skills/issues/27); merge is held until
that decision matches the constant. Merge rules, validation and
the exact build interface belong to the skills repository's
[CONTRIBUTING.md](https://github.com/wyrd-company/skills/blob/main/CONTRIBUTING.md#building-overlays-the-interface-for-skills-sync).
Task, uv and Python 3.11 or newer must be installed. Task and uv are in the
Brewfile; Python is part of the workspace environment. The first build needs
network access for `uv sync --frozen`, which writes the ignored `.venv/` inside
the clone. Prewarm it while online with:

```sh
task --dir "$HOME/.local/share/skills/base" tools
```

The builder checks and publishes `~/.local/share/skills/generated/skills`, a
managed symlink. Keep that path stable and reserve its parent for the builder's
lock and generation directories. Failed builds leave the last successful
output in place. `skills sync` also locks updates and migration for that HOME.

Codex and Pi discover `~/.agents/skills`, a link through the managed output.
Claude Code discovers individual links in `~/.claude/skills`, also through that
output. Claude-only skill folders and unrelated links survive; a Claude-only
skill with the same name as a built skill is refused so its data is preserved.
Old managed links to removed skills are pruned. Links never pin a generation.

To check a copy in an isolated HOME, override the two discovery paths:

```sh
HOME=/path/to/scratch-home skills sync \
  --workspace-root /path/to/scratch-workspace \
  --workspace-overlay /path/to/scratch-overlay
```

## Moving an existing installation

The first successful sync adopts the real clone previously at `~/.agents/skills`
as `~/.local/share/skills/base`. It builds before moving anything. The complete
clone, local branches and Git worktree metadata are retained. Linked worktrees
stay where they are; `git worktree repair` updates their backlinks, preserving
uncommitted content. The migrated clone gets one `uv sync --frozen --reinstall`
to repair absolute console-script paths in `.venv`; a temporary source link keeps
legacy consumers reading skills until the generated view is ready. Do this when agents using the clone have stopped editing it.
The two directories must be on the same filesystem; a cross-filesystem rename
fails safely before moving the clone.

`~/.agents/skills.before-sync` points to the relocated source clone, and
`~/.claude/skills.before-sync` holds Claude's previous directory or whole-clone
link. Later syncs retain these backups. If both a new base and a legacy clone
exist, sync refuses to choose between them; inspect them and reconcile them
before retrying. Chezmoi's external manager no longer owns either clone path.

To undo a completed legacy migration, stop syncs and agents editing the source,
then run these commands after checking the backup paths exist. Retain the
current Claude view for inspection:

```sh
test -L "$HOME/.agents/skills"
test -d "$HOME/.local/share/skills/base/.git"
test -e "$HOME/.claude/skills.before-sync"
test ! -e "$HOME/.claude/skills.after-sync"
rm "$HOME/.agents/skills"
mv "$HOME/.local/share/skills/base" "$HOME/.agents/skills"
git -C "$HOME/.agents/skills" worktree repair
uv sync --directory "$HOME/.agents/skills" --frozen --reinstall
mv "$HOME/.claude/skills" "$HOME/.claude/skills.after-sync"
mv "$HOME/.claude/skills.before-sync" "$HOME/.claude/skills"
rm "$HOME/.agents/skills.before-sync"
```

Run this as a script with `set -e` so a failed check stops recovery. The generated
output is independent and can stay for inspection. Do not run `skills sync`
again until ready to migrate again; the next chezmoi apply also attempts it.

If interrupted between moving the clone and creating the new shared link,
`~/.local/share/skills/base` still holds the complete clone. Rerun `skills sync`
to finish, or restore it to `~/.agents/skills` and run `git worktree repair` before
restoring Claude's saved view. A failed migration repairs backlinks when it
restores the clone automatically. Never delete worktrees or `.git/worktrees` as
part of recovery.

## Verification

```sh
python3 -m unittest discover -s tests -p test_skills.py -v
SKILLS_TEST_BASE=/absolute/path/to/skills-main \
  python3 -m unittest discover -s tests -p test_skills.py -v
python3 tests/mutate_skills.py
```

The default tests use a local Git remote and fake Task/uv commands to check discovery,
updates, migration, backups and apply failure handling. The opt-in real builder
test clones the specified checkout into a scratch HOME and proves workspace and
project appends, unchanged base-only output, `.venv` relocation, and last-good
output after broken links and orphan files. It needs Task, uv, Python and network
or a warm uv cache. It never changes the running account's skills.

With the three harnesses installed, list their discovered skills and verify a
unique appended token at the skill paths, without calling a model:

```sh
python3 tests/prove_skills_harnesses.py \
  --home /path/to/synced-scratch-home --cwd /path/to/scratch-project \
  --skill standards --token UNIQUE_OVERLAY_TOKEN
```

This initializes Claude Code's normal command list, calls Codex's `skills/list`
API, and uses Pi's installed default resource loader. It checks every built
skill is listed. Codex and Pi report the skill path; Claude reports user scope,
so the script checks its configured user skills path. CLI initialization may
write account state in the selected HOME; use a scratch HOME for branch proof.
