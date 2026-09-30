"""
Configurable reference retrieval policy.

Coordinates URI normalization, per-request resource budgets, tenant-scoped
caching and in-flight retrieval de-duplication for ``$ref`` /
``$dynamicRef`` resolution during validation.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from concurrent.futures import (
    ThreadPoolExecutor,
    TimeoutError as FutureTimeout,
)
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
from enum import Enum
from threading import Event, Lock, Semaphore
from urllib.parse import urldefrag, urlsplit, urlunsplit
import itertools
import json
import threading
import time
from typing import Any, Union

from attrs import define, field as attrs_field
import referencing
import referencing.jsonschema
import referencing.exceptions

from jsonschema import exceptions

_JSON_MEDIA_TYPES = frozenset(
    {"application/json", "application/schema+json"},
)

_request_counter = itertools.count()

_current_request: ContextVar = ContextVar(
    "_current_retrieval_request",
    default=None,
)
_reference_stack: ContextVar = ContextVar(
    "_reference_stack",
    default=(),
)

def normalize_uri(uri):
    """
    Normalize a URI for use as a retrieval/cache key.

    Fragments are stripped (they address locations *within* a document),
    schemes and hosts are lowercased, default ports are removed, and an
    empty trailing fragment is treated as equivalent to the document URI.
    """
    if not isinstance(uri, str):
        raise exceptions.InvalidReference(
            f"References must be strings, got {uri!r}.",
            repr(uri),
        )
    document_uri, _fragment = urldefrag(uri)
    try:
        split = urlsplit(document_uri)
    except ValueError as error:
        raise exceptions.InvalidReference(
            f"Reference {uri!r} is not a valid URI.",
            uri,
            cause=error,
        ) from error

    hostname = split.hostname or ""
    netloc = hostname.lower() if hostname else split.netloc
    if split.port and split.port not in (80, 443):
        netloc = f"{netloc}:{split.port}"
    normalized = urlunsplit(
        (
            split.scheme.lower(),
            netloc,
            split.path or ("/" if netloc else split.path),
            split.query,
            "",
        ),
    )
    normalized = normalized.rstrip("#")
    return normalized or document_uri.rstrip("#")


@define(frozen=True)
class RetrievalBudget:
    """
    Resource limits applied to a single validation request.

    Arguments:

        max_depth:
            maximum number of nested reference dereferences on a branch
        max_documents:
            maximum number of *new* documents retrieved during the request
            (cache hits and in-memory registry resources do not count)
        max_bytes:
            maximum aggregate serialized size of newly retrieved documents
        max_connections:
            maximum number of retrievals in flight at once
        max_redirects:
            maximum number of redirects followed for a single retrieval
        max_cycle:
            length of a repeating reference chain considered a
            non-terminating cycle

    Any limit may be ``None`` to disable the corresponding budget.
    """

    max_depth: int | None = 64
    max_documents: int | None = 128
    max_bytes: int | None = 16 * 1024 * 1024
    max_connections: int | None = 8
    max_redirects: int | None = 10
    max_cycle: int | None = 64

    def __attrs_post_init__(self):
        for name in (
            "max_depth",
            "max_documents",
            "max_bytes",
            "max_connections",
            "max_redirects",
            "max_cycle",
        ):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(
                    f"{name} must be a positive integer or None.",
                )

class EventKind(Enum):
    """Kinds of events recorded in a request's retrieval audit log."""

    CACHE_HIT = "cache_hit"
    IN_FLIGHT_SHARED = "in_flight_shared"
    RETRIEVE_START = "retrieve_start"
    REDIRECT = "redirect"
    RETRIEVE_OK = "retrieve_ok"
    RETRIEVE_ERROR = "retrieve_error"
    BUDGET_EXCEEDED = "budget_exceeded"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    INVALIDATED = "invalidated"


@define(frozen=True)
class RetrievalEvent:
    """An auditable event in the lifecycle of a reference retrieval."""

    kind: EventKind
    uri: str
    canonical_uri: str | None = None
    detail: Mapping[str, Any] = attrs_field(factory=dict, converter=dict)


@define(frozen=True)
class RetrievedDocument:
    """
    The result of a user-provided retrieval callable.

    A retriever may return one of these (or a plain
    `referencing.Resource`, mapping or JSON string/bytes, which are adapted
    automatically).
    """

    contents: Any = None
    canonical_uri: str | None = None
    media_type: str | None = None
    raw: bytes | str | None = None
    resource: referencing.Resource | None = None
    redirect_to: str | None = None


@dataclass
class _CacheEntry:
    resource: referencing.Resource
    canonical_uri: str
    expires_at: float | None
    media_type: str | None
    num_bytes: int


class _Inflight:
    __slots__ = ("event", "error", "entry")

    def __init__(self):
        self.event = Event()
        self.error: BaseException | None = None
        self.entry: _CacheEntry | None = None


@define
class RetrievalRequest:
    """
    Per-validation retrieval state.

    Owns the budget counters and audit log for a single top-level
    validation. Entered via `RetrievalPolicy.request` (or implicitly by
    the validator); it never mutates validator objects itself.
    """

    policy: "RetrievalPolicy"
    request_id: int
    budget: RetrievalBudget
    context: Mapping[str, Any] = attrs_field(factory=dict)
    cancel_event: threading.Event = attrs_field(factory=threading.Event)
    deadline: float | None = None

    documents: int = attrs_field(default=0, init=False)
    bytes_used: int = attrs_field(default=0, init=False)
    connections: int = attrs_field(default=0, init=False)
    events: deque = attrs_field(factory=deque, init=False, repr=False)
    _seen: set = attrs_field(factory=set, init=False, repr=False)
    _lock: Lock = attrs_field(factory=Lock, init=False, repr=False)
    _token: Any = attrs_field(default=None, init=False, repr=False)
    _stack_token: Any = attrs_field(default=None, init=False, repr=False)

    def audit(self):
        """Return the ordered list of retrieval events recorded so far."""
        with self._lock:
            return tuple(self.events)

    def cancel(self):
        """Signal cancellation to any retrieval in flight for this request."""
        self.cancel_event.set()

    @property
    def cancelled(self):
        return self.cancel_event.is_set()

    def remaining_time(self):
        if self.deadline is None:
            return None
        return max(0.0, self.deadline - time.monotonic())

    def _record(self, event):
        with self._lock:
            self.events.append(event)

    def _note_seen(self, canonical_uri):
        with self._lock:
            self._seen.add(canonical_uri)

    def seen_documents(self):
        with self._lock:
            return frozenset(self._seen)


RetrieveCallable = Callable[
    ...,
    Union[RetrievedDocument, referencing.Resource, Mapping, bytes, str],
]


class RetrievalPolicy:
    """
    A configurable, tenant-scoped reference retrieval policy.

    A policy wraps a user supplied retriever with URI normalization,
    a tenant-local TTL cache, per-request budgets (depth, documents, bytes,
    connections, redirects), in-flight de-duplication, cancellation and
    timeouts (which never populate caches or registries with partial
    results), and a per-request audit trail.

    Construct one policy per tenant; distinct policies never share caches.
    """

    def __init__(
        self,
        retrieve=None,
        *,
        budget=None,
        tenant=None,
        ttl=None,
        timeout=None,
        allowed_media_types=frozenset({"application/json"}),
        loads=None,
    ):
        self._user_retrieve = retrieve
        self._retrieve_takes_request = self._accepts_request(retrieve)
        self.budget = budget or RetrievalBudget()
        self.tenant = tenant
        self.ttl = ttl
        self.timeout = timeout
        self.allowed_media_types = frozenset(allowed_media_types)
        self._loads = loads or json.loads

        self._cache = {}
        self._cache_lock = Lock()
        self._inflight = {}
        self._inflight_lock = Lock()
        self._key_locks = {}
        self._key_locks_guard = Lock()
        # Only bound concurrency when a limit is configured.
        max_connections = self.budget.max_connections
        self._connections = (
            Semaphore(max_connections)
            if max_connections is not None
            else None
        )
        self._executor = ThreadPoolExecutor(
            max_workers=max_connections or 32,
            thread_name_prefix="jsonschema-ref-retrieve",
        )

    @staticmethod
    def _accepts_request(retrieve):
        if retrieve is None:
            return False
        try:
            import inspect

            signature = inspect.signature(retrieve)
        except (TypeError, ValueError):
            return False
        kinds = (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )
        parameters = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind in kinds
        ]
        return len(parameters) >= 2 or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )

    def configure_registry(self, registry=None):
        """
        Return a registry whose retriever is coordinated with this policy.

        All resources, anchors and uncrawled entries of ``registry`` are
        preserved; only its dynamic retrieval is replaced (and chained to
        the original registry retriever, if it had one).
        """
        inner = registry if registry is not None else referencing.Registry()
        return referencing.Registry(
            resources=inner._resources,
            anchors=inner._anchors,
            uncrawled=inner._uncrawled,
            retrieve=self.as_retrieve(inner._retrieve),
        )

    def as_retrieve(self, fallback=None):
        """
        Return a callable suitable for ``Registry(retrieve=...)``.

        ``fallback`` is invoked for URIs the user retriever does not know
        about (e.g. to retain a pre-existing registry retriever).
        """
        return _PolicyRetriever(self, fallback)

    def invalidate(self, uri=None):
        """
        Drop expired entries, a specific URI, or the whole tenant cache.
        """
        with self._cache_lock:
            if uri is None:
                removed = list(self._cache)
                self._cache.clear()
            else:
                normalized = normalize_uri(uri)
                removed = [normalized] if normalized in self._cache else []
                self._cache.pop(normalized, None)
        request = _current_request.get()
        if request is not None:
            for each in removed:
                request._record(
                    RetrievalEvent(
                        kind=EventKind.INVALIDATED,
                        uri=each,
                        canonical_uri=each,
                    ),
                )

    def _purge_expired(self, now):
        expired = [
            uri
            for uri, entry in self._cache.items()
            if entry.expires_at is not None and entry.expires_at <= now
        ]
        for uri in expired:
            self._cache.pop(uri, None)
        return expired

    def _key_lock(self, key):
        with self._key_locks_guard:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = Lock()
                self._key_locks[key] = lock
            return lock

    @contextmanager
    def request(
        self,
        *,
        budget=None,
        context=None,
        timeout=None,
        cancel_event=None,
    ):
        """
        Enter a per-validation request scope.

        Yields a `RetrievalRequest` whose budget counters, audit log and
        in-flight de-duplication are isolated to this request (and therefore
        to this validation), even when the same policy is shared across
        threads or tenants.
        """
        timeout = self.timeout if timeout is None else timeout
        current = RetrievalRequest(
            policy=self,
            request_id=next(_request_counter),
            budget=budget or self.budget,
            context=dict(context or {}),
            cancel_event=cancel_event or threading.Event(),
            deadline=(
                time.monotonic() + timeout if timeout is not None else None
            ),
        )
        token = _current_request.set(current)
        stack_token = _reference_stack.set(())
        current._token = token
        current._stack_token = stack_token
        try:
            yield current
        finally:
            if _reference_stack.get() is not None:
                _reference_stack.reset(stack_token)
            _current_request.reset(token)

    def current_request(self):
        """Return the active `RetrievalRequest` in this context, if any."""
        return _current_request.get()

    # ------------------------------------------------------------------
    # Core retrieval
    # ------------------------------------------------------------------

    def retrieve_resource(self, uri, *, fallback=None):
        """
        Retrieve (and cache) the document for ``uri`` as a Resource.

        Honors the active request's budgets, timeout, cancellation and
        audit log. Raises a subclass of
        `jsonschema.exceptions._ReferencingError` on failure; such failures
        never populate the cache or registry.
        """
        request = _current_request.get()
        if request is None:
            # Outside of a validation request: run with defaults but no
            # cross-request sharing of mutable state.
            with self.request() as implicit:
                return self.retrieve_resource(uri, fallback=fallback)

        if request.cancelled:
            error = exceptions.ReferenceCancelled(
                f"Reference retrieval was cancelled before resolving {uri!r}.",
                uri,
            )
            request._record(
                RetrievalEvent(kind=EventKind.CANCELLED, uri=uri),
            )
            raise error

        normalized = normalize_uri(uri)
        now = time.monotonic()

        with self._cache_lock:
            expired = self._purge_expired(now)
            entry = self._cache.get(normalized)
            if entry is not None and (
                entry.expires_at is None or entry.expires_at > now
            ):
                request._record(
                    RetrievalEvent(
                        kind=EventKind.CACHE_HIT,
                        uri=uri,
                        canonical_uri=entry.canonical_uri,
                    ),
                )
                return entry.resource, entry.canonical_uri

        # Within a single request, concurrent refs share one retrieval.
        inflight_key = (request.request_id, normalized)
        with self._inflight_lock:
            inflight = self._inflight.get(inflight_key)
            if inflight is None:
                inflight = _Inflight()
                self._inflight[inflight_key] = inflight
                leader = True
            else:
                leader = False

        if not leader:
            self._wait_for_inflight(request, inflight, uri, normalized)
            return inflight.entry.resource, inflight.entry.canonical_uri

        try:
            entry = self._fetch_guarded(
                request=request,
                uri=uri,
                normalized=normalized,
                fallback=fallback,
            )
        except BaseException as error:
            inflight.error = error
            inflight.event.set()
            with self._inflight_lock:
                self._inflight.pop(inflight_key, None)
            raise
        else:
            inflight.entry = entry
            inflight.event.set()
            with self._inflight_lock:
                self._inflight.pop(inflight_key, None)
            return entry.resource, entry.canonical_uri

    def _wait_for_inflight(self, request, inflight, uri, normalized):
        remaining = request.remaining_time()
        if not inflight.event.wait(timeout=remaining):
            request._record(
                RetrievalEvent(kind=EventKind.TIMEOUT, uri=uri),
            )
            raise exceptions.ReferenceTimeout(
                (
                    f"Timed out after {remaining!r}s waiting for an in-flight "
                    f"retrieval of {uri!r}."
                ),
                uri,
            )
        if request.cancelled:
            request._record(
                RetrievalEvent(kind=EventKind.CANCELLED, uri=uri),
            )
            raise exceptions.ReferenceCancelled(
                (
                    "Reference retrieval was cancelled while waiting "
                    f"for {uri!r}."
                ),
                uri,
            )
        if inflight.error is not None:
            request._record(
                RetrievalEvent(
                    kind=EventKind.IN_FLIGHT_SHARED,
                    uri=uri,
                    canonical_uri=normalized,
                    detail={"shared_error": True},
                ),
            )
            raise inflight.error
        request._record(
            RetrievalEvent(
                kind=EventKind.IN_FLIGHT_SHARED,
                uri=uri,
                canonical_uri=normalized,
            ),
        )

    def _fetch_guarded(self, *, request, uri, normalized, fallback):
        budget = request.budget
        max_connections = budget.max_connections
        abandoned = Event()

        if request.cancelled:
            request._record(
                RetrievalEvent(kind=EventKind.CANCELLED, uri=uri),
            )
            raise exceptions.ReferenceCancelled(
                (
                    "Reference retrieval was cancelled before acquiring "
                    f"a connection for {uri!r}."
                ),
                uri,
            )
        remaining = request.remaining_time()
        if remaining is not None and remaining <= 0:
            request._record(
                RetrievalEvent(kind=EventKind.TIMEOUT, uri=uri),
            )
            raise exceptions.ReferenceTimeout(
                (
                    "Timed out before a connection was available to "
                    f"retrieve {uri!r}."
                ),
                uri,
            )

        # Connection budget: fail deterministically (and distinguishably) at
        # the boundary rather than queueing behind an unknown workload.
        connection = self._connections
        if connection is not None and not connection.acquire(blocking=False):
            request._record(
                RetrievalEvent(
                    kind=EventKind.BUDGET_EXCEEDED,
                    uri=uri,
                    detail={
                        "budget": "connections",
                        "limit": max_connections,
                    },
                ),
            )
            raise exceptions.ConnectionBudgetExceeded(
                uri,
                limit=max_connections,
            )

        if max_connections is not None:
            with request._lock:
                request.connections += 1
        context = copy_context()

        def run_fetch():
            return self._fetch_with_redirects(
                request=request,
                initial_uri=uri,
                normalized=normalized,
                fallback=fallback,
                abandoned=abandoned,
            )

        future = self._executor.submit(
            context.run,
            run_fetch,
        )
        try:
            remaining = request.remaining_time()
            entry = future.result(timeout=remaining)
        except FutureTimeout as error:
            abandoned.set()
            request._record(
                RetrievalEvent(kind=EventKind.TIMEOUT, uri=uri),
            )
            raise exceptions.ReferenceTimeout(
                f"Timed out while retrieving {uri!r}.",
                uri,
                cause=error,
            ) from error
        finally:
            if connection is not None:
                connection.release()
                with request._lock:
                    request.connections = max(0, request.connections - 1)
        return entry

    def _fetch_with_redirects(
        self,
        request,
        initial_uri,
        normalized,
        fallback,
        abandoned,
    ):
        # Serialize cross-request duplicate fetches on the same normalized
        # URI: only one underlying retrieve call is ever in flight for it.
        key_lock = self._key_lock(normalized)
        with key_lock:
            # The document may have been fetched by another request while we
            # were waiting for the per-URI lock.
            now = time.monotonic()
            with self._cache_lock:
                entry = self._cache.get(normalized)
                if entry is not None and (
                    entry.expires_at is None or entry.expires_at > now
                ):
                    request._record(
                        RetrievalEvent(
                            kind=EventKind.CACHE_HIT,
                            uri=initial_uri,
                            canonical_uri=entry.canonical_uri,
                        ),
                    )
                    return entry
            return self._fetch_once_redirects(
                request,
                initial_uri,
                normalized,
                fallback,
                abandoned,
            )

    def _fetch_once_redirects(
        self,
        request,
        initial_uri,
        normalized,
        fallback,
        abandoned,
    ):
        budget = request.budget
        requested = initial_uri
        visited = [normalized]
        redirects = 0
        document = None
        media_type = None
        result_canonical = normalized
        raw = None

        while True:
            if abandoned.is_set():
                raise exceptions.ReferenceTimeout(
                    (
                        f"Retrieval of {requested!r} was abandoned "
                        "after a timeout."
                    ),
                    requested,
                )
            if request.cancelled:
                request._record(
                    RetrievalEvent(kind=EventKind.CANCELLED, uri=requested),
                )
                raise exceptions.ReferenceCancelled(
                    (
                        "Reference retrieval was cancelled while "
                        f"retrieving {requested!r}."
                    ),
                    requested,
                )
            remaining = request.remaining_time()
            if remaining is not None and remaining <= 0:
                request._record(
                    RetrievalEvent(kind=EventKind.TIMEOUT, uri=requested),
                )
                raise exceptions.ReferenceTimeout(
                    f"Timed out while retrieving {requested!r}.",
                    requested,
                )
            request._record(
                RetrievalEvent(
                    kind=EventKind.RETRIEVE_START,
                    uri=requested,
                    canonical_uri=result_canonical,
                    detail={"redirect": redirects},
                ),
            )
            try:
                result = self._call_user_retrieve(requested, request, fallback)
            except exceptions._ReferencingError:
                raise
            except referencing.exceptions.NoSuchResource as error:
                request._record(
                    RetrievalEvent(
                        kind=EventKind.RETRIEVE_ERROR,
                        uri=requested,
                    ),
                )
                raise
            except Exception as error:
                if request.cancelled:
                    request._record(
                        RetrievalEvent(
                            kind=EventKind.CANCELLED,
                            uri=requested,
                        ),
                    )
                    raise exceptions.ReferenceCancelled(
                        (
                            "Reference retrieval was cancelled while "
                            f"retrieving {requested!r}."
                        ),
                        requested,
                        cause=error,
                    ) from error
                request._record(
                    RetrievalEvent(
                        kind=EventKind.RETRIEVE_ERROR,
                        uri=requested,
                        detail={"error": error.__class__.__name__},
                    ),
                )
                raise exceptions.ReferenceRetrievalError(
                    (
                        f"Failed to retrieve {requested!r}: "
                        f"{error.__class__.__name__}: {error}"
                    ),
                    requested,
                    cause=error,
                ) from error

            adapted = self._adapt(result, requested)
            (
                document,
                raw,
                media_type,
                redirect_to,
                resource,
                declared,
            ) = adapted

            if redirect_to is not None:
                redirects += 1
                max_redirects = budget.max_redirects
                next_normalized = normalize_uri(redirect_to)
                if next_normalized in visited:
                    cycle = visited + [next_normalized]
                    request._record(
                        RetrievalEvent(
                            kind=EventKind.BUDGET_EXCEEDED,
                            uri=requested,
                            detail={"budget": "cycle", "chain": cycle},
                        ),
                    )
                    raise exceptions.CyclicReferenceError(
                        initial_uri,
                        limit=budget.max_cycle or len(cycle),
                        chain=cycle,
                    )
                if max_redirects is not None and redirects > max_redirects:
                    request._record(
                        RetrievalEvent(
                            kind=EventKind.BUDGET_EXCEEDED,
                            uri=requested,
                            detail={"budget": "redirects", "used": redirects},
                        ),
                    )
                    raise exceptions.RedirectBudgetExceeded(
                        initial_uri,
                        limit=max_redirects,
                        redirects=redirects,
                    )
                request._record(
                    RetrievalEvent(
                        kind=EventKind.REDIRECT,
                        uri=requested,
                        canonical_uri=next_normalized,
                        detail={"redirect": redirects},
                    ),
                )
                requested = redirect_to
                result_canonical = next_normalized
                visited.append(next_normalized)
                continue
            break

        if declared:
            result_canonical = normalize_uri(declared)
        return self._charge_and_cache(
            request=request,
            initial_uri=initial_uri,
            requested=requested,
            normalized=normalized,
            canonical=result_canonical,
            contents=document,
            raw=raw,
            media_type=media_type,
            resource=resource,
            abandoned=abandoned,
        )

    def _call_user_retrieve(self, uri, request, fallback):
        if self._user_retrieve is not None:
            if self._retrieve_takes_request:
                result = self._user_retrieve(uri, request)
            else:
                result = self._user_retrieve(uri)
            # A retriever may explicitly defer to the fallback registry
            # retriever by returning None.
            if result is not None:
                return result
        if fallback is not None:
            return fallback(uri)
        raise referencing.exceptions.NoSuchResource(ref=uri)

    def _adapt(self, result, requested):
        redirect_to = None
        declared_canonical = None
        media_type = None
        raw = None
        resource = None
        contents = None

        if isinstance(result, RetrievedDocument):
            redirect_to = result.redirect_to
            declared_canonical = result.canonical_uri
            media_type = result.media_type
            raw = result.raw
            resource = result.resource
            contents = result.contents
            if redirect_to is not None:
                return None, None, None, redirect_to, None, None
            if resource is not None:
                contents = resource.contents
                if media_type is None:
                    media_type = "application/schema+json"
                if raw is None:
                    raw = _estimate_bytes(contents)
            elif raw is not None:
                self._check_media_type(media_type, requested)
                try:
                    contents = self._loads(raw)
                except Exception as error:
                    raise exceptions.ReferenceRetrievalError(
                        (
                            "Failed to parse document retrieved from "
                            f"{requested!r}: {error}"
                        ),
                        requested,
                        cause=error,
                    ) from error
            else:
                if media_type is not None:
                    self._check_media_type(media_type, requested)
                raw = _estimate_bytes(contents)
        elif isinstance(result, referencing.Resource):
            resource = result
            contents = result.contents
            media_type = "application/schema+json"
            raw = _estimate_bytes(contents)
        elif isinstance(result, (bytes, str)):
            raw = result
            self._check_media_type(
                media_type or "application/json",
                requested,
            )
            try:
                contents = self._loads(raw)
            except Exception as error:
                raise exceptions.ReferenceRetrievalError(
                    (
                        "Failed to parse document retrieved from "
                        f"{requested!r}: {error}"
                    ),
                    requested,
                    cause=error,
                ) from error
        else:
            # Treat as already-deserialized contents (typically a mapping).
            contents = result
            media_type = "application/schema+json"
            raw = _estimate_bytes(contents)

        return contents, raw, media_type, None, resource, declared_canonical

    def _check_media_type(self, media_type, requested):
        if media_type is None:
            return
        primary = media_type.split(";", 1)[0].strip().lower()
        allowed = {each.lower() for each in self.allowed_media_types}
        if primary not in allowed and primary not in _JSON_MEDIA_TYPES:
            raise exceptions.UnsupportedMediaType(
                (
                    f"Retrieved document from {requested!r} has unsupported "
                    f"media type {media_type!r}; expected one of "
                    f"{sorted(allowed)!r}."
                ),
                requested,
                media_type=media_type,
            )

    def _charge_and_cache(
        self,
        *,
        request,
        initial_uri,
        requested,
        normalized,
        canonical,
        contents,
        raw,
        media_type,
        resource,
        abandoned,
    ):
        budget = request.budget
        if isinstance(raw, (bytes, str)):
            num_bytes = len(raw)
        else:
            num_bytes = _estimate_bytes(contents)

        # The caller may have timed out, or the request been cancelled while
        # the retrieval was running. Never charge budgets or write the cache
        # (and therefore never return a Resource to the registry) then.
        if abandoned.is_set():
            raise exceptions.ReferenceTimeout(
                f"Retrieval of {initial_uri!r} completed after its timeout.",
                initial_uri,
            )
        if request.cancelled:
            raise exceptions.ReferenceCancelled(
                f"Retrieval of {initial_uri!r} completed after cancellation.",
                initial_uri,
            )

        # Document budget is charged for a genuinely new fetch (a cache hit
        # returns earlier). Byte budget is checked against the new size.
        with request._lock:
            documents = request.documents + 1
            bytes_used = request.bytes_used + num_bytes
            if (
                budget.max_documents is not None
                and documents > budget.max_documents
            ):
                exceeded = (
                    "documents",
                    budget.max_documents,
                    documents,
                )
            elif (
                budget.max_bytes is not None
                and bytes_used > budget.max_bytes
            ):
                exceeded = (
                    "bytes",
                    budget.max_bytes,
                    bytes_used,
                )
            else:
                exceeded = None
                request.documents = documents
                request.bytes_used = bytes_used

        if exceeded is not None:
            kind, limit, used = exceeded
            request._record(
                RetrievalEvent(
                    kind=EventKind.BUDGET_EXCEEDED,
                    uri=initial_uri,
                    detail={
                        "budget": kind,
                        "limit": limit,
                        "used": used,
                        "document_size": num_bytes,
                    },
                ),
            )
            if kind == "documents":
                raise exceptions.DocumentBudgetExceeded(
                    initial_uri,
                    limit=limit,
                    documents=used,
                )
            raise exceptions.ByteBudgetExceeded(
                initial_uri,
                limit=limit,
                bytes_used=used,
                document_size=num_bytes,
            )

        if resource is None:
            resource = referencing.Resource.from_contents(
                contents,
                default_specification=referencing.jsonschema.DRAFT202012,
            )

        # Respect a canonical ``$id`` declared inside the document.
        inner_id = resource.id()
        if inner_id:
            canonical = normalize_uri(inner_id)

        expires_at = None
        if self.ttl is not None:
            expires_at = time.monotonic() + self.ttl
        entry = _CacheEntry(
            resource=resource,
            canonical_uri=canonical,
            expires_at=expires_at,
            media_type=media_type,
            num_bytes=num_bytes,
        )
        with self._cache_lock:
            self._cache[normalized] = entry
            if canonical != normalized:
                self._cache[canonical] = entry
        request._note_seen(normalized)
        request._record(
            RetrievalEvent(
                kind=EventKind.RETRIEVE_OK,
                uri=initial_uri,
                canonical_uri=canonical,
                detail={"bytes": num_bytes, "documents": documents},
            ),
        )
        return entry


def _estimate_bytes(contents):
    try:
        serialized = json.dumps(contents, separators=(",", ":"))
    except (TypeError, ValueError):
        serialized = repr(contents)
    return len(serialized.encode("utf-8"))


class _PolicyRetriever:
    """
    Adapts a `RetrievalPolicy` to the ``Registry(retrieve=...)`` protocol.
    """

    def __init__(self, policy, fallback=None):
        self.policy = policy
        self.fallback = fallback

    def __call__(self, uri):
        resource, _canonical = self.policy.retrieve_resource(
            uri,
            fallback=self.fallback,
        )
        return resource

    def __repr__(self):
        return f"<PolicyRetriever tenant={self.policy.tenant!r}>"


def current_request():
    """Return the `RetrievalRequest` active in the current context."""
    return _current_request.get()


def current_stack():
    """Return the current per-branch reference resolution stack."""
    return _reference_stack.get()
