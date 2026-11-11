"""Project setup: `r2i init DIR`, `r2i install [--load]`, `r2i doctor`."""
import json
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tomllib
from pathlib import Path

import r2i
from . import config

HOOKS = {'UserPromptSubmit': 'user-prompt', 'PreCompact': 'pre-compact'}
HOOK_MARK = 'r2i.cli hook '
SCALARS = ('project', 'owner', 'tz', 'quiet_hours', 'currency', 'approve_over', 'night_budget', 'max_experiment_threads')
FALLBACK_DIRS = ('汇报/要批', '汇报/同步', '进度', 'agent/交接')
TEXT_SUFFIXES = ('.md', '.base', '.css', '.json')
# job name -> (module args after -m, launchd schedule); the jobs a project needs running, by registry name
REQUIRED_JOBS = {
    'r2i-sync': (['r2i.cli', 'render', '--vault', '{vault}', '--quiet'], {'StartInterval': 60}),
    'report-reply': (['r2i.reply_watch', '--vault', '{vault}'], {'KeepAlive': True}),
    'selfcheck': (['r2i.cli', 'selfcheck', '--vault', '{vault}'], {'StartInterval': 3600}),
    'night-watchdog': (['r2i.watchdog', '--vault', '{vault}'], {'StartInterval': 1800}),
    # daily 04:30 local; required only when [worker] model and budget are set; it must not run at load
    'nightly': (['r2i.nightly_run', 'run'], {'StartCalendarInterval': {'Hour': 4, 'Minute': 30}, 'RunAtLoad': False}),
}
OPTIONAL_JOBS = ('nightly',)
DEFAULT_JOBS = Path(r2i.__file__).parent / 'jobs.default.json'
MIGRATIONS = Path(r2i.__file__).parent / 'schema' / 'migrations'


class SetupError(Exception):
    pass


def _templates():
    here = Path(r2i.__file__).parent
    return next((p for p in (here.parent / 'vault-template', here / 'vault-template') if p.is_dir()), None)


def _copy_template(src, dst, values):
    """Copy src into dst without overwriting; {{key}} of the config scalars is filled in text files, unknown ones stay."""
    made = 0
    fill = lambda m: values.get(m.group(1), m.group(0))
    for f in sorted(src.rglob('*')):
        out = dst / f.relative_to(src)
        if f.is_dir():
            out.mkdir(parents=True, exist_ok=True)
        elif not out.exists():
            out.parent.mkdir(parents=True, exist_ok=True)
            if f.suffix in TEXT_SUFFIXES:
                out.write_text(re.sub(r'\{\{(\w+)\}\}', fill, f.read_text(encoding='utf-8')), encoding='utf-8')
            else:
                shutil.copyfile(f, out)
            made += 1
    return made


def _hook_command(kind):
    return 'R2I_PROJECT_DIR="$CLAUDE_PROJECT_DIR" "%s" -m r2i.cli hook %s' % (sys.executable, kind)


def _merge_settings(path):
    """Keep every other key; replace only our hook entries. Returns True when the file changed."""
    old = path.read_text(encoding='utf-8') if path.exists() else None
    try:
        data = json.loads(old) if old and old.strip() else {}
    except ValueError as e:
        raise SetupError('%s is not valid JSON (%s); fix it by hand' % (path, e)) from None
    hooks = data.setdefault('hooks', {})
    for event, kind in HOOKS.items():
        groups = [g for g in hooks.get(event, []) if not any(HOOK_MARK in h.get('command', '') for h in g.get('hooks', []))]
        groups.append({'hooks': [{'type': 'command', 'command': _hook_command(kind)}]})
        hooks[event] = groups
    new = json.dumps(data, indent=2, ensure_ascii=False) + '\n'
    if new == old:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding='utf-8')
    return True


def init(root, config_file=None):
    """Set up a project directory. Returns [(item, 'created'|'updated'|'exists'|note)]; raises SetupError."""
    from . import db, entries
    root, report = Path(root).expanduser().resolve(), []
    root.mkdir(parents=True, exist_ok=True)
    toml = root / config.FILE
    if toml.exists():
        report.append((config.FILE, 'exists'))
    elif config_file:
        shutil.copyfile(config_file, toml)
        report.append((config.FILE, 'created'))
    else:
        raise SetupError('%s is missing: pass --config FILE (copy r2i.example.toml and edit %s)'
                         % (toml, ', '.join(config.REQUIRED)))
    try:
        cfg = config.load(root)
        raw = tomllib.loads(toml.read_text(encoding='utf-8'))
    except (config.ConfigError, tomllib.TOMLDecodeError) as e:
        raise SetupError(str(e)) from None
    old_env = os.environ.get('R2I_PROJECT_DIR')
    os.environ['R2I_PROJECT_DIR'] = str(root); config.reset()  # db and entries read the project through the config
    try:
        fresh = not cfg.ledger.exists()
        db.connect(cfg.ledger).close()
        report.append(('ledger', 'created' if fresh else 'exists'))
        (root / '.r2i').mkdir(exist_ok=True)
        report.append(('runs_root', 'exists' if cfg.runs_root.is_dir() else 'created'))
        cfg.runs_root.mkdir(parents=True, exist_ok=True)
        existed = cfg.vault.is_dir()
        cfg.vault.mkdir(parents=True, exist_ok=True)
        tpl, made = _templates(), 0
        if tpl:
            made = _copy_template(tpl, cfg.vault, {k: str(raw[k]) for k in SCALARS})
        else:
            for d in FALLBACK_DIRS:
                made += not (cfg.vault / d).is_dir()
                (cfg.vault / d).mkdir(parents=True, exist_ok=True)
        report.append(('vault', 'created' if not existed else 'updated (%d new files)' % made if made else 'exists'))
        changed = _merge_settings(root / '.claude' / 'settings.json')
        report.append(('.claude/settings.json', 'updated' if changed else 'exists'))
        try:
            res = entries.sync(cfg.vault)
            report.append(('entry files', 'updated' if res['changed'] else 'exists' if not res['skipped'] else
                           'skipped (no %s in the vault)' % entries.MUST))
        except Exception as e:
            report.append(('entry files', 'skipped (%s)' % e))
    finally:
        if old_env is None:
            os.environ.pop('R2I_PROJECT_DIR', None)
        else:
            os.environ['R2I_PROJECT_DIR'] = old_env
        config.reset()
    return report


def nightly_on(cfg=None):
    w = (cfg or config.get()).worker
    return bool(w['model'] and w['budget'])


def required_labels():
    return {config.label(n): n for n in REQUIRED_JOBS if n not in OPTIONAL_JOBS or nightly_on()}


def _jobs():
    path = config.state_dir() / 'jobs.json'
    data = json.loads((path if path.exists() else DEFAULT_JOBS).read_text(encoding='utf-8'))
    return [config.label(j['label'][1:]) if j['label'].startswith('@') else j['label'] for j in data['jobs']]


def plist_dir():
    return config.state_dir() / 'launchd'


def install(load=False, run=subprocess.run, agents_dir=None):
    """Write launchd plists for the required registry jobs; returns the lines to print."""
    cfg, want = config.get(), required_labels()
    logs = config.state_dir() / 'logs'
    logs.mkdir(exist_ok=True); plist_dir().mkdir(exist_ok=True)
    lines, dests = [], []
    for label in _jobs():
        if label not in want:
            continue
        args, schedule = REQUIRED_JOBS[want[label]]
        plist = {'Label': label, 'ProgramArguments': [sys.executable, '-m'] + [a.replace('{vault}', str(cfg.vault)) for a in args],
                 'WorkingDirectory': str(cfg.root), 'EnvironmentVariables': {'R2I_PROJECT_DIR': str(cfg.root)},
                 'StandardOutPath': str(logs / (want[label] + '.log')), 'StandardErrorPath': str(logs / (want[label] + '.log')),
                 'RunAtLoad': True, **schedule}
        dest = plist_dir() / (label + '.plist')
        dest.write_bytes(plistlib.dumps(plist))
        lines.append('wrote %s' % dest)
        dests.append(dest)
    target = Path(agents_dir) if agents_dir else Path.home() / 'Library' / 'LaunchAgents'
    domain = 'gui/%d' % os.getuid()
    for dest in dests:
        cmd = ['launchctl', 'bootstrap', domain, str(target / dest.name)]
        if not load:
            lines.append('would run: cp %s %s && %s' % (dest, target, ' '.join(cmd)))
            continue
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(dest, target / dest.name)
        r = run(cmd, capture_output=True, text=True)
        lines.append('%s %s' % ('loaded' if r.returncode == 0 else 'launchctl failed (%d): %s' % (r.returncode, (r.stderr or '').strip()), dest.stem))
    return lines


def keychain_has(item, run=subprocess.run):
    """Existence only: no -w, and the output is dropped."""
    try:
        return run(['security', 'find-generic-password', '-s', item], capture_output=True, text=True).returncode == 0
    except OSError:
        return False


def doctor(has_key=None):
    """[(ok, line)] one per check."""
    has_key = has_key or keychain_has
    out = []

    def check(ok, text):
        out.append((bool(ok), text))
        return ok

    try:
        cfg = config.load(config.find_root())
    except config.ConfigError as e:
        check(False, 'config loads: %s' % e)
        return out
    check(True, 'config loads: %s' % (cfg.root / config.FILE))
    newest = max([1] + [int(f.name[:4]) for f in MIGRATIONS.glob('[0-9][0-9][0-9][0-9]_*.sql')])
    try:
        conn = sqlite3.connect('file:%s?mode=ro' % cfg.ledger, uri=True)
        try:
            row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        finally:
            conn.close()
        have = json.loads(row[0]) if row else None
        check(have == newest, 'ledger %s: schema_version %s (newest %d)' % (cfg.ledger, have, newest))
    except sqlite3.Error as e:
        check(False, 'ledger %s does not open: %s' % (cfg.ledger, e))
    check(cfg.vault.is_dir(), 'vault exists: %s' % cfg.vault)
    try:
        hooks = json.loads((cfg.root / '.claude' / 'settings.json').read_text(encoding='utf-8')).get('hooks', {})
    except (OSError, ValueError):
        hooks = {}
    for event, kind in HOOKS.items():
        cmds = [h.get('command', '') for g in hooks.get(event, []) for h in g.get('hooks', [])]
        check(any(HOOK_MARK + kind in c for c in cmds), '.claude/settings.json has the %s hook (%s)' % (kind, event))
    for label in sorted(required_labels()):
        check((config.state_dir() / 'launchd' / (label + '.plist')).is_file(), 'plist rendered: %s' % label)
    if not nightly_on(cfg):
        check(True, 'nightly off: [worker] not configured')
    for name, item in sorted(cfg.keys.items()):
        check(has_key(item), 'keychain item for [keys] %s: %s' % (name, item))
    if not cfg.key('wake_token'):
        check(True, 'reply wake off: [keys] wake_token not set (suggested item: %swake-token)' % cfg.key_prefix)
    check(cfg.runs_root.is_dir(), 'runs_root exists: %s' % cfg.runs_root)
    return out
