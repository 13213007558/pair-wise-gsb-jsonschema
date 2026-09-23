"""
Collect a schema's resources into a self-contained, offline bundle.

This module provides `SchemaBundle`, a build-time / run-time pair of tools
for environments where the production network cannot reach the servers
hosting a schema's resources:

* at *build* time, `SchemaBundle.collect` walks an entry schema's
  ``$ref`` / ``$dynamicRef`` / ``$recursiveRef`` references -- across
  relative references, nested ``$id``s, anchors and dynamic anchors --
  fetching each referenced resource through a caller-provided `retrieve`
  callback, and records the resulting resources, their content digests,
  the root entry point and the dependency edges between them,

* at *run* time, `SchemaBundle.registry` rebuilds a
  `referencing.Registry` purely from the bundle's contents. No retrieval
  callback is involved: any resource missing from the bundle (or one
  whose contents no longer match the recorded digest) is reported as an
  error instead of being fetched from the network or silently treated
  as an unresolvable -- and hence passing -- reference.

Resources keep their original absolute URIs as their identity; no
references are rewritten into a single merged document, so dynamic
scope (``$dynamicRef`` / ``$dynamicAnchor``) keeps working exactly as it
did against the original registry.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Mapping
from typing import Any
from urllib.parse import urldefrag, urljoin
import hashlib
import json

from referencing.exceptions import NoSuchResource
import referencing.jsonschema

__all__ = [
    "BundleError",
    "BundleIncompleteError",
    "BundleTamperedError",
    "ConflictingResourceError",
    "SchemaBundle",
]

_REFERENCE_KEYWORDS = ("$ref", "$dynamicRef", "$recursiveRef")
_DYNAMIC_KEYWORDS = frozenset({"$dynamicRef", "$recursiveRef"})

_SPECIFICATIONS = {
    each.name: each
    for each in (
        referencing.jsonschema.DRAFT3,
        referencing.jsonschema.DRAFT4,
        referencing.jsonschema.DRAFT6,
        referencing.jsonschema.DRAFT7,
        referencing.jsonschema.DRAFT201909,
        referencing.jsonschema.DRAFT202012,
    )
}

_FORMAT_VERSION = 1


class BundleError(Exception):
    """Base class for errors related to offline schema bundles."""


class ConflictingResourceError(BundleError):
    """
    Two different documents claim the same canonical URI.

    The bundle format refuses to pick one arbitrarily, as validation
    results could differ from the original resource set.
    """

    def __init__(self, uri: str):
        self.uri = uri
        super().__init__(
            f"conflicting contents were found for {uri!r}; "
            "refusing to build a bundle from an ambiguous resource set",
        )


class BundleIncompleteError(BundleError):
    """
    The bundle is missing resources required by its dependency edges.
    """

    def __init__(self, missing: Iterable[str]):
        self.missing = frozenset(missing)
        super().__init__(
            "the bundle is incomplete; missing resources: "
            + ", ".join(sorted(self.missing)),
        )


class BundleTamperedError(BundleError):
    """
    A resource's contents do not match the digest recorded in the bundle.
    """

    def __init__(self, uri: str):
        self.uri = uri
        super().__init__(
            f"the contents of {uri!r} do not match the digest recorded "
            "in the bundle; the bundle may have been tampered with",
        )


def _normalize(uri: str) -> str:
    return urldefrag(uri)[0]


def _digest(contents: Any) -> str:
    serialized = json.dumps(contents, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _scan(specification, contents, resource_uri):
    """
    Find references, dynamic boundaries and embedded resources.

    Walks the subschemas of a single resource, tracking the base URI
    imposed by nested ``$id``s, and returns a triple of:

    * the canonical URIs this resource statically depends on,
    * one entry per ``$dynamicRef`` / ``$recursiveRef`` encountered --
      these are the boundaries where static collection cannot prove
      completeness, because dynamic scope may involve resources which
      are not reachable by following static references,
    * `(uri, contents)` pairs for resources embedded via nested
      ``$id``s, which satisfy references without further retrieval.
    """
    references = []
    dynamic = []
    embedded = []

    def visit(schema, base_uri):
        if not isinstance(schema, Mapping):
            return
        id = specification.id_of(schema)
        if isinstance(id, str):
            new_base = _normalize(urljoin(base_uri, id))
            if new_base != base_uri:
                embedded.append((new_base, schema))
                base_uri = new_base
        for keyword in _REFERENCE_KEYWORDS:
            value = schema.get(keyword)
            if not isinstance(value, str):
                continue
            target = _normalize(urljoin(base_uri, value))
            references.append(target)
            if keyword in _DYNAMIC_KEYWORDS:
                dynamic.append(
                    {
                        "resource": resource_uri,
                        "keyword": keyword,
                        "ref": value,
                        "target": target,
                    },
                )
        for each in specification.subresources_of(schema):
            visit(each, base_uri)

    visit(contents, resource_uri)
    return references, dynamic, embedded


def _offline(uri: str):
    """
    A retrieval callback which never touches the network.
    """
    raise NoSuchResource(ref=uri)


class SchemaBundle:
    """
    A self-contained, verifiable snapshot of a schema's resource set.

    Build one with `SchemaBundle.collect`, serialize it with `to_json`,
    and turn it back into a `referencing.Registry` with
    `SchemaBundle.registry`.
    """

    def __init__(
        self,
        root_uri: str,
        entries: Iterable[Mapping[str, Any]],
        dynamic_boundaries: Iterable[Mapping[str, Any]] = (),
    ):
        self._root_uri = root_uri
        self._entries = tuple(
            {
                "uri": each["uri"],
                "contents": each["contents"],
                "sha256": each["sha256"],
                "specification": each["specification"],
                "dependencies": tuple(each["dependencies"]),
            }
            for each in entries
        )
        self._dynamic_boundaries = tuple(
            dict(each) for each in dynamic_boundaries
        )

    @classmethod
    def collect(
        cls,
        root_uri: str,
        retrieve: Callable[[str], Any],
        *,
        extra_uris: Iterable[str] = (),
        specification=referencing.jsonschema.DRAFT202012,
    ) -> SchemaBundle:
        """
        Collect the resources reachable from `root_uri` into a bundle.

        `retrieve` is called with each referenced canonical URI and
        must return the (deserialized JSON) contents of that resource;
        it is the only I/O performed, and only happens now, at build
        time.

        `extra_uris` allows explicitly supplementing the bundle with
        resources which static analysis cannot discover -- in particular
        resources which only participate via ``$dynamicRef`` dynamic
        scope (see `SchemaBundle.dynamic_boundaries`).

        References are followed through relative ``$ref``s, nested
        ``$id``s, anchors and dynamic anchors, with circular
        references deduplicated. If two different documents claim the
        same canonical URI, `ConflictingResourceError` is raised rather
        than silently choosing one.
        """
        entries = {}
        embedded = {}
        boundaries = []

        def note_embedded(uri, contents):
            digest = _digest(contents)
            known = entries.get(uri)
            if known is not None and known["sha256"] != digest:
                raise ConflictingResourceError(uri)
            previous = embedded.setdefault(uri, digest)
            if previous != digest:
                raise ConflictingResourceError(uri)

        queue = deque([_normalize(root_uri)])
        queue.extend(_normalize(each) for each in extra_uris)
        while queue:
            uri = queue.popleft()
            if uri in entries or uri in embedded:
                continue
            try:
                contents = retrieve(uri)
            except Exception as error:
                raise BundleError(
                    f"could not retrieve {uri!r} while collecting "
                    f"the bundle: {error}",
                ) from error

            detected = specification.detect(contents)
            digest = _digest(contents)
            if uri in embedded and embedded[uri] != digest:
                raise ConflictingResourceError(uri)

            references, dynamic, subresources = _scan(
                detected,
                contents,
                uri,
            )
            for each_uri, each_contents in subresources:
                note_embedded(each_uri, each_contents)

            entries[uri] = {
                "uri": uri,
                "contents": contents,
                "sha256": digest,
                "specification": detected.name,
                "dependencies": tuple(sorted(set(references))),
            }
            boundaries.extend(dynamic)
            for target in references:
                if target not in entries and target not in embedded:
                    queue.append(target)

        return cls(
            root_uri=_normalize(root_uri),
            entries=entries.values(),
            dynamic_boundaries=boundaries,
        )

    @property
    def root_uri(self) -> str:
        """
        The canonical URI of the entry resource the bundle was built from.
        """
        return self._root_uri

    @property
    def resource_uris(self) -> frozenset:
        """
        The canonical URIs of the resources contained in the bundle.
        """
        return frozenset(each["uri"] for each in self._entries)

    @property
    def dynamic_boundaries(self) -> list:
        """
        Dynamic references whose full resource set is not statically known.

        Each entry describes a ``$dynamicRef`` or ``$recursiveRef`` found
        during collection. Any resource which provides a matching
        ``$dynamicAnchor`` (or ``$recursiveAnchor``) but is not statically
        reachable cannot be proven to be collected; if validation relies
        on such resources, collect the bundle again passing their URIs
        via `extra_uris`.
        """
        return [dict(each) for each in self._dynamic_boundaries]

    @property
    def dependencies(self) -> Mapping:
        """
        The recorded dependency edges, mapping each resource to its refs.
        """
        return {
            each["uri"]: frozenset(each["dependencies"])
            for each in self._entries
        }

    def contents_of(self, uri: str) -> Any:
        """
        Return the bundled contents of the resource identified by `uri`.
        """
        uri = _normalize(uri)
        for each in self._entries:
            if each["uri"] == uri:
                return each["contents"]
        raise NoSuchResource(ref=uri)

    def to_json(self) -> Mapping:
        """
        Serialize the bundle to a JSON-compatible mapping.
        """
        return {
            "version": _FORMAT_VERSION,
            "root": self._root_uri,
            "resources": [
                {
                    "uri": each["uri"],
                    "sha256": each["sha256"],
                    "specification": each["specification"],
                    "dependencies": list(each["dependencies"]),
                    "contents": each["contents"],
                }
                for each in self._entries
            ],
            "dynamicBoundaries": [
                dict(each) for each in self._dynamic_boundaries
            ],
        }

    @classmethod
    def from_json(cls, data: Mapping) -> SchemaBundle:
        """
        Rebuild a bundle from its serialized form, verifying it.

        Raises `BundleTamperedError` if any resource's contents do not
        match the recorded digest, and `BundleIncompleteError` if any
        dependency edge points at a resource missing from the bundle.
        """
        if data.get("version") != _FORMAT_VERSION:
            raise BundleError(
                f"unsupported bundle format version: {data.get('version')!r}",
            )
        bundle = cls(
            root_uri=data["root"],
            entries=data["resources"],
            dynamic_boundaries=data.get("dynamicBoundaries", ()),
        )
        bundle._verify()
        return bundle

    def registry(self) -> referencing.jsonschema.SchemaRegistry:
        """
        Rebuild a `referencing.Registry` purely from the bundle.

        The bundle is first verified (digests and dependency edges), so
        a missing or tampered resource fails loudly here rather than
        surfacing as an unresolvable -- and silently passing --
        reference at validation time. The returned registry has no
        retrieval callback: it will never access the network.
        """
        self._verify()
        return referencing.jsonschema.SchemaRegistry(
            retrieve=_offline,
        ).with_resources(
            (
                each["uri"],
                _SPECIFICATIONS[each["specification"]].create_resource(
                    contents=each["contents"],
                ),
            )
            for each in self._entries
        )

    def _verify(self) -> None:
        by_uri = {}
        for each in self._entries:
            if _digest(each["contents"]) != each["sha256"]:
                raise BundleTamperedError(each["uri"])
            if each["uri"] in by_uri:
                raise ConflictingResourceError(each["uri"])
            by_uri[each["uri"]] = each

        embedded = {}
        for each in self._entries:
            specification = _SPECIFICATIONS.get(each["specification"])
            if specification is None:
                raise BundleError(
                    f"unknown specification {each['specification']!r} "
                    f"recorded for {each['uri']!r}",
                )
            _, _, subresources = _scan(
                specification,
                each["contents"],
                each["uri"],
            )
            for uri, contents in subresources:
                digest = _digest(contents)
                previous = embedded.setdefault(uri, digest)
                if previous != digest:
                    raise ConflictingResourceError(uri)

        missing = {
            dependency
            for each in self._entries
            for dependency in each["dependencies"]
            if dependency not in by_uri and dependency not in embedded
        }
        if self._root_uri not in by_uri:
            missing.add(self._root_uri)
        if missing:
            raise BundleIncompleteError(missing)
