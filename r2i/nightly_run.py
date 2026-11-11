"""`r2i nightly run`: the nightly tidy-up as one job. Steps, in order: repository plan (organize), stale sync pages (list),
memory dream (proposal only), terminology pages, morning note, close the stale sync pages (unless a dry run), self-check of the
note (one rewrite). Every model step runs through r2i.worker in audit mode (read-only tools) with the [worker] config and its
standing budget; the program, not the model, writes the files. A failed step is logged and the next one still runs.

Usage: r2i nightly run [--date D] [--dry-run]; the program steps of r2i.nightly (threads, sync-stale, repo-list, report-check)
are available as r2i nightly <sub>.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import date as Date, datetime, timedelta
from pathlib import Path

from . import config, daily, nightly, terms, watchdog

PROMPTS = Path(__file__).with_name('nightly_prompts')
OVERALL_SECONDS = 4 * 3600
STALE_DAYS = 2
OUT_DIR = 'agent/管理/夜间'  # where the repository plan and the dream proposal go (vault-relative)
WARN = '> [!warning] Nightly self-check: '
TERMS_MARK = re.compile(r'^=== TERMS (.+?) ===[ \t]*$', re.MULTILINE)
MAX_TERM_ROWS = 32
SKIPPED = 'skipped'


def prompt(name):
    return (PROMPTS / (name + '.md')).read_text(encoding='utf-8')


def state():
    d = config.state_dir()
    return {'log': d / 'logs' / 'nightly.log', 'lock': d / 'nightly.lock', 'armed': d / 'nightly-armed', 'stage': d / 'nightly'}


def memory_dir():
    """Claude Code's memory folder of this project: ~/.claude/projects/<mangled project root>/memory."""
    return Path('~/.claude/projects').expanduser() / nightly.mangle(config.find_root()) / 'memory'


def worker_ready():
    w = config.get().worker
    return bool(w['model'] and w['budget'])


def run_worker(name, task, cwd, timeout, purpose, stage):
    """One read-only worker call. Returns the final text without its status line; raises RuntimeError on failure."""
    w = config.get().worker
    task_file = Path(stage) / ('task-%s.txt' % name)
    task_file.write_text(task, encoding='utf-8')
    cmd = [sys.executable, '-B', '-m', 'r2i.worker', '--task-file', str(task_file), '--mode', 'audit', '--cwd', str(cwd),
           '--purpose', purpose, '--conclusion', 'read-only, proposal text', '--timeout', str(timeout)]
    env = dict(os.environ, R2I_PROJECT_DIR=str(config.get().root))
    pkg_parent = str(Path(__file__).resolve().parents[1])
    env['PYTHONPATH'] = os.pathsep.join(filter(None, [pkg_parent, env.get('PYTHONPATH')]))
    res = subprocess.run(cmd, cwd=config.get().root, env=env, capture_output=True, text=True, encoding='utf-8',
                         errors='replace', timeout=timeout + 120)
    (Path(stage) / ('worker-%s.out' % name)).write_text(res.stdout, encoding='utf-8')
    (Path(stage) / ('worker-%s.err' % name)).write_text(res.stderr, encoding='utf-8')
    found = re.search(r'^Output: (.+)$', res.stdout, re.MULTILINE)
    final = Path(found.group(1).strip()) / 'final.md' if found else None
    if res.returncode != 0 or not final or not final.is_file() or not final.read_text(encoding='utf-8').strip():
        tail = (res.stderr.strip().splitlines() or res.stdout.strip().splitlines() or ['no output'])[-1][:200]
        raise RuntimeError('worker %s failed (exit %s): %s' % (name, res.returncode, tail))
    text = final.read_text(encoding='utf-8')
    return '\n'.join(l for l in text.splitlines() if 'R2I_WORKER_STATUS:' not in l).strip() + '\n'


def write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(text, encoding='utf-8')
    os.replace(tmp, path)


def link_vault(stage_dir, vault):
    link = Path(stage_dir) / 'vault'
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(Path(vault).resolve(), target_is_directory=True)


def strip_fence(text):
    t = text.strip()
    m = re.fullmatch(r'```(?:markdown|md)?\n(.*)\n```', t, re.DOTALL)
    return (m.group(1) if m else t).strip() + '\n'


def split_terms(text, shorts):
    """{short: page} for the parts that pass the format checks; the rest is reported in `notes`."""
    parts = TERMS_MARK.split(text)
    seen = {parts[i].strip(): parts[i + 1] for i in range(1, len(parts) - 1, 2)}
    pages, notes = {}, []
    for name in seen:
        if name not in shorts:
            notes.append('ignored unknown name %s' % name)
    for short in shorts:
        if short not in seen:
            notes.append('not written %s: no such part in the answer' % short)
            continue
        body = strip_fence(seen[short]).strip()
        rows = [l for l in body.splitlines() if l.lstrip().startswith('|')]
        if not body.startswith('---'):
            notes.append('not written %s: does not start with ---' % short)
        elif not rows:
            notes.append('not written %s: no table rows' % short)
        elif len(rows) > MAX_TERM_ROWS:
            notes.append('not written %s: %d table rows, over %d' % (short, len(rows), MAX_TERM_ROWS))
        else:
            pages[short] = body + '\n'
    return pages, notes


class Night:
    def __init__(self, date, dry, now=None, worker=None):
        self.cfg = config.get()
        self.st = state()
        self.D = date
        self.ND = (Date.fromisoformat(date) + timedelta(days=1)).isoformat()
        self.dry = dry
        self.start = time.monotonic()
        self.clock = time.monotonic
        self.worker = worker or run_worker
        self.vault = Path(self.cfg.vault)
        self.stage = self.st['stage'] / ('stage-' + self.ND)
        self.stage.mkdir(parents=True, exist_ok=True)
        self.note = self.vault / daily.note_rel(self.ND)
        self.daily_ok = False

    def log(self, msg):
        self.st['log'].parent.mkdir(parents=True, exist_ok=True)
        with self.st['log'].open('a', encoding='utf-8') as f:
            f.write('%s %s\n' % (datetime.now().strftime('%F %T'), msg))

    def remaining(self):
        return OVERALL_SECONDS - (self.clock() - self.start)

    def over(self):
        return self.remaining() <= 0

    def ask(self, name, task, cwd, purpose):
        w = self.cfg.worker
        timeout = int(min(w['timeout'], self.remaining()))
        if timeout < 60:
            raise RuntimeError('less than 60 seconds left of the 4 hour limit')
        out = self.worker(name, task, cwd, timeout, '%s %s' % (purpose, self.ND), self.stage)
        self.log('worker %s done' % name)
        return out

    # ---- steps ----
    def s_organize(self):
        repo = self.stage / 'repo.json'
        nightly._dump(repo, nightly.repo_list(str(self.cfg.root)))
        task = '%s\n\nInputs: %s\n' % (prompt('organize'), repo)
        text = self.ask('organize', task, self.cfg.root, 'nightly repository plan')
        rel = '%s/仓库整理-%s.md' % (OUT_DIR, self.ND)
        write_text(self.vault / rel, '---\ncategory: nightly tidy-up\ndate: %s\n---\n\n%s' % (self.ND, text))
        self.log('repository plan written to %s' % rel)

    def s_stale(self):
        res = nightly.sync_stale(self.vault, STALE_DAYS)
        nightly._dump(self.stage / 'stale.json', res)
        self.log('stale sync pages listed: %d' % len(res['candidates']))

    def s_dream(self):
        work = self.stage / 'dream'
        work.mkdir(exist_ok=True)
        nightly._dump(work / 'threads.json', nightly.threads(self.D))
        mem = memory_dir()
        if not mem.is_dir():
            raise RuntimeError('memory folder not found: %s' % mem)
        shutil.rmtree(work / 'memory', ignore_errors=True)
        shutil.copytree(mem, work / 'memory')
        text = self.ask('dream', prompt('dream'), work, 'nightly memory dream')
        rel = '%s/做梦-%s.md' % (OUT_DIR, self.ND)
        write_text(self.vault / rel, text)
        self.log('dream proposal written to %s' % rel)

    def s_terms(self):
        shorts = [m.short for m in self.cfg.mainlines]
        work = self.stage / 'terms'
        work.mkdir(exist_ok=True)
        link_vault(work, self.vault)
        pages = ['%s (short name %s)' % ('vault/' + terms.TERMS_FILE % s, s) for s in shorts]
        stamp = datetime.now(self.cfg.zone).strftime('%F %H:%M')
        task = '%s\n\nInputs: update time %s\nPages (relative to the current directory): %s\n' % (prompt('terms'), stamp, '; '.join(pages))
        text = self.ask('terms', task, work, 'nightly terminology update')
        written, notes = split_terms(text, shorts)
        for n in notes:
            self.log('terms: ' + n)
        for short, body in written.items():
            dest = self.vault / (terms.TERMS_FILE % short)
            if dest.exists():
                bak = self.stage / 'terms-backup'
                bak.mkdir(exist_ok=True)
                shutil.copy2(dest, bak / dest.name)
            write_text(dest, body)
            self.log('terms: written %s' % dest.name)

    def write_extra(self, extra_lines=()):
        lines = ['stale pages to close: %s' % (self.stage / 'stale.json'),
                 'repository plan page: %s/仓库整理-%s.md' % (OUT_DIR, self.ND),
                 'dream proposal page: %s/做梦-%s.md' % (OUT_DIR, self.ND),
                 'dry run: %s' % ('yes' if self.dry else 'no')] + list(extra_lines)
        write_text(self.stage / 'nightly-extra.txt', '\n'.join(lines) + '\n')

    def s_note(self):
        daily_dir = self.stage / 'daily'
        daily_dir.mkdir(exist_ok=True)
        link_vault(daily_dir, self.vault)
        inputs = daily_dir / 'inputs.json'
        conn = daily.connect_ro()
        try:
            digest = daily.digest(conn, self.D, str(self.vault), note_date=self.ND)
        finally:
            conn.close()
        nightly._dump(inputs, digest)
        extra = (self.stage / 'nightly-extra.txt').read_text(encoding='utf-8') if (self.stage / 'nightly-extra.txt').exists() else ''
        task = ('%s\n\nInputs:\ndate: %s\ninputs file: %s\nvault: %s\nnote date: %s\nnote path in the vault: %s\n'
                'nightly tidy-up:\n%s') % (prompt('daily'), self.D, inputs, daily_dir / 'vault', self.ND, daily.note_rel(self.ND), extra)
        text = strip_fence(self.ask('daily', task, daily_dir, 'nightly morning note'))
        if not text.startswith('---'):
            raise RuntimeError('the morning note does not start with front matter; not written')
        write_text(self.note, text)
        window_end = (digest.get('window') or {}).get('end')
        beat = {'date': self.D, 'at': datetime.now().astimezone().isoformat(timespec='seconds'), 'window_end': window_end}
        write_text(self.vault / 'agent/管理/.daily-heartbeat', json.dumps(beat, ensure_ascii=False) + '\n')
        problems = daily.check_problems(text)
        self.log('morning note written %s%s' % (daily.note_rel(self.ND), '; check: ' + '; '.join(problems) if problems else ''))
        self.daily_ok = True

    def s_apply(self):
        if self.daily_ok and not self.dry:
            res = nightly.sync_stale(self.vault, STALE_DAYS, apply=True)
            self.log('closed %d stale sync pages' % res['applied'])
        else:
            self.log('dry run or no morning note: stale sync pages not closed')

    def drop_warning(self):
        text = self.note.read_text(encoding='utf-8')
        kept = [l for l in text.split('\n') if not l.startswith(WARN)]
        if kept != text.split('\n'):
            write_text(self.note, '\n'.join(kept))

    def s_check(self):
        if not self.note.is_file():
            self.log('self-check: no morning note %s' % daily.note_rel(self.ND))
            return
        self.drop_warning()
        res = nightly.report_check(self.vault, self.note)
        if res['ok']:
            self.log('self-check passed (%d links)' % res['links'])
            return
        self.log('self-check failed, rewriting the note once')
        extra = ['previous attempt failed the self-check, fix these:']
        extra += ['link does not open or is ambiguous: [[%s]]' % b for b in res['bad_links']]
        extra += ['internal identifier: line %d "%s": %s' % (c['line'], c['match'], c['text']) for c in res['codes']]
        self.write_extra(extra)
        self.s_note()
        res = nightly.report_check(self.vault, self.note)
        if res['ok']:
            self.log('self-check passed after the rewrite')
            return
        self.drop_warning()
        with self.note.open('a', encoding='utf-8') as f:
            f.write('\n%s%d links do not open, %d internal identifiers\n' % (WARN, len(res['bad_links']), len(res['codes'])))
        raise RuntimeError('self-check still failed after the rewrite: %d bad links, %d identifiers'
                           % (len(res['bad_links']), len(res['codes'])))

    def go(self):
        self.log('nightly start D=%s ND=%s dry run=%s' % (self.D, self.ND, 'yes' if self.dry else 'no'))
        steps = [('organize', self.s_organize), ('sync-stale', self.s_stale), ('dream', self.s_dream),
                 ('terms', self.s_terms), ('extra', self.write_extra), ('note', self.s_note),
                 ('apply', self.s_apply), ('self-check', self.s_check)]
        for i, (name, fn) in enumerate(steps, 1):
            if self.over():
                self.log('over 4 hours: step %d (%s) and the rest are not run' % (i, name))
                break
            self.log('step %d %s start' % (i, name))
            try:
                fn()
            except Exception as exc:  # a failed step never stops the next one
                self.log('step %d %s failed: %s: %s' % (i, name, type(exc).__name__, str(exc)[:300]))
        self.log('nightly end %s' % self.D)


def run(date=None, dry_run=False, worker=None):
    """Returns 0 (also when nothing ran for a good reason; the log says why)."""
    cfg = config.get()
    st = state()
    D = date or (datetime.now(cfg.zone).date() - timedelta(days=1)).isoformat()
    Date.fromisoformat(D)
    st['log'].parent.mkdir(parents=True, exist_ok=True)

    def log(msg):
        with st['log'].open('a', encoding='utf-8') as f:
            f.write('%s %s\n' % (datetime.now().strftime('%F %T'), msg))

    ext = watchdog.external_mount()
    if ext and not os.path.ismount(ext):
        log('external disk %s is not mounted, nothing runs tonight (%s)' % (ext, D))
        return 0
    if not (worker or worker_ready()):
        log('[worker] model and budget are not set, nothing runs (%s)' % D)
        return 0
    try:
        os.mkdir(st['lock'])
    except FileExistsError:
        log('the previous run is not finished, skipping %s' % D)
        return 0
    try:
        dry = dry_run or not st['armed'].exists()
        Night(D, dry, worker=worker).go()
    finally:
        try:
            os.rmdir(st['lock'])
        except OSError:
            pass
    return 0


def main(argv=None):
    raw = sys.argv[1:] if argv is None else list(argv)
    if raw[:1] and raw[0] != 'run' and not raw[0].startswith('-'):
        return nightly.main(raw)  # threads, sync-stale, repo-list, report-check
    p = argparse.ArgumentParser(prog='r2i nightly', description=__doc__)
    sub = p.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('run', help='the nightly run (organize, dream, terms, morning note)')
    r.add_argument('--date', help='the day the note reports on (YYYY-MM-DD, local); default yesterday; the note is named for the next day')
    r.add_argument('--dry-run', action='store_true', help='do not close stale sync pages (always on until .r2i/nightly-armed exists)')
    sub.add_parser('threads', help='program step, see r2i.nightly')
    sub.add_parser('sync-stale', help='program step, see r2i.nightly')
    sub.add_parser('repo-list', help='program step, see r2i.nightly')
    sub.add_parser('report-check', help='program step, see r2i.nightly')
    args = p.parse_args(raw)
    return run(args.date, args.dry_run)


if __name__ == '__main__':
    sys.exit(main())
