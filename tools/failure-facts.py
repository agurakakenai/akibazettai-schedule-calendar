"""Content-free failure facts for one candidate or child process.

Only the exception class name and the innermost tools/ frame (file name,
function and line) are exposed. Exception messages, arguments, local paths,
source text, image bytes and URLs are never read or returned.
"""
from pathlib import Path
import re
import traceback


TOOLS = Path(__file__).resolve().parent
CLASS = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,63}\Z')
LOCATION = re.compile(r'[A-Za-z0-9_.-]{1,64}\.py:[A-Za-z_<>][A-Za-z0-9_<>]{0,63}:[0-9]{1,6}\Z')
UNEXPECTED_REASON = 'candidate_unexpected_error'
# Continuing after these would desynchronise the shared ledgers or local storage.
FATAL_REASONS = frozenset({
    'source_usage_save_failed', 'invalid_source_usage', 'missing_source_usage',
    'source_usage_not_open', 'invalid_ai_usage', 'missing_ai_usage', 'ai_usage_not_open',
    'invalid_ai_usage_clock',
})


def exception_facts(exc):
    name = type(exc).__name__
    result = {'exceptionClass': name if CLASS.fullmatch(name) else 'Exception'}
    location = None
    for frame in traceback.extract_tb(exc.__traceback__):
        try:
            path = Path(frame.filename).resolve()
        except (OSError, ValueError, RuntimeError):
            continue
        if path.parent == TOOLS:
            location = f'{path.name}:{frame.name}:{frame.lineno}'
    if location is not None and LOCATION.fullmatch(location):
        result['location'] = location
    return result


def reason_of(exc):
    reason = getattr(exc, 'reason', None)
    if reason is None and isinstance(exc, ValueError) and exc.args:
        reason = exc.args[0]
    return reason if isinstance(reason, str) else None


def fatal(exc, *, storage_errors=True):
    """True when a candidate failure must still stop the whole component."""
    if isinstance(exc, (MemoryError, KeyboardInterrupt, SystemExit)):
        return True
    if storage_errors and isinstance(exc, OSError):
        return True
    return reason_of(exc) in FATAL_REASONS


def unexpected(exc):
    """A failure without a recognised reason, isolated to its candidate."""
    return not fatal(exc) and reason_of(exc) is None and not isinstance(exc, OSError)
