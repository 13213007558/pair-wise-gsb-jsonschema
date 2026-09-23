from collections import deque
from unittest import TestCase
import base64
import json

from jsonschema import Draft202012Validator, ValidationError
from jsonschema.content import (
    ContentValidationError,
    extend_with_content_validation,
)


def b64(raw):
    return base64.b64encode(raw).decode("ascii")


def b64_json(document):
    return b64(json.dumps(document).encode("utf-8"))


ContentValidator = extend_with_content_validation(Draft202012Validator)


def content_schema(content_schema, **extra):
    return {
        "type": "string",
        "contentEncoding": "base64",
        "contentMediaType": "application/json",
        "contentSchema": content_schema,
        **extra,
    }


class TestOptIn(TestCase):
    def test_default_validators_still_treat_content_keywords_as_annotations(
        self,
    ):
        schema = content_schema({"type": "object"})
        self.assertTrue(
            Draft202012Validator(schema).is_valid("not base64 at all!!!"),
        )

    def test_unsupported_encodings_remain_annotations(self):
        schema = {"contentEncoding": "quoted-printable"}
        self.assertTrue(ContentValidator(schema).is_valid("=C3=27"))

    def test_unsupported_media_types_remain_annotations(self):
        schema = {
            "contentEncoding": "base64",
            "contentMediaType": "application/x-not-json",
        }
        self.assertTrue(ContentValidator(schema).is_valid(b64(b"\x00\xff")))

    def test_non_string_instances_are_ignored(self):
        schema = {
            "contentEncoding": "base64",
            "contentMediaType": "application/json",
            "contentSchema": {"type": "object"},
        }
        self.assertTrue(ContentValidator(schema).is_valid(12))


class TestDecoding(TestCase):
    def errors(self, schema, instance, Validator=ContentValidator):
        return list(Validator(schema).iter_errors(instance))

    def test_valid_content_passes_and_instance_is_not_mutated(self):
        schema = {
            "type": "object",
            "properties": {
                "payload": content_schema(
                    {
                        "type": "object",
                        "required": ["id"],
                        "properties": {"id": {"type": "integer"}},
                    },
                ),
            },
        }
        payload = b64_json({"id": 3})
        instance = {"payload": payload}
        self.assertEqual(self.errors(schema, instance), [])
        self.assertEqual(instance, {"payload": payload})
        self.assertIsInstance(instance["payload"], str)

    def test_invalid_base64_padding(self):
        schema = content_schema({"type": "object"})
        (error,) = self.errors(schema, "abc")
        self.assertIsInstance(error, ContentValidationError)
        self.assertEqual(error.kind, ContentValidationError.INVALID_BASE64)
        self.assertEqual(error.content_layer, 0)
        self.assertEqual(error.content_pointers, ())

    def test_invalid_base64_characters(self):
        schema = content_schema({"type": "object"})
        (error,) = self.errors(schema, "!!!!")
        self.assertEqual(error.kind, ContentValidationError.INVALID_BASE64)

    def test_invalid_utf8(self):
        schema = content_schema({"type": "object"})
        (error,) = self.errors(schema, b64(b'{"a": "\xff"}'))
        self.assertEqual(error.kind, ContentValidationError.INVALID_UTF8)

    def test_invalid_json(self):
        schema = content_schema({"type": "object"})
        (error,) = self.errors(schema, b64(b"{oops"))
        self.assertEqual(error.kind, ContentValidationError.INVALID_JSON)

    def test_duplicate_keys_are_rejected(self):
        schema = content_schema({"type": "object"})
        (error,) = self.errors(schema, b64(b'{"a": 1, "a": 2}'))
        self.assertEqual(error.kind, ContentValidationError.DUPLICATE_KEY)
        self.assertIn("'a'", error.message)

    def test_decoded_byte_limit(self):
        Validator = extend_with_content_validation(
            Draft202012Validator,
            max_content_bytes=4,
        )
        schema = content_schema({"type": "object"})
        (error,) = self.errors(schema, b64_json({"a": 1}), Validator=Validator)
        self.assertEqual(error.kind, ContentValidationError.TOO_LARGE)

    def test_encoding_checked_even_without_content_schema(self):
        schema = {
            "contentEncoding": "base64",
            "contentMediaType": "application/json",
        }
        (error,) = self.errors(schema, "abc")
        self.assertEqual(error.kind, ContentValidationError.INVALID_BASE64)
        self.assertEqual(error.validator, "contentEncoding")

    def test_content_errors_are_validation_errors(self):
        schema = content_schema({"type": "object"})
        with self.assertRaises(ValidationError):
            ContentValidator(schema).validate("abc")


class TestContentSchema(TestCase):
    def test_inner_error_carries_outer_path_and_inner_pointer(self):
        schema = {
            "type": "object",
            "properties": {
                "payload": content_schema(
                    {
                        "type": "object",
                        "properties": {"id": {"type": "integer"}},
                    },
                ),
            },
        }
        instance = {"payload": b64_json({"id": "not-an-int"})}
        (error,) = ContentValidator(schema).iter_errors(instance)

        self.assertIsInstance(error, ContentValidationError)
        self.assertEqual(error.kind, ContentValidationError.SCHEMA)
        self.assertEqual(error.path, deque(["payload"]))
        self.assertEqual(error.validator, "type")
        self.assertEqual(error.content_layer, 1)
        self.assertEqual(error.content_layers, (("id",),))
        self.assertEqual(error.content_pointers, ("/id",))
        self.assertEqual(
            list(error.schema_path),
            [
                "properties",
                "payload",
                "contentSchema",
                "properties",
                "id",
                "type",
            ],
        )

    def test_string_must_pass_its_own_constraints_first(self):
        schema = content_schema({"type": "object"}, maxLength=3)
        errors = list(ContentValidator(schema).iter_errors("!!!!"))
        self.assertEqual(len(errors), 1)
        (error,) = errors
        self.assertNotIsInstance(error, ContentValidationError)
        self.assertEqual(error.validator, "maxLength")

    def test_relative_references_resolve_against_the_schema_resource(self):
        schema = {
            "$id": "https://example.com/root",
            "$defs": {"inner": {"type": "object", "required": ["id"]}},
            "type": "object",
            "properties": {
                "payload": content_schema({"$ref": "#/$defs/inner"}),
            },
        }
        instance = {"payload": b64_json({"name": "x"})}
        (error,) = ContentValidator(schema).iter_errors(instance)
        self.assertEqual(error.kind, ContentValidationError.SCHEMA)
        self.assertEqual(error.validator, "required")
        self.assertEqual(error.content_pointers, ("",))

    def test_ordinary_and_content_errors_coexist_and_are_distinguishable(self):
        schema = {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "minimum": 0},
                "payload": content_schema(
                    {
                        "type": "object",
                        "required": ["id"],
                    },
                ),
            },
        }
        instance = {"count": -1, "payload": b64_json({"name": "x"})}

        ordinary, content = [], []
        for error in ContentValidator(schema).iter_errors(instance):
            if isinstance(error, ContentValidationError):
                content.append(error)
            else:
                ordinary.append(error)

        self.assertEqual(len(ordinary), 1)
        self.assertEqual(ordinary[0].validator, "minimum")
        self.assertEqual(list(ordinary[0].path), ["count"])

        self.assertEqual(len(content), 1)
        self.assertEqual(content[0].kind, ContentValidationError.SCHEMA)
        self.assertEqual(list(content[0].path), ["payload"])
        self.assertEqual(content[0].content_pointers, ("",))


def nested_schema():
    inner = {
        "type": "object",
        "properties": {"token": {"type": "integer"}},
    }
    middle = {
        "type": "object",
        "properties": {
            "nested": content_schema(inner),
        },
    }
    return {
        "type": "object",
        "properties": {"payload": content_schema(middle)},
    }


def nested_instance(token):
    level2 = b64_json({"token": token})
    level1 = b64_json({"nested": level2})
    return {"payload": level1}


class TestNestedContent(TestCase):
    def test_deep_error_reports_each_decoding_layer(self):
        (error,) = ContentValidator(
            nested_schema(),
        ).iter_errors(nested_instance("not-an-int"))

        self.assertEqual(error.kind, ContentValidationError.SCHEMA)
        self.assertEqual(list(error.path), ["payload"])
        self.assertEqual(error.content_layer, 2)
        self.assertEqual(error.content_layers, (("nested",), ("token",)))
        self.assertEqual(error.content_pointers, ("/nested", "/token"))
        self.assertEqual(error.validator, "type")

    def test_valid_nested_content_passes(self):
        self.assertTrue(
            ContentValidator(nested_schema()).is_valid(nested_instance(3)),
        )

    def test_decode_failure_in_nested_content(self):
        level1 = b64_json({"nested": "not base64!"})
        (error,) = ContentValidator(
            nested_schema(),
        ).iter_errors({"payload": level1})
        self.assertEqual(error.kind, ContentValidationError.INVALID_BASE64)
        self.assertEqual(list(error.path), ["payload"])
        self.assertEqual(error.content_layer, 1)
        self.assertEqual(error.content_pointers, ("/nested",))

    def test_shared_decode_depth_limit(self):
        Validator = extend_with_content_validation(
            Draft202012Validator,
            max_decode_depth=1,
        )
        (error,) = Validator(
            nested_schema(),
        ).iter_errors(nested_instance(3))
        self.assertEqual(error.kind, ContentValidationError.DEPTH_EXCEEDED)
        self.assertEqual(list(error.path), ["payload"])
        self.assertEqual(error.content_pointers, ("/nested",))

    def test_shared_total_decoded_byte_budget(self):
        instance = nested_instance(3)
        outer_size = len(base64.b64decode(instance["payload"]))
        Validator = extend_with_content_validation(
            Draft202012Validator,
            max_total_decoded_bytes=outer_size,
        )
        (error,) = Validator(nested_schema()).iter_errors(instance)
        self.assertEqual(error.kind, ContentValidationError.BUDGET_EXCEEDED)
        self.assertEqual(error.content_pointers, ("/nested",))
