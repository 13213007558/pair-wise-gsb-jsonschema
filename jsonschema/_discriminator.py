"""
Optional candidate indexing for ``oneOf`` applicators.

A JSON Schema ``oneOf`` with dozens of message branches, each carrying a
discriminating ``type``-like field, normally validates an instance against
every branch until one succeeds.  When every branch's match *can be proven*
ahead of time from a single property's ``const`` / ``enum`` values, the
branches can instead be indexed by those values, so that a normal request
only visits the single candidate branch it could possibly match.

The index is deliberately conservative.  It only prunes a branch when the
branch is *provably* inapplicable to an instance, so it can never hide a
second successful branch:

* every branch must require the same property and constrain it with inline
  ``const`` or ``enum`` (an optional property, or no usable constraint, means
  the branch stays on the unindexed fallback path),

* values are compared with `jsonschema._utils.equal`, so ``true`` and ``1``
  (and ``false`` and ``0``) never alias, and lists/objects are compared
  structurally rather than by Python equality quirks,

* overlapping value sets between branches are retained -- both branches stay
  candidates for the shared value,

* a ``$ref`` / ``$dynamicRef`` / ``$recursiveRef`` that cannot be statically
  resolved locally, a non-mapping schema, or a boolean schema prevents the
  affected branch from being indexed,

* a schema that changes after an index was built invalidates the index:
  each use re-checks a structural guard and otherwise rebuilds, so a stale
  index can never admit an instance it no longer covers.

Anything not provably discriminable keeps the original, exhaustive
validation behavior, including fully populated error contexts and the
``unevaluatedProperties`` / ``unevaluatedItems`` annotation machinery, which
walks the schema independently and is therefore unaffected by candidate
pruning.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os
from typing import Any

import referencing.exceptions

from jsonschema._utils import _UNSUPPORTED, _uniq_key, equal

# References which may move at validation time (dynamic scope, remote
# retrieval) cannot be proven statically, so schemas carrying them are never
# indexed.  Plain ``$ref`` is resolved through the already-configured local
# registry at index-build time and is only accepted when the lookup is
# synchronous and local.
_DYNAMIC_REFERENCE_KEYWORDS = ("$dynamicRef", "$recursiveRef")

# A hard bound on static ``$ref`` chasing, which additionally protects
# against cyclic references (cycles are tracked explicitly as well).
_MAX_STATIC_REF_DEPTH = 64


@dataclass(frozen=True)
class _BranchDescriptor:
    """
    The statically proven discriminating values of one branch.

    ``value_keys`` are hashable keys compatible with
    `jsonschema._utils._uniq_key`; any value that cannot be canonically
    hashed (e.g. an unhashable mapping) is kept in ``linear_values`` and
    matched with a linear scan using `jsonschema._utils.equal`.

    ``ref_chain`` records the plain ``$ref`` values the branch was
    statically resolved through (empty for an inline branch), so the
    freshness guard can re-walk the exact same targets on a live schema.

    ``fingerprint_key`` canonically hashes the branch *as observed at build
    time* (including any resolved ``$ref`` target).  It is only recomputed
    when the cheap identity/shape checks in `DiscriminatorIndex.is_fresh`
    notice that the live branch object changed.
    """

    index: int
    required: str
    value_keys: frozenset[Any]
    linear_values: tuple[Any, ...]
    ref_chain: tuple[str, ...]
    fingerprint_key: Any
    # Top-level shape captured for the cheap freshness check: the set of
    # keywords and the identity of each keyword's value object.  Any
    # in-place addition, removal, or value replacement on the branch is
    # visible here without re-hashing the whole branch.
    branch_keys: frozenset[Any]
    top_level_identities: tuple[tuple[Any, Any], ...]
    property_schema_key: Any


@dataclass(frozen=True)
class DiscriminatorIndex:
    """
    A candidate index for one ``oneOf`` applicator.

    ``descriptors`` retains the static shape each branch had when the index
    was built, so `is_fresh` can prove that a subsequently mutated schema no
    longer corresponds to this index.  ``branch_identities`` remembers the
    exact branch objects seen at build time, making the common "nothing
    changed" freshness check a cheap identity comparison rather than a
    structural re-hash of every branch.
    """

    property: str
    mapping: dict[Any, tuple[int, ...]]
    linear: tuple[tuple[Any, tuple[int, ...]], ...]
    descriptors: tuple[_BranchDescriptor, ...]
    branch_identities: tuple[Any, ...]
    registry: object

    def is_fresh(self, oneOf, registry, resolver):
        """
        Check that the live ``oneOf`` still matches this index.

        The steady-state check is cheap: each branch's keyword set and
        top-level value identities are compared, plus the discriminating
        property schema's canonical constraint key.  That detects every
        in-place edit of a branch -- added or removed keywords (including
        unrelated ones such as a new ``minimum``), replaced values, and
        changed ``const`` / ``enum`` constraints -- without re-hashing the
        whole branch on every request.  When an identity check differs, the
        full structural fingerprint (re-walking any resolved ``$ref``
        chain) decides whether a rebuild is truly needed.

        The registry is part of the guard because statically resolved
        ``$ref`` targets live in it; registries are immutable, so a changed
        registry identity always means a new, re-checked index is needed.
        """
        if len(oneOf) != len(self.branch_identities) or (
            registry is not self.registry
        ):
            return False

        for branch, identity, descriptor in zip(
            oneOf,
            self.branch_identities,
            self.descriptors,
        ):
            if not _branch_is_fresh(branch, identity, descriptor, resolver):
                return False
        return True

    def candidates(self, instance):
        """
        Return the indexes of branches the given instance may match.

        An empty tuple means every indexed branch is provably inapplicable.
        """
        value = instance[self.property]
        key = _uniq_key(value)
        if key is not _UNSUPPORTED:
            indexes = self.mapping.get(key)
            if indexes is not None:
                return indexes

        for each, indexes in self.linear:
            if equal(each, value):
                return indexes
        return ()


def _branch_is_fresh(branch, identity, descriptor, resolver):
    """
    Decide whether one live branch still matches its descriptor.

    The steady-state (unmutated) case is cheap: a top-level key-set
    comparison plus object-identity checks against each top-level value and
    the discriminating fragments.  That notices every in-place edit of the
    branch dictionary itself (added/removed keywords, replaced values),
    including unrelated keywords such as a new ``minimum``, without hashing
    the whole branch on each request.

    Only when one of those checks differs does the full structural
    fingerprint run, which also walks resolved ``$ref`` targets.  Nested
    in-place edits that leave every recorded identity intact are covered
    separately by comparing the discriminating property schema's canonical
    ``const`` / ``enum`` key.
    """
    if not isinstance(branch, Mapping):
        return False
    if frozenset(branch) != descriptor.branch_keys:
        return _branch_fingerprint_matches(branch, descriptor, resolver)

    for key, value_identity in descriptor.top_level_identities:
        if branch.get(key) is not value_identity:
            return _branch_fingerprint_matches(branch, descriptor, resolver)

    if branch is not identity:
        return _branch_fingerprint_matches(branch, descriptor, resolver)

    property_schema = branch.get("properties", {}).get(descriptor.required)
    if not isinstance(property_schema, Mapping):
        return False
    return _property_schema_key(property_schema) == (
        descriptor.property_schema_key
    )


def _property_schema_key(property_schema):
    """
    Canonical key for just the inline ``const`` / ``enum`` constraint.

    Both keywords present, or neither, is unindexable and represented by a
    distinct sentinel so it can never compare equal to a usable shape.
    """
    if "const" in property_schema:
        if "enum" in property_schema:
            return _AMBIGUOUS_CONSTRAINT
        return "const", _uniq_key(property_schema["const"])
    if "enum" in property_schema:
        return (
            "enum",
            tuple(
                sorted(
                    (_uniq_key(each) for each in property_schema["enum"]),
                    key=repr,
                ),
            ),
        )
    return _AMBIGUOUS_CONSTRAINT


_AMBIGUOUS_CONSTRAINT = ("ambiguous",)


def _branch_fingerprint_matches(branch, descriptor, resolver):
    """
    Re-derive a branch's fingerprint and compare it to the recorded one.

    This is the slow path of the freshness guard, used only when a cheap
    identity/key-set check notices the live branch changed.  Resolved
    ``$ref`` chains are re-walked so the comparison is against the same
    target schema observed at build time.
    """
    observed = branch
    if descriptor.ref_chain:
        observed, ref_chain = _resolve_static_ref_chain(branch, resolver)
        if observed is None or ref_chain != descriptor.ref_chain:
            return False
    elif isinstance(observed, Mapping) and "$ref" in observed:
        return False

    return _uniq_key(observed) == descriptor.fingerprint_key


def _resolve_static_ref_chain(branch, resolver):
    """
    Resolve a chain of plain ``$ref`` keywords, returning the target and
    the refs traversed, or ``(None, None)`` when anything is unprovable.
    """
    refs = []
    seen = frozenset()
    while isinstance(branch, Mapping) and "$ref" in branch:
        if any(keyword in branch for keyword in _DYNAMIC_REFERENCE_KEYWORDS):
            return None, None
        ref = branch["$ref"]
        if len(seen) >= _MAX_STATIC_REF_DEPTH or ref in seen:
            return None, None
        try:
            resolved = resolver.lookup(ref)
        except referencing.exceptions.Unresolvable:
            return None, None
        except Exception:  # noqa: BLE001 -- any other lookup failure is unsafe
            return None, None
        target = resolved.contents
        if not isinstance(target, Mapping):
            return None, None
        refs.append(ref)
        seen = seen | {ref}
        resolver = resolved.resolver
        branch = target
    return branch, tuple(refs)


def _branch_descriptor(index, branch, resolver):
    """
    Extract a `_BranchDescriptor` for one branch, or ``None`` if the branch
    cannot be proven discriminable.
    """
    if not isinstance(branch, Mapping) or branch is True or branch is False:
        return None
    if any(keyword in branch for keyword in _DYNAMIC_REFERENCE_KEYWORDS):
        return None
    if "$ref" in branch:
        branch, ref_chain = _resolve_static_ref_chain(branch, resolver)
        if branch is None:
            return None
    else:
        ref_chain = ()

    required = branch.get("required")
    # The discriminating property only has to be one of the required
    # properties; additional required fields constrain the branch further
    # but cannot make it applicable when the discriminator is absent.
    if not isinstance(required, list) or not required:
        return None
    if not all(isinstance(each, str) for each in required):
        return None

    properties = branch.get("properties")
    if not isinstance(properties, Mapping):
        return None

    # Prefer a property that is both required and directly constrained;
    # ``build_index`` then requires every branch to converge on the same
    # property name so the index has a single, stable lookup key.
    property = next(
        (each for each in required if each in properties),
        None,
    )
    if property is None:
        return None
    property_schema = properties[property]
    if not isinstance(property_schema, Mapping):
        return None
    if any(
        keyword in property_schema
        for keyword in ("$ref", *_DYNAMIC_REFERENCE_KEYWORDS)
    ):
        return None

    if "const" in property_schema:
        if "enum" in property_schema:
            return None
        values = (property_schema["const"],)
    elif "enum" in property_schema:
        values = tuple(property_schema["enum"])
        if not values:
            return None
    else:
        return None

    value_keys = set()
    linear_values = []
    for value in values:
        key = _uniq_key(value)
        if key is _UNSUPPORTED:
            linear_values.append(value)
        else:
            value_keys.add(key)

    if not value_keys and not linear_values:
        return None

    fingerprint_key = _uniq_key(branch)
    if fingerprint_key is _UNSUPPORTED:
        return None

    return _BranchDescriptor(
        index=index,
        required=property,
        value_keys=frozenset(value_keys),
        linear_values=tuple(linear_values),
        ref_chain=ref_chain,
        fingerprint_key=fingerprint_key,
        branch_keys=frozenset(branch),
        top_level_identities=tuple(
            (key, branch[key]) for key in branch
        ),
        property_schema_key=_property_schema_key(property_schema),
    )


def build_index(oneOf, registry, resolver):
    """
    Build a `DiscriminatorIndex` for ``oneOf``, or return ``None`` when the
    applicator does not have a provable discriminator.

    Only an applicator whose *every* branch is indexable on the same
    property is indexed; anything less leaves the exhaustive fallback path
    in place.
    """
    descriptors = []
    property = None
    for index, branch in enumerate(oneOf):
        descriptor = _branch_descriptor(index, branch, resolver)
        if descriptor is None:
            return None
        if property is not None and descriptor.required != property:
            return None
        property = descriptor.required
        descriptors.append(descriptor)

    if not descriptors or property is None:
        return None

    mapping: dict[Any, list[int]] = {}
    linear: list[tuple[Any, list[int]]] = []
    for descriptor in descriptors:
        for key in descriptor.value_keys:
            mapping.setdefault(key, []).append(descriptor.index)
        for value in descriptor.linear_values:
            for each, indexes in linear:
                if equal(each, value):
                    indexes.append(descriptor.index)
                    break
            else:
                linear.append((value, [descriptor.index]))

    return DiscriminatorIndex(
        property=property,
        mapping={key: tuple(indexes) for key, indexes in mapping.items()},
        linear=tuple(
            (value, tuple(indexes)) for value, indexes in linear
        ),
        descriptors=tuple(descriptors),
        branch_identities=tuple(oneOf),
        registry=registry,
    )


# The cache is a bounded LRU keyed by object *identity* of the branch
# list.  Neither lists nor plain dicts are hashable, weak-referenceable, or
# able to carry attributes, and ``id()`` is unsafe on its own because a
# replaced-and-collected list commonly has its id immediately reused by
# the replacement.  Entries therefore hold a strong reference to the live
# branch list and match with ``is``: a reused id resolves to a different
# object, fails the identity check, and never inherits the stale entry.
# The size cap bounds the resulting memory to a small constant number of
# recently used applicators.
_INDEX_CACHE: list[tuple[list, object]] = []
_INDEX_CACHE_MAX_SIZE = 256


def get_index(oneOf, containing_schema, registry, resolver):
    """
    Return a fresh `DiscriminatorIndex` for the given ``oneOf``, or ``None``.

    Built indexes are cached against the applicator's branch list.  Every
    retrieval verifies that the cached index still describes the live
    schema; a mutated or replaced schema rebuilds the index instead of
    reusing stale state.
    """
    if not _enabled or not isinstance(containing_schema, Mapping):
        return None

    position = next(
        (
            position
            for position, (branches, _) in enumerate(_INDEX_CACHE)
            if branches is oneOf
        ),
        None,
    )
    if position is not None:
        _, index = _INDEX_CACHE.pop(position)
        if index.is_fresh(oneOf, registry, resolver):
            _INDEX_CACHE.append((oneOf, index))
            _stats["cache_hits"] += 1
            return index
        _stats["stale_rebuilds"] += 1

    index = build_index(oneOf, registry, resolver)
    if index is None:
        _stats["unindexable"] += 1
        return None

    _INDEX_CACHE.append((oneOf, index))
    del _INDEX_CACHE[:-_INDEX_CACHE_MAX_SIZE]
    _stats["built"] += 1
    return index


def reset_cache():
    """Forget every cached index (used by tests and benchmarks)."""
    _INDEX_CACHE.clear()


_stats = {
    "built": 0,
    "cache_hits": 0,
    "stale_rebuilds": 0,
    "unindexable": 0,
    "branch_visits": 0,
    "fast_path_success": 0,
    "fallbacks": 0,
}


def stats():
    """Return a snapshot of index bookkeeping counters."""
    return dict(_stats)


def reset_stats():
    """Reset the index bookkeeping counters."""
    for key in _stats:
        _stats[key] = 0


def enable():
    """Turn candidate indexing on (the default)."""
    global _enabled
    _enabled = True


def disable():
    """Turn candidate indexing off, restoring exhaustive validation."""
    global _enabled
    _enabled = False


def is_enabled():
    """Whether candidate indexing is currently enabled."""
    return _enabled


_enabled = os.environ.get("JSONSCHEMA_DISABLE_DISCRIMINATOR_INDEX") is None
