"""
Version-aware reference registries.
"""

from __future__ import annotations

import copy
import inspect
import itertools
import threading

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from threading import Condition
from typing import Any, Callable

import referencing
import referencing.exceptions


class RegistryError(Exception):
    """Base class for versioned registry failures."""


class ResourceLoadError(RegistryError):
    """A loader could not produce a resource."""

    def __init__(self, uri: str, version: int, cause: BaseException):
        super().__init__(uri)
        self.ref = uri
        self.version = version
        self.original_error = cause

    def __str__(self) -> str:
        return (
            f"Could not load {self.ref!r} for registry version "
            f"{self.version}: {self.original_error}"
        )


class CircularReferenceError(RegistryError):
    """A resource loader recursively requested the same URI."""

    def __init__(self, ref: str, path: Iterable[str]):
        super().__init__(ref)
        self.ref = ref
        self.path = tuple(path)

    def __str__(self) -> str:
        return " -> ".join((*self.path, self.ref))


class ReferenceCycleError(CircularReferenceError):
    """A validation call encountered an active reference cycle."""


class ConcurrentRegistryUpdate(RegistryError):
    """A version-checked update observed a newer registry version."""

    def __init__(self, expected_version: int, current_version: int):
        self.expected_version = expected_version
        self.current_version = current_version

    def __str__(self) -> str:
        return (
            f"Expected registry version {self.expected_version}, "
            f"but current version is {self.current_version}."
        )


class UnknownSnapshot(RegistryError):
    """A requested snapshot version does not exist."""

    def __init__(self, version: int, current_version: int):
        self.version = version
        self.current_version = current_version

    def __str__(self) -> str:
        return (
            f"Unknown registry snapshot {self.version}; current version is "
            f"{self.current_version}."
        )


@dataclass(frozen=True)
class LoadEvent:
    registry_id: int
    snapshot_id: int
    version: int
    uri: str
    resource: Any

    @property
    def resource_id(self) -> int:
        return id(self.resource)


@dataclass
class _State:
    resources: dict[str, referencing.Resource]
    loaded: set[str]


@dataclass(frozen=True)
class Snapshot:
    version: int
    registry: referencing.Registry
    registry_id: int
    snapshot_id: int
    base_uri: str = ""

    @property
    def resolver(self) -> referencing.Resolver:
        resolver = self.registry.resolver(base_uri=self.base_uri)
        return _VersionedResolver(resolver)

    def resolver_with_root(self, resource: referencing.Resource):
        uri = resource.id() or ""
        registry = self.registry.with_resource(uri, resource)
        return replace(self, registry=registry, base_uri=uri).resolver

    def lookup(self, ref: str) -> referencing.Resolved:
        return self.resolver.lookup(ref)


class _VersionedResolver:
    def __init__(self, resolver: referencing.Resolver):
        self._resolver = resolver

    def __getattr__(self, name):
        return getattr(self._resolver, name)

    def lookup(self, ref: str):
        try:
            return self._resolver.lookup(ref)
        except referencing.exceptions.Unresolvable as error:
            cause = error.__cause__
            while cause is not None:
                if isinstance(cause, RegistryError):
                    raise cause from error
                next_cause = cause.__cause__
                if next_cause is cause:
                    break
                cause = next_cause
            raise


class VersionedRegistry(Mapping[str, referencing.Resource]):
    """
    A mutable facade around immutable referencing registry snapshots.
    """

    _ids = itertools.count(1)

    def __init__(
        self,
        retrieve: (
            Callable[[str], referencing.Resource]
            | Callable[[str, int], referencing.Resource]
            | None
        ) = None,
        *,
        resources: Iterable[tuple[str, referencing.Resource]] = (),
        base_uri: str = "",
    ):
        self._id = next(self._ids)
        self._snapshot_ids = itertools.count(1)
        self._condition = Condition(lock=threading.RLock())
        self._requests: dict[
            tuple[int, str],
            tuple[int, threading.Event],
        ] = {}
        self._loader = retrieve
        self._base_uri = base_uri
        self._version = 0
        self._states: dict[int, _State] = {0: _State({}, set())}
        self._snapshot_caches: dict[int, dict[str, referencing.Resource]] = {}
        self._loads: list[LoadEvent] = []
        self._loading = threading.local()

        initial_resources = {
            uri.rstrip("#"): resource
            for uri, resource in resources
        }
        self._states[0].resources.update(initial_resources)

    def __getitem__(self, uri: str) -> referencing.Resource:
        with self._condition:
            try:
                return self._states[self._version].resources[uri.rstrip("#")]
            except KeyError:
                raise KeyError(uri) from None

    def __iter__(self) -> Iterator[str]:
        with self._condition:
            return iter(list(self._states[self._version].resources))

    def __len__(self) -> int:
        with self._condition:
            return len(self._states[self._version].resources)

    def __contains__(self, uri: object) -> bool:
        return (
            isinstance(uri, str)
            and uri.rstrip("#") in self._states[self._version].resources
        )

    @property
    def version(self) -> int:
        return self._version

    @property
    def loads(self) -> tuple[LoadEvent, ...]:
        with self._condition:
            return tuple(self._loads)

    @property
    def load_count(self) -> int:
        with self._condition:
            return len(self._loads)

    def snapshot(
        self,
        version: int | None = None,
        *,
        base_uri: str | None = None,
    ) -> Snapshot:
        with self._condition:
            if version is None:
                version = self._version
            elif version < 0 or version > self._version:
                raise UnknownSnapshot(version, self._version)
            return self._build_snapshot(version, base_uri or self._base_uri)

    def pin(self, version: int | None = None) -> referencing.Registry:
        return self.snapshot(version).registry

    def resolver(self, base_uri: str = "") -> referencing.Resolver:
        return self.snapshot(base_uri=base_uri).resolver

    def resolver_with_root(self, resource: referencing.Resource):
        return self.snapshot(base_uri=resource.id() or "").resolver_with_root(
            resource,
        )

    def with_resource(self, uri: str, resource: referencing.Resource):
        cloned = self._copy(duplicate_resources=False)
        cloned.put(uri, resource)
        return cloned

    def with_resources(
        self,
        pairs: Iterable[tuple[str, referencing.Resource]],
    ):
        cloned = self._copy(duplicate_resources=False)
        for uri, resource in pairs:
            cloned.put(uri, resource)
        return cloned

    def combine(
        self,
        *registries: referencing.Registry,
    ) -> referencing.Registry:
        return self.snapshot().registry.combine(*registries)

    def contents(self, uri: str):
        return self[uri].contents

    def get_or_retrieve(self, uri: str):
        snapshot = self.snapshot()
        try:
            return snapshot.registry.get_or_retrieve(uri.rstrip("#"))
        except referencing.exceptions.Unretrievable as error:
            cause = error.__cause__
            if isinstance(cause, RegistryError):
                raise cause from error
            if isinstance(error.__context__, RegistryError):
                raise error.__context__ from error
            raise

    def anchor(self, uri: str, name: str):
        snapshot = self.snapshot()
        try:
            return snapshot.registry.anchor(uri.rstrip("#"), name)
        except referencing.exceptions.Unresolvable as error:
            cause = error.__cause__
            if isinstance(cause, RegistryError):
                raise cause from error
            if isinstance(error.__context__, RegistryError):
                raise error.__context__ from error
            raise

    def crawl(self) -> referencing.Registry:
        return self.snapshot().registry.crawl()

    def remove(self, uri: str):
        cloned = self._copy(duplicate_resources=False)
        with cloned._condition:
            uri = uri.rstrip("#")
            resources = cloned._states[cloned._version].resources
            next_resources = {
                current_uri: current_resource
                for current_uri, current_resource in resources.items()
                if current_uri != uri
            }
            if uri not in resources:
                raise referencing.exceptions.NoSuchResource(ref=uri) from None
            cloned._commit(next_resources, set())
        return cloned

    def put(
        self,
        uri: str,
        resource: referencing.Resource,
        *,
        expected_version: int | None = None,
    ) -> int:
        uri = uri.rstrip("#")
        with self._condition:
            if (
                expected_version is not None
                and expected_version != self._version
            ):
                raise ConcurrentRegistryUpdate(expected_version, self._version)
            current = self._states[self._version]
            next_resources = dict(current.resources)
            next_resources[uri] = resource
            next_loaded = set(current.loaded)
            next_loaded.discard(uri)
            return self._commit(next_resources, next_loaded)

    update = put

    def invalidate(
        self,
        *uris: str,
        expected_version: int | None = None,
    ) -> int:
        normalized = tuple(uri.rstrip("#") for uri in uris)
        with self._condition:
            if (
                expected_version is not None
                and expected_version != self._version
            ):
                raise ConcurrentRegistryUpdate(expected_version, self._version)
            if normalized:
                state = self._states[self._version]
                missing = [
                    uri
                    for uri in normalized
                    if uri not in state.resources and uri not in state.loaded
                ]
                if missing:
                    raise referencing.exceptions.NoSuchResource(ref=missing[0])
                current = state
                next_resources = {
                    uri: resource
                    for uri, resource in current.resources.items()
                    if uri not in normalized
                }
                next_loaded = set()
            else:
                current = self._states[self._version]
                next_resources = dict(current.resources)
                next_loaded = set()
            return self._commit(next_resources, next_loaded)

    def __copy__(self) -> "VersionedRegistry":
        return self._copy()

    def __deepcopy__(self, memo: dict[int, Any]) -> "VersionedRegistry":
        cloned = self._copy(
            resource_factory=lambda value: copy.deepcopy(value, memo),
        )
        memo[id(self)] = cloned
        return cloned

    def __reduce__(self):
        import pickle

        resources = self._states[self._version].resources
        published = pickle.dumps(
            {
                uri: (resource.contents, resource._specification.name)
                for uri, resource in resources.items()
            }
        )
        return (
            _restore_versioned_registry,
            (self._id, self._version, published, self._base_uri),
        )

    def _commit(
        self,
        resources: dict[str, referencing.Resource],
        loaded: set[str],
    ) -> int:
        self._version += 1
        self._states[self._version] = _State(
            resources=resources,
            loaded=loaded,
        )
        self._condition.notify_all()
        return self._version

    def _build_snapshot(self, version: int, base_uri: str) -> Snapshot:
        snapshot_id = next(self._snapshot_ids)
        state = self._states[version]
        cache = self._snapshot_caches.setdefault(snapshot_id, {})
        registry = referencing.Registry(
            retrieve=self._retrieve_for(version, state, cache, snapshot_id),
        )
        for uri, value in state.resources.items():
            registry = registry.with_resource(uri, value)
        return Snapshot(
            version=version,
            registry=registry,
            registry_id=self._id,
            snapshot_id=snapshot_id,
            base_uri=base_uri,
        )

    def _retrieve_for(
        self,
        version: int,
        state: _State,
        cache: dict[str, referencing.Resource],
        snapshot_id: int,
    ) -> Callable[[str], referencing.Resource]:
        def retrieve(uri: str) -> referencing.Resource:
            uri = uri.rstrip("#")
            load_stack = getattr(self._loading, "stack", None)
            if load_stack is None:
                load_stack = []
                self._loading.stack = load_stack

            with self._condition:
                resource = cache.get(uri)
                if resource is not None:
                    return resource
                if self._loader is None:
                    raise referencing.exceptions.NoSuchResource(ref=uri)

                loading = tuple(load_stack)
                if uri in loading:
                    raise CircularReferenceError(uri, loading)

                request_id = (version, uri)
                current_thread = threading.get_ident()
                request = self._requests.get(request_id)
                if request is not None and request[0] != current_thread:
                    completed = request[1]
                else:
                    completed = None

            if completed is not None:
                completed.wait(5)
                with self._condition:
                    resource = cache.get(uri)
                    if resource is not None:
                        return resource
                # If the first load failed, each concurrent caller retries
                # rather than reusing a stale cached resource.
                with self._condition:
                    request = self._requests.get(request_id)
                    if request is not None:
                        completed = request[1]
                    else:
                        completed = None
                if completed is not None:
                    completed.wait(5)
                    with self._condition:
                        resource = cache.get(uri)
                        if resource is not None:
                            return resource

            with self._condition:
                if request_id not in self._requests:
                    completed = threading.Event()
                    load_stack.append(uri)
                    self._requests[request_id] = current_thread, completed

            try:
                resource = self._invoke_loader(uri, version)
                with self._condition:
                    cached = cache.setdefault(uri, resource)
                    if cached is resource:
                        state.loaded.add(uri)
                        self._loads.append(
                            LoadEvent(
                                self._id,
                                snapshot_id,
                                version,
                                uri,
                                resource,
                            ),
                        )
            finally:
                with self._condition:
                    self._requests.pop(request_id, None)
                    load_stack.pop()
                    completed.set()
                    self._condition.notify_all()

            with self._condition:
                return cache[uri]

        return retrieve

    def _invoke_loader(self, uri: str, version: int) -> referencing.Resource:
        try:
            parameters = inspect.signature(self._loader).parameters
            if (
                any(
                    parameter.kind == inspect.Parameter.VAR_POSITIONAL
                    for parameter in parameters.values()
                )
                or len(parameters) >= 2
            ):
                return self._loader(uri, version)
            return self._loader(uri)
        except (
            CircularReferenceError,
            referencing.exceptions.CannotDetermineSpecification,
            referencing.exceptions.NoSuchResource,
        ):
            raise
        except BaseException as error:
            if isinstance(error.__context__, CircularReferenceError):
                raise error.__context__ from error
            raise ResourceLoadError(uri, version, error) from None

    def _copy(
        self,
        *,
        duplicate_resources: bool = True,
        resource_factory: Callable[
            [referencing.Resource],
            referencing.Resource,
        ] = lambda value: value._specification.create_resource(value.contents),
    ) -> "VersionedRegistry":
        cloned = object.__new__(type(self))
        with self._condition:
            cloned._id = next(VersionedRegistry._ids)
            cloned._snapshot_ids = itertools.count(1)
            cloned._condition = Condition(lock=threading.RLock())
            cloned._requests = {}
            original_loader = self._loader

            def copied_loader(uri, version, loader=original_loader):
                loaded = loader(uri, version)
                return loaded._specification.create_resource(loaded.contents)

            cloned._loader = (
                copied_loader
                if original_loader is not None
                else None
            )
            cloned._base_uri = self._base_uri
            cloned._version = self._version

            def copied_resource(value):
                if not duplicate_resources:
                    return value
                return resource_factory(value)

            resources = {
                uri: copied_resource(resource)
                for uri, resource
                in self._states[self._version].resources.items()
            }
            cloned._states = {
                each: _State(
                    dict(resources) if each == self._version else {},
                    set() if each == self._version else set(),
                )
                for each in range(self._version + 1)
            }
            cloned._snapshot_caches = {}
            cloned._loads = []
            cloned._loading = threading.local()
        return cloned


def _restore_versioned_registry(identifier, version, published, base_uri):
    import pickle
    import referencing.jsonschema

    specifications = {
        specification.name: specification
        for specification in (
            referencing.jsonschema.DRAFT3,
            referencing.jsonschema.DRAFT4,
            referencing.jsonschema.DRAFT6,
            referencing.jsonschema.DRAFT7,
            referencing.jsonschema.DRAFT201909,
            referencing.jsonschema.DRAFT202012,
            referencing.Specification.OPAQUE,
        )
    }

    def restore_resource(value):
        if isinstance(value, tuple):
            contents, name = value
            return specifications[name].create_resource(contents)
        return referencing.Resource.from_contents(value)

    registry = VersionedRegistry()
    registry._id = identifier
    registry._version = version
    published_resources = pickle.loads(published)
    resources = {
        uri: restore_resource(value)
        for uri, value in published_resources.items()
    }
    registry._base_uri = base_uri
    registry._states = {
        each: _State(dict(resources) if each == version else {}, set())
        for each in range(version + 1)
    }
    registry._snapshot_caches = {}
    return registry


RegistrySnapshot = Snapshot
DynamicRegistry = VersionedRegistry
