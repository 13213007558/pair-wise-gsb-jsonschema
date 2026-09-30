from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread
from unittest import TestCase
import copy
import pickle

from referencing.jsonschema import DRAFT202012

from jsonschema import Draft202012Validator, VersionedRegistry, registry


def resource(schema):
    return DRAFT202012.create_resource(schema)


class VersionedRegistryTests(TestCase):
    def test_explicit_snapshots_keep_old_resource_after_update(self):
        old = resource({"type": "integer"})
        new = resource({"type": "string"})
        registries = VersionedRegistry(
            resources=[("https://example.com/schema", old)],
        )
        pinned = registries.snapshot(0)

        self.assertEqual(registries.put("https://example.com/schema", new), 1)

        self.assertIs(
            pinned.registry.contents("https://example.com/schema"),
            old.contents,
        )
        current = registries.snapshot(1)
        self.assertIs(
            current.registry.contents("https://example.com/schema"),
            new.contents,
        )

    def test_snapshot_pinned_before_update_remains_unchanged(self):
        old = resource({"type": "integer"})
        new = resource({"type": "string"})
        registries = VersionedRegistry(resources=[("urn:immutable", old)])
        pinned = registries.snapshot()

        registries.put("urn:immutable", new)

        self.assertIs(
            pinned.registry.contents("urn:immutable"),
            old.contents,
        )

    def test_validation_pins_one_snapshot_and_switches_between_calls(self):
        versions = [
            resource({"type": "integer"}),
            resource({"type": "string"}),
        ]
        loaded_versions = []

        def retrieve(uri, version):
            loaded_versions.append((uri, version))
            return versions[version]

        registries = VersionedRegistry(retrieve)
        validator = Draft202012Validator(
            {"$ref": "https://example.com/versioned"},
            registry=registries,
        )

        self.assertTrue(validator.is_valid(12))
        self.assertEqual(
            loaded_versions,
            [("https://example.com/versioned", 0)],
        )
        registries.invalidate("https://example.com/versioned")
        self.assertFalse(validator.is_valid(12))
        self.assertEqual(
            loaded_versions[-1],
            ("https://example.com/versioned", 1),
        )
        self.assertEqual(registries.load_count, 2)
        self.assertIs(registries.loads[0].resource, versions[0])
        self.assertIs(registries.loads[1].resource, versions[1])
        self.assertEqual(registries.loads[0].version, 0)
        self.assertEqual(registries.loads[1].version, 1)
        self.assertNotEqual(
            registries.loads[0].resource_id,
            registries.loads[1].resource_id,
        )

    def test_old_tenant_remains_pinned_after_lazy_reload(self):
        versions = [
            resource({"type": "integer"}),
            resource({"type": "string"}),
        ]
        registries = VersionedRegistry(lambda uri, version: versions[version])
        old_validator = Draft202012Validator(
            {"$ref": "urn:tenant"},
            registry=registries.pin(0),
        )

        self.assertTrue(old_validator.is_valid(1))
        registries.invalidate("urn:tenant")
        new_validator = Draft202012Validator(
            {"$ref": "urn:tenant"},
            registry=registries,
        )

        self.assertFalse(new_validator.is_valid(1))
        self.assertTrue(old_validator.is_valid(1))
        self.assertEqual(registries.load_count, 2)

    def test_cache_does_not_cross_registry_instances_or_copies(self):
        resources = [resource({"const": "one"}), resource({"const": "two"})]
        registries = [
            VersionedRegistry(
                lambda uri, version, index=index: resources[index],
            )
            for index in range(2)
        ]

        first = registries[0].get_or_retrieve("urn:shared").value
        second = registries[1].get_or_retrieve("urn:shared").value
        copied = copy.copy(registries[0]).get_or_retrieve("urn:shared").value

        self.assertIs(first, resources[0])
        self.assertIs(second, resources[1])
        self.assertIsNot(copied, first)
        self.assertEqual(registries[0].load_count, 1)
        self.assertEqual(registries[1].load_count, 1)
        self.assertNotEqual(
            registries[0].loads[0].registry_id,
            registries[1].loads[0].registry_id,
        )

    def test_concurrent_validation_loads_each_uri_once_per_snapshot(self):
        entered = Event()
        release = Event()
        calls = []

        def retrieve(uri, version):
            calls.append((uri, version))
            entered.set()
            release.wait(1)
            return resource({"type": "integer"})

        registries = VersionedRegistry(retrieve)
        snapshot = registries.snapshot(0)
        validator = Draft202012Validator(
            {"$ref": "urn:concurrent"},
            _resolver=snapshot.registry.resolver(),
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(validator.is_valid, 12) for _ in range(2)]
            entered.wait(1)
            release.set()
            results = [future.result() for future in futures]

        self.assertEqual(results, [True, True])
        self.assertEqual(calls, [("urn:concurrent", 0)])
        self.assertEqual(registries.load_count, 1)

    def test_expected_version_conflict_is_identifiable(self):
        registries = VersionedRegistry()
        registries.put("urn:x", resource({"type": "integer"}))

        with self.assertRaises(registry.ConcurrentRegistryUpdate) as caught:
            registries.put(
                "urn:x",
                resource({"type": "string"}),
                expected_version=0,
            )

        self.assertEqual(caught.exception.expected_version, 0)
        self.assertEqual(caught.exception.current_version, 1)

    def test_concurrent_version_checked_update_is_identifiable(self):
        registries = VersionedRegistry(
            resources=[("urn:same", resource({"type": "integer"}))]
        )
        release = Event()
        errors = []

        def replace_resource(index):
            release.wait(1)
            try:
                registries.put(
                    "urn:same",
                    resource({"type": f"string-{index}"}),
                    expected_version=0,
                )
            except registry.ConcurrentRegistryUpdate as error:
                errors.append(error)

        threads = [
            Thread(target=replace_resource, args=(index,))
            for index in range(2)
        ]
        for thread in threads:
            thread.start()
        release.set()
        for thread in threads:
            thread.join()

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].expected_version, 0)
        self.assertEqual(errors[0].current_version, 1)

    def test_loader_errors_keep_uri_and_version(self):
        def retrieve(uri, version):
            raise RuntimeError("boom")

        registries = VersionedRegistry(retrieve)
        validator = Draft202012Validator(
            {"$ref": "urn:broken"},
            registry=registries,
        )

        with self.assertRaises(registry.ResourceLoadError) as caught:
            validator.validate(1)

        self.assertEqual(caught.exception.ref, "urn:broken")
        self.assertEqual(caught.exception.version, 0)
        self.assertIsInstance(caught.exception.original_error, RuntimeError)

    def test_snapshot_lookup_surfaces_loader_error(self):
        def retrieve(uri):
            raise RuntimeError("boom")

        registries = VersionedRegistry(retrieve)

        with self.assertRaises(registry.ResourceLoadError) as caught:
            registries.snapshot().lookup("urn:broken")

        self.assertEqual(caught.exception.ref, "urn:broken")

    def test_recursive_loader_raises_circular_reference(self):
        registries = VersionedRegistry()

        def retrieve(uri, version):
            if uri == "urn:a":
                registries.snapshot(version).registry.get_or_retrieve("urn:b")
            else:
                registries.snapshot(version).registry.get_or_retrieve("urn:a")
            return resource({"type": "integer"})

        registries._loader = retrieve

        with self.assertRaises(registry.CircularReferenceError) as caught:
            registries.get_or_retrieve("urn:a")

        self.assertEqual(caught.exception.ref, "urn:a")
        self.assertEqual(caught.exception.path, ("urn:a", "urn:b"))

    def test_direct_reference_cycle_is_identifiable(self):
        registries = VersionedRegistry(
            resources=[
                ("urn:a", resource({"$ref": "urn:b"})),
                ("urn:b", resource({"$ref": "urn:a"})),
            ]
        )
        validator = Draft202012Validator(
            {"$ref": "urn:a"},
            registry=registries,
        )

        with self.assertRaises(registry.ReferenceCycleError) as caught:
            validator.validate(1)

        self.assertEqual(caught.exception.path, ("urn:a", "urn:b"))

    def test_rfc_uri_resolution_and_dynamic_ref(self):
        root = resource(
            {
                "$id": "https://example.com/base/",
                "allOf": [
                    {"$ref": "child%20doc#"},
                    {"$ref": "file:///other#/$defs/item"},
                    {"$dynamicRef": "urn:dynamic#anchor"},
                ],
            }
        )
        child = resource(
            {
                "$id": "https://example.com/base/child%20doc",
                "type": "integer",
            }
        )
        file_resource = resource(
            {
                "$id": "file:///other",
                "$defs": {"item": {"type": "integer"}},
            }
        )
        dynamic = resource(
            {
                "$id": "urn:dynamic",
                "$dynamicAnchor": "anchor",
                "type": "integer",
            }
        )
        registries = VersionedRegistry(
            resources=[
                ("https://example.com/base/child%20doc", child),
                ("file:///other", file_resource),
                ("urn:dynamic", dynamic),
            ]
        )
        snapshot = registries.snapshot(base_uri="https://example.com/base/")

        self.assertIs(
            snapshot.lookup("child%20doc#").contents,
            child.contents,
        )
        self.assertIs(
            snapshot.lookup("file:///other#/$defs/item").contents,
            file_resource.contents["$defs"]["item"],
        )
        self.assertIs(
            snapshot.lookup("//example.com/base/child%20doc#").contents,
            child.contents,
        )
        self.assertTrue(
            Draft202012Validator(
                root.contents,
                registry=registries.pin(0),
            ).is_valid(1),
        )

    def test_pickle_preserves_published_resource_but_not_cache(self):
        published = resource({"type": "integer"})
        registries = VersionedRegistry(
            lambda uri, version: resource({"type": "string"}),
            resources=[("urn:published", published)],
        )
        registries.get_or_retrieve("urn:lazy")

        restored = pickle.loads(pickle.dumps(registries))

        self.assertEqual(
            restored["urn:published"].contents,
            published.contents,
        )
        self.assertEqual(restored.load_count, 0)
        self.assertNotIn("urn:lazy", restored)

    def test_deepcopy_independently_duplicates_published_resource(self):
        registries = VersionedRegistry(
            resources=[
                ("urn:published", resource({"type": "integer"})),
            ]
        )

        duplicated = copy.deepcopy(registries)

        self.assertIsNot(
            duplicated["urn:published"],
            registries["urn:published"],
        )
        self.assertEqual(duplicated.version, registries.version)
