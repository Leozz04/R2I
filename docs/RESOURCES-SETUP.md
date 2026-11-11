# Resource setup guide: install checklist

> Configure everything once from this checklist at install time. The bootstrap session (BOOTSTRAP step 5) sends the whole "Install checklist" to the user, who ticks the features they want and answers once; the agent does not ask one API at a time. Unticked features stay off and can be added later from this page.

## Install checklist (tick what you want)

| Tick | Feature | Needs | Goes into | Without it |
|---|---|---|---|---|
| required | Claude Code hooks | nothing, written by `r2i init` | project `.claude/settings.json` | — |
| required | Thread agent types | one definition file per resident thread | project `.claude/agents/` | threads cannot be told apart in the panel |
| suggested | Permission rules | an autoMode block | project `.claude/settings.json` | a confirmation prompt for every command |
| suggested | Scheduled jobs | the user agrees to install launchd jobs | `r2i install --load` | pages do not refresh, nobody collects replies |
| [ ] | Data on an external disk | a disk mounted at `/Volumes/<disk>` | `runs_root` | data stays in the project directory |
| [ ] | Relay (Claude-compatible endpoint) | endpoint URL + 1–2 keys | `[relay]`, `[keys] relay / relay_think` | paid calls use the official endpoint only |
| [ ] | Code worker (`r2i worker`) | model name, endpoint, standing budget id | `[worker]` | threads write code themselves or use Claude Code subagents |
| [ ] | Official API (batch / direct) | one API key | `[keys] batch_api` | paid_run does not fill the official key for children |
| [ ] | DeepSeek (Anthropic-compatible endpoint) | one key | `[keys] deepseek` | no DeepSeek; the watchdog does not check its balance |
| [ ] | Phone push (ntfy) | a private topic name | `[keys] ntfy_topic` | approval pages appear only in the vault, no push |
| [ ] | "Wake" button in pushes (experimental) | a second topic + the wake bridge (`r2i install` does not install it; set up separately; not covered by the acceptance run) | `[keys] ntfy_wake_topic` | pushes have no wake button |

## 1. Claude Code settings

### Hooks (written by `r2i init`, nothing to do)

`r2i init` merges two hooks into the project's `.claude/settings.json` and keeps every other key:

- `UserPromptSubmit` → `r2i hook user-prompt`: every message the owner types in a thread is logged in the ledger table `owner_msgs` with a message id. Anything needing the owner's approval (`thread-answer --owner-msg`, budget approval) accepts only message ids logged here. Messages forwarded by the dispatcher set `R2I_DISPATCH=1` and are logged as "not the owner".
- `PreCompact` → `r2i hook pre-compact`: before auto-compaction, reminds the thread to write its handover and run `r2i handover-check`.

`r2i doctor` checks that both are present.

### Thread agent types

One type file per resident thread, in the project's `.claude/agents/<department>-<line>.md`. The agent panel shows only the type name; the owner recognises threads by it. Format (see vault `agent/模板/线程 agent 定义示例.md`):

```markdown
---
name: 甲组-主线推进
description: <project> mainline driver (resident driver thread, started in the background by the dispatcher with the Agent tool; the first message holds the ledger thread id and the handover page)
---

You are the resident driver thread "甲组-主线推进" of the <project> project. At start read vault agent/必带清单.md and work under the current rules; the task, the ledger thread id (cc_…) and the handover page are in the first message.
```

The body holds only identity and the start entry; do not copy rules into it. To pin a model, add `model:` to the frontmatter with a full model id. New type files load only in a new Claude Code session.

### Permission rules (autoMode example)

Write routine operations as allow rules so not every command asks for confirmation; outward actions, deletions and keys still ask. Put this in the project's `.claude/settings.json` (an example; adjust the paths):

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

Keep these out of the allow rules: `git push`, `gh repo create`, delete commands, `security` reading keys, sending email. By the rules they need the owner's own typed words (R-10a).

## 2. Keychain items (`[keys]`)

`[keys]` holds **macOS Keychain item names**, never the keys. Keys live only in the Keychain; config files, rules and memory contain only item names (R-70). Programs read an item when they need it, pass it only to the child process, and never print it.

| `[keys]` entry | Used by | Env var given to the child | When |
|---|---|---|---|
| `batch_api` | `r2i paid-run` | `ANTHROPIC_API_KEY` | the child uses the official endpoint |
| `relay` | `r2i paid-run`, `r2i worker` | `ANTHROPIC_AUTH_TOKEN` | the host of `ANTHROPIC_BASE_URL` equals `[relay] base_url`; the official key is removed from the env |
| `relay_think` | same | `ANTHROPIC_AUTH_TOKEN` | same, with `R2I_RELAY_KEY=think` (when the relay gives reasoning models a separate key) |
| `deepseek` | `r2i paid-run`, night watchdog | `ANTHROPIC_AUTH_TOKEN` | `ANTHROPIC_BASE_URL` contains `deepseek.com`; the watchdog checks the balance |
| `ntfy_topic` | approval-page push | — | push a new approval page to the phone |
| `ntfy_wake_topic` | wake bridge | — | the push's "wake" button posts to this topic |
| `wake_token` | reply-watch | — | wakes the thread when a reply arrives; unset = no wake (`r2i doctor` says so) |

**Naming rule.** Every item name must start with `keychain_prefix` (top-level key of `r2i.toml`; default `<project>-`). Other names are refused with a config error, so r2i cannot be pointed at an unrelated Keychain item.

**Residual risk.** `r2i paid-run` puts the key into the environment of the child command, and the child can read it. Run only your own commands under paid-run.

Suggested item names `<project>-<purpose>`, for example:

```toml
[keys]
relay = "myproj-relay"
batch_api = "myproj-anthropic"
ntfy_topic = "myproj-ntfy"
```

### Write command (the user pastes it in a terminal)

This is the only thing in the whole install the user does in a terminal. The agent fills in the item names and sends all commands at once; the user pastes them one by one and types each key at the prompt. **Input is not echoed** and the key never passes through the agent:

```sh
security add-generic-password -U -a "$USER" -s <item name> -w
```

With `-w` last and no value, `security` prompts for the key twice; `-U` overwrites an existing item. Afterwards the agent runs `r2i doctor`, which checks only that the item exists and never reads the value.

## 3. Relay and code worker

```toml
[relay]
base_url = "https://relay.example.com"   # Claude-compatible endpoint; paid_run gives it [keys] relay, never the official key

[worker]
base_url = "https://relay.example.com"   # empty = official endpoint
model = "claude-sonnet-5-5"              # full model id, no aliases
effort = "high"
budget = "CODE-WORKER-20260101"          # standing paid_run budget id (create it with r2i budget-add first)
cap = 10.0                               # per-run cap; the CLI stops at headroom × cap
headroom = 0.75
usd_rate = 7.3                           # bookkeeping currency per USD, for the estimate
out_dir = ""                             # logs; empty = <runs_root>/code-worker
```

- Test a relay's thinking / effort parameters before relying on them: one minimal call to confirm the model name and parameters take effect.
- Standing budget: create one with `r2i budget-add` (within `approve_over` the thread decides; above it the owner does). Each job is `r2i worker --task-file F --cwd DIR`; spend over the cap is still booked and flagged `over_cap`.
- `r2i worker --dry-run` only prints the command it would run and costs nothing; run it once after setup.

## 4. Where data goes (runs_root)

- Default `runs` (inside the project). Projects with large data point `runs_root` at an external disk, for example `/Volumes/<disk>/<project>`.
- If the external disk is not mounted, the watchdog raises an alert; do not quietly write output back to the internal disk.
- One directory per experiment, `runs_root/runs/<code>/<YYYYMMDD-NN>/`, holding the command, config, seed, environment and results (R-31).

## 5. Scheduled jobs (launchd, macOS)

`r2i install` only writes the plists to `.r2i/launchd/` and prints the commands; once the user agrees in the conversation, the agent runs `r2i install --load` (copies to `~/Library/LaunchAgents` and runs `launchctl bootstrap`).

| Job | Frequency | What it does |
|---|---|---|
| r2i-sync | every 60 s | renders the ledger into the vault: home page, approval pages, progress pages |
| report-reply | resident | collects the owner's answers on approval pages and hands them to the waiting thread |
| selfcheck | hourly | self-checks: budget mismatches, stuck queue, reply-wait not armed, and so on |
| night-watchdog | every 30 min | night watch: disk, external disk, balances |

Labels are `com.r2i.<project>.<job>`; uninstall with `launchctl bootout gui/$(id -u)/<label>`.

## 6. Final check

`r2i doctor` prints one line per check: config, ledger version, vault, both hooks, the plist of each scheduled job, whether each `[keys]` item exists, runs_root. All ok means setup is done; optional features not ticked do not appear in the check.
