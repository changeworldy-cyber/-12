import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def pytest_configure(config):
    # 每次会话使用独立的 basetemp 子目录。pytest 启动时会清空 basetemp，而历史运行
    # 残留的 Git 对象文件带只读属性，Windows 下 rmtree 会因 WinError 5 失败。
    if config.option.basetemp is None:
        base = Path(config.rootpath) / ".pytest-tmp" / f"run-{os.getpid()}"
        config.option.basetemp = str(base)


@pytest.fixture
def tmp_path(tmp_path_factory):
    """Keep per-test directories short without using the system temp directory."""
    return tmp_path_factory.mktemp("t")


@pytest.fixture
def git_repo(tmp_path):
    import dispatch

    if shutil.which("git") is None:
        pytest.skip("需要 git")
    repo = tmp_path / "repo with spaces-清理"
    repo.mkdir()

    def run_git(*arguments):
        subprocess.run(
            ["git", *arguments], cwd=repo, check=True, capture_output=True,
            text=True, encoding="utf-8", env=dispatch.child_env(None),
        )

    if sys.platform == "win32":
        run_git("-c", "core.longpaths=true", "init", "-q")
        run_git("config", "core.longpaths", "true")
    else:
        run_git("init", "-q")
    run_git("config", "user.email", "t@t")
    run_git("config", "user.name", "t")
    (repo / "tracked.txt").write_text("initial\n", encoding="utf-8")
    run_git("add", "-A")
    run_git("commit", "-qm", "initial")
    return repo


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    if (
        sys.platform != "win32" or call.when == "teardown"
        or call.excinfo is None or "tmp_path" not in item.fixturenames
    ):
        return

    error = call.excinfo.value
    if isinstance(error, OSError) and getattr(error, "winerror", None) == 206:
        reason = f"Windows path length limit (WinError 206): {error.filename}"
    elif isinstance(error, subprocess.CalledProcessError):
        stderr = error.stderr or ""
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        if "filename too long" not in stderr.lower():
            return
        reason = f"Git for Windows path length limit: {stderr.strip()}"
    else:
        return

    report = outcome.get_result()
    report.outcome = "skipped"
    report.longrepr = (str(item.path), item.location[1] + 1, f"Skipped: {reason}")
