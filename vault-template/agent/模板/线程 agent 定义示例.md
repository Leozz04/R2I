---
cssclasses: [research, reader-agent]
读者: agent
---

# Thread agent definition example

One type per standing thread; the file goes at `~/.claude/agents/<department>-<line name>.md` (or the project's `.claude/agents/`). When the dispatcher starts a thread it writes this name as `subagent_type`; the agent panel shows only the type name, and {{owner}} recognizes threads by it. The body states only identity and the starting entry point; do not copy rules in (the rules have one source, the must-read checklist).

```markdown
---
name: 甲组-主线推进
description: {{project}} mainline drive (standing drive thread, started in the background by the dispatcher with the Agent tool; the first instruction carries the ledger thread id and the handover page)
---

You are the standing drive thread "甲组-主线推进" of the {{project}} project. On starting work, first read vault agent/必带清单.md and work under the current rules; the specific task, the ledger thread id (cc_…) and the handover page are in the first message.
```

The dispatcher's sequence: `r2i thread new --title '甲组｜推进主线' --kind 推进 --mainline <id>` returns `cc_…` and `first_message` → start in the background with the Agent tool (`subagent_type: 甲组-主线推进`, with first_message as the prompt) → `r2i thread set --id cc_… --agent <agent name>`. The dispatcher registers itself as `--kind 调度`, and anything in the owner's replies that names the dispatcher is copied to it.
