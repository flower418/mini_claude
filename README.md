# mini_claude

Anthropic Messages API 驱动的交互式编程 agent，支持工具调用、记忆系统、技能注册、上下文压缩和错误重试。

## 功能

- **交互式 CLI** — 输入自然语言指令，agent 自动调用工具完成任务
- **工具系统** — bash、文件读写/编辑、glob 搜索、todo 管理、子 agent 任务分派
- **记忆系统** — 自动记录用户偏好、反馈、项目知识，支持搜索和压缩去重
- **任务与调度** — 可持久化任务图、CronJob 调度、后台任务和子 agent 协作
- **隔离工作区** — 每个自动领取任务使用独立 worktree，基于 manifest 合并并检测冲突
- **技能系统** — 可插拔的 SKILL.md 注册表，按需加载
- **上下文压缩** — 多级流水线（snip → micro → persist → LLM summarize）
- **错误重试** — 三种策略：输出截断扩容、prompt 过长压缩、服务端抖动指数退避
- **529 模型回退** — 服务过载时自动切换到轻量 fallback 模型

## 安装

```sh
pip install -r requirements.txt
```

## 配置

在项目根目录创建 `.env`：

```env
ANTHROPIC_API_KEY=your_api_key
MODEL_ID=deepseek-v4-pro
MODEL_FALLBACK_ID=deepseek-v4-flash   # 可选，529 时自动回退
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
```

## 使用

```sh
python agent.py
```

输入问题按回车发送，`q` 退出。

## 项目结构

```
mini_claude/
├── agent.py              # CLI 入口、主循环、API 重试、调度/消息收集
├── tools.py              # Anthropic tool schema 与工具 handler 边界
├── config.py             # 环境变量、路径、安全常量、工作目录沙箱
├── state_store.py        # ID policy、原子写、JSON/JSONL、JsonDirStore
├── task_system.py        # 持久化任务图与依赖检查
├── scheduler.py          # CronJob 调度器与 durable/memory job 存储
├── agent_team.py         # 子 agent 生命周期、邮箱协议、自动领取任务
├── worktree.py           # 每任务隔离工作区、manifest 合并、冲突检测
├── memory.py             # 记忆存储、搜索、索引重建、压缩去重
├── compact.py            # 上下文压缩流水线与转录归档
├── hooks.py              # Hook 事件系统与安全拦截
├── mcp_client.py         # MCP stdio client 与工具桥接
├── skills.py             # 技能注册表、system prompt 组装与缓存
└── tests/                # 运行时边界与回归测试
```

## 架构

这个项目现在按四层组织：

1. **编排入口**：`agent.py` 负责交互式循环、消息历史、模型调用、重试策略、调度任务和子 agent 邮件的注入。
2. **工具边界**：`tools.py` 只暴露模型可调用的工具 schema，并把调用路由到各运行时模块；工具参数清洗和异常兜底集中在 `safe_dispatch`。
3. **运行时能力**：`task_system.py`、`scheduler.py`、`agent_team.py`、`worktree.py` 分别处理任务图、定时触发、多 agent 协议和隔离工作区，避免把领域逻辑塞进主循环。
4. **状态基础设施**：`state_store.py` 集中处理文件系统状态常见问题，包括 ID 规范化、路径穿越防护、原子写入、JSON/JSONL 容错读取和目录型 JSON store。

运行时状态默认写入被 `.gitignore` 忽略的目录：`.memory/`、`.task/`、`.schedule/`、`.agents/`、`.worktrees/`、`.transcripts/`、`.task_outputs/`。这些目录是本地会话状态，不应提交。

## 错误重试策略

| 类别 | 触发 | 策略 | 上限 |
|------|------|------|------|
| 输出截断 | `stop_reason = max_tokens` | max_tokens ×4 | 3 次 |
| Prompt 过长 | API 返回 prompt 过长错误 | emergency compact | 1 次 |
| 529 过载 | HTTP 529 | 切换到 MODEL_FALLBACK_ID | 1 次切换 |
| 服务端抖动 | 429 / 5xx / 网络错误 | 指数退避 0.5s→1s→2s→... | 10 次 |
