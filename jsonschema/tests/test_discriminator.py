from copy import deepcopy
from unittest import TestCase

from referencing import Registry
from referencing.jsonschema import DRAFT202012

from jsonschema import Draft202012Validator, Draft7Validator, _discriminator
from jsonschema.validators import _RefResolver


def message_schema(n):
    return {
        "oneOf": [
            {
                "properties": {"type": {"const": f"message-{index}"}},
                "required": ["type"],
            }
            for index in range(n)
        ],
    }


class DiscriminatorIndexTests(TestCase):
    def setUp(self):
        _discriminator.reset_cache()
        _discriminator.reset_stats()
        _discriminator.enable()
        self.addCleanup(_discriminator.reset_cache)
        self.addCleanup(_discriminator.reset_stats)
        self.addCleanup(_discriminator.enable)

    def test_success_visits_only_the_candidate_branch(self):
        validator = Draft202012Validator(message_schema(40))

        self.assertTrue(validator.is_valid({"type": "message-39"}))

        stats = _discriminator.stats()
        self.assertEqual(stats["built"], 1)
        self.assertEqual(stats["fast_path_success"], 1)
        self.assertEqual(stats["branch_visits"], 1)
        self.assertEqual(stats["fallbacks"], 0)

    def test_index_is_reused_for_later_requests(self):
        validator = Draft202012Validator(message_schema(20))

        for index in range(10):
            self.assertTrue(
                validator.is_valid({"type": f"message-{index}"}),
            )

        stats = _discriminator.stats()
        self.assertEqual(stats["built"], 1)
        self.assertEqual(stats["cache_hits"], 9)
        self.assertEqual(stats["fast_path_success"], 10)
        self.assertEqual(stats["branch_visits"], 10)

    def test_unknown_discriminator_value_falls_back_and_reports_all_branches(self):
        schema = message_schema(4)
        validator = Draft202012Validator(schema)

        errors = list(validator.iter_errors({"type": "nope"}))

        self.assertEqual(len(errors), 1)
        self.assertEqual(
            errors[0].message,
            "{'type': 'nope'} is not valid under any of the given schemas",
        )
        # Every branch was still evaluated, so the full context survives.
        self.assertEqual(len(errors[0].context), 4)
        self.assertEqual(_discriminator.stats()["fallbacks"], 1)

    def test_missing_required_property_falls_back(self):
        validator = Draft202012Validator(message_schema(3))

        errors = list(validator.iter_errors({}))
        self.assertEqual(len(errors), 1)
        self.assertEqual(
            errors[0].message,
            "{} is not valid under any of the given schemas",
        )
        self.assertEqual(len(errors[0].context), 3)

    def test_overlapping_enum_values_keep_both_branches_as_candidates(self):
        schema = {
            "oneOf": [
                {
                    "properties": {"type": {"enum": ["a", "shared"]}},
                    "required": ["type"],
                },
                {
                    "properties": {"type": {"enum": ["b", "shared"]}},
                    "required": ["type"],
                },
            ],
        }
        validator = Draft202012Validator(schema)

        errors = list(validator.iter_errors({"type": "shared"}))
        self.assertEqual(len(errors), 1)
        self.assertIn("is valid under each of", errors[0].message)

        self.assertTrue(validator.is_valid({"type": "a"}))
        self.assertTrue(validator.is_valid({"type": "b"}))

    def test_true_and_one_are_distinct_discriminators(self):
        schema = {
            "oneOf": [
                {"properties": {"v": {"const": True}}, "required": ["v"]},
                {"properties": {"v": {"const": 1}}, "required": ["v"]},
            ],
        }
        validator = Draft202012Validator(schema)

        self.assertTrue(validator.is_valid({"v": True}))
        self.assertEqual(_discriminator.stats()["branch_visits"], 1)
        self.assertTrue(validator.is_valid({"v": 1}))
        self.assertEqual(_discriminator.stats()["branch_visits"], 2)

        schema = {
            "oneOf": [
                {"properties": {"v": {"const": False}}, "required": ["v"]},
                {"properties": {"v": {"const": 0}}, "required": ["v"]},
            ],
        }
        validator = Draft202012Validator(schema)
        self.assertTrue(validator.is_valid({"v": False}))
        self.assertTrue(validator.is_valid({"v": 0}))

    def test_structural_discriminator_values_compare_as_json(self):
        schema = {
            "oneOf": [
                {
                    "properties": {"v": {"const": ["x", 1]}},
                    "required": ["v"],
                },
                {
                    "properties": {"v": {"const": {"k": True}}},
                    "required": ["v"],
                },
            ],
        }
        validator = Draft202012Validator(schema)

        self.assertTrue(validator.is_valid({"v": ["x", 1]}))
        self.assertTrue(validator.is_valid({"v": {"k": True}}))
        # Python would consider 1 == True here; JSON Schema must not.
        self.assertFalse(validator.is_valid({"v": ["x", True]}))
        self.assertFalse(validator.is_valid({"v": {"k": 1}}))

    def test_optional_discriminator_property_is_not_indexed(self):
        schema = {
            "oneOf": [
                {"properties": {"type": {"const": "a"}}},
                {
                    "properties": {"type": {"const": "b"}},
                    "required": ["type"],
                },
            ],
        }
        validator = Draft202012Validator(schema)
        _discriminator.reset_stats()

        self.assertTrue(validator.is_valid({"type": "b"}))
        self.assertEqual(_discriminator.stats()["built"], 0)

    def test_mixed_constraint_styles_are_not_indexed(self):
        schema = {
            "oneOf": [
                {
                    "properties": {"type": {"const": "a"}},
                    "required": ["type"],
                },
                {
                    "properties": {"type": {"minimum": 1}},
                    "required": ["type"],
                },
            ],
        }
        validator = Draft202012Validator(schema)
        _discriminator.reset_stats()

        validator.is_valid({"type": "a"})
        # No provable discriminator exists, so no index is ever built and
        # the original exhaustive keyword behavior is untouched.
        self.assertEqual(_discriminator.stats()["built"], 0)
        self.assertEqual(_discriminator.stats()["fast_path_success"], 0)

    def test_additional_required_fields_still_index_on_discriminator(self):
        schema = {
            "oneOf": [
                {
                    "properties": {
                        "type": {"const": "a"},
                        "other": {},
                    },
                    "required": ["type", "other"],
                },
                {
                    "properties": {
                        "type": {"const": "b"},
                        "other": {},
                    },
                    "required": ["type", "other"],
                },
            ],
        }
        validator = Draft202012Validator(schema)

        # Missing the second required field fails the candidate branch and
        # falls back exhaustively, but the index itself is usable.
        self.assertFalse(validator.is_valid({"type": "a"}))
        self.assertEqual(_discriminator.stats()["built"], 1)

        _discriminator.reset_stats()
        self.assertTrue(validator.is_valid({"type": "b", "other": 1}))
        # The successful request only visits the single matching branch.
        self.assertEqual(_discriminator.stats()["branch_visits"], 1)
        self.assertEqual(_discriminator.stats()["fast_path_success"], 1)

    def test_disjoint_required_properties_are_not_indexed(self):
        schema = {
            "oneOf": [
                {"properties": {"a": {"const": 1}}, "required": ["a"]},
                {"properties": {"b": {"const": 1}}, "required": ["b"]},
            ],
        }
        validator = Draft202012Validator(schema)
        _discriminator.reset_stats()

        self.assertTrue(validator.is_valid({"a": 1}))
        self.assertEqual(_discriminator.stats()["built"], 0)

    def test_boolean_branches_are_not_indexed(self):
        schema = {
            "oneOf": [
                True,
                {
                    "properties": {"type": {"const": "a"}},
                    "required": ["type"],
                },
            ],
        }
        validator = Draft202012Validator(schema)
        _discriminator.reset_stats()

        errors = list(validator.iter_errors({"type": "a"}))
        self.assertTrue(
            any("valid under each of" in e.message for e in errors),
        )
        self.assertEqual(_discriminator.stats()["built"], 0)

    def test_plain_ref_branches_are_indexed(self):
        schema = {
            "$defs": {
                "a": {
                    "properties": {"type": {"const": "a"}},
                    "required": ["type"],
                },
                "b": {
                    "properties": {"type": {"const": "b"}},
                    "required": ["type"],
                },
            },
            "oneOf": [{"$ref": "#/$defs/a"}, {"$ref": "#/$defs/b"}],
        }
        validator = Draft202012Validator(schema)

        self.assertTrue(validator.is_valid({"type": "b"}))
        self.assertEqual(_discriminator.stats()["branch_visits"], 1)

        errors = list(validator.iter_errors({"type": "nope"}))
        self.assertEqual(
            errors[0].message,
            "{'type': 'nope'} is not valid under any of the given schemas",
        )

    def test_unresolvable_ref_is_not_indexed_but_still_raises(self):
        import referencing.exceptions

        schema = {
            "oneOf": [
                {"$ref": "#/$defs/missing"},
                {
                    "properties": {"type": {"const": "b"}},
                    "required": ["type"],
                },
            ],
        }
        validator = Draft202012Validator(schema)
        _discriminator.reset_stats()

        # The unprovable branch keeps ordinary reference behavior,
        # including its error, rather than being silently pruned.
        with self.assertRaises(referencing.exceptions.Unresolvable):
            validator.validate({"type": "b"})
        self.assertEqual(_discriminator.stats()["built"], 0)

    def test_dynamic_ref_is_never_indexed(self):
        schema = {
            "$defs": {
                "a": {
                    "$dynamicAnchor": "node",
                    "properties": {"type": {"const": "a"}},
                    "required": ["type"],
                },
            },
            "oneOf": [
                {"$dynamicRef": "#node"},
                {
                    "properties": {"type": {"const": "b"}},
                    "required": ["type"],
                },
            ],
        }
        validator = Draft202012Validator(schema)
        _discriminator.reset_stats()

        validator.is_valid({"type": "b"})
        self.assertEqual(_discriminator.stats()["built"], 0)

    def test_legacy_ref_resolver_uses_the_generic_path(self):
        schema = message_schema(3)
        resolver = _RefResolver.from_schema(schema)
        validator = Draft7Validator(schema, resolver=resolver)
        _discriminator.reset_stats()

        self.assertTrue(validator.is_valid({"type": "message-2"}))
        self.assertEqual(_discriminator.stats()["built"], 0)
        self.assertEqual(_discriminator.stats()["fast_path_success"], 0)

    def test_mutating_a_discriminator_const_rebuilds_the_index(self):
        schema = message_schema(3)
        validator = Draft202012Validator(schema)
        self.assertTrue(validator.is_valid({"type": "message-1"}))

        schema["oneOf"][1]["properties"]["type"]["const"] = "changed"

        # The stale index must not admit the old value via the old branch.
        self.assertFalse(validator.is_valid({"type": "message-1"}))
        self.assertTrue(validator.is_valid({"type": "changed"}))
        self.assertGreaterEqual(_discriminator.stats()["stale_rebuilds"], 1)

    def test_mutating_required_rebuilds_the_index(self):
        schema = message_schema(3)
        validator = Draft202012Validator(schema)
        self.assertTrue(validator.is_valid({"type": "message-0"}))

        del schema["oneOf"][0]["required"]

        # The first branch no longer requires the property, so the new
        # shape is no longer provably discriminable: the stale index is
        # dropped and validation falls back to the exhaustive path.
        validator.is_valid({"type": "message-0"})
        self.assertGreaterEqual(_discriminator.stats()["stale_rebuilds"], 1)
        # The mutated shape is no longer discriminable, so no replacement
        # index is built and validation stays on the exhaustive path.
        self.assertEqual(_discriminator.stats()["built"], 1)
        self.assertEqual(_discriminator.stats()["unindexable"], 1)

    def test_replacing_the_branch_list_is_handled_by_a_new_validator(self):
        schema = message_schema(3)
        validator = Draft202012Validator(schema)
        self.assertTrue(validator.is_valid({"type": "message-2"}))

        schema["oneOf"] = [
            {
                "properties": {"type": {"const": "only"}},
                "required": ["type"],
            },
        ]
        # A validator caches its keywords at construction time, so a
        # replaced applicator is observed via a fresh validator, which
        # must not inherit the previous validator's cached index.
        rebuilt = Draft202012Validator(schema)
        self.assertFalse(rebuilt.is_valid({"type": "message-2"}))
        self.assertTrue(rebuilt.is_valid({"type": "only"}))
        self.assertGreaterEqual(_discriminator.stats()["stale_rebuilds"], 0)

    def test_in_place_branch_replacement_rebuilds_the_index(self):
        schema = message_schema(3)
        validator = Draft202012Validator(schema)
        self.assertTrue(validator.is_valid({"type": "message-2"}))

        schema["oneOf"][1] = {
            "properties": {"type": {"const": "only"}},
            "required": ["type"],
        }

        # The rebuilt index routes the new const to its branch, and the
        # previously valid message-1 value no longer matches any branch.
        self.assertTrue(validator.is_valid({"type": "only"}))
        self.assertFalse(validator.is_valid({"type": "message-1"}))
        self.assertGreaterEqual(_discriminator.stats()["stale_rebuilds"], 1)

    def test_mutating_an_unrelated_keyword_keeps_the_result_correct(self):
        schema = message_schema(2)
        validator = Draft202012Validator(schema)
        self.assertTrue(validator.is_valid({"type": "message-0"}))

        # A change outside the discriminating fragment invalidates the
        # index (forcing a rebuild) but must never weaken validation: the
        # new constraint is still fully enforced on the candidate branch.
        schema["oneOf"][0]["maxProperties"] = 1
        self.assertFalse(
            validator.is_valid(
                {"type": "message-0", "extra": "ignored"},
            ),
        )
        self.assertTrue(validator.is_valid({"type": "message-1"}))
        self.assertGreaterEqual(_discriminator.stats()["stale_rebuilds"], 1)

    def test_index_disabled_matches_enabled_behavior(self):
        schema = message_schema(5)
        instances = [
            {"type": "message-0"},
            {"type": "message-4"},
            {"type": "unknown"},
            {},
            {"type": "message-0", "other": 1},
        ]

        _discriminator.disable()
        disabled = [
            list(Draft202012Validator(deepcopy(schema)).iter_errors(each))
            for each in instances
        ]
        _discriminator.enable()
        _discriminator.reset_cache()
        enabled = [
            list(Draft202012Validator(deepcopy(schema)).iter_errors(each))
            for each in instances
        ]

        for left, right in zip(disabled, enabled):
            self.assertEqual(
                [error.message for error in left],
                [error.message for error in right],
            )

    def test_environment_toggle_disables_indexing(self):
        # The module reads the switch at import time; the documented toggle
        # controls the same flag the keyword consults.
        self.assertTrue(_discriminator.is_enabled())
        _discriminator.disable()
        self.assertFalse(_discriminator.is_enabled())
        validator = Draft202012Validator(message_schema(3))
        validator.is_valid({"type": "message-0"})
        self.assertEqual(_discriminator.stats()["built"], 0)

    def test_unevaluated_properties_annotation_is_unchanged(self):
        schema = {
            "type": "object",
            "oneOf": [
                {"properties": {"type": {"const": "x"}, "p": {}}},
                {"properties": {"type": {"const": "y"}, "q": {}}},
            ],
            "unevaluatedProperties": False,
        }
        instances = [
            {"type": "x", "p": 1},
            {"type": "x", "extra": 1},
            {"type": "y", "q": 2},
            {"type": "y", "extra": 2},
        ]

        _discriminator.disable()
        disabled = [
            [e.message for e in Draft202012Validator(schema).iter_errors(i)]
            for i in instances
        ]
        _discriminator.enable()
        _discriminator.reset_cache()
        enabled = [
            [e.message for e in Draft202012Validator(schema).iter_errors(i)]
            for i in instances
        ]
        self.assertEqual(disabled, enabled)
        self.assertEqual(
            enabled[1],
            ["Unevaluated properties are not allowed "
             "('extra' was unexpected)"],
        )

    def test_a_different_registry_rebuilds_the_index(self):
        schema = {
            "$defs": {
                "a": {
                    "properties": {"type": {"const": "a"}},
                    "required": ["type"],
                },
            },
            "oneOf": [
                {"$ref": "#/$defs/a"},
                {
                    "properties": {"type": {"const": "b"}},
                    "required": ["type"],
                },
            ],
        }
        validator = Draft202012Validator(schema)
        self.assertTrue(validator.is_valid({"type": "a"}))

        other_registry = Registry().with_resource(
            uri="urn:example",
            resource=DRAFT202012.create_resource({"type": "integer"}),
        )
        rebuilt = Draft202012Validator(
            schema,
            registry=other_registry,
        )
        _discriminator.reset_stats()
        self.assertTrue(rebuilt.is_valid({"type": "a"}))
        self.assertEqual(_discriminator.stats()["built"], 1)
        self.assertEqual(_discriminator.stats()["cache_hits"], 0)

    def test_non_object_instances_use_the_generic_path(self):
        validator = Draft202012Validator(message_schema(2))
        for instance in (12, "string", [1, 2], None, True):
            self.assertFalse(validator.is_valid(instance))
        self.assertEqual(_discriminator.stats()["fast_path_success"], 0)

    def test_nested_one_of_indexes_independently(self):
        schema = {
            "oneOf": [
                {
                    "properties": {
                        "outer": {"const": "o"},
                        "inner": message_schema(4),
                    },
                    "required": ["outer"],
                },
                {
                    "properties": {"outer": {"const": "p"}},
                    "required": ["outer"],
                },
            ],
        }
        validator = Draft202012Validator(schema)
        self.assertTrue(
            validator.is_valid(
                {"outer": "o", "inner": {"type": "message-3"}},
            ),
        )
