"""prune 的 Git 工作区集成测试与失败路径测试。"""

import shlex
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import dispatch


def run_git(repo, *arguments):
    completed = subprocess.run(
        ["git", *arguments], cwd=repo, check=True, capture_output=True,
        text=True, encoding="utf-8", env=dispatch.child_env(None),
    )
    return completed.stdout


def create_worktree(repo, name="task", branch=None):
    worktree = repo / ".worktrees" / name
    worktree.parent.mkdir(parents=True, exist_ok=True)
    run_git(repo, "worktree", "add", "-b", branch or f"wb/{name}", str(worktree), "HEAD")
    return worktree


def commit_change(worktree):
    (worktree / "tracked.txt").write_text("committed\n", encoding="utf-8")
    run_git(worktree, "add", "tracked.txt")
    run_git(worktree, "commit", "-qm", "task change")


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("change", ["unstaged", "staged", "untracked"])
def test_prune_skips_uncommitted_changes(git_repo, capsys, force, change):
    worktree = create_worktree(git_repo)
    if change == "untracked":
        (worktree / "pending.txt").write_text("pending\n", encoding="utf-8")
        run_git(git_repo, "config", "status.showUntrackedFiles", "no")
    else:
        (worktree / "tracked.txt").write_text("pending\n", encoding="utf-8")
        if change == "staged":
            run_git(worktree, "add", "tracked.txt")

    original_status = run_git(worktree, "status", "--porcelain", "--untracked-files=all")
    original_branch = run_git(git_repo, "rev-parse", "refs/heads/wb/task")
    assert dispatch.cmd_prune(SimpleNamespace(repo=str(git_repo), force=force)) == 0

    output = capsys.readouterr().out
    assert "跳过" in output
    assert "未提交改动" in output
    assert "wb/task" in output
    assert str(worktree) in output
    assert "将清理" not in output
    assert worktree.is_dir()
    assert run_git(git_repo, "rev-parse", "refs/heads/wb/task") == original_branch
    assert run_git(worktree, "status", "--porcelain", "--untracked-files=all") == original_status


@pytest.mark.parametrize("force", [False, True])
def test_prune_removes_merged_worktree_only_with_force(git_repo, monkeypatch, capsys, force):
    worktree = create_worktree(git_repo)
    commit_change(worktree)
    run_git(git_repo, "merge", "--ff-only", "wb/task")
    original_head = run_git(git_repo, "rev-parse", "HEAD")
    argv = ["dispatch.py", "prune", "--repo", str(git_repo)]
    if force:
        argv.append("--force")
    monkeypatch.setattr(sys, "argv", argv)

    assert dispatch.main() == 0

    output = capsys.readouterr().out
    assert str(worktree) in output
    assert "wb/task" in output
    assert run_git(git_repo, "rev-parse", "HEAD") == original_head
    if force:
        assert "已清理" in output
        assert not worktree.exists()
        assert not run_git(git_repo, "branch", "--list", "wb/task").strip()
        assert worktree.as_posix() not in run_git(git_repo, "worktree", "list", "--porcelain", "-z")
    else:
        assert "[dry-run]" in output
        assert "将清理" in output
        assert worktree.is_dir()
        assert run_git(git_repo, "branch", "--list", "wb/task").strip()


@pytest.mark.parametrize("force", [False, True])
def test_prune_skips_unmerged_branch(git_repo, capsys, force):
    worktree = create_worktree(git_repo)
    commit_change(worktree)

    assert dispatch.cmd_prune(SimpleNamespace(repo=str(git_repo), force=force)) == 0

    output = capsys.readouterr().out
    assert "尚未合并到当前 HEAD" in output
    assert worktree.is_dir()
    assert run_git(git_repo, "branch", "--list", "wb/task").strip()


@pytest.mark.parametrize("kind", ["outside", "other_branch", "detached"])
def test_prune_preserves_out_of_scope_worktrees(git_repo, kind):
    branch = "wb/task"
    if kind == "outside":
        worktree = git_repo / ".worktrees-other" / "task"
        worktree.parent.mkdir()
        run_git(git_repo, "worktree", "add", "-b", branch, str(worktree), "HEAD")
    elif kind == "other_branch":
        branch = "feature/task"
        worktree = create_worktree(git_repo, branch=branch)
    else:
        branch = None
        worktree = git_repo / ".worktrees" / "task"
        worktree.parent.mkdir()
        run_git(git_repo, "worktree", "add", "--detach", str(worktree), "HEAD")

    assert dispatch.cmd_prune(SimpleNamespace(repo=str(git_repo), force=True)) == 0

    assert worktree.is_dir()
    if branch:
        assert run_git(git_repo, "branch", "--list", branch).strip()


@pytest.mark.parametrize("path_kind", ["outside", "sibling", "traversal", "root"])
def test_prune_rejects_outside_removal_paths(git_repo, monkeypatch, path_kind):
    paths = {
        "outside": git_repo.parent / "outside",
        "sibling": git_repo / ".worktrees-other" / "task",
        "traversal": git_repo / ".worktrees" / ".." / "outside",
        "root": git_repo / ".worktrees",
    }
    commands = []
    monkeypatch.setattr(dispatch, "git", lambda *arguments: commands.append(arguments) or (0, ""))

    with pytest.raises(ValueError, match="路径越界"):
        dispatch.remove_prune_worktree(git_repo, paths[path_kind])

    assert commands == []


@pytest.mark.parametrize("force", [False, True])
def test_prune_reports_escaping_candidate(git_repo, monkeypatch, capsys, force):
    worktree = git_repo / "outside"
    run_git(git_repo, "worktree", "add", "-b", "wb/task", str(worktree), "HEAD")
    worktrees_root = git_repo / ".worktrees"
    worktrees_root.mkdir()
    escaping_path = worktrees_root / ".." / "outside"
    original_git = dispatch.git
    commands = []

    def escaping_listing(repo, *arguments):
        commands.append(arguments)
        if arguments[:2] == ("worktree", "list"):
            return 0, f"worktree {escaping_path}\0branch refs/heads/wb/task\0\0"
        return original_git(repo, *arguments)

    monkeypatch.setattr(dispatch, "git", escaping_listing)
    assert dispatch.cmd_prune(SimpleNamespace(repo=str(git_repo), force=force)) == 1

    output = capsys.readouterr().out
    assert "路径越界" in output
    assert str(worktrees_root) in output
    assert str(worktree) in output
    assert commands == [("worktree", "list", "--porcelain", "-z")]
    assert (worktree / "tracked.txt").read_text(encoding="utf-8") == "initial\n"
    assert run_git(git_repo, "branch", "--list", "wb/task").strip()


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("redirect", ["worktree", "root"])
def test_prune_rejects_resolved_path_escape(git_repo, monkeypatch, capsys, force, redirect):
    worktree = create_worktree(git_repo)
    outside = git_repo / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep\n", encoding="utf-8")
    redirected_path = worktree if redirect == "worktree" else git_repo / ".worktrees"
    original_resolve = Path.resolve
    original_git = dispatch.git
    commands = []

    def redirected_resolve(path, *arguments, **kwargs):
        if path == redirected_path:
            return outside
        return original_resolve(path, *arguments, **kwargs)

    def recording_git(repo, *arguments):
        commands.append(arguments)
        return original_git(repo, *arguments)

    monkeypatch.setattr(Path, "resolve", redirected_resolve)
    monkeypatch.setattr(dispatch, "git", recording_git)
    assert dispatch.cmd_prune(SimpleNamespace(repo=str(git_repo), force=force)) == 1

    output = capsys.readouterr().out
    assert "路径越界" in output
    assert str(outside) in output
    assert all(arguments[:2] not in (("worktree", "remove"), ("branch", "-d")) for arguments in commands)
    assert worktree.is_dir()
    assert marker.read_text(encoding="utf-8") == "keep\n"
    assert run_git(git_repo, "branch", "--list", "wb/task").strip()


def test_prune_rechecks_scope_before_force_retry(git_repo, monkeypatch, capsys):
    worktree = create_worktree(git_repo)
    outside = git_repo / "outside"
    outside.mkdir()
    original_resolve = Path.resolve
    original_git = dispatch.git
    remove_commands = []
    escaped = False

    def redirected_resolve(path, *arguments, **kwargs):
        if escaped and path == worktree:
            return outside
        return original_resolve(path, *arguments, **kwargs)

    def failing_git(repo, *arguments):
        nonlocal escaped
        if arguments[:2] == ("worktree", "remove"):
            remove_commands.append(arguments)
            escaped = True
            return 1, "Directory not empty"
        return original_git(repo, *arguments)

    monkeypatch.setattr(Path, "resolve", redirected_resolve)
    monkeypatch.setattr(dispatch, "git", failing_git)
    assert dispatch.cmd_prune(SimpleNamespace(repo=str(git_repo), force=True)) == 1

    output = capsys.readouterr().out
    assert "路径越界" in output
    assert "Remove-Item" not in output
    assert "rm -rf" not in output
    assert remove_commands == [("worktree", "remove", str(worktree))]
    assert worktree.is_dir()
    assert outside.is_dir()
    assert run_git(git_repo, "branch", "--list", "wb/task").strip()


def test_prune_skips_locked_worktree(git_repo, capsys):
    worktree = create_worktree(git_repo)
    run_git(git_repo, "worktree", "lock", "--reason", "active task", str(worktree))

    assert dispatch.cmd_prune(SimpleNamespace(repo=str(git_repo), force=True)) == 0

    assert "已锁定" in capsys.readouterr().out
    assert worktree.is_dir()
    assert run_git(git_repo, "branch", "--list", "wb/task").strip()


@pytest.mark.parametrize("failed_operation", ["worktree", "branch"])
def test_prune_reports_cleanup_failure(git_repo, monkeypatch, capsys, failed_operation):
    worktree = create_worktree(git_repo)
    original_git = dispatch.git
    commands = []
    failed_prefix = ("worktree", "remove") if failed_operation == "worktree" else ("branch", "-d")

    def failing_git(repo, *arguments):
        commands.append(arguments)
        if arguments[:2] == failed_prefix:
            return 1, "simulated cleanup failure"
        return original_git(repo, *arguments)

    monkeypatch.setattr(dispatch, "git", failing_git)
    assert dispatch.cmd_prune(SimpleNamespace(repo=str(git_repo), force=True)) == 1

    assert "失败" in capsys.readouterr().out
    assert run_git(git_repo, "branch", "--list", "wb/task").strip()
    if failed_operation == "worktree":
        assert worktree.is_dir()
        assert all(arguments[:2] != ("branch", "-d") for arguments in commands)
    else:
        assert not worktree.exists()


def test_prune_retries_readonly_remnants_with_force(git_repo, monkeypatch, capsys):
    worktree = create_worktree(git_repo)
    remnant = worktree / ".pytest-tmp" / "objects" / "ab" / "readonly-object"
    remnant.parent.mkdir(parents=True)
    remnant.write_bytes(b"readonly Git object")
    with (git_repo / ".git" / "info" / "exclude").open("a", encoding="utf-8") as excludes:
        excludes.write("\n.pytest-tmp/\n")
    remnant.chmod(stat.S_IREAD)
    original_git = dispatch.git
    remove_commands = []

    def readonly_git(repo, *arguments):
        if arguments[:2] == ("worktree", "remove"):
            remove_commands.append(arguments)
            assert remnant.is_file()
            assert remnant.stat().st_mode & stat.S_IWUSR == 0
            if "--force" not in arguments:
                return 1, f"failed to delete {remnant.parent}: Directory not empty"
            remnant.chmod(stat.S_IREAD | stat.S_IWRITE)
        return original_git(repo, *arguments)

    monkeypatch.setattr(dispatch, "git", readonly_git)
    try:
        assert not run_git(worktree, "status", "--porcelain", "--untracked-files=all").strip()
        assert dispatch.cmd_prune(SimpleNamespace(repo=str(git_repo), force=True)) == 0
    finally:
        if remnant.exists():
            remnant.chmod(stat.S_IREAD | stat.S_IWRITE)

    output = capsys.readouterr().out
    assert "Directory not empty" in output
    assert "重试 git worktree remove --force" in output
    assert "已清理" in output
    assert remove_commands == [
        ("worktree", "remove", str(worktree)),
        ("worktree", "remove", "--force", str(worktree)),
    ]
    assert not worktree.exists()
    assert not run_git(git_repo, "branch", "--list", "wb/task").strip()


@pytest.mark.parametrize("retry_error", ["not a working tree", "NOT A WORKING TREE"])
def test_prune_deletes_branch_after_worktree_is_unregistered(git_repo, monkeypatch, capsys, retry_error):
    worktree = create_worktree(git_repo)
    remnant = worktree / ".pytest-tmp" / "leftover"
    original_git = dispatch.git
    remove_commands = []

    def unregistered_git(repo, *arguments):
        if arguments[:2] == ("worktree", "remove"):
            remove_commands.append(arguments)
            code, output = original_git(repo, *arguments)
            if "--force" not in arguments:
                assert code == 0
                remnant.parent.mkdir(parents=True)
                remnant.write_text("leftover\n", encoding="utf-8")
                return 1, f"failed to delete {worktree}: Directory not empty"
            assert code != 0
            assert "not a working tree" in output.lower()
            return code, f"fatal: '{worktree}' is {retry_error}"
        return original_git(repo, *arguments)

    monkeypatch.setattr(dispatch, "git", unregistered_git)
    monkeypatch.setattr(sys, "argv", ["dispatch.py", "prune", "--repo", str(git_repo), "--force"])
    assert dispatch.main() == 0

    output = capsys.readouterr().out
    assert "Directory not empty" in output
    assert "重试 git worktree remove --force" in output
    assert "已清理" in output
    assert remove_commands == [
        ("worktree", "remove", str(worktree)),
        ("worktree", "remove", "--force", str(worktree)),
    ]
    assert remnant.is_file()
    assert not run_git(git_repo, "branch", "--list", "wb/task").strip()
    assert worktree.as_posix() not in run_git(git_repo, "worktree", "list", "--porcelain", "-z")


def test_prune_reports_manual_cleanup_after_force_retry_failure(git_repo, monkeypatch, capsys):
    branch = "wb/task'quoted"
    worktree = create_worktree(git_repo, name="task'quoted", branch=branch)
    remnants = worktree / ".pytest-tmp"
    remnants.mkdir()
    original_git = dispatch.git
    commands = []

    def failing_git(repo, *arguments):
        commands.append(arguments)
        if arguments[:2] == ("worktree", "remove"):
            return 1, "Permission denied" if "--force" in arguments else "Directory not empty"
        return original_git(repo, *arguments)

    monkeypatch.setattr(dispatch, "git", failing_git)
    assert dispatch.cmd_prune(SimpleNamespace(repo=str(git_repo), force=True)) == 1

    output = capsys.readouterr().out
    assert "Directory not empty" in output
    assert "Permission denied" in output
    assert "手工清理残留目录" in output
    assert str(worktree) in output
    assert str(remnants) in output
    if sys.platform == "win32":
        quote = lambda value: "'" + value.replace("'", "''") + "'"
        assert f"Remove-Item -LiteralPath {quote(str(worktree))} -Recurse -Force" in output
    else:
        quote = shlex.quote
        assert f"chmod -R u+w -- {quote(str(worktree))}" in output
        assert f"rm -rf -- {quote(str(worktree))}" in output
    assert f"git -C {quote(str(git_repo))} worktree remove --force {quote(str(worktree))}" in output
    assert f"git -C {quote(str(git_repo))} branch -d -- {quote(branch)}" in output
    assert [arguments for arguments in commands if arguments[:2] == ("worktree", "remove")] == [
        ("worktree", "remove", str(worktree)),
        ("worktree", "remove", "--force", str(worktree)),
    ]
    assert all(arguments[:2] != ("branch", "-d") for arguments in commands)
    assert worktree.is_dir()
    assert run_git(git_repo, "branch", "--list", branch).strip()


@pytest.mark.parametrize("failed_command", ["status", "merge-base"])
def test_prune_preserves_worktree_when_safety_check_fails(git_repo, monkeypatch, capsys, failed_command):
    worktree = create_worktree(git_repo)
    original_run_cmd = dispatch.run_cmd

    def failing_command(arguments, cwd, env, timeout, stdin_data=None):
        if arguments[1] == failed_command:
            return 128, b"", b"simulated check failure", 0.0
        return original_run_cmd(arguments, cwd, env, timeout, stdin_data)

    monkeypatch.setattr(dispatch, "run_cmd", failing_command)
    assert dispatch.cmd_prune(SimpleNamespace(repo=str(git_repo), force=True)) == 1

    assert "失败" in capsys.readouterr().out
    assert worktree.is_dir()
    assert run_git(git_repo, "branch", "--list", "wb/task").strip()


def test_prune_uses_status_stdout_for_cleanliness(git_repo, monkeypatch):
    worktree = create_worktree(git_repo)
    original_run_cmd = dispatch.run_cmd

    def warning_command(arguments, cwd, env, timeout, stdin_data=None):
        code, output, error, duration = original_run_cmd(arguments, cwd, env, timeout, stdin_data)
        if arguments[1] == "status":
            error += b"\nconfig warning"
        return code, output, error, duration

    monkeypatch.setattr(dispatch, "run_cmd", warning_command)
    assert dispatch.cmd_prune(SimpleNamespace(repo=str(git_repo), force=True)) == 0

    assert not worktree.exists()
    assert not run_git(git_repo, "branch", "--list", "wb/task").strip()


def test_prune_reports_worktree_list_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dispatch, "git", lambda *arguments: (128, "simulated list failure"))

    assert dispatch.cmd_prune(SimpleNamespace(repo=str(tmp_path), force=True)) == 1

    assert "读取 worktree 列表失败" in capsys.readouterr().out


def test_prune_continues_after_skipping_dirty_worktree(git_repo, capsys):
    dirty = create_worktree(git_repo, "dirty")
    ready = create_worktree(git_repo, "ready")
    (dirty / "pending.txt").write_text("pending\n", encoding="utf-8")

    assert dispatch.cmd_prune(SimpleNamespace(repo=str(git_repo), force=True)) == 0

    output = capsys.readouterr().out
    assert "未提交改动" in output
    assert "已清理" in output
    assert dirty.is_dir()
    assert run_git(git_repo, "branch", "--list", "wb/dirty").strip()
    assert not ready.exists()
    assert not run_git(git_repo, "branch", "--list", "wb/ready").strip()


def test_prune_help_exposes_repo_and_force():
    completed = subprocess.run(
        [sys.executable, str(Path(dispatch.__file__).resolve()), "prune", "--help"],
        capture_output=True, text=True, encoding="utf-8", env=dispatch.child_env(None),
    )

    assert completed.returncode == 0
    assert "--repo" in completed.stdout
    assert "--force" in completed.stdout
