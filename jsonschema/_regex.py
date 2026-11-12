"""
Pluggable execution of regular expressions used by schema keywords.

The ``pattern`` and ``patternProperties`` keywords contain regular
expressions whose syntax and matching behavior are controlled by the
schema author.  When schemas are received from an external team, a
pattern can trigger catastrophic backtracking and block the thread
performing validation.  Python's :mod:`re` module cannot be interrupted
once a search has started, so running the search in another thread with
a timer does not provide a real bound: the worker thread keeps running.

This module defines an optional execution interface,
`RegexExecutor`, which validators can be constructed with.  When no
executor is provided, validation keeps its original, unchanged
semantics and calls :func:`re.search` directly.

The provided `BoundedRegexExecutor` is a standard-library-only example
of a restricted executor.  It runs each search in a separate worker
process which can be forcibly terminated, enforces an explicit budget
both for a single search and for the whole validation run, and reports
timeouts, executor failures and syntax errors as distinguishable
exceptions.  None of these failure modes is ever collapsed into a
"does not match" result.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import multiprocessing
import queue
import re
import threading
import time
import weakref


class RegexError(Exception):
    """
    Base class for failures raised while executing a schema regular
    expression through a `RegexExecutor`.

    These failures always abort validation rather than being treated as
    a non-matching pattern.  Messages intentionally contain no pattern
    text or instance value; use `keyword` and `path` to locate the
    offending schema keyword within the validated document.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.keyword: str | None = None
        self.path: tuple[str | int, ...] = ()

    def annotate(self, keyword: str, path: tuple[str | int, ...]) -> None:
        """
        Record where in the validation the failing regex was used.

        A regex failure bubbles through several enclosing keyword
        loops. The first (deepest) annotation wins for both the
        keyword and the location; outer loops only fill in a location
        when the failure was raised before any descend annotated it.
        """
        if self.keyword is None:
            self.keyword = keyword
            self.path = tuple(path) + self.path
        elif not self.path and path:
            self.path = tuple(path)

    def __str__(self) -> str:
        location = self.keyword or "regex"
        if self.path:
            rendered = "".join(
                f"[{segment!r}]" if isinstance(segment, str)
                else f"[{segment}]"
                for segment in self.path
            )
            location = f"{location} at {rendered}"
        return (
            "Regular expression execution failed for "
            f"{location}: {self.reason}"
        )


class RegexSyntaxError(RegexError):
    """
    The regular expression itself is not valid syntax.

    The original :class:`re.error` is retained as the exception cause.
    """


class RegexTimeout(RegexError):
    """
    A regular expression exceeded its execution budget.

    The timeout applies either to a single search
    (``reason == "call timeout"``) or to the cumulative budget of an
    entire validation run (``reason == "total timeout"``).
    """


class RegexExecutorError(RegexError):
    """
    The execution backend failed independently of the pattern, for
    example because a worker process exited unexpectedly.
    """


@dataclass(frozen=True)
class RegexBudget:
    """
    The explicit limits applied by `BoundedRegexExecutor`.

    Arguments:

        call_timeout:
            wall-clock seconds allowed for any single ``re.search``
        total_timeout:
            cumulative wall-clock seconds of regex work allowed during
            one whole validation run
        max_calls:
            maximum number of regex searches allowed during one whole
            validation run
    """

    call_timeout: float
    total_timeout: float
    max_calls: int | None = None

    def __post_init__(self) -> None:
        if self.call_timeout <= 0:
            raise ValueError("call_timeout must be positive")
        if self.total_timeout <= 0:
            raise ValueError("total_timeout must be positive")
        if self.max_calls is not None and self.max_calls < 0:
            raise ValueError("max_calls must be non-negative")


class RegexExecutor(ABC):
    """
    Interface for executing the regular expressions embedded in schemas.

    Implementations must guarantee that `search` either returns a
    boolean match result or raises a `RegexError`; it must never
    silently treat a timeout or backend failure as a non-match.
    """

    @abstractmethod
    def search(self, pattern: str, string: str) -> bool:
        """
        Return whether ``pattern`` matches ``string``.

        Raises:

            RegexSyntaxError: the pattern is not a valid regex
            RegexTimeout: an execution budget was exceeded
            RegexExecutorError: the execution backend failed
        """

    @contextmanager
    def validation(self):
        """
        Reset any whole-validation budget for one validation run.

        A single executor instance may back many validations; each
        top-level ``iter_errors`` call enters this context so that the
        per-run quotas do not leak between documents.
        """
        yield

    def close(self) -> None:
        """Release any resources held by the executor."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


_ACTIVE_BUDGET: ContextVar["_RunBudget | None"] = ContextVar(
    "jsonschema_regex_budget",
    default=None,
)


class _PoolTimeout(Exception):
    """Internal signal: a worker did not answer within its deadline."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class _RunBudget:
    budget: RegexBudget
    deadline: float
    calls: int = 0


def _worker_loop(incoming, outgoing):  # pragma: no cover - runs in subprocess
    while True:
        job = incoming.get()
        if job is None:
            return
        job_id, pattern, string = job
        try:
            result = bool(re.search(pattern, string))
        except re.error as error:
            outgoing.put((job_id, "syntax", error.args))
        except BaseException as error:  # noqa: BLE001 - report any failure
            # Only the exception type crosses the process boundary; its
            # message could echo the pattern or the matched string.
            outgoing.put((job_id, "fatal", type(error).__name__))
        else:
            outgoing.put((job_id, "ok", result))


class _BoundedProcessPool:
    """
    A small process pool whose workers can be forcibly replaced.

    Unlike a thread, a worker process stuck inside :func:`re.search`
    can be killed, so a timeout is a hard bound rather than a timer
    that leaves runaway work behind.  Each worker owns a dedicated
    queue and result channel, which lets the parent tell a timed-out
    worker apart from one that exited on its own.
    """

    _POLL_INTERVAL = 0.02

    def __init__(self, max_workers: int, *, context=None) -> None:
        self._max_workers = max_workers
        self._context = context or multiprocessing.get_context("spawn")
        self._condition = threading.Condition()
        self._closed = False
        self._workers = set()
        self._job_counter = 0
        for _ in range(max_workers):
            self._workers.add(self._start_worker())
        # Backstop if a caller forgets to close(): the OS also reaps
        # daemon children, but finalize reclaims them deterministically.
        self._finalizer = weakref.finalize(self, self._shutdown, self._workers)

    @property
    def in_flight(self) -> int:
        """Number of workers currently executing a search."""
        with self._condition:
            return self._max_workers - len(self._workers)

    def _start_worker(self):
        incoming = self._context.Queue()
        outgoing = self._context.Queue()
        process = self._context.Process(
            target=_worker_loop,
            args=(incoming, outgoing),
            daemon=True,
        )
        process.start()
        return process, incoming, outgoing

    def submit(
        self,
        pattern: str,
        string: str,
        call_timeout: float,
        total_deadline: float | None,
    ):
        with self._condition:
            if self._closed:
                raise RegexExecutorError("the executor has been closed")
            while not self._workers:
                self._condition.wait()
            worker = self._workers.pop()
        process, incoming, outgoing = worker
        with self._condition:
            self._job_counter += 1
            job_id = self._job_counter
        incoming.put((job_id, pattern, string))

        wait_timeout = call_timeout
        total_limits_first = False
        if total_deadline is not None:
            total_remaining = total_deadline - time.monotonic()
            total_limits_first = total_remaining <= call_timeout
            wait_timeout = min(wait_timeout, max(0.0, total_remaining))
        deadline = time.monotonic() + wait_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._replace(worker)
                reason = (
                    "total timeout" if total_limits_first else "call timeout"
                )
                raise _PoolTimeout(reason)
            try:
                response = outgoing.get(
                    timeout=min(remaining, self._POLL_INTERVAL),
                )
            except queue.Empty:
                if not process.is_alive():
                    self._replace(worker, dead=True)
                    raise RegexExecutorError(
                        "a regex worker process exited unexpectedly",
                    ) from None
                continue
            self._release(worker)
            return response[1], response[2]

    def _release(self, worker) -> None:
        with self._condition:
            self._workers.add(worker)
            self._condition.notify()

    def _replace(self, worker, *, dead: bool = False) -> None:
        process, _incoming, outgoing = worker
        if not dead and process.is_alive():
            process.terminate()
            process.join(timeout=1.0)
            if process.is_alive():  # pragma: no cover - hard kill backstop
                process.kill()
                process.join()
        outgoing.close()
        try:
            incoming = worker[1]
            incoming.close()
        except Exception:  # pragma: no cover - best-effort cleanup
            pass
        with self._condition:
            if not self._closed:
                self._workers.add(self._start_worker())
            self._condition.notify()

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            workers = list(self._workers)
            self._workers.clear()
        self._finalizer.detach()
        self._shutdown(workers)

    @staticmethod
    def _shutdown(workers) -> None:
        for process, incoming, _outgoing in workers:
            if process.is_alive():
                try:
                    incoming.put(None)
                except Exception:  # pragma: no cover - process may be gone
                    pass
            process.join(timeout=1.0)
            if process.is_alive():
                process.terminate()
                process.join(timeout=1.0)
            if process.is_alive():  # pragma: no cover - hard kill backstop
                process.kill()
                process.join()
            incoming.close()


class BoundedRegexExecutor(RegexExecutor):
    """
    A standard-library-only restricted `RegexExecutor`.

    Each search runs in an isolated worker process.  Concurrency is
    capped at ``max_workers`` processes, a stuck process is terminated
    and replaced on timeout, and an explicit `RegexBudget` bounds both
    individual searches and the total regex work of one validation
    run.  No third-party regular-expression dependency is used.

    Arguments:

        budget:
            the call- and validation-level limits to enforce
        max_workers:
            maximum number of concurrently live worker processes
    """

    def __init__(
        self,
        budget: RegexBudget,
        *,
        max_workers: int = 1,
        context=None,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        self.budget = budget
        self._pool = _BoundedProcessPool(max_workers, context=context)

    @contextmanager
    def validation(self):
        run = _RunBudget(
            budget=self.budget,
            deadline=time.monotonic() + self.budget.total_timeout,
        )
        token = _ACTIVE_BUDGET.set(run)
        try:
            yield
        finally:
            _ACTIVE_BUDGET.reset(token)

    def search(self, pattern: str, string: str) -> bool:
        run = _ACTIVE_BUDGET.get()
        if run is not None:
            if (
                run.budget.max_calls is not None
                and run.calls >= run.budget.max_calls
            ):
                raise RegexTimeout("call quota exceeded")
            if time.monotonic() >= run.deadline:
                raise RegexTimeout("total timeout")
        total_deadline = run.deadline if run is not None else None
        try:
            status, payload = self._pool.submit(
                pattern,
                string,
                self.budget.call_timeout,
                total_deadline,
            )
        except _PoolTimeout as error:
            raise RegexTimeout(error.reason) from None
        if run is not None:
            run.calls += 1
        if status == "ok":
            return payload
        if status == "syntax":
            error = re.error(*payload)
            raise RegexSyntaxError(
                "invalid regular expression syntax",
            ) from error
        if status == "fatal":
            raise RegexExecutorError("a regex worker failed")
        raise RegexExecutorError(  # pragma: no cover
            "unexpected worker response",
        )

    def close(self) -> None:
        self._pool.close()
