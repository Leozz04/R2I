"""Terminology check for the human layer. Reads the vault's 术语/<mainline> 术语.md and flags an old internal name that has a current name, e.g. OLD-NAME -> new name.

It is a candidate list, not a verdict: a flagged word can be legitimate (an experiment code inside a link to
实验索引). The writer reads each hit and decides. Short ambiguous tokens (L1, c2, NEG) are not checked.
"""
from pathlib import Path
import re

from . import config

TERMS_FILE = '术语/%s 术语.md'  # one page per mainline (its short name)
MIN_LEN = 3
SKIP = set()  # tokens that are the same in the paper or are experiment codes; fill per project
EXTRA = []
TOKEN_RE = re.compile(r'[A-Za-z][A-Za-z0-9_.]*(?:-[A-Za-z0-9_.]+)*')
CODE_LIKE = re.compile(r'^(?=.*\d)[A-Za-z][A-Za-z0-9_.-]*$|^[A-Z]+(?:-[A-Z]+)+$')


def load(vault):
    """[(internal, current name)] from the tables of 术语/<mainline> 术语.md. When a token appears in several rows, the row
    that names fewest things wins (a row naming OLD-A alone beats a row that lumps OLD-A with other names)."""
    paths = [Path(vault) / (TERMS_FILE % m.short) for m in config.get().mainlines]
    best = {}
    lines = [l for p in paths if p.exists() for l in p.read_text(encoding='utf-8').splitlines()]
    for line in lines:
        if not line.lstrip().startswith('|'):
            continue
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        if len(cells) < 2 or set(cells[0]) <= set('-: '):
            continue
        internal, paper = cells[0], cells[1].strip('`')
        if internal in ('Internal', '') or not paper or paper == '—':
            continue
        toks = TOKEN_RE.findall(internal)
        for tok in toks:
            if (len(tok) < MIN_LEN or tok in SKIP or tok.lower() in paper.lower() or not CODE_LIKE.match(tok)
                    or re.match(r'^v\d', tok)):
                continue
            if tok not in best or len(toks) < best[tok][1]:
                best[tok] = (paper, len(toks))
    out = [(tok, paper) for tok, (paper, _n) in best.items()]
    return out + [(t, p) for t, p in EXTRA if t not in best]


def scan(text, pairs):
    """[(line number, token, current name, line)] for each internal name used as a whole word."""
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        for tok, paper in pairs:
            if re.search(r'(?<![A-Za-z0-9_-])%s(?![A-Za-z0-9_-])' % re.escape(tok), line):
                hits.append((n, tok, paper, line.strip()[:160]))
    return hits


def check_files(vault, paths):
    pairs = load(vault)
    report = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            files = sorted(p.rglob('*.md'))
        else:
            files = [p]
        for f in files:
            for n, tok, paper, line in scan(f.read_text(encoding='utf-8'), pairs):
                report.append({'file': str(f), 'line': n, 'old': tok, 'now': paper, 'text': line})
    return report
