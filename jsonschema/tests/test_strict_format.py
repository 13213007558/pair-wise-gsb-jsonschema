"""
Tests for strict :kw:`format` checking.

Strict format checking fails early while a validator is being prepared unless
every format the schema genuinely declares has a usable checker, instead of
silently ignoring formats whose optional dependencies are missing.
"""

from __future__ import annotations

from unittest import TestCase

from referencing import Registry
from referencing.jsonschema import DRAFT202012

from jsonschema import FormatChecker
from jsonschema.exceptions import (
    FrozenFormatCheckerError,
    StrictFormatError,
)
from jsonschema.validators import (
    Draft3Validator,
    Draft4Validator,
    Draft7Validator,
    Draft201909Validator,
    Draft202012Validator,
)


def accept_only(value):
    def checker(instance):
        return instance == value

    return checker


class TestStrictFormatPreparation(TestCase):
    def test_known_format_without_dependency_fails_early(self):
        # This environment deliberately has no optional format packages.
        with self.assertRaises(StrictFormatError) as cm:
            Draft202012Validator({"format": "uri"}, strict_format=True)

        error = cm.exception
        self.assertEqual(error.format, "uri")
        self.assertEqual(error.reason, StrictFormatError.MISSING_DEPENDENCY)
        self.assertEqual(error.dependency, "rfc3987")
        self.assertEqual(list(error.schema_path), ["format"])

    def test_unknown_format_fails_early(self):
        with self.assertRaises(StrictFormatError) as cm:
            Draft202012Validator(
                {"format": "made-up-format"},
                strict_format=True,
            )

        error = cm.exception
        self.assertEqual(error.format, "made-up-format")
        self.assertEqual(error.reason, StrictFormatError.UNKNOWN)
        self.assertIsNone(error.dependency)
        self.assertEqual(list(error.schema_path), ["format"])

    def test_available_builtin_format_is_enforced(self):
        validator = Draft202012Validator(
            {"format": "ipv4"}, strict_format=True,
        )

        self.assertTrue(validator.is_valid("127.0.0.1"))
        self.assertFalse(validator.is_valid("not-an-ipv4"))

    def test_injected_minimal_checker_validates_legal_and_illegal_values(self):
        checker = FormatChecker(formats=())
        checker.checks("custom-widget")(accept_only("ok"))

        validator = Draft202012Validator(
            {"format": "custom-widget"},
            format_checker=checker,
            strict_format=True,
        )

        self.assertTrue(validator.is_valid("ok"))
        self.assertFalse(validator.is_valid("nope"))

    def test_annotation_only_allows_known_missing_dependency_format(self):
        validator = Draft202012Validator(
            {"format": "uri"},
            strict_format=True,
            annotation_only_formats=frozenset({"uri"}),
        )

        # Annotation-only formats are not enforced.
        self.assertTrue(validator.is_valid("::::not-a-uri"))
        self.assertNotIn("uri", validator.format_checker.checkers)

    def test_annotation_only_format_still_requires_being_known(self):
        with self.assertRaises(StrictFormatError) as cm:
            Draft202012Validator(
                {"format": "made-up-format"},
                strict_format=True,
                annotation_only_formats=frozenset({"made-up-format"}),
            )
        self.assertEqual(cm.exception.reason, StrictFormatError.UNKNOWN)


class TestStrictSchemaTraversal(TestCase):
    def test_traverses_defs_and_combinator_keywords(self):
        schema = {
            "$defs": {"a": {"format": "email"}},
            "allOf": [{"$ref": "#/$defs/a"}],
            "anyOf": [{"oneOf": [{"format": "regex"}]}],
        }
        validator = Draft202012Validator(schema, strict_format=True)
        self.assertEqual(
            sorted(validator.format_checker.checkers),
            ["email", "regex"],
        )

    def test_const_and_enum_data_named_format_are_not_declarations(self):
        schema = {
            "const": {"format": "uri"},
            "enum": ["format", {"format": "hostname"}],
            "format": "email",
        }
        validator = Draft202012Validator(schema, strict_format=True)
        self.assertEqual(sorted(validator.format_checker.checkers), ["email"])

    def test_follows_local_registry_references(self):
        registry = Registry().with_resource(
            "https://example.com/referenced",
            DRAFT202012.create_resource({"format": "uuid"}),
        )
        validator = Draft202012Validator(
            {"$ref": "https://example.com/referenced"},
            registry=registry,
            strict_format=True,
        )
        self.assertEqual(sorted(validator.format_checker.checkers), ["uuid"])

    def test_missing_dependency_in_referenced_schema_reports_schema_path(self):
        registry = Registry().with_resource(
            "https://example.com/referenced",
            DRAFT202012.create_resource({"format": "duration"}),
        )
        with self.assertRaises(StrictFormatError) as cm:
            Draft202012Validator(
                {"allOf": [{"$ref": "https://example.com/referenced"}]},
                registry=registry,
                strict_format=True,
            )

        error = cm.exception
        self.assertEqual(error.format, "duration")
        self.assertEqual(error.reason, StrictFormatError.MISSING_DEPENDENCY)
        self.assertEqual(
            list(error.schema_path),
            ["allOf", 0, "$ref", "format"],
        )

    def test_cyclic_reference_does_not_expand_infinitely(self):
        schema = {
            "$defs": {
                "node": {
                    "type": "object",
                    "properties": {"child": {"$ref": "#/$defs/node"}},
                    "format": "ipv4",
                }
            },
            "$ref": "#/$defs/node",
        }
        validator = Draft202012Validator(schema, strict_format=True)
        self.assertEqual(sorted(validator.format_checker.checkers), ["ipv4"])

    def test_draft7_ref_siblings_are_ignored(self):
        schema = {
            "$ref": "#/$defs/x",
            "format": "uri",
            "$defs": {"x": {"type": "string"}},
        }
        # Draft <= 7 ignores siblings of a $ref, so the format is not declared.
        validator = Draft7Validator(schema, strict_format=True)
        self.assertEqual(dict(validator.format_checker.checkers), {})

    def test_draft202012_ref_siblings_are_applied(self):
        schema = {
            "$ref": "#/$defs/x",
            "format": "uri",
            "$defs": {"x": {"type": "string"}},
        }
        with self.assertRaises(StrictFormatError) as cm:
            Draft202012Validator(schema, strict_format=True)
        self.assertEqual(cm.exception.format, "uri")

    def test_nested_failure_reports_deep_schema_path(self):
        schema = {"properties": {"a": {"format": "uri"}}}
        with self.assertRaises(StrictFormatError) as cm:
            Draft202012Validator(schema, strict_format=True)
        self.assertEqual(
            list(cm.exception.schema_path),
            ["properties", "a", "format"],
        )

    def test_unresolvable_reference_does_not_break_preparation(self):
        schema = {"$ref": "https://example.com/missing", "format": "ipv4"}
        validator = Draft202012Validator(schema, strict_format=True)
        self.assertEqual(sorted(validator.format_checker.checkers), ["ipv4"])

    def test_boolean_schemas(self):
        validator = Draft202012Validator(True, strict_format=True)
        self.assertTrue(validator.format_checker.frozen)
        self.assertEqual(dict(validator.format_checker.checkers), {})

    def test_dynamic_reference_cycle_across_registry_documents(self):
        node = {
            "$id": "https://example.com/node",
            "type": "object",
            "properties": {"child": {"$dynamicRef": "#node"}},
            "format": "uuid",
        }
        root = {
            "$id": "https://example.com/root",
            "$dynamicAnchor": "node",
            "$ref": "https://example.com/node",
        }
        registry = Registry().with_resources(
            [
                (
                    "https://example.com/node",
                    DRAFT202012.create_resource(node),
                ),
                (
                    "https://example.com/root",
                    DRAFT202012.create_resource(root),
                ),
            ],
        )
        validator = Draft202012Validator(
            root,
            registry=registry,
            strict_format=True,
        )
        self.assertEqual(sorted(validator.format_checker.checkers), ["uuid"])

    def test_draft3_extends_and_definitions_are_traversed(self):
        schema = {
            "definitions": {"d": {"format": "regex"}},
            "extends": [{"$ref": "#/definitions/d"}],
        }
        validator = Draft3Validator(schema, strict_format=True)
        self.assertEqual(sorted(validator.format_checker.checkers), ["regex"])

    def test_draft201909_recursive_ref_does_not_loop(self):
        schema = {
            "$id": "https://example.com/r",
            "$recursiveAnchor": True,
            "properties": {"child": {"$recursiveRef": "#"}},
            "format": "email",
        }
        validator = Draft201909Validator(schema, strict_format=True)
        self.assertEqual(sorted(validator.format_checker.checkers), ["email"])

    def test_applicator_keywords_are_all_traversed(self):
        schema = {
            "patternProperties": {"^x": {"format": "regex"}},
            "prefixItems": [{"format": "ipv4"}, {"format": "email"}],
            "dependentSchemas": {"a": {"format": "uuid"}},
            "propertyNames": {"format": "regex"},
            "additionalProperties": {"format": "date"},
            "not": {"format": "ipv6"},
            "contains": {"format": "regex"},
        }
        validator = Draft202012Validator(schema, strict_format=True)
        self.assertEqual(
            sorted(validator.format_checker.checkers),
            ["date", "email", "ipv4", "ipv6", "regex", "uuid"],
        )

    def test_draft7_dependencies_and_tuple_items_are_traversed(self):
        schema = {
            "dependencies": {"a": {"format": "email"}},
            "items": [{"format": "ipv4"}, {"format": "regex"}],
        }
        validator = Draft7Validator(schema, strict_format=True)
        self.assertEqual(
            sorted(validator.format_checker.checkers),
            ["email", "ipv4", "regex"],
        )


class TestFrozenChecker(TestCase):
    def test_checker_set_is_frozen(self):
        validator = Draft202012Validator(
            {"format": "ipv4"}, strict_format=True,
        )
        with self.assertRaises(FrozenFormatCheckerError):
            validator.format_checker.checks("email")(lambda thing: True)

    def test_later_registration_does_not_change_prepared_validator(self):
        validator = Draft202012Validator(
            {"format": "ipv4"}, strict_format=True,
        )
        Draft202012Validator.FORMAT_CHECKER.checks("ipv4")(lambda thing: True)

        self.assertFalse(validator.is_valid("not-an-ipv4"))

    def test_source_checker_is_left_mutable(self):
        checker = FormatChecker()
        checker.checks("custom-widget")(accept_only("ok"))
        Draft202012Validator(
            {"format": "custom-widget"},
            format_checker=checker,
            strict_format=True,
        )
        self.assertFalse(checker.frozen)
        checker.checks("other")(lambda thing: True)

    def test_prepared_checker_only_contains_declared_formats(self):
        validator = Draft202012Validator(
            {"format": "ipv4"}, strict_format=True,
        )
        self.assertEqual(set(validator.format_checker.checkers), {"ipv4"})


class TestStrictEvolve(TestCase):
    def test_evolve_reprepares_for_new_schema(self):
        validator = Draft202012Validator(
            {"format": "ipv4"}, strict_format=True,
        )
        evolved = validator.evolve(schema={"format": "uuid"})

        self.assertTrue(evolved.strict_format)
        self.assertEqual(sorted(evolved.format_checker.checkers), ["uuid"])
        self.assertIsNot(
            evolved.format_checker,
            validator.format_checker,
        )

    def test_evolve_into_unsupported_format_fails(self):
        validator = Draft202012Validator(
            {"format": "ipv4"}, strict_format=True,
        )
        with self.assertRaises(StrictFormatError):
            validator.evolve(schema={"format": "uri"})


class TestStrictFormatError(TestCase):
    def test_it_carries_format_reason_path_and_dependency(self):
        error = StrictFormatError(
            "uri",
            StrictFormatError.MISSING_DEPENDENCY,
            schema_path=("properties", "a", "format"),
            dependency="rfc3987",
        )
        self.assertEqual(error.format, "uri")
        self.assertEqual(error.reason, "missing-dependency")
        self.assertEqual(error.dependency, "rfc3987")
        self.assertEqual(
            list(error.schema_path),
            ["properties", "a", "format"],
        )
        self.assertIn("rfc3987", str(error))
        self.assertIn("['properties']['a']['format']", str(error))

    def test_the_three_states_are_distinct_constants(self):
        self.assertEqual(
            {
                StrictFormatError.UNKNOWN,
                StrictFormatError.MISSING_DEPENDENCY,
                StrictFormatError.ANNOTATION_ONLY,
            },
            {"unknown", "missing-dependency", "annotation-only"},
        )


class TestLenientDefault(TestCase):
    def test_default_behavior_remains_lenient(self):
        validator = Draft202012Validator({"format": "totally-unknown"})
        self.assertIsNone(validator.format_checker)
        self.assertTrue(validator.is_valid("anything"))

    def test_non_strict_explicit_checker_unchanged(self):
        validator = Draft4Validator({"format": "ipv4"})
        self.assertIsNone(validator.format_checker)
