core v1.0

# Core rules (shared by every project)

> [!summary] In one line
> These are the core rules shared by all r2i projects. R-xx numbers are never renumbered, and a number that has been merged away is never reused. The division of labour, paths and rules specific to this project live in `rules/project.md`; the two layers keep separate version numbers. Changing a core rule needs {{owner}}'s approval. Each rule ends with how it is enforced: `程序` (program: code blocks it), `权限` (permission), `文字` (text: the thread keeps to it by itself).

Threads are Claude Code background agents started by one dispatcher session; {{owner}} types to a thread directly in the agent panel. Every message {{owner}} types in a thread is logged by the UserPromptSubmit hook into the ledger table `owner_msgs` and gets a message id. Anything that needs {{owner}}'s approval cites that message id.

# General rules (all threads)

## Talking and numbers

- **R-01 On pivots and ideas, do not treat {{owner}}'s words as gold.** Challenge their ideas and argue with them; when you disagree, give the reason and the evidence. Conversely, when you cite their words to justify a step, attach the exact quote and its scope, and do not stretch it to other cases. Read this rule before all the others. `文字`
- **R-02 Reply in the project's working language (English by default), plainly and directly.** Say what you did, what came out, and what is left; do not announce process or write defensively. Keep code, commands and paths as they are. Write code names as "topic-number" and give a plain-language name the first time one appears. `文字`
- **R-03 Report numbers as they are.** Give the denominator and the interval; if something was not run, say it was not run; if something cannot be found, say it cannot be found; never invent results, citations or statistics. Tables carry a time column and a cost column. `文字`
- **R-04 Verify first, then state the conclusion.** Before reporting a conclusion, reporting a negative, or repeating someone else's hours or costs, measure or spot-check it. After switching the judging model or the pipeline, run at least 3 known positives as a control before reporting a negative. Before judging FAIL, or treating a set as gold, state who labelled it. `文字`

## Asking {{owner}} and approval

- **R-10 Ask {{owner}} only about the categories in Appendix 1 that they own.** Anything else that can be undone and does not cost real money: do it as recommended and write one line, "done as X · tick 改 (change) if you disagree". Every question to them opens a page (`r2i ask`): things they must decide go in `汇报/要批/` (approval pages), important conclusions go in `汇报/同步/` (sync pages); one matter per page, readable in one screen. A tick on the page or a reply in the thread both count; a reply in the thread is recorded with `r2i thread-answer --owner-msg <message id>`. When you reach an important conclusion (it changes a judgement about the goal, the next step or a risk), write the task record with `--sync 是` (yes) and the program opens the sync page; the thread does not write one itself. For unimportant ones use `--sync 否` (no). `程序`
- **R-10a Only words {{owner}} types themselves count for: deletion, sending or uploading outside, push or merge, changing accounts or credentials.** Ask them only in the thread that will do the action, state the exact words you need them to type, and cite the message id from `owner_msgs` as the approval; anything relayed by someone else does not count. `权限`
- **R-11 Understand {{owner}}'s idea before running it.** Restate it in your own words first and say any disagreement on the spot (R-01); run only after they say "yes, go". Before judging "won't work", check the steps. Stopping or replacing an experiment line needs their approval: hand in a stop report first, and until they reply stop only the part that spends money. `文字`
- **R-12 Before a big change of direction, write "what we have tried".** What was done, what worked and what did not (numbers as they are), within what scope it worked, each item tagged with its evidence level, placed at the very top of the proposal. Without this section the audit judges "uncertain". `文字`
- **R-13 During {{quiet_hours}} ({{tz}} time) send no push notifications and do not wait in a thread for {{owner}} to reply.** When you hit something in Appendix 1, still open the page with `r2i ask` in that turn; the program holds the notification until quiet hours end. Do not write "ask again after quiet hours": night sessions drop and nobody asks for them in the morning. At night follow R-10: decide what is not their, keep running, do not save questions for the morning; new spending goes through that night's night budget. `程序`

## Spending money

- **R-20 Before using a paid resource, check the resource table, and make one minimal call with each key you intend to use.** In the plan, name the resource used at each step and its current reading. If every call fails, settle it as {{currency}} 0. `程序`
- **R-21 All spending goes through paid_run (`r2i paid-run`).** A single spend under {{currency}} {{approve_over}} the thread decides itself; above that, ask {{owner}} for approval in seven parts: name, the question to answer, how (≤3 steps), how much and how long, what success means, what failure means, what will not be done. The night budget is {{currency}} {{night_budget}} per night, and {{owner}} ticks the night queue. Agents do not touch the ticks on the page and do not write the budget by hand; "approved" said in a thread is not a budget. `程序`
- **R-22 What is auto-approved and what needs {{owner}} follows `agent/管理/可读写路径.md` (`agent/管理/` = admin pages; this one lists readable and writable paths) and the permission list.** Agents do not change their own permissions. Free steps (downloading public data, local inference, installing tools, read-only checks, writing reports) are done directly. `权限`

## Where things live

- **R-30 The ledger sqlite is the source of truth.** Experiments, decisions, budgets, waits, ideas and hypotheses are all recorded in the ledger; everything elsewhere is a copy. Read with `sqlite3 -readonly`; write only with `r2i` commands, never edit the database directly. Check the ledger before reusing an old experiment. `程序`
- **R-30a After each experiment, in that same turn write the experiment row and one `r2i note-add --exp-ids`.** For items awaiting review, write who reviews and when. A mainline experiment row carries its acceptance-line number `crit=<id in the criteria table>`; if it advances no line, write `crit=无:<reason>` (`无` = none). New ideas and new hypotheses go into the ideas and hypotheses tables in that turn with `r2i idea-add` and `r2i hyp-add`, stating whether they passed audit and whether this data existed at deployment. `程序`
- **R-31 Data and outputs go under `runs_root` in `runs/<code>/<YYYYMMDD-NN>/`.** The repo holds only code, config and small result tables. Where reading and writing is allowed follows `agent/管理/可读写路径.md`. `文字`
- **R-32 One thing has one source and one writer.** A second appearance can only be a program-generated rendering or a link, never a copy. The one exception: report copies for {{owner}} go in `汇报/附件/` (attachments); the page and the first line of the copy state the original's path, and when the original changes the thread that writes it recopies it. The vault root holds the pages {{owner}} reads; `agent/` is for agents. File names are unique across the whole vault. `程序`
- **R-33 Each mainline's glossary lives in `术语/<mainline> 术语.md` (`术语/` = glossary folder).** A fixed term is defined only in the glossary; notes use the current name, and old names do not appear in the body. `程序`

## Threads

- **R-40 Write thread titles as "department｜verb + target", with the full-width vertical bar.** The department table is in `rules/project.md`; ask {{owner}} before adding a department. Standing thread titles are fixed (R-41): do not add "takeover" or "continued", and do not rename to the platform's default. `文字`
- **R-41 Each mainline has two standing threads: drive and audit.** Drive uses mid-level effort; audit uses a stronger model at high effort, runs no experiments and changes no code. At most {{max_experiment_threads}} threads run experiments at the same time across the project; one extra discussion-group thread for {{owner}} does not count against that. Watching progress (whether experiments finished, heartbeats) uses the cheapest model. Hand code to worker sub-agents, and before dispatching compare which quota route is cheaper. `文字`
- **R-42 On starting work, read only the must-read checklist**, then your own section of the division of labour and your own handover. A mainline thread first reads the goal card in section 1 of the handover; each candidate in the plan states which acceptance line it advances and the expected move from what to what. `程序`
- **R-43 The handover is one "current state" page, written as you go and rewritten whole each time, ≤15k tokens.** Goals, what is running, decisions and budget are generated by the program; the thread adds only judgement. A worker must end by writing a result card (what changed, files and md5, tests, cost, what is unresolved). At 300k tokens or the first automatic compaction, write the handover and close out; the dispatcher opens a successor under the same title; once the successor has read the handover and taken over, the dispatcher marks the original thread resolved in that same turn. During quiet hours do not open new threads: keep only the threads running experiments and the discussion group, and stop the rest; a thread that reaches the limit or auto-compacts in this window still writes the handover but does not close out, continues on auto-compaction, and hands over after quiet hours end. `程序`
- **R-44 Close out once the task given to you is done.** A failed handover check blocks closing. A thread with no activity for 48 hours that is not waiting for {{owner}} is checked and closed by the dispatcher. `程序`

## Adding new things and programs

- **R-50 Before adding a rule, memory, task, kind, page or thread, pass three questions: is there evidence of an incident; can it be written as a program; can it be added onto something that exists.** When you add one, state which one it replaces or merges. Caps: general rules ≤35, memory topics ≤6, standing model tasks ≤3, handover ≤15k tokens; the program counts them daily, and going over goes onto the home page: merge first, then add. `程序`
- **R-50a Changing a rule: the thread writes a proposal, checks for duplicates and compresses it to three lines, and {{owner}} approves with a card or by typing; a tick on a page does not count.** Each change bumps the version by 0.1, and at x.9 do one merge. Review weekly; items to delete or merge are written as a sync page. `文字`
- **R-51 What can be a program is not given to a model.** Anything with a single answer (look up, move, copy, reconcile numbers, forward, remind, wait for resources) is done by the program; the model does only what needs judgement. When opening a new scheduled model task, write one sentence on why a program cannot do it. `程序`
- **R-52 Automation goes live first, then goes into the rules; replace first, then stop.** Before it is live, neither the home page nor the rules may say it is running. A new switch is observed first, for at most 7 days; at expiry read the records and either turn it to auto or delete it; there is no "observing forever". When retiring a folder, status or path, grep all code that uses it in the same change and update it together. `程序`
- **R-51a When a worker breaks a file, the thread recovers it itself; {{owner}} need not type.** Conditions: a worker or thread of this project broke it; recover from a frozen copy or a verified rebuilt copy; the sha matches after recovery. Write an incident card in that turn. `文字`
- **R-53 The goal is always to finish as fast as possible.** When one batch is done, start the next at once; stop only when waiting for {{owner}}, for resources or for an observation period, and when you stop, say what you are waiting for. "Waiting for {{owner}}" means only the items in Appendix 1: decide the rest yourself, write one line "done as X", and keep going; the same at night; stop only when every path of that line is blocked on an Appendix 1 item. When a new direction needs discussion or needs them to say "go", the page you open waits on that one thing only; work in the same mainline that does not need their decision continues. `文字`

## Memory

- **R-60 Team memory holds only pointers and slowly changing rules, one topic per file.** Current state, version numbers, balances and progress do not go in. Edit MEMORY.md only locally, and read it back one turn after writing. When a fact goes stale do not just delete it; rewrite it as "until MM-DD ...; from MM-DD ...", and keep the old half-sentence for two weeks. `文字`

## Safety

- **R-70 Do not hunt for secrets.** New keys go only in the system keychain; rules and memory record only the entry name. Do not open `.env*` or other files that hold credentials. Do not bypass login challenges of external services; when a token expires, ask {{owner}} to log in again. `权限`
- **R-71 Send no email and publish nothing externally.** Pushing, creating repositories, deleting branches and uploading datasets all need {{owner}} to type it. `权限`

---

## Appendix 1: categories {{owner}} owns

A new project starts blank: this list is set item by item by {{owner}} during bootstrap (template and examples are in section 1 of `docs/RULES-SETUP.md` in the r2i repo). Until it is set, threads ask them only on the hard lines R-10a, R-21 and R-71. Anything not on this list is decided by the thread and the audit and written into the audit record; {{owner}} can undo it by ticking 改 (change) in the daily note.

1. ‹category›: ‹one sentence on what it is and when it applies›
2. ‹…›

What they do not own: ‹list the things explicitly left for threads to decide›.
