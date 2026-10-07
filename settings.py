"""
settings.py — the one place tunable settings are read from.

Values live in the `settings` table (JSON, so numbers stay numbers). A key
that isn't in the table falls back to the module constant it replaces, so an
empty table behaves exactly like the constants did. The CLI and the panel
both read through here — if either wrote anywhere else, they would disagree.

Read at call time, never cached at import: the panel can change a setting
between two runs and the second run must see it.

    settings.get("min_coverage")        -> 0.85 (or whatever was set)
    settings.set("language", "ru")
    settings.reset("language")          -> back to the constant
    settings.all()                      -> {key: value} for every setting
    with settings.override(vad_enabled=True): ...   -> this process only
"""

import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import store


def _const(module: str, name: str):
    """The module constant a setting replaces. Imported lazily: transcribe.py
    and analyze.py import this module, so importing them at the top would be
    circular."""
    def default():
        mod = __import__(module)
        return getattr(mod, name)
    return default


def _number(lo=None, hi=None, integer=False):
    def check(v):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("must be a number")
        if integer and v != int(v):
            raise ValueError("must be a whole number")
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            raise ValueError(f"must be between {lo} and {hi}")
        return int(v) if integer else v
    return check


def _optional(check):
    return lambda v: None if v is None else check(v)


def _bool(v):
    if not isinstance(v, bool):
        raise ValueError("must be true or false")
    return v


def _model(v):
    if not isinstance(v, str) or not Path(v).is_file():
        raise ValueError(f"no model file at {v!r}")
    return v


def _language(v):
    if v != "auto" and not (isinstance(v, str) and re.fullmatch(r"[a-z]{2,3}", v)):
        raise ValueError('must be "auto" or a language code such as "ru"')
    return v


def _text(v):
    if not isinstance(v, str) or not v.strip():
        raise ValueError("must be a non-empty string")
    return v.strip()


# key -> (default, validator). The default is the constant the setting
# replaced; change a default there, not here.
SPEC = {
    "whisper_model":     (lambda: str(_const("transcribe", "WHISPER_MODEL")()).replace("\\", "/"), _model),
    "whisper_threads":   (_const("transcribe", "WHISPER_THREADS"), _number(1, 64, integer=True)),
    "language":          (_const("transcribe", "WHISPER_LANGUAGE"), _language),
    "vad_enabled":       (_const("transcribe", "WHISPER_VAD"), _bool),
    "vad_threshold":     (_const("transcribe", "WHISPER_VAD_THRESHOLD"), _optional(_number(0, 1))),
    "min_coverage":      (_const("transcribe", "MIN_COVERAGE"), _number(0, 1)),
    "min_density":       (_const("transcribe", "MIN_DENSITY"), _number(0, 1)),
    "min_words_per_min": (_const("transcribe", "MIN_WORDS_PER_MIN"), _number(0)),
    "analysis_language": (_const("analyze", "ANALYSIS_LANGUAGE"), _optional(_text)),
    "retention_days":    (_const("store", "RETENTION_DAYS"), _number(0, integer=True)),
}


def _check_key(key: str) -> None:
    if key not in SPEC:
        raise KeyError(f"unknown setting {key!r}; known: {', '.join(SPEC)}")


def _rows() -> dict:
    """Stored values. Empty — never an error — when there is no db or no
    table yet, e.g. `python transcribe.py` on a machine that never ran init."""
    if not store.DB_PATH.exists():
        return {}
    try:
        conn = store.connect()
        try:
            return {r["key"]: json.loads(r["value"])
                    for r in conn.execute("SELECT key, value FROM settings")}
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return {}


# In-process values that win over the table, for scripts that try settings
# without changing anyone's saved ones (bench_vad.py). Never persisted.
_overrides: dict = {}


@contextmanager
def override(**values):
    for k, v in values.items():
        _check_key(k)
        values[k] = SPEC[k][1](v)
    saved = dict(_overrides)
    _overrides.update(values)
    try:
        yield
    finally:
        _overrides.clear()
        _overrides.update(saved)


def get(key: str):
    _check_key(key)
    return all()[key]


def all() -> dict:
    rows = {**_rows(), **_overrides}
    return {k: rows[k] if k in rows else default() for k, (default, _) in SPEC.items()}


def defaults() -> dict:
    """What each setting falls back to — for a UI's "reset" and "default: …"."""
    return {k: default() for k, (default, _) in SPEC.items()}


def stored() -> set:
    """Keys that have a saved value, i.e. differ from 'use the default'."""
    return {k for k in _rows() if k in SPEC}


def set(key: str, value) -> None:
    """Validates, then stores. A stored null is a real value (e.g. 'no
    analysis language'); use reset() to go back to the default."""
    _check_key(key)
    value = SPEC[key][1](value)
    store.init()
    conn = store.connect()
    try:
        with conn:
            conn.execute(
                """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                       updated_at = excluded.updated_at""",
                (key, json.dumps(value), store.now()),
            )
    finally:
        conn.close()


def reset(key: str) -> None:
    _check_key(key)
    store.init()
    conn = store.connect()
    try:
        with conn:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))
    finally:
        conn.close()


if __name__ == "__main__":
    for k, v in all().items():
        print(f"{k:18} {v!r}")
