from unittest import TestCase
import json

from referencing import Registry
from referencing.exceptions import NoSuchResource
from referencing.jsonschema import DRAFT202012

from jsonschema import Draft202012Validator, SchemaBundle
from jsonschema.bundling import (
    BundleError,
    BundleIncompleteError,
    BundleTamperedError,
    ConflictingResourceError,
)

ROOT = "https://a.example.com/root.json"
NODE = "https://b.example.com/node.json"
ALWAYS = "https://a.example.com/always.json"
EXTRA = "https://b.example.com/extra.json"

DOCUMENTS = {
    ROOT: {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": ROOT,
        "type": "object",
        "properties": {
            "child": {"$ref": NODE},
            "local": {"$ref": "thing.json"},
            "named": {"$ref": "#named"},
            "anything": {"$ref": "always.json"},
        },
        "$defs": {
            "named": {"$anchor": "named", "type": "null"},
            "thing": {
                "$id": "thing.json",
                "$dynamicAnchor": "node",
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "next": {"$dynamicRef": "#node"},
                },
            },
        },
    },
    NODE: {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": NODE,
        "$dynamicAnchor": "node",
        "type": "object",
        "properties": {
            "name": {"type": "integer"},
            "next": {"$dynamicRef": "#node"},
            "back": {"$ref": ROOT},
            "nm": {"$ref": ROOT + "#named"},
        },
    },
    ALWAYS: True,
    EXTRA: {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": EXTRA,
        "$dynamicAnchor": "node",
        "type": "object",
        "properties": {"name": {"type": "number"}},
    },
}

INSTANCES = [
    {"named": None, "anything": [1, 2]},
    {"named": 1},
    {"local": {"name": "x", "next": {"name": "y"}}},
    {"local": {"name": "x", "next": {"name": 1}}},
    {"child": {"name": 1, "nm": None, "next": {"name": 2}}},
    {"child": {"name": "x"}},
    {"child": {"nm": 1}},
    {
        "child": {
            "name": 1,
            "back": {"local": {"name": "a", "next": {"name": 7}}},
        },
    },
    {
        "child": {
            "name": 1,
            "back": {"local": {"name": "a", "next": {"name": "x"}}},
        },
    },
    {
        "child": {
            "name": 1,
            "back": {"child": {"name": 2, "back": {"anything": "ok"}}},
        },
    },
]


def retrieve(uri):
    return DOCUMENTS[uri]


def retrieve_resource(uri):
    return DRAFT202012.create_resource(contents=DOCUMENTS[uri])


def errors_of(validator, instance):
    return sorted(
        (list(error.path), error.message)
        for error in validator.iter_errors(instance)
    )


class TestSchemaBundle(TestCase):
    def setUp(self):
        self.retrieved = []

        def recording_retrieve(uri):
            self.retrieved.append(uri)
            return retrieve(uri)

        self.bundle = SchemaBundle.collect(ROOT, recording_retrieve)

    def original_validator(self):
        registry = Registry(retrieve=retrieve_resource)
        return Draft202012Validator(DOCUMENTS[ROOT], registry=registry)

    def bundled_validator(self, bundle=None):
        bundle = self.bundle if bundle is None else bundle
        return Draft202012Validator(
            bundle.contents_of(bundle.root_uri),
            registry=bundle.registry(),
        )

    def test_collects_each_resource_exactly_once(self):
        self.assertEqual(self.bundle.resource_uris, {ROOT, NODE, ALWAYS})
        self.assertEqual(
            set(self.retrieved),
            {ROOT, NODE, ALWAYS},
            "circular references should be deduplicated, and the "
            "embedded thing.json resource should not be retrieved",
        )

    def test_resource_identities_are_preserved(self):
        registry = self.bundle.registry().crawl()
        self.assertEqual(
            registry.contents(NODE)["$dynamicAnchor"],
            "node",
        )
        self.assertEqual(
            registry.contents("https://a.example.com/thing.json")[
                "$dynamicAnchor"
            ],
            "node",
        )

    def test_dependency_edges_are_recorded(self):
        self.assertEqual(
            self.bundle.dependencies[ROOT],
            {
                "https://a.example.com/thing.json",
                NODE,
                ALWAYS,
                ROOT,
            },
        )
        self.assertEqual(self.bundle.dependencies[NODE], {ROOT, NODE})
        self.assertEqual(self.bundle.dependencies[ALWAYS], set())

    def test_dynamic_boundaries_are_reported(self):
        boundaries = self.bundle.dynamic_boundaries
        self.assertTrue(boundaries)
        self.assertTrue(
            all(each["keyword"] == "$dynamicRef" for each in boundaries),
        )
        self.assertEqual(
            {each["resource"] for each in boundaries},
            {ROOT, NODE},
        )

    def test_offline_validation_matches_original_registry(self):
        original = self.original_validator()
        bundled = self.bundled_validator()
        for instance in INSTANCES:
            with self.subTest(instance=instance):
                self.assertEqual(
                    errors_of(bundled, instance),
                    errors_of(original, instance),
                )

    def test_some_instances_are_invalid(self):
        bundled = self.bundled_validator()
        results = [bundled.is_valid(each) for each in INSTANCES]
        self.assertIn(True, results)
        self.assertIn(False, results)

    def test_json_roundtrip_preserves_validation(self):
        data = json.loads(json.dumps(self.bundle.to_json()))
        restored = SchemaBundle.from_json(data)
        self.assertEqual(restored.root_uri, ROOT)
        self.assertEqual(restored.resource_uris, self.bundle.resource_uris)
        original = self.original_validator()
        bundled = self.bundled_validator(restored)
        for instance in INSTANCES:
            with self.subTest(instance=instance):
                self.assertEqual(
                    errors_of(bundled, instance),
                    errors_of(original, instance),
                )

    def test_missing_resource_is_reported_as_incomplete(self):
        data = self.bundle.to_json()
        data["resources"] = [
            each for each in data["resources"] if each["uri"] != NODE
        ]
        with self.assertRaises(BundleIncompleteError) as caught:
            SchemaBundle.from_json(data)
        self.assertEqual(caught.exception.missing, {NODE})

    def test_missing_root_is_reported_as_incomplete(self):
        data = self.bundle.to_json()
        data["resources"] = [
            each for each in data["resources"] if each["uri"] != ROOT
        ]
        with self.assertRaises(BundleIncompleteError) as caught:
            SchemaBundle.from_json(data)
        self.assertIn(ROOT, caught.exception.missing)

    def test_tampered_contents_are_detected(self):
        data = self.bundle.to_json()
        for each in data["resources"]:
            if each["uri"] == NODE:
                each["contents"]["properties"]["name"] = {"type": "string"}
        with self.assertRaises(BundleTamperedError) as caught:
            SchemaBundle.from_json(data)
        self.assertEqual(caught.exception.uri, NODE)

    def test_unknown_version_is_rejected(self):
        data = self.bundle.to_json()
        data["version"] = 999
        with self.assertRaises(BundleError):
            SchemaBundle.from_json(data)

    def test_runtime_registry_never_retrieves(self):
        registry = self.bundle.registry()
        with self.assertRaises(NoSuchResource):
            registry.get_or_retrieve("https://elsewhere.example.com/x")

    def test_extra_uris_supplement_dynamic_scope_resources(self):
        bundle = SchemaBundle.collect(ROOT, retrieve, extra_uris=[EXTRA])
        self.assertEqual(
            bundle.resource_uris,
            {ROOT, NODE, ALWAYS, EXTRA},
        )
        registry = bundle.registry()
        self.assertEqual(registry.contents(EXTRA)["$dynamicAnchor"], "node")

    def test_unretrievable_reference_fails_loudly(self):
        documents = {
            ROOT: {
                "$id": ROOT,
                "properties": {"x": {"$ref": "https://gone.example.com/"}},
            },
        }

        def failing_retrieve(uri):
            raise KeyError(uri)

        with self.assertRaises(BundleError):
            SchemaBundle.collect(ROOT, documents.__getitem__)
        with self.assertRaises(BundleError):
            SchemaBundle.collect(ROOT, failing_retrieve)


class TestConflictingResources(TestCase):
    def test_conflicting_embedded_resources_are_rejected(self):
        one = "https://a.example.com/one.json"
        two = "https://a.example.com/two.json"
        documents = {
            one: {
                "$id": one,
                "$defs": {
                    "dup": {"$id": "dup.json", "type": "string"},
                },
                "properties": {"other": {"$ref": "two.json"}},
            },
            two: {
                "$id": two,
                "$defs": {
                    "dup": {"$id": "dup.json", "type": "integer"},
                },
            },
        }

        def local_retrieve(uri):
            return documents[uri]

        with self.assertRaises(ConflictingResourceError) as caught:
            SchemaBundle.collect(one, local_retrieve)
        self.assertEqual(
            caught.exception.uri,
            "https://a.example.com/dup.json",
        )

    def test_identical_contents_are_not_a_conflict(self):
        one = "https://a.example.com/one.json"
        two = "https://a.example.com/two.json"
        shared = {"$id": "dup.json", "type": "string"}
        documents = {
            one: {
                "$id": one,
                "$defs": {"dup": shared},
                "properties": {"other": {"$ref": "two.json"}},
            },
            two: {
                "$id": two,
                "$defs": {"dup": dict(shared)},
            },
        }

        def local_retrieve(uri):
            return documents[uri]

        bundle = SchemaBundle.collect(one, local_retrieve)
        self.assertEqual(bundle.resource_uris, {one, two})
