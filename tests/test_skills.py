"""Exercise sync in isolated homes, including a real Git clone and linked worktree.

Task is replaced only in orchestration tests. Set SKILLS_TEST_BASE to a checkout
of skills main to also exercise its real locked builder (first run needs network).
"""

import importlib.machinery
import fcntl
import io
import importlib.util
import json
import os
from pathlib import Path
import shutil
import select
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "dot_local/bin/executable_skills"
loader = importlib.machinery.SourceFileLoader("skills_sync", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
SYNC = importlib.util.module_from_spec(spec)
with patch("sys.dont_write_bytecode", True):
    loader.exec_module(SYNC)
FAKE_TASK = '''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
pathlib.Path(os.environ['SKILLS_CALLS']).write_text(json.dumps(args))
if os.environ.get('SKILLS_FAIL'):
    sys.exit(9)
(pathlib.Path(args[args.index('--base') + 1]) / '.venv').mkdir(exist_ok=True)
output = pathlib.Path(args[args.index('--output') + 1])
output.parent.mkdir(parents=True, exist_ok=True)
tree = output.parent / 'fixture-generation'
(tree / 'example').mkdir(parents=True, exist_ok=True)
(tree / 'example/SKILL.md').write_text(os.environ.get('SKILLS_TOKEN', 'built skill'))
if not output.is_symlink():
    output.symlink_to(tree)
'''


class SkillsFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="skills-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home with spaces"
        self.home.mkdir()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.overlay = self.root / "overlay"
        self.overlay_name = SYNC.PROJECT_OVERLAY
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "task").write_text(FAKE_TASK)
        (self.bin / "task").chmod(0o755)
        (self.bin / "uv").write_text(
            '#!/bin/sh\n'
            '[ -f "$HOME/.agents/skills/.skills-sync-view" ] || exit 12\n'
            'printf "%s\\n" "$@" > "$HOME/uv-calls"\n'
            'pwd >> "$HOME/uv-calls"\n'
            'exit ${SKILLS_UV_FAIL:-0}\n'
        )
        (self.bin / "uv").chmod(0o755)
        # All fixture repositories are disposable. Give the fixture Git an identity
        # so the rebase mutant can actually run and prove the ff-only guard.
        # Production continues to invoke the workspace's operator-aware PATH shim.
        (self.bin / "git").write_text(
            '#!/bin/sh\nexec /usr/bin/git -c user.name=Fixture '
            '-c user.email=fixture@example.test "$@"\n'
        )
        (self.bin / "git").chmod(0o755)
        self.calls = self.root / "calls.json"
        self.env = {**os.environ, "HOME": str(self.home), "SKILLS_CALLS": str(self.calls),
                    "PATH": f"{self.bin}:{os.environ['PATH']}"}
        self.remote = self.root / "remote"
        self.git("init", "--initial-branch=main", self.remote)
        (self.remote / "example").mkdir()
        (self.remote / "example/SKILL.md").write_text("base skill")
        self.git("-C", self.remote, "add", "example/SKILL.md")
        self.git("-C", self.remote, "commit", "-m", "test: fixture")
        self.shared = self.home / ".agents/skills"
        self.shared.parent.mkdir()
        self.git("clone", self.remote, self.shared)
        self.base = self.home / ".local/share/skills/base"
        self.output = self.base.parent / "generated/skills"
        self.claude = self.home / ".claude/skills"
        self.claude.mkdir(parents=True)
        (self.claude / "example").symlink_to(self.shared / "example")
        (self.claude / "removed").symlink_to(self.shared / "removed")
        (self.claude / "private").mkdir()
        (self.claude / "private/local.txt").write_text("Claude-only content")

    def git(self, *args):
        # Fixture commits only: the production script uses the registered git shim.
        return subprocess.run(
            ["/usr/bin/git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
             *map(str, args)], check=True, text=True, capture_output=True, env=self.env,
        ).stdout.strip()

    def sync(self, **env):
        return subprocess.run(
            [str(SCRIPT), "sync", "--workspace-root", str(self.workspace),
             "--workspace-overlay", str(self.overlay)],
            env={**self.env, **env}, text=True, capture_output=True, timeout=180,
        )

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS: skills sync", result.stdout)


class SkillsTests(SkillsFixture):
    def test_discovers_only_existing_overlays_in_sorted_project_order(self):
        self.overlay.mkdir()
        (self.workspace / "z project" / self.overlay_name).mkdir(parents=True)
        (self.workspace / "a project" / self.overlay_name).mkdir(parents=True)
        (self.workspace / "no-overlay").mkdir()
        (self.workspace / "file").touch()
        self.assert_ok(self.sync())
        self.assertEqual(json.loads(self.calls.read_text()), [
            "--dir", str(self.shared), "build", "--", "--base", str(self.shared),
            "--output", str(self.output), "--overlay", str(self.overlay),
            "--project", str(self.workspace / "a project"), str(self.workspace / "a project" / self.overlay_name),
            "--project", str(self.workspace / "z project"), str(self.workspace / "z project" / self.overlay_name),
        ])

    def test_discovery_skips_repository_containers_for_either_overlay_name(self):
        # The final folder decision changes one production constant, not the tests.
        for folder in ("skills", ".skills"):
            with self.subTest(folder=folder):
                root = self.workspace / folder
                (root / "bare" / folder / f"{folder}.git").mkdir(parents=True)
                (root / "gitdir" / folder / ".git").mkdir(parents=True)
                (root / "gitfile" / folder).mkdir(parents=True)
                (root / "gitfile" / folder / ".git").write_text("gitdir: elsewhere")
                # main/ is a valid skill name and is not a clone marker.
                (root / "normal" / folder / "main").mkdir(parents=True)
                (root / "empty" / folder).mkdir(parents=True)
                with patch("sys.stderr", new_callable=io.StringIO) as stderr:
                    layers = SYNC.discover_layers(root, self.overlay, project_overlay=folder)
                self.assertEqual(layers, [(root / "empty", root / "empty" / folder),
                                          (root / "normal", root / "normal" / folder)])
                for name in ("bare", "gitdir", "gitfile"):
                    self.assertIn(f"skipping repository clone at {root / name / folder}", stderr.getvalue())
                self.assertEqual(len(stderr.getvalue().splitlines()), 3)
        (self.overlay / ".git").mkdir(parents=True)
        with patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertTrue(all(project is not None for project, _ in
                                SYNC.discover_layers(self.workspace, self.overlay)))
        self.assertIn(f"skipping repository clone at {self.overlay}", stderr.getvalue())

    def test_migrates_clone_repairs_dirty_linked_worktree_and_keeps_backups(self):
        worktree = self.home / ".agents/worktrees/draft"
        self.git("-C", self.shared, "worktree", "add", "-b", "draft", worktree)
        (worktree / "example/SKILL.md").write_text("uncommitted draft")
        before = self.git("-C", worktree, "status", "--porcelain")
        self.assert_ok(self.sync())
        self.assertFalse(self.shared.is_symlink())
        self.assertTrue(SYNC.managed_view(self.shared))
        self.assertTrue((self.base / ".git").is_dir())
        self.assertTrue((self.home / "uv-calls").is_file(), "relocated environment is reinstalled")
        self.assertEqual((self.home / "uv-calls").read_text().splitlines(),
                         ["sync", "--frozen", "--reinstall", str(self.base)])
        self.assertEqual(self.git("-C", self.base, "status", "--porcelain"), "")
        self.assertTrue(Path((worktree / ".git").read_text().strip().removeprefix("gitdir: ")).is_dir(),
                        "worktree backlink points at a valid git directory")
        self.assertEqual(self.git("-C", worktree, "status", "--porcelain"), before)
        self.assertEqual(self.git("-C", worktree, "branch", "--show-current"), "draft")
        self.assertEqual((worktree / "example/SKILL.md").read_text(), "uncommitted draft")
        self.assertEqual((self.claude / "private/local.txt").read_text(), "Claude-only content")
        self.assertEqual(os.readlink(self.claude / "example"), str(self.shared / "example"))
        self.assertFalse((self.claude / "removed").is_symlink())
        self.assertTrue((self.claude.parent / "skills.before-sync/private/local.txt").exists())
        self.assertEqual((self.shared.parent / "skills.before-sync").resolve(), self.base)
        self.assert_ok(self.sync())
        self.assertEqual(json.loads(self.calls.read_text())[1], str(self.base))
        self.assertTrue(Path((worktree / ".git").read_text().strip().removeprefix("gitdir: ")).is_dir(),
                        "worktree backlink points at a valid git directory")
        self.assertEqual(self.git("-C", worktree, "status", "--porcelain"), before)
        # The documented rollback leaves local branches and dirty worktrees usable.
        shutil.rmtree(self.shared)
        self.base.rename(self.shared)
        self.git("-C", self.shared, "worktree", "repair")
        shutil.rmtree(self.claude)
        (self.claude.parent / "skills.before-sync").rename(self.claude)
        self.assertEqual((self.claude / "example/SKILL.md").read_text(), "base skill")
        self.assertTrue(Path((worktree / ".git").read_text().strip().removeprefix("gitdir: ")).is_dir(),
                        "worktree backlink points at a valid git directory")
        self.assertEqual(self.git("-C", worktree, "status", "--porcelain"), before)

    def test_stable_real_view_survives_generation_change(self):
        self.assert_ok(self.sync())
        root = self.shared.resolve()
        path = (self.shared / "example/SKILL.md").resolve()
        (self.output / "example/SKILL.md").write_text("old generation")
        self.assertEqual(path.read_text(), "built skill", "view is an independent copy")
        self.assert_ok(self.sync(SKILLS_TOKEN="second token"))
        self.assertEqual(self.shared.resolve(), root)
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_text(), "second token")
        self.assertFalse(any(item.is_symlink() for item in self.shared.rglob("*")))

    def test_build_failure_preserves_legacy_install_and_later_published_install(self):
        failed = self.sync(SKILLS_FAIL="1")
        self.assertNotEqual(failed.returncode, 0)
        self.assertNotIn("PASS: skills sync", failed.stdout)
        self.assertTrue(SYNC.managed_view(self.shared))
        self.assertTrue(json.loads((self.shared / SYNC.VIEW_MARKER).read_text())["fallback"])
        self.assertIn("base skills were published unbuilt", failed.stderr)
        self.assertEqual((self.claude / "example/SKILL.md").read_text(), "base skill")
        self.assert_ok(self.sync())
        generation = os.readlink(self.output)
        before = {str(p.relative_to(self.shared)): p.read_bytes()
                  for p in self.shared.rglob("*") if p.is_file()}
        inode = self.shared.stat().st_ino
        failed = self.sync(SKILLS_FAIL="1")
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(os.readlink(self.output), generation)
        self.assertEqual(self.shared.stat().st_ino, inode)
        self.assertEqual(before, {str(p.relative_to(self.shared)): p.read_bytes()
                                  for p in self.shared.rglob("*") if p.is_file()})
        self.assertEqual((self.shared / "example/SKILL.md").read_text(), "built skill")
        self.assertEqual((self.claude / "example/SKILL.md").read_text(), "built skill")

    def test_rebuild_with_task_failure_publishes_base_without_builder_output(self):
        self.assert_ok(self.sync())
        shutil.rmtree(self.home / ".agents")
        shutil.rmtree(self.base.parent)
        self.shared.parent.mkdir()
        # The dangling variant models the earlier output-link layout after rebuild.
        self.shared.symlink_to(self.output)
        with patch.dict(os.environ, {**self.env, "SKILLS_FAIL": "1"}), \
                patch.object(SYNC, "URL", str(self.remote)), \
                patch("sys.stderr", new_callable=io.StringIO) as stderr:
            with self.assertRaises(subprocess.CalledProcessError):
                SYNC.sync(self.home, self.workspace, self.overlay)
        self.assertIn("base skills were published unbuilt", stderr.getvalue())
        self.assertTrue(SYNC.managed_view(self.shared))
        self.assertTrue(json.loads((self.shared / SYNC.VIEW_MARKER).read_text())["fallback"])
        self.assertFalse(self.output.exists(), "fallback never calls the builder again")
        self.assertEqual((self.shared / "example/SKILL.md").read_text(), "base skill")
        self.assertTrue(all(item.exists() for item in self.claude.iterdir() if item.is_symlink()))
        previous = {str(p.relative_to(self.shared)): p.read_bytes()
                    for p in self.shared.rglob("*") if p.is_file()}
        inode = self.shared.stat().st_ino
        result = self.sync(SKILLS_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.shared.stat().st_ino, inode)
        self.assertEqual(previous, {str(p.relative_to(self.shared)): p.read_bytes()
                                   for p in self.shared.rglob("*") if p.is_file()})

    def test_missing_workspace_overlay_has_no_argument(self):
        args = SYNC.build_arguments(self.shared, self.output,
                                    SYNC.discover_layers(self.workspace, self.overlay))
        self.assertNotIn("--overlay", args, "missing workspace overlay must be omitted")

    def test_project_without_overlay_has_no_argument(self):
        (self.workspace / "no-overlay").mkdir()
        args = SYNC.build_arguments(self.shared, self.output,
                                    SYNC.discover_layers(self.workspace, self.overlay))
        self.assertNotIn("--project", args, "project without an overlay must be omitted")

    def test_fresh_home_clones_main(self):
        shutil.rmtree(self.shared)
        with patch.dict(os.environ, self.env), patch.object(SYNC, "URL", str(self.remote)):
            SYNC.sync(self.home, self.workspace, self.overlay)
        self.assertTrue((self.base / ".git").is_dir())
        self.assertEqual(self.git("-C", self.base, "branch", "--show-current"), "main")
        self.assertTrue(SYNC.managed_view(self.shared))

    def test_failed_clone_leaves_no_partial_checkout_and_can_retry(self):
        shutil.rmtree(self.shared)
        with patch.dict(os.environ, self.env), patch.object(SYNC, "URL", str(self.root / "missing")):
            with self.assertRaises(subprocess.CalledProcessError):
                SYNC.sync(self.home, self.workspace, self.overlay)
        self.assertFalse(self.base.exists())
        self.assertEqual(list(self.base.parent.glob(".clone-*")), [])
        with patch.dict(os.environ, self.env), patch.object(SYNC, "URL", str(self.remote)):
            SYNC.sync(self.home, self.workspace, self.overlay)
        self.assertTrue((self.base / ".git").is_dir())

    def test_chezmoi_installs_executable_command(self):
        (self.home / ".local/bin").mkdir(parents=True)
        result = subprocess.run(
            ["chezmoi", "--source", str(REPO), "--destination", str(self.home),
             "apply", "--exclude=scripts", str(self.home / ".local/bin/skills")],
            env=self.env, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        binary = self.home / ".local/bin/skills"
        self.assertTrue(os.access(binary, os.X_OK))
        self.assertEqual(binary.read_bytes(), SCRIPT.read_bytes())

    def test_dirty_or_branch_checkout_is_preserved_and_not_built(self):
        (self.shared / "example/SKILL.md").write_text("uncommitted")
        result = self.sync()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("clean checkout of main", result.stderr)
        self.assertFalse(self.calls.exists())
        self.assertEqual((self.shared / "example/SKILL.md").read_text(), "uncommitted")
        self.git("-C", self.shared, "checkout", "--", "example/SKILL.md")
        self.git("-C", self.shared, "checkout", "-b", "draft")
        result = self.sync()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git("-C", self.shared, "branch", "--show-current"), "draft")

    def test_detached_head_has_documented_error(self):
        self.git("-C", self.shared, "checkout", "--detach")
        result = self.sync()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be a clean checkout of main", result.stderr)
        self.assertNotIn("symbolic-ref", result.stderr)
        self.assertFalse(self.calls.exists())

    def test_retries_worktree_repair_even_when_next_build_fails(self):
        worktree = self.home / ".agents/worktrees/draft"
        self.git("-C", self.shared, "worktree", "add", "-b", "draft", worktree)
        (worktree / "example/SKILL.md").write_text("keep draft")
        self.assert_ok(self.sync())
        (worktree / ".git").write_text(f"gitdir: {self.shared}/.git/worktrees/draft\n")
        result = self.sync(SKILLS_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        target = Path((worktree / ".git").read_text().strip().removeprefix("gitdir: "))
        self.assertTrue(target.is_dir(), "stale backlink is repaired before the failed build")
        self.assertEqual(self.git("-C", worktree, "status", "--porcelain"), "M example/SKILL.md")

    def test_sync_lock_blocks_second_sync_without_touching_state(self):
        self.assert_ok(self.sync())
        state = self.base.parent
        before = {str(p.relative_to(state)): p.read_bytes()
                  for p in state.rglob("*") if p.is_file()}
        calls = self.calls.read_bytes()
        runner = 'import runpy,sys; script=sys.argv.pop(1); print("ready", flush=True); runpy.run_path(script, run_name="__main__")'
        with (state / "sync.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            process = subprocess.Popen(
                [sys.executable, "-u", "-c", runner, str(SCRIPT), "sync", "--workspace-root", str(self.workspace),
                 "--workspace-overlay", str(self.overlay)], env=self.env, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            def cleanup():
                if process.poll() is None:
                    process.kill()
                process.communicate()
            self.addCleanup(cleanup)
            try:
                self.assertTrue(select.select([process.stdout], [], [], 5)[0], "second process started")
                self.assertEqual(process.stdout.readline().strip(), "ready")
                with self.assertRaises(subprocess.TimeoutExpired, msg="sync must wait for sync.lock"):
                    process.wait(timeout=0.5)
                self.assertEqual(self.calls.read_bytes(), calls, "blocked sync did not invoke the builder")
                self.assertEqual(before, {str(p.relative_to(state)): p.read_bytes()
                                         for p in state.rglob("*") if p.is_file()},
                                 "blocked sync changed no clone or generated state")
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stdout + stderr)
            self.assertIn("PASS: skills sync", stdout)

    def test_fast_forward_updates_main_and_network_failure_keeps_current_source(self):
        (self.remote / "example/SKILL.md").write_text("updated")
        self.git("-C", self.remote, "add", "example/SKILL.md")
        self.git("-C", self.remote, "commit", "-m", "test: update")
        self.assert_ok(self.sync())
        self.assertEqual((self.base / "example/SKILL.md").read_text(), "updated")
        self.git("-C", self.base, "remote", "set-url", "origin", str(self.root / "missing"))
        result = self.sync()
        self.assert_ok(result)
        self.assertIn("building the current copy", result.stderr)
        self.assertEqual((self.base / "example/SKILL.md").read_text(), "updated")

    def test_diverged_main_is_never_rebased_or_reset(self):
        (self.shared / "local.txt").write_text("local commit")
        self.git("-C", self.shared, "add", "local.txt")
        self.git("-C", self.shared, "commit", "-m", "test: local commit")
        before = self.git("-C", self.shared, "rev-parse", "HEAD")
        (self.remote / "remote.txt").write_text("remote commit")
        self.git("-C", self.remote, "add", "remote.txt")
        self.git("-C", self.remote, "commit", "-m", "test: remote commit")
        result = self.sync()
        self.assert_ok(result)
        self.assertIn("building the current copy", result.stderr)
        self.assertEqual(self.git("-C", self.base, "rev-parse", "HEAD"), before)
        self.assertFalse((self.base / "remote.txt").exists())

    def test_two_clones_or_unmanaged_shared_view_are_refused(self):
        self.base.mkdir(parents=True)
        result = self.sync()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("reconcile", result.stderr)
        shutil.rmtree(self.shared)
        self.shared.symlink_to(self.remote)
        result = self.sync()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unmanaged skills path", result.stderr)
        self.assertFalse(self.calls.exists())

    def test_unmanaged_directory_and_invalid_marker_are_preserved(self):
        shutil.rmtree(self.shared)
        self.shared.mkdir()
        (self.shared / "keep.txt").write_text("unmanaged")
        for marker in (None, "invalid json", '{"version":1,"fallback":0}'):
            if marker is not None:
                (self.shared / SYNC.VIEW_MARKER).write_text(marker)
            result = self.sync()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unmanaged skills path", result.stderr)
            self.assertEqual((self.shared / "keep.txt").read_text(), "unmanaged")
            self.assertFalse(self.calls.exists())

    def test_claude_conflicts_are_refused_before_build_or_migration(self):
        (self.claude / "example").unlink()
        (self.claude / "example").mkdir()
        (self.claude / "example/SKILL.md").write_text("private skill")
        result = self.sync()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("conflicts", result.stderr)
        self.assertFalse(self.calls.exists())
        self.assertFalse(self.output.exists())
        self.assertTrue((self.shared / ".git").is_dir())
        self.assertEqual((self.claude / "example/SKILL.md").read_text(), "private skill")

    def test_claude_only_entries_and_existing_links_stay_in_place(self):
        private = self.claude / "private"
        unrelated = self.claude / "unrelated"
        unrelated.symlink_to(private)
        directory_inode = self.claude.stat().st_ino
        private_inode = private.stat().st_ino
        local_inode = (private / "local.txt").stat().st_ino
        original_link_inode = (self.claude / "example").lstat().st_ino
        self.assert_ok(self.sync())
        backup_bytes = (self.claude.parent / "skills.before-sync/private/local.txt").read_bytes()
        (private / "local.txt").write_text("edited after first sync")
        self.assert_ok(self.sync(SKILLS_TOKEN="new view"))
        self.assertEqual(self.claude.stat().st_ino, directory_inode)
        self.assertEqual(private.stat().st_ino, private_inode)
        self.assertEqual((private / "local.txt").stat().st_ino, local_inode)
        self.assertEqual((private / "local.txt").read_text(), "edited after first sync")
        self.assertEqual((self.claude / "example").lstat().st_ino, original_link_inode)
        self.assertTrue(unrelated.is_symlink())
        self.assertEqual((self.claude.parent / "skills.before-sync/private/local.txt").read_bytes(), backup_bytes)

    def test_directory_publication_restores_previous_if_second_rename_fails(self):
        destination = self.root / "published"
        destination.mkdir()
        (destination / "old.txt").write_text("retain me")
        stage = self.root / "stage"
        stage.mkdir()
        original = Path.rename
        def rename(path, target):
            if path == stage:
                raise OSError("second rename failed")
            return original(path, target)
        with patch.object(Path, "rename", rename):
            with self.assertRaisesRegex(OSError, "second rename failed"):
                SYNC.publish_directory(stage, destination)
        self.assertEqual((destination / "old.txt").read_text(), "retain me")

    def test_claude_whole_clone_link_becomes_per_skill_view_with_backup(self):
        shutil.rmtree(self.claude)
        self.claude.symlink_to(self.shared)
        self.assert_ok(self.sync())
        self.assertFalse(self.claude.is_symlink())
        self.assertEqual((self.claude / "example/SKILL.md").read_text(), "built skill")
        self.assertTrue((self.claude.parent / "skills.before-sync").is_symlink())

    def test_migration_failure_restores_clone_and_worktree_backlinks(self):
        worktree = self.home / ".agents/worktrees/draft"
        self.git("-C", self.shared, "worktree", "add", "-b", "draft", worktree)
        (worktree / "example/SKILL.md").write_text("dirty draft")
        with patch.dict(os.environ, self.env), patch.object(SYNC, "publish_directory", side_effect=OSError("disk full")):
            try:
                SYNC.sync(self.home, self.workspace, self.overlay)
            except (OSError, subprocess.CalledProcessError):
                pass
            else:
                self.fail("migration failure must propagate")
        self.assertTrue((self.shared / ".git").is_dir())
        self.assertFalse(self.base.exists())
        self.assertEqual(self.git("-C", worktree, "status", "--porcelain"), "M example/SKILL.md")
        self.assertEqual((self.claude / "example/SKILL.md").read_text(), "base skill")

    def interrupted_move(self, bridge):
        worktree = self.home / ".agents/worktrees/draft"
        self.git("-C", self.shared, "worktree", "add", "-b", "draft", worktree)
        (worktree / "example/SKILL.md").write_text("interrupted draft")
        shutil.rmtree(self.claude)
        self.claude.symlink_to(self.shared)
        self.base.parent.mkdir(parents=True)
        self.shared.rename(self.base)
        if bridge:
            self.shared.mkdir()
            (self.shared / SYNC.VIEW_MARKER).write_text(json.dumps({"version": 1, "fallback": False}))
        self.assert_ok(self.sync())
        self.assertTrue(SYNC.managed_view(self.shared))
        self.assertEqual((self.claude / "example/SKILL.md").read_text(), "built skill")
        self.assertEqual(self.git("-C", worktree, "status", "--porcelain"), "M example/SKILL.md")
        self.assertEqual((worktree / "example/SKILL.md").read_text(), "interrupted draft")

    def test_resumes_interrupted_move_before_source_bridge(self):
        self.interrupted_move(False)

    def test_resumes_interrupted_move_after_source_bridge(self):
        self.interrupted_move(True)

    def test_relocated_environment_failure_restores_live_source_view(self):
        result = self.sync(SKILLS_UV_FAIL="9")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.shared / ".git").is_dir())
        self.assertFalse(self.base.exists())
        self.assertEqual((self.claude / "example/SKILL.md").read_text(), "base skill")
        self.assert_ok(self.sync())

    def test_harness_proof_uses_disposable_claude_config(self):
        module_spec = importlib.util.spec_from_file_location("skills_proof", REPO / "tests/prove_skills_harnesses.py")
        proof = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(proof)
        before = set(self.claude.parent.iterdir())
        with proof.temporary_claude_config(self.home) as config:
            self.assertNotEqual(config, self.claude.parent)
            self.assertEqual((config / "skills").resolve(), self.claude)
            (config / ".claude.json").write_text("startup state")
            (config / "sessions").mkdir()
        self.assertFalse(config.exists(), "temporary Claude startup state is deleted")
        self.assertEqual(set(self.claude.parent.iterdir()), before)
        self.assertFalse((self.claude.parent / ".claude.json").exists())

    def test_apply_reports_failure_without_failing(self):
        binary = self.home / ".local/bin/skills"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\necho sync-called >&2\nexit 9\n")
        binary.chmod(0o755)
        result = subprocess.run(["sh", str(REPO / "run_after_update-skills.sh")],
                                env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("sync-called", result.stderr)
        self.assertIn("skills sync failed", result.stderr)


@unittest.skipUnless(os.environ.get("SKILLS_TEST_BASE"), "set SKILLS_TEST_BASE for real builder proof")
class RealBuilderTests(SkillsFixture):
    def setUp(self):
        super().setUp()
        (self.bin / "task").unlink()
        (self.bin / "uv").unlink()
        shutil.rmtree(self.shared)
        self.git("clone", os.environ["SKILLS_TEST_BASE"], self.shared)
        # Keep this snapshot fixed: it is the exact builder revision under proof.
        self.git("-C", self.shared, "remote", "set-url", "origin", str(self.root / "offline"))
        shutil.rmtree(self.claude)
        self.claude.mkdir()

    def test_real_build_overlay_and_failure(self):
        base_skill = self.shared / "standards/SKILL.md"
        original = base_skill.read_text()
        (self.overlay / "standards").mkdir(parents=True)
        (self.overlay / "standards/SKILL.md").write_text("Workspace proof token SKILLS_WORKSPACE_PROOF.\n")
        project = self.workspace / "a project"
        (project / self.overlay_name / "standards").mkdir(parents=True)
        (project / self.overlay_name / "standards/SKILL.md").write_text("Project proof token SKILLS_PROJECT_PROOF.\n")
        self.assert_ok(self.sync())
        built = (self.shared / "standards/SKILL.md").read_text()
        self.assertTrue(built.startswith(original.rstrip()))
        self.assertLess(built.index("SKILLS_WORKSPACE_PROOF"), built.index("SKILLS_PROJECT_PROOF"))
        self.assertIn(f"## Project: {project}", built)
        self.assertEqual((self.claude / "standards/SKILL.md").read_text(), built)
        self.assertEqual(self.git("-C", self.base, "status", "--porcelain"), "")
        self.assertTrue((self.base / ".venv").is_dir())
        self.assert_ok(self.sync())  # uv environment remains usable after the move.
        console = subprocess.run([str(self.base / ".venv/bin/pymarkdown"), "version"],
                                 env=self.env, capture_output=True, text=True)
        self.assertEqual(console.returncode, 0, console.stderr)
        previous = os.readlink(self.output)
        (self.overlay / "standards/SKILL.md").write_text("[broken](missing-proof.md)\n")
        result = self.sync()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(os.readlink(self.output), previous)
        self.assertEqual((self.shared / "standards/SKILL.md").read_text(), built)
        (self.overlay / "standards/SKILL.md").write_text("Workspace proof token SKILLS_WORKSPACE_PROOF.\n")
        (self.overlay / "standards/orphan.txt").write_text("orphan")
        result = self.sync()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(os.readlink(self.output), previous)
        self.assertEqual((self.shared / "standards/SKILL.md").read_text(), built)

        # With no optional layers, every published skill file matches the source.
        shutil.rmtree(self.overlay)
        shutil.rmtree(project / self.overlay_name)
        self.assert_ok(self.sync())
        expected = {}
        for skill in self.base.iterdir():
            if skill.is_dir() and (skill / "SKILL.md").is_file():
                for item in skill.rglob("*"):
                    if item.is_file():
                        expected[str(item.relative_to(self.base))] = item.read_bytes()
        actual = {str(item.relative_to(self.output)): item.read_bytes()
                  for skill in self.output.iterdir() if skill.is_dir()
                  for item in skill.rglob("*") if item.is_file()}
        self.assertEqual(actual, expected)

    def test_rebuild_with_invalid_overlay_keeps_persisted_claude_links_working(self):
        self.assert_ok(self.sync())
        original = (self.base / "standards/SKILL.md").read_bytes()
        shutil.rmtree(self.home / ".agents")
        shutil.rmtree(self.base.parent)
        (self.overlay / "standards").mkdir(parents=True)
        (self.overlay / "standards/SKILL.md").write_text("[broken](missing-proof.md)\n")
        with patch.dict(os.environ, self.env), patch.object(SYNC, "URL", os.environ["SKILLS_TEST_BASE"]):
            with self.assertRaises(subprocess.CalledProcessError):
                SYNC.sync(self.home, self.workspace, self.overlay)
        self.assertTrue(SYNC.managed_view(self.shared))
        self.assertTrue(json.loads((self.shared / SYNC.VIEW_MARKER).read_text())["fallback"])
        self.assertEqual((self.shared / "standards/SKILL.md").read_bytes(), original)
        links = [item for item in self.claude.iterdir() if item.is_symlink()]
        self.assertGreater(len(links), 0)
        self.assertTrue(all(item.exists() for item in links))
