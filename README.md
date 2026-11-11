<div align="center">

<img src="docs/img/logo.png" alt="R²I logo: a sleeping cat in a wide hat under a glowing light bulb" width="200">

# R²I

**Resources · Rules · Insight**

A research harness for the next generation of AI agents. The agents do the work and file the reports; you read them and sign off.

Built on Claude Code and Obsidian · one SQLite ledger as the source of truth · a new project from a single prompt

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB.svg)
![Claude Code](https://img.shields.io/badge/built%20for-Claude%20Code-D97757.svg)
![Obsidian](https://img.shields.io/badge/vault-Obsidian-7C3AED.svg)

[中文](README.zh-CN.md) | English

</div>

---

![The human provides resources, rules (a 3 KB must-carry list) and insight; a manager agent and its coworkers write to the SQLite ledger; reports and edicts flow back to the human](docs/img/c-simple.svg)

AI agents can already read papers, write code, run experiments and write up the results on their own. What a research project still needs a person for is narrower: **providing resources, setting the rules, and supplying insight** — choosing the direction and deciding which conclusions to accept. R²I is a harness built around that split.

A team of Claude Code background agents ("threads") works under rules you set. When something genuinely needs your call, it becomes a one-page request in your Obsidian vault, and a checkbox or a sentence from you settles it. Everything else, the threads decide, log and hand off on their own.

The better the models get, the less there is for you to manage. The name comes from the only three things R²I means to leave with a person: **Resources, Rules and Insight**.

The square has a second meaning. In statistics, R² (the coefficient of determination) is the share of the variance a model explains. Treat yourself as the model and the project as the data: your R² is how much of what happened in the project you can actually explain. The more the agents do, the faster that number drops, unless something keeps it up. R²I is built to keep your R² close to 1, so you never lose track of what the work has found.

## Highlights

- **Decisions come to you as pages, not pings.** Threads don't chase you in chat. Anything you need to decide becomes an approval page via `r2i ask`, with the question, the options, a recommendation, the cost and the evidence. With ntfy set up, it is pushed to your phone. Tick an option or type under "my answer", and your exact words go back to the thread. Notifications during quiet hours wait until morning.
- **Designed for stronger models.** Threads are Claude Code background agents that one dispatcher session starts, assigns and closes. Each has a `cc_…` ID, an agent type named after its team, a handover page and a closing check. As models improve, the rules get shorter and you step in less.
- **You stay in sync with the work.** A SQLite ledger is the single source of truth for experiments, decisions, budgets, pending questions, ideas and hypotheses. A script renders it into Obsidian pages every minute: home, approvals, updates, progress, knowledge base and glossary. You read the vault, the agents write the ledger, and rendering never calls a model.
- **One source for the rules.** The project has one rules page (`项目规则.md` in the vault) and one must-read checklist. `CLAUDE.md`, `AGENTS.md` and `GEMINI.md` are all generated from it and never edited by hand, so the rules don't drift or fork when sessions, models or people change.
- **Housekeeping runs overnight.** Approve the night queue from your phone and it runs within the night budget. A night watchdog checks disk space, the external drive and account balances; an hourly self-check catches budget mismatches, stuck queues and unanswered replies. At 04:30 every night, `r2i nightly run` drafts a repo clean-up plan, "dreams" over the previous day's threads to propose memory edits, updates the glossary and writes a morning summary.
- **Ideas don't get lost.** New ideas and hypotheses are logged to the ideas and hypotheses tables as they come up, along with their review status. Before any major change of direction, a thread has to write up what has already been tried.
- **Handoffs and memory that maintain themselves.** A handoff is a single "current state" page. Threads are prompted to rewrite it before they hit the context limit, and a check must pass before a thread can close. Shared memory holds only pointers and conventions that rarely change.
- **Hard gates on spending, anything external and deletion.** Every paid call goes through `r2i paid-run` (budget, per-run cap, ledger entry), and API keys live only in the macOS Keychain. Pushing, publishing and deleting go ahead only on words you typed yourself in that thread.

## Quick start

Create an empty directory, open a new Claude Code session in it, and paste:

```
Set up an R²I research project in this empty directory. First clone
https://github.com/Leozz04/R2I.git into tools/r2i, then read
tools/r2i/BOOTSTRAP.md and follow steps 1 through 8. Ground rules: don't make me
run terminal commands (except adding keys to the Keychain); ask all your questions
at once, and only the ones you need; configure resources in a single pass using
docs/RESOURCES-SETUP.md; set up the rules with me from scratch using
docs/RULES-SETUP.md; ask me before creating remote repositories, pushing, or
sending anything outside this machine.
```

If the project already has a `CLAUDE.md`, `AGENTS.md` or `GEMINI.md`, nothing is lost: `r2i entries` keeps your content below the generated block and saves a one-time backup as `*.pre-r2i`.

The agent checks dependencies, installs everything and asks you four things: the project name, the owner, the time zone and the first research line. It then runs `r2i init`, `r2i install` and `r2i doctor`, sends you a resource checklist to fill in once, works out the rules with you from scratch and starts the first thread. Apart from adding keys to the Keychain, you never need a terminal.

## How it works

![Full architecture: the human provides resources, rules and insight; a manager agent and its coworkers work through the approval and paid-run gates; the SQLite ledger is the single record; reports and edicts flow back to the human; the rules reach every session as a 3 KB must-carry list](docs/img/c-full.svg)

In the diagram, the coworkers are the Claude Code background threads, and "Report · Edict" is the set of vault pages where you read results and record your decisions.

| You own | How |
|---|---|
| Resources | Set up once during install with the [resource checklist](docs/RESOURCES-SETUP.md). After that, you approve only spending above your threshold and the nightly budget. |
| Rules | Use the [rules setup guide](docs/RULES-SETUP.md) to write, from scratch, the list of things you decide. Threads decide everything else and leave a one-line "done as X". |
| Insight | Read the update pages, progress pages and knowledge base in the vault. Important findings get their own update page automatically. |

## Status

Listed as it stands: nothing is described as running until it actually ships.

| Feature | Status |
|---|---|
| Ledger, approval pages, reply collection, thread layer, hooks, `r2i init` / `install` / `doctor` | Shipped (tested privately) |
| Spending gate `r2i paid-run`, code worker `r2i worker`, separate keys for relay endpoints | Shipped |
| Night queue, night watchdog, hourly self-check | Shipped (launchd jobs) |
| Ideas and hypotheses tables, handoff check | Shipped |
| Nightly run `r2i nightly run` (launchd job at 04:30): repo clean-up plan, memory "dreaming", glossary update, morning summary, closing stale update pages | Shipped; requires `[worker]` to be configured. Tested end to end once on a toy project with a real model (claude-sonnet-5-5 through a relay, dry run, 4 worker calls, about 2.1 CNY, 3 minutes): it produced the repo plan, the dream proposal, the glossary page and the morning summary, and passed the link and ID self-check. |
| Self-maintaining memory | Partial: each night, "dreaming" reads the previous day's threads and writes a page of proposed edits. Memory itself changes only after review (pointers only, targeted edits, stale facts rewritten). |

The vault pages, rule files and CLI messages are in Chinese; the English docs describe the same setup.

## What comes next

Planned, not yet built. R²I is tuned for today's models; as base models get stronger, these come next.

1. **Dreaming that brainstorms, not just tidies.** The nightly "dream" pass should also:
   - collect garbage: archive and collapse failed branches in the `experiments` table that taught nothing;
   - cross-pollinate: compare results across research lines, add new hypotheses to the `hypotheses` table, and put a ready-to-approve option in the morning summary, e.g. "Based on last night's 50 runs, drop plan 2 and test hypothesis X instead."
2. **Incubation mode.** For a new project or a major pivot, you and the manager agent work through it in a long, dense conversation (similar to [ARIS](https://github.com/wanshuiyin/Auto-claude-code-research-in-sleep)). Once you agree, a single command turns the conversation into `goals`, `criteria` and `structures` rows in the ledger, and the project switches to quiet, approval-driven execution.
3. **More base models, beyond the Claude Code CLI.** The rules already compile to `CLAUDE.md`, `AGENTS.md` and `GEMINI.md`. Next: threads and workers running on other base models and agent runtimes, all sharing one ledger.
4. **Fewer approvals as models improve.** The list of things you decide should shrink as models get better: fewer approval pages, shorter reports. The ledger still keeps everything for audit.

## Docs

- [BOOTSTRAP.md](BOOTSTRAP.md): the 8 steps the setup session follows
- [docs/RULES-SETUP.md](docs/RULES-SETUP.md): rules setup guide
- [docs/RESOURCES-SETUP.md](docs/RESOURCES-SETUP.md): resource setup checklist
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): the manager, the ledger and the five core mechanisms, one diagram each
- [docs/REFERENCE.md](docs/REFERENCE.md): full command and configuration reference

## Requirements

macOS (launchd, Keychain) · Python 3.11+ · git · Claude Code · Obsidian (recommended; the vault is a plain Markdown folder)

## License

[MIT](LICENSE)
