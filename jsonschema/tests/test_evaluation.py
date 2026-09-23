"""
Tests for `jsonschema.evaluation`, the explicit evaluation entry point.
"""
from unittest import TestCase
import json

from jsonschema.evaluation import (
    Draft202012Evaluator,
    KeywordResult,
    evaluate,
)


def keywords(node):
    return [child.keyword for child in node.children]


def child(node, keyword):
    [child] = [each for each in node.children if each.keyword == keyword]
    return child


class TestAnyOfUnevaluatedProperties(TestCase):
    """
    The acceptance schema: two branches each declaring their own
    properties, with ``unevaluatedProperties: false`` on the outside.
    """

    schema = {
        "type": "object",
        "anyOf": [
            {
                "properties": {"a": {"type": "string"}},
                "required": ["a"],
            },
            {
                "properties": {"b": {"type": "integer"}},
                "required": ["b"],
            },
        ],
        "unevaluatedProperties": False,
    }

    def test_single_matching_branch_evaluates_its_properties(self):
        result = evaluate({"a": "x"}, self.schema)
        self.assertTrue(result.valid)
        self.assertEqual(
            result.annotations["evaluatedProperties"], ["a"],
        )

    def test_matching_branches_merge_evaluated_properties(self):
        result = evaluate({"a": "x", "b": 1}, self.schema)
        self.assertTrue(result.valid)
        any_of = child(result, "anyOf")
        self.assertTrue(all(branch.valid for branch in any_of.children))
        self.assertEqual(
            result.annotations["evaluatedProperties"], ["a", "b"],
        )

    def test_unevaluated_property_is_rejected_and_explained(self):
        result = evaluate({"a": "x", "c": 1}, self.schema)
        self.assertFalse(result.valid)
        unevaluated = child(result, "unevaluatedProperties")
        self.assertFalse(unevaluated.valid)
        [error] = unevaluated.errors
        self.assertIn("'c'", error)
        self.assertEqual(unevaluated.instance_location, "")
        self.assertEqual(
            unevaluated.schema_location, "/unevaluatedProperties",
        )

    def test_no_matching_branch_is_rejected(self):
        result = evaluate({"c": 1}, self.schema)
        self.assertFalse(result.valid)
        any_of = child(result, "anyOf")
        self.assertFalse(any_of.valid)
        self.assertIn(
            "is not valid under any of the given schemas",
            any_of.errors[0],
        )

    def test_failed_branch_annotations_do_not_leak(self):
        schema = {
            "anyOf": [
                {
                    "properties": {"secret": {"type": "string"}},
                    "required": ["missing"],
                },
                {"properties": {"b": {"type": "integer"}}},
            ],
            "unevaluatedProperties": False,
        }
        result = evaluate({"secret": "x", "b": 1}, schema)
        self.assertFalse(result.valid)
        unevaluated = child(result, "unevaluatedProperties")
        self.assertFalse(unevaluated.valid)
        self.assertIn("'secret'", unevaluated.errors[0])


class TestOneOf(TestCase):
    schema = {
        "oneOf": [
            {"properties": {"a": {"const": 1}}, "required": ["a"]},
            {"properties": {"b": {"const": 2}}, "required": ["b"]},
        ],
        "unevaluatedProperties": False,
    }

    def test_exactly_one_matching_branch_is_valid(self):
        result = evaluate({"a": 1}, self.schema)
        self.assertTrue(result.valid)
        self.assertEqual(
            result.annotations["evaluatedProperties"], ["a"],
        )

    def test_two_matching_branches_still_fail(self):
        result = evaluate({"a": 1, "b": 2}, self.schema)
        self.assertFalse(result.valid)
        one_of = child(result, "oneOf")
        self.assertFalse(one_of.valid)
        self.assertEqual(
            [branch.valid for branch in one_of.children], [True, True],
        )
        self.assertIn("expected exactly one", one_of.errors[0])


class TestIfThenElse(TestCase):
    schema = {
        "if": {
            "properties": {"kind": {"const": "a"}},
            "required": ["kind"],
        },
        "then": {"properties": {"a": {"type": "string"}}},
        "else": {"properties": {"b": {"type": "integer"}}},
        "unevaluatedProperties": False,
    }

    def test_then_branch_evaluates_its_properties(self):
        result = evaluate({"kind": "a", "a": "x"}, self.schema)
        self.assertTrue(result.valid)
        self.assertIn("if", keywords(result))
        self.assertIn("then", keywords(result))
        self.assertNotIn("else", keywords(result))
        self.assertEqual(
            sorted(result.annotations["evaluatedProperties"]),
            ["a", "kind"],
        )

    def test_else_branch_rejects_unevaluated_property(self):
        result = evaluate({"kind": "a", "b": 1}, self.schema)
        self.assertFalse(result.valid)
        unevaluated = child(result, "unevaluatedProperties")
        self.assertIn("'b'", unevaluated.errors[0])

    def test_else_branch_is_taken_when_if_fails(self):
        schema = dict(self.schema, unevaluatedProperties=True)
        result = evaluate({"kind": "b", "b": 1}, schema)
        self.assertTrue(result.valid)
        self.assertIn("else", keywords(result))
        self.assertNotIn("then", keywords(result))


class TestUnevaluatedItems(TestCase):
    def test_prefix_items_are_evaluated(self):
        schema = {
            "prefixItems": [{"type": "string"}],
            "unevaluatedItems": False,
        }
        result = evaluate(["a"], schema)
        self.assertTrue(result.valid)
        self.assertEqual(result.annotations["evaluatedItems"], [0])

    def test_unevaluated_item_is_rejected(self):
        schema = {
            "prefixItems": [{"type": "string"}],
            "unevaluatedItems": False,
        }
        result = evaluate(["a", "b"], schema)
        self.assertFalse(result.valid)
        unevaluated = child(result, "unevaluatedItems")
        self.assertFalse(unevaluated.valid)
        self.assertIn("'b'", unevaluated.errors[0])

    def test_items_evaluated_via_matching_any_of_branch(self):
        schema = {
            "prefixItems": [{"type": "string"}],
            "anyOf": [
                {"prefixItems": [True], "items": {"type": "integer"}},
                {"prefixItems": [True], "items": {"type": "boolean"}},
            ],
            "unevaluatedItems": False,
        }
        result = evaluate(["a", 1, 2], schema)
        self.assertTrue(result.valid)
        self.assertEqual(result.annotations["evaluatedItems"], [0, 1, 2])


class TestReferences(TestCase):
    schema = {
        "$id": "https://example.com/root",
        "$defs": {"thing": {"type": "string"}},
        "properties": {
            "x": {"$ref": "#/$defs/thing"},
            "y": {"$ref": "#/$defs/thing"},
        },
    }

    def reference_nodes(self, result):
        properties = child(result, "properties")
        return [
            child(schema_node, "$ref")
            for schema_node in properties.children
        ]

    def test_call_sites_to_the_same_def_are_not_merged(self):
        result = evaluate({"x": "a", "y": "b"}, self.schema)
        self.assertTrue(result.valid)
        x_ref, y_ref = self.reference_nodes(result)

        self.assertEqual(x_ref.schema_location, "/properties/x/$ref")
        self.assertEqual(y_ref.schema_location, "/properties/y/$ref")
        self.assertEqual(x_ref.instance_location, "/x")
        self.assertEqual(y_ref.instance_location, "/y")
        self.assertIsNot(x_ref, y_ref)

        (x_target,), (y_target,) = x_ref.children, y_ref.children
        self.assertEqual(
            x_target.absolute_schema_location,
            "https://example.com/root#/$defs/thing",
        )
        self.assertEqual(
            y_target.absolute_schema_location,
            "https://example.com/root#/$defs/thing",
        )
        self.assertIsNot(x_target, y_target)

    def test_reference_failure_is_located_at_the_call_site(self):
        result = evaluate({"x": 1, "y": "b"}, self.schema)
        self.assertFalse(result.valid)
        x_ref, _ = self.reference_nodes(result)
        self.assertFalse(x_ref.valid)
        (target,) = x_ref.children
        self.assertEqual(target.instance_location, "/x")
        self.assertEqual(
            child(target, "type").errors, ["1 is not of type 'string'"],
        )

    def test_dynamic_ref_records_both_call_site_and_target(self):
        schema = {
            "$id": "https://example.com/main",
            "$ref": "base",
            "$defs": {
                "item": {"$dynamicAnchor": "item", "type": "string"},
                "base": {
                    "$id": "base",
                    "type": "object",
                    "properties": {"v": {"$dynamicRef": "#item"}},
                    "$defs": {
                        "item": {
                            "$dynamicAnchor": "item",
                            "type": "integer",
                        },
                    },
                },
            },
        }
        result = evaluate({"v": "hello"}, schema)
        self.assertTrue(result.valid)

        ref = child(result, "$ref")
        self.assertEqual(ref.schema_location, "/$ref")
        (base,) = ref.children
        self.assertEqual(
            base.absolute_schema_location, "https://example.com/base#",
        )

        properties = child(base, "properties")
        (v,) = properties.children
        dynamic_ref = child(v, "$dynamicRef")
        self.assertEqual(
            dynamic_ref.absolute_schema_location,
            "https://example.com/base#/properties/v/$dynamicRef",
        )
        (target,) = dynamic_ref.children
        self.assertEqual(
            target.absolute_schema_location,
            "https://example.com/main#/$defs/item",
        )

        invalid = evaluate({"v": 1}, schema)
        self.assertFalse(invalid.valid)


class TestCustomKeywords(TestCase):
    def test_custom_keyword_is_evaluated_once_and_joins_the_tree(self):
        calls = []

        def parity(context, value, instance, schema):
            calls.append(context.instance_location)
            if not isinstance(instance, int) or not value:
                return None
            if instance % 2:
                return KeywordResult(
                    valid=False,
                    errors=[f"{instance!r} is odd"],
                    annotations={"parity": "odd"},
                )
            return KeywordResult(annotations={"parity": "even"})

        evaluator = Draft202012Evaluator(
            {"type": "integer", "parity": True},
            extra_keywords={"parity": parity},
        )

        result = evaluator.evaluate(4)
        self.assertTrue(result.valid)
        self.assertEqual(calls, [""])
        parity_node = child(result, "parity")
        self.assertEqual(parity_node.annotations["parity"], "even")
        self.assertEqual(result.annotations["parity"], "even")

        json.dumps(result.to_dict())
        self.assertEqual(calls, [""])

        result = evaluator.evaluate(3)
        self.assertFalse(result.valid)
        self.assertEqual(calls, ["", ""])
        parity_node = child(result, "parity")
        self.assertFalse(parity_node.valid)
        self.assertEqual(parity_node.errors, ["3 is odd"])

    def test_custom_keyword_can_descend_and_contribute_children(self):
        def checked(context, subschema, instance, schema):
            if not isinstance(instance, dict):
                return None
            children = [
                context.descend(value, subschema, path=key, schema_path=key)
                for key, value in instance.items()
            ]
            return KeywordResult(
                valid=all(each.valid for each in children),
                children=children,
                annotations={
                    "evaluatedProperties": [
                        key
                        for (key, each) in zip(instance, children)
                        if each.valid
                    ],
                },
            )

        evaluator = Draft202012Evaluator(
            {
                "checked": {"type": "integer"},
                "unevaluatedProperties": False,
            },
            extra_keywords={"checked": checked},
        )

        result = evaluator.evaluate({"a": 1})
        self.assertTrue(result.valid)
        self.assertEqual(
            result.annotations["evaluatedProperties"], ["a"],
        )
        checked_node = child(result, "checked")
        (grandchild,) = checked_node.children
        self.assertEqual(grandchild.instance_location, "/a")

        result = evaluator.evaluate({"a": "no"})
        self.assertFalse(result.valid)
        self.assertFalse(child(result, "checked").valid)


class TestResultTree(TestCase):
    def test_tree_is_json_serializable(self):
        schema = {
            "$id": "https://example.com/root",
            "properties": {"a/b": {"type": "integer"}},
            "required": ["a/b"],
        }
        result = evaluate({"a/b": "no"}, schema)
        self.assertFalse(result.valid)
        serialized = json.dumps(result.to_dict())
        self.assertIn("/a~1b", serialized)

    def test_instance_locations_are_json_pointers(self):
        schema = {"properties": {"a/b": {"type": "integer"}}}
        result = evaluate({"a/b": 1}, schema)
        properties = child(result, "properties")
        (grandchild,) = properties.children
        self.assertEqual(grandchild.instance_location, "/a~1b")
        self.assertEqual(
            grandchild.schema_location, "/properties/a~1b",
        )

    def test_boolean_schemas(self):
        self.assertTrue(evaluate(1, True).valid)
        result = evaluate(1, False)
        self.assertFalse(result.valid)
        self.assertEqual(
            result.errors, ["False schema does not allow 1"],
        )

    def test_evaluate_convenience_matches_evaluator(self):
        schema = {"type": "string"}
        self.assertEqual(
            evaluate("x", schema).valid,
            Draft202012Evaluator(schema).evaluate("x").valid,
        )
