# Rules setup guide (new project, from scratch)

> For the bootstrap session (BOOTSTRAP step 6) and the project owner. This assumes a brand-new project: no rule content is carried over from any earlier project. r2i brings only the **mechanisms** (ledger, approval pages, handover checks, spending gates); the owner decides the rule content here.

The rules have two layers, each with its own version number:

| Layer | File | Content | Who changes it |
|---|---|---|---|
| Core | the project's `rules/core.md` (copied from `tools/r2i/rules/core.md`) | numbered R-xx; mechanism rules the programs rely on (ask the owner through `r2i ask`, spend through `r2i paid-run`, the ledger is the source of truth, …) | may be changed, but know which program relies on a rule before changing it; record the version |
| Project | the project's `rules/project.md` (copied from `project.md.example`) | department table, duties, paths, rules that hold only for this project; numbered P-xx and per-group letters | per R-50a |

The two layers are assembled into the vault root page `项目规则.md` (project rules), the only rules page threads read. `agent/必带清单.md` (the must-read checklist) is the summary every thread reads at start; the entry files are generated from it.

The rule files and vault pages are in Chinese; this guide describes them in English.

---

## 1. Owner decision list (Appendix 1)

This is the most important list in the rules: **threads ask the owner only about items on it; everything else they decide themselves**, writing one line "done as X · tick 改 (change) if you disagree" and moving on. Too long, and threads ask about everything and the owner drowns; too short, and important things get decided by threads.

Go through the categories below; for each the owner picks "I decide / threads decide / not applicable" and, for the ones they keep, writes one sentence on when it applies. **This is structure and examples only, not recommended answers**; adapt it to the project.

| Category | Example (to explain, not a default) | I decide / threads decide / n/a | When it applies |
|---|---|---|---|
| Direction | whether to open a new idea or research direction | | |
| Goals | changing a mainline goal or acceptance criteria | | |
| Stopping | stopping or replacing an experiment line | | |
| Go-ahead | after a thread restates the idea, the owner says "go" | | |
| Spending | a single spend above `approve_over`; the night budget; top-ups | | |
| Permissions | changing which paths agents may read or write, which commands they may run | | |
| Deletion | deleting data, branches, pages | | |
| Outward | email, publishing, push, merge, uploading datasets, making anything public | | |
| In person | logins, Keychain, installing system jobs, restarting machines | | |
| Conclusions | paper conclusions and public numbers before they are final | | |
| Rules | changing project rules or audit rules | | |
| Spot checks | spot-checking what threads or auditors decided themselves | | |

Also ask "**what I explicitly do not need to be asked about**", for example: code changes, opening and closing small experiments, choosing between two options when one is clearly better, renting compute within budget. That goes on the "not theirs to decide" line at the end of Appendix 1.

Format: number the items in Appendix 1 of the project's `rules/core.md`, one per line (the file itself is in Chinese):

```
1. 对外：发邮件、push、上传数据集、公开任何内容。只认我在执行线程里亲手打的字。
2. 花费：单笔 {{currency}} {{approve_over}} 以上。
…
负责人不管的：改代码；小实验的开和结；预算内租算力。
```

Three hard lines hold whatever the list says: **deletion, anything outward, and changes to accounts or money** need the owner's own typed words (R-10a); spending goes through paid_run (R-21); nothing is made public (R-71). Programs or permissions enforce these; to relax one, change the permission settings first, then the rules.

## 2. Writing the must-read checklist

`vault/agent/必带清单.md` is the one page every thread must read at start. Principles:

- **One page, numbered, one or two sentences per item**, with the rule number in brackets at the end (for example "(R-10)"). Details live in the rules, not here.
- Only what every thread meets every day: how to ask the owner, how to spend, where the source of truth is, where data goes, how to write a handover, what is never allowed. Anything for one department goes into that department's section of the rules.
- Write commands, not principles: "in the same turn you ask the owner, run `r2i ask`" is more useful than "communicate promptly".
- After changing the checklist run `r2i entries --vault vault`; it generates `CLAUDE.md`/`AGENTS.md`/`GEMINI.md` in the project root and the vault. Do not edit those files by hand.
- Rule updates go only into the checklist and the rules page; do not message each thread to reread them. Threads read them at their next start.

The template already contains a checklist written from `core.md`. For a new project: work the Appendix 1 categories into item 4, change the department table and the model tiers (item 17) to this project's reality, and delete items that do not apply.

## 3. Handover, acceptance criteria, preregistration: the minimum

**Handover** (R-43): one page per mainline, `agent/交接/<short> 交接.md`. It is the current state, not a log; rewrite it whole each time, at most 15k tokens. At least four sections: goal card (goal + acceptance criteria, generated from the ledger), what is running, decisions made, next steps. Before closing run `r2i handover-check --mainline <id>`; a FAIL blocks closing. At about 300k tokens of context (or the first auto-compaction), write the handover and ask the dispatcher to open a successor.

**Acceptance criteria**: write each mainline's goal into the ledger with `r2i goal-set`, with 2–5 checkable criteria ("metric Y ≥ Z on data X", not "works well"). Changing goals and criteria belongs to the owner (the "Goals" category). Every experiment row carries `crit=<criterion id>` to say which criterion it pushes; if none, write `crit=无:<reason>`.

**Preregistration**: before an experiment that costs money or runs for hours, write three things in the experiment row or task note: the question it answers, the pass line (a number), and what decision each outcome leads to. After the run, compare honestly and do not move the line afterwards. If the line must change, write why separately and mark the conclusion "post hoc".

## 4. Quiet hours and nights

- During `quiet_hours` (default `00:00-09:00`, in `tz`) there are no push notifications and threads do not wait on the owner. Items on Appendix 1 still get an `r2i ask` page in the same turn; the program holds the notification until the quiet window ends.
- At night, threads decide what is not the owner's and keep going; a line stops only when every path is blocked on Appendix 1 items, after finishing everything that does not need the owner (R-53).
- Night work that costs money uses the night budget `night_budget`: the owner sets it once and it stays; the owner ticks the night queue on its page.
- No new threads and no successors during quiet hours; a thread that reaches its limit still writes its handover, continues through auto-compaction, and hands over after the quiet window.

Ask the owner: their schedule, whether experiments may run overnight, and how large the night budget is.

## 5. Spending tiers (approve_over)

| Tier | Condition | Who decides | How |
|---|---|---|---|
| Small | single spend ≤ `approve_over` | the thread | register with `r2i budget-add` → `r2i paid-run --budget <id> --cap <amount> -- <command>` |
| Large | single spend > `approve_over` | the owner | open an approval page with `r2i ask`, in seven parts: name, the question it answers, how (≤3 steps), how much and how long, what success means, what failure means, what it does not do |
| Night | inside the quiet window | the owner's night budget | the night queue |

Before spending, check the resource table with `r2i res-list` and make one minimal call with every key you will use (R-20). If every call fails, the spend is settled as 0. How high `approve_over` should be depends on the project's money and pace; a common choice is around the price of a meal. Changing it means editing `r2i.toml` and recording one line in the rules' version table.

## 6. Writing into the vault source

1. Create `rules/` in the project root: `core.md` (copied, with Appendix 1 filled) and `project.md` (copied from the example and filled).
2. Assemble `vault/项目规则.md`: keep the template's frontmatter and opening paragraph; replace "第一部分" (part one) entirely with `rules/core.md` and "第二部分" (part two) with `rules/project.md`; replace `{{owner}}` and the other placeholders with the values from `r2i.toml`. Set frontmatter `version` to `core v1.0 + project v1.0`.
3. Edit `vault/agent/必带清单.md` (section 2), then run `r2i entries --vault vault`.
4. Show Appendix 1, the mainline goal and the criteria to the owner; once they agree in the conversation, write the ledger with `r2i goal-set … --approved-by '<owner>'`.
5. Later rule changes follow R-50a: a thread writes a proposal, condensed to three lines, and the owner approves by typing; each change bumps the version by 0.1 and updates `rules/*.md`, `项目规则.md` and the version table together. Each rule has exactly one source; elsewhere use links only (R-32).

Before adding a rule, ask three questions (R-50): is there evidence something went wrong; can it be a program instead; can it be added to something that already exists.
