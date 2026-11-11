"""Write path for sessions that are not on the Mac host (cloud threads reaching the repo through a mounted folder).

They must not open the live sqlite (WAL over a VM mount can corrupt it). Instead each write becomes one JSON file in
queue/, and the hourly host job applies the files in name order, then exports snapshot.json for them to read.
"""
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import re
import secrets
import sqlite3

from . import db, experiments, modes
from . import config

QUEUE = Path(os.environ.get('R2I_QUEUE') or config.state_dir() / 'queue')         # override only in tests
SNAPSHOT = Path(os.environ.get('R2I_SNAPSHOT') or config.state_dir() / 'snapshot.json')
OPS = ('ms-add', 'ms-skip', 'ms-supersede', 'dec-add', 'dec-retitle', 'dec-act', 'dec-detail', 'crit-read', 'res-set', 'nq-add', 'note-add', 'exp-note', 'exp-upsert', 'idea-add', 'hyp-add', 'goal-set', 'struct-set', 'handover-append', 'crit-set',
       'budget-add', 'budget-revoke', 'wait-add', 'wait-done', 'thread-answer', 'wait-move', 'reply-ack', 'audit-add', 'audit-undo-done', 'audit-edit', 'note-edit', 'log-change', 'mode-set', 'close-report', 'notify', 'stop-add', 'stop-lift', 'ask')
# Threads without repo access drop hand-written JSON here (they ask the owner once for access to this one folder). It sits in
# the home folder because macOS refuses background jobs access to the external disk (Operation not permitted).
INBOX = Path(os.environ.get('R2I_INBOX') or config.state_dir() / 'inbox')
# no goal or budget changes from the inbox: paid runs need the repo (paid_run.py), so its threads use the queue
INBOX_OPS = ('ms-add', 'ms-skip', 'dec-add', 'dec-act', 'dec-detail', 'nq-add', 'note-add', 'exp-note', 'exp-upsert', 'handover-append', 'wait-add', 'wait-done', 'wait-move', 'reply-ack', 'thread-answer',
             'audit-add', 'audit-undo-done', 'audit-edit', 'note-edit', 'close-report')
INBOX_MIN_AGE = 30  # seconds: a file still being copied in is left for the next run
INBOX_README = config.say("""# r2i-sync 投递目录（<项目>/.r2i/inbox）

给没有仓库权限的线程用：向 {owner} 申请这一个文件夹的权限，然后往里放文件。每次投一个 JSON 文件，Mac 上每分钟一次的运行会写进实验台账：

    {"op": "note-add", "args": {"mainline": "<主线 id>", "editor": "<线程名>", "title": "…", "did": "…",
     "result": "…", "meaning": "…", "data": "…", "limits": "…", "exp_ids": "…", "cost": "…", "duration": "…",
     "links": "…", "detail": "最多 4000 字"}}

- 先写成 `.名字.json.tmp`，写完再改名成 `名字.json`；写好 30 秒后才会被处理。
- op 只能是：%s。参数和 `python3 -B -m r2i.cli <op>` 一样，`-` 换成 `_`。
- 处理完的文件移到 `done/`，旁边的 `.result.txt` 里有记录编号；出错的移到 `failed/`，旁边的 `.error.txt` 写原因。
""") % '、'.join(INBOX_OPS)


def enqueue(op, args):
    if op not in OPS:
        raise db.HSError('cannot queue ' + op)
    QUEUE.mkdir(exist_ok=True)
    # microseconds keep files from one session in the order they were written (applied in name order)
    name = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f') + '-' + secrets.token_hex(3) + '.json'
    tmp = QUEUE / ('.' + name)
    tmp.write_text(json.dumps({'op': op, 'args': args}, ensure_ascii=False), encoding='utf-8')
    os.replace(tmp, QUEUE / name)
    return name


def exp_fields(pairs=(), obj=None):
    """--field KEY=VALUE items plus the --json object -> one dict (a --field wins over the file)."""
    fields = {}
    if obj is not None:
        if not isinstance(obj, dict):
            raise db.HSError('--json must hold a JSON object of fields')
        fields.update(obj)
    for item in pairs or ():
        key, sep, value = item.partition('=')
        if not sep:
            raise db.HSError('--field must be KEY=VALUE, got %r' % item)
        fields[key.strip()] = value
    return fields


REVIEW_ALIASES = {'reviewer': 'reviewer', '复核人': 'reviewer', 'review_due': 'review_due', '复核截止': 'review_due'}


def split_review(fields):
    """(experiment fields, {reviewer, review_due}) - the review columns are local and not in the field validator."""
    rest, review = {}, {}
    for k, v in fields.items():
        if k in REVIEW_ALIASES:
            review[REVIEW_ALIASES[k]] = (v or '').strip() if isinstance(v, str) else v
        else:
            rest[k] = v
    due = review.get('review_due')
    if due:
        try:
            db.parse_time(due) if 'T' in due else datetime.strptime(due, '%Y-%m-%d')
        except (TypeError, ValueError):
            raise db.HSError('review_due must be YYYY-MM-DD or an ISO time, got %r' % due) from None
    return rest, review


def check_exp_upsert(local_id, fields, editor):
    """Same validation experiments.upsert does before it touches the database (normalize_fields); nothing is written.
    Setting stage 已运行待复核 needs reviewer and review_due in the same call."""
    if not (local_id or '').strip():
        raise db.HSError('--id is required')
    if not (editor or '').strip():
        raise db.HSError('--editor is required')
    rest, review = split_review(fields)
    try:
        norm = experiments.normalize_fields(rest)
    except experiments.FieldError as e:
        raise db.HSError('bad experiment field: %s' % e) from None
    if norm.get('stage') == db.REVIEW_STAGE and not all(review.get(c) for c in db.REVIEW_COLUMNS):
        raise db.HSError('stage %s needs --field reviewer=<谁复核> and --field review_due=YYYY-MM-DD' % db.REVIEW_STAGE)


def exp_crit(fields):
    """The crit field of exp-upsert fields (by its key), stripped; '' when absent."""
    return next(((v or '').strip() for k, v in fields.items() if experiments.ALIASES.get(k) == 'crit'), '')


def exp_crit_problem(fields):
    """crit-required: exp-upsert must say which criterion the experiment advances (crit=<id>), or crit=无:<理由>."""
    if not exp_crit(fields):
        return 'exp-upsert needs --field crit=<验收条目编号> (or crit=无:<理由> when it advances no criterion)'


def crit_mainline(fields, local_id, editor='', row=None):
    """The mainline an exp-upsert row belongs to (exp_index.mainline_of on the stored row plus the given fields; a new row's
    editor name can indicate the mainline when nothing else does)."""
    from . import exp_index
    cur = {'mainline': '[]', **(dict(row) if row is not None else {})}
    try:
        norm = experiments.normalize_fields(split_review(fields)[0])
    except Exception:  # check_exp_upsert has already said what is wrong with the fields
        norm = {}
    if 'mainline' in norm:
        cur['mainline'] = json.dumps(norm['mainline'], ensure_ascii=False)
    return exp_index.mainline_of(cur)


def check_crit(fields, ids, local_id='', editor='', row=None):
    """Always on: a given crit is existing criteria ids (comma-separated) or 无:<理由>. `ids` {id: mainline}, None = cannot look them up here."""
    value = exp_crit(fields)
    if not value or ids is None or re.match(r'^无[:：]\s*\S', value):
        return
    bad = [c for c in (x.strip() for x in re.split(r'[,，]', value)) if c not in ids]
    if bad or not value.strip(',， '):
        ml = crit_mainline(fields, local_id, editor, row)
        mine = sorted(i for i, m in ids.items() if m == ml)
        shown = mine or sorted(ids)
        raise db.HSError('crit: no criterion %s；要写完整 id（逗号分隔），或 无:<理由>。%s 的 id：%s。例如 `--field crit=%s` 不行，要写完整 id，例如 `--field crit=%s`'
                         % ('、'.join(bad) or repr(value), ml if mine else '全部', '、'.join(shown) or '（还没有验收条目）', (bad or [value])[0], shown[0] if shown else '<id>'))


def exp_upsert(conn, local_id, fields, editor):
    """Write one row of the experiments table through r2i.experiments.upsert. It has no editor column, so the editor is only in
    the returned result."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='experiments'").fetchone():
        raise db.HSError('experiments table missing in this database (connect once with db.connect to create it)')
    check_exp_upsert(local_id, fields, editor)
    check_crit(fields, {r[0]: r[1] for r in conn.execute('SELECT id, mainline FROM criteria')}, local_id, editor,
               conn.execute('SELECT * FROM experiments WHERE local_id=?', (local_id,)).fetchone())
    from .render import log_change
    modes.require(conn, log_change, 'crit-required', '实验/' + local_id, exp_crit_problem(fields), local_id)  # auto: nothing written
    rest, review = split_review(fields)
    try:
        result = experiments.upsert(conn, local_id, rest)
    except experiments.FieldError as e:
        raise db.HSError('bad experiment field: %s' % e) from None
    if review:
        conn.execute('UPDATE experiments SET %s WHERE local_id=?' % ', '.join(c + '=?' for c in review),
                     list(review.values()) + [local_id])
        conn.commit()
        result['review'] = review
    result['editor'] = editor
    return result


AUDIT_ARGS = ('mainline', 'experiment', 'title', 'crit', 'before', 'after', 'verdict', 'grade', 'cost_cny', 'kind', 'decision',
              'evidence', 'editor', 'thread', 'at', 'rules_version', 'subject', 'on_crit_set')
LAST_INBOX = {}


def apply_all(conn, vault=None):
    out = _apply_dir(conn, QUEUE, OPS, vault=vault)
    out['inbox'] = inbox = _apply_inbox(conn)
    LAST_INBOX.clear(); LAST_INBOX.update({k: inbox.get(k) for k in ('status', 'error')})
    out['applied'] += inbox.pop('applied', [])
    out['failed'] += inbox.pop('failed', [])
    return out


def _apply_inbox(conn):
    """The drop folder on the external disk. Unmounted disk = nothing to do; any other problem is reported, not raised."""
    if not INBOX.parent.exists():
        return {'status': 'unmounted'}
    try:
        INBOX.mkdir(exist_ok=True)
        readme = INBOX / 'README.md'
        if not readme.exists() or readme.read_text(encoding='utf-8') != INBOX_README:
            readme.write_text(INBOX_README, encoding='utf-8')
        res = _apply_dir(conn, INBOX, INBOX_OPS, min_age=INBOX_MIN_AGE, receipts=True)
        res['status'] = 'ok'
        return res
    except OSError as e:  # e.g. macOS refuses the background job access to the external disk
        return {'status': 'error', 'error': '%s: %s' % (type(e).__name__, e)}


def _op_of(f):
    try:
        return json.loads(f.read_text(encoding='utf-8')).get('op')
    except (OSError, ValueError, AttributeError):
        return None


NOTE_WAIT_SECONDS = 3600  # a note whose @tag audit has not landed after this long fails


def snapshot_audit_experiment(aid):
    """The experiment of audit `aid` as the snapshot (written by the host every minute) shows it; None when not visible."""
    try:
        rows = json.loads(SNAPSHOT.read_text(encoding='utf-8')).get('audits') or []
    except (OSError, ValueError):
        return None
    return next((r.get('experiment') for r in rows if r.get('id') == aid), None)


def _apply_dir(conn, folder, allowed, min_age=0, receipts=False, vault=None):
    import time
    done, failed = [], []
    if not folder.exists():
        return {'applied': done, 'failed': failed}
    (folder / 'done').mkdir(exist_ok=True)
    (folder / 'failed').mkdir(exist_ok=True)
    # a note-add goes after the audits of the same round (stable: everything else keeps its order)
    for f in sorted(folder.glob('*.json'), key=lambda f: (_op_of(f) == 'note-add', f.name)):
        if min_age and time.time() - f.stat().st_mtime < min_age:
            continue
        try:
            item = json.loads(f.read_text(encoding='utf-8'))
            a = item['args']
            if item.get('op') not in allowed:
                raise db.HSError('op %r is not accepted here; allowed: %s' % (item.get('op'), ', '.join(allowed)))
            if item['op'] == 'ms-add':
                result = db.add_milestone(conn, date=a['date'], mainline=a['mainline'], kind=a['kind'],
                                          summary=a['summary'], editor=a['editor'], impact=a.get('impact'),
                                          exp_id=a.get('exp'), evidence=a.get('evidence'), cost=a.get('cost'),
                                          duration=a.get('duration'), sync_page=bool(a.get('sync_page')))
            elif item['op'] == 'notify':
                from . import notify
                result = notify.run(conn, a['thread'], a['editor'], a['type'], a.get('why'), vault=vault)
            elif item['op'] == 'close-report':
                from . import kinds
                from . import handover
                lint_bad = handover.close_gate(conn, vault, a.get('mainline'), a.get('editor'))
                result = {'id': kinds.add_close_report(conn, a), 'handover_lint': lint_bad}
            elif item['op'] == 'ask':  # the page is written here on the Mac; autowait opens the wait on this render
                from . import kinds
                result = {'page': kinds.write_ask(vault, a, log_change=lambda v, ed, rel, msg: db.add_change(conn, ed, rel, msg))}
            elif item['op'] == 'ms-skip':
                result = db.skip_experiment(conn, a['exp'], a['editor'])
            elif item['op'] == 'ms-supersede':
                result = db.supersede(conn, a['old'], a['new'])
            elif item['op'] == 'dec-add':
                result = db.add_decision(conn, a['mainline'], a['question'], a.get('source'), a.get('title'), a.get('option'),
                                         a.get('thread'))
            elif item['op'] == 'dec-detail':
                result = db.set_detail(conn, a['id'], a['detail'])
                from .render import log_ledger_change
                result['changelog'] = log_ledger_change(vault, 'dec-detail', result, a['editor'])
            elif item['op'] == 'dec-act':
                result = db.act_decision(conn, a['id'], a['done'], a['editor'], a.get('thread'))
                from .render import log_ledger_change
                result['changelog'] = log_ledger_change(vault, 'dec-act', result, a['editor'])
            elif item['op'] == 'dec-retitle':
                result = db.retitle_decision(conn, a['id'], a['title'], a.get('option'))
                from .render import log_ledger_change
                result['changelog'] = log_ledger_change(vault, 'dec-retitle', result, a['editor'])
            elif item['op'] == 'crit-read':
                result = db.set_reading(conn, a['id'], **{k: a.get(k) for k in db.READING_FIELDS})
                from .render import log_ledger_change
                result['changelog'] = log_ledger_change(vault, 'crit-read', result, a['editor'])
            elif item['op'] == 'nq-add':
                result = db.add_night_item(conn, mainline=a['mainline'], task=a['task'], cap_cny=a['cap'],
                                           stop_if=a['stop_if'], editor=a['editor'], deadline=a.get('deadline'),
                                           exp_id=a.get('exp'))
            elif item['op'] == 'note-add':
                db.check_closes(conn, a.get('editor'), a.get('closes'))  # before anything is written
                a = dict(a)
                a['links'], unknown = db.resolve_audit_tags(conn, a.get('editor'), a.get('links'))
                if unknown:
                    if time.time() - f.stat().st_mtime > NOTE_WAIT_SECONDS:
                        raise db.HSError('note-add 的 --links 里 %s 对应的审计排队超过 1 小时还没落盘（没有同一线程的 audit-add --tag 这个标记）'
                                         % '、'.join('@' + t for t in unknown))
                    continue  # the audit has not landed yet: the note stays queued for the next round
                db.check_note_audit_links(a['links'], a.get('exp_ids'), lambda aid: db.ledger_audit_experiment(conn, aid))
                result = {'id': db.add_task_note(conn, **{k: a.get(k) for k in db.NOTE_FIELDS})}
                result['closed_waits'] = db.close_waits_of_editor(conn, a.get('editor'), db.now(), 'note-add', vault=vault,
                                                                  closes=a.get('closes'), answered=False)
                result['still_open_waits'] = db.still_open_waits(conn, a.get('editor'))
            elif item['op'] == 'exp-note':
                result = db.set_exp_note(conn, a['code'], a['editor'], a.get('plain_name'), a.get('data_layer'))
            elif item['op'] == 'exp-upsert':
                result = exp_upsert(conn, a['id'], a.get('fields') or {}, a['editor'])
            elif item['op'] in ('idea-add', 'hyp-add'):
                result = {'id': db.add_idea(conn, 'ideas' if item['op'] == 'idea-add' else 'hypotheses',
                                            **{k: a.get(k) for k in ('mainline', 'content', 'editor', 'source_exp', 'status',
                                                                     'ra_checked', 'deploy_data') if a.get(k) is not None})}
            elif item['op'] == 'goal-set':
                result = db.set_goal(conn, mainline=a['mainline'], goal=a['goal'], criteria=a['criterion'],
                                     approved_by=a['approved_by'], evidence=a.get('evidence'))
            elif item['op'] == 'struct-set':
                from . import structure
                result = structure.set_struct(conn, vault, mainline=a['mainline'], version=a['version'],
                                              approved_by=a.get('approved_by'), evidence=a.get('evidence'),
                                              editor=a['editor'], body=a['body'], changes=a.get('changes'))
            elif item['op'] == 'handover-append':
                from . import handover
                result = handover.append(vault, a['mainline'], a['section'], a['text'], a['editor'],
                                         lambda v, ed, rel, msg: db.add_change(conn, ed, rel, msg))
                if result['status'] == 'busy':
                    continue  # the page was edited a moment ago: the item stays queued for the next run
            elif item['op'] == 'crit-set':
                result = db.set_criterion(conn, a['id'], a['status'], a.get('evidence'))
            elif item['op'] == 'budget-add':
                db.check_closes(conn, a['editor'], a.get('closes'))  # before anything is written
                result = db.add_verbal_budget(conn, id=a['id'], cap_cny=a['cap'], purpose=a['purpose'],
                                              approval_msg=a.get('approval_msg'), approval_at=a.get('approval_at'),
                                              approval_quote=a.get('approval_quote'), editor=a['editor'],
                                              expires=a.get('expires'), basis=a.get('basis'), audit=a.get('audit'),
                                              approval_reply=a.get('approval_reply'), vault=vault)
                result = {'id': result, 'closed_waits': db.close_waits_of_editor(conn, a['editor'], a.get('approval_at') or db.now(),
                                                                                  'budget-add', vault=vault, closes=a.get('closes')),
                          'still_open_waits': db.still_open_waits(conn, a['editor'])}
            elif item['op'] == 'budget-revoke':
                result = db.revoke_verbal_budget(conn, a['id'], a['editor'], a['reason'])
            elif item['op'] == 'res-set':
                result = db.set_resource(conn, a['service'], **{k: a.get(k) for k in db.RESOURCE_FIELDS})
            elif item['op'] == 'wait-add':
                result = db.add_wait(conn, a['thread'], a['ask'], a['editor'], report=a.get('report'), typed=a.get('typed'))
            elif item['op'] == 'stop-add':
                result = {'id': db.add_stop(conn, a['target'], a['reason'], a['editor'], a.get('words'))}
                db.add_change(conn, a['editor'], 'agent/管理/叫停', '叫停 %s：%s' % (a['target'], a['reason']))
            elif item['op'] == 'stop-lift':
                result = db.lift_stop(conn, a['id'], a['editor'])
                db.add_change(conn, a['editor'], 'agent/管理/叫停', '解除叫停 %s' % a['id'])
            elif item['op'] == 'mode-set':
                from . import modes
                result = modes.set_mode(conn, a['name'], a['mode'], a.get('until'))
            elif item['op'] == 'wait-done':
                db.check_wait_thread_match(conn, a.get('id'), a.get('thread'))
                result = db.close_wait(conn, a.get('id'))  # by id only; a queued thread-only item is refused
            elif item['op'] == 'thread-answer':
                from . import thread_answer
                result = thread_answer.apply(conn, vault, a['wait'], a.get('answer'), a.get('owner_msg'), bool(a.get('done')), a.get('thread'))
            elif item['op'] == 'wait-move':
                result = db.move_waits(conn, a['thread'], a.get('id'), a.get('from_thread'), a.get('report'))
                from .render import log_wait_move
                result['changelog'] = log_wait_move(vault, result, a['editor'])
            elif item['op'] == 'reply-ack':
                result = db.ack_replies(conn, a['thread'], a.get('id'))
            elif item['op'] == 'audit-add':
                from . import audits
                result = {'id': audits.add(conn, **{k: a.get(k) for k in AUDIT_ARGS})}
                if a.get('tag'):
                    db.remember_audit_tag(conn, a.get('editor'), a['tag'], result['id'])
                from . import audit_flow
                result['wake'] = audit_flow.wake_after_audit(conn, result['id'])
            elif item['op'] == 'log-change':  # a change line queued by a session that could not open the ledger
                db.add_change(conn, a['editor'], a['path'], a['summary'], at=a.get('at'))
                result = {'logged': True}
            elif item['op'] == 'audit-undo-done':
                from . import audits
                result = audits.undo_done(conn, a['id'])
            elif item['op'] == 'note-edit':
                result = db.edit_task_note(conn, a['id'], a.get('fields') or {}, a.get('reason'), a.get('editor'))
            elif item['op'] == 'audit-edit':
                from . import audits
                result = audits.edit(conn, a['id'], a.get('fields') or {}, a.get('reason'), a.get('editor'), a.get('thread'))
            os.replace(f, folder / 'done' / f.name)
            if receipts:
                (folder / 'done' / (f.name + '.result.txt')).write_text(
                    json.dumps(result, ensure_ascii=False, default=str) + '\n', encoding='utf-8')
            done.append({'file': f.name, 'result': result})
        except Exception as e:  # keep going; a bad file must not block the rest
            os.replace(f, folder / 'failed' / f.name)
            (folder / 'failed' / (f.name + '.error.txt')).write_text(repr(e), encoding='utf-8')
            failed.append({'file': f.name, 'error': str(e)})
    return {'applied': done, 'failed': failed}


def _days_ago(n):
    from datetime import timedelta
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat(timespec='seconds').replace('+00:00', 'Z')


EXPERIMENTS = SNAPSHOT.with_name('experiments.json')


def export_experiments(conn):
    """Every experiment row for remote readers (index building), long text cut at 400 characters."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='experiments'").fetchone():
        return 0
    rows = []
    for r in conn.execute('SELECT * FROM experiments').fetchall():
        rows.append({k: (v[:400] if isinstance(v, str) else v) for k, v in dict(r).items()
                     if not isinstance(v, (bytes, memoryview))})
    text = json.dumps({'exported_at': db.now(), 'rows': rows}, ensure_ascii=False, indent=1)
    if EXPERIMENTS.exists() and EXPERIMENTS.read_text(encoding='utf-8').split('\n', 2)[2:] == text.split('\n', 2)[2:]:
        return len(rows)  # unchanged apart from the export time: leave the file alone
    tmp = EXPERIMENTS.with_name('.experiments.json.tmp')
    tmp.write_text(text, encoding='utf-8')
    os.replace(tmp, EXPERIMENTS)
    return len(rows)


def reply_status(page=None, dec=None, conn=None):
    """Read-only (fallback): has the owner answered this report page or 首页 decision? From the database on
    the Mac, else from snapshot.json (at most a minute old)."""
    if page:
        page = db.check_report_name(page)
    if conn is not None:
        rows = lambda sql, *p: [dict(r) for r in conn.execute(sql, p).fetchall()]
        replies = rows('SELECT * FROM report_replies WHERE page=? ORDER BY id', page) if page else []
        wts = rows('SELECT * FROM waits WHERE report=? ORDER BY id', page) if page else []
        decs = rows('SELECT * FROM decisions WHERE id=?', dec) if dec else []
        src = 'database'
    else:
        snap = json.loads(SNAPSHOT.read_text(encoding='utf-8'))
        replies = [r for r in snap.get('report_replies', []) if r['page'] == page] if page else []
        wts = [w for w in snap.get('report_waits', []) if w.get('report') == page] if page else []
        decs = [d for d in snap.get('open_decisions', []) + snap.get('recent_decided', []) if d['id'] == dec] if dec else []
        src = 'snapshot.json %s' % snap.get('exported_at')
    out = {'source': src}
    if page:
        out.update({'page': page, 'answered': bool(replies), 'replies': replies,
                    'waits': [{k: w.get(k) for k in ('id', 'status', 'answer', 'closed_by', 'closed_at')} for w in wts]})
    if dec:
        d = decs[0] if decs else None
        out['decision'] = {k: d.get(k) for k in ('id', 'status', 'answer', 'answered_via', 'decided_at')} if d else None
    return out


def export_snapshot(conn):
    """What remote sessions need to read: pending experiments, open decisions, recent milestones, goals."""
    rows = lambda sql, *p: [dict(r) for r in conn.execute(sql, p).fetchall()]
    from . import modes
    data = {
        'exported_at': db.now(),
        'modes': modes.listing(conn),
        'observe_until': modes.until_map(conn),
        'db_check': conn.execute('PRAGMA quick_check').fetchone()[0],  # 'ok', or the first problem sqlite found
        'pending_experiments': [dict(r) for r in db.pending_experiments(conn)],
        'open_decisions': rows("SELECT * FROM decisions WHERE status='待决' ORDER BY id"),
        'recent_decided': rows("SELECT * FROM decisions WHERE status!='待决' ORDER BY decided_at DESC LIMIT 20"),
        'milestones': rows('SELECT * FROM milestones WHERE superseded_by IS NULL ORDER BY date DESC, id DESC LIMIT 60'),
        'goals': rows('SELECT * FROM goals ORDER BY mainline, version'),
        'criteria': rows('SELECT * FROM criteria ORDER BY mainline, goal_version, id'),
        'resources': rows('SELECT * FROM resources ORDER BY service'),
        'stops': rows('SELECT * FROM stops WHERE lifted_at IS NULL ORDER BY id'),
        'open_waits': rows("SELECT * FROM waits WHERE status='等待' ORDER BY id"),
        'report_waits': rows("SELECT * FROM waits WHERE report IS NOT NULL AND asked_at>=? ORDER BY id", _days_ago(7)),
        'report_replies': rows('SELECT * FROM report_replies WHERE received_at>=? ORDER BY id', _days_ago(7)),
        'task_notes': rows('SELECT * FROM task_notes WHERE created_at>=? ORDER BY id', _days_ago(3)),
        'ideas': rows('SELECT * FROM ideas WHERE created_at>=? ORDER BY id', _days_ago(30)),
        'hypotheses': rows('SELECT * FROM hypotheses WHERE created_at>=? ORDER BY id', _days_ago(30)),
        'audits': rows('SELECT * FROM audits WHERE created_at>=? OR undo_status=? ORDER BY id', _days_ago(7), '已勾改'),
        'changes': rows('SELECT * FROM changes WHERE at>=? ORDER BY id', _days_ago(3)),  # cli log-read reads this off the Mac
        'structures': rows('SELECT * FROM structures ORDER BY mainline, version'),
        'exp_notes': rows('SELECT * FROM exp_notes ORDER BY code'),
        'inbox': {'path': str(INBOX), 'status': LAST_INBOX.get('status'), 'error': LAST_INBOX.get('error')},
        'night_items': rows("SELECT * FROM night_items WHERE created_at>=? OR status IN ('待批','已批') ORDER BY id DESC",
                            _days_ago(3)),
        'verbal_budgets': rows("SELECT * FROM verbal_budgets WHERE created_at>=? OR (status='有效' AND expires_at>?) "
                               "ORDER BY created_at DESC", _days_ago(7), db.now()),
    }
    try:  # never breaks the export
        from . import unclaimed_check
        data['unclaimed'] = unclaimed_check.snapshot_rows(conn)
    except sqlite3.Error:
        data['unclaimed'] = []
    tmp = SNAPSHOT.with_name('.snapshot.json.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(tmp, SNAPSHOT)
    export_experiments(conn)
    return len(data['pending_experiments'])
