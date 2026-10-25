"""Collection health, independent of resource rows and provider error messages.

A failed page must never turn into an authoritative empty inventory. Providers
report failures at their I/O boundary; a sync session commits complete scopes or
keeps their previous rows. Context is copied into provider worker threads.
"""
from concurrent.futures import ThreadPoolExecutor as _Executor
from contextvars import ContextVar, copy_context
from datetime import datetime, timezone
from functools import wraps
import json
import subprocess
from threading import Lock

_attempt = ContextVar("collection_attempt", default=None)
_session = ContextVar("collection_session", default=None)
_optional = ContextVar("collection_optional", default=False)


class ThreadPoolExecutor(_Executor):
    def submit(self, fn, /, *args, **kwargs):
        return super().submit(copy_context().run, fn, *args, **kwargs)


def failed(code="query_failed"):
    """Record only a fixed code, never credentials, command arguments or responses."""
    attempt = _attempt.get()
    if attempt is not None:
        with attempt["lock"]:
            attempt["warnings" if _optional.get() else "errors"].add(code)


def _exception_code(exc):
    if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
        return "timeout"
    if isinstance(exc, FileNotFoundError):
        return "cli_missing"
    if isinstance(exc, (json.JSONDecodeError, TypeError, KeyError, ValueError)):
        return "invalid_response"
    return "query_failed"


def optional(func):
    """Enrichment can omit fields without invalidating a successful resource list."""
    @wraps(func)
    def wrapped(*args, **kwargs):
        token = _optional.set(True)
        try:
            return func(*args, **kwargs)
        finally:
            _optional.reset(token)
    return wrapped


def try_fallback(call, *args, **kwargs):
    """Try an alternate API; caller MUST report failure if all alternatives fail."""
    token = _attempt.set(None)
    try:
        return call(*args, **kwargs)
    finally:
        _attempt.reset(token)


def observed(fn=None, *, result="nullable"):
    """Observe existing I/O contracts without changing callers outside sync."""
    def decorate(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            try:
                value = func(*args, **kwargs)
            except Exception as exc:
                failed(_exception_code(exc))
                raise
            bad = (value is None if result == "nullable" else
                   not value[0] if result == "pair" else not value)
            if bad:
                failed("credential_unavailable" if result == "token" else "query_failed")
            return value
        return wrapped
    return decorate(fn) if fn else decorate


def current():
    return _session.get()


class Session:
    def __init__(self, previous):
        self.previous = previous
        self.statuses = {self.key(s): dict(s) for s in previous.get("collection_status", [])}
        self.attempted = set()
        self.lock = Lock()

    @staticmethod
    def key(s):
        return (s["cloud"], s["account"], s["kind"], s.get("region", ""))

    def __enter__(self):
        self.token = _session.set(self)
        return self

    def __exit__(self, *exc):
        _session.reset(self.token)

    def collect(self, cloud, account, kind, old, call, *, region="", unavailable=False):
        key = (cloud, account, kind, region)
        attempt = {"errors": set(), "warnings": set(), "lock": Lock()}
        token = _attempt.set(attempt)
        rows = []
        try:
            if unavailable:
                failed("cli_missing")
            else:
                rows = call()
                if not isinstance(rows, list):
                    failed("invalid_response")
        except Exception as exc:
            # Provider exceptions are failures, not zero resources. Raw exception
            # strings may contain tokens/URLs, so they never enter status metadata.
            failed(_exception_code(exc))
        finally:
            _attempt.reset(token)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        errors = sorted(attempt["errors"])
        warnings = sorted(attempt["warnings"])
        with self.lock:
            previous = self.statuses.get(key, {})
            status = {"cloud": cloud, "account": account, "kind": kind, "region": region,
                      "status": "error" if errors else "partial" if warnings else "ok", "attempted_at": now,
                      "last_success_at": previous.get("last_success_at") if errors or warnings else now,
                      "preserved": bool(errors and old), "row_count": len(old if errors else rows),
                      "errors": errors, "warnings": warnings}
            self.statuses[key] = status
            self.attempted.add(key)
        return list(old) if errors else rows

    def attach(self, data):
        data["collection_status"] = [self.statuses[k] for k in sorted(self.statuses)]

    def failures(self):
        return [self.statuses[k] for k in sorted(self.attempted)
                if self.statuses[k]["status"] == "error"]

    def report(self):
        failures = self.failures()
        if failures:
            print(f"⚠️ {len(failures)} 个采集范围失败；已有数据已保留，未作为资源删除处理。")
            for s in failures:
                scope = "/".join(x for x in (s["cloud"], s["account"], s["kind"], s["region"]) if x)
                last = s["last_success_at"] or "未知（尚无成功时间记录）"
                print(f"  · {scope}：保留 {s['row_count']} 条；上次成功 {last}")
        partial = [self.statuses[k] for k in self.attempted if self.statuses[k]["status"] == "partial"]
        if partial:
            print(f"⚠️ {len(partial)} 个采集范围的列表已更新，部分补充字段获取失败；详见看板采集状态。")
