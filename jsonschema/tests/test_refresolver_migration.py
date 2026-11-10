"""
Executable migration samples from the deprecated ``RefResolver`` to
`referencing.Registry`.

The tests in this module are deliberately written as *side by side*
migrations: for each supported dialect we build the same logical setup once
through the legacy `jsonschema.RefResolver` (a local ``store`` plus a custom
URI ``handlers`` entry) and once through a `referencing.Registry`, then assert
that the two produce byte-for-byte equivalent validation outcomes.

The scenarios intentionally cover more than swapping a constructor call:

* relative ``id`` / ``$id`` resolution and nested (in-document) resources,
* circular references that hop between documents and back,
* ``$ref`` with adjacent keywords, whose meaning changed across drafts,
* a local store that ships resources together with the application,
* a custom URI scheme handler adapted into a ``retrieve`` callable,
* counting how many times a resource is actually fetched,
* unregistered URIs failing loudly instead of triggering a network download.

Nothing in here is allowed to reach the network.
"""

from __future__ import annotations

from unittest import TestCase, mock
import warnings

from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource
from referencing.jsonschema import DRAFT4, DRAFT201909, DRAFT202012
import referencing.exceptions

from jsonschema import (
    Draft4Validator,
    Draft201909Validator,
    Draft202012Validator,
    exceptions,
)


def _ref_resolver():
    # Imported lazily so merely collecting this module does not emit the
    # RefResolver deprecation warning.
    from jsonschema import RefResolver

    return RefResolver


def _error_signature(error):
    """
    Normalize a `ValidationError` to the parts a migration must keep stable:

    the human readable message, the instance path and the schema path.
    """
    return (
        error.message,
        [str(part) for part in error.absolute_path],
        [str(part) for part in error.absolute_schema_path],
    )


def _signatures(validator, instance):
    return sorted(
        _error_signature(error) for error in validator.iter_errors(instance)
    )


def _outcome(validator, instance):
    """
    Return ``(is_valid, sorted_error_signatures)`` from a single
    ``iter_errors`` pass.

    A second validation call on a RefResolver-backed validator would mutate
    its shared scope stack, so the legacy comparison must never validate the
    same instance twice.
    """
    signatures = _signatures(validator, instance)
    return not signatures, signatures


class _CountingRetriever:
    """
    Stand-in for an application's custom URI handler.

    It records every URI it is asked for and only knows about resources that
    were explicitly handed to it. Anything else raises `NoSuchResource`,
    which is the documented, locatable way to say "this URI is not provided",
    rather than silently falling back to a network download.
    """

    def __init__(self, documents):
        self._documents = documents
        self.calls = []

    def __call__(self, uri):
        self.calls.append(uri)
        try:
            return self._documents[uri]
        except KeyError:
            raise NoSuchResource(ref=uri) from None


def _silence_deprecation_warnings():
    return warnings.catch_warnings(
        action="ignore",
        category=DeprecationWarning,
    )


class Draft4MigrationTest(TestCase):
    """
    Draft 4 uses the bare ``id`` keyword (not ``$id``) and ignores every
    keyword sitting next to a ``$ref``. Both behaviors must be preserved by
    pinning the resource to `DRAFT4` rather than re-interpreting it under a
    newer dialect.
    """

    root_uri = "http://example.com/draft4/node"
    child_uri = "http://example.com/draft4/child"

    def setUp(self):
        # The child resource is identified by a *relative* id; it points back
        # to the root by its absolute URI, forming a cross-document cycle.
        self.child = {
            "id": "child",
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "parent": {"$ref": self.root_uri},
            },
            "required": ["name"],
        }
        # The sibling ``minimum`` next to ``$ref`` must be ignored on
        # Draft 4; it is kept here to prove we do not apply newer semantics.
        self.root = {
            "id": self.root_uri,
            "type": "object",
            "properties": {
                "label": {"type": "string"},
                "child": {"$ref": "child"},
                "count": {
                    "$ref": "#/definitions/positive",
                    "minimum": 99,
                },
            },
            "definitions": {
                "positive": {"type": "integer", "minimum": 0},
            },
        }
        self.valid_instance = {
            "label": "root",
            "count": 7,
            "child": {
                "name": "kid",
                "parent": {"label": "deep", "child": {"name": "nested"}},
            },
        }
        self.invalid_child = {"child": {"parent": {}}}
        self.negative_count = {"count": -5}
        self.ignored_sibling = {"count": 5}

    def _legacy_validator(self, fetches):
        def handler(uri):
            fetches.append(uri)
            return self.child

        resolver = _ref_resolver().from_schema(
            self.root,
            id_of=Draft4Validator.ID_OF,
            store={self.child_uri: self.child},
            handlers={"custom": handler},
        )
        return Draft4Validator(self.root, resolver=resolver)

    def _registry_validator(self, retriever):
        registry = Registry(retrieve=retriever).with_resources(
            [
                (self.root_uri, DRAFT4.create_resource(self.root)),
                (self.child_uri, DRAFT4.create_resource(self.child)),
            ],
        )
        return Draft4Validator(self.root, registry=registry)

    def test_validation_results_and_error_paths_match(self):
        # A fresh validator per instance mirrors how applications normally
        # validate. Reusing one RefResolver-backed validator across instances
        # is itself unsafe (see test_legacy_resolver_leaks_resolution_scope),
        # so the parity comparison does not rely on that behavior.
        for instance in (
            self.valid_instance,
            self.invalid_child,
            self.negative_count,
            self.ignored_sibling,
        ):
            with _silence_deprecation_warnings():
                legacy = self._legacy_validator(fetches=[])
            migrated = self._registry_validator(
                _CountingRetriever({self.child_uri: DRAFT4.create_resource(
                    self.child,
                )}),
            )
            self.assertEqual(
                _outcome(migrated, instance),
                _outcome(legacy, instance),
            )

    def test_legacy_resolver_leaks_resolution_scope(self):
        """
        The deprecated resolver carries a mutable resolution-scope stack. When
        a cross-document ref is evaluated *before* a same-document fragment
        ref inside one validation, the fragment is wrongly resolved against
        the nested document and fails.

        `referencing.Registry` tracks scope per resource and has no shared
        stack, so it resolves both correctly. The divergence is pinned here
        explicitly rather than silently replicated.
        """
        root_uri = "http://example.com/draft4/leak/"
        child_uri = root_uri + "child"
        child = {"id": "child", "type": "object"}
        # Property order matters for the legacy bug: the cross-document ref
        # is visited before the same-document ``#/definitions`` ref.
        root = {
            "id": root_uri,
            "type": "object",
            "properties": {
                "child": {"$ref": "child"},
                "count": {"$ref": "#/definitions/int"},
            },
            "definitions": {"int": {"type": "integer"}},
        }
        instance = {"child": {}, "count": 3}

        with _silence_deprecation_warnings():
            legacy_resolver = _ref_resolver().from_schema(
                root,
                id_of=Draft4Validator.ID_OF,
                store={child_uri: child},
            )
            legacy = Draft4Validator(root, resolver=legacy_resolver)
        with self.assertRaises(exceptions._RefResolutionError):
            legacy.is_valid(instance)

        registry = Registry().with_resources(
            [
                (root_uri, DRAFT4.create_resource(root)),
                (child_uri, DRAFT4.create_resource(child)),
            ],
        )
        migrated = Draft4Validator(root, registry=registry)
        self.assertTrue(migrated.is_valid(instance))

    def test_ref_adjacent_keyword_is_ignored_like_draft4_requires(self):
        with _silence_deprecation_warnings():
            legacy = self._legacy_validator(fetches=[])
        migrated = self._registry_validator(_CountingRetriever({}))

        # minimum: 99 next to $ref is ignored; the referenced minimum: 0 wins.
        self.assertTrue(migrated.is_valid(self.ignored_sibling))
        self.assertTrue(legacy.is_valid(self.ignored_sibling))
        self.assertFalse(migrated.is_valid(self.negative_count))
        self.assertFalse(legacy.is_valid(self.negative_count))

    def test_shipped_store_resources_are_not_fetched(self):
        fetches = []
        retriever = _CountingRetriever(
            {self.child_uri: DRAFT4.create_resource(self.child)},
        )
        with _silence_deprecation_warnings():
            legacy = self._legacy_validator(fetches)
            migrated = self._registry_validator(retriever)

        self.assertTrue(migrated.is_valid(self.valid_instance))
        self.assertTrue(legacy.is_valid(self.valid_instance))
        # The child is preloaded in both setups, so nothing is fetched.
        self.assertEqual(retriever.calls, [])
        self.assertEqual(fetches, [])

    def test_shipped_resources_are_enumerable_by_the_caller(self):
        registry = Registry().with_resources(
            [
                (self.root_uri, DRAFT4.create_resource(self.root)),
                (self.child_uri, DRAFT4.create_resource(self.child)),
            ],
        )
        self.assertEqual(
            set(registry.crawl().keys()),
            {self.child_uri, self.root_uri},
        )
        self.assertIn(self.child_uri, registry.crawl())

    def test_unregistered_uri_fails_without_network(self):
        ref = "http://unregistered.example/draft4/missing"
        with _silence_deprecation_warnings():
            legacy_resolver = _ref_resolver().from_schema({})
            legacy = Draft4Validator({"$ref": ref}, resolver=legacy_resolver)
        migrated = Draft4Validator(
            {"$ref": ref},
            registry=Registry(
                retrieve=_CountingRetriever({}),
            ),
        )

        with self.assertRaises(exceptions._WrappedReferencingError) as caught:
            migrated.validate({})
        self.assertIsInstance(
            caught.exception.args[0],
            referencing.exceptions.Unresolvable,
        )
        self.assertEqual(caught.exception.args[0].ref, ref)

        # The legacy object raised a different, RefResolver-specific error.
        # Stub the network entry point so the comparison stays hermetic and
        # still proves that RefResolver only failed *after* attempting a
        # remote fetch, which the registry never does.
        with mock.patch.object(
            legacy_resolver,
            "resolve_remote",
            side_effect=OSError("network disabled in tests"),
        ) as resolve_remote, self.assertRaises(
            exceptions._RefResolutionError,
        ):
            legacy.validate({})
        resolve_remote.assert_called_once_with(ref)


class _ModernDraftMigration:
    """
    Shared checks for Draft 2019-09 and 2020-12.

    Both drafts use ``$id`` and *do* evaluate keywords adjacent to a
    ``$ref``. The resource is pinned to the matching `Specification` so an old
    schema is never silently read under the newest dialect.
    """

    specification = None
    validator_class = None
    tag = None

    def setUp(self):
        self.root_uri = f"http://example.com/{self.tag}/"
        self.child_uri = self.root_uri + "child"
        self.child = {
            "$schema": (
                f"https://json-schema.org/draft/{self.tag}/schema"
            ),
            "$id": "child",
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "root": {"$ref": self.root_uri},
            },
            "required": ["name"],
            "$anchor": "thing",
        }
        self.root = {
            "$schema": (
                f"https://json-schema.org/draft/{self.tag}/schema"
            ),
            "$id": self.root_uri,
            "type": "object",
            "properties": {
                # Adjacent minimum MUST be applied on these drafts.
                "count": {"$ref": "#/$defs/int", "minimum": 5},
                "label": {"type": "string"},
                "child": {"$ref": "child"},
            },
            "$defs": {"int": {"type": "integer"}},
        }
        self.valid_instance = {
            "label": "a",
            "count": 7,
            "child": {
                "name": "c",
                "root": {"label": "r"},
            },
        }
        self.below_adjacent_minimum = {"count": 2}
        self.missing_child_name = {"child": {"root": {}}}

    def _legacy_validator(self, fetches):
        def handler(uri):
            fetches.append(uri)
            return self.child

        resolver = _ref_resolver().from_schema(
            self.root,
            id_of=self.validator_class.ID_OF,
            store={self.child_uri: self.child},
            handlers={"custom": handler},
        )
        return self.validator_class(self.root, resolver=resolver)

    def _registry_validator(self, retriever):
        registry = Registry(retrieve=retriever).with_resources(
            [
                (
                    self.root_uri,
                    self.specification.create_resource(self.root),
                ),
                (
                    self.child_uri,
                    self.specification.create_resource(self.child),
                ),
            ],
        )
        return self.validator_class(self.root, registry=registry)

    def test_validation_results_and_error_paths_match(self):
        # The same-document ref is validated first; a reused legacy resolver
        # leaks the child scope if a cross-document ref runs before it.
        for instance in (
            self.below_adjacent_minimum,
            self.valid_instance,
            self.missing_child_name,
        ):
            with _silence_deprecation_warnings():
                legacy = self._legacy_validator(fetches=[])
            migrated = self._registry_validator(
                _CountingRetriever(
                    {
                        self.child_uri: self.specification.create_resource(
                            self.child,
                        ),
                    },
                ),
            )
            self.assertEqual(
                _outcome(migrated, instance),
                _outcome(legacy, instance),
            )

        migrated = self._registry_validator(_CountingRetriever({}))
        signature = _signatures(
            migrated,
            self.below_adjacent_minimum,
        )
        self.assertEqual(
            signature,
            [
                (
                    "2 is less than the minimum of 5",
                    ["count"],
                    ["properties", "count", "minimum"],
                ),
            ],
        )

    def test_ref_adjacent_keyword_is_applied_on_modern_drafts(self):
        with _silence_deprecation_warnings():
            legacy = self._legacy_validator(fetches=[])
            migrated = self._registry_validator(_CountingRetriever({}))
        self.assertFalse(migrated.is_valid(self.below_adjacent_minimum))
        self.assertFalse(legacy.is_valid(self.below_adjacent_minimum))
        self.assertTrue(migrated.is_valid({"count": 5}))
        self.assertTrue(legacy.is_valid({"count": 5}))

    def test_relative_nested_resource_and_cycle_validate(self):
        with _silence_deprecation_warnings():
            legacy = self._legacy_validator(fetches=[])
            migrated = self._registry_validator(
                _CountingRetriever(
                    {
                        self.child_uri: self.specification.create_resource(
                            self.child,
                        ),
                    },
                ),
            )
        self.assertTrue(migrated.is_valid(self.valid_instance))
        self.assertTrue(legacy.is_valid(self.valid_instance))

    def test_unregistered_uri_raises_unresolvable_not_network(self):
        ref = f"http://unregistered.example/{self.tag}/missing"
        migrated = self.validator_class(
            {"$ref": ref},
            registry=Registry(retrieve=_CountingRetriever({})),
        )
        with self.assertRaises(exceptions._WrappedReferencingError) as caught:
            migrated.validate({})
        underlying = caught.exception.args[0]
        self.assertIsInstance(
            underlying,
            referencing.exceptions.Unresolvable,
        )
        self.assertEqual(underlying.ref, ref)


class Draft201909MigrationTest(_ModernDraftMigration, TestCase):
    specification = DRAFT201909
    validator_class = Draft201909Validator
    tag = "2019-09"


class Draft202012MigrationTest(_ModernDraftMigration, TestCase):
    specification = DRAFT202012
    validator_class = Draft202012Validator
    tag = "2020-12"


class CustomHandlerMigrationTest(TestCase):
    """
    A legacy ``handlers={scheme: callable}`` entry moves to a single
    ``retrieve`` callable. We additionally show how to restore the legacy
    cache-once behavior, since a plain `Registry` does not memoize fetches.
    """

    remote_uri = "http://app.example/remote"
    remote_contents = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "integer",
        "minimum": 0,
    }

    def test_handler_is_adapted_into_a_retrieve_callable(self):
        calls = []

        def legacy_handler(uri):
            calls.append(uri)
            return self.remote_contents

        with _silence_deprecation_warnings():
            legacy = Draft202012Validator(
                {"$ref": self.remote_uri},
                resolver=_ref_resolver()(
                    "",
                    {},
                    handlers={"http": legacy_handler},
                ),
            )

        retriever = _CountingRetriever(
            {
                self.remote_uri: Resource.from_contents(
                    self.remote_contents,
                ),
            },
        )
        migrated = Draft202012Validator(
            {"$ref": self.remote_uri},
            registry=Registry(retrieve=retriever),
        )

        self.assertEqual(migrated.is_valid(-1), legacy.is_valid(-1))
        self.assertFalse(migrated.is_valid(-1))
        self.assertTrue(migrated.is_valid(3))

        # Both engines are asked for the same explicit URI. A plain Registry
        # does not memoize retrieval (unlike RefResolver's remote cache), so we
        # compare the set of requested URIs here; cache-once behavior is
        # covered separately in test_cached_retriever_restores_single_fetch.
        self.assertEqual(set(retriever.calls), {self.remote_uri})
        self.assertEqual(set(calls), {self.remote_uri})

    def test_cached_retriever_restores_single_fetch_semantics(self):
        import referencing.retrieval

        calls = []

        @referencing.retrieval.to_cached_resource()
        def retrieve(uri):
            calls.append(uri)
            if uri != self.remote_uri:
                raise NoSuchResource(ref=uri)
            import json

            return json.dumps(self.remote_contents)

        registry = Registry(retrieve=retrieve)
        for _ in range(3):
            validator = Draft202012Validator(
                {"$ref": self.remote_uri},
                registry=registry,
            )
            self.assertFalse(validator.is_valid(-1))

        # The legacy RefResolver cached remote documents; the lru_cache on the
        # retriever restores that "fetch each URI at most once" guarantee.
        self.assertEqual(calls, [self.remote_uri])

    def test_unknown_scheme_is_not_implicitly_downloaded(self):
        retriever = _CountingRetriever({})
        validator = Draft202012Validator(
            {"$ref": "ftp://files.example/schema.json"},
            registry=Registry(retrieve=retriever),
        )
        with self.assertRaises(exceptions._WrappedReferencingError):
            validator.validate({})
        # The retriever was consulted once and refused; no fallback fetch.
        self.assertEqual(retriever.calls, ["ftp://files.example/schema.json"])
