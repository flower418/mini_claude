# mini_claude

Anthropic Messages API 驱动的交互式编程 agent，支持工具调用、记忆系统、技能注册、上下文压缩和错误重试。

## 功能

- **交互式 CLI** — 输入自然语言指令，agent 自动调用工具完成任务
- **工具系统** — bash、文件读写/编辑、glob 搜索、todo 管理、子 agent 任务分派
- **记忆系统** — 自动记录用户偏好、反馈、项目知识，支持搜索和压缩去重
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
├── agent.py          # 主循环、错误重试、工具分发
├── tools.py          # 工具定义与实现
├── memory.py         # 记忆存储、搜索、压缩、去重
├── skills.py         # 技能注册表、system prompt 组装与缓存
├── compact.py        # 上下文压缩流水线
├── config.py         # 环境变量、路径、安全常量
├── hooks.py          # Hook 事件系统
├── skills/           # 技能目录（SKILL.md 每技能一个）
├── .memory/          # 记忆存储（每条目一个子目录）
├── .transcripts/     # 对话转录归档
└── .task_outputs/    # 大工具输出持久化
```

## 错误重试策略

| 类别 | 触发 | 策略 | 上限 |
|------|------|------|------|
| 输出截断 | `stop_reason = max_tokens` | max_tokens ×4 | 3 次 |
| Prompt 过长 | API 返回 prompt 过长错误 | emergency compact | 1 次 |
| 529 过载 | HTTP 529 | 切换到 MODEL_FALLBACK_ID | 1 次切换 |
| 服务端抖动 | 429 / 5xx / 网络错误 | 指数退避 0.5s→1s→2s→... | 10 次 |
