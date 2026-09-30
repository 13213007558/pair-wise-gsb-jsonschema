"""
Versioned, snapshot-based schema registries.

This module adds an explicit snapshot/versioning and invalidation layer
on top of referencing.Registry. A VersionedRegistry hands out immutable
RegistrySnapshot objects. Each snapshot pins the registry version it was
taken at: resources resolved through it never change, even if the
registry is updated or invalidated afterwards. Taking a new snapshot
after an update is what makes subsequent $ref resolution see the new
resource versions, while other tenants may keep validating against
their older pinned snapshots.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urldefrag
import threading

from attrs import frozen
from referencing import Resource
import referencing
import referencing.exceptions
import referencing.jsonschema

from jsonschema import exceptions, validators

__all__ = [
    "LoadRecord",
    "RegistrySnapshot",
    "VersionedRegistry",
]


_KNOWN_SPECIFICATIONS = {
    specification.name: specification
    for specification in (
        referencing.jsonschema.DRAFT202012,
        referencing.jsonschema.DRAFT201909,
        referencing.jsonschema.DRAFT7,
        referencing.jsonschema.DRAFT6,
        referencing.jsonschema.DRAFT4,
        referencing.jsonschema.DRAFT3,
        referencing.Specification.OPAQUE,
    )
}


def _normalize(uri: str) -> str:
    """
    Normalize a URI the same way resolution does (empty fragments dropped).
    """
    return urldefrag(uri)[0]


def _unwrap_snapshot_error(
    error: BaseException,
) -> exceptions.RegistrySnapshotError | None:
    """
    Find the deepest snapshot-specific error in a wrapped cause chain.
    """
    found = None
    seen: BaseException | None = error
    while seen is not None:
        if isinstance(seen, exceptions.RegistrySnapshotError):
            found = seen
        seen = seen.__cause__
    return found


@frozen
class LoadRecord:
    """
    A record of a single resource load performed for a snapshot.

    The combination of sequence, snapshot and version makes loads of
    different resources -- or of a new version of the same resource --
    distinguishable from each other in a registry load log.
    """

    #: A registry-wide monotonically increasing load number.
    sequence: int

    #: The token of the snapshot the load was performed for.
    snapshot: int

    #: The registry version the loading snapshot is pinned at.
    version: int

    #: The (normalized) URI which was loaded.
    uri: str

    #: The loaded resource, or None if the load failed.
    resource: Resource | None = None

    #: The error the load failed with, or None if it succeeded.
    error: Exception | None = None


class RegistrySnapshot:
    """
    An immutable, version-pinned view of a VersionedRegistry.

    A single validation call (and all reference resolution it performs)
    is fixed to the snapshot it uses; switching versions happens between
    calls, by taking a new snapshot from the registry. Caches are never
    shared across snapshots or across registry instances, so a cache hit
    can never serve a resource belonging to a different version.

    Snapshots are safe to share between threads. Concurrent validations
    through the same snapshot load each resource at most once.

    Pickling a snapshot preserves its version, its resources and its
    cached loads, but detaches it from its originating registry: loads
    performed after unpickling are recorded in the snapshot's own
    load_log only. The retrieve callable and resource contents must
    themselves be picklable, and resources are rebuilt from their
    contents (with the registry's default specification) when restored.
    """

    __slots__ = (
        "_backend",
        "_cache",
        "_default_specification",
        "_guard",
        "_inflight",
        "_load_locks",
        "_loads",
        "_local_sequence",
        "_record",
        "_retrieve",
        "_store",
        "_token",
        "_version",
    )

    def __init__(
        self,
        *,
        version: int,
        token: int,
        store: Mapping[str, Resource],
        retrieve: Callable[[str], Any] | None,
        default_specification: referencing.Specification,
        record: Callable[..., LoadRecord] | None,
    ) -> None:
        self._version = version
        self._token = token
        self._store = store
        self._retrieve = retrieve
        self._default_specification = default_specification
        self._record = record
        self._loads: list[LoadRecord] = []
        self._cache: dict[str, Resource] = {}
        self._load_locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._inflight = threading.local()
        self._backend: referencing.Registry | None = None
        self._local_sequence = 0

    def __repr__(self) -> str:
        return (
            f"<RegistrySnapshot(version={self._version}, "
            f"token={self._token})>"
        )

    @property
    def version(self) -> int:
        """
        The registry version this snapshot is pinned at.
        """
        return self._version

    @property
    def token(self) -> int:
        """
        A token uniquely identifying this snapshot within its registry.
        """
        return self._token

    @property
    def load_log(self) -> tuple[LoadRecord, ...]:
        """
        The loads performed (so far) for this specific snapshot.
        """
        return tuple(self._loads)

    @property
    def registry(self) -> referencing.Registry:
        """
        A referencing.Registry pinned to this snapshot.

        Suitable for passing to validator classes via their registry
        argument.
        """
        backend = self._backend
        if backend is None:
            with self._guard:
                backend = self._backend
                if backend is None:
                    backend = referencing.Registry(retrieve=self._load)
                    known = {**self._cache, **self._store}
                    if known:
                        backend = backend.with_resources(
                            known.items(),
                        )
                    self._backend = backend
        return backend

    def resolver(self) -> referencing.Resolver:
        """
        Create a new resolver bound to this snapshot.
        """
        return self.registry.resolver()

    def validator_for(self, schema, cls=None, **kwargs):
        """
        Create a validator for the schema, bound to this snapshot.
        """
        if cls is None:
            cls = validators.validator_for(schema)
        return cls(schema, registry=self.registry, **kwargs)

    def iter_errors(self, instance, schema, cls=None, **kwargs):
        """
        Lazily yield validation errors, using this snapshot for $ref.

        Resource loading failures are re-raised as
        jsonschema.exceptions.ResourceLoadError and reference cycles as
        jsonschema.exceptions.CircularReferenceError rather than opaque
        lower-level errors.
        """
        validator = self.validator_for(schema, cls=cls, **kwargs)
        try:
            yield from validator.iter_errors(instance)
        except RecursionError as error:
            raise exceptions.CircularReferenceError() from error
        except referencing.exceptions.Unresolvable as error:
            unwrapped = _unwrap_snapshot_error(error)
            if unwrapped is None:
                raise
            raise unwrapped from error

    def validate(self, instance, schema, cls=None, **kwargs) -> None:
        """
        Validate an instance against a schema, using this snapshot.
        """
        error = exceptions.best_match(
            self.iter_errors(instance, schema, cls=cls, **kwargs),
        )
        if error is not None:
            raise error

    def _log(self, uri, resource, error) -> LoadRecord:
        if self._record is not None:
            record = self._record(
                uri=uri,
                version=self._version,
                token=self._token,
                resource=resource,
                error=error,
            )
        else:
            self._local_sequence += 1
            record = LoadRecord(
                sequence=self._local_sequence,
                snapshot=self._token,
                version=self._version,
                uri=uri,
                resource=resource,
                error=error,
            )
        self._loads.append(record)
        return record

    def _load(self, uri: str) -> Resource:
        uri = _normalize(uri)
        cached = self._cache.get(uri)
        if cached is not None:
            return cached

        inflight = getattr(self._inflight, "uris", None)
        if inflight is None:
            inflight = self._inflight.uris = []
        if uri in inflight:
            error = exceptions.CircularReferenceError(
                uri=uri,
                chain=[*inflight, uri],
            )
            self._log(uri, None, error)
            raise error

        with self._guard:
            lock = self._load_locks.setdefault(uri, threading.Lock())

        with lock:
            cached = self._cache.get(uri)
            if cached is not None:
                return cached
            if self._retrieve is None:
                error = referencing.exceptions.NoSuchResource(ref=uri)
                self._log(uri, None, error)
                raise error
            inflight.append(uri)
            try:
                result = self._retrieve(uri)
                if isinstance(result, Resource):
                    resource = result
                else:
                    resource = Resource.from_contents(
                        result,
                        default_specification=self._default_specification,
                    )
            except exceptions.RegistrySnapshotError:
                raise
            except (
                referencing.exceptions.CannotDetermineSpecification,
                referencing.exceptions.NoSuchResource,
            ) as error:
                self._log(uri, None, error)
                raise
            except Exception as cause:
                error = exceptions.ResourceLoadError(
                    uri=uri,
                    version=self._version,
                    cause=cause,
                )
                self._log(uri, None, error)
                raise error from cause
            finally:
                inflight.pop()
            self._cache[uri] = resource
            self._log(uri, resource, None)
            with self._guard:
                if self._backend is not None:
                    self._backend = self._backend.with_resource(
                        uri,
                        resource,
                    )
            return resource

    def __copy__(self) -> RegistrySnapshot:
        copied = self.__class__.__new__(self.__class__)
        for slot in self.__slots__:
            setattr(copied, slot, getattr(self, slot))
        return copied

    def __deepcopy__(self, memo) -> RegistrySnapshot:
        copied = self.__class__.__new__(self.__class__)
        memo[id(self)] = copied
        copied._version = self._version
        copied._token = self._token
        copied._store = self._store
        copied._retrieve = self._retrieve
        copied._default_specification = self._default_specification
        copied._record = self._record
        copied._loads = list(self._loads)
        copied._cache = dict(self._cache)
        copied._load_locks = {}
        copied._guard = threading.Lock()
        copied._inflight = threading.local()
        copied._backend = None
        copied._local_sequence = self._local_sequence
        return copied

    def __getstate__(self) -> dict:
        return {
            "version": self._version,
            "token": self._token,
            "store": {
                uri: resource.contents
                for uri, resource in self._store.items()
            },
            "cache": {
                uri: resource.contents
                for uri, resource in self._cache.items()
            },
            "retrieve": self._retrieve,
            "specification": self._default_specification.name,
        }

    def __setstate__(self, state: dict) -> None:
        specification = _KNOWN_SPECIFICATIONS.get(state["specification"])
        if specification is None:
            raise exceptions.RegistrySnapshotError(
                f"cannot restore a snapshot whose default specification "
                f"{state['specification']!r} is not a known specification",
            )
        self._version = state["version"]
        self._token = state["token"]
        self._store = self._resources_from(state["store"], specification)
        self._cache = self._resources_from(state["cache"], specification)
        self._retrieve = state["retrieve"]
        self._default_specification = specification
        self._record = None
        self._loads = []
        self._load_locks = {}
        self._guard = threading.Lock()
        self._inflight = threading.local()
        self._backend = None
        self._local_sequence = 0

    @staticmethod
    def _resources_from(contents_by_uri, specification):
        return {
            uri: Resource.from_contents(
                contents,
                default_specification=specification,
            )
            for uri, contents in contents_by_uri.items()
        }


class VersionedRegistry:
    """
    A schema registry with explicit snapshots, versions and invalidation.

    Resources are either set explicitly via set_resource or loaded on
    demand through the retrieve callable. Every mutation bumps the
    registry's version. Validation happens against RegistrySnapshot
    objects taken via snapshot(): a snapshot keeps serving exactly the
    resources of the version it was taken at, while new snapshots see
    newer versions. invalidate() drops resources (or, with no
    arguments, all of them) and bumps the version, so that subsequent
    snapshots reload them from their source instead of silently reusing
    a stale cached copy.

    All methods are safe to call concurrently. Callers performing
    read-modify-write sequences may pass expected_version to detect
    concurrent updates; a mismatch raises
    jsonschema.exceptions.VersionConflictError.

    Loads performed for snapshots of this registry are recorded in
    load_log, each with the snapshot token and registry version, so
    that old and new loads of the same URI can be told apart.
    """

    __slots__ = (
        "_default_specification",
        "_lock",
        "_log",
        "_retrieve",
        "_sequence",
        "_snapshot_tokens",
        "_store",
        "_version",
    )

    def __init__(
        self,
        retrieve: Callable[[str], Any] | None = None,
        *,
        default_specification: referencing.Specification = (
            referencing.jsonschema.DRAFT202012
        ),
    ) -> None:
        self._retrieve = retrieve
        self._default_specification = default_specification
        self._lock = threading.Lock()
        self._log: list[LoadRecord] = []
        self._sequence = 0
        self._snapshot_tokens = 0
        self._store: dict[str, Resource] = {}
        self._version = 0

    def __repr__(self) -> str:
        return (
            f"<VersionedRegistry(version={self._version}, "
            f"resources={len(self._store)})>"
        )

    @property
    def version(self) -> int:
        """
        The current version of the registry.
        """
        return self._version

    @property
    def load_log(self) -> tuple[LoadRecord, ...]:
        """
        The loads performed for snapshots of this registry, in order.
        """
        with self._lock:
            return tuple(self._log)

    def clear_load_log(self) -> None:
        """
        Forget all load records accumulated so far.
        """
        with self._lock:
            self._log.clear()

    def snapshot(self) -> RegistrySnapshot:
        """
        Take an immutable snapshot of the registry at its current version.
        """
        with self._lock:
            self._snapshot_tokens += 1
            return RegistrySnapshot(
                version=self._version,
                token=self._snapshot_tokens,
                store=self._store,
                retrieve=self._retrieve,
                default_specification=self._default_specification,
                record=self._record_load,
            )

    def set_resource(
        self,
        uri: str,
        resource: Resource | Mapping,
        *,
        expected_version: int | None = None,
    ) -> int:
        """
        Add or replace a resource, bumping the registry version.

        Existing snapshots are unaffected; snapshots taken afterwards
        resolve the new resource for the URI.
        """
        uri = _normalize(uri)
        if not isinstance(resource, Resource):
            resource = Resource.from_contents(
                resource,
                default_specification=self._default_specification,
            )
        with self._lock:
            self._check_version(expected_version)
            self._store = {**self._store, uri: resource}
            self._version += 1
            return self._version

    def invalidate(
        self,
        *uris: str,
        expected_version: int | None = None,
    ) -> int:
        """
        Drop resources and bump the registry version.

        With no arguments, all explicitly set resources are dropped.
        Snapshots taken afterwards reload the URIs from the registry's
        retrieval logic (or fail with an identifiable error), never
        from a stale cache. Already taken snapshots are unaffected.
        """
        with self._lock:
            self._check_version(expected_version)
            if uris:
                doomed = {_normalize(uri) for uri in uris}
                self._store = {
                    uri: resource
                    for uri, resource in self._store.items()
                    if uri not in doomed
                }
            else:
                self._store = {}
            self._version += 1
            return self._version

    def _check_version(self, expected_version: int | None) -> None:
        if (
            expected_version is not None
            and expected_version != self._version
        ):
            raise exceptions.VersionConflictError(
                expected=expected_version,
                actual=self._version,
            )

    def _record_load(self, *, uri, version, token, resource, error):
        with self._lock:
            self._sequence += 1
            record = LoadRecord(
                sequence=self._sequence,
                snapshot=token,
                version=version,
                uri=uri,
                resource=resource,
                error=error,
            )
            self._log.append(record)
            return record

    def __copy__(self) -> VersionedRegistry:
        copied = self.__class__.__new__(self.__class__)
        with self._lock:
            copied._retrieve = self._retrieve
            copied._default_specification = self._default_specification
            copied._sequence = self._sequence
            copied._snapshot_tokens = self._snapshot_tokens
            copied._store = self._store
            copied._version = self._version
        copied._lock = threading.Lock()
        copied._log = []
        return copied

    def __deepcopy__(self, memo) -> VersionedRegistry:
        copied = self.__class__.__new__(self.__class__)
        memo[id(self)] = copied
        with self._lock:
            copied._retrieve = self._retrieve
            copied._default_specification = self._default_specification
            copied._sequence = self._sequence
            copied._snapshot_tokens = self._snapshot_tokens
            copied._store = dict(self._store)
            copied._version = self._version
        copied._lock = threading.Lock()
        copied._log = list(self._log)
        return copied

    def __getstate__(self) -> dict:
        with self._lock:
            return {
                "version": self._version,
                "store": {
                    uri: resource.contents
                    for uri, resource in self._store.items()
                },
                "retrieve": self._retrieve,
                "specification": self._default_specification.name,
                "sequence": self._sequence,
                "snapshot_tokens": self._snapshot_tokens,
            }

    def __setstate__(self, state: dict) -> None:
        specification = _KNOWN_SPECIFICATIONS.get(state["specification"])
        if specification is None:
            raise exceptions.RegistrySnapshotError(
                f"cannot restore a registry whose default specification "
                f"{state['specification']!r} is not a known specification",
            )
        self._retrieve = state["retrieve"]
        self._default_specification = specification
        self._lock = threading.Lock()
        self._log = []
        self._sequence = state["sequence"]
        self._snapshot_tokens = state["snapshot_tokens"]
        self._store = RegistrySnapshot._resources_from(
            state["store"],
            specification,
        )
        self._version = state["version"]
