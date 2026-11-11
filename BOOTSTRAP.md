# BOOTSTRAP: turn an empty directory into an R²I project

> This page is for the agent. The user opened a new Claude Code session in an **empty directory** and handed you the prompt from the top of the README. Follow this page from start to end. The user only answers questions, agrees in the conversation, and does the one thing only they can do: write keys into the macOS Keychain.

## Ground rules (for the whole bootstrap)

- **Do not make the user run terminal commands.** You run every command with the Bash tool. The only exception is writing keys into the Keychain (step 5): keys must not pass through you, so the user pastes one command per key into a terminal and types the key without echo.
- **Ask everything at once, and ask as little as possible.** Step 3 asks only 4 things; every other setting uses its default, listed at the end of step 3 so the user can change it. Resources (keys, relay, worker, external disk) are **not asked one API at a time**: send the checklist from `docs/RESOURCES-SETUP.md` once and let the user fill it in once.
- **Rules start from zero.** This is a new project and carries over no rule content from any earlier project. `rules/core.md` holds mechanism defaults; its Appendix 1 (what the owner decides) is empty and is filled with the user in step 6.
- **Ask before anything outward-facing.** Creating a remote repository, pushing, sending email or uploading data all need the user to type a confirmation in the conversation.
- Below, `$R2I` means `tools/r2i` (cloned in step 2) and `r2i` means `$R2I/.venv/bin/r2i`. Run every `r2i` command from the project root.

## Step 1: check dependencies

Check each item and report once (a small table: item / requirement / found):

| Item | Requirement | How to check |
|---|---|---|
| OS | macOS (launchd and the Keychain exist only there). On Linux the ledger and vault work, but skip the scheduled jobs and Keychain in steps 4 and 5 | `uname -s` |
| Python | 3.11 or newer | `python3 --version`; otherwise look for `python3.12`, `python3.13` |
| git | present | `git --version` |
| Current directory | empty, or only a few files the user put there | `ls -A` |
| Obsidian | optional. The vault is a plain Markdown folder; it reads best in Obsidian | no check, just tell the user |

If Python 3.11+ is missing, stop and tell the user which version to install (for example `brew install python@3.12` or the python.org installer); continue once it is installed.

## Step 2: get the code and install

```
git clone https://github.com/Leozz04/R2I.git tools/r2i
python3 -m venv tools/r2i/.venv
tools/r2i/.venv/bin/pip install -e tools/r2i
```

If the current directory is not yet a git repository, run `git init` and add `tools/r2i/` to `.gitignore` (it is a separate repository).

## Step 3: write r2i.toml (ask only 4 things)

Ask in one go (use option cards where possible):

1. **Project name** (lowercase English, hyphens allowed; used in launchd labels, for example `protein-folding`)
2. **What to call the owner** (the name threads and pages use, for example "PI" or their name)
3. **Time zone** (IANA name, for example `Asia/Shanghai`, `Europe/Berlin`; you can guess the system zone with `readlink /etc/localtime` and ask the user to confirm)
4. **The first mainline**: one sentence on what it should achieve; from it you choose `id` (a short English word), `name` and `short` (the short name shown on pages)

Then copy `$R2I/r2i.example.toml` to `r2i.toml` in the project root and fill in these 4. Everything else keeps its default; list them for the user in one table:

| Key | Default | Meaning |
|---|---|---|
| quiet_hours | `00:00-09:00` | quiet window: no push notifications, threads do not wait on the owner |
| vault / ledger / runs_root | `vault`, `ledger/ledger.sqlite`, `runs` | all inside the project directory; with an external disk, point runs_root there (see the resource checklist) |
| currency | `CNY` | bookkeeping currency |
| approve_over | `10` | a single spend above this needs the owner's approval |
| night_budget | `50` | how much the night queue may spend per night |
| max_experiment_threads | `3` | how many threads may run experiments at once |

Leave `[keys]`, `[relay]` and `[worker]` empty; they are filled in step 5 from the checklist.

## Step 4: init, install, doctor

```
r2i init .            # ledger, vault, runs_root, .r2i/, two hooks in .claude/settings.json, entry files; safe to repeat
r2i install           # writes launchd plists to .r2i/launchd/ and only prints the launchctl commands
r2i doctor            # one ok / FAIL line per check
```

`r2i install --load` copies the jobs into `~/Library/LaunchAgents` and starts them (ledger rendering every minute, a resident reply listener, a self-check every hour, a night watchdog every 30 minutes). Installing system jobs is the owner's call: ask "install the scheduled jobs now?" in the conversation and run `r2i install --load` only if they say yes.

FAIL lines about `[keys]` are expected before step 5; fix any other FAIL before moving on.

## Step 5: configure resources in one pass

Send the user the whole "Install checklist" from `docs/RESOURCES-SETUP.md` and ask them to tick the features they want (relay, code worker, phone push, external disk, batch API) in one answer. Then:

1. For the ticked features, write the `[keys]` item names, `[relay]`, `[worker]` and `runs_root` into `r2i.toml` in one edit (you choose the item names; suggested `<project>-<purpose>`).
2. Send the user the write command for every key item (the template from the checklist, with the item names filled in) **once**, and ask them to paste each into a terminal and type the key at the prompt (no echo). This is the only terminal work they do.
3. When they say done, run `r2i doctor`; continue only when everything is ok. You **never read or print** a key value; doctor only checks that the item exists.
4. For paid resources, register them in the resource table with `r2i res-set <service> --purpose … --credential-ref <Keychain item> --check-method …`.
5. Fill `vault/agent/管理/可读写路径.md` (the allowed paths page: vault, runs_root, code repository, ledger read-only) to match reality and show it to the owner (R-22).

Features the user does not want now stay unconfigured: they switch off by themselves and can be added later from the checklist.

## Step 6: set up the rules with the owner

Follow `docs/RULES-SETUP.md`, in this order:

1. The owner decision list (Appendix 1): send the template, let the owner tick and rewrite it, and write it into the project copy of `rules/core.md` (see below).
2. Copy `$R2I/rules/project.md.example` to the project's `rules/project.md` and fill in the department table, the mainline and resident-thread table, and the duties.
3. Assemble the vault page `项目规则.md` (project rules): part one is the project copy of `rules/core.md` (with `{{…}}` placeholders replaced by config values), part two is `rules/project.md`.
4. Review `vault/agent/必带清单.md` (the must-read checklist) per section 2 of the guide and adapt it to this project; then regenerate the entry files of the project root and the vault (CLAUDE.md, AGENTS.md, GEMINI.md) with `r2i entries --vault vault`. Existing CLAUDE.md/AGENTS.md/GEMINI.md are kept below the generated block and backed up once as `*.pre-r2i`.
5. Mainline goal and acceptance criteria: `r2i goal-set --mainline <id> --goal '…' --criterion '…' --criterion '…' --approved-by '<owner>' --editor 调度者`. The owner agrees to the goal and to every criterion in the conversation.

The project's rule copies live in `rules/` at the project root (`core.md` copied from `$R2I/rules/core.md`); the copy in `$R2I` is the upstream template and is not edited.

## Step 7: open the first thread

This session is the **dispatcher**.

1. Register yourself: `r2i thread new --title '调度者' --kind 调度`, note the printed `cc_…`, then `r2i thread set --id cc_… --agent 调度者`.
2. Write an agent type definition for the first mainline's driver thread: `.claude/agents/<department>-<line>.md`, in the format of `vault/agent/模板/线程 agent 定义示例.md` (the body holds only identity and the start entry; do not copy rules into it).
3. `r2i thread new --title '<department>｜推进<mainline short name>' --kind 推进 --mainline <id>`, which prints `id` and `first_message`.
4. Start it with the Agent tool in the background: `subagent_type` is the type name from item 2, `run_in_background: true`, prompt = `first_message`. If the Agent tool does not know the type (new files load only in a new session), ask the user to close this session and open it again; then read step 7 of this page and continue.
5. `r2i thread set --id <cc_…> --agent <type name>`.
6. Tell the user: click into this thread in the agent panel to talk to it; anything that needs them will appear in vault `汇报/要批/` and under 「要你定」 on the Obsidian home page.

## Step 8: final checks

- `r2i doctor` all ok (except optional features not configured; say which).
- The project root `.gitignore` excludes at least `tools/r2i/`, `.r2i/`, `runs/` (when runs_root is inside the project) and `ledger/`.
- Give the user a summary of at most 5 lines: where the project is, where the vault is (open that folder in Obsidian), the name of the first thread, which optional features are not configured, and that the rules are in `vault/项目规则.md`.
