"""
Tests for the explicit evaluation entry point (Validator.evaluate).
"""
from unittest import TestCase
import json

from jsonschema import Draft202012Validator, ValidationError
from jsonschema.validators import extend
import jsonschema


def child(node, keyword):
    return next(each for each in node.children if each.keyword == keyword)


class TestEvaluate(TestCase):
    def test_result_tree_is_json_serializable(self):
        node = jsonschema.evaluate(
            {"a": 1}, {"properties": {"a": {"type": "integer"}}},
        )
        serialized = json.dumps(node.to_dict())
        self.assertTrue(json.loads(serialized)["valid"])

    def test_evaluate_method_on_validator(self):
        validator = Draft202012Validator({"type": "integer"})
        self.assertTrue(validator.evaluate(1).valid)
        node = validator.evaluate("nope")
        self.assertFalse(node.valid)
        self.assertEqual(
            child(node, "type").errors,
            ["'nope' is not of type 'integer'"],
        )

    def test_module_level_evaluate_checks_schema(self):
        with self.assertRaises(jsonschema.SchemaError):
            jsonschema.evaluate(1, {"type": "nope"})

    def test_instance_locations_are_json_pointers(self):
        node = jsonschema.evaluate(
            {"a/b": [12]},
            {"properties": {"a/b": {"items": {"type": "string"}}}},
        )
        self.assertFalse(node.valid)
        properties = child(node, "properties")
        (subschema,) = properties.children
        self.assertEqual(subschema.instance_location, "/a~1b")
        items = child(subschema, "items")
        (item,) = items.children
        self.assertEqual(item.instance_location, "/a~1b/0")
        self.assertEqual(
            item.absolute_schema_location,
            "#/properties/a~1b/items",
        )

    def test_annotations_are_kept_on_valid_nodes(self):
        schema = {
            "title": "a thing",
            "properties": {"a": {"type": "integer", "default": 3}},
        }
        node = jsonschema.evaluate({"a": 1}, schema)
        self.assertEqual(node.annotations, {"title": "a thing"})
        properties = child(node, "properties")
        (subschema,) = properties.children
        self.assertEqual(subschema.annotations, {"default": 3})

    def test_format_is_an_annotation_without_a_format_checker(self):
        node = jsonschema.evaluate(
            "not-an-email", {"format": "email"},
        )
        self.assertTrue(node.valid)
        self.assertEqual(
            child(node, "format").annotations, {"format": "email"},
        )

    def test_false_schema(self):
        node = jsonschema.evaluate(1, False)
        self.assertFalse(node.valid)
        self.assertEqual(node.errors, ["False schema does not allow 1"])


class TestAnyOfOneOfIf(TestCase):
    def test_successful_any_of_branches_merge(self):
        schema = {
            "anyOf": [
                {"properties": {"a": {"type": "string"}}, "required": ["a"]},
                {"properties": {"b": {"type": "integer"}}, "required": ["b"]},
            ],
            "unevaluatedProperties": False,
        }
        node = jsonschema.evaluate({"a": "x", "b": 1}, schema)
        self.assertTrue(node.valid)
        any_of = child(node, "anyOf")
        self.assertEqual(any_of.evaluated_properties, {"a", "b"})
        self.assertEqual(node.evaluated_properties, {"a", "b"})

    def test_failed_any_of_branch_annotations_do_not_leak(self):
        schema = {
            "anyOf": [
                {"properties": {"a": {"type": "string"}}, "required": ["a"]},
                {"properties": {"b": {"type": "integer"}}, "required": ["b"]},
            ],
            "unevaluatedProperties": False,
        }
        # "a" is only evaluated by the first branch, which fails (5 is
        # not a string), so it must remain unevaluated and be rejected.
        node = jsonschema.evaluate({"a": 5, "b": 1}, schema)
        self.assertFalse(node.valid)
        any_of = child(node, "anyOf")
        self.assertEqual(any_of.evaluated_properties, {"b"})
        unevaluated = child(node, "unevaluatedProperties")
        self.assertEqual(
            unevaluated.errors,
            ["Unevaluated properties are not allowed ('a' was unexpected)"],
        )

    def test_unevaluated_properties_rejects_extras(self):
        schema = {
            "anyOf": [
                {"properties": {"a": {"type": "string"}}, "required": ["a"]},
                {"properties": {"b": {"type": "integer"}}, "required": ["b"]},
            ],
            "unevaluatedProperties": False,
        }
        node = jsonschema.evaluate({"a": "x", "c": 1}, schema)
        self.assertFalse(node.valid)
        unevaluated = child(node, "unevaluatedProperties")
        self.assertEqual(
            unevaluated.errors,
            ["Unevaluated properties are not allowed ('c' was unexpected)"],
        )
        self.assertEqual(unevaluated.evaluated_properties, {"c"})

    def test_one_of_fails_when_two_branches_match(self):
        schema = {"oneOf": [{"type": "integer"}, {"minimum": 2}]}
        node = jsonschema.evaluate(5, schema)
        self.assertFalse(node.valid)
        one_of = child(node, "oneOf")
        self.assertEqual(
            one_of.errors,
            ["5 is valid under each of schemas 0, 1"],
        )

    def test_one_of_merges_the_single_valid_branch(self):
        schema = {
            "oneOf": [
                {"properties": {"a": {"const": 1}}, "required": ["a"]},
                {"properties": {"b": {"const": 2}}, "required": ["b"]},
            ],
            "unevaluatedProperties": False,
        }
        node = jsonschema.evaluate({"a": 1}, schema)
        self.assertTrue(node.valid)
        self.assertEqual(child(node, "oneOf").evaluated_properties, {"a"})

    def test_if_then_else(self):
        schema = {
            "if": {"type": "string"},
            "then": {"minLength": 3},
            "else": {"type": "integer"},
        }
        node = jsonschema.evaluate("ab", schema)
        self.assertFalse(node.valid)
        self.assertFalse(child(node, "then").valid)

        node = jsonschema.evaluate("abc", schema)
        self.assertTrue(node.valid)

        node = jsonschema.evaluate(2.5, schema)
        self.assertFalse(node.valid)
        self.assertFalse(child(node, "else").valid)

    def test_if_does_not_contribute_evaluated_properties(self):
        schema = {
            "if": {"properties": {"a": {"type": "string"}}},
            "then": {"required": ["a"]},
            "unevaluatedProperties": False,
        }
        # "a" is evaluated only within the if subschema, whose evaluated
        # properties are discarded, so it stays unevaluated.
        node = jsonschema.evaluate({"a": "x"}, schema)
        self.assertFalse(node.valid)
        unevaluated = child(node, "unevaluatedProperties")
        self.assertEqual(
            unevaluated.errors,
            ["Unevaluated properties are not allowed ('a' was unexpected)"],
        )


class TestUnevaluatedItems(TestCase):
    def test_unevaluated_items_rejects_extras(self):
        schema = {
            "prefixItems": [{"type": "string"}],
            "unevaluatedItems": False,
        }
        node = jsonschema.evaluate(["ok", 1], schema)
        self.assertFalse(node.valid)
        unevaluated = child(node, "unevaluatedItems")
        self.assertEqual(
            unevaluated.errors,
            ["Unevaluated items are not allowed (1 was unexpected)"],
        )
        self.assertEqual(unevaluated.evaluated_items, {1})

    def test_unevaluated_items_sees_prefix_items(self):
        schema = {
            "prefixItems": [{"type": "string"}, {"type": "integer"}],
            "unevaluatedItems": False,
        }
        node = jsonschema.evaluate(["ok", 1], schema)
        self.assertTrue(node.valid)
        self.assertEqual(node.evaluated_items, {0, 1})

    def test_unevaluated_items_after_items(self):
        schema = {
            "prefixItems": [{"type": "string"}],
            "items": {"type": "integer"},
            "unevaluatedItems": False,
        }
        self.assertTrue(jsonschema.evaluate(["ok", 1, 2], schema).valid)
        node = jsonschema.evaluate(["ok", 1, "no"], schema)
        self.assertFalse(node.valid)


class TestReferences(TestCase):
    def test_ref_records_call_site_and_resource_location(self):
        schema = {
            "$id": "https://example.com/root",
            "$defs": {"positive": {"type": "integer", "minimum": 1}},
            "properties": {
                "x": {"$ref": "#/$defs/positive"},
                "y": {"$ref": "#/$defs/positive"},
            },
        }
        node = jsonschema.evaluate({"x": 1, "y": 2}, schema)
        self.assertTrue(node.valid)
        properties = child(node, "properties")
        self.assertEqual(len(properties.children), 2)

        refs = [child(each, "$ref") for each in properties.children]
        # Each call site is recorded separately...
        self.assertEqual(
            {each.schema_location for each in refs},
            {"/properties/x/$ref", "/properties/y/$ref"},
        )
        # ...even though both point at the same schema resource.
        for ref in refs:
            (resolved,) = ref.children
            self.assertEqual(
                resolved.absolute_schema_location,
                "https://example.com/root#/$defs/positive",
            )
            self.assertEqual(
                child(resolved, "type").absolute_schema_location,
                "https://example.com/root#/$defs/positive/type",
            )

    def test_dynamic_ref_records_anchor_target(self):
        schema = {
            "$id": "https://example.com/dynamic",
            "$defs": {
                "node": {
                    "$dynamicAnchor": "node",
                    "type": "object",
                    "properties": {"next": {"$dynamicRef": "#node"}},
                },
            },
            "$ref": "#/$defs/node",
        }
        node = jsonschema.evaluate({"next": {}}, schema)
        self.assertTrue(node.valid)
        root_ref = child(node, "$ref")
        (resolved,) = root_ref.children
        self.assertEqual(
            resolved.absolute_schema_location,
            "https://example.com/dynamic#/$defs/node",
        )
        properties = child(resolved, "properties")
        (next_,) = properties.children
        dynamic_ref = child(next_, "$dynamicRef")
        self.assertEqual(
            dynamic_ref.schema_location,
            "/$ref/properties/next/$dynamicRef",
        )
        (target,) = dynamic_ref.children
        self.assertEqual(
            target.absolute_schema_location,
            "https://example.com/dynamic#/$defs/node",
        )


class TestCustomKeywords(TestCase):
    def test_custom_keyword_joins_tree_and_runs_once(self):
        calls = []

        def forbid_secret(validator, value, instance, schema):
            calls.append(instance)
            if value and isinstance(instance, dict) and "secret" in instance:
                yield ValidationError("secret is forbidden")

        Custom = extend(
            Draft202012Validator, {"forbidSecret": forbid_secret},
        )
        schema = {
            "type": "object",
            "forbidSecret": True,
            "properties": {
                "name": {"type": "string", "forbidSecret": True},
            },
        }

        node = jsonschema.evaluate({"name": "x"}, schema, cls=Custom)
        self.assertTrue(node.valid)
        # Once for the root schema, once for the name subschema --
        # and crucially not a second time to produce the report.
        self.assertEqual(calls, [{"name": "x"}, "x"])
        keywords = {each.keyword for each in node.children}
        self.assertIn("forbidSecret", keywords)

        node = jsonschema.evaluate(
            {"name": "x", "secret": 1}, schema, cls=Custom,
        )
        self.assertFalse(node.valid)
        forbid = child(node, "forbidSecret")
        self.assertEqual(forbid.errors, ["secret is forbidden"])
        self.assertEqual(
            calls,
            [{"name": "x"}, "x", {"name": "x", "secret": 1}, "x"],
        )

    def test_iter_errors_behavior_is_unchanged(self):
        schema = {
            "anyOf": [
                {"properties": {"a": {"type": "string"}}, "required": ["a"]},
                {"properties": {"b": {"type": "integer"}}, "required": ["b"]},
            ],
            "unevaluatedProperties": False,
        }
        validator = Draft202012Validator(schema)
        errors = list(validator.iter_errors({"a": 5, "c": 1}))
        self.assertEqual(len(errors), 2)
        self.assertEqual(
            {error.validator for error in errors},
            {"anyOf", "unevaluatedProperties"},
        )
