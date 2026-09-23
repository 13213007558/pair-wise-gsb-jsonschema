"""
Tests for the opt-in `content*` keyword validation in `jsonschema.content`.
"""
from unittest import TestCase
import base64
import json

import referencing.jsonschema
from referencing import Registry, Resource

from jsonschema import Draft202012Validator, ValidationError
from jsonschema.content import (
    ContentBudgetExceededError,
    ContentConfig,
    ContentDecodingError,
    ContentDepthExceededError,
    ContentError,
    ContentSchemaError,
    ContentTooLargeError,
    DuplicateKeyError,
    InvalidBase64DataError,
    InvalidBase64PaddingError,
    InvalidJSONError,
    InvalidUTF8Error,
    extend_with_content_validation,
)

Validator = extend_with_content_validation(Draft202012Validator)


def b64(document):
    if not isinstance(document, (bytes, str)):
        document = json.dumps(document)
    if isinstance(document, str):
        document = document.encode()
    return base64.b64encode(document).decode()


def content_schema(contentSchema, **kwargs):
    return {
        "contentMediaType": "application/json",
        "contentSchema": contentSchema,
        **kwargs,
    }


def the_only_error(validator, schema, instance):
    errors = list(validator.iter_errors(instance))
    assert len(errors) == 1, errors
    return errors[0]


class TestOptIn(TestCase):
    def test_default_validator_treats_content_keywords_as_annotations(self):
        schema = {
            "type": "string",
            "contentEncoding": "base64",
            "contentMediaType": "application/json",
            "contentSchema": {"type": "integer"},
        }
        # Garbage content, no decoding attempted, contentSchema ignored.
        self.assertTrue(Draft202012Validator(schema).is_valid("not base64!"))

    def test_valid_base64_json_passes(self):
        schema = content_schema({"type": "object"}, contentEncoding="base64")
        self.assertTrue(Validator(schema).is_valid(b64({"ok": True})))

    def test_success_does_not_mutate_the_instance(self):
        schema = {
            "properties": {
                "payload": content_schema(
                    {"type": "object"}, contentEncoding="base64",
                ),
            },
        }
        instance = {"payload": b64({"a": [1, 2, 3]})}
        Validator(schema).validate(instance)
        self.assertEqual(instance, {"payload": b64({"a": [1, 2, 3]})})
        self.assertIsInstance(instance["payload"], str)

    def test_plain_json_text_without_encoding(self):
        schema = content_schema({"type": "integer"})
        self.assertTrue(Validator(schema).is_valid("12"))
        self.assertFalse(Validator(schema).is_valid("\"nope\""))

    def test_non_strings_are_not_content_validated(self):
        schema = content_schema({"type": "integer"}, contentEncoding="base64")
        self.assertTrue(Validator(schema).is_valid(12))

    def test_unsupported_encoding_stays_an_annotation(self):
        schema = content_schema(
            {"type": "integer"}, contentEncoding="quoted-printable",
        )
        self.assertTrue(Validator(schema).is_valid("=4A=4B"))

    def test_unsupported_media_type_stays_an_annotation(self):
        schema = {
            "contentEncoding": "base64",
            "contentMediaType": "application/octet-stream",
            "contentSchema": {"type": "integer"},
        }
        self.assertTrue(Validator(schema).is_valid(b64({"not": "an int"})))

    def test_missing_media_type_stays_an_annotation(self):
        schema = {"contentEncoding": "base64", "contentSchema": {"type": "int"}}
        self.assertTrue(Validator(schema).is_valid(b64(12)))

    def test_media_type_with_utf8_charset_parameter(self):
        schema = {
            "contentMediaType": "application/json; charset=utf-8",
            "contentSchema": {"type": "integer"},
        }
        self.assertFalse(Validator(schema).is_valid("\"nope\""))

    def test_media_type_with_non_utf8_charset_stays_an_annotation(self):
        schema = {
            "contentMediaType": "application/json; charset=utf-16",
            "contentSchema": {"type": "integer"},
        }
        self.assertTrue(Validator(schema).is_valid("\"nope\""))


class TestDecodingErrors(TestCase):
    def validate(self, instance, **kwargs):
        schema = content_schema(True, contentEncoding="base64", **kwargs)
        return the_only_error(Validator(schema), schema, instance)

    def test_invalid_padding(self):
        error = self.validate(b64({}).rstrip("="))
        self.assertIsInstance(error, InvalidBase64PaddingError)
        self.assertIsInstance(error, ContentDecodingError)
        self.assertIsInstance(error, ValidationError)

    def test_misplaced_padding(self):
        error = self.validate("ab=cd1234=")
        self.assertIsInstance(error, InvalidBase64PaddingError)

    def test_invalid_base64_alphabet(self):
        error = self.validate("not base64 at all!!!")
        self.assertIsInstance(error, InvalidBase64DataError)
        self.assertNotIsInstance(error, InvalidBase64PaddingError)

    def test_invalid_base64_non_ascii(self):
        error = self.validate("\u00e9t\u00e9")
        self.assertIsInstance(error, InvalidBase64DataError)

    def test_invalid_utf8(self):
        error = self.validate(base64.b64encode(b"\xff\xfe").decode())
        self.assertIsInstance(error, InvalidUTF8Error)
        self.assertNotIn("\ufffd", str(error))

    def test_invalid_json(self):
        error = self.validate(b64("{no"))
        self.assertIsInstance(error, InvalidJSONError)

    def test_duplicate_keys(self):
        error = self.validate(b64('{"a": 1, "b": 2, "a": 3}'))
        self.assertIsInstance(error, DuplicateKeyError)
        self.assertEqual(error.key, "a")

    def test_nested_duplicate_keys(self):
        error = self.validate(b64('{"outer": {"x": 1, "x": 2}}'))
        self.assertIsInstance(error, DuplicateKeyError)
        self.assertEqual(error.key, "x")

    def test_each_decoding_error_is_distinguishable(self):
        instances = [
            b64({}).rstrip("="),
            "%%%invalid%%%",
            base64.b64encode(b"\xff").decode(),
            b64('{"a": 1, "a": 2}'),
        ]
        kinds = {
            type(self.validate(instance)) for instance in instances
        }
        self.assertEqual(len(kinds), 4)

    def test_decoding_error_location(self):
        schema = {
            "properties": {
                "payload": content_schema(True, contentEncoding="base64"),
            },
        }
        error = the_only_error(
            Validator(schema), schema, {"payload": "nope!"},
        )
        self.assertIsInstance(error, ContentError)
        self.assertEqual(list(error.path), ["payload"])
        self.assertEqual(error.decode_path, [])
        self.assertEqual(error.decode_depth, 1)


class TestContentSchemaErrors(TestCase):
    def test_decoded_document_is_validated(self):
        schema = content_schema(
            {"type": "object", "required": ["id"]},
            contentEncoding="base64",
        )
        error = the_only_error(Validator(schema), schema, b64({"other": 1}))
        self.assertIsInstance(error, ContentSchemaError)
        self.assertEqual(error.validator, "required")
        self.assertIn("'id' is a required property", error.message)

    def test_inner_json_pointer(self):
        schema = {
            "properties": {
                "payload": content_schema(
                    {
                        "type": "object",
                        "properties": {
                            "items": {"items": {"type": "integer"}},
                        },
                    },
                    contentEncoding="base64",
                ),
            },
        }
        error = the_only_error(
            Validator(schema),
            schema,
            {"payload": b64({"items": [1, "two", 3]})},
        )
        self.assertEqual(list(error.path), ["payload"])
        self.assertEqual(list(error.content_path), ["items", 1])
        self.assertEqual(error.content_pointer, "/items/1")
        self.assertEqual(error.decode_depth, 1)
        self.assertEqual(list(error.schema_path), [
            "properties", "payload", "contentSchema",
            "properties", "items", "items", "type",
        ])

    def test_inner_error_is_preserved_as_cause(self):
        schema = content_schema({"type": "integer"}, contentEncoding="base64")
        error = the_only_error(Validator(schema), schema, b64("12.5"))
        self.assertIsInstance(error.__cause__, ValidationError)
        self.assertNotIsInstance(error.__cause__, ContentError)

    def test_false_content_schema(self):
        schema = content_schema(False, contentEncoding="base64")
        error = the_only_error(Validator(schema), schema, b64({}))
        self.assertIsInstance(error, ContentSchemaError)


class TestNestedContent(TestCase):
    def nested_schema(self, inner_content_schema):
        return {
            "properties": {
                "payload": content_schema(
                    {
                        "type": "object",
                        "properties": {
                            "inner": content_schema(
                                inner_content_schema,
                                contentEncoding="base64",
                            ),
                        },
                    },
                    contentEncoding="base64",
                ),
            },
        }

    def test_nested_content_is_validated(self):
        schema = self.nested_schema({"type": "object", "required": ["x"]})
        instance = {"payload": b64({"inner": b64({"y": 1})})}
        error = the_only_error(Validator(schema), schema, instance)
        self.assertIsInstance(error, ContentSchemaError)
        # Outer field position + decode layer + inner JSON Pointer.
        self.assertEqual(list(error.path), ["payload"])
        self.assertEqual(error.decode_path, [("inner",)])
        self.assertEqual(error.decode_pointers, ["/inner"])
        self.assertEqual(list(error.content_path), [])
        self.assertEqual(error.decode_depth, 2)

    def test_nested_decoding_error_carries_layers(self):
        schema = self.nested_schema(True)
        instance = {"payload": b64({"inner": "not base64!"})}
        error = the_only_error(Validator(schema), schema, instance)
        self.assertIsInstance(error, InvalidBase64DataError)
        self.assertEqual(list(error.path), ["payload"])
        self.assertEqual(error.decode_path, [("inner",)])
        self.assertEqual(error.decode_depth, 2)

    def test_decode_depth_is_limited(self):
        config = ContentConfig(max_depth=1)
        ShallowValidator = extend_with_content_validation(
            Draft202012Validator, config=config,
        )
        schema = self.nested_schema(True)
        instance = {"payload": b64({"inner": b64({})})}
        error = the_only_error(ShallowValidator(schema), schema, instance)
        self.assertIsInstance(error, ContentDepthExceededError)
        self.assertEqual(list(error.path), ["payload"])
        self.assertEqual(error.decode_path, [("inner",)])

    def test_total_decoded_byte_budget_is_shared(self):
        config = ContentConfig(max_total_decoded_bytes=10)
        StingyValidator = extend_with_content_validation(
            Draft202012Validator, config=config,
        )
        schema = {
            "properties": {
                "one": content_schema(True, contentEncoding="base64"),
                "two": content_schema(True, contentEncoding="base64"),
            },
        }
        instance = {"one": b64({"a": 1}), "two": b64({"b": 2})}
        errors = list(StingyValidator(schema).iter_errors(instance))
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ContentBudgetExceededError)

    def test_nested_content_shares_the_byte_budget(self):
        # The outer document decodes to 26 bytes and the inner one to 8,
        # so a budget of 30 fits the former but not both.
        config = ContentConfig(max_total_decoded_bytes=30)
        StingyValidator = extend_with_content_validation(
            Draft202012Validator, config=config,
        )
        schema = self.nested_schema(True)
        instance = {"payload": b64({"inner": b64({"x": 1})})}
        error = the_only_error(StingyValidator(schema), schema, instance)
        self.assertIsInstance(error, ContentBudgetExceededError)
        self.assertEqual(error.decode_path, [("inner",)])

    def test_per_string_size_limit(self):
        config = ContentConfig(max_decoded_bytes=4)
        TinyValidator = extend_with_content_validation(
            Draft202012Validator, config=config,
        )
        schema = content_schema(True, contentEncoding="base64")
        error = the_only_error(TinyValidator(schema), schema, b64({"a": 1}))
        self.assertIsInstance(error, ContentTooLargeError)

    def test_budget_is_reset_between_validation_runs(self):
        config = ContentConfig(max_total_decoded_bytes=10)
        StingyValidator = extend_with_content_validation(
            Draft202012Validator, config=config,
        )
        schema = content_schema(True, contentEncoding="base64")
        validator = StingyValidator(schema)
        self.assertTrue(validator.is_valid(b64({"a": 1})))
        self.assertTrue(validator.is_valid(b64({"a": 1})))


class TestReferences(TestCase):
    def test_inner_refs_use_the_schema_resource_base(self):
        schema = {
            "$id": "https://example.com/root.json",
            "$defs": {"positive": {"type": "integer", "minimum": 1}},
            "properties": {
                "payload": content_schema(
                    {"$ref": "#/$defs/positive"},
                    contentEncoding="base64",
                ),
            },
        }
        validator = Validator(schema)
        self.assertTrue(validator.is_valid({"payload": b64(12)}))
        error = the_only_error(validator, schema, {"payload": b64(-3)})
        self.assertIsInstance(error, ContentSchemaError)
        self.assertEqual(error.validator, "minimum")

    def test_relative_refs_are_not_resolved_against_the_content(self):
        registry = Registry().with_resource(
            "https://example.com/other.json",
            Resource.from_contents(
                {"type": "integer"},
                default_specification=referencing.jsonschema.DRAFT202012,
            ),
        )
        schema = {
            "$id": "https://example.com/root.json",
            "properties": {
                "payload": content_schema(
                    {"$ref": "other.json"},
                    contentEncoding="base64",
                ),
            },
        }
        validator = Validator(schema, registry=registry)
        # The content string is not a URI base; the relative reference
        # resolves against the enclosing schema resource.
        self.assertTrue(validator.is_valid({"payload": b64(12)}))
        self.assertFalse(validator.is_valid({"payload": b64("12.5")}))


class TestCoexistingErrors(TestCase):
    def test_ordinary_and_content_errors_are_distinguishable(self):
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 3},
                "payload": content_schema(
                    {"type": "object", "required": ["id"]},
                    contentEncoding="base64",
                ),
            },
        }
        instance = {"name": "x", "payload": b64({"other": 1})}
        errors = list(Validator(schema).iter_errors(instance))
        self.assertEqual(len(errors), 2)

        ordinary = [
            error for error in errors
            if not isinstance(error, ContentError)
        ]
        content = [
            error for error in errors
            if isinstance(error, ContentError)
        ]
        self.assertEqual(len(ordinary), 1)
        self.assertEqual(len(content), 1)

        [ordinary_error] = ordinary
        self.assertEqual(ordinary_error.validator, "minLength")
        self.assertEqual(list(ordinary_error.path), ["name"])

        [content_error] = content
        self.assertIsInstance(content_error, ContentSchemaError)
        self.assertEqual(list(content_error.path), ["payload"])
        self.assertEqual(content_error.validator, "required")
        self.assertEqual(content_error.content_pointer, "")

    def test_validate_raises_content_errors(self):
        schema = content_schema({"type": "integer"}, contentEncoding="base64")
        with self.assertRaises(ContentSchemaError):
            Validator(schema).validate(b64("12.5"))

    def test_is_valid(self):
        schema = content_schema({"type": "integer"}, contentEncoding="base64")
        validator = Validator(schema)
        self.assertTrue(validator.is_valid(b64(12)))
        self.assertFalse(validator.is_valid(b64("12.5")))
        self.assertFalse(validator.is_valid("not base64!"))
