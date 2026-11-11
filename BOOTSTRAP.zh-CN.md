# BOOTSTRAP：把一个空目录变成 R²I 项目

> 这一页写给 agent 看。用户在一个**空目录**里开了新的 Claude Code 会话，把 README 顶部那段 prompt 交给你。你照这一页从头做到尾，用户只需要回答问题、在对话里点头，以及做一件只有本人能做的事：往钥匙串里写钥匙。

## 守则（整个引导过程都适用）

- **不让用户在终端跑命令。** 所有命令由你用 Bash 工具跑。唯一例外是往 macOS 钥匙串写钥匙（第 5 步）：钥匙不能经过你，用户自己在终端里粘一条命令、输入时不回显。
- **问题一次问完、问最少的。** 第 3 步只问 4 件事；其余配置项用默认值，在第 3 步末尾列出来，用户想改再说。资源（钥匙、中转站、worker、外置盘）**不逐个 API 去问**：把 `docs/RESOURCES-SETUP.zh-CN.md` 的清单一次给用户，让用户照清单一次配好。
- **规则从零设立。** 这是一个全新项目，不沿用任何旧项目的规则内容；`rules/core.md` 是机制默认值，附录一（负责人管哪些事）是空的，第 6 步和用户一起填。
- **对外的事一律先问。** 建远程仓库、push、发邮件、上传数据，都要用户在对话里亲手打字确认。
- 下文 `$R2I` 指 `tools/r2i`（第 2 步克隆的位置），`r2i` 指 `$R2I/.venv/bin/r2i`。所有 `r2i` 命令在项目根目录跑。

## 第 1 步：检查依赖

逐项检查，一次报给用户结果（一张小表：项 / 要求 / 现状）：

| 项 | 要求 | 怎么查 |
|---|---|---|
| 系统 | macOS（launchd 和钥匙串只在 macOS 上有）；Linux 也能用台账和 vault，但第 4、5 步的定时任务和钥匙串要跳过 | `uname -s` |
| Python | 3.11 或更新 | `python3 --version`；没有就找 `python3.12`、`python3.13` |
| git | 有就行 | `git --version` |
| 当前目录 | 空的，或只有用户自己放的少量文件 | `ls -A` |
| Obsidian | 可选。vault 是普通 Markdown 文件夹，用 Obsidian 打开最好看 | 不用查，告诉用户就行 |

缺 Python 3.11+ 时停下，告诉用户装哪个版本（例如 `brew install python@3.12` 或官网安装包），装好再继续。

## 第 2 步：取代码、装好

```
git clone https://github.com/Leozz04/R2I.git tools/r2i
python3 -m venv tools/r2i/.venv
tools/r2i/.venv/bin/pip install -e tools/r2i
```

如果当前目录还不是 git 仓库，`git init`，并把 `tools/r2i/` 写进 `.gitignore`（它是独立的仓库）。

## 第 3 步：生成 r2i.toml（只问 4 件事）

用一次提问（能用选项卡就用选项卡）问：

1. **项目名**（英文小写、可带连字符，用于 launchd 标签，例如 `protein-folding`）
2. **负责人怎么称呼**（线程和页面上叫用户的名字，例如「PI」或用户的名字）
3. **时区**（IANA 名，例如 `Asia/Shanghai`、`Europe/Berlin`；可以先猜系统时区 `readlink /etc/localtime` 让用户确认）
4. **第一条主线**：一句话说要做成什么，你据此起 `id`（英文短词）、`name`、`short`（页面上用的短名）

然后复制 `$R2I/r2i.example.toml` 到项目根目录的 `r2i.toml`，填入这 4 项。其余用默认值，并一次列给用户看：

| 键 | 默认 | 意思 |
|---|---|---|
| quiet_hours | `00:00-09:00` | 静默时段：不推送、不在线程里等负责人 |
| vault / ledger / runs_root | `vault`、`ledger/ledger.sqlite`、`runs` | 都在项目目录里；有外置盘的话 runs_root 改到外置盘（见资源清单） |
| currency | `CNY` | 记账币种 |
| approve_over | `10` | 单笔超过这个数要负责人批 |
| night_budget | `50` | 夜间队列一晚最多花多少 |
| max_experiment_threads | `3` | 同时跑实验的线程上限 |

`[keys]`、`[relay]`、`[worker]` 先不填，留到第 5 步按资源清单一次填。

## 第 4 步：init、install、doctor

```
r2i init .            # 台账、vault、runs_root、.r2i/、.claude/settings.json 两个钩子、入口文件；可重复跑
r2i install           # 生成 launchd plist 到 .r2i/launchd/，只打印要跑的 launchctl 命令
r2i doctor            # 每项一行 ok / FAIL
```

`r2i install --load` 会把定时任务装进 `~/Library/LaunchAgents` 并启动（台账渲染每分钟一次、回复监听常驻、自检每小时、夜间巡检每 30 分钟）。这是装系统任务，先在对话里问用户「要不要现在装定时任务」，用户说装再跑 `r2i install --load`。

`r2i doctor` 里 `[keys]` 相关的 FAIL 在第 5 步前是正常的；其他 FAIL 当场修好再往下走。

## 第 5 步：资源一次配好

把 `docs/RESOURCES-SETUP.zh-CN.md` 的「安装清单」整张发给用户，请用户勾出要用的功能（中转站、代码 worker、手机推送、外置盘、批量 API），一次回答。然后：

1. 按用户勾的功能，把 `[keys]` 的条目名、`[relay]`、`[worker]`、`runs_root` 一次写进 `r2i.toml`（条目名你来起，建议 `<项目名>-<用途>`）。
2. 把每个钥匙条目对应的写入命令（清单里的模板，条目名已替换好）**一次**发给用户，请用户在终端里逐条粘贴、按提示输入钥匙（不回显）。这是唯一要用户在终端做的事。
3. 用户说好了以后跑 `r2i doctor`，全部 ok 才往下走。你**不读取、不打印**任何钥匙的值；doctor 只查条目在不在。
4. 用到付费资源的，用 `r2i res-set <服务名> --purpose … --credential-ref <钥匙条目名> --check-method …` 登记到资源台。
5. 按实际情况填 `vault/agent/管理/可读写路径.md`（vault、runs_root、代码仓库、台账只读），给负责人过目（R-22）。

用户暂时不想配的功能就不配：对应功能自动关闭，以后再按清单补。

## 第 6 步：和负责人一起把规则立起来

照 `docs/RULES-SETUP.zh-CN.md` 做，顺序是：

1. 负责人决策权清单（附录一）：把模板发给用户，用户勾选、改写，你写进 `rules/core.md` 的项目副本（见下）。
2. 复制 `$R2I/rules/project.md.example` 为项目的 `rules/project.md`，填部门表、主线和常驻线程表、分工。
3. 拼出 vault 的 `项目规则.md`：第一部分放项目副本 `rules/core.md`（`{{…}}` 占位符换成配置里的值），第二部分放 `rules/project.md`。
4. 按指南第 2 节检查 `vault/agent/必带清单.md`，按本项目改；然后 `r2i entries --vault vault` 重新生成项目根目录和 vault 的入口文件（CLAUDE.md、AGENTS.md、GEMINI.md）。已有的 CLAUDE.md/AGENTS.md/GEMINI.md 原样保留在生成段下面，并备份一次为 `*.pre-r2i`。
5. 主线目标和验收线：`r2i goal-set --mainline <id> --goal '…' --criterion '…' --criterion '…' --approved-by '<负责人>' --editor 调度者`，目标和每条验收线都要负责人在对话里认可。

项目的规则副本放在项目根目录 `rules/`（`core.md` 从 `$R2I/rules/core.md` 复制）；`$R2I` 里的那份是上游模板，不在上面改。

## 第 7 步：开第一条线程

这个会话就是**调度者**。

1. 登记自己：`r2i thread new --title '调度者' --kind 调度`，记下打印的 `cc_…`，再 `r2i thread set --id cc_… --agent 调度者`。
2. 为第一条主线的推进线程写 agent 类型定义：`.claude/agents/<部门>-<线名>.md`，格式见 `vault/agent/模板/线程 agent 定义示例.md`（正文只写身份和开工入口，规则不抄进去）。
3. `r2i thread new --title '<部门>｜推进<主线短名>' --kind 推进 --mainline <id>`，拿到 `id` 和 `first_message`。
4. 用 Agent 工具后台开它：`subagent_type` 写第 2 步的类型名，`run_in_background: true`，prompt 用 `first_message`。如果 Agent 工具不认识这个类型（新文件要重开会话才加载），请用户关掉这个会话再开一次，你读 `BOOTSTRAP.zh-CN.md` 第 7 步接着做。
5. `r2i thread set --id <cc_…> --agent <类型名>`。
6. 告诉用户：去 agent 面板点进这条线程就能直接和它说话；以后要问用户的事会出现在 vault `汇报/要批/`，Obsidian 首页的「要你定」里能看到。

## 第 8 步：收尾检查

- `r2i doctor` 全 ok（没配的可选功能除外，说明哪些没配）。
- 项目根目录 `.gitignore` 至少排除 `tools/r2i/`、`.r2i/`、`runs/`（runs_root 在项目里时）、`ledger/`。
- 给用户一段 5 行以内的总结：项目在哪、vault 在哪（用 Obsidian 打开这个文件夹）、第一条线程叫什么、还有哪些可选功能没配、规则在 `vault/项目规则.md`。
