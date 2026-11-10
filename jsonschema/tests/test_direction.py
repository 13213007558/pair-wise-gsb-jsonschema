"""
Tests for directional (read/write) validation profiles.
"""

from copy import deepcopy
from unittest import TestCase

from jsonschema import Draft202012Validator, SchemaError
from jsonschema.validators import (
    directional_profile,
    directional_validator_for,
)

ACCOUNT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "id": {"type": "string", "readOnly": True},
        "username": {"type": "string"},
        "email": {"type": "string"},
        "password": {"type": "string", "writeOnly": True},
    },
    "required": ["id", "username", "password"],
    "additionalProperties": False,
}


class TestAccountExample(TestCase):
    """
    The same account schema validates both creation requests and
    read responses.
    """

    def test_create_account_request(self):
        validator = directional_validator_for(ACCOUNT_SCHEMA, "write")
        request = {"username": "janedoe", "password": "s3cret"}
        self.assertEqual(list(validator.iter_errors(request)), [])

    def test_create_account_request_cannot_contain_server_id(self):
        validator = directional_validator_for(ACCOUNT_SCHEMA, "write")
        request = {"id": "chosen-by-client", "username": "j", "password": "x"}
        [error] = validator.iter_errors(request)
        self.assertIn("read-only", error.message)
        self.assertIn("write", error.message)
        self.assertEqual(list(error.absolute_path), ["id"])

    def test_read_account_response(self):
        validator = directional_validator_for(ACCOUNT_SCHEMA, "read")
        response = {"id": "u-123", "username": "janedoe"}
        self.assertEqual(list(validator.iter_errors(response)), [])

    def test_read_account_response_cannot_leak_password(self):
        validator = directional_validator_for(ACCOUNT_SCHEMA, "read")
        response = {"id": "u-123", "username": "j", "password": "s3cret"}
        [error] = validator.iter_errors(response)
        self.assertIn("write-only", error.message)
        self.assertIn("read", error.message)
        self.assertEqual(list(error.absolute_path), ["password"])

    def test_normal_fields_are_preserved_in_both_directions(self):
        write = directional_validator_for(ACCOUNT_SCHEMA, "write")
        read = directional_validator_for(ACCOUNT_SCHEMA, "read")
        request = {"username": "j", "email": "j@example.com", "password": "x"}
        response = {"id": "u-1", "username": "j", "email": "j@example.com"}
        self.assertTrue(write.is_valid(request))
        self.assertTrue(read.is_valid(response))
        self.assertEqual(
            request,
            {"username": "j", "email": "j@example.com", "password": "x"},
        )
        self.assertEqual(
            response,
            {"id": "u-1", "username": "j", "email": "j@example.com"},
        )


class TestRequiredInteraction(TestCase):
    def test_readonly_property_is_not_required_from_clients(self):
        validator = directional_validator_for(ACCOUNT_SCHEMA, "write")
        self.assertTrue(
            validator.is_valid({"username": "j", "password": "x"}),
        )

    def test_writeonly_property_is_not_required_in_responses(self):
        validator = directional_validator_for(ACCOUNT_SCHEMA, "read")
        self.assertTrue(validator.is_valid({"id": "u-1", "username": "j"}))

    def test_normal_properties_are_still_required(self):
        write = directional_validator_for(ACCOUNT_SCHEMA, "write")
        [error] = write.iter_errors({"password": "x"})
        self.assertEqual(error.message, "'username' is a required property")

        read = directional_validator_for(ACCOUNT_SCHEMA, "read")
        [error] = read.iter_errors({"id": "u-1"})
        self.assertEqual(error.message, "'username' is a required property")

    def test_writeonly_property_is_required_from_clients(self):
        validator = directional_validator_for(ACCOUNT_SCHEMA, "write")
        [error] = validator.iter_errors({"username": "j"})
        self.assertEqual(error.message, "'password' is a required property")

    def test_readonly_property_is_required_in_responses(self):
        validator = directional_validator_for(ACCOUNT_SCHEMA, "read")
        [error] = validator.iter_errors({"username": "j"})
        self.assertEqual(error.message, "'id' is a required property")


class TestStructureInteraction(TestCase):
    def test_nested_objects(self):
        schema = {
            "type": "object",
            "properties": {
                "meta": {
                    "type": "object",
                    "properties": {
                        "created": {"type": "string", "readOnly": True},
                        "note": {"type": "string"},
                    },
                },
            },
        }
        validator = directional_validator_for(schema, "write")
        instance = {"meta": {"created": "today", "note": "hi"}}
        [error] = validator.iter_errors(instance)
        self.assertIn("write", error.message)
        self.assertEqual(list(error.absolute_path), ["meta", "created"])

    def test_arrays(self):
        schema = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "secret": {"type": "string", "writeOnly": True},
                    "name": {"type": "string"},
                },
            },
        }
        validator = directional_validator_for(schema, "read")
        instance = [{"name": "a"}, {"name": "b", "secret": "s"}]
        [error] = validator.iter_errors(instance)
        self.assertIn("read", error.message)
        self.assertEqual(list(error.absolute_path), [1, "secret"])

    def test_ref(self):
        schema = {
            "type": "object",
            "properties": {
                "id": {"$ref": "#/$defs/server_id"},
                "name": {"type": "string"},
            },
            "required": ["id"],
            "$defs": {
                "server_id": {"type": "string", "readOnly": True},
            },
        }
        write = directional_validator_for(schema, "write")

        # a readOnly property behind a $ref is rejected in client input
        [error] = write.iter_errors({"id": "x", "name": "n"})
        self.assertIn("read-only", error.message)
        self.assertEqual(list(error.absolute_path), ["id"])

        # ... and is not required from clients either
        self.assertTrue(write.is_valid({"name": "n"}))

        # ... but it is required in server output
        read = directional_validator_for(schema, "read")
        self.assertTrue(read.is_valid({"id": "x", "name": "n"}))
        [error] = read.iter_errors({"name": "n"})
        self.assertEqual(error.message, "'id' is a required property")

    def test_ref_chain(self):
        schema = {
            "type": "object",
            "properties": {"token": {"$ref": "#/$defs/alias"}},
            "$defs": {
                "alias": {"$ref": "#/$defs/real"},
                "real": {"type": "string", "writeOnly": True},
            },
        }
        read = directional_validator_for(schema, "read")
        [error] = read.iter_errors({"token": "t"})
        self.assertIn("write-only", error.message)


class TestConditionalBranches(TestCase):
    CONDITIONAL_SCHEMA = {
        "type": "object",
        "properties": {
            "kind": {"type": "string"},
            "token": {"type": "string"},
        },
        "if": {"properties": {"kind": {"const": "admin"}}},
        "then": {"properties": {"token": {"readOnly": True}}},
    }

    def test_non_applicable_branch_does_not_disable_property(self):
        validator = directional_validator_for(self.CONDITIONAL_SCHEMA, "write")
        # the "then" branch does not apply to non-admins, so the
        # readOnly declaration there must not affect "token"
        self.assertTrue(validator.is_valid({"kind": "user", "token": "t"}))

    def test_applicable_branch_enforces_direction(self):
        validator = directional_validator_for(self.CONDITIONAL_SCHEMA, "write")
        [error] = validator.iter_errors({"kind": "admin", "token": "t"})
        self.assertIn("read-only", error.message)
        self.assertEqual(list(error.absolute_path), ["token"])

    def test_anyof_only_applies_matching_branch(self):
        schema = {
            "type": "object",
            "properties": {"kind": {"type": "string"}},
            "anyOf": [
                {
                    "properties": {
                        "kind": {"const": "a"},
                        "value": {"readOnly": True},
                    },
                },
                {"properties": {"kind": {"const": "b"}}},
            ],
        }
        validator = directional_validator_for(schema, "write")
        self.assertTrue(validator.is_valid({"kind": "b", "value": "v"}))
        [error] = validator.iter_errors({"kind": "a", "value": "v"})
        self.assertTrue(
            any("read-only" in each.message for each in error.context),
            msg=[each.message for each in error.context],
        )


class TestConflictingAnnotations(TestCase):
    CONFLICTING_SCHEMA = {
        "type": "object",
        "properties": {
            " confused": {"readOnly": True, "writeOnly": True},
        },
    }

    def test_conflict_is_a_configuration_error_in_both_directions(self):
        for direction in ("read", "write"):
            validator = directional_validator_for(
                self.CONFLICTING_SCHEMA,
                direction,
            )
            with self.assertRaises(SchemaError) as caught:
                list(validator.iter_errors({}))
            self.assertIn("readOnly", str(caught.exception))
            self.assertIn("writeOnly", str(caught.exception))

    def test_conflict_is_reported_even_when_property_is_present(self):
        validator = directional_validator_for(self.CONFLICTING_SCHEMA, "read")
        with self.assertRaises(SchemaError):
            list(validator.iter_errors({" confused": 1}))


class TestValidationDoesNotMutate(TestCase):
    def test_instance_is_not_modified(self):
        validator = directional_validator_for(ACCOUNT_SCHEMA, "write")
        instance = {
            "id": "u-1",
            "username": "j",
            "password": "x",
            "extra": [1, 2, {"nested": True}],
        }
        schema = dict(ACCOUNT_SCHEMA)  # additionalProperties: False
        original = deepcopy(instance)
        errors = list(
            directional_validator_for(schema, "write").iter_errors(instance),
        )
        self.assertTrue(errors)
        self.assertEqual(instance, original)
        self.assertTrue(validator.is_valid({"username": "j", "password": "x"}))


class TestInterleavedUse(TestCase):
    def test_same_profile_interleaved_across_requests(self):
        Profile = directional_profile(Draft202012Validator)
        write = Profile(ACCOUNT_SCHEMA, direction="write")
        read = write.evolve(direction="read")

        requests = [
            ("write", {"username": "a", "password": "p"}, True),
            ("read", {"id": "1", "username": "a"}, True),
            ("write", {"id": "2", "username": "b", "password": "p"}, False),
            ("read", {"id": "1", "username": "a", "password": "p"}, False),
            ("write", {"username": "c", "password": "p"}, True),
            ("read", {"id": "3", "username": "d"}, True),
        ]
        for direction, instance, expected in requests:
            validator = write if direction == "write" else read
            with self.subTest(direction=direction, instance=instance):
                self.assertEqual(validator.is_valid(instance), expected)

        # interleaving did not leak state between the two validators
        self.assertEqual(write.direction, "write")
        self.assertEqual(read.direction, "read")

    def test_directional_validator_for_shares_profile_class(self):
        first = directional_validator_for(ACCOUNT_SCHEMA, "write")
        second = directional_validator_for(ACCOUNT_SCHEMA, "read")
        self.assertIs(type(first), type(second))
        self.assertTrue(first.is_valid({"username": "a", "password": "p"}))
        self.assertTrue(second.is_valid({"id": "1", "username": "a"}))


class TestStandardBehaviorUnchanged(TestCase):
    def test_standard_validator_ignores_annotations(self):
        validator = Draft202012Validator(ACCOUNT_SCHEMA)
        instance = {"id": "1", "username": "j", "password": "p"}
        self.assertTrue(validator.is_valid(instance))

    def test_standard_validator_still_enforces_required(self):
        validator = Draft202012Validator(ACCOUNT_SCHEMA)
        errors = list(validator.iter_errors({"username": "j"}))
        self.assertEqual(len(errors), 2)

    def test_profile_without_direction_matches_standard_behavior(self):
        Profile = directional_profile(Draft202012Validator)
        validator = Profile(ACCOUNT_SCHEMA)
        instance = {"id": "1", "username": "j", "password": "p"}
        self.assertTrue(validator.is_valid(instance))
        self.assertEqual(
            [e.message for e in validator.iter_errors({"username": "j"})],
            [
                e.message
                for e in Draft202012Validator(
                    ACCOUNT_SCHEMA,
                ).iter_errors({"username": "j"})
            ],
        )

    def test_invalid_direction_is_rejected(self):
        with self.assertRaises(ValueError):
            directional_validator_for(ACCOUNT_SCHEMA, "sideways")
        Profile = directional_profile(Draft202012Validator)
        with self.assertRaises(ValueError):
            Profile(ACCOUNT_SCHEMA, direction="sideways")


class TestErrorDetails(TestCase):
    def test_errors_carry_direction_and_instance_path(self):
        schema = {
            "type": "object",
            "properties": {
                "nested": {
                    "type": "object",
                    "properties": {
                        "id": {"readOnly": True},
                    },
                },
            },
        }
        validator = directional_validator_for(schema, "write")
        [error] = validator.iter_errors({"nested": {"id": 1}})
        self.assertIn("write direction", error.message)
        self.assertEqual(list(error.absolute_path), ["nested", "id"])
        self.assertEqual(error.instance, {"id": 1})
