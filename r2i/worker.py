#!/usr/bin/env python3
"""Headless code worker (`claude -p`) under paid_run, configured by [worker] in r2i.toml (standard library only).

Interface: --task-file FILE [--mode code|audit] [--cwd DIR] [--budget ID] [--cap CNY] [--dry-run] [--timeout] [--max-turns].
Every project value (model, effort, relay base URL, budget id, cap, headroom, usd_rate, out_dir) comes from `config.get().worker`.

Layers: this module (outer) -> r2i.paid_run (budget, ledger, relay key from the Keychain item in [keys]) -> this module with
--run-child (inner) -> `claude -p --model <model> --effort <effort>`. The inner layer writes the CLI's own cost
(total_cost_usd x usd_rate) to PAID_RUN_SPEND_FILE so the run settles at that, not at the cap; no cost in the output = the whole
cap is charged. The CLI's cost is its list-price estimate, not the relay's bill. --max-budget-usd (headroom x cap / usd_rate)
stops the CLI inside the run: it checks the limit only after a turn, so the last turn can overshoot; the headroom leaves room.
--timeout bounds both local process groups.
Isolation: user settings are not loaded (--setting-sources project,local), hooks off, no MCP, model-routing environment
variables removed, ANTHROPIC_BASE_URL set to [worker] base_url when given. Keys are never printed or written to the logs.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

from r2i import config

STATUS = re.compile(r'^\s*R2I_WORKER_STATUS: (SUCCESS|FAILED)\s*$', re.MULTILINE)
CODE_TOOLS = 'Bash,Read,Glob,Grep,Write,Edit'
AUDIT_TOOLS = 'Read,Glob,Grep'
SESSION_ENV = (
    'CLAUDECODE', 'CLAUDE_PID', 'CLAUDE_EFFORT', 'CLAUDE_CODE_SESSION_ID',
    'CLAUDE_CODE_CHILD_SESSION', 'CLAUDE_CODE_ENTRYPOINT', 'CLAUDE_CODE_EXECPATH',
    'CLAUDE_CODE_MESSAGING_SOCKET', 'CLAUDE_CODE_MESSAGING_TOKEN',
    'CLAUDE_CODE_SESSION_ATTENDED', 'CLAUDE_CODE_AUTO_MODE_SERVER',
)
ENV_OVERRIDES = {
    'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1',
    'CLAUDE_CODE_DISABLE_AUTO_MEMORY': '1',
    'DISABLE_AUTOUPDATER': '1',
}
# variables that could send the child elsewhere or to another model (Bedrock/Vertex, aliases, other keys)
ROUTE_ENV = ('ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_API_KEY', 'ANTHROPIC_MODEL', 'ANTHROPIC_SMALL_FAST_MODEL',
             'ANTHROPIC_DEFAULT_SONNET_MODEL', 'ANTHROPIC_DEFAULT_OPUS_MODEL', 'ANTHROPIC_DEFAULT_HAIKU_MODEL',
             'ANTHROPIC_CUSTOM_HEADERS', 'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY',
             'CLAUDE_CODE_OAUTH_TOKEN', 'CLAUDE_CONFIG_DIR')


def utc():
    return datetime.now(timezone.utc).isoformat()


def redact(text):
    """Remove credentials from text that may echo the environment."""
    for name, value in os.environ.items():
        if any(word in name.upper() for word in ('TOKEN', 'SECRET', 'PASSWORD', 'API_KEY')) and len(value) >= 6:
            text = text.replace(value, '[REDACTED]')
    return re.sub(r'(?i)(bearer\s+)[^\s"\x27]+', r'\1[REDACTED]', text)


def save_json(path, data):
    path.write_text(redact(json.dumps(data, ensure_ascii=False, indent=2)) + '\n', encoding='utf-8')


def text_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(b.get('text', '') for b in content if isinstance(b, dict))
    return ''


def stop_group(process):
    """Stop only the process group created for this invocation."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def model_family(model):
    """The word of the model id that a served model must contain: 'claude-sonnet-5-5' -> 'sonnet'; a one-word id -> itself."""
    parts = model.split('-')
    return parts[1] if len(parts) > 1 and parts[1] else model


def claude_command(mode, max_turns, max_budget_usd, model=None, effort=None):
    w = config.get().worker
    tools = AUDIT_TOOLS if mode == 'audit' else CODE_TOOLS
    cmd = [shutil.which('claude') or 'claude', '-p', '--model', model or w['model'], '--effort', effort or w['effort'],
           '--tools', tools, '--allowedTools', tools, '--disallowedTools', 'Agent,Task,Workflow,NotebookEdit,WebFetch,WebSearch',
           '--permission-mode', 'dontAsk', '--permission-prompts', 'none', '--no-session-persistence',
           '--setting-sources', 'project,local', '--settings', '{"disableAllHooks":true}',
           '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--disable-slash-commands', '--no-chrome',
           '--output-format', 'stream-json', '--verbose', '--max-turns', str(max_turns),
           '--max-budget-usd', '%.4f' % max_budget_usd]
    if mode == 'audit':
        cmd[cmd.index('--disallowedTools') + 1] += ',Bash,Write,Edit'
    return cmd


def max_budget_usd(cap):
    w = config.get().worker
    return w['headroom'] * cap / w['usd_rate']


def child_env(environ=None):
    """The environment paid_run gets: no session variables, no keys, no routing overrides; [worker] base_url if set."""
    env = dict(os.environ if environ is None else environ)
    for name in SESSION_ENV + ROUTE_ENV + ('R2I_RELAY_KEY', 'ANTHROPIC_BASE_URL'):
        env.pop(name, None)
    env.update(ENV_OVERRIDES)
    base_url = config.get().worker['base_url']
    if base_url:
        env['ANTHROPIC_BASE_URL'] = base_url
    return env


def spend_record(final, usd_rate, cap):
    """What the inner layer writes to PAID_RUN_SPEND_FILE from the CLI's result event."""
    usd = (final or {}).get('total_cost_usd')
    if isinstance(usd, (int, float)) and not isinstance(usd, bool) and usd >= 0:
        rec = {'spent_cny': round(usd * usd_rate, 4), 'source': 'claude -p total_cost_usd x %g (CLI list-price estimate)' % usd_rate,
               'total_cost_usd': usd}
        if cap and rec['spent_cny'] > cap:
            rec['over_cap'] = True  # booked as spent (the real cost), flagged so the dispatching thread sees it
        return rec
    return {'spent_cny': 0, 'unknown_calls': 1, 'per_call_cap_cny': cap, 'source': 'no cost in the CLI output: whole cap'}


def inspect_events(path, model=None):
    """Final result, models seen, status line. SUCCESS needs a successful result, the configured model family and the status line."""
    family = model_family(model or config.get().worker['model'])
    models, final, turns = set(), None, 0
    for line in path.read_text(encoding='utf-8').splitlines() if path.exists() else []:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get('type') == 'assistant' and not event.get('parent_tool_use_id'):
            turns += 1
            if (event.get('message') or {}).get('model'):
                models.add(event['message']['model'])
        if event.get('type') == 'result':
            final = event
    errors = []
    if not final or final.get('is_error') or final.get('subtype') != 'success':
        errors.append('CLI did not report a successful session result.')
        detail = (final or {}).get('errors') or (final or {}).get('result')
        if detail:
            errors.append('CLI error: ' + redact(str(detail))[:2000])
    if final and final.get('permission_denials'):
        errors.append('CLI reported denied tool permissions.')
    report = text_content((final or {}).get('result')).strip()
    found = list(STATUS.finditer(report))
    status = None
    if len(found) != 1:
        errors.append('Report must contain exactly one R2I_WORKER_STATUS line.')
    else:
        status = found[0].group(1)
        report = report[:found[0].start()].strip()
        if status != 'SUCCESS':
            errors.append('worker reported FAILED.')
    if not any(family.lower() in m.lower() for m in models):
        errors.append('No %s assistant response; served model unverified (saw %s).' % (family, sorted(models) or 'none'))
    return {'report': report, 'errors': errors, 'worker_status': status, 'observed_models': sorted(models),
            'assistant_messages': turns, 'num_turns': (final or {}).get('num_turns'),
            'cli_reported_total_cost_usd': (final or {}).get('total_cost_usd'),
            'cli_reported_model_usage': (final or {}).get('modelUsage'), 'usage': (final or {}).get('usage')}


def _pump(pipe, path, errors):
    try:
        with path.open('w', encoding='utf-8') as log:
            for line in pipe:
                log.write(redact(line))
                log.flush()
    except Exception as exc:  # keep the run going; the error is reported
        errors.append(str(exc))
    finally:
        pipe.close()


def run_group(command, cwd, env, out_path, err_path, timeout, stdin_text=None):
    """Run in its own process group, logs to files, stop the group at the deadline. Returns (exit code, failure)."""
    errors, readers, process = [], [], None
    try:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
                                   errors='replace', start_new_session=True)
        for pipe, path in ((process.stdout, out_path), (process.stderr, err_path)):
            t = threading.Thread(target=_pump, args=(pipe, path, errors), daemon=True)
            t.start()
            readers.append(t)
        if stdin_text is not None:
            def send():
                try:
                    process.stdin.write(stdin_text)
                    process.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
            threading.Thread(target=send, daemon=True).start()
        return process.wait(timeout=timeout), None
    except subprocess.TimeoutExpired:
        return 124, 'timed out after %d seconds' % timeout
    except KeyboardInterrupt:
        return 130, 'interrupted; local process group stopped'
    except OSError as exc:
        return 1, str(exc)
    finally:
        if process is not None:
            stop_group(process)
        for t in readers:
            t.join(timeout=5)
        if errors:
            with err_path.open('a', encoding='utf-8') as f:
                f.write('\nlog errors: %s\n' % errors)


def inner(args):
    """Under paid_run: run claude, then write the spend file from its result."""
    w = config.get().worker
    out = Path(args.output)
    prompt = (out / 'prompt.txt').read_text(encoding='utf-8')
    cmd = claude_command(args.mode, args.max_turns, max_budget_usd(args.cap))
    previous = signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        code, failure = run_group(cmd, args.cwd, os.environ.copy(), out / 'events.jsonl', out / 'stderr.log',
                                  max(1, args.timeout - 30), prompt)
    finally:
        signal.signal(signal.SIGTERM, previous)
    final = None
    for line in (out / 'events.jsonl').read_text(encoding='utf-8').splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get('type') == 'result':
            final = e
    record = spend_record(final, w['usd_rate'], args.cap)
    spend_file = os.environ.get('PAID_RUN_SPEND_FILE')
    if spend_file:
        Path(spend_file).write_text(json.dumps(record), encoding='utf-8')
    save_json(out / 'spend.json', dict(record, failure=failure, cli_exit=code))
    return code


def out_base():
    w = config.get().worker
    return Path(w['out_dir']) if w['out_dir'] else config.get().runs_root / 'code-worker'


def main(argv=None):
    w = config.get().worker
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--task-file', type=Path)
    p.add_argument('--mode', choices=('code', 'audit'), default='code')
    p.add_argument('--cwd', type=Path, default=config.get().root, help='directory the worker works in (default: the project root)')
    p.add_argument('--budget', default=w['budget'], help='paid_run budget id (default: [worker] budget)')
    p.add_argument('--cap', type=float, default=w['cap'], help='CNY cap of this run (default: [worker] cap)')
    p.add_argument('--purpose', default='code worker (headless claude -p)')
    p.add_argument('--conclusion', default='task done as specified, tests pass')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--timeout', type=int, default=w['timeout'], help='wall time limit in seconds (default: [worker] timeout)')
    p.add_argument('--max-turns', type=int, default=w['max_turns'], help='worker turn limit (default: [worker] max_turns)')
    p.add_argument('--run-child', action='store_true', help=argparse.SUPPRESS)
    p.add_argument('--output', help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    if not w['model']:
        print('The code worker needs a model: set [worker] model in r2i.toml.', file=sys.stderr)
        return 2
    if args.timeout < 60 or args.max_turns < 1 or w['usd_rate'] <= 0:
        p.error('--timeout must be >= 60, --max-turns and [worker] usd_rate positive.')
    if args.run_child:
        return inner(args)
    if not args.budget:
        print('No budget id: give --budget or set [worker] budget in r2i.toml.', file=sys.stderr)
        return 2
    if not args.cap or args.cap <= 0:
        p.error('A positive --cap (or [worker] cap) is required.')
    if not args.task_file:
        p.error('--task-file is required.')
    try:
        task = args.task_file.read_text(encoding='utf-8')
    except (OSError, UnicodeError) as exc:
        p.error(redact(str(exc)))
    if not task.strip():
        p.error('The task must not be empty.')
    cwd = args.cwd.resolve()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '-' + uuid.uuid4().hex[:8]
    out = out_base() / stamp
    prompt = (
        f'Working directory: {cwd}\nMode: {args.mode}\n'
        'Execute only the following task, directly, without delegation. Do not run paid or scientific APIs, change '
        'configuration, commit, push, or modify frozen runs, saved responses or cost ledgers. Do not reveal credentials. '
        'Preserve unrelated changes. In audit mode read only and report findings. Verify with the commands the task gives. '
        'Report briefly: changed files, actual commands and results, remaining issues. A required command or test '
        'failing, or an incomplete task, means FAILED. End the final report with exactly one standalone line: '
        'R2I_WORKER_STATUS: SUCCESS or R2I_WORKER_STATUS: FAILED.\n\nTASK:\n' + task)
    inner_cmd = [sys.executable, '-B', '-m', 'r2i.worker', '--run-child', '--output', str(out), '--cwd', str(cwd),
                 '--mode', args.mode, '--max-turns', str(args.max_turns), '--timeout', str(args.timeout),
                 '--cap', repr(args.cap)]
    command = [sys.executable, '-B', '-m', 'r2i.paid_run', '--budget', args.budget, '--cap', repr(args.cap),
               '--purpose', args.purpose, '--conclusion', args.conclusion, '--'] + inner_cmd
    env = child_env()
    # the child runs from the project root and must import this package
    pkg_parent = str(Path(__file__).resolve().parents[1])
    env['PYTHONPATH'] = os.pathsep.join(filter(None, [pkg_parent, env.get('PYTHONPATH')]))
    env['R2I_PROJECT_DIR'] = str(config.get().root)  # --cwd may be another repo without r2i.toml
    receipt = {'worker': 'r2i.worker', 'model': w['model'], 'effort': w['effort'], 'base_url': w['base_url'] or None,
               'mode': args.mode, 'cwd': str(cwd), 'output': str(out), 'paid_run_argv': command,
               'claude_argv': claude_command(args.mode, args.max_turns, max_budget_usd(args.cap)),
               'env_removed': list(SESSION_ENV + ROUTE_ENV + ('R2I_RELAY_KEY',)),
               'env_set': dict(ENV_OVERRIDES, **({'ANTHROPIC_BASE_URL': w['base_url']} if w['base_url'] else {})),
               'auth': 'paid_run fills the key from the Keychain item named in [keys] in the child only; value never logged',
               'timeout_seconds': args.timeout, 'max_turns': args.max_turns, 'launched': False}
    if args.dry_run:
        print(redact(json.dumps(receipt, ensure_ascii=False, indent=2)))
        return 0
    out.mkdir(parents=True)
    (out / 'task.txt').write_text(redact(task), encoding='utf-8')
    (out / 'prompt.txt').write_text(redact(prompt), encoding='utf-8')
    (out / 'events.jsonl').touch()
    receipt.update(launched=True, started_at_utc=utc())
    save_json(out / 'launch.json', receipt)
    print('r2i.worker: %s %s%s\nOutput: %s' % (w['model'], w['effort'], ' via ' + w['base_url'] if w['base_url'] else '', out), flush=True)
    started = time.monotonic()
    code, failure = run_group(command, config.get().root, env, out / 'paid_run.log', out / 'paid_run.err', args.timeout)
    details = inspect_events(out / 'events.jsonl')
    if failure:
        details['errors'].insert(0, 'Worker ' + failure)
    if code:
        details['errors'].insert(0, 'paid_run exit code %s (see paid_run.err).' % code)
    try:
        details['spend'] = json.loads((out / 'spend.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        details['spend'] = None
    report = details.pop('report')
    if details['errors']:
        report += '\n\nFAILED\n' + '\n'.join('- ' + e for e in details['errors'])
        code = code or 1
    report = redact(report.strip()) + '\n'
    (out / 'final.md').write_text(report, encoding='utf-8')
    receipt.update(details, exit_code=code, elapsed_seconds=round(time.monotonic() - started, 3), finished_at_utc=utc())
    save_json(out / 'result.json', receipt)
    print(report, end='')
    spend = details.get('spend') or {}
    stream = sys.stderr if code else sys.stdout
    print('models %s · turns %s · ¥%s · %s' % (','.join(details['observed_models']) or '—', details.get('num_turns'),
                                              spend.get('spent_cny', '?'), out), file=stream)
    print('R2I_WORKER_STATUS: %s' % ('FAILED' if code else 'SUCCESS'), file=stream)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
