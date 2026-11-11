# 资源设置指南：安装清单

> 安装时照这张清单一次配好。引导会话（BOOTSTRAP 第 5 步）把「安装清单」整张发给用户，用户勾出要用的功能、一次回答；agent 不逐个 API 去问。没勾的功能自动关闭，以后随时按这页补。

## 安装清单（勾要用的）

| 勾 | 功能 | 要什么 | 写进哪里 | 不配会怎样 |
|---|---|---|---|---|
| 必配 | Claude Code 钩子 | 无，`r2i init` 自动写 | 项目 `.claude/settings.json` | — |
| 必配 | 线程 agent 类型 | 每条常驻线程一个定义文件 | 项目 `.claude/agents/` | 面板里认不出线程 |
| 建议 | 权限规则 | 一段 autoMode 规则 | 项目 `.claude/settings.json` | 每条命令都弹确认 |
| 建议 | 定时任务 | 用户同意装 launchd | `r2i install --load` | 页面不自动刷新、回复没人收 |
| [ ] | 外置盘放数据 | 一个挂载在 `/Volumes/<盘名>` 的盘 | `runs_root` | 数据放项目目录里 |
| [ ] | 中转站（Claude 兼容端点） | 端点地址 + 1–2 把钥匙 | `[relay]`、`[keys] relay / relay_think` | 付费调用只走官方端点 |
| [ ] | 代码 worker（`r2i worker`） | 模型名、端点、常设预算号 | `[worker]` | 线程自己写代码或用 Claude Code 子 agent |
| [ ] | 官方 API（批量 / 直连） | 一把 API 钥匙 | `[keys] batch_api` | paid_run 不替子进程填官方钥匙 |
| [ ] | DeepSeek（Anthropic 兼容端点） | 一把钥匙 | `[keys] deepseek` | 不能用 DeepSeek，巡检不查它的余额 |
| [ ] | 手机推送（ntfy） | 一个私有 topic 名 | `[keys] ntfy_topic` | 要批页只在 vault 里出现，不推送 |
| [ ] | 推送里的「叫醒」按钮（实验性） | 第二个 topic + 唤醒桥（`r2i install` 不装它，要另配，没在验收里测过） | `[keys] ntfy_wake_topic` | 推送没有叫醒按钮 |

## 1. Claude Code 设置

### 钩子（`r2i init` 写好，不用手配）

`r2i init` 在项目 `.claude/settings.json` 里合并两条钩子，别的键原样保留：

- `UserPromptSubmit` → `r2i hook user-prompt`：负责人在线程里打的每条消息记进台账 `owner_msgs`，得到一个消息号。要负责人批的事（`thread-answer --owner-msg`、预算批准）只认这里有记录的消息号。调度者转发的消息设 `R2I_DISPATCH=1`，记成「不是负责人」。
- `PreCompact` → `r2i hook pre-compact`：自动压缩前提醒线程写交接、跑 `r2i handover-check`。

`r2i doctor` 会查这两条在不在。

### 线程 agent 类型

每条常驻线程一个类型文件，放项目 `.claude/agents/<部门>-<线名>.md`。agent 面板只显示类型名，负责人靠它认线程。格式见 vault `agent/模板/线程 agent 定义示例.md`：

```markdown
---
name: 甲组-主线推进
description: <项目> 主线推进（常驻推进线程，由调度者用 Agent 工具后台开；第一条指令里有台账线程 id 和交接页）
---

你是 <项目> 项目的常驻推进线程「甲组-主线推进」。开工先读 vault agent/必带清单.md，按现行规则开工；具体任务、台账线程 id（cc_…）和交接页在第一条消息里。
```

正文只写身份和入口，规则不抄进去。需要固定模型时在 frontmatter 加 `model:`，写完整模型 id。新加的类型文件要重开 Claude Code 会话才被加载。

### 权限规则（autoMode 示例）

把常规操作写成允许规则，免得每条命令弹确认；对外、删除、钥匙仍然要确认。放项目 `.claude/settings.json`（示例，按项目改路径）：

```json
{
  "permissions": {
    "allow": [
      "Bash(tools/r2i/.venv/bin/r2i:*)",
      "Bash(sqlite3 -readonly:*)"
    ],
    "deny": [
      "Read(./.env*)"
    ]
  },
  "autoMode": {
    "allow": [
      "$defaults",
      "r2i ledger: running `tools/r2i/.venv/bin/r2i` subcommands in this project (ask, reply-wait, thread, exp-upsert, note-add, budget-add, paid-run, handover-check, render) is the project's own bookkeeping, not credential access. `r2i paid-run` may read Keychain items named in r2i.toml [keys] and pass them only to the child process.",
      "Experiment runs: writing under <runs_root>/runs/<code>/<YYYYMMDD-NN>/ and running the project's own experiment scripts there is routine work."
    ]
  }
}
```

不放进允许规则的：`git push`、`gh repo create`、删除命令、`security` 读钥匙、发邮件。这些按规则只认负责人亲手打的字（R-10a）。

## 2. 钥匙串条目（`[keys]`）

`[keys]` 里写的是 **macOS 钥匙串条目名**，不是钥匙本身。钥匙只放钥匙串，配置文件、规则、记忆里只出现条目名（R-70）。程序在用的时候读一次，只交给子进程，不打印。

| `[keys]` 键 | 谁用 | 填给子进程的环境变量 | 什么时候用 |
|---|---|---|---|
| `batch_api` | `r2i paid-run` | `ANTHROPIC_API_KEY` | 子进程走官方端点 |
| `relay` | `r2i paid-run`、`r2i worker` | `ANTHROPIC_AUTH_TOKEN` | `ANTHROPIC_BASE_URL` 的主机等于 `[relay] base_url` 时；官方钥匙同时从环境里去掉 |
| `relay_think` | 同上 | `ANTHROPIC_AUTH_TOKEN` | 同上，且 `R2I_RELAY_KEY=think`（中转站给推理模型单独一把钥匙时） |
| `deepseek` | `r2i paid-run`、夜间巡检 | `ANTHROPIC_AUTH_TOKEN` | `ANTHROPIC_BASE_URL` 含 `deepseek.com`；巡检查余额 |
| `ntfy_topic` | 要批页推送 | — | 有新的要批页时推到手机 |
| `ntfy_wake_topic` | 唤醒桥 | — | 推送上的「叫醒」按钮发到这个 topic |
| `wake_token` | reply-watch | — | 收到回复时叫醒线程；不设就不叫醒（`r2i doctor` 会提示） |

**命名规则。** 条目名必须以 `keychain_prefix`（`r2i.toml` 顶层键，默认 `<项目名>-`）开头，否则配置报错拒绝，r2i 因此不会被指向无关的钥匙串条目。

**剩余风险。** `r2i paid-run` 会把钥匙放进子命令的环境变量，子命令读得到。只在 paid-run 下运行你自己的命令。

条目名建议 `<项目名>-<用途>`，例如：

```toml
[keys]
relay = "myproj-relay"
batch_api = "myproj-anthropic"
ntfy_topic = "myproj-ntfy"
```

### 写入命令（用户本人在终端里粘贴）

这是整个安装过程唯一要用户自己在终端做的事。agent 把条目名替换好、一次发出；用户逐条粘贴，按提示输入钥匙，**输入不回显**，钥匙不经过 agent：

```sh
security add-generic-password -U -a "$USER" -s <条目名> -w
```

`-w` 放在最后、不跟值，`security` 会提示输入两遍；`-U` 表示条目已存在时覆盖。写完后 agent 跑 `r2i doctor`，它只查条目在不在，不读值。

## 3. 中转站和代码 worker

```toml
[relay]
base_url = "https://relay.example.com"   # Claude 兼容端点；paid_run 给它 [keys] relay，绝不给官方钥匙

[worker]
base_url = "https://relay.example.com"   # 空 = 官方端点
model = "claude-sonnet-5-5"              # 写完整模型 id，不用别名
effort = "high"
budget = "CODE-WORKER-20260101"          # 常设 paid_run 预算号（先用 r2i budget-add 建）
cap = 10.0                               # 每单上限；CLI 在 headroom × cap 停
headroom = 0.75
usd_rate = 7.3                           # 记账币种 / 美元，用于估算
out_dir = ""                             # 日志；空 = <runs_root>/code-worker
```

- 中转站的 thinking / effort 参数先实测再依赖：用一次最小调用确认模型名和参数生效。
- 常设预算：`r2i budget-add` 建一条（金额在 `approve_over` 以内线程自己定，超过的找负责人）；每单 `r2i worker --task-file F --cwd DIR`，超上限的花费照记并标 `over_cap`。
- `r2i worker --dry-run` 只打印将要跑的命令，不花钱，装好后先跑一次。

## 4. 数据放哪（runs_root）

- 默认 `runs`（项目目录里）。数据大的项目把 `runs_root` 指到外置盘，例如 `/Volumes/<盘名>/<项目名>`。
- 外置盘没挂载时，巡检会报警；不要把输出悄悄写回内置盘。
- 每次实验一个目录 `runs_root/runs/<代号>/<YYYYMMDD-NN>/`，里面放命令、配置、种子、环境和结果（R-31）。

## 5. 定时任务（launchd，macOS）

`r2i install` 只生成 plist 到 `.r2i/launchd/` 并打印命令；用户在对话里同意后，agent 跑 `r2i install --load`（复制到 `~/Library/LaunchAgents` 并 `launchctl bootstrap`）。

| 任务 | 频率 | 做什么 |
|---|---|---|
| r2i-sync | 每 60 秒 | 渲染台账到 vault：首页、要批页、进度页 |
| report-reply | 常驻 | 收负责人在要批页上写的回答，交给等着的线程 |
| selfcheck | 每小时 | 自检：预算对不上、队列卡住、reply-wait 没挂等 |
| night-watchdog | 每 30 分钟 | 夜间巡检：磁盘、外置盘、余额 |

标签是 `com.r2i.<项目名>.<任务>`；卸载用 `launchctl bootout gui/$(id -u)/<标签>`。

## 6. 配完检查

`r2i doctor` 每项一行：config、台账版本、vault、两个钩子、各定时任务的 plist、`[keys]` 每个条目在不在、runs_root。全部 ok 就配好了；没勾的可选功能不会出现在检查里。
