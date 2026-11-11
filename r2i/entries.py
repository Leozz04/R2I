"""Entry files (CLAUDE.md / AGENTS.md / GEMINI.md) of the project root and the vault's CLAUDE.md, generated from the vault's
agent/必带清单.md (the one source) plus entry_source/header.md, and minute copies of the vault's rule table and path
allowlist into the project root. Never opens the database."""
from pathlib import Path
import os
import re

from . import config

SRC = Path(__file__).resolve().parent / 'entry_source'
MUST = 'agent/必带清单.md'
START = ('<!-- r2i-entry:start 本段由 r2i/entries.py 生成，手改会被覆盖；'
         '改先改 vault agent/必带清单.md -->')
START_RE = re.compile(r'<!-- r2i-entry:start[^\n]*-->')  # tolerant of another wording of the marker
END = '<!-- r2i-entry:end -->'
NAMES = ('CLAUDE.md', 'AGENTS.md', 'GEMINI.md')
COPIES = (('项目规则.md', 'PROJECT-RULES.md'), ('agent/管理/可读写路径.md', 'PATHS-ALLOWLIST.md'))
COPY_HEAD = '<!-- 副本：原件是 vault 的 %s，r2i-sync 每分钟同步一次，不要在这里改。 -->\n'


def block(vault):
    """The marked block: 必带清单's body without its frontmatter; None when the page is missing."""
    p = Path(vault) / MUST
    if not p.exists():
        return None
    text = p.read_text(encoding='utf-8')
    if text.startswith('---\n') and '\n---\n' in text[4:]:
        text = text[4:].split('\n---\n', 1)[1]
    return START + '\n' + text.strip() + '\n' + END


BACKUP_SUFFIX = '.pre-r2i'


def _marked(existing):
    return bool(existing and START_RE.search(existing) and END in existing)


def _backup(path, existing, backed_up, dry_run):
    """Copy a hand-written file to <name>.pre-r2i once (never over an existing backup); not on dry_run."""
    bak = path.with_name(path.name + BACKUP_SUFFIX)
    if dry_run or bak.exists():
        return
    bak.write_text(existing, encoding='utf-8')
    backed_up.append(str(bak))


def _tail(existing):
    """What follows the block in an existing file (a hand-written section, e.g. terms and macros); kept as is.
    A non-empty file without the markers is hand-written: all of it is kept below the block."""
    if _marked(existing):
        return existing.split(END, 1)[1]
    if existing.strip():
        return '\n' + existing
    return '\n'


def entry_text(blk, existing=''):
    head = (SRC / 'header.md').read_text(encoding='utf-8').replace('{project}', config.get().project)
    return head.rstrip() + '\n\n' + blk + _tail(existing)


def vault_text(existing, blk):
    m = START_RE.search(existing)
    if m and END in existing[m.end():]:
        return existing[:m.start()] + blk + existing[m.end():].split(END, 1)[1]
    return existing.rstrip() + '\n\n' + blk + '\n'


def _read(path):
    return path.read_text(encoding='utf-8') if path.exists() else ''


def _put(path, text, changed, dry_run):
    if path.exists() and path.read_text(encoding='utf-8') == text:
        return False
    changed.append(str(path))
    if not dry_run:
        tmp = path.with_name('.%s.tmp' % path.name)
        tmp.write_text(text, encoding='utf-8')
        os.replace(tmp, path)
    return True


def sync(vault, repo=None, dry_run=False):
    vault, repo = Path(vault), Path(repo or config.get().root)
    changed, skipped, backed_up = [], [], []
    blk = block(vault)
    if blk is None:  # no source page: leave every entry file as it is
        skipped.append(str(vault / MUST))
    for root in (repo,):
        if not root.is_dir():
            skipped.append(str(root))
            continue
        for name in NAMES if blk else ():
            old = _read(root / name)
            if old.strip() and not _marked(old):
                _backup(root / name, old, backed_up, dry_run)
            _put(root / name, entry_text(blk, old), changed, dry_run)
        for src, dst in COPIES:
            if (vault / src).exists():
                old = _read(root / dst)
                if old.strip() and not old.startswith(COPY_HEAD.split('%s')[0]):
                    _backup(root / dst, old, backed_up, dry_run)
                _put(root / dst, COPY_HEAD % src + (vault / src).read_text(encoding='utf-8'), changed, dry_run)
    vc = vault / 'CLAUDE.md'
    if blk and vc.exists() and _put(vc, vault_text(vc.read_text(encoding='utf-8'), blk), changed, dry_run) and not dry_run:
        from . import render  # render imports db; only needed for the changelog line
        render.log_change(vault, render.EDITOR, 'CLAUDE.md', '入口文件「必带清单」一段更新')
    return {'changed': changed, 'skipped': skipped, 'backed_up': backed_up}
