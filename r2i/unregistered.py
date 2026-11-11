"""Report pages that wait on the owner but have no open wait. The check only marks: it never
changes a report page and never registers a wait. 首页 shows a 「⚠ 没登记」 line; a Mac notification and a changelog
line go out once per page per day (no notification 00:00-09:00 local). It also marks pages of a
type that is no longer opened (结果, 只看, 已按推荐做); those marks and the attachment ⚠ lines go to 首页's folded 「给线程看」."""
from datetime import datetime
from pathlib import Path
import os
import re

from . import db, waits
from . import config

KEYWORDS = ('要你', '请你', '等你', '还差你', '需要你', '你来', '你手动')
TYPED_RE = re.compile(r'打字.{0,4}?[「“]([^」”\n]+)[」”]')  # 0-4 characters may sit between 打字 and the quote
RECEIVED_RE = re.compile(r'(?:收到你|不用)打字.{0,4}?[「“]([^」”\n]+)[」”]')
CODE_RE = re.compile(r'(?<![A-Za-z0-9-])[A-Z][A-Z0-9-]{2,}(?![A-Za-z0-9-])')  # DEV-01, CODE-01; a bare 「批」 is not a code


def typed_strings(text):
    """The quoted words a page asks the owner to type; only strings that carry a code count."""
    return [t for t in TYPED_RE.findall(text) if CODE_RE.search(t)]
SECTION = lambda name: re.compile(r'(?ms)^## %s[ \t]*\n(.*?)(?=^## |\Z)' % name)
WHY = {'a': '状态还是待你看、要你定没答', 'b': '要你打字', 'c': '更新里最后一行在等你',
       'fmt': '待你看的页没写问你，也没写要你打'}  # 'fmt': the page lacks both asks


def _props(text):
    from . import reply_watch
    return reply_watch.front(text)


def _title(text, name):
    m = re.search(r'(?m)^# (.+)$', text)
    return (m.group(1).strip() if m else re.sub(r'^\d{4}-\d{2}-\d{2} ', '', name))


HEADING_RE = re.compile(r'(?m)^## 更新(?=[ \t:：（(]|$)(.*)$')
NEWEST_FIRST = '（新的在上）'  # 「## 更新（新的在上）」 = one section, the newest line on top
STAMP_RE = re.compile(r'(\d{1,2})-(\d{1,2})[ T]+(\d{1,2}):(\d{2})')


def _stamp(text):
    m = STAMP_RE.search(text or '')
    try:
        return datetime(2000, int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))) if m else None
    except ValueError:
        return None


def update_sections(text):
    """Every 「## 更新…」 section (a bare one or one with a date such as 「## 更新 01-15 03:55」): [(heading rest, body)]."""
    out, flip = [], False
    for m in HEADING_RE.finditer(text):
        nxt = re.search(r'(?m)^## ', text[m.end():])
        body = text[m.end():m.end() + nxt.start()] if nxt else text[m.end():]
        head = m.group(1).strip()
        if head.startswith(NEWEST_FIRST):
            flip, head = True, head[len(NEWEST_FIRST):].strip()
        out.append((head, body))
    if flip:  # newest-first page: hand callers the old bottom-up order, so 「the last line wins」 still means the newest
        out = [(h, '\n'.join(reversed(b.split('\n')))) for h, b in reversed(out)]
    return out


def _future(t, now):
    from .archive import FUTURE
    if t is None:
        return False
    try:
        t = t.replace(year=now.year, tzinfo=now.tzinfo)
    except ValueError:
        return False
    return t > now + FUTURE


def newest_update(sections, now=None):
    """The newest update line and its section: {'line', 'section' (heading rest + body), 'when' (the text its time is
    read from)}, or None. A dated heading gives its lines its time, a bare section's lines carry their own; without
    times for all lines, the last one in the file wins. Blank lines and lines starting with `>` are not candidates."""
    cands = []
    for head, body in sections:
        rest = STAMP_RE.sub('', head, count=1).lstrip(' :：').strip()
        lines = ([rest] if rest else []) + body.split('\n')
        for l in lines:
            if l.strip() and not l.strip().startswith('>'):
                t = _stamp(l)
                if now is not None:  # a stamp further ahead than archive.FUTURE is ignored: fall back to the other time
                    t = None if _future(t, now) else t
                    tl = None if _future(_stamp(head), now) else _stamp(head)
                else:
                    tl = _stamp(head)
                cands.append((t or tl, {'line': l, 'section': head + '\n' + body, 'when': l if t else head}))
    if not cands:
        return None
    if all(t for t, _ in cands):
        return max(enumerate(cands), key=lambda ic: (ic[1][0], ic[0]))[1][1]
    return cands[-1][1]


def newest_update_line(sections):
    nu = newest_update(sections)
    return nu['line'] if nu else None


def _no_receipts(text):
    return '\n'.join(l for l in text.split('\n') if '收到你打字' not in l and '不用打字' not in l)


def pending_typed(text, sections):
    """Typed words the page still asks for. A code is needed to count a string, except in the newest update section,
    where any 打字「X」 counts."""
    done = set(RECEIVED_RE.findall('\n'.join(body for _, body in sections)))
    pend = [t for t in typed_strings(_no_receipts(text)) if t not in done]
    nu = newest_update(sections)
    if nu:
        pend += [t for t in TYPED_RE.findall(_no_receipts(nu['section'])) if t not in done]
    return pend


WAIT_REF_RE = re.compile(r'等你回[:： ]*wait-\d{8}-\d+')


def _asks_owner(line):
    """The line asks the owner something: a KEYWORDS hit that is not just a wait reference, and not a receipt of their answer."""
    s = line.strip()
    if s.startswith('- '):
        s = s[2:].lstrip()
    s = re.sub(r'^\d{1,2}-\d{1,2}[ T]\d{1,2}:\d{2}[ :：]*', '', s)  # a leading 「01-15 23:52」 stamp
    if s.startswith('收到你') or ('你已在' in s and '勾了' in s):
        return False
    return any(k in WAIT_REF_RE.sub('', line) for k in KEYWORDS)


def newest_section_asks(text, now=None):
    """'b' or 'c' when the newest update section asks the owner something, else None (used to reopen a finished page)."""
    sections = update_sections(text)
    nu = newest_update(sections, now)
    if not nu:
        return None
    done = set(RECEIVED_RE.findall('\n'.join(body for _, body in sections)))
    if any(t not in done for t in TYPED_RE.findall(_no_receipts(nu['section']))):
        return 'b'
    if _asks_owner(nu['line']):
        return 'c'
    return None


def waiting_reason(text):
    """'a', 'b' or 'c' when the page is waiting on the owner (first condition met), else None."""
    props = _props(text)
    if db.report_done(props.get('状态')):  # a finished page is only reopened by its newest section (REOPEN rule)
        return newest_section_asks(text)
    sec = re.search(r'(?ms)^## 回复[ \t]*\n(.*?)(?=^## |\Z)', text)
    if props.get('状态') == '待你看' and db.report_type(props.get('类型')) == '要你定' \
            and not (sec and re.search(r'\[!done\][^\n]*已收到', sec.group(1))):
        return 'a'
    if props.get('问你') or props.get('要你打'):  # these pages open their own waits (autowait): no b/c fallback
        return None
    sections = update_sections(text)
    if pending_typed(text, sections):
        return 'b'
    last = newest_update_line(sections)
    if last and _asks_owner(last):
        return 'c'
    return None


def _reply_received(text):
    sec = re.search(r'(?ms)^## 回复[ \t]*\n(.*?)(?=^## |\Z)', text)
    return bool(sec and re.search(r'\[!done\][^\n]*已收到', sec.group(1)))


def is_sync(props, path):
    """A 同步 page: no wait, no 问你; it has one box 「已看」."""
    return props.get('类别') == '同步' or path.parent.name == '同步'


# An experiment plan is written AFTER reading the resource table (cli res-list) and records what it read.
PLAN_WORDS = re.compile(r'GPU|paid_run|租卡', re.I)
RES_STAMP = re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$')
RES_LINE = re.compile(r'^[ \t>]*(?:[-*+][ \t]*)?\**资源\**[ \t]*[:：][ \t]*\S', re.M)


MONEY_RE = re.compile(r'(\d+(?:[.,]\d+)*)')


def _money(v):
    """True when a 金额 / 预算 value holds a number > 0 (¥25, $3.5, 25元); ¥0, 0 and empty are not."""
    for n in MONEY_RE.findall(v or ''):
        try:
            if float(n.replace(',', '')) > 0:
                return True
        except ValueError:
            pass
    return False


def _outside_thread_block(body):
    """The body without the folded 「给线程看」 callout (its header and every following `>` line)."""
    out, skip = [], False
    for l in body.split('\n'):
        if l.strip().startswith('> [!note]- 给线程看'):
            skip = True
            continue
        if skip and l.startswith('>'):
            continue
        skip = False
        out.append(l)
    return '\n'.join(out)


def is_plan(props, text):
    """A page that spends money or compute: 金额 / 预算 > 0, or GPU / paid_run / 租卡 in the body outside 「给线程看」.
    A 类型 要你做 page is exempt."""
    if (props.get('类型') or '').strip() == '要你做':
        return False
    if _money(props.get('金额')) or _money(props.get('预算')):
        return True
    body = text.split('\n---', 1)[1] if text.startswith('---\n') and '\n---' in text[3:] else text
    return bool(PLAN_WORDS.search(_outside_thread_block(body)))


def read_resource_table(props, text, services=()):
    """True when the page has the property 资源台 (YYYY-MM-DD HH:MM) AND a 「资源：…」 line or a table row naming a resource."""
    if not RES_STAMP.match((props.get('资源台') or '').strip()):
        return False
    if RES_LINE.search(text):
        return True
    low = [x.lower() for x in services if x]
    return any(l.lstrip().startswith('|') and any(x in l.lower() for x in low) for l in text.split('\n'))


def res_flag(conn, props, text, path):
    """⚠ 没看资源台 applies to a live 要批/ (or root) page that is a plan, is not answered or closed, and lacks the read record."""
    if path.parent.name not in ('要批', db.REPORT_DIR) or db.report_done(props.get('状态')) or _reply_received(text):
        return False
    if not is_plan(props, text):
        return False
    try:
        services = [r[0] for r in conn.execute('SELECT service FROM resources')]
    except Exception:
        services = []
    return not read_resource_table(props, text, services)


def duplicates(vault):
    """⚠ 重名 : live pages whose name another page under 汇报/ (live or any 归档 month) also has.
    Returns [{'stem', 'path', 'others': [paths]}], one per live page."""
    out = []
    taken = db.all_report_stems(vault)
    for path in db.report_pages(vault):
        same = [p for p in taken.get(path.stem, []) if p != path]
        if same:
            out.append({'stem': path.stem, 'path': path, 'others': same})
    return out


def dup_rows(conn, vault):
    """The ⚠ 重名 rows 首页 shows: only once mode 'kinds' is auto (observe: run() only logs)."""
    from . import modes
    if not vault or modes.get(conn, 'kinds') != 'auto':
        return []
    out = []
    for r in duplicates(vault):
        props = _props(r['path'].read_text(encoding='utf-8'))
        out.append({'page': r['stem'], 'title': r['stem'], 'source': props.get('来自') or '—', 'mainline': props.get('主线') or '',
                    'date': props.get('日期') or '', 'reason': 'dup', 'path': r['path'],
                    'rel': r['path'].relative_to(vault).with_suffix('').as_posix()})
    return out


def scan(conn, vault):
    """Live report pages (要批/, 同步/, the old 待决/ and the 汇报/ root; not 归档/). Returns the unregistered pages, oldest
    日期 first, then the ⚠ 重名 rows."""
    folder = Path(vault) / db.REPORT_DIR if vault else None
    if not folder or not folder.is_dir():
        return []
    open_waits = conn.execute("SELECT id, report FROM waits WHERE status='等待'").fetchall()
    reports = {w['report'] for w in open_waits if w['report']}
    ids = {w['id'] for w in open_waits}
    out, res_rows = [], []
    for path in db.report_pages(vault):
        if path.parent.name == db.RETIRED_DIR:
            continue  # the retired 汇报/待决/: those pages register through dec_pages, never through this check
        try:
            text = path.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            continue
        name = path.name[:-3]
        reason = waiting_reason(text)
        props = _props(text)
        if is_sync(props, path):
            continue  # a 同步 page waits for nobody
        from .archive import settled_by_thread
        if props.get('待决') and settled_by_thread(conn, props.get('待决')):
            continue  # a thread settled it; not the owner's to register or decide
        if res_flag(conn, props, text, path):
            res_rows.append({'page': name, 'title': _title(text, name), 'source': props.get('来自') or '—', 'mainline': props.get('主线') or '',
                             'date': props.get('日期') or '', 'reason': 'res', 'path': path, 'rel': path.relative_to(vault).with_suffix('').as_posix()})
        # 问你 / 要你打 is the only way to register; a 待你看 page with neither is a format error, and the
        # body is no longer read for hints (a/b/c above only decide finished pages and pages that carry no property)
        if props.get('状态') == '待你看' and not (props.get('问你') or props.get('要你打')) and not props.get('待决') and not props.get('预算') \
                and props.get('类型') not in db.NO_PAGE_TYPES and not _reply_received(text):
            reason = 'fmt'  # (决策页 with 待决 and 预算/上限 money pages register through their own mechanism)
        if not reason or name in reports or props.get('等你回') in ids:
            continue
        out.append({'page': name, 'title': _title(text, name), 'source': props.get('来自') or '—',
                    'mainline': props.get('主线') or '', 'date': props.get('日期') or '', 'reason': reason, 'path': path,
                    'rel': path.relative_to(vault).with_suffix('').as_posix()})
    out.sort(key=lambda r: (r['date'], r['page']))
    return out + sorted(res_rows, key=lambda r: (r['date'], r['page'])) + dup_rows(conn, vault)


ATTACH_DIR = db.REPORT_DIR + '/附件'
STRAY_DAYS = 3  # "newly put in": touched in the last 3 days


def stray_attachments(vault, now=None):
    """Specs and code do not belong in 汇报/附件 (they go to the external disk, <runs_root>/_vault-slim). Names of files put there in the last
    3 days that are a .py or carry -spec- / -draft- in the name."""
    import time
    folder = Path(vault) / ATTACH_DIR if vault else None
    if not folder or not folder.is_dir():
        return []
    now = now or time.time()
    out = []
    for f in sorted(folder.iterdir()):
        low = f.name.lower()
        if f.is_file() and (low.endswith('.py') or '-spec-' in low or '-draft-' in low) and now - f.stat().st_mtime < STRAY_DAYS * 86400:
            out.append(f.name)
    return out


def slim_where():
    return '外置盘 _vault-slim（%s）' % db.slim_root()


def stray_lines(names, md):
    return ['⚠ 汇报/附件 里新放了规格或代码：%s · 这类文件放%s，不放库里' % (md(n), slim_where()) for n in names]


def nopage_pages(vault):
    """Top-level 汇报/ pages of a type that is not opened (结果, 只看, 已按推荐做), and not finished. Returns [{'page', 'type', 'path'}]."""
    folder = Path(vault) / db.REPORT_DIR if vault else None
    if not folder or not folder.is_dir():
        return []
    out = []
    for path in db.report_pages(vault):
        if path.parent.name == db.RETIRED_DIR:
            continue
        try:
            props = _props(path.read_text(encoding='utf-8'))
        except (OSError, UnicodeDecodeError):
            continue
        if is_sync(props, path):
            continue  # 结果 / 只看 pages are 同步 pages now
        if props.get('类型') in db.NO_PAGE_TYPES and not db.report_done(props.get('状态')):
            out.append({'page': path.name[:-3], 'type': props.get('类型'), 'path': path})
    return out


def nopage_lines(pages, md):
    return ['⚠ 「%s」是「%s」类：不开这类页，写 audit-add' % (md(p['page']), p['type']) for p in pages]


def home_lines(flagged, md):
    """One 「⚠ 没登记」 row per page, in the 首页 row format: kind chip, question, 主线, link."""
    out = []
    for r in flagged:
        ml = db.SHORT_NAMES.get(r.get('mainline'), r.get('mainline') or '')
        link = r.get('rel') or '%s/%s' % (db.REPORT_DIR, r['page'])
        if r.get('reason') == 'dup':
            out.append('- <span class="r2i-k k-warn">⚠ 重名</span>%s%s · [[%s|去看]]' % (md(r['title']), ' · ' + md(ml) if ml else '', link))
            continue
        if r.get('reason') == 'res':
            out.append('- <span class="r2i-k k-warn">⚠ 没看资源台</span>计划页没写「资源台」时间和「资源」行 · %s%s · [[%s|去看]]' % (
                md(r['title']), ' · ' + md(ml) if ml else '', link))
            continue
        if r.get('reason') == 'fmt':
            out.append('- <span class="r2i-k k-warn">⚠ 格式不对</span>没写问你 · %s%s · [[%s|去看]]' % (
                md(r['title']), ' · ' + md(ml) if ml else '', link))
            continue
        out.append('- <span class="r2i-k k-warn">⚠ 没登记</span>%s%s · [[%s|去看]]' % (
            md(r['title']), ' · ' + md(ml) if ml else '', link))
    return out


def run(conn, vault, busy, log_change, now=None, sender=None):
    """Once per page per local day: one Mac notification (not at night) and one changelog line. A page that was
    touched a moment ago (busy) is left for the next round. Returns {'flagged': [...], 'notified': [...], 'logged': [...]}."""
    now = now or datetime.now(db.LOCAL_TZ)
    day = now.astimezone(db.LOCAL_TZ).strftime('%Y-%m-%d')
    flagged = scan(conn, vault)
    out = {'flagged': [r['page'] for r in flagged], 'notified': [], 'logged': [], 'attachments': []}
    for name in stray_attachments(vault):  # once per file per day, in the change table only
        if db.meta_get(conn, 'stray-log:' + name) != day:
            log_change(vault, 'r2i-sync 脚本', '%s/%s' % (ATTACH_DIR, name), '汇报/附件 里新放了规格或代码，应该放外置盘 _vault-slim（%s）' % db.slim_root())
            db.meta_set(conn, 'stray-log:' + name, day)
            out['attachments'].append(name)
    out['nopage'] = []
    for r in nopage_pages(vault):  # once per page per day, in the change table only
        if db.meta_get(conn, 'nopage-log:' + r['page']) != day:
            log_change(vault, 'r2i-sync 脚本', r['path'].relative_to(vault).as_posix(),
                       '「%s」类页：不开这类页，写 audit-add' % r['type'])
            db.meta_set(conn, 'nopage-log:' + r['page'], day)
            out['nopage'].append(r['page'])
    from . import modes
    out['dup'] = []
    if modes.get(conn, 'kinds') != 'off':  # ⚠ 重名: in the change table once per page per day; 首页 shows it only in auto
        for r in duplicates(vault):
            if db.meta_get(conn, 'dup-log:' + r['stem']) != day:
                log_change(vault, 'r2i-sync 脚本', r['path'].relative_to(vault).as_posix(),
                           '⚠ 重名：「%s」在 汇报/ 里还有 %d 页同名（%s）' % (r['stem'], len(r['others']), '、'.join(p.relative_to(vault).parent.as_posix() for p in r['others'])))
                db.meta_set(conn, 'dup-log:' + r['stem'], day)
                out['dup'].append(r['stem'])
    out['res'] = []
    for r in flagged:
        if r['reason'] == 'res':  # the homepage mark only; one changelog line per page per day, never a push
            if not busy(r['path']) and db.meta_get(conn, 'res-log:' + r['page']) != day:
                log_change(vault, 'r2i-sync 脚本', r['path'].relative_to(vault).as_posix(),
                           '⚠ 没看资源台：计划页要写「资源台: 读 res-list 的时间」和「资源：…」一行（R-20）')
                db.meta_set(conn, 'res-log:' + r['page'], day)
                out['res'].append(r['page'])
            continue
        if r['reason'] == 'dup':
            continue
        if busy(r['path']):
            continue
        if db.meta_get(conn, 'unreg-log:' + r['page']) != day:
            log_change(vault, 'r2i-sync 脚本', r['path'].relative_to(vault).as_posix(),
                       ('格式不对（%s）：%s 的页属性要写「问你」或「要你打」' if r['reason'] == 'fmt' else
                        config.say('没登记等你的事（%s）：%s 有一件事在等 {owner}，但没有 wait-add')) % (WHY[r['reason']], r['source']))
            db.meta_set(conn, 'unreg-log:' + r['page'], day)
            out['logged'].append(r['page'])
        if waits.quiet(now) or db.meta_get(conn, 'unreg-notified:' + r['page']) == day:
            continue
        if sender is None and os.environ.get('R2I_NO_PUSH'):  # tests never pop a real notification
            continue
        try:
            (sender or waits.send)('%s：没登记' % config.get().project, ('%s 的页格式不对，没写问你' if r['reason'] == 'fmt' else '%s 有一件事在等你，但没登记') % r['source'])
        except Exception:  # a notification must never stop rendering
            continue
        db.meta_set(conn, 'unreg-notified:' + r['page'], day)
        out['notified'].append(r['page'])
    return out
