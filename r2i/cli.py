"""r2i commands. Run from the repo root: python3 -B -m r2i.cli <command> ...

  init DIR [--config FILE]               set up a project: r2i.toml (copied from --config if missing), ledger, vault (from vault-template),
                                         DIR/.claude/settings.json hooks, entry files. Idempotent
  install [--load]                       launchd plists for the required jobs into <project>/.r2i/launchd/; --load copies and bootstraps them
  doctor                                 setup checks, one line each (ok/FAIL); exit 1 if any FAIL
  close-report --title --summary --what --mainline --editor [--thread] [--link …]
  ask --thread cc_… --title … --ask … [--mainline --editor --body --option … --links]   open a 汇报/要批/ page; autowait registers the wait
                                         结档汇报: a 同步 page (种类 结档, ≤5 lines in --what); --link repeats: exp code, AUD-…, URL, path (kinds mode)
  ms-add --date --mainline --kind --summary --editor [--impact --exp --evidence --cost --duration] [--sync-page]
                                         --sync-page: render also writes a 汇报/同步/ page skeleton (里程碑: MS-…) for the thread to fill
  ms-supersede OLD NEW                   mark OLD replaced by NEW (corrections keep history)
  ms-skip EXP_ID --editor                judged: this ended experiment is not a milestone
  pending                                ended experiments not yet judged (milestone or skip)
  baseline --editor                      one-time: mark all currently ended, unjudged experiments as judged
  list [--mainline]                      milestones, newest first
  dec-add --mainline --question [--source --title --option ... --thread cc_…]
                                         --title: the question in at most 25 characters (the checkbox line);
                                         --option: one option 「做什么：后果」, repeat for each (folded below)
  dec-retitle --id --title --editor [--option ...]
                                         give an open decision its short title / options; question and status stay
  dec-detail --id DEC-NN --file BODY.md --editor
                                         the decision page's body (agent/模板/汇报模板.md: 结论、背景、结果、要你定、详情；≤4000 字);
                                         the script keeps the title and the 「## 回复」 options (dec-retitle --option)
  dec-act --id --done --editor [--thread cc_…]
                                         R-10: settle an open decision yourself (no money, undoable, no goal/rule change);
                                         --done: what you did, ≤40 字. 首页 shows 「已按 X 做 · 不同意勾「改」」; 改 wakes you
  res-set SERVICE [--kind --unit --balance --limit-note --reset-at --source --category --purpose --credential-ref --runs-on
                  --check-method]        R-20: log the balance after using a resource (catalog fields are optional)
  res-list                               R-20: every resource with its last balance; read before using one
  notify --thread <cc_…> --editor '<线程名>' --type '<负责人要打的原话>' [--why '<一句>']
  wait-add --thread --ask --editor [--report PAGE] [--typed '<words the owner types in chat>']
                                         R-10: you just asked the owner something; after 30 min it goes to 待办 + notification.
                                         --report: the 汇报/ page name where they can answer (write the page first)
  wait-done [ID] [--thread]              the owner replied: close the wait by id; --thread alone only lists that thread's open waits
  thread-answer --wait wait-… --answer '<负责人的选择或摘要>' --owner-msg cc_… --thread cc_… [--done]
                                         the owner answered in the thread chat: record it, close the wait, write 「已在线程里批」 on its page (never ticks);
                                         multi-question pages: --answer "1A 2B". Later: thread-answer --wait … --thread … --done
  wait-move (--id wait-… [--report PAGE] | --from-thread cc_old) --thread cc_new --editor
                                         R-43 handover: point waits at the successor thread; status stays
  reply-ack --thread cc_… [--id RR-…]  cloud threads: you read the owner's answer (reply-status), mark it 已送到
  audit-add --mainline --title --editor [--tag T] [--kind result|decided --experiment --crit --before N/M --after N/M --verdict --grade
            --subject system|tool|baseline --on-crit-set yes|no --cost --decision --evidence ... --thread cc_…]
                                         one audit record (result: audited an experiment; decided: the audit decided something,
                                         needs --decision and --thread; the owner's 改 in 每日笔记 reaches that thread)
  audit-edit --id AUD-… --field before|after|title|note|editor=V ... --reason '…' (--thread cc_… | --editor '<线程名>')
                                         fix an audit record you wrote (old value goes to the 变更 table with the reason); the maintainer thread (config `maintainer`) may too
  note-edit --id TN-… --field links|exp_ids=V ... --reason '…' --editor '<线程名>'
                                         fix the links / exp_ids of a task note (old → new and the reason go to the 变更 table)
  audit-undo-done --id AUD-…             you finished taking back a decision the owner ticked 改 on: closes the wait
  ms-import FILE --editor               import a JSON list of milestones (skips ones already present)
  log-change --editor --path --summary   one row in the ledger's 变更 table for a page written outside render (e.g. 知识/);
                                         off the Mac it is queued. The vault has no 变更记录 page any more
  log-read [--path P] [--since DATE] [--limit N]
                                         read the 变更 table (the snapshot's last 3 days with --queue)
  nq-add --mainline --task --cap --stop-if --editor [--deadline --exp]
                                         night queue item (cap in CNY); the owner ticks it in 今晚可跑 to approve
  nq-list                                night items of the last 3 days
  note-add --mainline --title --did --result --meaning --editor [--exp-ids --limits --cost --duration --links --date --step 步3 --sync 是|否]
                                         end-of-task record for the 07:00 daily agent (printed: terminology warnings)
                                         --sync 是: render writes 汇报/同步/<日期> <title>.md from it (same exp-id: a line in that page's
                                         「## 更新」 instead); no --sync: 是 when --exp-ids is given and --editor is a 推进 thread, else 否
  idea-add / hyp-add --mainline --content --editor [--source-exp CODE --status 新|在试|已证伪|已采纳 --ra-checked 0|1 --deploy-data TEXT]
                                         one new idea (IDEA-…) / hypothesis (HYP-…); the 07:00 note lists yesterday's ideas. Off the Mac use --queue
  terms-check PATH... --vault            internal names that have a current name in 术语/<mainline> 术语.md
  exp-upsert --id LOCAL_ID --editor (--field KEY=VALUE ... | --json FILE)
                                         write one row of the experiment table (r2i.experiments.upsert; fields are checked first).
                                         Off the Mac use --queue
  exp-note CODE --editor [--plain-name --data-layer]
                                         plain name / data layer shown in 实验索引 (CODE 'series:XY' names a series)
  struct-set --mainline --file PAGE.md --version vN --approved-by '<负责人原话 · 出处>' --evidence '<运行编号>' --editor
                                         freeze a new agent/结构/ page (only after the owner ticks 同意; old version moves to agent/结构/_old/)
  handover-append --mainline alpha|beta --section '<段名>' --file MD --editor '<线程名>'
                                         add the file's text (≤8000 字) at the end of that section of vault agent/交接/<主线> 交接.md (no such heading:
                                         a new ## section at the end; never 「一、脚本生成」). Page backed up first; same text already there: already_present.
                                         For threads that cannot write the vault: add --queue (the text travels in the queue file)
  goal-set --mainline --goal --criterion ... --approved-by --editor [--evidence]
                                         new goal version (only after the owner approves; --approved-by names the owner and where)
  crit-set ID --status --editor [--evidence]
                                         progress on one acceptance criterion (未验证/部分/满足/被反证; evidence required)
  crit-read --id --editor [--short --reading --reading-label --target --target-label --scale-max --direction
            --kind --at --note]          the reading drawn as a bar on the 进度 page (direction higher/lower, kind
                                         基线/验收); text and status stay, so no approval is needed
  budget-add --id --cap --purpose (--approval-msg --approval-at --approval-quote | --approval-reply RR-…) --editor [--expires]
                                         under CNY 10 (R-21) instead of the three approval fields: --basis 'small R-21' --audit AUD-…
                                         a paid budget the owner approved in words (cc_ id, time, their words); the host
                                         writes it into BUDGETS.json; never overwrites, never NQ- ids
  budget-revoke ID --editor --reason     withdraw a verbal budget: it expires at once
  entries --vault PATH [--dry-run]       regenerate the repos' CLAUDE/AGENTS/GEMINI.md and rule copies (no database)
  reply-status [--page 汇报页名] [--dec DEC-NN]
                                         read-only: did the owner answer this report page / 首页 decision? (snapshot off the Mac)
  reply-wait --thread cc_… [--timeout 0 --every 20 --window 300]   (timeout 0 = wait until a reply or an `r2i send` message arrives;
                                         after the first answer it collects --window s more and prints them all at once, 0 = at once;
                                         a wake tap or an `r2i send` message goes out at once; on exit stderr says 「重挂：<command>」:
                                         run it again so this thread keeps receiving)
  unwatched                              Mac only: threads with open waits / undelivered answers and no live reply-wait
  thread new --title T [--kind 推进|审计|讨论 --model M --mainline ID --agent A]   mint cc_<uuid4>, insert the row, print {id, first_message}
  thread set --id cc_… [--agent A --title T --status closed] | thread list [--all]
  send --to cc_… --body TEXT [--from cc_…]   program-to-thread message (one undelivered per from/to per hour)
  hook user-prompt|pre-compact           Claude Code hook entry points (stdin = the hook JSON); user-prompt logs the owner_msgs row
  handover-check --mainline ID [--editor T]   the closing gate on that mainline's 交接 page (lint + ledger checks): prints the problems,
                                         exit 1 if there are any (a missing page counts); --editor also runs close-report's size gate
  worker --task-file F [--cwd DIR --mode code|audit --budget B --cap N --dry-run]   code worker ([worker] in r2i.toml), under paid_run
  paid-run <paid_run options> -- <command>   spending goes through this; every argument is passed on to r2i.paid_run unchanged
  audit-wait --mainline M [--timeout 7080 --every 20]   Mac only: wakes an audit on a new 已运行待复核 row or note-add --exp-ids
  status [--stop JOB | --resume JOB]    Mac only, read-only: every launchd job, queue, ledger, vault, disks; exit 0 fine / 1 a job is
                                         down (restartable) / 2 broken (do not restart) / 3 the owner must act. --stop keeps the
                                         hourly self-check from restarting JOB, --resume lifts it
  selfcheck --vault PATH                 the hourly com.r2i.<project>.selfcheck pass (restart dead jobs, note a reboot)
                                         Mac only: wait for the owner's next answer to this thread (report page or 首页 card);
                                         prints 「RR｜页名或小卡｜wait｜选项｜原话」, marks it 已送到, exits. Run it in the
                                         background before you end a turn; one at a time per thread
  render --vault PATH [--dry-run]        apply queue/, read the owner's ticks back, rewrite the auto regions, export snapshot.json

Not on the Mac host? Add --queue before the command (all writes except ms-import and baseline): the write is
saved as a file in queue/ and applied by the next run (every minute). Read state from snapshot.json instead of the database.
"""
import argparse
import json
import os
import re
import sys

from . import db, modes, queue, render, structure, terms, threads
from . import config


def _reply_line(r):
    choice = r['choice'] if 'kind' in r.keys() and r['kind'] == 'wake' else ('选 ' + r['choice']) if r['choice'] else '没选'  # wake: just a nudge
    page = ('抄送·' + r['page']) if 'kind' in r.keys() and r['kind'] == 'copy' else r['page']  # a copy for the dispatcher
    return '｜'.join([r['id'], page, r['wait_id'] or '—', choice,
                     ' '.join((r['text'] or '—').split())])


def _status_cmd(args):
    from . import selfcheck
    from datetime import datetime, timezone
    try:
        if args.queue:
            raise db.HSError('%s runs on the Mac only' % args.cmd)
        conn = db.connect(args.db)
    except db.HSError as e:
        print(json.dumps({'error': str(e)}, ensure_ascii=False)); return 2
    try:
        vault = getattr(args, 'vault', None) or _vault()
        if args.cmd == 'selfcheck':
            out = selfcheck.run(conn, vault, str(args.db), lambda v, e, p, s: render.log_change(v, e, p, s))
            print(json.dumps(out, ensure_ascii=False)); return 0
        if args.stop or args.resume:
            label = args.stop or args.resume
            prefix = config.label('')
            if not label.startswith(prefix):
                label = prefix + label
            print(json.dumps({'stopped': selfcheck.stop(conn, label, resume=bool(args.resume))}, ensure_ascii=False)); return 0
        st = selfcheck.status(vault, str(args.db), datetime.now(timezone.utc))
        st['stopped'] = selfcheck.stopped(conn)
        print(json.dumps(st, ensure_ascii=False, indent=1)); return st['code']
    finally:
        conn.close()


WINDOW = 300  # reply-wait aggregation window in seconds; --window 0 = hand over at once


def _reply_wait(args, sleep=None, clock=None):
    import time
    sleep, clock = sleep or time.sleep, clock or time.monotonic
    try:
        db.check_thread(args.thread)
        if args.queue:
            raise db.HSError('reply-wait runs on the Mac only (it reads the live ledger); off the Mac use reply-status')
        conn = db.connect(args.db)
    except db.HSError as e:
        print(json.dumps({'error': str(e)}, ensure_ascii=False)); return 2
    end = clock() + args.timeout if args.timeout > 0 else None  # timeout 0 = no limit: wait for a reply
    meta_key = 'reply-wait:' + args.thread  # lets the post-reboot checklist see who was hanging
    began = {'start': db.now(), 'timeout': args.timeout, 'pid': os.getpid()}  # pid: selfcheck.unwatched sees a dead wait
    db.meta_set(conn, meta_key, began)
    window = getattr(args, 'window', WINDOW)
    got, msgs, flush_at = {}, {}, None  # after the first answer keep collecting `window` s, then hand everything over once
    while True:
        for r in db.undelivered(conn, args.thread):
            got.setdefault(r['id'], r)
        for m in threads.undelivered_msgs(conn, args.thread):
            msgs.setdefault(m['id'], m)
        if (got or msgs) and flush_at is None:
            flush_at = clock() + window
        if (got or msgs) and (clock() >= flush_at or (end is not None and clock() >= end) or msgs
                              or any(r['kind'] == 'wake' for r in got.values())):  # a wake tap or a program message means now
            rows = list(got.values())
            db.mark_delivered(conn, [r['id'] for r in rows])
            threads.mark_msgs_delivered(conn, list(msgs.values()))
            db.meta_set(conn, meta_key, dict(began, end=db.now()))
            for r in rows:
                print(_reply_line(r))
            for m in msgs.values():
                print(threads.msg_line(m))
            sys.stdout.flush()
            print('重挂：' + _reply_wait_line(args.thread), file=sys.stderr)  # the asking thread keeps receiving
            return 0
        if end is not None and clock() >= end:
            db.meta_set(conn, meta_key, dict(began, end=db.now()))
            mins = args.timeout // 60
            print('%s没有新答复' % ('2 小时' if 7000 <= args.timeout <= 7200 else '%d 分钟' % mins))
            print('重挂：' + _reply_wait_line(args.thread), file=sys.stderr)
            return 0
        stop = min(x for x in (end, flush_at) if x is not None) if (end, flush_at) != (None, None) else None
        sleep(max(1, min(args.every, stop - clock())) if stop is not None else max(1, args.every))


def _setup_cmd(args):
    from . import project
    try:
        if args.cmd == 'init':
            for item, what in project.init(args.dir, args.config):
                print('%-24s %s' % (item, what))
        elif args.cmd == 'install':
            print('\n'.join(project.install(args.load)))
        else:
            res = project.doctor()
            print('\n'.join('%-4s %s' % ('ok' if ok else 'FAIL', line) for ok, line in res))
            return 0 if all(ok for ok, _ in res) else 1
    except (project.SetupError, config.ConfigError) as e:
        print('error: %s' % e, file=sys.stderr); return 2
    return 0


def _thread_cmd(args):
    try:
        if args.queue:
            raise db.HSError('%s writes the ledger directly (Mac only)' % args.cmd)
        conn = db.connect(args.db)
        try:
            if args.cmd == 'send':
                out = threads.send(conn, args.to, args.body, args.from_)
            elif args.thread_cmd == 'new':
                out = threads.new(conn, args.title, args.kind, args.model, args.mainline, args.agent)
            elif args.thread_cmd == 'set':
                out = threads.set_fields(conn, args.id, args.agent, args.title, args.status)
            else:
                for t in threads.list_threads(conn, args.all):
                    print(json.dumps(t, ensure_ascii=False))
                return 0
        finally:
            conn.close()
    except db.HSError as e:
        print(json.dumps({'error': str(e)}, ensure_ascii=False)); return 2
    print(json.dumps(out, ensure_ascii=False))
    return 0


def _queued_mode(name):
    """Off the Mac a queuing session sees the modes in snapshot.json (at most a minute old), else the default."""
    try:
        return json.loads(queue.SNAPSHOT.read_text(encoding='utf-8')).get('modes', {}).get(name) or modes.DEFAULTS[name]
    except (OSError, ValueError, AttributeError):
        return modes.DEFAULTS[name]


def _queued_criteria():
    """Criteria {id: mainline} in snapshot.json, or None where there is no snapshot."""
    try:
        return {c['id']: c.get('mainline') for c in json.loads(queue.SNAPSHOT.read_text(encoding='utf-8')).get('criteria') or []}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _audit_wait(args, sleep=None, clock=None):
    import time
    from . import audit_flow
    try:
        db._check(args.mainline, db.MAINLINES, 'mainline')
        if args.queue:
            raise db.HSError('audit-wait runs on the Mac only (it reads the live ledger)')
        conn = db.connect(args.db)
    except db.HSError as e:
        print(json.dumps({'error': str(e)}, ensure_ascii=False)); return 2
    for line in audit_flow.audit_wait(conn, args.mainline, args.timeout, args.every, sleep or time.sleep, clock or time.monotonic):
        print(line)
    sys.stdout.flush()
    return 0


def _log_read(args):
    import sqlite3
    rows, src = None, 'database'
    try:
        if not args.queue:
            rows = db.get_changes(db.connect(args.db), args.path, args.since, args.limit)
    except db.HSError:
        pass
    if rows is None:
        try:
            snap = json.loads(queue.SNAPSHOT.read_text(encoding='utf-8'))
        except (OSError, ValueError) as e:
            print(json.dumps({'error': 'no ledger and no snapshot: %s' % e}, ensure_ascii=False)); return 2
        mem = sqlite3.connect(':memory:'); mem.row_factory = sqlite3.Row
        mem.execute('CREATE TABLE changes (id INTEGER PRIMARY KEY, at TEXT NOT NULL, editor TEXT, path TEXT, summary TEXT)')
        for r in snap.get('changes', []):
            mem.execute('INSERT INTO changes(id,at,editor,path,summary) VALUES (?,?,?,?,?)', (r['id'], r['at'], r['editor'], r['path'], r['summary']))
        rows = db.get_changes(mem, args.path, args.since, args.limit)
        src = 'snapshot.json %s (last 3 days)' % snap.get('exported_at')
    print('# %s' % src)
    print(''.join(db.change_line(r) for r in rows) or '没有记录。', end='')
    return 0


def _vault():
    """The vault of this project (r2i.toml `vault`)."""
    return str(config.get().vault)


def _rules_version():
    """The rules table's version now (audit-add records it), or None when the vault is not reachable."""
    try:
        v = (render.rules_info(_vault()) or {}).get('version')
    except Exception:
        return None
    return None if not v or v == '?' else v


IDEA_ARGS = ('mainline', 'content', 'editor', 'source_exp', 'status', 'ra_checked', 'deploy_data')


def _note_terms(vals):
    """Old names in an end-of-task record, so the thread can fix them before the 07:00 agent reads it."""
    vault = _vault()
    if not vault:
        return '没找到 vault 路径，跳过术语检查'
    pairs = terms.load(vault)
    text = '\n'.join(str(v) for v in vals.values() if v)
    hits = sorted({(tok, now) for _n, tok, now, _l in terms.scan(text, pairs)})
    return [{'old': tok, 'now': now} for tok, now in hits] or '没有旧名'


def _exp_link(codes):
    """Links from experiments.json, so remote sessions get the same anchors as the page without opening the database.
    Plain series names live in the database, so a series heading the 07:00 agent has named comes from snapshot.json."""
    from . import exp_index
    try:
        rows = json.loads(queue.EXPERIMENTS.read_text(encoding='utf-8'))['rows']
    except (OSError, ValueError, KeyError):
        print(json.dumps({'error': '读不到 %s' % queue.EXPERIMENTS}, ensure_ascii=False)); return 2
    try:
        notes = {n['code']: n for n in json.loads(queue.SNAPSHOT.read_text(encoding='utf-8')).get('exp_notes', [])}
    except (OSError, ValueError):
        notes = {}
    links = exp_index.links(rows, notes)
    print(json.dumps({c: links.get(c, '不在实验台账里') for c in codes}, ensure_ascii=False, indent=1)); return 0


def _sync_said(vals):
    if db.note_sync_wanted(vals.get('sync'), vals.get('exp_ids'), vals.get('editor')):
        return '是：程序下一轮 render 开同步页（同一实验已有页就加一条更新），线程不用自己写页'
    return '否：不开同步页'


def _reply_wait_line(thread):
    """A1: what the asking thread runs next (Mac only; off the Mac, reply-status --page)."""
    return 'r2i reply-wait --thread %s' % thread


def _queued_struct_version(mainline):
    """The frozen version snapshot.json last showed, so a stale version fails now; None when there is no snapshot."""
    try:
        snap = json.loads(queue.SNAPSHOT.read_text(encoding='utf-8'))
        return max([r['version'] for r in snap.get('structures', []) if r['mainline'] == mainline] or [0])
    except (OSError, ValueError, KeyError):
        return None


def _live_vault(vault):
    """Entry files follow only the real vault, so tests rendering a temp vault never rewrite the repos."""
    from pathlib import Path
    return Path(vault).resolve() == config.get().vault.resolve()


def _handover_check(args):
    from . import handover
    try:
        db._check(args.mainline, db.MAINLINES, 'mainline')
        conn = db.connect(args.db)
    except db.HSError as e:
        print(json.dumps({'error': str(e)}, ensure_ascii=False)); return 2
    try:
        bad = handover.lint_page(conn, _vault(), args.mainline)
        if bad is None:
            bad = ['交接页不存在：%s' % handover.PAGES[args.mainline]]
        if args.editor:
            try:
                handover.close_gate(conn, _vault(), args.mainline, args.editor)
            except db.HSError as e:
                bad.append(str(e))
    finally:
        conn.close()
    print('\n'.join('FAIL %s' % b for b in bad) if bad else 'ok 交接页通过：%s' % handover.PAGES[args.mainline])
    return 1 if bad else 0


def main(argv=None):
    raw = sys.argv[1:] if argv is None else list(argv)
    if raw[:1] == ['paid-run']:  # all remaining arguments belong to paid_run, including its `--` separator
        from . import paid_run
        return paid_run.main(raw[1:])
    if raw[:1] == ['worker']:  # code worker: every argument goes to r2i.worker
        from . import worker
        return worker.main(raw[1:])
    if raw[:1] == ['nightly']:  # nightly run + the program steps: every argument goes to r2i.nightly_run
        from . import nightly_run
        return nightly_run.main(raw[1:])
    p = argparse.ArgumentParser(prog='r2i')
    p.add_argument('--db', default=None)
    p.add_argument('--queue', action='store_true', help='do not open the database; write a queue file instead')
    sub = p.add_subparsers(dest='cmd', required=True)
    ini = sub.add_parser('init', help='set up a project directory: config, ledger, vault, .claude hooks')
    ini.add_argument('dir'); ini.add_argument('--config', help='r2i.toml to copy in when DIR has none')
    ins = sub.add_parser('install', help='render launchd plists for the required jobs (--load: copy and bootstrap them)')
    ins.add_argument('--load', action='store_true')
    sub.add_parser('doctor', help='one line per setup check; exit 1 if any FAIL')
    a = sub.add_parser('ms-add')
    for k in ('date', 'mainline', 'kind', 'summary', 'editor'):
        a.add_argument('--' + k, required=True)
    for k in ('impact', 'exp', 'evidence', 'cost', 'duration'):
        a.add_argument('--' + k)
    a.add_argument('--sync-page', action='store_true', dest='sync_page')
    cr = sub.add_parser('close-report', help='结档汇报: a 同步 page (种类 结档) for a thread that closed something out')
    for k in ('title', 'summary', 'what', 'mainline', 'editor'):
        cr.add_argument('--' + k, required=True)
    cr.add_argument('--thread'); cr.add_argument('--link', action='append', help='exp code, AUD-…, URL, or a path; repeat')
    ak = sub.add_parser('ask', help='A1: one 汇报/要批/ page with 问你 and 线程; then run reply-wait')
    for k in ('thread', 'title', 'ask'):
        ak.add_argument('--' + k, required=True)
    ak.add_argument('--mainline', default=None, help='default: the first configured mainline'); ak.add_argument('--editor'); ak.add_argument('--body')
    ak.add_argument('--links', help='files to attach (absolute paths, comma or newline separated), linked from the page')
    ak.add_argument('--option', action='append', help='a tick in 回复; repeat (default 批 / 不批)')
    s = sub.add_parser('ms-supersede'); s.add_argument('old'); s.add_argument('new')
    k = sub.add_parser('ms-skip'); k.add_argument('exp'); k.add_argument('--editor', required=True)
    m = sub.add_parser('ms-import'); m.add_argument('file'); m.add_argument('--mainline', required=True); m.add_argument('--editor', required=True)
    sub.add_parser('pending')
    b = sub.add_parser('baseline'); b.add_argument('--editor', required=True)
    l = sub.add_parser('list'); l.add_argument('--mainline')
    d = sub.add_parser('dec-add')
    d.add_argument('--mainline', required=True); d.add_argument('--question', required=True); d.add_argument('--source')
    d.add_argument('--title'); d.add_argument('--option', action='append')
    d.add_argument('--thread', help=config.say('your thread id (cc_<uuid>): woken when {owner} answers the card on 首页'))
    dr = sub.add_parser('dec-retitle')
    dr.add_argument('--id', required=True); dr.add_argument('--title', required=True); dr.add_argument('--editor', required=True)
    dr.add_argument('--option', action='append')
    dd = sub.add_parser('dec-detail')
    dd.add_argument('--id', required=True); dd.add_argument('--file', required=True); dd.add_argument('--editor', required=True)
    da = sub.add_parser('dec-act')
    da.add_argument('--id', required=True); da.add_argument('--done', required=True); da.add_argument('--editor', required=True)
    da.add_argument('--thread', help='your thread id (cc_<uuid>), if the decision does not name one yet')
    r = sub.add_parser('res-set'); r.add_argument('service')
    for k2 in ('kind', 'unit', 'balance', 'limit-note', 'reset-at', 'source', 'category', 'purpose', 'credential-ref',
               'check-method', 'runs-on'):
        r.add_argument('--' + k2)
    sub.add_parser('res-list')
    w = sub.add_parser('wait-add')
    for k2 in ('thread', 'ask', 'editor'):
        w.add_argument('--' + k2, required=True)
    w.add_argument('--report')
    w.add_argument('--typed', help=config.say('words {owner} must type in chat (no checkbox on 首页), e.g. 批 CODE-01 改账'))
    wd = sub.add_parser('wait-done'); wd.add_argument('id', nargs='?'); wd.add_argument('--thread')
    ta = sub.add_parser('thread-answer')
    ta.add_argument('--wait', required=True); ta.add_argument('--answer'); ta.add_argument('--owner-msg')
    ta.add_argument('--thread', required=True, help='the calling thread (cc_<uuid>); must be the wait\'s own thread'); ta.add_argument('--done', action='store_true')
    c = sub.add_parser('log-change')
    for k2 in ('editor', 'path', 'summary', 'vault'):
        c.add_argument('--' + k2, required=True)
    lr = sub.add_parser('log-read', help='the 变更 table (it replaced 管理/变更记录.md): who changed which page, newest last')
    lr.add_argument('--path', help='one page, e.g. 首页 or 汇报/2030-01-15 某页'); lr.add_argument('--since', help='YYYY-MM-DD (local) or an ISO time')
    lr.add_argument('--limit', type=int, default=50)
    n = sub.add_parser('nq-add')
    for k2 in ('mainline', 'task', 'cap', 'stop-if', 'editor'):
        n.add_argument('--' + k2, required=True)
    n.add_argument('--deadline'); n.add_argument('--exp')
    sa = sub.add_parser('stop-add', help=config.say('{owner} stopped something; paid_run checks this before a run'))
    sa.add_argument('--target', required=True); sa.add_argument('--reason', required=True)
    sa.add_argument('--words'); sa.add_argument('--editor', required=True)
    sl = sub.add_parser('stop-lift'); sl.add_argument('--id', required=True); sl.add_argument('--editor', required=True)
    sub.add_parser('stop-list')
    ms = sub.add_parser('mode-set', help='set an automation to off / observe / auto')
    ms.add_argument('name'); ms.add_argument('mode'); ms.add_argument('--editor')
    ms.add_argument('--until', help='observe only: end date YYYY-MM-DD, at most 7 days from today (default today+7)')
    sub.add_parser('mode-list', help='the mode of every automation (snapshot.json with --queue)')
    sub.add_parser('nq-list')
    b = sub.add_parser('budget-add')
    for k2 in ('id', 'cap', 'purpose', 'editor'):
        b.add_argument('--' + k2, required=True)
    for k2 in ('approval-msg', 'approval-at', 'approval-quote'):  # required unless the small-spend route (--basis --audit) is used
        b.add_argument('--' + k2)
    b.add_argument('--approval-reply', help=config.say("RR-… id of {owner}'s ticked option on a report page without 预算/上限 (instead of --approval-msg/-at/-quote); the page must name the amount"))
    b.add_argument('--basis', help="small-spend route (cap under CNY 10): why a thread may record it, must start with 'small', e.g. 'small R-21'")
    b.add_argument('--audit', help='small-spend route: the audit id (AUD-YYYYMMDD-NN) that covers this spend')
    b.add_argument('--expires'); b.add_argument('--closes', help=config.say('wait-id[,wait-id…] of your own waits {owner} already answered in chat'))
    br = sub.add_parser('budget-revoke'); br.add_argument('id')
    for k2 in ('editor', 'reason'):
        br.add_argument('--' + k2, required=True)
    t = sub.add_parser('note-add')
    for k2 in ('mainline', 'title', 'did', 'result', 'meaning', 'editor'):
        t.add_argument('--' + k2, required=True)
    for k2 in ('exp-ids', 'limits', 'cost', 'duration', 'links', 'date', 'detail', 'data', 'step'):
        t.add_argument('--' + k2)
    t.add_argument('--sync', choices=db.SYNC_VALUES, help='是: the program opens (or updates) the 同步 page; default: 是 for a 推进 thread with --exp-ids')
    t.add_argument('--closes', help='wait-id[,wait-id…] of your own waits: closes exactly these (note-add closes no wait by default)')
    t.add_argument('--detail-file', help='read --detail from this file (markdown, at most 4000 characters)')
    for name, what in (('idea-add', '想法 IDEA-…'), ('hyp-add', '假设 HYP-…')):
        ia = sub.add_parser(name, help='write one %s (ideas / hypotheses table)' % what)
        for k2 in ('mainline', 'content', 'editor'):
            ia.add_argument('--' + k2, required=True)
        for k2 in ('source-exp', 'deploy-data'):
            ia.add_argument('--' + k2)
        ia.add_argument('--status', default='新')
        ia.add_argument('--ra-checked', type=int, choices=(0, 1), default=0, help='1 = checked against prior work')
    el = sub.add_parser('exp-link', help='print the 实验索引 link for each code (reads experiments.json, not the database)')
    el.add_argument('codes', nargs='+')
    en = sub.add_parser('exp-note'); en.add_argument('code'); en.add_argument('--editor', required=True)
    en.add_argument('--plain-name'); en.add_argument('--data-layer')
    eu = sub.add_parser('exp-upsert', help='write one experiment-table row (r2i.experiments.upsert)')
    eu.add_argument('--id', required=True, help='experiment local_id'); eu.add_argument('--editor', required=True)
    eu.add_argument('--field', action='append', default=[], metavar='KEY=VALUE'); eu.add_argument('--json', metavar='FILE')
    g = sub.add_parser('goal-set')
    for k2 in ('mainline', 'goal', 'approved-by', 'editor'):
        g.add_argument('--' + k2, required=True)
    g.add_argument('--criterion', action='append', required=True, help='one acceptance criterion; repeat for each')
    g.add_argument('--evidence', help='what triggered this version (for a pivot: the evidence, old goal, new goal)')
    ha = sub.add_parser('handover-append', help="append text to a section of a mainline's vault handover page")
    for k2 in ('mainline', 'section', 'file', 'editor'):
        ha.add_argument('--' + k2, required=True)
    ss = sub.add_parser('struct-set', help=config.say('freeze a new version of a mainline structure page (only after {owner} ticks 同意)'))
    for k2 in ('mainline', 'file', 'version', 'editor'):
        ss.add_argument('--' + k2, required=True)
    ss.add_argument('--approved-by', help=config.say("{owner}'s words · where they approved (required)"))
    ss.add_argument('--changes', help='what changed in this version (one line for 版本记录; default: 测试 <evidence>)')
    ss.add_argument('--evidence', help='run ids of the tests that passed (required)')
    cs = sub.add_parser('crit-set'); cs.add_argument('id'); cs.add_argument('--status', required=True)
    cs.add_argument('--editor', required=True); cs.add_argument('--evidence')
    cr = sub.add_parser('crit-read')
    cr.add_argument('--id', required=True); cr.add_argument('--editor', required=True)
    for k2 in ('short', 'reading', 'reading-label', 'target', 'target-label', 'scale-max', 'direction'):
        cr.add_argument('--' + k2)
    cr.add_argument('--kind', dest='reading_kind'); cr.add_argument('--at', dest='reading_at')
    cr.add_argument('--note', dest='reading_note')
    tc = sub.add_parser('terms-check'); tc.add_argument('paths', nargs='+'); tc.add_argument('--vault', required=True)
    e = sub.add_parser('entries'); e.add_argument('--vault', required=True); e.add_argument('--dry-run', action='store_true')
    wm = sub.add_parser('wait-move'); wm.add_argument('--id'); wm.add_argument('--from-thread')
    wm.add_argument('--thread', required=True); wm.add_argument('--report'); wm.add_argument('--editor', required=True)
    nt = sub.add_parser('notify', help=config.say('one ntfy push: the exact words {owner} must type in a thread (no wait is opened)'))
    nt.add_argument('--thread', required=True); nt.add_argument('--editor', required=True)
    nt.add_argument('--type', required=True); nt.add_argument('--why')
    ra = sub.add_parser('reply-ack'); ra.add_argument('--thread', required=True); ra.add_argument('--id')
    au = sub.add_parser('audit-add', help='one audit record (kind=result: audited an experiment; kind=decided: the audit decided something)')
    for k2 in ('mainline', 'title', 'editor'):
        au.add_argument('--' + k2, required=True)
    for k2 in ('experiment', 'crit', 'before', 'after', 'verdict', 'grade', 'decision', 'thread', 'subject'):
        au.add_argument('--' + k2)
    au.add_argument('--tag', help='short label [A-Za-z0-9_-]{1,32}: a note-add queued in the same round writes --links @<tag> and gets this audit\'s real id')
    au.add_argument('--on-crit-set', dest='on_crit_set', help='yes|no: is the criterion in the approved criteria set (crit-rollup moves status only for subject=system + yes)')
    au.add_argument('--cost', help='CNY spent'); au.add_argument('--kind', default='result', choices=('result', 'decided'))
    au.add_argument('--evidence', action='append', help='a link (vault attachment / ledger entry / C-grade URL); repeat for several')
    ae = sub.add_parser('audit-edit', help='fix before/after/title/note of an audit record (old value logged with the reason)')
    ae.add_argument('--id', required=True); ae.add_argument('--field', action='append', default=[], metavar='KEY=VALUE')
    ae.add_argument('--reason', required=True); ae.add_argument('--editor'); ae.add_argument('--thread')
    ne = sub.add_parser('note-edit', help='fix the links / exp_ids of a task note (old value logged with the reason)')
    ne.add_argument('--id', required=True); ne.add_argument('--field', action='append', default=[], metavar='KEY=VALUE')
    ne.add_argument('--reason', required=True); ne.add_argument('--editor', required=True)
    ad = sub.add_parser('audit-undo-done', help=config.say('the audit thread finished taking back a decision {owner} ticked 改 on'))
    ad.add_argument('--id', required=True); ad.add_argument('--editor')
    st = sub.add_parser('status'); st.add_argument('--stop'); st.add_argument('--resume')
    sc = sub.add_parser('selfcheck'); sc.add_argument('--vault')
    aw = sub.add_parser('audit-wait', help='E1: block until this mainline has a new 已运行待复核 row or note-add --exp-ids')
    aw.add_argument('--mainline', required=True); aw.add_argument('--timeout', type=int, default=7080)
    aw.add_argument('--every', type=int, default=20)
    rw = sub.add_parser('reply-wait'); rw.add_argument('--thread', required=True)
    rw.add_argument('--timeout', type=int, default=0); rw.add_argument('--every', type=int, default=20)
    rw.add_argument('--window', type=int, default=WINDOW, help='seconds to keep collecting after the first answer (0 = at once)')
    sub.add_parser('unwatched', help='Mac only, read-only: threads with open waits or undelivered answers and no live reply-wait')
    th = sub.add_parser('thread', help='the threads table: new (mint an id + first message), set, list')
    ths = th.add_subparsers(dest='thread_cmd', required=True)
    tn = ths.add_parser('new'); tn.add_argument('--title', required=True); tn.add_argument('--kind', choices=threads.KINDS)
    tn.add_argument('--model'); tn.add_argument('--mainline'); tn.add_argument('--agent')
    tt = ths.add_parser('set'); tt.add_argument('--id', required=True); tt.add_argument('--agent'); tt.add_argument('--title')
    tt.add_argument('--status', choices=('closed',))
    tl = ths.add_parser('list'); tl.add_argument('--all', action='store_true')
    sd = sub.add_parser('send', help='program-to-thread message, handed over by that thread\'s next reply-wait')
    sd.add_argument('--to', required=True); sd.add_argument('--body', required=True); sd.add_argument('--from', dest='from_')
    hc = sub.add_parser('handover-check', help="the closing gate on a mainline's vault 交接 page: lists the problems, exit 1 if any")
    hc.add_argument('--mainline', required=True); hc.add_argument('--editor', help='the closing thread: also applies close-report\'s size gate')
    sub.add_parser('nightly', help='nightly tidy-up: r2i nightly run [--date D] [--dry-run]; also threads, sync-stale, repo-list, report-check (r2i.nightly_run)')
    sub.add_parser('worker', help='code worker: r2i worker --task-file F [--cwd DIR] (r2i.worker; [worker] in r2i.toml)')
    sub.add_parser('paid-run', help='run a command under a paid budget: r2i paid-run --budget B --cap N --purpose P --conclusion C -- CMD (r2i.paid_run)')
    hk = sub.add_parser('hook', help='Claude Code hook entry points (read the hook JSON on stdin)')
    hk.add_argument('kind', choices=('user-prompt', 'pre-compact'))
    rs = sub.add_parser('reply-status'); rs.add_argument('--page'); rs.add_argument('--dec')
    v = sub.add_parser('render'); v.add_argument('--vault', required=True); v.add_argument('--dry-run', action='store_true')
    v.add_argument('--quiet', action='store_true', help='print nothing when the run changed nothing')
    args = p.parse_args(argv)
    if args.cmd == 'budget-add' and not (args.basis or args.audit or args.approval_reply):  # no small-spend route asked for: the three fields stay required
        miss = [('--' + k) for k in ('approval-msg', 'approval-at', 'approval-quote') if not getattr(args, k.replace('-', '_'))]
        if miss:
            p.error('the following arguments are required: ' + ', '.join(miss))
    if getattr(args, 'detail_file', None):
        args.detail = open(args.detail_file, encoding='utf-8').read().strip()
    if args.cmd == 'exp-link':
        return _exp_link(args.codes)

    if args.cmd in ('status', 'selfcheck'):  # Mac only: reads launchd, the live ledger and the disks
        return _status_cmd(args)
    if args.cmd in ('init', 'install', 'doctor'):
        return _setup_cmd(args)
    if args.cmd == 'hook':  # never fails: it runs inside the owner's prompt
        return threads.hook(args.kind, db_path=args.db)
    if args.cmd in ('thread', 'send'):  # direct ledger writes, Mac only
        return _thread_cmd(args)
    if args.cmd == 'handover-check':  # read-only
        return _handover_check(args)
    if args.cmd == 'reply-wait':  # Mac only: reads the ledger, writes only delivered_at
        return _reply_wait(args)
    if args.cmd == 'unwatched':
        from . import selfcheck
        conn = db.connect(args.db)
        try:
            for t, w, u in selfcheck.unwatched(conn):
                print('%s｜open_waits=%d｜undelivered=%d｜%s' % (t, w, u, _reply_wait_line(t)))
        finally:
            conn.close()
        return 0
    if args.cmd == 'audit-wait':  # Mac only, like reply-wait; writes only its cursor in meta
        return _audit_wait(args)
    if args.cmd == 'reply-status':  # read-only; the live database only on the Mac, else snapshot.json
        if not (args.page or args.dec):
            print(json.dumps({'error': 'give --page or --dec'}, ensure_ascii=False)); return 2
        try:
            try:
                c = None if args.queue else db.connect(args.db)
            except db.HSError:
                c = None
            out = queue.reply_status(args.page, args.dec, c)
        except (db.HSError, OSError, ValueError) as e:
            print(json.dumps({'error': str(e)}, ensure_ascii=False)); return 2
        print(json.dumps(out, ensure_ascii=False, indent=1, default=str)); return 0
    if args.cmd == 'log-read':  # the ledger on the Mac, else snapshot.json (at most a minute old)
        return _log_read(args)
    if args.cmd == 'dec-detail':  # the file is read here, so the queue item carries the text itself
        try:
            args.detail = db.clean_detail(open(args.file, encoding='utf-8').read())
        except (OSError, UnicodeDecodeError, db.HSError) as e:
            print(json.dumps({'error': str(e)}, ensure_ascii=False)); return 2
    if args.cmd == 'handover-append':  # the file is read here, so the queue item carries the text itself
        try:
            from . import handover as ho
            with open(args.file, encoding='utf-8') as fh:
                args.text = ho.check_append(args.mainline, args.section, fh.read(), args.editor)
        except (OSError, UnicodeDecodeError, db.HSError) as e:
            print(json.dumps({'error': str(e)}, ensure_ascii=False)); return 2
    if args.cmd == 'struct-set':  # the file is read here, so the queue item carries the text itself
        try:
            with open(args.file, encoding='utf-8') as fh:
                args.body = structure.clean_body(fh.read())
        except (OSError, UnicodeDecodeError, db.HSError) as e:
            print(json.dumps({'error': str(e)}, ensure_ascii=False)); return 2
    if args.cmd == 'exp-upsert':  # the file is read here, so the queue item carries the fields themselves; checked before queuing
        try:
            obj = None
            if args.json:
                with open(args.json, encoding='utf-8') as fh:
                    obj = json.load(fh)
            args.fields = queue.exp_fields(args.field, obj)
            queue.check_exp_upsert(args.id, args.fields, args.editor)
            if args.queue:  # criteria ids from the snapshot; the host checks again when it applies the item
                queue.check_crit(args.fields, _queued_criteria(), args.id, args.editor)
                if _queued_mode('crit-required') == 'auto' and queue.exp_crit_problem(args.fields):
                    raise db.HSError(queue.exp_crit_problem(args.fields))  # observe: queued, logged when applied
            if not args.queue:
                conn = db.connect(args.db)
                try:
                    out = queue.exp_upsert(conn, args.id, args.fields, args.editor)
                finally:
                    conn.close()
                print(json.dumps(out, ensure_ascii=False)); return 0
        except (OSError, UnicodeDecodeError, ValueError, db.HSError) as e:
            print(json.dumps({'error': str(e)}, ensure_ascii=False)); return 2
    if args.cmd == 'note-edit':  # KEY=VALUE pairs -> one dict; checked before queuing
        try:
            args.fields = queue.exp_fields(args.field)
            db.check_note_edit(args.fields, args.reason)
            if not db.NOTE_ID_RE.match(args.id):
                raise db.HSError('--id must look like TN-YYYYMMDD-NN')
        except db.HSError as e:
            print(json.dumps({'error': str(e)}, ensure_ascii=False)); return 2
    if args.cmd == 'audit-edit':  # KEY=VALUE pairs -> one dict; checked before queuing
        try:
            args.fields = queue.exp_fields(args.field)
            from .audits import check_edit
            check_edit(args.fields, args.reason)
            if not (args.editor or args.thread):
                raise db.HSError('give --thread (the audit thread) or --editor')
        except db.HSError as e:
            print(json.dumps({'error': str(e)}, ensure_ascii=False)); return 2
    if args.cmd == 'mode-list' and args.queue:  # off the Mac: the snapshot
        try:
            snap = json.loads(queue.SNAPSHOT.read_text(encoding='utf-8'))
            print(json.dumps(modes.table(None, snap.get('modes') or modes.DEFAULTS, snap.get('observe_until') or {}), ensure_ascii=False, indent=1)); return 0
        except (OSError, ValueError) as e:
            print(json.dumps({'error': 'no snapshot: %s' % e}, ensure_ascii=False)); return 2
    if args.queue:
        mapping = {'ms-add': ('date', 'mainline', 'kind', 'summary', 'editor', 'impact', 'exp', 'evidence', 'cost', 'duration', 'sync_page'),
                   'close-report': ('title', 'summary', 'what', 'mainline', 'editor', 'thread', 'link'),
                   'ask': ('thread', 'title', 'ask', 'mainline', 'editor', 'body', 'option', 'links'),
                   'ms-skip': ('exp', 'editor'), 'ms-supersede': ('old', 'new'), 'dec-add': ('mainline', 'question', 'source', 'title', 'option', 'thread'),
                   'dec-retitle': ('id', 'title', 'option', 'editor'), 'dec-act': ('id', 'done', 'editor', 'thread'), 'dec-detail': ('id', 'detail', 'editor'), 'crit-read': ('id', 'editor') + db.READING_FIELDS,
                   'res-set': ('service',) + db.RESOURCE_FIELDS,
                   'wait-add': ('thread', 'ask', 'editor', 'report', 'typed'), 'wait-done': ('id', 'thread'),
                   'thread-answer': ('wait', 'answer', 'owner_msg', 'thread', 'done'),
                   'wait-move': ('id', 'from_thread', 'thread', 'report', 'editor'), 'reply-ack': ('thread', 'id'),
                   'nq-add': ('mainline', 'task', 'cap', 'stop_if', 'editor', 'deadline', 'exp'),
                   'note-add': db.NOTE_FIELDS + ('closes',),
                   'exp-note': ('code', 'editor', 'plain_name', 'data_layer'),
                   'idea-add': IDEA_ARGS, 'hyp-add': IDEA_ARGS,
                   'exp-upsert': ('id', 'fields', 'editor'),
                   'goal-set': ('mainline', 'goal', 'criterion', 'approved_by', 'editor', 'evidence'),
                   'handover-append': ('mainline', 'section', 'text', 'editor'),
                   'struct-set': ('mainline', 'version', 'approved_by', 'evidence', 'editor', 'body', 'changes'),
                   'crit-set': ('id', 'status', 'editor', 'evidence'),
                   'budget-add': ('id', 'cap', 'purpose', 'approval_msg', 'approval_at', 'approval_quote', 'editor', 'expires', 'closes', 'basis', 'audit', 'approval_reply'),
                   'budget-revoke': ('id', 'editor', 'reason'),
                   'audit-add': ('mainline', 'experiment', 'title', 'crit', 'before', 'after', 'verdict', 'grade', 'cost', 'kind',
                                 'decision', 'evidence', 'editor', 'thread', 'subject', 'on_crit_set', 'tag'),
                   'audit-edit': ('id', 'fields', 'reason', 'editor', 'thread'),
                   'note-edit': ('id', 'fields', 'reason', 'editor'),
                   'audit-undo-done': ('id', 'editor'),
                   'notify': ('thread', 'editor', 'type', 'why'),
                   'stop-add': ('target', 'reason', 'words', 'editor'), 'stop-lift': ('id', 'editor'),
                   'log-change': ('editor', 'path', 'summary'), 'mode-set': ('name', 'mode', 'until')}
        if args.cmd not in mapping:
            print(json.dumps({'error': '--queue only works with ' + ', '.join(mapping)}, ensure_ascii=False)); return 2
        vals = {k: getattr(args, k) for k in mapping[args.cmd] if getattr(args, k, None) is not None}
        try:  # validate here so a bad item fails now, not an hour later on the host
            if args.cmd == 'ms-add':
                db._date(vals['date']); db._check(vals['mainline'], db.MAINLINES, 'mainline'); db._check(vals['kind'], db.KINDS, 'kind')
                if len(vals['summary']) > 120:
                    raise db.HSError('summary must be one sentence of at most 120 characters')
            elif args.cmd == 'close-report':
                from . import kinds
                kinds.check_close_report(vals)
            elif args.cmd == 'ask':
                from . import kinds
                kinds.check_ask(vals)
            elif args.cmd == 'dec-add':
                db._check(vals['mainline'], db.MAINLINES, 'mainline')
                db.check_decision_text(vals.get('title'), vals.get('option'))
                db.check_thread(vals.get('thread'))
            elif args.cmd == 'dec-retitle':
                db.check_decision_text(vals['title'], vals.get('option'))
            elif args.cmd == 'dec-act':
                if not vals['done'].strip() or len(' '.join(vals['done'].split())) > db.ACT_MAX:
                    raise db.HSError('--done: what you did, in at most %d characters' % db.ACT_MAX)
                db.check_thread(vals.get('thread'))
            elif args.cmd == 'crit-read':
                if not db.check_reading({k: vals.get(k) for k in db.READING_FIELDS}):
                    raise db.HSError('nothing to set')
            elif args.cmd in ('wait-move', 'reply-ack'):
                db.check_wait_thread(vals['thread'])
                if args.cmd == 'wait-move':
                    if bool(vals.get('id')) == bool(vals.get('from_thread')):
                        raise db.HSError('give --id or --from-thread (one of them)')
                    db.check_report_name(vals.get('report'))
            elif args.cmd == 'wait-add':
                db.check_wait_thread(vals['thread'])
                if len(vals['ask']) > 120:
                    raise db.HSError('ask must be one sentence of at most 120 characters')
                db.check_report_name(vals.get('report'))
                db.check_typed(vals.get('typed'))
            elif args.cmd == 'thread-answer':
                from . import thread_answer
                answer, owner_msg, vals['thread'] = thread_answer.check(
                    vals['wait'], vals.get('answer'), vals.get('owner_msg'), vals.get('done'), vals.get('thread'))
                vals.pop('answer', None); vals.pop('owner_msg', None)
                if answer:
                    vals.update(answer=answer, owner_msg=owner_msg)
            elif args.cmd == 'wait-done':
                if not vals.get('id'):
                    raise db.HSError('wait-done 只按编号关：给等待编号（--thread 单独用只列出等待，不进队列，去掉 --queue）')
            elif args.cmd == 'note-add':
                db._check(vals['mainline'], db.MAINLINES, 'mainline')
                db.check_close_ids(vals.get('closes'))
                db.check_note_sizes(vals['title'], vals.get('detail'))
                db.check_sync(vals.get('sync'))
                if vals.get('step') is not None:
                    vals['step'] = db.check_step(vals['step'])
                db.check_note_audit_links(vals.get('links'), vals.get('exp_ids'), queue.snapshot_audit_experiment)  # not visible now: the apply-time check decides
            elif args.cmd in ('idea-add', 'hyp-add'):
                db._check(vals['mainline'], db.MAINLINES, 'mainline')
                db._check(vals.get('status', '新'), db.IDEA_STATUS, 'status')
                if not vals['content'].strip():
                    raise db.HSError('content is required')
            elif args.cmd == 'struct-set':
                structure.check_struct(vals['mainline'], vals['version'], vals.get('approved_by'), vals.get('evidence'),
                                       vals['body'], current=_queued_struct_version(vals['mainline']))
            elif args.cmd == 'goal-set':
                db.check_goal(vals['mainline'], vals['goal'], vals['criterion'], vals['approved_by'])
            elif args.cmd == 'crit-set':
                db._check(vals['status'], db.CRITERIA_STATUS, 'status')
                if vals['status'] != '未验证' and not vals.get('evidence'):
                    raise db.HSError('status %s needs --evidence' % vals['status'])
            elif args.cmd == 'nq-add':
                db.check_night_item(vals['mainline'], vals['task'], vals['cap'], vals['stop_if'], vals.get('deadline'))
            elif args.cmd == 'budget-add':
                db.check_close_ids(vals.get('closes'))
                if vals.get('approval_reply'):  # the reply row and the page are checked on the host when it is applied
                    if not db.REPLY_ID_RE.match(vals['approval_reply'].strip()):
                        raise db.HSError('approval-reply must be a report reply id such as RR-20300115-01')
                    if any(vals.get(k) for k in ('approval_msg', 'approval_at', 'approval_quote', 'basis', 'audit')):
                        raise db.HSError('--approval-reply replaces --approval-msg/-at/-quote and the small-spend route; give only one way')
                    if not (db.VERBAL_ID_RE.match(vals['id']) and not vals['id'].upper().startswith('NQ-')) or not str(vals['purpose']).strip():
                        raise db.HSError('budget id / purpose not valid')
                    try:
                        if float(vals['cap']) <= 0:
                            raise ValueError
                    except ValueError:
                        raise db.HSError('cap must be a number of CNY greater than 0')
                else:
                    db.check_verbal_budget(vals['id'], vals['cap'], vals['purpose'], vals.get('approval_msg'), vals.get('approval_at'),
                                           vals.get('approval_quote'), vals.get('expires'), basis=vals.get('basis'), audit=vals.get('audit'))
                if not db.small_route(vals['cap'], vals.get('approval_msg'), vals.get('approval_at'), vals.get('approval_quote'),
                                   vals.get('basis'), vals.get('audit')):  # the audit's existence is checked when it is applied
                    vals.pop('basis', None); vals.pop('audit', None)
            elif args.cmd == 'budget-revoke':
                if not vals['reason'].strip():
                    raise db.HSError('reason is required')
            elif args.cmd == 'audit-add':
                from . import audits
                db.check_tag(vals.get('tag'))
                vals = {('cost_cny' if k == 'cost' else k): v for k, v in vals.items()}
                problem = audits.required(audits.check(vals))
                if problem and _queued_mode('crit-required') == 'auto':  # observe: queued, logged when applied
                    raise db.HSError(problem)
                vals['at'] = db.now()  # the day it was written, not the day the host applies it
                vals['rules_version'] = _rules_version()
            elif args.cmd == 'mode-set':
                modes.check(vals['name'], vals['mode'])
                if vals['mode'] == 'observe' or vals.get('until'):
                    modes.check_until(vals.get('until'))
            elif args.cmd == 'notify':
                from . import notify
                notify.check(vals['thread'], vals['editor'], vals['type'], vals.get('why'))
            elif args.cmd == 'log-change':
                vals['at'] = db.now()
            elif args.cmd == 'audit-edit':
                if not re.match(r'^AUD-\d{8}-\d+$', vals['id']):
                    raise db.HSError('--id must look like AUD-YYYYMMDD-NN')
            elif args.cmd == 'audit-undo-done':
                if not re.match(r'^AUD-\d{8}-\d+$', vals['id']):
                    raise db.HSError('--id must look like AUD-YYYYMMDD-NN')
        except db.HSError as e:
            print(json.dumps({'error': str(e)}, ensure_ascii=False)); return 2
        out = {'queued': queue.enqueue(args.cmd, vals)}
        if args.cmd in ('ask', 'thread-answer'):
            out['next'] = _reply_wait_line(args.thread)
        if args.cmd == 'note-add':
            out['terminology'] = _note_terms(vals)
            out['sync_page'] = _sync_said(vals)
        print(json.dumps(out, ensure_ascii=False)); return 0
    if args.cmd == 'terms-check':  # reads files only, never the database
        print(json.dumps(terms.check_files(args.vault, args.paths), ensure_ascii=False, indent=1)); return 0
    if args.cmd == 'entries':  # files only, never the database
        from . import entries
        print(json.dumps(entries.sync(args.vault, dry_run=args.dry_run), ensure_ascii=False, indent=1)); return 0
    if args.cmd == 'log-change':  # one row in the 变更 table; where the ledger cannot be opened, through the queue
        try:
            db.add_change(db.connect(args.db), render._one_line(args.editor), args.path, render._one_line(args.summary))
            out = {'ok': True}
        except db.HSError:
            out = {'ok': True, 'queued': queue.enqueue('log-change', {
                'editor': render._one_line(args.editor), 'path': args.path,
                'summary': render._one_line(args.summary), 'at': db.now()})}
        print(json.dumps(out, ensure_ascii=False)); return 0

    try:
        conn = db.connect(args.db)
    except db.HSError as e:
        print(json.dumps({'error': str(e)}, ensure_ascii=False)); return 2
    try:
        if args.cmd == 'ms-add':
            out = {'id': db.add_milestone(conn, date=args.date, mainline=args.mainline, kind=args.kind,
                                          summary=args.summary, editor=args.editor, impact=args.impact,
                                          exp_id=args.exp, evidence=args.evidence, cost=args.cost,
                                          duration=args.duration, sync_page=args.sync_page)}
        elif args.cmd == 'close-report':
            from . import kinds
            from . import handover
            lint_bad = handover.close_gate(conn, _vault(), args.mainline, args.editor)  # D3
            out = {'id': kinds.add_close_report(conn, {k: getattr(args, k) for k in ('title', 'summary', 'what', 'mainline', 'editor', 'thread', 'link')})}
            if lint_bad:
                out['handover_lint'] = lint_bad
        elif args.cmd == 'ask':
            from . import kinds
            vault = _vault()
            if not vault:
                raise db.HSError('no vault here: use --queue')
            a = {k: getattr(args, k) for k in ('thread', 'title', 'ask', 'mainline', 'editor', 'body', 'option', 'links')}
            out = {'page': kinds.write_ask(vault, a, log_change=render.log_change), 'next': _reply_wait_line(args.thread)}
        elif args.cmd == 'ms-supersede':
            db.supersede(conn, args.old, args.new); out = {'ok': True}
        elif args.cmd == 'ms-skip':
            db.skip_experiment(conn, args.exp, args.editor); out = {'ok': True}
        elif args.cmd == 'ms-import':
            added, skipped = [], []
            for item in json.load(open(args.file, encoding='utf-8')):
                try:
                    added.append(db.add_milestone(conn, date=item['date'], mainline=args.mainline, kind=item['kind'],
                                                  summary=item['summary'], editor=args.editor, impact=item.get('impact'),
                                                  exp_id=item.get('exp'), evidence=item.get('evidence'),
                                                  cost=item.get('cost'), duration=item.get('duration')))
                except db.HSError as e:
                    if 'already exists' not in str(e):
                        raise
                    skipped.append(item.get('exp'))
            out = {'added': len(added), 'skipped': skipped}
        elif args.cmd == 'pending':
            out = [dict(r) for r in db.pending_experiments(conn)]
        elif args.cmd == 'baseline':
            out = {'marked': db.baseline_reviews(conn, args.editor)}
        elif args.cmd == 'list':
            out = [dict(r) for r in render._milestones(conn, args.mainline)]
        elif args.cmd == 'dec-add':
            out = {'id': db.add_decision(conn, args.mainline, args.question, args.source, args.title, args.option, args.thread)}
        elif args.cmd == 'dec-detail':
            out = db.set_detail(conn, args.id, args.detail)
            out['changelog'] = render.log_ledger_change(_vault(), 'dec-detail', out, args.editor)
        elif args.cmd == 'dec-act':
            out = db.act_decision(conn, args.id, args.done, args.editor, args.thread)
            out['changelog'] = render.log_ledger_change(_vault(), 'dec-act', out, args.editor)
        elif args.cmd == 'dec-retitle':
            out = db.retitle_decision(conn, args.id, args.title, args.option)
            out['changelog'] = render.log_ledger_change(_vault(), 'dec-retitle', out, args.editor)
        elif args.cmd == 'crit-read':
            out = db.set_reading(conn, args.id, **{k: getattr(args, k) for k in db.READING_FIELDS})
            out['changelog'] = render.log_ledger_change(_vault(), 'crit-read', out, args.editor)
        elif args.cmd == 'res-set':
            db.set_resource(conn, args.service, **{k: getattr(args, k) for k in db.RESOURCE_FIELDS})
            out = {'ok': True}
        elif args.cmd == 'res-list':
            out = [dict(r) for r in conn.execute('SELECT * FROM resources ORDER BY category, service').fetchall()]
        elif args.cmd == 'wait-add':
            out = {'id': db.add_wait(conn, args.thread, args.ask, args.editor, report=args.report, typed=args.typed)}
        elif args.cmd == 'wait-done':
            if args.id:
                db.check_wait_thread_match(conn, args.id, args.thread)
                out = {'closed': db.close_wait(conn, args.id)}
            elif args.thread:  # list only, close nothing
                rows = conn.execute("SELECT id, ask, asked_at FROM waits WHERE status='等待' AND thread=? ORDER BY asked_at",
                                    (args.thread,)).fetchall()
                out = {'closed': 0, 'open': [dict(r) for r in rows], 'note': '没关任何等待；按编号关：wait-done <ID> --thread %s' % args.thread}
            else:
                raise db.HSError('give an id or --thread')
        elif args.cmd == 'thread-answer':
            from . import thread_answer
            out = thread_answer.apply(conn, _vault(), args.wait, args.answer, args.owner_msg, args.done, args.thread)
            if isinstance(out, dict) and args.thread:
                out['next'] = _reply_wait_line(args.thread)  # re-arm hint
        elif args.cmd == 'wait-move':
            out = db.move_waits(conn, args.thread, args.id, args.from_thread, args.report)
            out['changelog'] = render.log_wait_move(_vault(), out, args.editor)
        elif args.cmd == 'notify':
            from . import notify
            out = notify.run(conn, args.thread, args.editor, args.type, args.why, vault=_vault())
        elif args.cmd == 'reply-ack':
            out = db.ack_replies(conn, args.thread, args.id)
        elif args.cmd == 'nq-add':
            out = {'id': db.add_night_item(conn, mainline=args.mainline, task=args.task, cap_cny=args.cap,
                                           stop_if=args.stop_if, editor=args.editor, deadline=args.deadline,
                                           exp_id=args.exp)}
        elif args.cmd == 'note-add':
            vals = {k: getattr(args, k) for k in db.NOTE_FIELDS}
            vals['links'], unknown = db.resolve_audit_tags(conn, args.editor, vals['links'])
            if unknown:
                raise db.HSError('--links 里 %s 没有对应的审计（先用同一个线程 audit-add --tag）' % '、'.join('@' + t for t in unknown))
            db.check_closes(conn, args.editor, args.closes)  # before anything is written
            out = {'id': db.add_task_note(conn, **vals), 'terminology': _note_terms(vals), 'sync_page': _sync_said(vals)}
            out['closed_waits'] = db.close_waits_of_editor(conn, args.editor, db.now(), 'note-add', vault=_vault(), closes=args.closes, answered=False)
            out['still_open_waits'] = db.still_open_waits(conn, args.editor)
        elif args.cmd in ('idea-add', 'hyp-add'):
            out = {'id': db.add_idea(conn, 'ideas' if args.cmd == 'idea-add' else 'hypotheses',
                                     **{k: getattr(args, k) for k in IDEA_ARGS})}
        elif args.cmd == 'exp-note':
            db.set_exp_note(conn, args.code, args.editor, args.plain_name, args.data_layer); out = {'ok': True}
        elif args.cmd == 'goal-set':
            out = db.set_goal(conn, mainline=args.mainline, goal=args.goal, criteria=args.criterion,
                              approved_by=args.approved_by, evidence=args.evidence)
        elif args.cmd == 'handover-append':
            vault = _vault()
            if not vault:
                raise db.HSError('no vault here: use --queue')
            from . import handover as ho
            out = ho.append(vault, args.mainline, args.section, args.text, args.editor, render.log_change)
            if out['status'] == 'busy':
                print(json.dumps({**out, 'error': 'page edited in the last minute; nothing written, run it again shortly'}, ensure_ascii=False)); return 2
        elif args.cmd == 'struct-set':
            out = structure.set_struct(conn, _vault(), mainline=args.mainline, version=args.version,
                                       approved_by=args.approved_by, evidence=args.evidence, editor=args.editor, body=args.body, changes=args.changes)
        elif args.cmd == 'crit-set':
            out = db.set_criterion(conn, args.id, args.status, args.evidence)
        elif args.cmd == 'budget-add':
            db.check_closes(conn, args.editor, args.closes)  # before anything is written
            out = {'id': db.add_verbal_budget(conn, id=args.id, cap_cny=args.cap, purpose=args.purpose,
                                              approval_msg=args.approval_msg, approval_at=args.approval_at,
                                              approval_quote=args.approval_quote, editor=args.editor, expires=args.expires,
                                              basis=args.basis, audit=args.audit, approval_reply=args.approval_reply, vault=_vault())}
            _row = conn.execute('SELECT approval_at FROM verbal_budgets WHERE id=?', (args.id,)).fetchone()
            out['closed_waits'] = db.close_waits_of_editor(conn, args.editor, args.approval_at or (_row['approval_at'] if args.approval_reply else None) or db.now(), 'budget-add', vault=_vault(), closes=args.closes)
            out['still_open_waits'] = db.still_open_waits(conn, args.editor)
        elif args.cmd == 'budget-revoke':
            out = {'id': db.revoke_verbal_budget(conn, args.id, args.editor, args.reason)}
        elif args.cmd == 'audit-add':
            from . import audits
            out = {'id': audits.add(conn, mainline=args.mainline, experiment=args.experiment, title=args.title, crit=args.crit,
                                    before=args.before, after=args.after, verdict=args.verdict, grade=args.grade,
                                    subject=args.subject, on_crit_set=args.on_crit_set,
                                    cost_cny=args.cost, kind=args.kind, decision=args.decision, evidence=args.evidence,
                                    editor=args.editor, thread=args.thread, rules_version=_rules_version())}
            if args.tag:
                db.check_tag(args.tag)
                db.remember_audit_tag(conn, args.editor, args.tag, out['id'])
            from . import audit_flow
            out['wake'] = audit_flow.wake_after_audit(conn, out['id'])  # E2
        elif args.cmd == 'note-edit':
            out = db.edit_task_note(conn, args.id, args.fields, args.reason, args.editor)
        elif args.cmd == 'audit-edit':
            from . import audits
            out = audits.edit(conn, args.id, args.fields, args.reason, args.editor, args.thread)
        elif args.cmd == 'audit-undo-done':
            from . import audits
            out = audits.undo_done(conn, args.id)
        elif args.cmd == 'mode-set':
            out = modes.set_mode(conn, args.name, args.mode, args.until)
            db.add_change(conn, render._one_line(args.editor or 'r2i-sync 脚本'), 'agent/管理/自动化开关',
                          '自动化 %s：%s → %s' % (args.name, out['was'], args.mode))
        elif args.cmd == 'mode-list':
            out = modes.table(conn)
        elif args.cmd == 'stop-add':
            out = {'id': db.add_stop(conn, args.target, args.reason, args.editor, args.words)}
            db.add_change(conn, render._one_line(args.editor), 'agent/管理/叫停', '叫停 %s：%s' % (args.target, args.reason))
        elif args.cmd == 'stop-lift':
            out = db.lift_stop(conn, args.id, args.editor)
            db.add_change(conn, render._one_line(args.editor), 'agent/管理/叫停', '解除叫停 %s' % args.id)
        elif args.cmd == 'stop-list':
            out = [dict(r) for r in db.active_stops(conn)]
        elif args.cmd == 'nq-list':
            out = [dict(r) for r in conn.execute("SELECT * FROM night_items WHERE created_at>=? OR status IN ('待批','已批') "
                                                 "ORDER BY id DESC", (render._days_ago(3),)).fetchall()]
        elif args.cmd == 'render':
            applied = {} if args.dry_run else queue.apply_all(conn, args.vault)
            out = render.render(conn, args.vault, dry_run=args.dry_run, sync_entries=_live_vault(args.vault))
            out['queue'] = {'applied': len(applied.get('applied', [])), 'failed': applied.get('failed', []),
                            'inbox': applied.get('inbox', {}).get('status')}
            if not args.dry_run:
                out['snapshot_pending'] = queue.export_snapshot(conn)
            nightly = out.get('night', {})
            if args.quiet and not (out['changed'] or out['decisions_closed'] or out['queue']['applied'] or out['queue']['failed']
                                   or nightly.get('ticks') or nightly.get('budgets') or nightly.get('error')
                                   or nightly.get('notified') or nightly.get('waits_ticked') or nightly.get('task_log')
                                   or out.get('entries', {}).get('changed') or out.get('entries', {}).get('error')):
                return 0
    except db.HSError as e:
        print(json.dumps({'error': str(e)}, ensure_ascii=False)); return 2
    finally:
        conn.close()
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
