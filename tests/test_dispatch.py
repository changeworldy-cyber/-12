"""dispatch.py 的单元测试：只覆盖不产生副作用的纯逻辑与错误路径。"""

import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import dispatch


# ---------------------------------------------------------------- slugify

def test_slugify_basic():
    assert dispatch.slugify("Fix the bug") == "Fix-the-bug"


def test_slugify_collapses_punctuation():
    assert dispatch.slugify("a!!!b???c") == "a-b-c"


def test_slugify_keeps_cjk():
    assert dispatch.slugify("修掉登录 NPE") == "修掉登录-NPE"


def test_slugify_never_empty():
    assert dispatch.slugify("!!!") == "task"
    assert dispatch.slugify("") == "task"


def test_slugify_respects_limit():
    assert len(dispatch.slugify("x" * 200)) <= 32


def test_slugify_strips_edge_dashes():
    assert not dispatch.slugify("---abc---").startswith("-")


# ---------------------------------------------------------------- child_env

def test_child_env_drops_inherited_proxy(monkeypatch):
    monkeypatch.setenv("http_proxy", "http://sandbox.invalid:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://sandbox.invalid:1")
    env = dispatch.child_env(None)
    assert "http_proxy" not in env
    assert "HTTPS_PROXY" not in env


def test_child_env_applies_given_proxy():
    env = dispatch.child_env("http://127.0.0.1:7897")
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:7897"
    assert env["ALL_PROXY"] == "http://127.0.0.1:7897"


def test_child_env_keeps_localhost_out_of_proxy():
    env = dispatch.child_env("http://127.0.0.1:7897")
    assert "localhost" in env["NO_PROXY"]
    assert "127.0.0.1" in env["NO_PROXY"]


# ---------------------------------------------------------------- 事件解析

def test_parse_codex_events_takes_last_agent_message():
    payload = b"\n".join([
        b'{"type":"item.completed","item":{"type":"agent_message","text":"first"}}',
        b'{"type":"item.completed","item":{"type":"agent_message","text":"second"}}',
        b'{"type":"turn.completed","usage":{"input_tokens":123,"output_tokens":7}}',
    ])
    last, usage = dispatch.parse_codex_events(payload)
    assert last == "second"
    assert usage["input_tokens"] == 123


def test_parse_codex_events_ignores_noise():
    last, usage = dispatch.parse_codex_events(b"not json\n{broken\n")
    assert last == ""
    assert usage == {}


def test_parse_dsh_events_reads_final():
    payload = b'{"type":"text","text":"draft"}\n{"type":"final","text":"done"}\n'
    last, _ = dispatch.parse_dsh_events(payload)
    assert last == "done"


# ---------------------------------------------------------------- 任务清单

def test_load_tasks_text_with_names(tmp_path):
    spec = tmp_path / "tasks.txt"
    spec.write_text("# 注释\nadd :: 加功能\n\n纯任务\n", encoding="utf-8")
    assert dispatch.load_tasks(spec) == [("add", "加功能"), ("", "纯任务")]


def test_load_tasks_json_strings(tmp_path):
    spec = tmp_path / "tasks.json"
    spec.write_text(json.dumps(["a", "b"]), encoding="utf-8")
    assert dispatch.load_tasks(spec) == [("", "a"), ("", "b")]


def test_load_tasks_json_objects(tmp_path):
    spec = tmp_path / "tasks.json"
    spec.write_text(json.dumps([{"name": "n1", "task": "t1"}]), encoding="utf-8")
    assert dispatch.load_tasks(spec) == [("n1", "t1")]


# ---------------------------------------------------------------- 后端选择

def test_resolve_backend_passthrough():
    assert dispatch.resolve_backend("codex") == "codex"


def test_resolve_backend_auto_picks_available():
    assert dispatch.resolve_backend("auto") in ("codex", "dsh")


# ---------------------------------------------------------------- 工作区

def test_prepare_worktree_rejects_non_git(tmp_path, monkeypatch):
    # 注入 git 失败返回值，避免依赖测试目录是否恰好位于某个仓库内
    monkeypatch.setattr(dispatch, "git", lambda *a, **k: (128, "fatal: not a git repository"))
    opts = SimpleNamespace(isolate=False, name="x")
    with pytest.raises(SystemExit):
        dispatch.prepare_worktree(tmp_path, opts, "x")


@pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")
def test_prepare_worktree_isolation_creates_branch(git_repo):
    repo = git_repo

    opts = SimpleNamespace(isolate=True, name="fix-a")
    wt, branch, cleanup = dispatch.prepare_worktree(repo, opts, "fix-a")

    assert wt == repo / ".worktrees" / "fix-a"
    assert wt.is_dir()
    assert branch == "wb/fix-a"
    assert cleanup == 1


# ---------------------------------------------------------------- 报告

def test_report_roundtrip(tmp_path):
    rep = dispatch.Report(
        runId="r1", backend="codex", repo=str(tmp_path), worktree=str(tmp_path),
        branch=None, isolated=False, task="t")
    path = tmp_path / "report.json"
    path.write_text(json.dumps(dispatch.asdict(rep), ensure_ascii=False), encoding="utf-8")

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["runId"] == "r1"
    assert data["status"] == "running"
    assert data["filesChanged"] == []


def test_launcher_resolves_plain_executable():
    out = dispatch.launcher(sys.executable)
    assert out and Path(out[0]).exists()
