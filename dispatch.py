#!/usr/bin/env python
"""wb-dispatch — 由 WorkBuddy 指挥外部 CLI 编码代理（Codex / DeepSeek Harness）干活的统一调度层.

设计要点
--------
1. 统一语义：两家后端都是「任务进 → 最终答案出 → 退出码」，因此用同一层抽象。
2. 隔离执行：默认给每个任务开独立 git worktree + 独立分支，互不污染。
3. 结构化交付：每次运行落盘 report.json / events.jsonl / last.md，便于审查与回溯。
4. stdin 恒接 /dev/null：Codex 在 stdin 非 TTY 时会等待管道 EOF，不接会永久挂起。
5. 代理可覆盖：WorkBuddy 沙箱代理只放行白名单，需显式走本机代理端口。

用法
----
  python dispatch.py doctor
  python dispatch.py run --repo D:/proj --task "修掉 xxx 的 NPE"
  python dispatch.py run --repo D:/proj --task-file t.md --backend dsh --isolate
  python dispatch.py list
  python dispatch.py show <runId>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(os.environ.get("WB_DISPATCH_HOME", Path(__file__).resolve().parent))
RUNS = HOME / "runs"
DEFAULT_PROXY = os.environ.get("WB_DISPATCH_PROXY", "http://127.0.0.1:7897")
DSH_CMD = os.environ.get("WB_DSH_CMD", "npx -y @deepseek-ai/dsh@0.2.1-rc.5")
DSH_RUNTIME_NODE = Path.home() / ".dsh/dsh-runtimes/dsh-primary-runtime/dependencies/node/bin/node.exe"


# ---------------------------------------------------------------- 基础设施

def now_tag() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def slugify(text: str, limit: int = 32) -> str:
    s = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fa5]+", "-", text.strip())[:limit].strip("-")
    return s or "task"


def which(cmd: str) -> str | None:
    if os.path.isabs(cmd) and Path(cmd).exists():
        return cmd
    return shutil.which(cmd)


def node_exe() -> str:
    """dsh 要求 Node >= 24.9，优先用 dsh 桌面版自带的运行时，避免另装。"""
    for cand in (os.environ.get("WB_NODE"), HOME / "tools" / "node" / "node.exe", DSH_RUNTIME_NODE):
        if cand and Path(cand).exists():
            return str(cand)
    return which("node") or "node"


def launcher(cmd: str, node_hint: str | None = None) -> list[str]:
    """把命令解析成可直接被 CreateProcess 执行的 argv 前缀。

    Windows 上 npm 生成的 .CMD 垫片无法直接执行，且经 cmd.exe 传递含引号/换行的
    长任务文本会被二次解析破坏。这里解析垫片内容，直接调用 node <entry.js>。
    node_hint 用于强制指定运行时（dsh 需要 Node >= 24.9）。
    """
    path = which(cmd)
    if not path:
        return [cmd]
    if Path(path).suffix.lower() not in (".cmd", ".bat"):
        return [path]

    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ["cmd.exe", "/c", path]

    js = re.findall(r'"([^"]*\.js)"', text)
    for raw in reversed(js):
        raw = raw.split("=")[-1]          # 去掉垫片里的 SET "VAR=..." 前缀
        if "%%" in raw.replace("%~dp0", "").replace("%dp0%", ""):
            continue                       # 跳过 %%F 这类循环变量拼接的路径
        entry = re.sub(r"%~?dp0%?[\\/]", "", raw)
        cand = Path(path).parent / entry
        if cand.exists():
            if node_hint:
                return [node_hint, str(cand)]
            local = Path(path).parent / "node.exe"
            return [str(local) if local.exists() else node_exe(), str(cand)]
    return ["cmd.exe", "/c", path]


def child_env(proxy: str | None) -> dict:
    env = dict(os.environ)
    # 剥离沙箱注入的代理，避免白名单拦截
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(k, None)
    if proxy:
        env.update({
            "HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "ALL_PROXY": proxy,
            "http_proxy": proxy, "https_proxy": proxy, "all_proxy": proxy,
        })
    env["NO_PROXY"] = "127.0.0.1,localhost,::1"
    env["no_proxy"] = env["NO_PROXY"]
    # 关键：禁止任何子进程交互式等待
    env["CI"] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def run_cmd(args: list[str], cwd: Path, env: dict, timeout: int,
            stdin_data: str | None = None) -> tuple[int, bytes, bytes, float]:
    t0 = time.time()
    try:
        proc = subprocess.run(
            args, cwd=str(cwd), env=env,
            input=(stdin_data.encode("utf-8") if stdin_data is not None else None),
            stdin=(None if stdin_data is not None else subprocess.DEVNULL),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr, time.time() - t0
    except subprocess.TimeoutExpired as exc:
        return 124, exc.stdout or b"", (exc.stderr or b"") + b"\n[wb-dispatch] timeout", time.time() - t0
    except FileNotFoundError as exc:
        return 127, b"", str(exc).encode(), time.time() - t0


def git(repo: Path, *args: str) -> tuple[int, str]:
    code, out, err, _ = run_cmd(["git", *args], repo, child_env(None), 120)
    return code, (out or err).decode("utf-8", "replace")


def deepseek_key() -> str | None:
    """dsh 的 headless 档不读托管凭据库，需从 ~/.dsh/.credentials.yaml 的 refs 取出注入环境。"""
    if os.environ.get("DEEPSEEK_API_KEY"):
        return os.environ["DEEPSEEK_API_KEY"]
    cred = Path.home() / ".dsh" / ".credentials.yaml"
    if not cred.exists():
        return None
    for line in cred.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if s.startswith("DEEPSEEK_API_KEY:"):
            return s.split(":", 1)[1].strip() or None
    return None


# ---------------------------------------------------------------- 数据模型

@dataclass
class Report:
    runId: str
    backend: str
    repo: str
    worktree: str
    branch: str | None
    isolated: bool
    task: str
    status: str = "running"          # completed | failed | timeout | error
    exitCode: int | None = None
    durationSec: float = 0.0
    lastMessage: str = ""
    filesChanged: list[str] = field(default_factory=list)
    diffstat: str = ""
    usage: dict = field(default_factory=dict)
    artifacts: dict = field(default_factory=dict)
    startedAt: str = ""
    error: str | None = None


# ---------------------------------------------------------------- 后端适配

def build_codex(task: str, worktree: Path, run_dir: Path, opts) -> tuple[list[str], str | None]:
    head = launcher("codex")
    if not which("codex"):
        raise SystemExit("codex 未找到。请确认 codex CLI 已安装并在 PATH 中。")
    args = [*head, "exec", "--json", "-C", str(worktree),
            "-s", opts.sandbox, "-o", str(run_dir / "last.md")]
    if opts.model:
        args += ["-m", opts.model]
    args.append(task)
    return args, None      # 任务走命令行参数，stdin 接 DEVNULL


def dsh_prefix() -> list[str]:
    """优先用本地安装（tools/dsh），否则回退 npx 拉取；两者都强制 Node >= 24 运行时。"""
    pkg = HOME / "tools" / "dsh" / "node_modules" / "@deepseek-ai" / "dsh" / "package.json"
    if pkg.exists():
        meta = json.loads(pkg.read_text(encoding="utf-8"))
        bins = meta.get("bin") or {}
        rel = bins.get("dsh") if isinstance(bins, dict) else None
        if rel:
            entry = pkg.parent / rel
            if entry.exists():
                return [node_exe(), str(entry)]
    return launcher("npx", node_hint=node_exe())


def build_dsh(task: str, worktree: Path, run_dir: Path, opts) -> tuple[list[str], str | None]:
    """0.2.1-rc.5 的 headless 档只支持 --json / --session-id / -，无权限类参数。

    权限与沙箱由 dsh 自身的 settings 决定（默认 workspace-write 语义），
    因此这里不再传 --permission-mode / --add-dir，否则会报未知选项。
    """
    base = DSH_CMD.split()
    prefix = dsh_prefix() if base[0] in ("npx", "npx.cmd") else launcher(base[0])
    args = [*prefix, "--profile", "headless", "--json"]
    if opts.session_id:
        args += ["--session-id", opts.session_id]
    args.append("-")       # 从 stdin 读任务，避免中文长文本被 shell 二次解析
    return args, task


BACKENDS = {"codex": build_codex, "dsh": build_dsh}


def parse_codex_events(raw: bytes) -> tuple[str, dict]:
    last, usage = "", {}
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "item.completed":
            item = ev.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                last = item["text"]
        elif ev.get("type") == "turn.completed" and isinstance(ev.get("usage"), dict):
            usage = ev["usage"]
    return last, usage


def parse_dsh_events(raw: bytes) -> tuple[str, dict]:
    last, usage = "", {}
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "text" and ev.get("text"):
            last = ev["text"]
        elif ev.get("type") == "final":
            if ev.get("text"):
                last = ev["text"]
            if isinstance(ev.get("usage"), dict):
                usage = ev["usage"]
    return last, usage


PARSERS = {"codex": parse_codex_events, "dsh": parse_dsh_events}


# ---------------------------------------------------------------- 核心动作

def prepare_worktree(repo: Path, opts, name: str) -> tuple[Path, str | None, int | None]:
    """返回 (工作目录, 分支名, 清理标记)。非隔离模式直接用仓库根。"""
    code, _ = git(repo, "rev-parse", "--is-inside-work-tree")
    if code != 0:
        raise SystemExit(f"不是 git 仓库：{repo}\n先用 git init 或指向真实项目目录。")

    if not opts.isolate:
        return repo, None, None

    branch = f"wb/{name}"
    wt = repo / ".worktrees" / name
    wt.parent.mkdir(parents=True, exist_ok=True)
    if wt.exists():
        raise SystemExit(f"worktree 已存在：{wt}\n换一个 --name，或手工清理。")
    code, out = git(repo, "worktree", "add", "-b", branch, str(wt), "HEAD")
    if code != 0:
        raise SystemExit(f"创建 worktree 失败：{out}")
    return wt, branch, 1


def resolve_backend(name: str) -> str:
    if name != "auto":
        return name
    return "codex" if which("codex") else "dsh"


def execute_one(repo: Path, task: str, name: str, backend: str, opts) -> tuple[Report, bytes]:
    """执行单个任务：建隔离工作区 → 派发 → 收集结果 → 落盘报告。"""
    run_id = f"{now_tag()}-{name}"
    run_dir = RUNS / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "task.txt").write_text(task, encoding="utf-8")

    rep = Report(
        runId=run_id, backend=backend, repo=str(repo), worktree="", branch=None,
        isolated=bool(getattr(opts, "isolate", False)), task=task,
        startedAt=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

    try:
        worktree, branch, cleanup = prepare_worktree(repo, opts, name)
        rep.worktree, rep.branch = str(worktree), branch
        cmd, stdin_data = BACKENDS[backend](task, worktree, run_dir, opts)
    except SystemExit as exc:
        rep.status, rep.error = "error", str(exc)
        write_report(run_dir, rep)
        return rep, b""

    env = child_env(getattr(opts, "proxy", None))
    if backend == "dsh" and not env.get("DEEPSEEK_API_KEY"):
        key = deepseek_key()
        if key:
            env["DEEPSEEK_API_KEY"] = key
    (run_dir / "cmd.txt").write_text(" ".join(cmd), encoding="utf-8")

    code, out, err, dur = run_cmd(cmd, worktree, env, getattr(opts, "timeout", 900), stdin_data)
    (run_dir / "events.jsonl").write_bytes(out)
    (run_dir / "stderr.log").write_bytes(err)

    last, usage = PARSERS[backend](out)
    if not last and (run_dir / "last.md").exists():
        last = (run_dir / "last.md").read_text(encoding="utf-8", errors="replace").strip()

    rep.exitCode, rep.durationSec, rep.usage = code, round(dur, 1), usage
    rep.lastMessage = last.strip()
    rep.status = "completed" if code == 0 else ("timeout" if code == 124 else "failed")

    _, status = git(worktree, "status", "--porcelain")
    rep.filesChanged = [
        ln[3:] for ln in status.splitlines()
        if ln.strip() and not ln[3:].startswith(".worktrees/")
    ]
    _, diffstat = git(worktree, "diff", "--stat", "HEAD")
    rep.diffstat = diffstat.strip()
    rep.artifacts = {
        "dir": str(run_dir),
        "events": str(run_dir / "events.jsonl"),
        "stderr": str(run_dir / "stderr.log"),
        "last": str(run_dir / "last.md") if (run_dir / "last.md").exists() else None,
    }
    write_report(run_dir, rep)
    return rep, err


def cmd_run(args) -> int:
    repo = Path(args.repo).resolve()
    task = args.task or (Path(args.task_file).read_text(encoding="utf-8") if args.task_file else None)
    if not task or not task.strip():
        raise SystemExit("缺少任务描述：用 --task 或 --task-file（或 stdin）提供。")
    task = task.strip()

    backend = resolve_backend(args.backend)
    name = args.name or slugify(task)

    print(f"[wb-dispatch] backend={backend} task={name}")
    print(f"[wb-dispatch] 已派发，超时上限 {args.timeout}s …", flush=True)

    rep, err = execute_one(repo, task, name, backend, args)
    print_human(rep, err)
    if args.json:
        print(json.dumps(asdict(rep), ensure_ascii=False, indent=2))
    return 0 if rep.status == "completed" else 1


def load_tasks(spec: Path) -> list[tuple[str, str]]:
    """任务清单：JSON 数组，或纯文本（一行一个任务，支持 `名称 :: 任务`）。"""
    raw = spec.read_text(encoding="utf-8")
    out: list[tuple[str, str]] = []
    if spec.suffix.lower() == ".json":
        for item in json.loads(raw):
            if isinstance(item, str):
                out.append(("", item))
            elif isinstance(item, dict) and item.get("task"):
                out.append((item.get("name") or "", item["task"]))
        return out
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "::" in s:
            nm, tk = s.split("::", 1)
            out.append((nm.strip(), tk.strip()))
        else:
            out.append(("", s))
    return out


def cmd_batch(args) -> int:
    repo = Path(args.repo).resolve()
    items = load_tasks(Path(args.file))
    if not items:
        raise SystemExit("任务清单为空。")

    backend = resolve_backend(args.backend)
    used: dict[str, int] = {}
    jobs: list[tuple[str, str]] = []
    for nm, task in items:
        base = slugify(nm or task)
        used[base] = used.get(base, 0) + 1
        jobs.append((base if used[base] == 1 else f"{base}-{used[base]}", task))

    print(f"[wb-dispatch] 批量派发 {len(jobs)} 个任务 · 并发 {args.concurrency} · 后端 {backend}")
    for i, (n, t) in enumerate(jobs, 1):
        print(f"  {i}. {n:<26} {t[:50]}")
    if args.dry_run:
        print(f"\n[dry-run] 未执行。预估输入 token 上限约 {len(jobs) * 250_000:,}（按单任务 25 万保守估计）。")
        return 0
    print(f"[wb-dispatch] 预估输入 token 上限约 {len(jobs) * 250_000:,}，共 {len(jobs)} 个隔离工作区\n")

    results: list[tuple[Report, bytes]] = []
    lock = threading.Lock()

    def work(name: str, task: str) -> None:
        rep, err = execute_one(repo, task, name, backend, args)
        with lock:
            results.append((rep, err))
            print(f"[完成] {name:<26} {rep.status:<10} {rep.durationSec:>7}s  "
                  f"{len(rep.filesChanged)} 文件", flush=True)

    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        list(pool.map(lambda p: work(*p), jobs))

    order = {n: i for i, (n, _) in enumerate(jobs)}
    results.sort(key=lambda r: order.get(r[0].runId.split("-", 2)[-1], 99))

    batch_id = f"{now_tag()}-batch"
    bdir = RUNS / batch_id
    bdir.mkdir(parents=True, exist_ok=True)
    summary = {
        "batchId": batch_id,
        "repo": str(repo),
        "backend": backend,
        "concurrency": args.concurrency,
        "total": len(results),
        "completed": sum(1 for r, _ in results if r.status == "completed"),
        "failed": sum(1 for r, _ in results if r.status != "completed"),
        "totalDurationSec": round(sum(r.durationSec for r, _ in results), 1),
        "totalInputTokens": sum(int(r.usage.get("input_tokens") or 0) for r, _ in results),
        "totalOutputTokens": sum(int(r.usage.get("output_tokens") or 0) for r, _ in results),
        "runs": [asdict(r) for r, _ in results],
    }
    (bdir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 74)
    print(f"{'任务':<28}{'状态':<11}{'退出':<6}{'文件':<5}{'耗时':>8}{'输入token':>12}")
    for r, _ in results:
        nm = r.runId.split("-", 2)[-1][:27]
        print(f"{nm:<28}{r.status:<11}{str(r.exitCode):<6}{len(r.filesChanged):<5}"
              f"{r.durationSec:>8}{int(r.usage.get('input_tokens') or 0):>12}")
    print("=" * 74)
    print(f"汇总: {summary['completed']} 成功 / {summary['failed']} 失败 · "
          f"累计 {summary['totalDurationSec']}s · 输入 token {summary['totalInputTokens']:,}")
    print(f"批次报告: {bdir / 'summary.json'}")
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["failed"] == 0 else 1


def prune_worktree_path(repo: Path, path: Path) -> Path:
    worktrees_root = repo / ".worktrees"
    resolved_root = worktrees_root.resolve()
    if resolved_root != worktrees_root:
        raise ValueError(f"路径越界：{worktrees_root} 指向 {resolved_root}。")

    worktree = path.resolve()
    try:
        relative_path = worktree.relative_to(worktrees_root)
    except ValueError:
        raise ValueError(f"路径越界：{path} 指向 {worktree}；仅允许清理 {worktrees_root} 下的路径。") from None
    if not relative_path.parts:
        raise ValueError(f"路径越界：清理目标须为 {worktrees_root} 的子目录。")
    return worktree


def remove_prune_worktree(repo: Path, worktree: Path) -> tuple[int, str]:
    for flags in ((), ("--force",)):
        target = prune_worktree_path(repo, worktree)
        code, output = git(repo, "worktree", "remove", *flags, str(target))
        if code == 0:
            return code, output
        if not flags:
            print(f"普通移除 {target} 失败：{output.strip()}\n"
                  "重试 git worktree remove --force。")
    return code, output


def print_prune_cleanup_hint(repo: Path, worktree: Path, branch: str) -> None:
    worktree = prune_worktree_path(repo, worktree)
    quote = shlex.quote
    if sys.platform == "win32":
        quote = lambda value: "'" + value.replace("'", "''") + "'"
    repo_arg = quote(str(repo))
    worktree_arg = quote(str(worktree))
    print(f"请手工清理残留目录：{worktree}（包括 {worktree / '.pytest-tmp'} 等只读残留）。")
    if sys.platform == "win32":
        print("PowerShell:")
        print(f"  Remove-Item -LiteralPath {worktree_arg} -Recurse -Force")
    else:
        print(f"  chmod -R u+w -- {worktree_arg}")
        print(f"  rm -rf -- {worktree_arg}")
    print("目录清理后，若工作区仍登记在 Git 中，执行：")
    print(f"  git -C {repo_arg} worktree remove --force {worktree_arg}")
    print("最后删除已合并分支：")
    print(f"  git -C {repo_arg} branch -d -- {quote(branch)}")


def cmd_prune(args) -> int:
    repo = Path(args.repo).resolve()
    worktrees_root = repo / ".worktrees"
    try:
        resolved_root = worktrees_root.resolve()
    except (OSError, RuntimeError) as exc:
        print(f"拒绝清理：检查清理范围 {worktrees_root} 失败：{exc}")
        return 1
    if resolved_root != worktrees_root:
        print(f"拒绝清理：路径越界，{worktrees_root} 指向 {resolved_root}。")
        return 1
    code, listing = git(repo, "worktree", "list", "--porcelain", "-z")
    if code != 0:
        print(f"读取 worktree 列表失败：{listing.strip()}")
        return 1

    if not args.force:
        print("[dry-run] 仅列出清理计划，使用 --force 执行清理。")

    result = 0
    for record in listing.split("\0\0"):
        fields = {}
        for entry in record.split("\0"):
            field_name, _separator, field_value = entry.partition(" ")
            fields[field_name] = field_value
        if not fields.get("worktree"):
            continue

        listed_worktree = Path(fields["worktree"])
        try:
            listed_worktree.relative_to(worktrees_root)
        except ValueError:
            continue
        try:
            worktree = prune_worktree_path(repo, listed_worktree)
        except (ValueError, OSError, RuntimeError) as exc:
            print(f"拒绝清理 {listed_worktree}：{exc}")
            result = 1
            continue

        branch_ref = fields.get("branch", "")
        if not branch_ref.startswith("refs/heads/wb/"):
            print(f"跳过 {worktree}：分支不是 wb/<name>。")
            continue
        branch = branch_ref[len("refs/heads/"):]

        status_code, status, status_error = run_cmd(
            ["git", "status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none"],
            worktree, child_env(None), 120,
        )[:3]
        if status_code != 0:
            print(f"跳过 {branch} ({worktree})：读取工作区状态失败："
                  f"{(status_error or status).decode('utf-8', 'replace').strip()}")
            result = 1
            continue
        if status.strip():
            print(f"跳过 {branch} ({worktree})：有未提交改动。")
            continue
        if "locked" in fields:
            print(f"跳过 {branch} ({worktree})：工作区已锁定。")
            continue

        code, output = git(repo, "merge-base", "--is-ancestor", branch_ref, "HEAD")
        if code == 1:
            print(f"跳过 {branch} ({worktree})：分支尚未合并到当前 HEAD。")
            continue
        if code != 0:
            print(f"跳过 {branch} ({worktree})：检查合并状态失败：{output.strip()}")
            result = 1
            continue

        if not args.force:
            print(f"[dry-run] 将清理 {worktree}  [分支 {branch}]")
            continue

        try:
            code, output = remove_prune_worktree(repo, worktree)
            if code != 0:
                print(f"--force 重试后清理 {branch} ({worktree}) 失败：{output.strip()}")
                print_prune_cleanup_hint(repo, worktree, branch)
                result = 1
                continue
        except (ValueError, OSError, RuntimeError) as exc:
            print(f"拒绝清理 {branch} ({worktree})：{exc}")
            result = 1
            continue
        code, output = git(repo, "branch", "-d", branch)
        if code != 0:
            print(f"工作区已移除，删除分支 {branch} 失败：{output.strip()}")
            result = 1
            continue
        print(f"已清理 {worktree}  [分支 {branch}]")
    return result


def write_report(run_dir: Path, rep: Report) -> None:
    (run_dir / "report.json").write_text(
        json.dumps(asdict(rep), ensure_ascii=False, indent=2), encoding="utf-8")


def print_human(rep: Report, err: bytes) -> None:
    print("\n" + "=" * 62)
    print(f"状态      : {rep.status}  (exit={rep.exitCode}, {rep.durationSec}s)")
    print(f"后端      : {rep.backend}")
    print(f"工作区    : {rep.worktree}" + (f"  [分支 {rep.branch}]" if rep.branch else ""))
    if rep.usage:
        keys = ("input_tokens", "output_tokens", "reasoning_output_tokens")
        print("用量      : " + ", ".join(f"{k}={rep.usage[k]}" for k in keys if k in rep.usage))
    print(f"改动文件  : {len(rep.filesChanged)}")
    for f in rep.filesChanged[:15]:
        print(f"            - {f}")
    if len(rep.filesChanged) > 15:
        print(f"            … 另有 {len(rep.filesChanged) - 15} 个")
    if rep.diffstat:
        print("变更统计  :")
        for line in rep.diffstat.splitlines()[-8:]:
            print(f"            {line}")
    print("-" * 62)
    print("最终答复  :")
    print(rep.lastMessage or "(空)")
    if rep.status != "completed":
        tail = err.decode("utf-8", "replace").strip().splitlines()[-15:]
        if tail:
            print("-" * 62)
            print("stderr 尾部:")
            for line in tail:
                print(f"  {line}")
    print("=" * 62)
    print(f"产物目录  : {rep.artifacts.get('dir')}")


def cmd_list(args) -> int:
    if not RUNS.exists():
        print("暂无运行记录。")
        return 0
    rows = []
    for d in sorted(RUNS.iterdir(), reverse=True):
        f = d / "report.json"
        if not f.exists():
            continue
        r = json.loads(f.read_text(encoding="utf-8"))
        rows.append((r["runId"], r["backend"], r["status"], r.get("exitCode"),
                     len(r.get("filesChanged") or []), r["task"][:40].replace("\n", " ")))
    if not rows:
        print("暂无运行记录。")
        return 0
    print(f"{'RUN ID':<34}{'后端':<8}{'状态':<11}{'退出':<6}{'文件':<5}任务")
    for rid, be, st, ec, nf, tk in rows[: args.limit]:
        print(f"{rid:<34}{be:<8}{st:<11}{str(ec):<6}{nf:<5}{tk}")
    return 0


def cmd_show(args) -> int:
    f = RUNS / args.run_id / "report.json"
    if not f.exists():
        print(f"未找到：{args.run_id}")
        return 1
    print(json.dumps(json.loads(f.read_text(encoding="utf-8")), ensure_ascii=False, indent=2))
    return 0


def cmd_doctor(args) -> int:
    print("wb-dispatch 环境自检")
    print("-" * 62)
    checks = []

    codex_path = which("codex")
    checks.append(("codex CLI", codex_path or "未安装", codex_path is not None))

    dsh_first = DSH_CMD.split()[0]
    dsh_path = which(dsh_first)
    checks.append(("dsh CLI", dsh_path or f"未安装（将用 {DSH_CMD} 走 npx）", True))

    gv_code, gv, _, _ = run_cmd(["git", "--version"], HOME, child_env(None), 30)
    checks.append(("git", gv.decode().strip() if gv_code == 0 else "缺失", gv_code == 0))

    py = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    checks.append(("python", py, sys.version_info >= (3, 9)))

    # 代理连通性（决定模型 API 能否出网）
    proxy = args.proxy or DEFAULT_PROXY
    ok_proxy = False
    if proxy:
        code, out, _, _ = run_cmd(
            ["curl", "-sS", "-m", "12", "-o", os.devnull, "-w", "%{http_code}",
             "https://agentrouter.org/v1/models"], HOME, child_env(proxy), 30)
        ok_proxy = code == 0 and out.decode().strip() not in ("", "000")
        checks.append((f"出口代理 {proxy} → AgentRouter", out.decode().strip() or "不可达", ok_proxy))
    else:
        checks.append(("出口代理", "未配置", False))

    for name, val, ok in checks:
        print(f"  [{'OK' if ok else '!!'}] {name:<38} {val}")

    print("-" * 62)
    print(f"运行目录：{RUNS}")
    if not ok_proxy:
        print("\n提示：WorkBuddy 沙箱只放行白名单域名，必须通过本机代理出网。")
        print("      确认 Clash 类代理端口（如 7897）在监听，并把 --proxy 指向它。")
    return 0 if all(ok for _, _, ok in checks) else 1


# ---------------------------------------------------------------- 入口

def main() -> int:
    p = argparse.ArgumentParser(prog="wb-dispatch", description="WorkBuddy → Codex / dsh 调度器")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="派发一个任务")
    r.add_argument("--repo", required=True, help="目标 git 仓库根目录")
    r.add_argument("--task", help="任务描述")
    r.add_argument("--task-file", help="从文件读取任务描述")
    r.add_argument("--backend", choices=["codex", "dsh", "auto"], default="codex")
    r.add_argument("--isolate", action="store_true", help="在独立 git worktree 中执行")
    r.add_argument("--name", help="工作区/分支名（默认由任务生成）")
    r.add_argument("--sandbox", default="workspace-write",
                   choices=["read-only", "workspace-write", "danger-full-access"],
                   help="codex 的沙箱策略")
    r.add_argument("--session-id", help="dsh: 沿用已有会话")
    r.add_argument("--model", help="codex: 覆盖模型")
    r.add_argument("--timeout", type=int, default=900)
    r.add_argument("--proxy", help=f"出口代理，默认 {DEFAULT_PROXY}")
    r.add_argument("--json", action="store_true", help="额外输出 JSON 报告")
    r.set_defaults(func=cmd_run)

    l = sub.add_parser("list", help="列出历史运行")
    l.add_argument("--limit", type=int, default=20)
    l.set_defaults(func=cmd_list)

    b = sub.add_parser("batch", help="并行派发多个任务（每个任务独立 worktree）")
    b.add_argument("--repo", required=True, help="目标 git 仓库根目录")
    b.add_argument("--file", required=True, help="任务清单：.json 数组，或纯文本一行一个（名称 :: 任务）")
    b.add_argument("--concurrency", type=int, default=2, help="并发数，默认 2")
    b.add_argument("--backend", choices=["codex", "dsh", "auto"], default="codex")
    b.add_argument("--sandbox", default="workspace-write",
                   choices=["read-only", "workspace-write", "danger-full-access"])
    b.add_argument("--model", help="codex: 覆盖模型")
    b.add_argument("--timeout", type=int, default=900)
    b.add_argument("--proxy", help=f"出口代理，默认 {DEFAULT_PROXY}")
    b.add_argument("--dry-run", action="store_true", help="只打印执行计划与成本预估")
    b.add_argument("--json", action="store_true", help="额外输出批次汇总 JSON")
    b.set_defaults(func=cmd_batch, isolate=True, session_id=None)

    prune = sub.add_parser("prune", help="清理已合并且无未提交改动的隔离工作区（默认 dry-run）")
    prune.add_argument("--repo", required=True, help="目标 git 仓库根目录")
    prune.add_argument("--force", action="store_true", help="执行工作区与分支清理；默认只列出计划")
    prune.set_defaults(func=cmd_prune)

    s = sub.add_parser("show", help="查看单次运行的完整报告")
    s.add_argument("run_id")
    s.set_defaults(func=cmd_show)

    d = sub.add_parser("doctor", help="环境自检")
    d.add_argument("--proxy", help=f"出口代理，默认 {DEFAULT_PROXY}")
    d.set_defaults(func=cmd_doctor)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
