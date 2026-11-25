"""prune 的 Git 工作区集成测试与失败路径测试。"""

import shutil
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


@pytest.fixture
def git_repo(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("需要 git")
    repo = tmp_path / "repo with spaces-清理"
    repo.mkdir()
    run_git(repo, "init", "-q")
    run_git(repo, "config", "user.email", "t@t")
    run_git(repo, "config", "user.name", "t")
    (repo / "tracked.txt").write_text("initial\n", encoding="utf-8")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-qm", "initial")
    return repo


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
