"""
Tests for jsonschema.VersionedRegistry and jsonschema.RegistrySnapshot.
"""
from copy import copy, deepcopy
from unittest import TestCase
import pickle
import threading

from referencing import Resource
from referencing.exceptions import NoSuchResource, Unresolvable
import referencing.jsonschema
import pytest

from jsonschema import ValidationError, VersionedRegistry
from jsonschema.exceptions import (
    CircularReferenceError,
    RegistrySnapshotError,
    ResourceLoadError,
    VersionConflictError,
)

THING = "https://example.com/thing.json"

#: A mutable "service" whose resources change between registry versions.
SERVICE = {}

#: All URIs the service was asked for, in order.
SERVICE_CALLS = []


def service_retrieve(uri):
    """
    A picklable retrieve callable backed by the mutable SERVICE mapping.
    """
    SERVICE_CALLS.append(uri)
    try:
        return SERVICE[uri]
    except KeyError:
        raise NoSuchResource(ref=uri) from None


@pytest.fixture(autouse=True)
def clean_service():
    SERVICE.clear()
    SERVICE_CALLS.clear()
    yield
    SERVICE.clear()
    SERVICE_CALLS.clear()


def registry_with(**resources):
    SERVICE.update(resources)
    return VersionedRegistry(retrieve=service_retrieve)


class TestVersioningAndPinning(TestCase):
    def test_new_snapshot_uses_new_resource_version(self):
        registry = registry_with(**{THING: {"type": "string"}})
        schema = {"$ref": THING}

        old = registry.snapshot()
        old.validate("a string", schema)
        with self.assertRaises(ValidationError):
            old.validate(12, schema)

        SERVICE[THING] = {"type": "integer"}
        registry.invalidate(THING)

        new = registry.snapshot()
        self.assertNotEqual(old.version, new.version)
        new.validate(12, schema)
        with self.assertRaises(ValidationError):
            new.validate("a string", schema)

    def test_other_tenant_keeps_pinned_old_snapshot(self):
        registry = registry_with(**{THING: {"type": "string"}})
        schema = {"$ref": THING}

        pinned = registry.snapshot()
        pinned.validate("a string", schema)

        SERVICE[THING] = {"type": "integer"}
        registry.invalidate(THING)
        registry.snapshot().validate(12, schema)

        # the pinned snapshot still resolves the old resource version
        pinned.validate("a string", schema)
        with self.assertRaises(ValidationError):
            pinned.validate(12, schema)

    def test_one_call_is_fixed_to_one_snapshot(self):
        registry = registry_with(**{THING: {"type": "string"}})
        snapshot = registry.snapshot()
        schema = {"$ref": THING}

        snapshot.validate("a string", schema)
        SERVICE[THING] = {"type": "integer"}
        registry.invalidate(THING)

        # the same snapshot keeps serving the version it pinned
        snapshot.validate("a string", schema)
        self.assertEqual(SERVICE_CALLS, [THING])

    def test_set_resource_bumps_version_and_is_seen_by_new_snapshots(self):
        registry = VersionedRegistry()
        registry.set_resource(THING, {"type": "string"})
        first = registry.snapshot()

        registry.set_resource(THING, {"type": "integer"})
        second = registry.snapshot()

        self.assertEqual((first.version, second.version), (1, 2))
        schema = {"$ref": THING}
        first.validate("a string", schema)
        second.validate(12, schema)
        with self.assertRaises(ValidationError):
            first.validate(12, schema)
        with self.assertRaises(ValidationError):
            second.validate("a string", schema)

    def test_set_resource_accepts_resources(self):
        registry = VersionedRegistry()
        registry.set_resource(THING, {"type": "string"})
        registry.set_resource(
            "https://example.com/other.json",
            Resource.from_contents(
                {"type": "integer"},
                default_specification=referencing.jsonschema.DRAFT202012,
            ),
        )
        snapshot = registry.snapshot()
        snapshot.validate("ok", {"$ref": THING})
        snapshot.validate(1, {"$ref": "https://example.com/other.json"})

    def test_invalidate_all(self):
        registry = VersionedRegistry()
        registry.set_resource(THING, {"type": "string"})
        registry.snapshot().validate("ok", {"$ref": THING})

        version = registry.invalidate()
        self.assertEqual(version, 2)
        self.assertEqual(registry.version, 2)

        snapshot = registry.snapshot()
        with self.assertRaises(Unresolvable):
            snapshot.validate("boom", {"$ref": THING})

    def test_empty_fragment_invalidates_the_same_resource(self):
        registry = VersionedRegistry()
        registry.set_resource(THING, {"type": "string"})
        registry.invalidate(THING + "#")
        self.assertEqual(registry.version, 2)
        with self.assertRaises(Unresolvable):
            registry.snapshot().validate("boom", {"$ref": THING})


class TestLoadLog(TestCase):
    def test_loads_are_logged_with_version_and_snapshot(self):
        registry = registry_with(**{THING: {"type": "string"}})
        schema = {"$ref": THING}

        first = registry.snapshot()
        first.validate("ok", schema)

        SERVICE[THING] = {"type": "integer"}
        registry.invalidate(THING)
        second = registry.snapshot()
        second.validate(12, schema)

        log = registry.load_log
        self.assertEqual(len(log), 2)
        old_load, new_load = log
        self.assertEqual(
            (old_load.uri, new_load.uri),
            (THING, THING),
        )
        self.assertLess(old_load.version, new_load.version)
        self.assertLess(old_load.sequence, new_load.sequence)
        self.assertEqual(old_load.snapshot, first.token)
        self.assertEqual(new_load.snapshot, second.token)
        # the two loads of the same URI are distinguishable
        self.assertIsNot(old_load.resource, new_load.resource)
        self.assertIsNone(old_load.error)
        self.assertIsNone(new_load.error)

    def test_failed_loads_are_logged(self):
        registry = registry_with()
        snapshot = registry.snapshot()
        with self.assertRaises(Unresolvable):
            snapshot.validate(1, {"$ref": THING})

        (record,) = registry.load_log
        self.assertEqual(record.uri, THING)
        self.assertIsNone(record.resource)
        self.assertIsNotNone(record.error)

    def test_snapshot_load_log_is_per_snapshot(self):
        registry = registry_with(**{THING: {"type": "string"}})
        first = registry.snapshot()
        second = registry.snapshot()
        first.validate("ok", {"$ref": THING})

        self.assertEqual(len(first.load_log), 1)
        self.assertEqual(len(second.load_log), 0)
        self.assertEqual(len(registry.load_log), 1)

    def test_clear_load_log(self):
        registry = registry_with(**{THING: {"type": "string"}})
        registry.snapshot().validate("ok", {"$ref": THING})
        self.assertEqual(len(registry.load_log), 1)
        registry.clear_load_log()
        self.assertEqual(registry.load_log, ())


class TestCacheIsolation(TestCase):
    def test_cache_hits_do_not_cross_registry_instances(self):
        registry_with(**{THING: {"type": "string"}})
        one = VersionedRegistry(retrieve=service_retrieve)
        two = VersionedRegistry(retrieve=service_retrieve)

        one.snapshot().validate("ok", {"$ref": THING})
        one.snapshot().validate("ok", {"$ref": THING})
        two.snapshot().validate("ok", {"$ref": THING})

        # each snapshot of each registry loaded the resource itself
        self.assertEqual(SERVICE_CALLS, [THING, THING, THING])
        self.assertEqual(len(one.load_log), 2)
        self.assertEqual(len(two.load_log), 1)

    def test_cache_hit_within_a_single_snapshot(self):
        registry = registry_with(**{THING: {"type": "string"}})
        snapshot = registry.snapshot()
        schema = {
            "properties": {
                "a": {"$ref": THING},
                "b": {"$ref": THING},
            },
        }
        snapshot.validate({"a": "x", "b": "y"}, schema)
        snapshot.validate({"a": "x"}, schema)
        self.assertEqual(SERVICE_CALLS, [THING])

    def test_resource_identity_is_stable_within_a_snapshot(self):
        registry = registry_with(**{THING: {"type": "string"}})
        snapshot = registry.snapshot()
        snapshot.validate("ok", {"$ref": THING})

        (record,) = snapshot.load_log
        self.assertIs(snapshot.registry[THING], record.resource)
        self.assertIs(snapshot.registry[THING], record.resource)

    def test_snapshots_of_different_versions_have_different_resources(self):
        registry = registry_with(**{THING: {"type": "string"}})
        old = registry.snapshot()
        old.validate("ok", {"$ref": THING})

        SERVICE[THING] = {"type": "integer"}
        registry.invalidate(THING)
        new = registry.snapshot()
        new.validate(1, {"$ref": THING})

        self.assertIsNot(old.registry[THING], new.registry[THING])


class TestConcurrency(TestCase):
    def test_concurrent_validation_loads_each_resource_once(self):
        registry = registry_with(**{THING: {"type": "string"}})
        snapshot = registry.snapshot()
        schema = {"$ref": THING}

        barrier = threading.Barrier(8)
        errors = []

        def validate():
            barrier.wait()
            try:
                snapshot.validate("ok", schema)
            except Exception as error:  # noqa: BLE001
                errors.append(error)

        threads = [threading.Thread(target=validate) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(SERVICE_CALLS, [THING])
        self.assertEqual(len(registry.load_log), 1)

    def test_concurrent_update_with_stale_version_fails(self):
        registry = VersionedRegistry()
        registry.set_resource(THING, {"type": "string"})
        version = registry.version

        registry.set_resource(THING, {"type": "integer"})
        with self.assertRaises(VersionConflictError) as caught:
            registry.set_resource(
                THING,
                {"type": "boolean"},
                expected_version=version,
            )
        self.assertEqual(caught.exception.expected, version)
        self.assertEqual(caught.exception.actual, version + 1)

        # the conflicting update did not go through
        registry.snapshot().validate(12, {"$ref": THING})

    def test_update_with_matching_expected_version_succeeds(self):
        registry = VersionedRegistry()
        registry.set_resource(THING, {"type": "string"})
        new_version = registry.set_resource(
            THING,
            {"type": "integer"},
            expected_version=registry.version,
        )
        self.assertEqual(new_version, registry.version)
        registry.snapshot().validate(12, {"$ref": THING})

    def test_invalidate_with_stale_version_fails(self):
        registry = VersionedRegistry()
        registry.set_resource(THING, {"type": "string"})
        with self.assertRaises(VersionConflictError):
            registry.invalidate(THING, expected_version=0)
        # the resource is still there
        registry.snapshot().validate("ok", {"$ref": THING})


class TestErrors(TestCase):
    def test_load_errors_are_identifiable(self):
        def broken(uri):
            raise OSError("boom")

        registry = VersionedRegistry(retrieve=broken)
        snapshot = registry.snapshot()
        with self.assertRaises(ResourceLoadError) as caught:
            snapshot.validate(1, {"$ref": THING})

        error = caught.exception
        self.assertIsInstance(error, RegistrySnapshotError)
        self.assertEqual(error.uri, THING)
        self.assertEqual(error.version, snapshot.version)
        self.assertIsInstance(error.cause, OSError)

        (record,) = registry.load_log
        self.assertIs(record.error, error)

    def test_failed_loads_are_not_cached(self):
        attempts = []

        def flaky(uri):
            attempts.append(uri)
            if len(attempts) == 1:
                raise OSError("boom")
            return {"type": "string"}

        registry = VersionedRegistry(retrieve=flaky)
        snapshot = registry.snapshot()
        with self.assertRaises(ResourceLoadError):
            snapshot.validate(1, {"$ref": THING})
        snapshot.validate("ok", {"$ref": THING})
        self.assertEqual(attempts, [THING, THING])

    def test_missing_resource_is_unresolvable(self):
        registry = VersionedRegistry()
        with self.assertRaises(Unresolvable):
            registry.snapshot().validate(1, {"$ref": THING})

    def test_circular_references_are_identifiable(self):
        registry = VersionedRegistry()
        registry.set_resource(
            "https://example.com/a.json",
            {"$ref": "b.json"},
        )
        registry.set_resource(
            "https://example.com/b.json",
            {"$ref": "a.json"},
        )
        snapshot = registry.snapshot()
        with self.assertRaises(CircularReferenceError):
            snapshot.validate(1, {"$ref": "https://example.com/a.json"})

    def test_self_references_are_identifiable(self):
        registry = VersionedRegistry()
        registry.set_resource(THING, {"$ref": THING})
        with self.assertRaises(CircularReferenceError):
            registry.snapshot().validate(1, {"$ref": THING})

    def test_reentrant_retrieval_cycle_is_identifiable(self):
        def reentrant(uri):
            # a loader which (incorrectly) consults the snapshot itself
            backend = holder["snapshot"].registry
            return backend.get_or_retrieve(uri).value.contents

        registry = VersionedRegistry(retrieve=reentrant)
        snapshot = registry.snapshot()
        holder = {"snapshot": snapshot}
        with self.assertRaises(CircularReferenceError) as caught:
            snapshot.validate(1, {"$ref": THING})
        self.assertIn(THING, caught.exception.chain)


class TestURISemantics(TestCase):
    def test_relative_references_resolve_against_base_uri(self):
        registry = registry_with(
            **{
                "https://example.com/schemas/root.json": {
                    "$id": "https://example.com/schemas/root.json",
                    "properties": {"x": {"$ref": "other.json"}},
                },
                "https://example.com/schemas/other.json": {
                    "type": "string",
                },
            },
        )
        snapshot = registry.snapshot()
        snapshot.validate(
            {"x": "ok"},
            {"$ref": "https://example.com/schemas/root.json"},
        )
        self.assertEqual(
            SERVICE_CALLS,
            [
                "https://example.com/schemas/root.json",
                "https://example.com/schemas/other.json",
            ],
        )

    def test_empty_fragment_addresses_the_same_resource(self):
        registry = registry_with(**{THING: {"type": "string"}})
        snapshot = registry.snapshot()
        schema = {
            "properties": {
                "a": {"$ref": THING},
                "b": {"$ref": THING + "#"},
            },
        }
        snapshot.validate({"a": "x", "b": "y"}, schema)
        self.assertEqual(SERVICE_CALLS, [THING])
        (record,) = registry.load_log
        self.assertEqual(record.uri, THING)

    def test_percent_encoded_uris_are_preserved(self):
        encoded = "https://example.com/sch%C3%A9ma.json"
        registry = registry_with(
            **{
                "https://example.com/root.json": {
                    "$id": "https://example.com/root.json",
                    "$ref": "sch%C3%A9ma.json",
                },
                encoded: {"type": "string"},
            },
        )
        snapshot = registry.snapshot()
        snapshot.validate("ok", {"$ref": "https://example.com/root.json"})
        self.assertIn(encoded, SERVICE_CALLS)

    def test_cross_protocol_references_are_absolute(self):
        registry = registry_with(
            **{
                "https://example.com/root.json": {
                    "$id": "https://example.com/root.json",
                    "properties": {
                        "a": {"$ref": "http://example.com/a.json"},
                        "b": {"$ref": "urn:example:schema"},
                    },
                },
                "http://example.com/a.json": {"type": "string"},
                "urn:example:schema": {"type": "integer"},
            },
        )
        snapshot = registry.snapshot()
        snapshot.validate(
            {"a": "x", "b": 1},
            {"$ref": "https://example.com/root.json"},
        )
        with self.assertRaises(ValidationError):
            snapshot.validate(
                {"a": 1, "b": "x"},
                {"$ref": "https://example.com/root.json"},
            )
        self.assertEqual(
            SERVICE_CALLS,
            [
                "https://example.com/root.json",
                "http://example.com/a.json",
                "urn:example:schema",
            ],
        )

    def test_anchors_resolve_within_resources(self):
        registry = registry_with(
            **{
                THING: {
                    "$id": THING,
                    "$defs": {
                        "positive": {
                            "$anchor": "positive",
                            "type": "integer",
                            "minimum": 1,
                        },
                    },
                },
            },
        )
        snapshot = registry.snapshot()
        schema = {"$ref": THING + "#positive"}
        snapshot.validate(3, schema)
        with self.assertRaises(ValidationError):
            snapshot.validate(-3, schema)

    def test_plain_name_fragments_resolve_to_anchors(self):
        registry = registry_with(
            **{
                "https://example.com/root.json": {
                    "$id": "https://example.com/root.json",
                    "$ref": "other.json#foo",
                },
                "https://example.com/other.json": {
                    "$id": "https://example.com/other.json",
                    "$defs": {
                        "foo": {"$anchor": "foo", "type": "string"},
                    },
                },
            },
        )
        snapshot = registry.snapshot()
        snapshot.validate("ok", {"$ref": "https://example.com/root.json"})
        with self.assertRaises(ValidationError):
            snapshot.validate(1, {"$ref": "https://example.com/root.json"})


class TestDynamicRef(TestCase):
    STRICT_TREE_V1 = {"$ref": "tree.json"}
    STRICT_TREE_V2 = {
        "$id": "https://example.com/strict-tree.json",
        "$dynamicAnchor": "node",
        "$ref": "tree.json",
        "unevaluatedProperties": False,
    }
    TREE = {
        "$id": "https://example.com/tree.json",
        "$dynamicAnchor": "node",
        "type": "object",
        "properties": {
            "data": True,
            "children": {
                "type": "array",
                "items": {"$dynamicRef": "#node"},
            },
        },
    }
    INSTANCE = {"children": [{"daft": 1}]}

    def registry(self):
        registry = VersionedRegistry()
        registry.set_resource("https://example.com/tree.json", self.TREE)
        registry.set_resource(
            "https://example.com/strict-tree.json",
            self.STRICT_TREE_V1,
        )
        return registry

    def test_dynamic_ref_uses_the_snapshot_resource_version(self):
        registry = self.registry()
        schema = {"$ref": "https://example.com/strict-tree.json"}

        old = registry.snapshot()
        old.validate(self.INSTANCE, schema)

        registry.set_resource(
            "https://example.com/strict-tree.json",
            self.STRICT_TREE_V2,
        )
        new = registry.snapshot()
        with self.assertRaises(ValidationError):
            new.validate(self.INSTANCE, schema)

        # the old snapshot still resolves the old, non-strict version
        old.validate(self.INSTANCE, schema)

    def test_dynamic_ref_load_counts_follow_the_snapshot(self):
        registry = self.registry()
        old = registry.snapshot()
        old.validate(self.INSTANCE, {"$ref": "https://example.com/strict-tree.json"})
        self.assertEqual(registry.load_log, ())

        registry.set_resource(
            "https://example.com/strict-tree.json",
            self.STRICT_TREE_V2,
        )
        new = registry.snapshot()
        self.assertEqual(new.version, old.version + 1)
        with self.assertRaises(ValidationError):
            new.validate(
                self.INSTANCE,
                {"$ref": "https://example.com/strict-tree.json"},
            )
        self.assertEqual(registry.load_log, ())


class TestCopyAndPickle(TestCase):
    def registry(self):
        return registry_with(**{THING: {"type": "string"}})

    def test_copy_shares_the_snapshot_cache(self):
        registry = self.registry()
        snapshot = registry.snapshot()
        snapshot.validate("ok", {"$ref": THING})

        copied = copy(snapshot)
        copied.validate("ok", {"$ref": THING})
        self.assertEqual(SERVICE_CALLS, [THING])

    def test_deepcopy_has_an_independent_cache_but_keeps_resources(self):
        registry = self.registry()
        snapshot = registry.snapshot()
        snapshot.validate("ok", {"$ref": THING})

        copied = deepcopy(snapshot)
        self.assertIsNot(copied, snapshot)
        # no reload happens: resources are shared, caches are not
        copied.validate("ok", {"$ref": THING})
        self.assertEqual(SERVICE_CALLS, [THING])
        self.assertIs(copied.registry[THING], snapshot.registry[THING])

        SERVICE[THING] = {"type": "integer"}
        registry.invalidate(THING)
        # the deepcopy still pins the old version
        copied.validate("ok", {"$ref": THING})
        with self.assertRaises(ValidationError):
            copied.validate(1, {"$ref": THING})

    def test_pickle_preserves_version_and_avoids_reloads(self):
        registry = self.registry()
        snapshot = registry.snapshot()
        snapshot.validate("ok", {"$ref": THING})
        loads = len(SERVICE_CALLS)

        restored = pickle.loads(pickle.dumps(snapshot))
        self.assertEqual(restored.version, snapshot.version)
        self.assertEqual(restored.token, snapshot.token)

        restored.validate("ok", {"$ref": THING})
        with self.assertRaises(ValidationError):
            restored.validate(1, {"$ref": THING})
        self.assertEqual(len(SERVICE_CALLS), loads)

    def test_pickle_detached_snapshot_still_loads_new_resources(self):
        registry = self.registry()
        snapshot = registry.snapshot()
        SERVICE["https://example.com/other.json"] = {"type": "integer"}

        restored = pickle.loads(pickle.dumps(snapshot))
        restored.validate(1, {"$ref": "https://example.com/other.json"})
        self.assertIn("https://example.com/other.json", SERVICE_CALLS)
        # detached snapshots keep their own log
        self.assertEqual(len(restored.load_log), 1)
        (record,) = restored.load_log
        self.assertEqual(record.uri, "https://example.com/other.json")
        self.assertEqual(record.version, snapshot.version)

    def test_pickle_registry_preserves_version_and_store(self):
        registry = self.registry()
        registry.set_resource(THING, {"type": "string"})
        snapshot = registry.snapshot()
        snapshot.validate("ok", {"$ref": THING})

        restored = pickle.loads(pickle.dumps(registry))
        self.assertEqual(restored.version, registry.version)
        new = restored.snapshot()
        self.assertEqual(new.version, registry.version)
        new.validate("ok", {"$ref": THING})

        # the restored registry keeps evolving independently
        restored.set_resource(THING, {"type": "integer"})
        restored.snapshot().validate(1, {"$ref": THING})
        snapshot.validate("ok", {"$ref": THING})

    def test_copy_and_deepcopy_registry(self):
        registry = self.registry()
        registry.set_resource(THING, {"type": "string"})

        shallow = copy(registry)
        self.assertEqual(shallow.version, registry.version)
        shallow.snapshot().validate("ok", {"$ref": THING})

        deep = deepcopy(registry)
        deep.set_resource(THING, {"type": "integer"})
        # mutating the deepcopy does not affect the original
        self.assertNotEqual(deep.version, registry.version)
        registry.snapshot().validate("ok", {"$ref": THING})
        deep.snapshot().validate(1, {"$ref": THING})

    def test_invalidate_after_pickle_follows_the_new_version(self):
        registry = self.registry()
        registry.set_resource(THING, {"type": "string"})
        restored = pickle.loads(pickle.dumps(registry.snapshot()))

        SERVICE[THING] = {"type": "integer"}
        registry.invalidate(THING)
        registry.snapshot().validate(1, {"$ref": THING})

        # the restored snapshot is still pinned to the old version
        restored.validate("ok", {"$ref": THING})
        with self.assertRaises(ValidationError):
            restored.validate(1, {"$ref": THING})
