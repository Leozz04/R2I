"""Copy every mainline handover and the rule table to <dest>/<YYYYMMDD-HH>/ (relative paths kept, mtimes kept).
The hourly self-check calls backup() when mode 'vault-backup' is auto; by hand:
  python3 -B -m r2i.vault_backup --vault PATH [--dest DIR]      (copies whatever the mode)"""
import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import config, db, handover

RULES = '项目规则.md'
TIMEOUT = 60  # seconds; the hourly self-check skips the backup after this


def default_dest():
    return config.get().runs_root / '_backup'


def files():
    return list(handover.PAGES.values()) + [RULES]


def backup(vault, dest=None, now=None):
    """Copy to <dest>/<YYYYMMDD-HH>/ (local hour); a rerun in the same hour overwrites. Files missing from the vault are
    skipped. Returns the directory."""
    now = now or datetime.now(db.LOCAL_TZ)
    out = Path(dest or default_dest()) / now.astimezone(db.LOCAL_TZ).strftime('%Y%m%d-%H')
    for rel in files():
        src = Path(vault) / rel
        if src.is_file():
            (out / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, out / rel)
    return out


def _child_cmd(vault, dest, now):
    return [sys.executable, '-B', '-m', 'r2i.vault_backup', '--vault', str(vault), '--dest', str(dest),
            '--at', now.isoformat()]


def backup_with_timeout(vault, dest=None, now=None, timeout=TIMEOUT):
    """backup() in a child process. A write to the external disk can hang for hours (e.g. mkdir waiting on a macOS
    permission prompt). After `timeout` s the child is killed and not waited for (a child
    stuck in the kernel must not hold us too); TimeoutError says the backup was skipped this hour."""
    now = now or datetime.now(db.LOCAL_TZ)
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))  # the child imports r2i from this checkout
    p = subprocess.Popen(_child_cmd(vault, dest or default_dest(), now),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        raise TimeoutError('备份写外置盘超过 %d 秒，这次跳过' % timeout)
    if p.returncode:
        raise RuntimeError(((err or '').strip().splitlines() or ['exit %d' % p.returncode])[-1][-300:])  # last line = the exception
    return Path(out.strip().splitlines()[-1])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--vault', required=True)
    ap.add_argument('--dest')
    ap.add_argument('--at', help='ISO time that names the hour folder (default now)')
    a = ap.parse_args(argv)
    print(backup(a.vault, a.dest, datetime.fromisoformat(a.at) if a.at else None))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
