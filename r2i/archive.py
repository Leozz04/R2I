"""Report pages and the 汇报/归档/ folder. Runs inside render, before 首页 is written.

Every page in 汇报/, 汇报/待决/ and 汇报/归档/ gets the property 「更新: YYYY-MM-DD HH:MM」: the latest of the times in
its 「## 更新…」 sections (bare or dated headings), its 「[!done] 已收到」 time and its 「日期」 (else the file's mtime). 汇报.base sorts by it.
A page goes to 汇报/归档/YYYY-MM/ when its 状态 is 已回 or 已结 (the old 已看 and 已取代… count as 已结) AND 更新 is at least QUIET_HOURS (1 hour) old;
it gets 「归档于」. An old decision page in 汇报/待决/ goes the same way once its decision is no longer 待决 (page 状态 done, or the ledger says
已决/撤回 and the page is not one dec_pages still keeps): to 汇报/归档/YYYY-MM/待决/<same name>, and back to 汇报/要批/ from there. An archived page whose 更新 becomes later than 归档于 (a thread wrote another update) comes back to
汇报/ (decision pages to 汇报/要批/) and loses 归档于. Each move rewrites every `[[<old folder>/<页名>…]]` in the vault and
logs one changelog line. File names never change, so report_replies.page and waits.report still match. A page edited
in the last minute (or a note linking it) waits for the next run. Writing 更新 keeps the file's mtime."""
import hashlib
import os
import re
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import db, modes
from . import config

ARCHIVE = '归档'
DEC_DIR = '待决'
QUIET_HOURS = 1
QUIET = timedelta(hours=QUIET_HOURS)
FUTURE = timedelta(minutes=10)  # a stamp further ahead than this is ignored (UTC typos would otherwise pin pages)
EDITOR = 'r2i-sync 归档'
SKIP_DIRS = {'.obsidian', '.trash', '.git'}
SCRIPT_NOTES = {'管理/变更记录.md', '首页.md', 'agent/实验索引.md', '今晚可跑.md', 'agent/管理/今晚可跑-历史.md', '资源.md', 'agent/管理/常驻脚本.md'}  # rewritten by scripts only, so a fresh write is not someone editing
SCRIPT_PREFIXES = ('进度/', 'agent/主线/')  # 主线 links sit in the script-owned 「## 已同步」 block


def _script_note(rel):
    return rel in SCRIPT_NOTES or rel.startswith(SCRIPT_PREFIXES)
DONE = ('已回', '已结')
STAMP_RE = re.compile(r'(?<!\d)(?:(\d{4})-)?(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?!\d)')
RECEIVED_RE = re.compile(r'\[!done\][ \t]*已收到[ \t]*(?:(\d{4})-)?(\d{2})-(\d{2}) (\d{2}):(\d{2})')
DATE_RE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?')
FMT = '%Y-%m-%d %H:%M'


def _front(text):
    from .reply_watch import front
    return front(text)


def _stamp(g, year, now):
    y, mo, d, h, mi = g
    try:
        t = datetime(int(y or year), int(mo), int(d), int(h), int(mi), tzinfo=db.LOCAL_TZ)
    except ValueError:
        return None
    if not y and t > now + timedelta(days=1):  # 「12-31 23:00」 read in January
        t = t.replace(year=t.year - 1)
    return t


def _stamps(text, now):
    """Every (time, raw text) the page offers: 日期, the 「## 更新…」 sections and the 「已收到」 time."""
    props = _front(text)
    dm = DATE_RE.match(props.get('日期', ''))
    base = None
    if dm:
        y, mo, d, h, mi = dm.groups()
        base = datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), tzinfo=db.LOCAL_TZ)
    year = base.year if base else now.year
    found = [(base, props.get('日期', ''))] if base else []
    sm = DATE_RE.match(props.get('已看于', ''))  # a 同步 page: the time the owner ticked 已看 starts its hour
    if sm and sm.group(4):
        found.append((datetime(int(sm.group(1)), int(sm.group(2)), int(sm.group(3)), int(sm.group(4)), int(sm.group(5)),
                               tzinfo=db.LOCAL_TZ), props['已看于']))
    from .unregistered import update_sections  # every 「## 更新…」: bare, or dated like 「## 更新 03-05 04:25：…」
    for head, body in update_sections(text):
        for m in STAMP_RE.finditer(head + '\n' + body):
            found.append((_stamp(m.groups(), year, now), m.group(0)))
    for m in RECEIVED_RE.finditer(text):
        found.append((_stamp(m.groups(), year, now), m.group(0).split('已收到')[-1].strip()))
    return found


def future_stamps(text, now=None):
    """The raw texts of stamps more than 10 minutes ahead of now (ignored by last_update)."""
    now = now or datetime.now(db.LOCAL_TZ)
    return [raw for t, raw in _stamps(text, now) if t and t > now + FUTURE]


def last_update(text, mtime, now=None):
    """The page's latest update time (local)."""
    now = now or datetime.now(db.LOCAL_TZ)
    # a time more than 10 minutes ahead is a typo (a UTC/local slip): it would pin the page for days
    found = [t for t, _ in _stamps(text, now) if t and t <= now + FUTURE]
    return max(found) if found else datetime.fromtimestamp(mtime, db.LOCAL_TZ)


STILL_SECONDS = 2  # a page the owner just ticked is archived once it has been still this long (no 1-hour wait after a tick)
SEEN_BOX = re.compile(r'^[ \t]*[-*+] \[([xX])\][ \t]*已看', re.M)
TICK_MSG = config.say('{owner} 已勾选，马上移进归档')
SYNC_BACK_MSG = '有新结果，改成同步页从归档挪回'
TRIVIAL_WORDS = ('无新结论',)
TRIVIAL_STARTS = ('做完', '已做')
BACK_MSG = '有新问题，从归档挪回'
REOPEN_MSG = '新更新里在问你，状态改回待你看'


def reopen_needed(text, props, now):
    """A finished page (已回/已结) whose newest update section asks the owner something and is newer than their receipt
    (or 归档于): it goes back to 待你看. A plain result update with no ask never reopens. Needs a reference time."""
    if not db.report_done(props.get('状态')):
        return False
    from .unregistered import newest_section_asks, newest_update, update_sections
    if not newest_section_asks(text, now):
        return False
    nu = newest_update(update_sections(text), now)
    m = nu and STAMP_RE.search(nu['when'])
    dm = DATE_RE.match(props.get('日期', ''))
    year = int(dm.group(1)) if dm else now.year
    when = m and _stamp(m.groups(), year, now)
    refs = [_stamp(g, year, now) for g in RECEIVED_RE.findall(text)]
    try:
        refs.append(datetime.strptime(props.get('归档于', ''), FMT).replace(tzinfo=db.LOCAL_TZ))
    except ValueError:
        pass
    refs = [r for r in refs if r]
    return bool(when and refs and when <= now + FUTURE and when > max(refs))


def ticked_at(text, props, now=None):
    """The time the owner answered this page by ticking, when their tick is the page's latest event (rule A), else None.
    A 同步 page: 状态 done, 「- [x] 已看」 and 已看于. A 要批 page (incl. DEC pages): 状态 done and a 「[!done] 已收到」 receipt, which only
    reply_watch writes from their answer. A tick older than a later 「## 更新」 does not count (the page then waits QUIET_HOURS)."""
    now = now or datetime.now(db.LOCAL_TZ)
    if not db.report_done(props.get('状态')):
        return None
    dm = DATE_RE.match(props.get('日期', ''))
    year = int(dm.group(1)) if dm else now.year
    t = None
    sm = DATE_RE.match(props.get('已看于', ''))
    if sm and sm.group(4) and SEEN_BOX.search(text):
        t = datetime(int(sm.group(1)), int(sm.group(2)), int(sm.group(3)), int(sm.group(4)), int(sm.group(5)), tzinfo=db.LOCAL_TZ)
    else:
        rs = [_stamp(g, year, now) for g in RECEIVED_RE.findall(text)]
        rs = [r for r in rs if r]
        t = max(rs) if rs else None
    if not t:
        return None
    return t if last_update(text, 0, now) <= t else None


def _update_lines(nu):
    """The candidate lines of the newest update's section (heading rest first, no stamp; blank and 「>」 lines left out)."""
    head, _, body = nu['section'].partition('\n')
    rest = STAMP_RE.sub('', head, count=1).lstrip(' :：').strip()
    return [l.strip() for l in ([rest] if rest else []) + body.split('\n') if l.strip() and not l.strip().startswith('>')]


def trivial_update(nu):
    """Rule B exception: an update that says nothing new. Either the section contains 「无新结论」, or it is one line that
    starts with 「做完」/「已做」 and has no link and no number. Such an update never pops an archived page back."""
    if not nu:
        return False
    text = nu['section']
    if any(w in text for w in TRIVIAL_WORDS):
        return True
    lines = _update_lines(nu)
    if len(lines) != 1:
        return False
    l = re.sub(r'^[-*+]\s*', '', lines[0])
    return l.startswith(TRIVIAL_STARTS) and not re.search(r'\[\[|\]\(|https?://|\d', l)


def new_result_update(text, props, now):
    """An archived page whose newest 「## 更新」 is newer than the owner's receipt / 已看于 / 归档于, is not a question to them
    (reopen_needed handles those) and is not trivial: it carries a result, so the page comes back as an unseen 同步 page."""
    from .unregistered import newest_update, update_sections
    nu = newest_update(update_sections(text), now)
    if not nu or trivial_update(nu):
        return False
    m = STAMP_RE.search(nu['when'])
    dm = DATE_RE.match(props.get('日期', ''))
    year = int(dm.group(1)) if dm else now.year
    when = m and _stamp(m.groups(), year, now)
    refs = [_stamp(g, year, now) for g in RECEIVED_RE.findall(text)]
    for k in ('归档于', '已看于'):
        try:
            refs.append(datetime.strptime(props.get(k, ''), FMT).replace(tzinfo=db.LOCAL_TZ))
        except ValueError:
            pass
    refs = [r for r in refs if r]
    return bool(when and refs and when <= now + FUTURE and when > max(refs))


def to_unseen_sync(text):
    """The page as an unseen 同步 page: 类别 同步, 状态 待你看, no 已看于 / 归档于, the box 「- [ ] 已看」 unticked (added under the title)."""
    for k, v in (('类别', '同步'), ('状态', '待你看'), ('已看于', None), ('归档于', None)):
        text = set_prop(text, k, v)
    if SEEN_BOX.search(text):
        return SEEN_BOX.sub(lambda m: m.group(0).replace('[%s]' % m.group(1), '[ ]', 1), text, count=1)
    m = re.search(r'^# .+$', text, re.M)
    if not m:
        return text.rstrip('\n') + '\n\n- [ ] 已看\n'
    return text[:m.end()] + '\n\n- [ ] 已看' + text[m.end():]


def set_prop(text, key, value):
    """Set (value) or drop (None) one plain property in the front matter."""
    line = re.compile(r'^%s:.*\n?' % re.escape(key), re.M)
    if not text.startswith('---\n') or '\n---' not in text[3:]:
        return text if value is None else '---\n%s: %s\n---\n\n%s' % (key, value, text)
    end = text.index('\n---', 3) + 1
    head, rest = text[:end], text[end:]
    if value is None:
        head = line.sub('', head, count=1)
    elif line.search(head):
        head = line.sub('%s: %s\n' % (key, value), head, count=1)
    else:
        head += '%s: %s\n' % (key, value)
    return head + rest


def _write_keep_mtime(path, text):
    st = path.stat()
    path.write_text(text, encoding='utf-8')
    os.utime(path, (st.st_atime, st.st_mtime))


def _notes(vault):
    for p in Path(vault).rglob('*.md'):
        if not SKIP_DIRS.intersection(p.relative_to(vault).parts):
            yield p


def _move(vault, path, dest_dir, notes, busy, log_change, why, props, conn=None):
    """Move one page, links first. Returns the new relative path or None (postponed). A name already taken by another page
    anywhere under 汇报/ (live folders or any 归档 month) gets 「 · 2」 (then 「 · 3」 …); waits and replies follow the new name."""
    here = path.parent.relative_to(vault).as_posix()
    page = path.stem
    new_page = db.unique_stem(vault, page, exclude=path)
    old_link = re.compile(r'\[\[%s/%s(?=[\]|#^])' % (re.escape(here), re.escape(page)))
    edits = []
    for n in notes:
        if n == path:
            continue
        try:
            t = n.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            continue
        if old_link.search(t):
            edits.append((n, t))
    if any(busy(n) for n, _ in edits if not _script_note(n.relative_to(vault).as_posix())):
        return None
    target = vault / dest_dir / (new_page + '.md')
    if target.exists():
        return None  # never overwrite a page
    for n, t in edits:
        n.write_text(old_link.sub(lambda m: '[[%s/%s' % (dest_dir, new_page), t), encoding='utf-8')
    target.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding='utf-8')
    for k, v in props.items():
        text = set_prop(text, k, v)
    st = path.stat()
    path.rename(target)
    target.write_text(text, encoding='utf-8')
    os.utime(target, (st.st_atime, st.st_mtime))
    new_rel = '%s/%s' % (dest_dir, target.name)
    if new_page != page:
        db.rename_report_page(conn, page, new_page)
        why += '，重名改叫「%s」' % new_page
    if log_change:
        log_change(vault, EDITOR, new_rel, '%s（改了 %d 篇笔记里的链接）' % (why, len(edits)))
    return new_rel


# specs, drafts and code in 汇报/附件 that only archived pages still link to go to the external disk (db.slim_root()).
ATTACH_DIR = db.REPORT_DIR + '/附件'
MOVABLE_EXT = ('.md', '.py', '.txt')
SPEC_WORDS = {'spec', 'draft', 'options', 'rethink', 'review'}  # a name token that marks a spec/draft (reports and figures stay)
FOR_THREAD = '> [!note]- 给线程看'


def movable(name):
    """Specs, drafts and code, not the owner's evidence: .py always; .md/.txt only when the name says spec/draft/…; pictures never."""
    stem, ext = os.path.splitext(name)
    if ext.lower() not in MOVABLE_EXT:
        return False
    return ext.lower() == '.py' or bool(SPEC_WORDS & set(re.split(r'[-_ ]+', stem.lower())))


def attach_link_re(name):
    """Every way a note links the attachment: [[…/name]], ![[name|alias]], [text](…/name); the whole link is matched."""
    stem, ext = os.path.splitext(name)
    target = re.escape(stem) + r'(?:\.md)?' if ext.lower() == '.md' else re.escape(name)
    return re.compile(r'!?\[\[(?:[^\]|#^\n]*/)?%s(?:[#^][^\]|\n]*)?(?:\|[^\]\n]*)?\]\]|\[[^\]\n]*\]\((?:[^)\n]*/)?%s\)'
                      % (target, re.escape(name)))


def _add_thread_note(text, line):
    """Append one line to the page's 「给线程看」 folded block (made at the end when there is none)."""
    lines = text.rstrip('\n').split('\n')
    for i, l in enumerate(lines):
        if l.strip() == FOR_THREAD:
            j = i + 1
            while j < len(lines) and lines[j].startswith('>'):
                j += 1
            return '\n'.join(lines[:j] + ['> - ' + line] + lines[j:]) + '\n'
    return '\n'.join(lines + ['', FOR_THREAD, '> - ' + line]) + '\n'


def sweep_attachments(vault, notes=None, busy=None, log_change=None, now=None):
    """Move each movable attachment whose every linking note is an archived page; rewrite those links to the new path
    (inline code, plus a line in 「给线程看」), add a row to MOVES.tsv. Returns [(vault rel, new path)]. Nothing happens while the disk is not mounted."""
    vault = Path(vault)
    folder = vault / ATTACH_DIR
    if not folder.is_dir() or not db.slim_ready():
        return []
    now = now or datetime.now(db.LOCAL_TZ)
    busy = busy or (lambda p: False)
    notes = list(notes) if notes is not None else list(_notes(vault))
    arch = vault / db.REPORT_DIR / ARCHIVE
    texts = {}
    for n in notes:
        try:
            texts[n] = n.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            pass
    day = now.strftime('%Y%m%d')
    root = db.slim_root()
    out = []
    for f in sorted(folder.iterdir()):
        if not f.is_file() or not movable(f.name):
            continue
        rx = attach_link_re(f.name)
        linkers = [n for n, t in texts.items() if n != f and rx.search(t)]
        if not linkers or any(arch not in n.parents for n in linkers) or any(busy(n) for n in linkers if not _script_note(n.relative_to(vault).as_posix())):
            continue
        dest = root / '_old' / day / ATTACH_DIR / f.name      # keeps the vault-relative path
        lib_rel = str(dest)
        if dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.md5(f.read_bytes()).hexdigest()
        shutil.move(str(f), str(dest))
        for n in linkers:
            new = _add_thread_note(rx.sub('`%s`' % lib_rel, texts[n]), '附件 %s 挪出了库，现在在：`%s`' % (f.name, lib_rel))
            _write_keep_mtime(n, new)
            texts[n] = new
        moves = root / 'MOVES.tsv'
        head = '' if moves.exists() else 'time_local\tfrom_vault\tto\tmd5\n'
        with open(moves, 'a', encoding='utf-8') as m:
            m.write('%s%s\t%s\t%s\t%s\n' % (head, now.strftime(FMT), '%s/%s' % (ATTACH_DIR, f.name), lib_rel, digest))
        if log_change:
            log_change(vault, EDITOR, '%s/%s' % (ATTACH_DIR, f.name), '规格或草案只剩归档页链着，挪到 %s（改了 %d 页的链接）' % (lib_rel, len(linkers)))
        out.append(('%s/%s' % (ATTACH_DIR, f.name), lib_rel))
    return out


def due(text, mtime, now=None, closed=False, instant=False):
    """Finished and quiet for QUIET_HOURS. `closed`: the page's decision is no longer open in the ledger, so a
    decision page counts as finished even when its 状态 has not been turned yet."""
    now = now or datetime.now(db.LOCAL_TZ)
    s = _front(text).get('状态', '')
    if instant and (db.report_done(s) or closed):  # the owner ticked: no waiting (rule A)
        return True
    return (db.report_done(s) or closed) and now - last_update(text, mtime, now) >= QUIET


def settled_by_thread(conn, did):
    """The decision is 已决 in the ledger and a thread settled it itself (R-10: answered_via 线程), not the owner."""
    if conn is None or not did:
        return False
    try:
        row = conn.execute('SELECT status, answered_via FROM decisions WHERE id=?', (did,)).fetchone()
    except Exception:
        return False
    return bool(row) and row[0] == '已决' and row[1] == '线程'


def decision_state(conn, vault, did):
    """'open' / 'closed' / None (no ledger row or no ledger) for a decision page. A decision a thread settled itself
    keeps its 「改」 page while dec_pages.wanted lists it: that counts as open."""
    if conn is None or not did:
        return None
    try:
        row = conn.execute('SELECT status FROM decisions WHERE id=?', (did,)).fetchone()
        if not row:
            return None
        if row[0] == '待决':
            return 'open'
        from .dec_pages import wanted
        return 'open' if did in wanted(conn, vault) else 'closed'
    except Exception:  # an older database: fall back to the page's own 状态
        return None


def _note_future(vault, path, text, now, log_change, conn):
    """One changelog line per page per local day when a stamp on it lies in the future (probably written in UTC)."""
    raws = future_stamps(text, now)
    if not raws:
        return
    day = now.astimezone(db.LOCAL_TZ).strftime('%Y-%m-%d')
    key = 'future-stamp:' + path.stem
    if conn is not None:
        if db.meta_get(conn, key) == day:
            return
        db.meta_set(conn, key, day)
    log_change(vault, EDITOR, path.relative_to(vault).as_posix(),
               '%s：更新时间 %s 在未来，可能写成了 UTC' % (path.stem, '、'.join(dict.fromkeys(raws))))


def run(vault, busy=None, log_change=None, now=None, conn=None):
    """Returns [(old rel path, new rel path)] for the pages moved this run."""
    vault = Path(vault)
    folder = vault / db.REPORT_DIR
    now = now or datetime.now(db.LOCAL_TZ)
    busy = busy or (lambda p: False)
    if not folder.is_dir():
        return []
    m_arch, m_reo = modes.get(conn, 'archive'), modes.get(conn, 'reopen')
    if m_arch == 'off' and m_reo == 'off':
        return []
    live = db.report_pages(vault)
    archived = sorted((folder / ARCHIVE).rglob('*.md'))
    todo = []
    for path in live + archived:
        try:
            text, mtime = path.read_text(encoding='utf-8'), path.stat().st_mtime
        except (OSError, UnicodeDecodeError):
            continue
        tick = path not in archived and ticked_at(text, _front(text), now)
        if busy(path) and not (tick and time.time() - mtime >= STILL_SECONDS):
            continue  # a page the owner just ticked only needs the short still-check, not the 60-second cooldown
        from .dec_pages import dirty
        if dirty(text):
            continue  # the owner has started answering here (ticks or words not yet read): leave the page alone
        if log_change:
            _note_future(vault, path, text, now, log_change, conn)
        upd = last_update(text, mtime, now)
        stamp = upd.strftime(FMT)
        props = _front(text)
        in_archive = path in archived
        kind_dir = path.parent.parent == folder and path.parent.name in db.KIND_DIRS  # 要批/ and 同步/
        asks = (path.parent == folder or kind_dir or in_archive) and (in_archive or not props.get('待决')) and reopen_needed(text, props, now)  # live: report pages only; archived: decision pages too (D)
        reopen = asks
        rel = path.relative_to(vault).as_posix()
        if reopen and m_reo != 'auto':
            if m_reo == 'observe' and log_change:
                modes.would(conn, vault, log_change, 'reopen', rel, '状态改回待你看（%s）' % REOPEN_MSG)
            reopen = False
        new = set_prop(text, '更新', stamp) if props.get('更新') != stamp and m_arch == 'auto' else text
        if reopen and not in_archive:  # an archived page is reopened as it moves back, below
            new = set_prop(new, '状态', '待你看')
        if new != text:
            text = new
            _write_keep_mtime(path, text)
            if reopen and not in_archive and log_change:
                log_change(vault, EDITOR, path.relative_to(vault).as_posix(), REOPEN_MSG)
            props = _front(text)
        old_dec = path.parent == folder / DEC_DIR  # an old 汇报/待决/ page keeps its folder name inside the archive month
        dstate = decision_state(conn, vault, props.get('待决')) if props.get('待决') else None
        by_thread = db.report_done(props.get('状态')) and settled_by_thread(conn, props.get('待决'))  # R-10: no owner tick to wait for
        instant = (bool(tick) and dstate != 'open') or by_thread
        if not in_archive and (dstate != 'open' or by_thread) and not db.threadless_open(conn, path.stem) and due(text, mtime, now, closed=old_dec and dstate == 'closed', instant=instant):
            sub = path.parent.name if (old_dec or kind_dir) else ''  # the folder name stays inside the archive month
            dest = '%s/%s/%s' % (db.REPORT_DIR, ARCHIVE, stamp[:7]) + ('/' + sub if sub else '')
            why = TICK_MSG if instant else '%s 小时没有更新，移进归档' % QUIET_HOURS
            item = (path, dest, '%s「%s」，%s' % ('待决页状态' if old_dec else '汇报页状态', props.get('状态', ''), why), {'归档于': now.strftime(FMT)})
            if m_arch == 'auto':
                todo.append(item)
            elif m_arch == 'observe' and log_change:
                modes.would(conn, vault, log_change, 'archive', rel, '%s → %s' % (item[2], dest))
        elif in_archive:
            try:
                since = datetime.strptime(props.get('归档于', ''), FMT).replace(tzinfo=db.LOCAL_TZ)
            except ValueError:
                since = None  # archived before v2: back only when an update is newer than 24 hours
            dec_back = path.parent.name == DEC_DIR or bool(props.get('待决') and path.stem.startswith('DEC-'))  # 2.7: new decision pages live in 汇报/
            reopened = bool(props.get('待决')) and dstate == 'open' and path.parent.name in (DEC_DIR, '要批')  # the decision was reopened (撤回)
            by_hand = props.get('状态') == '待你看'  # set back by hand
            m_kind = modes.get(conn, 'kinds')
            sync_back = (not asks and not by_hand and not reopened and m_kind != 'off' and not props.get('待决')
                         and db.report_done(props.get('状态')) and new_result_update(text, props, now))
            if sync_back:  # rule B: a result update on an archived page -> an unseen 同步 page (rides the kinds mode)
                dest = '%s/同步' % db.REPORT_DIR
                if m_kind == 'auto' and m_arch == 'auto':
                    new_text = to_unseen_sync(text)
                    _write_keep_mtime(path, new_text)
                    todo.append((path, dest, SYNC_BACK_MSG, {}))
                elif m_kind == 'observe' and log_change:
                    modes.would(conn, vault, log_change, 'kinds', rel, '%s → %s（状态改待你看，去掉已看）' % (SYNC_BACK_MSG, dest))
            if asks or by_hand or reopened:  # a plain update (no question) leaves an archived page where it is
                dest = db.REPORT_DIR + ('/要批' if dec_back else '')  # 待决/ is retired, decision pages come back to 要批/
                if path.parent.name in db.KIND_DIRS:  # archived under 归档/YYYY-MM/要批|同步/: back to the same kind folder
                    dest = '%s/%s' % (db.REPORT_DIR, path.parent.name)
                back = {'归档于': None}
                if asks:
                    back['状态'] = '待你看'
                why = BACK_MSG if (asks or by_hand) else '待决已重新打开，从归档挪回'
                if m_arch == 'auto':
                    todo.append((path, dest, why, back))
                elif m_arch == 'observe' and log_change:
                    modes.would(conn, vault, log_change, 'archive', rel, '%s → %s' % (why, dest))
    today = now.strftime('%Y-%m-%d')
    sweep = m_arch == 'auto' and (bool(todo) or conn is None or db.meta_get(conn, 'attach-sweep') != today)  # also once a day: pages archived earlier
    if not todo and not sweep:
        return []
    notes = list(_notes(vault))
    moved = []
    for path, dest, why, props in todo:
        old_rel = path.relative_to(vault).as_posix()
        new_rel = _move(vault, path, dest, notes, busy, log_change, why, props, conn)
        if new_rel:
            moved.append((old_rel, new_rel))
            if props.get('状态') and log_change:
                log_change(vault, EDITOR, new_rel, REOPEN_MSG)
    if sweep and not db.slim_ready():  # never another place: skip, say so once a day, retry next round
        day = now.strftime('%Y-%m-%d')
        if log_change and (conn is None or db.meta_get(conn, 'attach-skip') != day):
            log_change(vault, EDITOR, ATTACH_DIR, db.slim_skip_line('汇报/附件 里的规格和代码'))
            if conn is not None:
                db.meta_set(conn, 'attach-skip', day)
    elif sweep:
        sweep_attachments(vault, list(_notes(vault)), busy, log_change, now)
        if conn is not None:
            db.meta_set(conn, 'attach-sweep', today)
    return moved
