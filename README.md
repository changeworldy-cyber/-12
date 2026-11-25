# wb-dispatch

由 WorkBuddy 指挥外部 CLI 编码代理（Codex / DeepSeek Harness）执行编码任务的调度层。

一个任务 = 一个隔离 git worktree = 一份结构化报告。

## 它解决什么

直接让编码代理在终端里跑，会撞上四类问题。这一层把它们逐个处理掉：

| 问题 | 现象 | 处理方式 |
|---|---|---|
| stdin 挂起 | 代理在 stdin 非 TTY 时等待管道 EOF，永久阻塞 | 子进程 stdin 恒接 `DEVNULL` |
| 出口受限 | 沙箱代理只放行白名单，模型 API 直接被拦 | 剥离继承的代理变量，显式指定出口代理 |
| Windows 垫片 | npm 生成的 `.cmd` 无法被 `CreateProcess` 执行；经 `cmd /c` 传长文本会被二次解析破坏 | 解析垫片中的 `.js` 入口，直接 `node <entry.js>` |
| 互相污染 | 多任务并行时共用工作区，diff 混在一起 | 每任务独立 worktree + 独立分支 |

## 设计原则

1. **控制权留在确定性代码里。** 派几个任务、何时停、失败怎么办，都是写死的，不交给模型临时决定。
2. **执行后端可替换。** codex 与 dsh 的接口语义一致（任务进 → 最终答案出 → 退出码），因此抽象成同一层。
3. **审查发生在提交之前。** 代理的改动以未提交状态留在隔离工作区，由人审完再决定是否合入。
4. **不自动推送。** 调度器不碰远端，合入与推送始终是明确的外显动作。

## 用法

```bash
python dispatch.py doctor                      # 环境自检（含出口代理连通性）

python dispatch.py run --repo D:/proj --task "修掉登录接口的空指针" --isolate
python dispatch.py run --repo D:/proj --task-file task.md --backend dsh --name fix-login

python dispatch.py batch --repo D:/proj --file tasks.txt --concurrency 2
python dispatch.py batch --repo D:/proj --file tasks.txt --dry-run   # 只看计划与成本预估

python dispatch.py list                        # 历史运行
python dispatch.py show <runId>                # 单次运行的完整报告
```

### 参数

| 参数 | 说明 |
|---|---|
| `--backend codex\|dsh\|auto` | 后端选择，`auto` 优先 codex |
| `--isolate` | 在 `<repo>/.worktrees/<name>` + 分支 `wb/<name>` 中执行 |
| `--sandbox` | codex 沙箱策略：`read-only` / `workspace-write` / `danger-full-access` |
| `--model` | 覆盖 codex 模型 |
| `--timeout` | 秒，默认 900 |
| `--proxy` | 出口代理，默认 `http://127.0.0.1:7897` |
| `--json` | 追加输出机器可读报告 |

### 任务清单格式

纯文本，一行一个，`名称 :: 任务`：

```
# 名称 :: 任务
add-square :: 在 calc.py 中新增 square(x) 并补充测试断言
write-readme :: 为该仓库写一份 README.md
```

也支持 JSON：`["任务一", "任务二"]` 或 `[{"name": "fix-a", "task": "..."}]`。

批量模式**强制隔离**——多个代理同时改同一工作区必然互相覆盖。

## 产物结构

```
runs/<时间戳>-<任务名>/
├── task.txt        派发的原始任务
├── cmd.txt         实际执行的命令（便于复现）
├── events.jsonl    后端事件流
├── stderr.log      诊断输出
├── last.md         codex 的最终答复
└── report.json     状态/退出码/耗时/改动文件/diffstat/token 用量
```

批次汇总写 `runs/<批次id>/summary.json`，含每个任务的报告与总 token。

## 审查与合入

调度器不自动提交。改动以未提交状态留在隔离工作区：

```bash
git -C .worktrees/<name> status --porcelain   # 改动清单
git -C .worktrees/<name> diff                 # 逐行审查

git -C .worktrees/<name> add -A
git -C .worktrees/<name> commit -m "feat: <描述>"
git merge wb/<name>
git worktree remove .worktrees/<name>
```

注意：`git diff HEAD wb/<name>` 看不到内容——分支指针未移动，改动是工作区状态而非提交。这是刻意设计。

## 踩坑记录

这些是实测得出的，不是推测：

- **退出码 0 不等于任务正确。** 曾有一个任务报告 `completed`、代理自称"运行验证"过，实际产物跑起来直接 `AssertionError`。派发任务的描述必须包含可执行的完成定义，例如"`python calc.py` 必须输出 `ok`"。
- **成本远高于预期。** 在推理强度 `ultra` 下，一个一行的 bug 修复可消耗 22–31 万输入 token。批量派发前应先用 `--dry-run` 看预估。
- **上游中转可能在响应里注入推广文本。** 检查最终答复时不要把它当成模型结论。
- **批量并发的收益是非线性的。** 实测 2 任务并发：累计 398.8 秒，墙钟 206 秒，约 1.93 倍。并发数受上游限流约束，默认 2。

## 平台

Windows (Git Bash) 上开发与验证，Python ≥ 3.9。核心逻辑不依赖平台，但 `launcher()` 里的 `.cmd` 垫片解析是 Windows 专用分支。

## 测试

```bash
python -m pytest -q
```

## 许可

MIT
