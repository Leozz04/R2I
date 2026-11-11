# Architecture

[中文](ARCHITECTURE.zh-CN.md) | English

The pieces behind the [overview](../README.md#how-it-works), one picture each. Commands are in [REFERENCE.md](REFERENCE.md).

## Manager, ledger and what you see

One dispatcher session opens and assigns the threads; the threads write the sqlite ledger; a program renders the ledger
into vault pages every minute; your ticks and answers go back into the ledger.

![Manager opens threads, threads write the ledger, the ledger renders vault pages, the owner reads and ticks them](img/b1-manager-pipeline.svg)

![Three layers: pages the owner reads, the ledger as the only record, the agents that write it](img/b2-manager-three-bands.svg)

![The owner's three inputs: approval pages, reports and the morning note come from the ledger; ticks, answers and new goals go back to it](img/b3-manager-human-channels.svg)

## Approval gate

Only the things on your "what I decide" list become an approval page (`r2i ask`); everything else a thread decides and
records in one line.

![Approval gate: a decision on the owner's list opens an approval page; anything else is decided by the thread and logged](img/d1-approval-gate.svg)

## Spending gate

Every paid call runs through `r2i paid-run`: a budget, a per-run cap, keys only from the Keychain, the spend in the ledger.

![Spending gate: each paid call passes a budget and a cap, takes its key from the Keychain and writes its spend to the ledger](img/d4-paid-run-gate.svg)

## Nightly run

`r2i nightly run` reads the day's threads and the ledger and writes a memory proposal, a repository plan, the
terminology pages and the morning note.

![Nightly run: thread logs and the ledger go in; memory proposal, repository plan, terminology and morning note come out](img/d2-nightly-consolidation.svg)

## Handover

At the context line a thread rewrites its handover page; after the handover check passes a new session takes over and
the old one stops.

![Handover: write the handover at the line, pass the check, a new session takes over, the old one stops](img/d3-handover.svg)

## One source for the rules

The rules live in the ledger and are rendered into one must-carry list (about 3 KB); `r2i entries` generates CLAUDE.md,
AGENTS.md and GEMINI.md from it, so any session starts from the same rules.

![Rules single source: the ledger renders the must-carry list, which becomes CLAUDE.md, AGENTS.md and GEMINI.md for any session](img/d5-rules-single-source.svg)
