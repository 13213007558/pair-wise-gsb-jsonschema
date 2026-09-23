"""
Tests for jsonschema.bundling: offline collection and loading of resources.
"""
import json

import pytest
import referencing
import referencing.jsonschema

from jsonschema import Draft202012Validator, bundling
from jsonschema.bundling import (
    Bundle,
    BundleConflictError,
    BundleIntegrityError,
    IncompleteBundleError,
    collect,
)

ROOT = "https://a.example.com/root.json"
TREE = "https://a.example.com/tree.json"
ITEM = "https://b.example.com/item.json"
INNER = "https://b.example.com/inner.json"
STRICT = "https://c.example.com/strict-tree.json"
LEAF = "https://c.example.com/leaf.json"

DIALECT = "https://json-schema.org/draft/2020-12/schema"

DOCUMENTS = {
    ROOT: {
        "$schema": DIALECT,
        "$id": ROOT,
        "type": "object",
        "properties": {
            "tree": {"$ref": "tree.json"},
            "other": {"$ref": ITEM},
        },
        "additionalProperties": False,
    },
    TREE: {
        "$schema": DIALECT,
        "$id": TREE,
        "$dynamicAnchor": "node",
        "type": "object",
        "properties": {
            "value": {"type": "integer"},
            "children": {
                "type": "array",
                "items": {"$dynamicRef": "#node"},
            },
            "back": {"$ref": "root.json"},
        },
        "additionalProperties": False,
    },
    ITEM: {
        "$schema": DIALECT,
        "$id": ITEM,
        "$dynamicAnchor": "node",
        "$defs": {
            "inner": {
                "$id": "inner.json",
                "$anchor": "name",
                "type": "string",
            },
        },
        "type": "object",
        "properties": {
            "name": {"$ref": "inner.json#name"},
            "sub": {"$dynamicRef": "#node"},
        },
        "additionalProperties": False,
    },
}

EXTRA_DOCUMENTS = {
    STRICT: {
        "$schema": DIALECT,
        "$id": STRICT,
        "$dynamicAnchor": "node",
        "type": "object",
        "properties": {"leaf": {"$ref": "leaf.json"}},
    },
    LEAF: {
        "$schema": DIALECT,
        "$id": LEAF,
        "type": "object",
    },
}


def retrieve_from(documents):
    def retrieve(uri):
        try:
            contents = documents[uri]
        except KeyError:
            raise referencing.exceptions.NoSuchResource(ref=uri)
        return referencing.Resource.from_contents(
            contents,
            default_specification=referencing.jsonschema.DRAFT202012,
        )
    return retrieve


def collect_default(**kwargs):
    return collect(ROOT, retrieve_from(DOCUMENTS), **kwargs)


def live_validator():
    registry = referencing.Registry(retrieve=retrieve_from(DOCUMENTS))
    return Draft202012Validator(DOCUMENTS[ROOT], registry=registry)


INSTANCES = [
    {},
    {"tree": {"value": 1}},
    {"tree": {"value": 1, "children": [{"value": 2, "children": []}]}},
    {"tree": {"value": "not-an-integer"}},
    {"tree": {"children": [{"value": "not-an-integer"}]}},
    {"tree": {"back": {"tree": {"value": 3}}}},
    {"tree": {"back": {"tree": {"value": "not-an-integer"}}}},
    {"other": {"name": "ok"}},
    {"other": {"name": 12}},
    {"other": {"sub": {"name": "nested"}}},
    {"other": {"sub": {"name": 12}}},
    {"tree": {"value": 1}, "other": {"name": "ok"}},
    {"unknown": 1},
]


class TestCollect:
    def test_collects_dependencies_under_their_original_uris(self):
        bundle = collect_default()
        assert bundle.root == ROOT
        assert set(bundle.resources) == {ROOT, TREE, ITEM}
        assert bundle.resources[ROOT] == DOCUMENTS[ROOT]

    def test_nested_ids_satisfy_references_without_retrieval(self):
        asked = []

        def retrieve(uri):
            asked.append(uri)
            return retrieve_from(DOCUMENTS)(uri)

        bundle = collect(ROOT, retrieve)
        assert INNER not in bundle.resources
        assert INNER not in asked  # provided by ITEM's nested $id

    def test_dependency_edges_are_recorded(self):
        bundle = collect_default()
        assert bundle.edges == {
            ROOT: frozenset({TREE, ITEM}),
            TREE: frozenset({TREE, ROOT}),
            ITEM: frozenset({ITEM, INNER}),
        }

    def test_circular_references_are_collected_once(self):
        seen = []

        def retrieve(uri):
            seen.append(uri)
            return retrieve_from(DOCUMENTS)(uri)

        collect(ROOT, retrieve)
        assert sorted(seen) == sorted({ROOT, TREE, ITEM})

    def test_dynamic_frontier_reports_dynamic_anchor_uses(self):
        bundle = collect_default()
        assert bundle.dynamic_frontier == {"node": (TREE, ITEM)}

    def test_extra_resources_are_bundled_and_crawled(self):
        bundle = collect(
            ROOT,
            retrieve_from({**DOCUMENTS, **EXTRA_DOCUMENTS}),
            extra_resources={STRICT: EXTRA_DOCUMENTS[STRICT]},
        )
        assert set(bundle.resources) == {ROOT, TREE, ITEM, STRICT, LEAF}
        assert bundle.edges[STRICT] == frozenset({LEAF})

    def test_extra_resources_survive_a_round_trip(self):
        bundle = collect(
            ROOT,
            retrieve_from({**DOCUMENTS, **EXTRA_DOCUMENTS}),
            extra_resources={STRICT: EXTRA_DOCUMENTS[STRICT]},
        )
        assert Bundle.from_json(bundle.to_json()) == bundle

    def test_extra_resources_may_be_resources(self):
        extra = {
            STRICT: referencing.Resource.from_contents(
                EXTRA_DOCUMENTS[STRICT],
                default_specification=referencing.jsonschema.DRAFT202012,
            ),
            LEAF: EXTRA_DOCUMENTS[LEAF],
        }
        bundle = collect_default(extra_resources=extra)
        assert set(bundle.resources) == {ROOT, TREE, ITEM, STRICT, LEAF}

    def test_unretrievable_dependency_fails_collection(self):
        documents = {uri: doc for uri, doc in DOCUMENTS.items() if uri != TREE}
        with pytest.raises(IncompleteBundleError) as excinfo:
            collect(ROOT, retrieve_from(documents))
        assert excinfo.value.missing == {TREE}

    def test_conflicting_contents_for_one_uri_are_refused(self):
        conflicting = {INNER: {"$id": INNER, "type": "number"}}
        with pytest.raises(BundleConflictError) as excinfo:
            collect_default(extra_resources=conflicting)
        assert excinfo.value.uri == INNER

    def test_identical_contents_for_one_uri_are_deduplicated(self):
        duplicate = {TREE: DOCUMENTS[TREE]}
        bundle = collect_default(extra_resources=duplicate)
        assert set(bundle.resources) == {ROOT, TREE, ITEM}

    def test_root_with_fragment_is_rejected(self):
        with pytest.raises(ValueError):
            collect(ROOT + "#foo", retrieve_from(DOCUMENTS))


class TestOfflineValidation:
    @pytest.mark.parametrize("instance", INSTANCES)
    def test_matches_the_live_registry(self, instance):
        bundle = collect_default()
        offline = bundle.validator()
        live = live_validator()
        assert offline.is_valid(instance) == live.is_valid(instance)

    def test_errors_match_the_live_registry(self):
        bundle = collect_default()
        offline = bundle.validator()
        live = live_validator()
        for instance in INSTANCES:
            offline_errors = {
                (each.message, tuple(each.absolute_schema_path))
                for each in offline.iter_errors(instance)
            }
            live_errors = {
                (each.message, tuple(each.absolute_schema_path))
                for each in live.iter_errors(instance)
            }
            assert offline_errors == live_errors

    def test_dynamic_scope_is_preserved_offline(self):
        bundle = collect_default()
        offline = bundle.validator()
        # "sub" is validated against ITEM itself (its own dynamic anchor),
        # so an integer "name" is invalid through the inner anchor...
        assert not offline.is_valid({"other": {"sub": {"name": 12}}})
        # ...while tree children recurse through TREE's dynamic anchor.
        assert offline.is_valid(
            {"tree": {"children": [{"children": [{"value": 1}]}]}},
        )
        assert not offline.is_valid(
            {"tree": {"children": [{"children": [{"value": "no"}]}]}},
        )

    def test_registry_performs_no_retrieval(self):
        bundle = collect_default()
        registry = bundle.registry()
        validator = Draft202012Validator(
            {"$ref": "https://unknown.example.com/missing.json"},
            registry=registry,
        )
        with pytest.raises(referencing.exceptions.Unresolvable):
            validator.is_valid(1)


class TestSerialization:
    def test_round_trip(self):
        bundle = collect_default()
        loaded = Bundle.from_json(bundle.to_json())
        assert loaded == bundle

    def test_round_trip_preserves_validation(self):
        bundle = collect_default()
        loaded = Bundle.from_json(bundle.to_json())
        offline = loaded.validator()
        live = live_validator()
        for instance in INSTANCES:
            assert offline.is_valid(instance) == live.is_valid(instance)

    def test_serializes_digests_root_and_edges(self):
        document = json.loads(collect_default().to_json())
        assert document["root"] == ROOT
        assert set(document["resources"]) == {ROOT, TREE, ITEM}
        for entry in document["resources"].values():
            assert len(entry["sha256"]) == 64
        assert set(document["edges"][ROOT]) == {TREE, ITEM}

    def test_missing_resource_is_reported_as_incomplete(self):
        document = json.loads(collect_default().to_json())
        del document["resources"][TREE]
        with pytest.raises(IncompleteBundleError) as excinfo:
            Bundle.from_json(document)
        assert TREE in excinfo.value.missing

    def test_missing_root_is_reported_as_incomplete(self):
        document = json.loads(collect_default().to_json())
        del document["resources"][ROOT]
        with pytest.raises(IncompleteBundleError) as excinfo:
            Bundle.from_json(document)
        assert ROOT in excinfo.value.missing

    def test_tampered_contents_are_rejected(self):
        document = json.loads(collect_default().to_json())
        document["resources"][TREE]["contents"]["type"] = "string"
        with pytest.raises(BundleIntegrityError):
            Bundle.from_json(document)

    def test_tampered_edges_are_rejected(self):
        document = json.loads(collect_default().to_json())
        tree = document["resources"][TREE]
        contents = tree["contents"]
        contents["properties"]["value"] = {"$ref": ITEM}
        tree["sha256"] = bundling._digest(contents)
        with pytest.raises(BundleIntegrityError):
            Bundle.from_json(document)

    def test_unknown_version_is_rejected(self):
        document = json.loads(collect_default().to_json())
        document["version"] = 9000
        with pytest.raises(bundling.BundleError):
            Bundle.from_json(document)
