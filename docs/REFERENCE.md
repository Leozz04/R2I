# Reference

A sqlite ledger, an Obsidian vault and a Claude Code harness for a research project. Rendering never calls a model; threads and the owner
talk through the ledger and the vault pages.

The ledger schema is
frozen as `r2i/schema/schema.sql` (version 1; later changes are numbered files in `r2i/schema/migrations/`, applied by
`db.connect`). The thread layer is in (`r2i thread`, `r2i send`, hooks), `r2i init`/`r2i install`/`r2i doctor` set a project up.

## Use

```
python3.12 -m venv .venv && .venv/bin/pip install -e .
cp r2i.example.toml <project>/r2i.toml      # edit; every key except [keys] and departments is required
cd <project> && r2i --help                                     # or R2I_PROJECT_DIR=<project> r2i ...
```

`r2i/config.py` finds `r2i.toml` through `R2I_PROJECT_DIR`, else the nearest one above the current directory.
There are no built-in project values: a missing key is an error that names it. Runtime state (queue, snapshot,
logs, budget file and spend ledger) lives in `<project>/.r2i/`, never next to the code.

## Config keys

project, owner, tz, quiet_hours, vault, ledger, runs_root, currency, approve_over, night_budget,
max_experiment_threads, `[[mainlines]]` (id, name, short), optional `[keys]` (Keychain item names: batch_api,
deepseek, relay, relay_think, ntfy_topic, ntfy_wake_topic, wake_token; a feature whose item is unset is off; every item name must
start with `keychain_prefix`, default `<project>-`, otherwise config refuses it), optional `departments`,
optional `[relay] base_url` (a Claude-compatible relay: paid_run gives it the relay key, never the official one) and
optional `[worker]` (the code worker: model, base_url, effort, budget, cap, headroom, usd_rate, max_turns, timeout, out_dir).

**Residual risk of paid-run.** `r2i paid-run` reads the `[keys]` item for the endpoint and puts it into the environment of
the child command, so the child (and anything it starts) can read the key. The prefix rule limits which Keychain items
r2i can ever hand out, but not what the child does with its own key: run only your own commands under paid-run.
`wake_token` is read by reply-watch only (never by paid-run); `r2i doctor` reports whether its item exists, never the value.

Thread ids are `cc_<uuid4>` (the Claude Code session id), checked by one validator in `r2i/ids.py`.

## Threads

A thread is a Claude Code background agent run by a dispatcher session. The dispatcher runs `r2i thread new --title T
[--kind 推进|审计|讨论 --model M --mainline ID]`, which mints the `cc_<uuid4>` id, inserts a `threads` row and prints
`{id, first_message}`; it spawns a background agent with `first_message` as the prompt, then records the agent with
`r2i thread set --id ID --agent <agent id>`. The owner types to the thread from the agent panel. `r2i send --to cc_...
--body TEXT [--from cc_...]` is for program-to-thread messages: the thread's next `r2i reply-wait` prints and delivers
them (a thread may leave one undelivered message per recipient per hour); live agents talk through the native
SendMessage. `r2i thread list [--all]` and `r2i thread set --status closed` manage the table.

`r2i hook user-prompt` is the Claude Code UserPromptSubmit hook: it logs an `owner_msgs` row and adds
`owner_msg id: <uuid>` to the prompt context. `thread-answer --owner-msg` and the `budget-add --approval-msg` route
accept only ids of such rows (prompts sent with `R2I_DISPATCH=1` are logged as not the owner's). `r2i hook pre-compact`
reminds the agent to write its handover and run `r2i handover-check --mainline ID [--editor T]` (the closing gate on that
mainline's 交接 page: prints the problems, exit 1 if any). Spending goes through `r2i paid-run <paid_run options> -- <command>`;
a child that makes calls from several threads adds its cost with `r2i.paid_run.add_spend(cny)` (locked, atomic).

Asking the owner: `r2i ask … [--links /abs/report.md,/abs/pack.zip]` opens a page with the files copied into 汇报/附件 and a
「### 我的回答」 block (one slot per numbered item, a 发出 box). `r2i reply-wait --thread cc_…` returns 5 minutes after the
first answer with everything collected (`--window 0` = at once), with the owner's text verbatim and linked files as
absolute paths; on exit it prints `重挂：<command>` on stderr, to be re-armed. Answers that name 调度者 are copied to the
open thread of kind `调度`. `r2i unwatched` lists threads that have something waiting and no live reply-wait.

Code: `r2i worker --task-file F --cwd DIR [--mode audit] [--dry-run]` runs `claude -p` with the `[worker]` model under
paid_run (cap from config, the CLI stops at headroom x cap, cost over the cap is booked and flagged `over_cap`).

## Start a project

```
r2i init <project> --config r2i.toml   # copies the config if missing; ledger, vault (from vault-template/), runs_root,
                                               # <project>/.claude/settings.json hooks, entry files. Safe to repeat.
cd <project> && r2i install                     # renders launchd plists into .r2i/launchd/ (r2i-sync, report-reply,
                                               # selfcheck, night-watchdog); prints the launchctl commands. `r2i install --load`
                                               # copies them to ~/Library/LaunchAgents and bootstraps them.
r2i doctor                                      # one ok/FAIL line per check: config, ledger schema version, vault, hooks,
                                               # plists, Keychain items of [keys] (existence only), runs_root. Exit 1 on any FAIL.
r2i thread new --title '推进｜first task' --kind 推进    # then spawn a background agent with the printed first_message
```

The two hooks in `.claude/settings.json` run `R2I_PROJECT_DIR="$CLAUDE_PROJECT_DIR" <python> -m r2i.cli hook ...`; a second
`r2i init` keeps every other key of that file and replaces only these entries.

