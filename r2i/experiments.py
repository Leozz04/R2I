"""The experiments table: DDL, field validation and upsert. Local only; nothing is pushed anywhere.

Same field rules the ledger always had: lists accept JSON or comma text, dates are YYYY-MM-DD (date takes `start/end`),
stage and verdict come from fixed lists, mainline must be a configured mainline id, department (when the project lists
departments) must be one of them, a new row needs a title, and entering an ended stage stamps completed_at once."""
from datetime import datetime, timezone
import json
import re

from . import config

# field key -> label accepted as an alias (the labels the vault pages use)
FIELDS = {'title': '实验 / 审计', 'date': '日期', 'department': '负责组', 'stage': '阶段', 'verdict': '结论状态',
          'observation': '关键观察', 'cost': '费用与口径', 'path': '路径', 'next_check': '下一判别', 'raw_record': '原始记录',
          'mainline': '主线', 'completed_at': '完成时间'}
ALIASES = {**{k: k for k in FIELDS}, **{label: k for k, label in FIELDS.items()}, 'crit': 'crit'}
ENDED_STAGES = ('历史结果', '已复核', '已运行待复核', '失败/停止', '已取代')
OPTIONS = {
    'stage': ['历史结果', '方案准备', '拟议未执行', '已运行待复核', '已复核', '失败/停止', '进行中', '已取代'],
    'verdict': ['待验证', '仅机制证据', '局部支持', '混合结果', '不支持', '不可比'],
}
LISTS = ('department', 'mainline')
JSON_COLUMNS = LISTS
TEXT = ('observation', 'cost', 'path', 'next_check', 'raw_record')


class FieldError(Exception):
    pass


def _list_value(key, value):
    if value is None:
        items = []
    elif isinstance(value, list):
        items = value
    elif isinstance(value, str):
        value = value.strip()
        if value.startswith('['):
            try:
                items = json.loads(value)
            except ValueError:
                raise FieldError('invalid_json_list:' + key) from None
        else:
            items = value.split(',')
    else:
        raise FieldError('invalid_list_value:' + key)
    if not isinstance(items, list) or not all(isinstance(x, str) for x in items):
        raise FieldError('invalid_list_value:' + key)
    return list(dict.fromkeys(x.strip() for x in items if x.strip()))


def _allowed(key):
    if key == 'mainline':
        return [m.id for m in config.get().mainlines]
    if key == 'department':
        return list(config.get().departments) or None  # no departments configured: any name
    return OPTIONS[key]


def _check_option(key, value):
    allowed = _allowed(key)
    if allowed is not None and value not in allowed:
        raise FieldError('unknown_option:%s=%s' % (key, value))


def _date_part(text):
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}(T[0-9:.+Z-]+)?', text):
        raise FieldError('invalid_date:' + text)
    try:
        datetime.strptime(text[:10], '%Y-%m-%d')
    except ValueError:
        raise FieldError('invalid_date:' + text) from None
    return text


def normalize_fields(raw):
    """Validate a {key-or-label: value} dict into {column: value}. Raises FieldError, writes nothing."""
    out = {}
    for name, value in raw.items():
        key = ALIASES.get(name)
        if key is None:
            raise FieldError('unknown_field:' + str(name))
        if key == 'date':
            if value in (None, ''):
                out['date_start'] = out['date_end'] = None
                continue
            if not isinstance(value, str):
                raise FieldError('invalid_date_value')
            start, _, end = value.strip().partition('/')
            start, end = _date_part(start.strip()), (_date_part(end.strip()) if end.strip() else None)
            if end and end < start:
                raise FieldError('date_end_before_start')
            out['date_start'], out['date_end'] = start, end
        elif key == 'completed_at':
            if value is not None and not isinstance(value, str):
                raise FieldError('invalid_date_value')
            out[key] = _date_part(value.strip()) if value and value.strip() else None
        elif key in LISTS:
            items = _list_value(key, value)
            for item in items:
                _check_option(key, item)
            out[key] = items
        else:
            if value is not None and not isinstance(value, str):
                raise FieldError('invalid_text_value:' + key)
            value = (value or '').strip() if key != 'raw_record' else (value or '')
            if value and key in OPTIONS:
                _check_option(key, value)
            out[key] = value or None
    if 'title' in out and not out['title']:
        raise FieldError('title_must_not_be_empty')
    return out


def _stored(column, value):
    return json.dumps(value, ensure_ascii=False) if column in JSON_COLUMNS else value


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def upsert(conn, local_id, raw_fields, today=None):
    fields = normalize_fields(raw_fields)
    if not local_id or not local_id.strip():
        raise FieldError('missing_id')
    exists = conn.execute('SELECT stage, completed_at FROM experiments WHERE local_id=?', (local_id,)).fetchone()
    if not exists and not fields.get('title'):
        raise FieldError('new_experiment_requires_title')
    # Entering an ended stage stamps the completion date once; an explicit completed_at (even empty) wins.
    if fields.get('stage') in ENDED_STAGES and 'completed_at' not in fields \
            and (not exists or (exists['stage'] != fields['stage'] and not exists['completed_at'])):
        fields['completed_at'] = today or datetime.now().date().isoformat()
    with conn:
        if not exists:
            conn.execute("INSERT INTO experiments(local_id, department, mainline) VALUES(?,'[]','[]')", (local_id,))
        sets = ''.join(c + '=?, ' for c in fields)
        conn.execute('UPDATE experiments SET %supdated_at=? WHERE local_id=?' % sets,
                     [_stored(c, v) for c, v in fields.items()] + [_now(), local_id])
    return {'local_id': local_id, 'created': not exists, 'fields': sorted(fields)}
