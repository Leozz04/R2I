"""The one place that says what a thread id and a message id look like."""
import re
import uuid

THREAD_RE = re.compile(r'^cc_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')
MSG_RE = re.compile(r'^(?:cc_)?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')  # a Claude Code transcript message uuid
THREAD_HINT = 'cc_<uuid4> (the Claude Code session id)'


def is_thread_id(text):
    return bool(THREAD_RE.match(text or ''))


def new_thread_id():
    return 'cc_' + str(uuid.uuid4())


def is_msg_id(text):
    return bool(MSG_RE.match(text or ''))

THREAD_PAT = THREAD_RE.pattern[1:-1]  # the same shape, unanchored, for finding ids inside text
THREAD_FIND = re.compile(THREAD_PAT)
