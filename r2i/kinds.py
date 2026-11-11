"""汇报/ pages in two kinds . Runs inside render
just before archive.py.

The page property 类别: 要批|同步 decides the folder: 汇报/要批/ (the owner decides something; waits, ticks, 回复) or 汇报/同步/
(a result the owner should know; one box 「- [ ] 已看」). A page in the 汇报/ root, in the retired 汇报/待决/ or in the wrong
folder moves to the folder of its 类别; a page without 类别 gets one inferred from 类型 (要你定 / 要你做 / 已按推荐做 /
结果和要你定 / 待决 / 叫停报告 -> 要批; 结果 / 只看 -> 同步; a page with 待决: DEC-NN is 要批) written into the front matter
first. Each move rewrites every `[[old folder/页名…]]` in the vault (archive._move), a name already taken anywhere under 汇报/
(live folders and every 归档 month) gets 「 · 2」, and the waits and replies that name the page follow. A 要批 page of 类型
结果和要你定 (or 带结论: 是) that the owner has answered turns into a 同步 page, marked 已看 by the script (状态 已看, 已看于, the box
ticked): it is archived like any seen 同步 page, one hour after 已看于.

Mode 'kinds' (modes.py; default observe, R-52): observe only logs 「本来会挪到 …」 (once per page per day), auto does it, off
does nothing. The 已看 read-back (reply_watch) and the archive rules do not depend on it.

Also here: the 同步 skeleton page that `ms-add --sync-page` asks for, the list of unseen 同步 pages for 首页's 「## 新同步」,
and the link lines that keep a seen (archived) 同步 page findable: yesterday's in the progress page's 「## 昨天」 and a
script-owned block 「## 已同步」 at the end of each 主线笔记 (rendered from the vault, so it is idempotent)."""
import hashlib
import re
import shutil
from datetime import datetime
from pathlib import Path

from . import archive, db, modes

EDITOR = 'r2i-sync 分类'
SEEN_BOX = re.compile(r'^[ \t]*[-*+] \[([ xX])\][ \t]*已看', re.M)
BATCH_TYPES = ('要你定', '要你做', '已按推荐做', '结果和要你定', '待决', '叫停报告')
SYNC_TYPES = ('结果', '只看')
MIXED_TYPE = '结果和要你定'
LAST_SYNCED = 20
NOTE_START = '<!-- r2i-sync:synced:start 本段由脚本生成，手改会被覆盖 -->'
NOTE_END = '<!-- r2i-sync:synced:end -->'
DATE_PREFIX = re.compile(r'^\d{4}-\d{2}-\d{2} ')
HOME_ROWS = 5
BASE_LINK = '[[%s/汇报.base|全部]]' % db.REPORT_DIR
BAD = re.compile(r'[\\/:*?"<>|#^\[\]]')


def _front(text):
    from .reply_watch import front
    return front(text)


def kind_of(props):
    """(类别, inferred): the property when it is 要批 or 同步, else inferred from 待决 / 类型; (None, False) when neither says."""
    k = (props.get('类别') or '').strip()
    if k in db.KIND_DIRS:
        return k, False
    if props.get('待决'):
        return '要批', True
    t = (props.get('类型') or '').strip()
    if t in BATCH_TYPES:
        return '要批', True
    if t in SYNC_TYPES:
        return '同步', True
    return None, False


def is_mixed(props):
    """A 要批 page that also carries a conclusion; a small DEC page (待决: DEC-NN) never does."""
    if props.get('待决'):
        return False
    return (props.get('类型') or '').strip() == MIXED_TYPE or (props.get('带结论') or '').strip() == '是'


def title_of(stem):
    return DATE_PREFIX.sub('', stem)


def _read(path):
    try:
        return path.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        return None


def _would(conn, vault, log_change, rel, text):
    """Observe mode: 「本来会挪到 …」, once per page and text per local day."""
    if not log_change:
        return
    day = datetime.now(db.LOCAL_TZ).strftime('%Y-%m-%d')
    key = 'would-kinds:' + rel + '|' + text
    if conn is not None:
        if db.meta_get(conn, key) == day:
            return
        db.meta_set(conn, key, day)
    log_change(vault, modes.EDITOR, rel, '本来会%s（kinds 在 observe）' % text)


def to_seen(text, stamp):
    """The page as a seen 同步 page: 类别 同步, 状态 已看, 已看于, and the box 「已看」 ticked (added under the title when missing)."""
    text = archive.set_prop(text, '类别', '同步')
    text = archive.set_prop(text, '状态', '已看')
    text = archive.set_prop(text, '已看于', stamp)
    if SEEN_BOX.search(text):
        return SEEN_BOX.sub(lambda m: m.group(0).replace('[%s]' % m.group(1), '[x]', 1), text, count=1)
    m = re.search(r'^# .+$', text, re.M)
    if not m:
        return text.rstrip('\n') + '\n\n- [x] 已看\n'
    return text[:m.end()] + '\n\n- [x] 已看' + text[m.end():]


STUB_MAX_LINES = 15
STUB_PROPS = ('类别', '类型', '状态')
UPDATE_HEAD = re.compile(r'(?m)^## 更新(?=[ \t:：（(]|$).*$')


def _body_bullets(text):
    t = text
    if t.startswith('---\n') and '\n---' in t[3:]:
        t = t.split('\n---', 1)[1]
    return [l.rstrip() for l in t.split('\n') if re.match(r'^\s*[-*+] \S', l)]


def _is_stub(text):
    props = _front(text)
    return len(text.rstrip('\n').split('\n')) <= STUB_MAX_LINES and not any(props.get(k) for k in STUB_PROPS)


def _add_updates(text, lines):
    """Add bullet lines to the page's 「## 更新」. Newest on top. A page with 「## 更新（新的在上）」, or
    with no 更新 yet, gets them right under that heading (the section is made above the page's first 「## 」, under the
    summary). A page that still has only old bottom-up 「## 更新」 sections keeps appending at the end of the last one."""
    from .unregistered import NEWEST_FIRST
    head = '## 更新' + NEWEST_FIRST
    i = text.find('\n' + head)
    if i >= 0:
        j = text.index('\n', i + 1) if '\n' in text[i + 1:] else len(text)
        rest = text[j:].lstrip('\n')
        return text[:j] + '\n\n' + '\n'.join(lines) + '\n' + ('' if rest.startswith(('- ', '* ', '+ ')) or not rest else '\n') + rest
    heads = list(UPDATE_HEAD.finditer(text))
    if not heads:
        start = 0
        if text.startswith('---\n') and '\n---' in text[3:]:
            start = text.index('\n---', 3) + 4
        nxt = re.search(r'(?m)^## ', text[start:])
        block = head + '\n\n' + '\n'.join(lines) + '\n'
        if not nxt:
            return text.rstrip('\n') + '\n\n' + block
        at = start + nxt.start()
        return text[:at] + block + '\n' + text[at:]
    end = heads[-1].end()
    nxt = re.search(r'(?m)^## ', text[end:])
    stop = end + nxt.start() if nxt else len(text)
    body = text[:stop].rstrip('\n')
    return body + '\n' + '\n'.join(lines) + '\n' + (('\n' + text[stop:]) if nxt else '')


def _trash_path(vault, stem):
    d = Path(vault) / '.trash'
    d.mkdir(parents=True, exist_ok=True)
    p = d / ('%s（残片）.md' % stem)
    n = 2
    while p.exists():
        p = d / ('%s（残片） %d.md' % (stem, n))
        n += 1
    return p


def stray_stubs(conn, vault, busy=None, log_change=None):
    """A thread appended 「## 更新」 to a page's OLD path after the script moved the page: a small file with only 更新 lines
    sits there while the real page (same stem) lives elsewhere under 汇报/. Merge its bullet lines into the real page's
    「## 更新」 and move the stub to <vault>/.trash. observe only logs. Returns [stub rel]."""
    vault = Path(vault)
    base = vault / db.REPORT_DIR
    mode = modes.get(conn, 'kinds')
    if mode != 'auto' and mode != 'observe' or not base.is_dir():
        return []
    busy = busy or (lambda p: False)
    stems = db.all_report_stems(vault)
    done = []
    for stem, paths in sorted(stems.items()):
        if len(paths) < 2:
            continue
        texts = {p: _read(p) for p in paths}
        stubs = [p for p in paths if texts[p] is not None and _is_stub(texts[p])]
        reals = [p for p in paths if texts[p] is not None and p not in stubs]
        if not reals:
            continue
        real = reals[0]
        for stub in stubs:
            if busy(stub) or busy(real):
                continue
            rel = stub.relative_to(vault).as_posix()
            real_text = _read(real)
            have = {l.strip() for l in real_text.split('\n')}
            add = [l for l in _body_bullets(texts[stub]) if l.strip() not in have]
            what = '把挪走后留在旧位置的残片合并进 %s（补 %d 行更新），残片移到 .trash' % (real.relative_to(vault).as_posix(), len(add))
            if mode == 'observe':
                _would(conn, vault, log_change, rel, what)
                continue
            if add:
                real.write_text(_add_updates(real_text, add), encoding='utf-8')
            shutil.move(str(stub), str(_trash_path(vault, stem)))
            if log_change:
                log_change(vault, EDITOR, rel, what)
            done.append(rel)
    return done


def run(conn, vault, busy=None, log_change=None, now=None):
    """Convert answered mixed pages and move every live page to its 类别 folder. Returns [(old rel, new rel)] moved."""
    vault = Path(vault)
    folder = vault / db.REPORT_DIR
    now = now or datetime.now(db.LOCAL_TZ)
    busy = busy or (lambda p: False)
    mode = modes.get(conn, 'kinds')
    if mode == 'off' or not folder.is_dir():
        return []
    try:
        stray_stubs(conn, vault, busy, log_change)
    except OSError:
        pass
    from .dec_pages import dirty
    todo = []
    for path in db.report_pages(vault):
        if busy(path):
            continue
        text = _read(path)
        if text is None or dirty(text):
            continue  # the owner is answering here: leave the page alone
        props = _front(text)
        kind, _ = kind_of(props)
        if kind is None:
            continue
        rel = path.relative_to(vault).as_posix()
        convert = kind == '要批' and is_mixed(props) and db.report_done(props.get('状态'))
        if convert:
            kind = '同步'
        dest = '%s/%s' % (db.REPORT_DIR, kind)
        here = path.parent == vault / dest
        if here and not convert and props.get('类别') == kind:
            continue
        if mode == 'observe':
            what = '挪到 %s/，写 类别: %s' % (dest, kind) if not here else '在原地写 类别: %s' % kind
            if convert:
                what = '把答完的「结论＋要批」页转成同步页（已看），' + what
            _would(conn, vault, log_change, rel, what)
            continue
        if convert:
            stamp = min(archive.last_update(text, path.stat().st_mtime, now), now).strftime(archive.FMT)
            text = to_seen(text, stamp)
            archive._write_keep_mtime(path, text)
            ms = props.get('里程碑') or ''
            db.add_seen(conn, path.stem, ms if ms.startswith('MS-') else None)
            if log_change:
                log_change(vault, EDITOR, rel, '答完的结论＋要批页转成同步页，标为已看')
        elif props.get('类别') != kind:
            archive._write_keep_mtime(path, archive.set_prop(text, '类别', kind))
        if not here:
            todo.append((path, dest, '类别「%s」，挪到 %s/' % (kind, dest)))
    if not todo:
        return []
    notes = list(archive._notes(vault))
    moved = []
    for path, dest, why in todo:
        old = path.relative_to(vault).as_posix()
        new = archive._move(vault, path, dest, notes, busy, log_change, why, {}, conn)
        if new:
            moved.append((old, new))
    return moved


# ---- 同步 pages for the readers: 首页, 进度, 主线笔记 ----

def unseen_sync(vault):
    """Live 同步 pages the owner has not seen: [{'rel', 'stem', 'title', 'mainline', 'date'}], newest 日期 first, then name."""
    out = []
    for path in db.report_pages(vault):
        text = _read(path)
        if text is None:
            continue
        props = _front(text)
        if kind_of(props)[0] != '同步' or db.report_done(props.get('状态')):
            continue
        out.append({'rel': path.relative_to(vault).with_suffix('').as_posix(), 'stem': path.stem, 'title': title_of(path.stem),
                    'mainline': props.get('主线') or '', 'date': props.get('日期') or ''})
    out.sort(key=lambda r: r['stem'])
    out.sort(key=lambda r: r['date'], reverse=True)
    return out


def home_block(vault, md):
    """首页's 「## 新同步」: up to 5 lines `- <结论> · <主线> · [[页|去看]]`, a 6th 「共 N 篇没看 → 全部」 when there are more,
    the fixed line 「新同步：没有」 when none. No times: unchanged pages give identical bytes."""
    rows = unseen_sync(vault)
    out = ['## 新同步', '']
    if not rows:
        return out + ['新同步：没有']
    for r in rows[:HOME_ROWS]:
        ml = db.SHORT_NAMES.get(r['mainline'], r['mainline'])
        out.append('- %s%s · [[%s|去看]]' % (md(r['title']), ' · ' + md(ml) if ml else '', r['rel']))
    if len(rows) > HOME_ROWS:
        out.append('共 %d 篇没看 → %s' % (len(rows), BASE_LINK))
    return out


def seen_pages(vault, mainline=None):
    """Seen 同步 pages, live or archived: [{'rel', 'stem', 'title', 'mainline', 'seen'}], newest 已看于 first.
    `seen` is the 已看于 time (else 日期) as text."""
    base = Path(vault) / db.REPORT_DIR
    paths = list(db.report_pages(vault)) + (sorted((base / archive.ARCHIVE).rglob('*.md')) if (base / archive.ARCHIVE).is_dir() else [])
    out = []
    for path in paths:
        text = _read(path)
        if text is None:
            continue
        props = _front(text)
        if kind_of(props)[0] != '同步' or not db.report_done(props.get('状态')) or (mainline and props.get('主线') != mainline):
            continue
        out.append({'rel': path.relative_to(vault).with_suffix('').as_posix(), 'stem': path.stem, 'title': title_of(path.stem),
                    'mainline': props.get('主线') or '', 'seen': props.get('已看于') or props.get('日期') or ''})
    out.sort(key=lambda r: r['stem'])
    out.sort(key=lambda r: r['seen'], reverse=True)
    return out


def yesterday_lines(vault, ml, day, md):
    """Link lines for the progress page's 「## 昨天」: the mainline's 同步 pages ticked 已看 on `day` (YYYY-MM-DD)."""
    return ['- 同步 · %s · [[%s|去看]]' % (md(r['title']), r['rel']) for r in seen_pages(vault, ml) if r['seen'][:10] == day]


def sync_notes(conn, vault, busy=None, log_change=None):
    """Keep a script-owned block 「## 已同步」 at the end of each 主线笔记 (one link line per seen 同步 page, newest first, at most
    20). Written only in mode auto and only when the text changes; a note someone is editing waits for the next run."""
    mode = modes.get(conn, 'kinds')
    vault = Path(vault)
    busy = busy or (lambda p: False)
    done = []
    if mode == 'off':
        return done
    from .render import _md
    for ml in db.MAINLINES:
        path = vault / db.NOTE_REL[ml]
        text = _read(path) if path.is_file() else None
        rows = seen_pages(vault, ml)[:LAST_SYNCED]
        if text is None or (not rows and NOTE_START not in text):
            continue
        body = ['## 已同步', '', '看过的同步页，新的在上；页归档后链接照样能点。', ''] + [
            '- %s · [[%s|%s]]' % (r['seen'][5:10] if r['seen'] else '—', r['rel'], _md(r['title'])) for r in rows]
        block = NOTE_START + '\n' + '\n'.join(body) + '\n' + NOTE_END
        if NOTE_START in text and NOTE_END in text:
            pre, rest = text.split(NOTE_START, 1)
            new = pre + block + rest.split(NOTE_END, 1)[1]
        else:
            new = text.rstrip('\n') + '\n\n' + block + '\n'
        if new == text:
            continue
        rel = path.relative_to(vault).as_posix()
        if mode == 'observe':
            _would(conn, vault, log_change, rel, '在主线笔记末尾写「已同步」块（%d 行）' % len(rows))
            continue
        if busy(path):
            continue
        archive._write_keep_mtime(path, new)
        if log_change:
            log_change(vault, EDITOR, rel, '主线笔记的「已同步」块更新（%d 行）' % len(rows))
        done.append(rel)
    return done


# ---- ms-add --sync-page ----

def skeleton(m, now):
    """A 同步 page from a milestone, shaped like agent/模板/同步模板.md; the asking thread writes the body."""
    from .render import _md
    code = ' '.join((m['exp_id'] or '').split())
    out = ['---', '类别: 同步', '种类: ' + (m['kind'] if m['kind'] in ('正结果', '负结果', '版本变更', '实战教训') else '发现'),
           '主线: ' + m['mainline'], '状态: 待你看', '日期: ' + now.strftime(archive.FMT), '来自: ' + m['editor'],
           '里程碑: ' + m['id']]
    if code:
        out.append('代号: [%s]' % code)
    if m['cost']:
        out.append('花费: ' + m['cost'])
    if m['duration']:
        out.append('用时: ' + m['duration'])
    out += ['cssclasses: [reading]', '---', '', '# ' + _md(m['summary']), '', '- [ ] 已看', '',
            '> [!summary] 一句话', '> **%s**' % _md(m['summary']), '']
    if m['impact']:
        out += ['**改变了什么**：' + _md(m['impact']), '']
    out += ['## 结果', '', '（骨架由 ms-add --sync-page 生成，提出线程补正文。）', '']
    if m['evidence']:
        out += ['## 详情', '', '- ' + _md(m['evidence']), '']
    return '\n'.join(out)


def write_skeletons(conn, vault, log_change=None, now=None):
    """One skeleton page in 汇报/同步/ per milestone added with --sync-page (mode auto; observe logs; off waits). A milestone
    whose 里程碑 page exists already (live or archived) is only marked done. Returns the relative paths written."""
    mode = modes.get(conn, 'kinds')
    vault = Path(vault)
    now = now or datetime.now(db.LOCAL_TZ)
    out = []
    if mode == 'off':
        return out
    try:
        rows = conn.execute("SELECT * FROM milestones WHERE sync_page='1' AND superseded_by IS NULL ORDER BY id").fetchall()
    except Exception:
        return out
    if not rows:
        return out
    have = set()
    base = vault / db.REPORT_DIR
    for path in (base.rglob('*.md') if base.is_dir() else []):
        t = _read(path)
        if t and '里程碑:' in t:
            have.add(_front(t).get('里程碑'))
    for m in rows:
        if m['id'] in have:
            if mode == 'auto':
                conn.execute("UPDATE milestones SET sync_page='done' WHERE id=?", (m['id'],)); conn.commit()
            continue
        stem = '%s %s' % (m['date'], BAD.sub(' ', m['summary']).strip()[:40].strip())
        if mode == 'observe':
            _would(conn, vault, log_change, '%s/同步/%s.md' % (db.REPORT_DIR, stem), '为 %s 写一页同步骨架' % m['id'])
            continue
        stem = db.unique_stem(vault, stem)
        path = vault / db.REPORT_DIR / '同步' / (stem + '.md')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(skeleton(m, now), encoding='utf-8')
        conn.execute("UPDATE milestones SET sync_page='done' WHERE id=?", (m['id'],)); conn.commit()
        rel = path.relative_to(vault).as_posix()
        out.append(rel)
        if log_change:
            log_change(vault, EDITOR, rel, '%s 的同步页骨架（ms-add --sync-page），提出线程补正文' % m['id'])
    return out


# ---- close-report (结档汇报) ----

CLOSE_KIND = '结档'
CLOSE_MAX_LINES = 5
CLOSE_PREFIX = 'close-report:'
URL_RE = re.compile(r'^https?://\S+$')
EXP_RE = re.compile(r'^[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)+$')


def check_close_report(a):
    """Validate the arguments of `cli close-report` (raises db.HSError). Returns the cleaned dict."""
    title = ' '.join((a.get('title') or '').split())
    summary = ' '.join((a.get('summary') or '').split())
    what = [l.strip() for l in (a.get('what') or '').split('\n') if l.strip()]
    if not title or not summary or not what:
        raise db.HSError('close-report needs --title, --summary and --what')
    if len(what) > CLOSE_MAX_LINES:
        raise db.HSError('--what is at most %d lines (got %d)' % (CLOSE_MAX_LINES, len(what)))
    db._check(a.get('mainline'), db.MAINLINES, 'mainline')
    if not (a.get('editor') or '').strip():
        raise db.HSError('--editor is required')
    if not title.endswith(CLOSE_KIND):
        title += ' ' + CLOSE_KIND
    if BAD.search(title):
        raise db.HSError('--title must not contain \\ / : * ? " < > | # ^ [ ]')
    return {'title': title, 'summary': summary, 'what': what, 'mainline': a['mainline'], 'editor': a['editor'].strip(),
            'thread': (a.get('thread') or '').strip() or None, 'links': [' '.join(str(x).split()) for x in (a.get('link') or []) if str(x).strip()]}


def add_close_report(conn, a, now=None):
    """Store one close-out report for render to write (same path as --sync-page: the host owns the vault). Returns its id."""
    import secrets
    c = check_close_report(a)
    now = now or datetime.now(db.LOCAL_TZ)
    c['date'] = now.strftime('%Y-%m-%d')
    c['at'] = now.strftime(archive.FMT)
    rid = 'CR-%s-%s' % (now.strftime('%Y%m%d'), secrets.token_hex(2))
    c['state'] = 'new'
    db.meta_set(conn, CLOSE_PREFIX + rid, c)
    return rid


def _index_codes(vault):
    p = Path(vault) / 'agent/实验索引.md'
    t = _read(p) if p.is_file() else None
    return t or ''


ATTACH_EXT = ('.md', '.png', '.jpg', '.jpeg', '.svg', '.pdf', '.json', '.csv', '.txt', '.tsv', '.zip')
ATTACH_MAX = 2 * 1024 * 1024  # bigger files and directories stay path text
ATTACH_MAX_ZIP = 20 * 1024 * 1024  # zips (check bundles) too; internal disk is short, bigger ones stay path text


def attach_max(path):
    return ATTACH_MAX_ZIP if Path(path).suffix.lower() == '.zip' else ATTACH_MAX


def attach_copy(vault, src):
    """A report file outside the vault is copied into 汇报/附件 so the page can link it. Returns the vault-relative
    path, or None (not a small report file). Same name + same bytes reuses the copy; same name + other bytes gets -<sha8>."""
    try:
        src = Path(src)
        if not vault or not src.is_absolute() or src.suffix.lower() not in ATTACH_EXT or not src.is_file() \
                or Path(vault).resolve() in src.resolve().parents or src.stat().st_size > attach_max(src):
            return None
        data = src.read_bytes()
    except OSError:
        return None
    folder = Path(vault) / archive.ATTACH_DIR
    name = BAD.sub('-', src.name)
    dest = folder / name
    if dest.is_file() and dest.read_bytes() != data:
        dest = folder / ('%s-%s%s' % (Path(name).stem, hashlib.sha256(data).hexdigest()[:8], Path(name).suffix))
    if not (dest.is_file() and dest.read_bytes() == data):
        folder.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name('.' + dest.name + '.tmp')
        tmp.write_bytes(data)
        tmp.replace(dest)
    return dest.relative_to(vault).as_posix()


def attach_line(vault, src):
    """「- [[汇报/附件/x|x]]」 (images embedded) plus a 原路径 sub-line, or None when src is not copied."""
    rel = attach_copy(vault, src)
    if not rel:
        return None
    p = Path(rel)
    target, bang = (rel[:-3], '') if p.suffix.lower() == '.md' else (rel, '!' if p.suffix.lower() in ('.png', '.jpg', '.jpeg', '.svg') else '')
    return '- %s[[%s|%s]]\n  - 原路径 `%s`' % (bang, target, p.name, src)


def relink_page(vault, page):
    """Backfill: turn bare 「- /abs/path」 lines of one 同步 page into attachment links (the rest of the page is untouched)."""
    page = Path(page)
    text = page.read_text(encoding='utf-8')
    new = re.sub(r'^- (/\S+)$', lambda m: attach_line(vault, m.group(1)) or m.group(0), text, flags=re.M)
    if new != text:
        page.write_text(new, encoding='utf-8')
    return new != text


def close_links(links, index_text, vault=None):
    """(lines for 「## 去哪看」, lines for the folded 「给线程看」). A code like XY-20 is inline code, plus a link to 实验索引 when that
    page lists the code; AUD-… is inline code; a URL is a markdown link; lib: paths cannot be opened from Obsidian and go in the folded block."""
    from .render import _md
    shown, thread = [], []
    for l in links:
        if l.startswith('lib:'):
            thread.append('`%s`' % l)
        elif URL_RE.match(l):
            shown.append('- [%s](%s)' % (re.sub(r'^https?://(www\.)?', '', l).split('/')[0], l))
        elif l.startswith('[['):
            shown.append('- ' + l)
        elif l.startswith('/') and (att := attach_line(vault, l)):
            shown.append(att)
        elif EXP_RE.match(l):
            shown.append('- `%s`%s' % (l, ' · [[实验索引|在实验索引里]]' if ('`%s`' % l) in index_text else ''))
        else:
            shown.append('- ' + _md(l))
    return shown, thread


def close_page(c, index_text='', vault=None):
    from .render import _md
    shown, thread = close_links(c['links'], index_text, vault)
    out = ['---', '类别: 同步', '种类: ' + CLOSE_KIND, '主线: ' + c['mainline'], '状态: 待你看', '日期: ' + c['at'], '来自: ' + c['editor']]
    if c.get('thread'):
        out.append('线程: ' + c['thread'])
    out += ['cssclasses: [reading]', '---', '', '# ' + _md(c['title']), '', '- [ ] 已看', '',
            '> [!summary] 一句话', '> **%s**' % _md(c['summary']), '', '## 做了什么', ''] + [_md(l) for l in c['what']] + ['']
    out += ['## 去哪看', ''] + (shown or ['- （没有链接）']) + ['']
    if thread:
        out += ['> [!note]- 给线程看'] + ['> - ' + t for t in thread] + ['']
    return '\n'.join(out)


def write_close_reports(conn, vault, log_change=None, now=None):
    """One page in 汇报/同步/ per stored close-report (mode kinds: auto writes; observe only logs 「本来会…」; off waits).
    Returns the relative paths written. The page shows in 首页's 「## 新同步」 like any unseen 同步 page."""
    mode = modes.get(conn, 'kinds')
    vault = Path(vault)
    now = now or datetime.now(db.LOCAL_TZ)
    out = []
    if mode == 'off':
        return out
    try:
        rows = conn.execute("SELECT key FROM meta WHERE key LIKE ? ORDER BY key", (CLOSE_PREFIX + '%',)).fetchall()
    except Exception:
        return out
    index_text = _index_codes(vault)
    for r in rows:
        c = db.meta_get(conn, r['key'])
        if not isinstance(c, dict) or c.get('state') != 'new':
            continue
        stem = '%s %s' % (c['date'], BAD.sub(' ', c['title']).strip())
        if mode == 'observe':
            _would(conn, vault, log_change, '%s/同步/%s.md' % (db.REPORT_DIR, stem), '写一页结档汇报「%s」' % c['title'])
            continue
        stem = db.unique_stem(vault, stem)
        path = vault / db.REPORT_DIR / '同步' / (stem + '.md')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(close_page(c, index_text, vault), encoding='utf-8')
        c['state'] = 'done'; c['page'] = stem
        db.meta_set(conn, r['key'], c)
        rel = path.relative_to(vault).as_posix()
        out.append(rel)
        if log_change:
            log_change(vault, EDITOR, rel, '结档汇报（close-report，%s）' % c['editor'])
    return out



# ---- note-add --sync 是 ----

NOTE_KIND = '发现'
SPLIT_LINKS = re.compile(r'[\n,，;；]+')


def _codes(raw):
    return [c for c in re.split(r'[\s,，;；、\[\]]+', raw or '') if c]


def _note_links(raw):
    out = []
    for part in SPLIT_LINKS.split(raw or ''):
        part = part.strip()
        if not part:
            continue
        out += [part] if part.startswith('[[') else part.split()
    return out


def note_page(n, now, index_text='', vault=None):
    """A 同步 page from one task note, shaped like agent/模板/同步模板.md (title, did, result, meaning, limits, links)."""
    from .render import _md
    codes = _codes(n['exp_ids'])
    out = ['---', '类别: 同步', '种类: ' + NOTE_KIND, '主线: ' + n['mainline'], '状态: 待你看', '日期: ' + now.strftime(archive.FMT),
           '来自: ' + ' '.join(str(n['editor'] or '').split()), '记录: ' + n['id']]
    if codes:
        out.append('代号: [%s]' % ', '.join(codes))
    if n['cost']:
        out.append('花费: ' + ' '.join(n['cost'].split()))
    if n['duration']:
        out.append('用时: ' + ' '.join(n['duration'].split()))
    one = lambda t: _md(' '.join(str(t or '').split()))
    out += ['cssclasses: [reading]', '---', '', '# ' + one(n['title']), '', '- [ ] 已看', '',
            '> [!summary] 一句话', '> **%s**' % one(n['result']), '',
            '**改变了什么**：' + one(n['meaning']), '', '## 做了什么', '', one(n['did']), '', '## 结果', '', one(n['result']), '']
    if (n['limits'] or '').strip():
        out += ['**限制**：' + one(n['limits']), '']
    shown, thread = close_links(_note_links(n['links']), index_text, vault)
    out += ['## 详情', ''] + (shown or ['- （没有链接）']) + ['- 任务记录 `%s`' % n['id'], '']
    if thread:
        out += ['> [!note]- 给线程看'] + ['> - ' + t for t in thread] + ['']
    return '\n'.join(out)


def _sync_pages_by_code(vault):
    """{experiment code: newest 同步 page Path} over 汇报/ (live folders and every 归档 month, not 附件), from 代号 in the front matter."""
    base = Path(vault) / db.REPORT_DIR
    found = {}
    for path in (base.rglob('*.md') if base.is_dir() else []):
        if '附件' in path.relative_to(base).parts:
            continue
        t = _read(path)
        if not t or '代号:' not in t:
            continue
        props = _front(t)
        if kind_of(props)[0] != '同步':
            continue
        key = (props.get('日期') or '', path.name)
        for c in _codes(props.get('代号')):
            if c not in found or key > found[c][0]:
                found[c] = (key, path)
    return {c: p for c, (_, p) in found.items()}


def note_update_line(n, now):
    from .render import _md
    line = '- %s %s：%s（任务记录 %s）' % (now.strftime('%m-%d %H:%M'), ' '.join(str(n['title']).split()),
                                         ' '.join(str(n['result']).split()), n['id'])
    return _md(line)


def write_note_syncs(conn, vault, log_change=None, now=None):
    """One 同步 page per task note with sync='1' (note-add --sync 是, or the 推进-thread default). When one of the note's
    exp-ids already has a 同步 page (live or archived), the note becomes a line on top of that page's 「## 更新」 instead;
    archive.run then brings an archived page back as unseen. Mode kinds: auto writes; observe only logs; off waits."""
    mode = modes.get(conn, 'kinds')
    vault = Path(vault)
    now = now or datetime.now(db.LOCAL_TZ)
    out = []
    if mode == 'off':
        return out
    try:
        rows = conn.execute("SELECT * FROM task_notes WHERE sync='1' ORDER BY created_at, id").fetchall()
    except Exception:
        return out
    if not rows:
        return out
    by_code = _sync_pages_by_code(vault)
    index_text = _index_codes(vault)
    for n in rows:
        page = next((by_code[c] for c in _codes(n['exp_ids']) if c in by_code), None)
        if page is not None:
            rel = page.relative_to(vault).as_posix()
            text = _read(page) or ''
            if ('任务记录 %s）' % n['id']) in text:  # already there (a crash between write and mark)
                conn.execute("UPDATE task_notes SET sync='done' WHERE id=?", (n['id'],)); conn.commit()
                continue
            if mode == 'observe':
                _would(conn, vault, log_change, rel, '在同步页「## 更新」加一条 %s' % n['id'])
                continue
            page.write_text(_add_updates(text, [note_update_line(n, now)]), encoding='utf-8')
            conn.execute("UPDATE task_notes SET sync='done' WHERE id=?", (n['id'],)); conn.commit()
            out.append(rel)
            if log_change:
                log_change(vault, EDITOR, rel, '%s 的新结果加进同步页「## 更新」（note-add --sync）' % n['id'])
            continue
        stem = '%s %s' % (n['date'], BAD.sub(' ', ' '.join(str(n['title']).split())).strip()[:60].strip())
        if mode == 'observe':
            _would(conn, vault, log_change, '%s/同步/%s.md' % (db.REPORT_DIR, stem), '为 %s 写一页同步页' % n['id'])
            continue
        stem = db.unique_stem(vault, stem)
        path = vault / db.REPORT_DIR / '同步' / (stem + '.md')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(note_page(n, now, index_text, vault), encoding='utf-8')
        conn.execute("UPDATE task_notes SET sync='done' WHERE id=?", (n['id'],)); conn.commit()
        for c in _codes(n['exp_ids']):  # a second note of the same experiment in this pass updates this page
            by_code.setdefault(c, path)
        rel = path.relative_to(vault).as_posix()
        out.append(rel)
        if log_change:
            log_change(vault, EDITOR, rel, '%s 的同步页（note-add --sync，%s）' % (n['id'], ' '.join(str(n['editor'] or '').split())))
    return out

ASK_EDITOR = '线程'
ANSWER_HEAD = '### 我的回答'
ANSWER_HINT = '> 在下面写你的回答和判断，可以多行；有编号的就写在编号后面。写完勾「发出」才会送出（只勾上面的选项、不写字也行）。'
ITEM_RE = re.compile(r'^(?:#{2,4}[ \t]+((?:[A-Za-z]{1,2})?\d{1,2})(?=[ \t　·:：]|$)|[ \t]*(\d{1,2})[.、)）][ \t])', re.M)  # 「### E1 ·」「### a1　」「1. 」
REPLY_SEC = re.compile(r'^## 回复[ \t]*\n(.*?)(?=^## |\Z)', re.M | re.S)


def add_answer_block(text):
    """Every ask page gets a free-text 「### 我的回答」 at the end of 「## 回复」, one slot per numbered item above
    (「E1：」「a2：」「3：」), and a 「发出」 box. No change when the section is missing, already received, or has the block."""
    m = REPLY_SEC.search(text)
    if not m or '[!done]' in m.group(1) or ANSWER_HEAD in m.group(1):
        return text
    items = list(dict.fromkeys(h or n for h, n in ITEM_RE.findall(text[:m.start()].split('\n---\n', 1)[-1])))
    if re.search(r'^[ \t]*[-*+] \[.\][ \t]*\d{1,2}[ \t]*[A-Za-z](?![A-Za-z])', m.group(1), re.M):
        items = []  # a 1A/1B page already numbers its questions in the boxes: free text only
    block = [ANSWER_HEAD, '', ANSWER_HINT, ''] + ['- %s：' % i for i in items] + ([''] if items else ['', '']) + ['- [ ] 发出', '']
    sec = m.group(1).rstrip('\n') + '\n\n'
    return text[:m.start(1)] + sec + '\n'.join(block) + '\n' + text[m.end(1):]


def check_ask(a):
    """`cli ask`: thread, title, one-sentence ask; checked before queuing too."""
    db.check_wait_thread(a.get('thread'))
    for k in ('title', 'ask'):
        if not (a.get(k) or '').strip():
            raise db.HSError('--%s is empty' % k)
    if len(a['ask']) > 120:
        raise db.HSError('--ask must be one sentence of at most 120 characters')
    db._check(a.get('mainline') or db.MAINLINES[0], db.MAINLINES, 'mainline')


def ask_page(a, now, vault=None):
    from .render import _md
    opts = a.get('option') or ['批', '不批']
    out = ['---', '类型: 要你定', '主线: ' + (a.get('mainline') or db.MAINLINES[0]), '状态: 待你看', '日期: ' + now.strftime('%Y-%m-%d %H:%M'),
           '来自: ' + (a.get('editor') or ASK_EDITOR), '问你: ' + ' '.join(a['ask'].split()), '线程: ' + a['thread'], '类别: 要批', '---', '',
           '# ' + _md(a['title']), '', '## 要你定', '', '> [!owner] ' + _md(a['ask'])]
    if a.get('body'):  # keep the thread's lines (headings, lists): _md would squash a 10-item checklist into one line
        out += ['', re.sub(r'(?<!\\)\$', r'\\$', a['body'].strip())]
    if a.get('links'):  # report files and zips go to 汇报/附件 and are linked, like note-add --links
        out += ['', '## 附件', ''] + close_links(_note_links(a['links']), '', vault)[0]
    out += ['', '## 回复', ''] + ['- [ ] ' + _md(o) for o in opts] + ['', '## 更新', '']
    return add_answer_block('\n'.join(out) + '\n')


def write_ask(vault, a, log_change=None, now=None):
    """Writes 汇报/要批/<date> <title>.md with 问你 and 线程; autowait opens the wait on the next render. Returns the rel path."""
    check_ask(a)
    if not vault:
        raise db.HSError('no vault: ask pages are written on the Mac')
    vault = Path(vault)
    now = now or datetime.now(db.LOCAL_TZ)
    stem = db.unique_stem(vault, '%s %s' % (now.strftime('%Y-%m-%d'), BAD.sub(' ', a['title']).strip()))
    path = vault / db.REPORT_DIR / '要批' / (stem + '.md')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ask_page(a, now, vault), encoding='utf-8')
    rel = path.relative_to(vault).as_posix()
    if log_change:
        log_change(vault, a.get('editor') or ASK_EDITOR, rel, '开要批页（cli ask，线程 %s）' % a['thread'])
    return rel
