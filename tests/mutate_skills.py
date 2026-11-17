#!/usr/bin/env python3
"""Prove the sync's caller guards with targeted changes; exit nonzero on survivors."""

import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import unittest


TESTS = Path(__file__).with_name("test_skills.py")
spec = importlib.util.spec_from_file_location("test_skills", TESTS)
tests = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tests)
SOURCE = tests.SCRIPT.read_text()
MUTATIONS = [
    ("dirty guard", 'if branch != "main" or git(source, "status", "--porcelain"):',
     'if branch != "main":', "test_dirty_or_branch_checkout_is_preserved_and_not_built"),
    ("branch guard", 'if branch != "main" or git(source, "status", "--porcelain"):',
     'if git(source, "status", "--porcelain"):', "test_dirty_or_branch_checkout_is_preserved_and_not_built"),
    ("fast-forward guard", '"pull", "--ff-only", "--quiet"',
     '"pull", "--rebase", "--quiet"', "test_diverged_main_is_never_rebased_or_reset"),
    ("workspace omission", "if is_overlay(overlay):", "if True:",
     "test_no_optional_overlays_passes_no_optional_flags"),
    ("project omission", 'if project.is_dir() and is_overlay(candidate):',
     'if project.is_dir():', "test_discovers_only_existing_overlays_in_sorted_project_order"),
    ("project order", "sorted(workspace.iterdir())", "sorted(workspace.iterdir(), reverse=True)",
     "test_discovers_only_existing_overlays_in_sorted_project_order"),
    ("git metadata discovery guard", 'exists(path / ".git") or exists(path / f"{path.name}.git")',
     'exists(path / f"{path.name}.git")', "test_discovery_skips_repository_containers_for_either_overlay_name"),
    ("bare repository discovery guard", 'exists(path / ".git") or exists(path / f"{path.name}.git")',
     'exists(path / ".git")', "test_discovery_skips_repository_containers_for_either_overlay_name"),
    ("build failure propagation", "        run(*args)\n",
     "        try:\n            run(*args)\n        except subprocess.CalledProcessError:\n            pass\n",
     "test_build_failure_preserves_legacy_install_and_later_published_install"),
    ("worktree repair", '                run("git", "-C", base, "worktree", "repair")',
     '                pass', "test_migrates_clone_repairs_dirty_linked_worktree_and_keeps_backups"),
    ("stable output links", "destination.symlink_to(output / skill.name)",
     "destination.symlink_to(output.resolve() / skill.name)",
     "test_migrates_clone_repairs_dirty_linked_worktree_and_keeps_backups"),
    ("Claude conflict preflight", "        check_claude(claude, output, source, shared, inputs)",
     "        pass", "test_claude_conflicts_are_refused_before_build_or_migration"),
    ("source bridge", "                link(base, shared)", "                pass",
     "test_migrates_clone_repairs_dirty_linked_worktree_and_keeps_backups"),
    ("relocated environment", '                run("uv", "sync", "--frozen", "--reinstall", cwd=base)',
     '                pass', "test_migrates_clone_repairs_dirty_linked_worktree_and_keeps_backups"),
    ("migration rollback", "                base.rename(shared)",
     "                pass", "test_migration_failure_restores_clone_and_worktree_backlinks"),
]


def main():
    failed = []
    original_script = tests.SCRIPT
    original_module = tests.SYNC
    # Baseline must pass: a fixture failure is not a caught mutation.
    baseline = unittest.TestSuite(tests.SkillsTests(name) for name in sorted({m[3] for m in MUTATIONS}))
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        result = unittest.TextTestRunner(stream=io.StringIO()).run(baseline)
    if not result.wasSuccessful():
        print("FAIL: mutation baseline does not pass")
        return 1
    with tempfile.TemporaryDirectory(prefix="skills-mutations-") as directory:
        for index, (name, old, new, method) in enumerate(MUTATIONS):
            if SOURCE.count(old) != 1:
                print(f"FAIL: mutation {name} expected one target")
                failed.append(name)
                continue
            mutant = Path(directory) / f"skills-{index}"
            mutant.write_text(SOURCE.replace(old, new))
            mutant.chmod(0o755)
            tests.SCRIPT = mutant
            loader = tests.importlib.machinery.SourceFileLoader(f"mutant_{index}", str(mutant))
            spec = importlib.util.spec_from_loader(loader.name, loader)
            tests.SYNC = importlib.util.module_from_spec(spec)
            loader.exec_module(tests.SYNC)
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                result = unittest.TextTestRunner(stream=output).run(tests.SkillsTests(method))
            # Errors such as import failures do not prove that the assertion caught it.
            if result.failures and not result.errors:
                assertion = result.failures[0][1].strip().splitlines()[-1]
                print(f"CAUGHT: {name}; {method}; {assertion}")
            else:
                print(f"FAIL: {name}; survived or errored\n{output.getvalue()}")
                failed.append(name)
        tests.SCRIPT = original_script
        tests.SYNC = original_module
    if failed:
        return 1
    print(f"PASS: {len(MUTATIONS)}/{len(MUTATIONS)} sync guard mutations caught")
    return 0


if __name__ == "__main__":
    sys.exit(main())
