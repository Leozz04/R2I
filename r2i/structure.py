"""主线结构图: one frozen pipeline diagram per mainline in the vault's agent/结构/.
Only struct-set writes over a page; render only creates the 「未定」 placeholder when the page is missing."""
from datetime import datetime
from pathlib import Path
import json
import os
import re

from . import db
from . import config

DIR = 'agent/结构'
OLD_DIR = 'agent/结构/_old'
BODY_MAX = 8000  # characters of page body; the queue item carries the text itself
NODE_MAX = 12
PLACEHOLDER_LINE = config.say('还没定版：推进线程画 v1 后在汇报页给 {owner} 勾')
VERSION_RE = re.compile(r'^v(\d+)$')
APPROVER_RE = re.compile(r'(?m)^(版本|定版|批准|测试): *(.*)$')


def page_rel(ml):
    return '%s/%s 结构.md' % (DIR, db.MAINLINE_NAMES[ml])


def old_rel(ml, version):
    return '%s/%s 结构 %s.md' % (OLD_DIR, db.MAINLINE_NAMES[ml], version)


def embed(ml):
    return '![[%s/%s 结构#结构图]]' % (DIR, db.MAINLINE_NAMES[ml])


def _vnum(version):
    m = VERSION_RE.match(version or '')
    if not m:
        raise db.HSError('--version must look like v1, v2 …, got %r' % version)
    return int(m.group(1))


def _fm_value(value):
    value = ' '.join(str(value).split())
    return json.dumps(value, ensure_ascii=False) if re.search(r'[:#"\[\]{}]|^[-?&*!|>\'%@`]', value) else value


def clean_body(text):
    """The page body: everything from the first 「## 」 heading on. Whatever comes before it (a 来源 line, a property block,
    a 「# 标题」) is dropped, and so is any 「## 版本记录」 section (the script writes those)."""
    t = (text or '').replace('\r\n', '\n')
    m = re.search(r'(?m)^## ', t)
    if not m:
        raise db.HSError('the file has no 「## 」 heading (expected 「## 结构图」 first)')
    t = t[m.start():].strip()
    t = re.sub(r'(?ms)^## 版本记录[ \t]*\n.*?(?=^## |\Z)', '', t).strip()
    if not t:
        raise db.HSError('--file is empty')
    if len(t) > BODY_MAX:
        raise db.HSError('the structure page must be at most %d characters, got %d' % (BODY_MAX, len(t)))
    return t


def check_steps_match(body, blocks):
    """Every diagram node BN needs a 「步N」 row in the 步骤 table, and the table has no 步N the diagram lacks."""
    nodes = set()
    for b in blocks:
        nodes |= set(re.findall(r'\b(B\d+)[\[\(\{]+"', b))
    in_diagram = {int(n[1:]) for n in nodes}
    sec = re.search(r'(?ms)^## 步骤[ \t]*\n(.*?)(?=^## |\Z)', body)
    rows = set()
    for line in (sec.group(1) if sec else '').split('\n'):
        m = re.match(r'\s*\|\s*\**步(\d+)\b', line)
        if m:
            rows.add(int(m.group(1)))
    no_row, no_node = sorted(in_diagram - rows), sorted(rows - in_diagram)
    if no_row or no_node:
        msg = []
        if no_row:
            msg.append('图里有、「## 步骤」表里没有：' + '、'.join('步%d' % n for n in no_row))
        if no_node:
            msg.append('表里有、图里没有：' + '、'.join('步%d' % n for n in no_node))
        raise db.HSError('步号对不上（' + '；'.join(msg) + '）')


def _mermaid_blocks(body):
    return re.findall(r'```mermaid[ \t]*\n(.*?)```', body, flags=re.S)


def check_mermaid(block):
    """Flowchart node text must be double-quoted: B3["步3 汇总"], never B3[步3 汇总]."""
    if not re.match(r'\s*(flowchart|graph)\b', block):
        return
    lines = [l for l in block.split('\n') if not l.strip().startswith('%%')]
    text = re.sub(r'"[^"\n]*"', '""', '\n'.join(lines))
    for m in re.finditer(r'\b([A-Za-z_]\w*)[\[\(\{]', text):
        i = m.end()
        while i < len(text) and text[i] in '[({':
            i += 1
        if text[i:i + 1] != '"':
            raise db.HSError('mermaid node %s: the text must be in double quotes, e.g. %s["…"] (排版规范 第 7 节)' % (m.group(1), m.group(1)))
    labelled = re.findall(r'\b([A-Za-z_]\w*)[\[\(\{]+"([^"]*)"', '\n'.join(lines))
    for nid, label in labelled:  # 「步N」: node id BN, label starts with 步N
        m = re.fullmatch(r'B(\d+)', nid)
        if not m or not label.startswith('步' + m.group(1)) or re.match(r'步%s\d' % m.group(1), label):
            raise db.HSError('mermaid node %s: use id BN and text starting with 「步N」 with the same N, e.g. B3["步3 汇总"]' % nid)
    nodes = {n for n, _ in labelled}
    if len(nodes) > NODE_MAX:
        raise db.HSError('the diagram has %d nodes, at most %d' % (len(nodes), NODE_MAX))


def check_struct(mainline, version, approved_by, evidence, body, current=None):
    db._check(mainline, db.MAINLINES, 'mainline')
    n = _vnum(version)
    if not (approved_by or '').strip():
        raise db.HSError(config.say("the structure changes only with {owner}'s approval: --approved-by ({owner}'s words · where) is required"))
    if not (evidence or '').strip():
        raise db.HSError('--evidence (the run ids of the tests) is required')
    if current is not None and n <= current:
        raise db.HSError('--version must be greater than the current v%d, got %s' % (current, version))
    body = clean_body(body)
    if not re.search(r'(?m)^## 结构图[ \t]*$', body):
        raise db.HSError('the page needs a 「## 结构图」 section')
    if not re.search(r'(?m)^## 步骤[ \t]*$', body):
        raise db.HSError('the page needs a 「## 步骤」 section')
    blocks = _mermaid_blocks(body)
    if not blocks:
        raise db.HSError('「## 结构图」 needs a ```mermaid block')
    for b in blocks:
        check_mermaid(b)
    check_steps_match(body, blocks)
    return body


LOG_HEAD = ['| 版本 | 日期 | 批准 | 改了什么 |', '|---|---|---|---|']


def _page_info(path):
    """(version number or None, version-log rows) of an existing page; a placeholder (版本: 未定) has no version."""
    if not path.exists():
        return None, []
    text = path.read_text(encoding='utf-8')
    fm = dict(APPROVER_RE.findall(text.split('\n---', 1)[0])) if text.startswith('---\n') else {}
    m = VERSION_RE.match(fm.get('版本', '').strip().strip('"'))
    rows = []
    sec = re.search(r'(?ms)^## 版本记录[ \t]*\n(.*?)(?=^## |\Z)', text)
    if sec:
        rows = [l.rstrip() for l in sec.group(1).split('\n')
                if l.strip().startswith('|') and not re.match(r'\|\s*(版本\s*\||-)', l.strip())]
    return (int(m.group(1)) if m else None), rows


def current_version(conn, vault, ml):
    nums = [r[0] for r in conn.execute('SELECT version FROM structures WHERE mainline=?', (ml,))] if conn else []
    if vault:
        page_v, _ = _page_info(Path(vault) / page_rel(ml))
        if page_v is not None:
            nums.append(page_v)
    return max(nums) if nums else 0


def shown_version(conn, ml):
    row = conn.execute('SELECT MAX(version) FROM structures WHERE mainline=?', (ml,)).fetchone()
    return 'v%d' % row[0] if row and row[0] is not None else '未定'


def set_struct(conn, vault, *, mainline, version, approved_by, evidence, editor, body, changes=None):
    """Write the frozen page; the previous version moves to agent/结构/_old/ (never deleted). Returns the result with changelog."""
    from . import render
    if not vault:
        raise db.HSError('no vault found: struct-set writes agent/结构/ in the vault')
    db._check(mainline, db.MAINLINES, 'mainline')
    vault = Path(vault)
    cur = current_version(conn, vault, mainline)
    body = check_struct(mainline, version, approved_by, evidence, body, current=cur)
    n = _vnum(version)
    if not (editor or '').strip():
        raise db.HSError('--editor is required')
    page = vault / page_rel(mainline)
    old_v, log = _page_info(page)
    today = datetime.now(db.LOCAL_TZ).strftime('%Y-%m-%d')
    name = db.MAINLINE_NAMES[mainline]
    cell = lambda t: ' '.join(str(t).split()).replace('|', '／')
    log.append('| %s | %s | %s | %s |' % (version, today, cell(approved_by),
                                         cell(changes) if (changes or '').strip() else '测试 ' + cell(evidence)))
    text = '---\n版本: %s\n定版: %s\n批准: %s\n测试: %s\n---\n\n# %s 结构\n\n%s\n\n## 版本记录\n\n%s\n' % (
        version, today, _fm_value(approved_by), _fm_value(evidence), name, body, '\n'.join(LOG_HEAD + log))
    moved = None
    if old_v is not None:
        moved = vault / old_rel(mainline, 'v%d' % old_v)
        if moved.exists():
            raise db.HSError('%s already exists; not overwriting an old version' % moved.name)
    conn.execute('INSERT INTO structures(mainline,version,approved_by,evidence,editor,created_at) VALUES (?,?,?,?,?,?)',
                 (mainline, n, approved_by.strip(), evidence.strip(), editor, db.now()))
    try:
        if moved is not None:
            moved.parent.mkdir(parents=True, exist_ok=True)
            os.replace(page, moved)
        render._write(page, text)
    except OSError:
        conn.rollback()
        raise
    conn.commit()
    summary = '结构定版 %s（批准：%s；测试：%s）%s' % (version, ' '.join(approved_by.split()), ' '.join(evidence.split()),
                                                  '，旧版挪进 agent/结构/_old/' if moved else '')
    render.log_change(vault, editor, page_rel(mainline), summary)
    return {'mainline': mainline, 'version': version, 'page': page_rel(mainline), 'old': str(moved.relative_to(vault)) if moved else None,
            'changelog': 'ok'}


def placeholder_text(ml):
    return '---\n版本: 未定\n---\n\n# %s 结构\n\n## 结构图\n\n%s\n' % (db.MAINLINE_NAMES[ml], PLACEHOLDER_LINE)


def ensure_placeholders(vault, log_change=None):
    """Create the missing pages so embeds never break. An existing page (frozen or not) is never touched."""
    made = []
    for ml in db.MAINLINES:
        page = Path(vault) / page_rel(ml)
        if page.exists():
            continue
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(placeholder_text(ml), encoding='utf-8')
        made.append(page_rel(ml))
        if log_change:
            log_change(vault, 'r2i-sync 脚本', page_rel(ml), '建了空的结构页（版本未定），等推进线程画 v1')
    return made
