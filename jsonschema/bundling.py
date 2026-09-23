"""
Collect the resources a schema needs into a self-contained offline bundle.

Production environments without network access cannot retrieve schemas
(or their dependencies) at validation time. This module provides two
public entry points for moving resolution to build time:

* `collect`, which crawls an entry schema's references -- relative
  ``$ref`` keywords, nested ``$id`` keywords, anchors and dynamic
  anchors -- using a caller-provided retrieval function, and freezes
  everything it finds into a `Bundle`.

* `Bundle`, which serializes to JSON (recording each resource's digest,
  the root entry point and the dependency edges between resources) and
  later rebuilds a `referencing.Registry` with no network access.

Loading a bundle verifies it is complete and untampered. Missing or
modified resources are reported as errors, never silently fetched or
treated as validation successes.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urldefrag, urljoin
import hashlib
import json

from attrs import field, frozen
import referencing
import referencing.jsonschema

__all__ = [
    "Bundle",
    "BundleConflictError",
    "BundleError",
    "BundleIntegrityError",
    "IncompleteBundleError",
    "collect",
]

#: The version of the serialized bundle format produced by Bundle.to_json.
_FORMAT_VERSION = 1

#: Reference-like keywords whose targets are collected as dependencies.
_REFERENCE_KEYWORDS = ("$ref", "$dynamicRef", "$recursiveRef")

_DIALECT_IDS = {
    "draft2020-12": "https://json-schema.org/draft/2020-12/schema",
    "draft2019-09": "https://json-schema.org/draft/2019-09/schema",
    "draft-07": "http://json-schema.org/draft-07/schema",
    "draft-06": "http://json-schema.org/draft-06/schema",
    "draft-04": "http://json-schema.org/draft-04/schema",
    "draft-03": "http://json-schema.org/draft-03/schema",
}

_DEFAULT_SPECIFICATION = referencing.jsonschema.DRAFT202012


class BundleError(Exception):
    """
    Base class for all errors raised while collecting or loading bundles.
    """


class BundleConflictError(BundleError):
    """
    Two distinct resources claim the same normalized URI.
    """

    def __init__(self, uri: str):
        super().__init__(
            f"conflicting contents were provided for {uri!r}; refusing "
            "to bundle two different resources under one URI",
        )
        self.uri = uri


class IncompleteBundleError(BundleError):
    """
    A bundle is missing resources its schemas refer to.
    """

    def __init__(self, missing: Iterable[str]):
        self.missing = frozenset(missing)
        uris = "\n".join(sorted(self.missing))
        super().__init__(
            f"the bundle is incomplete, missing resources:\n{uris}",
        )


class BundleIntegrityError(BundleError):
    """
    A bundle's contents do not match its recorded digests or edges.
    """


def _canonical(contents: Any) -> str:
    """
    Return a canonical serialization of the given contents.
    """
    return json.dumps(contents, sort_keys=True, separators=(",", ":"))


def _digest(contents: Any) -> str:
    """
    Return a hex digest of the canonical form of the given contents.
    """
    return hashlib.sha256(_canonical(contents).encode("utf-8")).hexdigest()


def _dialect_of(specification: referencing.Specification) -> str:
    try:
        return _DIALECT_IDS[specification.name]
    except KeyError:
        raise ValueError(
            "cannot serialize bundles collected with the custom "
            f"specification {specification!r}; use one of the "
            "specifications from referencing.jsonschema",
        ) from None


def _specification_for(dialect: str) -> referencing.Specification:
    try:
        return referencing.jsonschema.specification_with(dialect)
    except referencing.jsonschema.UnknownDialect:
        raise BundleError(
            f"the bundle uses the unknown dialect {dialect!r}",
        ) from None


def _as_resource(
    contents_or_resource: Any,
    specification: referencing.Specification,
) -> referencing.Resource:
    if isinstance(contents_or_resource, referencing.Resource):
        return contents_or_resource
    return referencing.Resource.from_contents(
        contents_or_resource,
        default_specification=specification,
    )


@frozen
class _Scanned:
    """
    The references and nested resources found within one document.
    """

    edges: set[str] = field(factory=set)
    provided: dict[str, str] = field(factory=dict)
    dynamic_refs: set[tuple[str, str]] = field(factory=set)


def _scan(
    specification: referencing.Specification,
    contents: Any,
    base_uri: str,
    scanned: _Scanned,
) -> None:
    """
    Walk a document, tracking the base URI through nested ``$id`` keywords.

    Record the (defragmented) URIs the document depends on, the nested
    resources it provides, and the dynamic anchors it refers to.
    """
    if not isinstance(contents, Mapping):
        return

    id = specification.id_of(contents)
    if id is not None:
        base_uri = urljoin(base_uri, id.rstrip("#"))
        specification = specification.detect(contents)
        canonical = _canonical(contents)
        existing = scanned.provided.setdefault(base_uri, canonical)
        if existing != canonical:
            raise BundleConflictError(base_uri)

    for keyword in _REFERENCE_KEYWORDS:
        ref = contents.get(keyword)
        if not isinstance(ref, str):
            continue
        uri, fragment = urldefrag(urljoin(base_uri, ref))
        scanned.edges.add(uri)
        if keyword == "$dynamicRef" and fragment and "/" not in fragment:
            scanned.dynamic_refs.add((fragment, uri))

    for subschema in specification.subresources_of(contents):
        _scan(specification, subschema, base_uri, scanned)


def _scan_all(
    resources: Mapping[str, Any],
    specification: referencing.Specification,
) -> tuple[dict[str, set[str]], dict[str, str], dict[str, set[str]]]:
    """
    Scan every resource, returning edges, provided URIs and dynamic refs.
    """
    edges: dict[str, set[str]] = {}
    provided: dict[str, str] = {}
    dynamic_refs: dict[str, set[str]] = {}
    for uri, contents in resources.items():
        scanned = _Scanned()
        _scan(specification.detect(contents), contents, uri, scanned)
        edges[uri] = scanned.edges
        for nested, canonical in scanned.provided.items():
            existing = provided.setdefault(nested, canonical)
            if existing != canonical:
                raise BundleConflictError(nested)
        for name, _target in scanned.dynamic_refs:
            dynamic_refs.setdefault(name, set()).add(uri)
    return edges, provided, dynamic_refs


@frozen
class Bundle:
    """
    A self-contained, serializable set of resources for offline use.

    A bundle records the contents of every resource an entry schema
    (transitively) refers to, keeping each resource under its original
    absolute URI. Use `collect` to build one at build time, `to_json`
    to serialize it, and `from_json` followed by `registry` (or
    `validator`) to validate with it at runtime, without any network
    access.
    """

    _root: str = field(alias="root")
    _resources: Mapping[str, Any] = field(alias="resources")
    _edges: Mapping[str, frozenset[str]] = field(alias="edges")
    _dynamic_frontier: Mapping[str, tuple[str, ...]] = field(
        alias="dynamic_frontier",
    )
    _dialect: str = field(alias="dialect")

    @property
    def root(self) -> str:
        """
        The URI of the entry schema the bundle was collected from.
        """
        return self._root

    @property
    def resources(self) -> Mapping[str, Any]:
        """
        The bundled resources, keyed by their absolute URIs.
        """
        return self._resources

    @property
    def edges(self) -> Mapping[str, frozenset[str]]:
        """
        The dependency edges between bundled resources.
        """
        return self._edges

    @property
    def dynamic_frontier(self) -> Mapping[str, tuple[str, ...]]:
        """
        Dynamic anchors whose dynamic scope cannot be proven statically.

        Maps each anchor name referred to by a ``$dynamicRef`` to the
        URIs of the bundled resources containing such a reference. Any
        *other* resource defining a matching ``$dynamicAnchor`` which
        is meant to participate must be supplied explicitly (via
        ``extra_resources`` to `collect`), as static analysis cannot
        discover it.
        """
        return self._dynamic_frontier

    @property
    def root_schema(self) -> Any:
        """
        The contents of the entry schema.
        """
        return self._resources[self._root]

    def _specification(self) -> referencing.Specification:
        return _specification_for(self._dialect)

    def registry(
        self,
        specification: referencing.Specification | None = None,
    ) -> referencing.jsonschema.SchemaRegistry:
        """
        Rebuild a `referencing.Registry` from the bundle's resources.

        The registry performs no retrieval: any reference to a resource
        missing from the bundle fails loudly rather than reaching the
        network.
        """
        if specification is None:
            specification = self._specification()
        return referencing.jsonschema.EMPTY_REGISTRY.with_resources(
            (
                uri,
                referencing.Resource.from_contents(
                    contents,
                    default_specification=specification,
                ),
            )
            for uri, contents in self._resources.items()
        )

    def validator(self, validator_class=None, **kwargs: Any):
        """
        Create a `jsonschema` validator for the entry schema, offline.

        Uses `jsonschema.validators.validator_for` to pick a validator
        class unless one is passed explicitly.
        """
        from jsonschema import validators

        schema = self.root_schema
        if validator_class is None:
            validator_class = validators.validator_for(schema)
        return validator_class(schema, registry=self.registry(), **kwargs)

    def to_json(self, **json_kwargs: Any) -> str:
        """
        Serialize the bundle to a JSON document.

        The document records each resource's contents and digest, the
        root entry point, and the dependency edges between resources.
        """
        document = {
            "version": _FORMAT_VERSION,
            "dialect": self._dialect,
            "root": self._root,
            "resources": {
                uri: {"sha256": _digest(contents), "contents": contents}
                for uri, contents in sorted(self._resources.items())
            },
            "edges": {
                uri: sorted(targets)
                for uri, targets in sorted(self._edges.items())
            },
            "dynamicFrontier": {
                name: sorted(uris)
                for name, uris in sorted(self._dynamic_frontier.items())
            },
        }
        json_kwargs.setdefault("indent", 2)
        return json.dumps(document, **json_kwargs)

    @classmethod
    def from_json(
        cls,
        data: str | Mapping[str, Any],
        specification: referencing.Specification | None = None,
    ) -> Bundle:
        """
        Load a bundle serialized by `Bundle.to_json`, verifying it.

        Every resource's digest is recomputed and compared, the recorded
        dependency edges are checked against the actual references in the
        bundled contents, and all referenced resources are required to be
        present. No resource is ever retrieved from the network.

        Raises:

            `BundleIntegrityError`:

                if contents were modified relative to their recorded
                digests or edges

            `IncompleteBundleError`:

                if resources referred to by the bundle are absent

        """
        if isinstance(data, str):
            data = json.loads(data)
        if data.get("version") != _FORMAT_VERSION:
            raise BundleError(
                f"unsupported bundle format version {data.get('version')!r}",
            )

        if specification is None:
            specification = _specification_for(data["dialect"])

        resources = {}
        for uri, entry in data["resources"].items():
            contents = entry["contents"]
            if _digest(contents) != entry["sha256"]:
                raise BundleIntegrityError(
                    f"the contents of {uri!r} do not match its digest; "
                    "the bundle was modified or corrupted",
                )
            resources[uri] = contents

        edges, provided, dynamic_refs = _scan_all(resources, specification)
        edges = {uri: frozenset(targets) for uri, targets in edges.items()}
        recorded = {
            uri: frozenset(targets)
            for uri, targets in data["edges"].items()
        }

        for uri, canonical in provided.items():
            if uri in resources and _canonical(resources[uri]) != canonical:
                raise BundleIntegrityError(
                    f"{uri!r} is both a top-level resource and nested "
                    "within another resource, with different contents",
                )

        root = data["root"]
        missing = {
            uri
            for source, targets in recorded.items()
            for uri in (source, *targets)
            if uri not in resources and uri not in provided
        }
        if root not in resources:
            missing.add(root)
        if missing:
            raise IncompleteBundleError(missing)

        if edges != recorded:
            raise BundleIntegrityError(
                "the bundle's dependency edges do not match its contents; "
                "the bundle was modified or corrupted",
            )

        frontier = {
            name: tuple(sorted(uris)) for name, uris in dynamic_refs.items()
        }
        return cls(
            root=root,
            resources=resources,
            edges=edges,
            dynamic_frontier=frontier,
            dialect=data["dialect"],
        )


def collect(
    root: str,
    retrieve,
    specification: referencing.Specification = _DEFAULT_SPECIFICATION,
    extra_resources: Iterable[tuple[str, Any]] | Mapping[str, Any] = (),
) -> Bundle:
    """
    Collect an entry schema and its dependencies into an offline `Bundle`.

    Resources keep their original absolute URIs, and circular references
    are collected once. If two different documents claim the same
    normalized URI, `BundleConflictError` is raised and nothing is
    bundled. If a statically required resource cannot be retrieved,
    `IncompleteBundleError` is raised listing what is missing.

    Arguments:

        root:

            the absolute URI of the entry schema

        retrieve:

            a callable given each referenced URI, returning the resource
            (or its plain contents) found there, or raising
            `referencing.exceptions.NoSuchResource`. This is the *only*
            source of resources; nothing is fetched implicitly.

        specification:

            the `referencing.Specification` to assume for resources
            which do not identify their own dialect via ``$schema``

        extra_resources:

            additional ``(URI, resource)`` pairs to include explicitly
            -- e.g. resources defining ``$dynamicAnchor`` keywords
            which static analysis cannot prove are needed (see
            `Bundle.dynamic_frontier`). Their own dependencies are
            collected as well.

    """
    root, fragment = urldefrag(root)
    if fragment:
        raise ValueError(f"the root URI must not have a fragment: {root!r}")

    resources: dict[str, Any] = {}
    canonicals: dict[str, str] = {}
    provided: dict[str, str] = {}
    edges: dict[str, set[str]] = {}
    dynamic_refs: dict[str, set[str]] = {}

    def add(uri: str, resource: referencing.Resource) -> None:
        canonical = _canonical(resource.contents)
        existing = canonicals.setdefault(uri, canonical)
        if existing != canonical or provided.get(uri, canonical) != canonical:
            raise BundleConflictError(uri)
        resources[uri] = resource.contents

    if isinstance(extra_resources, Mapping):
        extra_resources = extra_resources.items()
    seeded = []
    for uri, contents_or_resource in extra_resources:
        uri, fragment = urldefrag(uri)
        if fragment:
            raise ValueError(
                f"resource URIs must not have fragments: {uri!r}",
            )
        add(uri, _as_resource(contents_or_resource, specification))
        seeded.append(uri)

    missing = []
    walked = set()
    queue = [root, *seeded]
    while queue:
        uri = queue.pop()
        if uri in walked:
            continue
        walked.add(uri)

        if uri not in resources:
            if uri in provided:
                continue  # a nested resource of an already-scanned document
            try:
                retrieved = retrieve(uri)
            except referencing.exceptions.NoSuchResource:
                missing.append(uri)
                continue
            add(uri, _as_resource(retrieved, specification))

        scanned = _Scanned()
        _scan(
            specification.detect(resources[uri]),
            resources[uri],
            uri,
            scanned,
        )
        for nested, canonical in scanned.provided.items():
            if canonicals.get(nested, canonical) != canonical:
                raise BundleConflictError(nested)
            existing = provided.setdefault(nested, canonical)
            if existing != canonical:
                raise BundleConflictError(nested)
        edges[uri] = scanned.edges
        for name, _target in scanned.dynamic_refs:
            dynamic_refs.setdefault(name, set()).add(uri)
        queue.extend(scanned.edges - walked)

    if missing:
        raise IncompleteBundleError(missing)

    return Bundle(
        root=root,
        resources=resources,
        edges={uri: frozenset(targets) for uri, targets in edges.items()},
        dynamic_frontier={
            name: tuple(sorted(uris)) for name, uris in dynamic_refs.items()
        },
        dialect=_dialect_of(specification),
    )
