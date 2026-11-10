"""
Security regressions for branch-aware evaluation annotations.

These tests pin down which object properties and array items the
Draft 2020-12 validator considers *evaluated* when ``unevaluatedProperties``
or ``unevaluatedItems`` is set to ``false`` and the schema combines
``allOf``, ``anyOf``, ``oneOf``, ``not``, ``if``/``then``/``else`` and
``$ref``.

The threat model is an attacker sending an extra field (or array item)
that is only mentioned inside an applicator branch which does not
successfully evaluate against the instance. Such a branch must not mark
the extra data as evaluated: merging the property names or item indexes
of *every* branch would turn unevaluated, rejected data into data the
schema claims to have handled.

How to re-derive each expectation by hand
------------------------------------------

Per the Draft 2020-12 specification, in-place applicators only contribute
evaluation annotations when they evaluate successfully:

* ``properties`` / ``prefixItems`` evaluate the names / indexes they
  declare, but only when the enclosing subschema itself evaluates.
* ``allOf``: every subschema must pass; annotations are the union of the
  annotations of all subschemas.
* ``anyOf``: only the subschemas that pass contribute; with several
  passing subschemas their annotations are unioned. A failing subschema
  contributes nothing, even for names it mentions.
* ``oneOf``: exactly one subschema must pass, and only that subschema
  contributes. When zero or more than one subschema pass the keyword
  fails, so an instance carrying extra data must still be rejected.
* ``not``: never produces evaluation annotations, regardless of whether
  its subschema matches.
* ``if``: on success the ``if`` and ``then`` subschemas contribute; on
  failure only ``else`` contributes. A branch that is not taken
  contributes nothing.
* ``$ref``: annotations are computed from the referenced schema, so a
  reference living inside a failing branch contributes nothing.

The expected evaluated set for every corpus entry below is therefore the
union over *successful* branches only -- never the union of every name
or index written anywhere in the schema. Each test keeps a positive
example (the evaluated data is accepted) and a negative example (data
only named by an unsuccessful branch is reported as unevaluated).

All ``$ref`` targets live in a local in-memory registry; no schema is
ever fetched over the network.
"""

from __future__ import annotations

from unittest import TestCase

from referencing.jsonschema import DRAFT202012
import referencing.jsonschema

from jsonschema import Draft202012Validator, ValidationError, validate

LOCAL_REGISTRY_ROOT = "https://local.test/"

OBJECT_TARGET_URI = LOCAL_REGISTRY_ROOT + "evaluated-object"
ARRAY_TARGET_URI = LOCAL_REGISTRY_ROOT + "evaluated-array"
LONG_ARRAY_TARGET_URI = LOCAL_REGISTRY_ROOT + "long-evaluated-array"

# Referenced schemas deliberately evaluate different names / indexes than
# the schemas that reference them, so a reference resolved from a failing
# branch cannot be confused with annotations produced by a passing one.
REGISTRY = referencing.jsonschema.EMPTY_REGISTRY.combine(
    referencing.jsonschema.Registry().with_contents(
        [
            (
                OBJECT_TARGET_URI,
                {"properties": {"referenced": {}}},
            ),
            (
                ARRAY_TARGET_URI,
                {"prefixItems": [{}]},
            ),
            (
                LONG_ARRAY_TARGET_URI,
                {"prefixItems": [{}, {}]},
            ),
        ],
        default_specification=DRAFT202012,
    ),
)


class EvaluationAnnotationMixin:
    """
    Assertion helpers shared by the object and array corpora.

    Every expectation is checked through the three public validator entry
    points plus the module-level ``jsonschema.validate``: the boolean
    ``is_valid`` answer, the collected ``iter_errors`` errors (including
    their instance and schema paths) and the exception raised by
    ``validate`` must all agree.
    """

    validator_cls = Draft202012Validator
    registry = REGISTRY

    def validator_for(self, schema):
        return self.validator_cls(schema, registry=self.registry)

    def assertValid(self, schema, instance):
        validator = self.validator_for(schema)

        self.assertTrue(validator.is_valid(instance))
        self.assertEqual(list(validator.iter_errors(instance)), [])
        # Raises nothing when the instance is valid.
        validator.validate(instance)
        validate(
            instance=instance,
            schema=schema,
            cls=self.validator_cls,
            registry=self.registry,
        )

    def assertUnevaluated(self, schema, instance, keyword):
        """
        The instance is rejected specifically by ``keyword``.

        ``unevaluatedProperties`` and ``unevaluatedItems`` report at the
        root of the instance, with the keyword itself at the end of the
        schema path, so error paths are asserted rather than only the
        boolean result.
        """
        validator = self.validator_for(schema)

        self.assertFalse(validator.is_valid(instance))

        errors = list(validator.iter_errors(instance))
        self.assertTrue(errors)
        unevaluated_errors = [
            error for error in errors if error.validator == keyword
        ]
        self.assertEqual(len(unevaluated_errors), 1, errors)
        error = unevaluated_errors[0]
        self.assertEqual(list(error.absolute_path), [])
        self.assertEqual(list(error.absolute_schema_path), [keyword])

        with self.assertRaises(ValidationError) as raised:
            validator.validate(instance)
        self.assertEqual(raised.exception.validator, keyword)

        with self.assertRaises(ValidationError):
            validate(
                instance=instance,
                schema=schema,
                cls=self.validator_cls,
                registry=self.registry,
            )


class TestUnevaluatedPropertyAnnotations(EvaluationAnnotationMixin, TestCase):
    """
    Object corpus: only successful branches may evaluate property names.
    """

    def test_anyOf_failing_branch_does_not_evaluate_its_properties(self):
        # Branch 0 requires "a" and also mentions "extra"; branch 1
        # requires "b" and evaluates only "b". An instance without "a"
        # makes branch 0 fail, so "extra" stays unevaluated even though
        # the name appears in the schema.
        schema = {
            "type": "object",
            "anyOf": [
                {"required": ["a"], "properties": {"a": {}, "extra": {}}},
                {"required": ["b"], "properties": {"b": {}}},
            ],
            "unevaluatedProperties": False,
        }
        # Branch 1 passes and evaluates {"b"}: no unevaluated name.
        self.assertValid(schema, {"b": 1})
        # "extra" is named only by the failing branch 0.
        self.assertUnevaluated(
            schema,
            {"b": 1, "extra": 2},
            "unevaluatedProperties",
        )

    def test_anyOf_several_passing_branches_union_their_annotations(self):
        # Both branches pass for {"a": 1, "b": 2}; the evaluated set is
        # the union {"a", "b"} -- this is the legitimate merge case,
        # which must not be confused with merging failing branches.
        schema = {
            "type": "object",
            "anyOf": [
                {"properties": {"a": {}}},
                {"properties": {"b": {}}},
            ],
            "unevaluatedProperties": False,
        }
        self.assertValid(schema, {"a": 1, "b": 2})
        # No branch mentions "c", so it is not part of the union.
        self.assertUnevaluated(
            schema,
            {"a": 1, "c": 3},
            "unevaluatedProperties",
        )

    def test_oneOf_single_passing_branch_evaluates_only_its_properties(self):
        # The branches are made exclusive with "required". An instance
        # with "y" passes only branch 1, which evaluates {"y"}.
        schema = {
            "type": "object",
            "oneOf": [
                {
                    "required": ["x"],
                    "properties": {"x": {}, "extra": {}},
                },
                {"required": ["y"], "properties": {"y": {}}},
            ],
            "unevaluatedProperties": False,
        }
        self.assertValid(schema, {"y": 1})
        # "extra" belongs to the branch that did not match.
        self.assertUnevaluated(
            schema,
            {"y": 1, "extra": 2},
            "unevaluatedProperties",
        )

    def test_oneOf_multiple_passing_branches_still_rejects_extra_data(self):
        # Without an exclusivity guard both branches match {"a": 1}, so
        # "oneOf" itself fails and the instance is rejected no matter
        # how annotations are collected. Adding "extra" -- a name only
        # the first branch mentions -- must not make the instance
        # valid: the failure of "oneOf" cannot launder a branch's
        # property names into evaluated ones. The guaranteed signal in
        # both instances is the "oneOf" error at the instance root.
        schema = {
            "type": "object",
            "oneOf": [
                {"properties": {"a": {}, "extra": {}}},
                {"properties": {"a": {}}},
            ],
            "unevaluatedProperties": False,
        }
        validator = self.validator_for(schema)
        for instance in ({"a": 1}, {"a": 1, "extra": 2}):
            self.assertFalse(validator.is_valid(instance))
            errors = list(validator.iter_errors(instance))
            self.assertEqual(
                {error.validator for error in errors},
                {"oneOf"},
            )
            one_of_error = errors[0]
            self.assertEqual(list(one_of_error.absolute_path), [])
            self.assertEqual(
                list(one_of_error.absolute_schema_path),
                ["oneOf"],
            )
            with self.assertRaises(ValidationError):
                validator.validate(instance)

    def test_allOf_unions_annotations_of_every_required_subschema(self):
        # Both subschemas must pass; together they evaluate {"a", "b"}.
        schema = {
            "type": "object",
            "allOf": [
                {"properties": {"a": {}}},
                {"properties": {"b": {}}},
            ],
            "unevaluatedProperties": False,
        }
        self.assertValid(schema, {"a": 1, "b": 2})
        self.assertUnevaluated(
            schema,
            {"a": 1, "c": 3},
            "unevaluatedProperties",
        )

    def test_not_never_evaluates_properties(self):
        # "not" only rejects instances matching its subschema; it has no
        # successful branch and therefore contributes no annotations, so
        # "extra" is evaluated by nothing.
        schema = {
            "type": "object",
            "not": {"required": ["forbidden"]},
            "properties": {"allowed": {}},
            "unevaluatedProperties": False,
        }
        self.assertValid(schema, {"allowed": 1})
        self.assertUnevaluated(
            schema,
            {"allowed": 1, "extra": 2},
            "unevaluatedProperties",
        )
        # Sanity check that "not" still does its own rejecting job.
        self.assertFalse(
            self.validator_for(schema).is_valid({"forbidden": 1}),
        )

    def test_if_then_else_only_the_taken_branch_evaluates(self):
        # When "if" succeeds the "then" names ("then_only") are
        # evaluated and the "else" name ("else_only") is not, and vice
        # versa. Names from the branch not taken must stay unevaluated.
        schema = {
            "type": "object",
            "if": {"required": ["switch"]},
            "then": {"properties": {"switch": {}, "then_only": {}}},
            "else": {"properties": {"else_only": {}}},
            "unevaluatedProperties": False,
        }
        self.assertValid(schema, {"switch": 1, "then_only": 2})
        self.assertUnevaluated(
            schema,
            {"switch": 1, "else_only": 2},
            "unevaluatedProperties",
        )
        self.assertValid(schema, {"else_only": 1})
        self.assertUnevaluated(
            schema,
            {"else_only": 1, "then_only": 2},
            "unevaluatedProperties",
        )

    def test_ref_evaluates_properties_from_the_referenced_schema(self):
        # A top-level "$ref" succeeds, so "referenced" is evaluated via
        # the locally registered target schema.
        schema = {
            "type": "object",
            "$ref": OBJECT_TARGET_URI,
            "unevaluatedProperties": False,
        }
        self.assertValid(schema, {"referenced": 1})
        self.assertUnevaluated(
            schema,
            {"referenced": 1, "extra": 2},
            "unevaluatedProperties",
        )

    def test_ref_inside_a_failing_branch_contributes_nothing(self):
        # Branch 0 references a schema evaluating "referenced" but also
        # requires "a", which the instance does not have. Branch 1
        # passes and evaluates only "b". The referenced name must not
        # leak out of the failing branch.
        schema = {
            "type": "object",
            "anyOf": [
                {
                    "required": ["a"],
                    "$ref": OBJECT_TARGET_URI,
                },
                {"required": ["b"], "properties": {"b": {}}},
            ],
            "unevaluatedProperties": False,
        }
        self.assertValid(schema, {"b": 1})
        self.assertUnevaluated(
            schema,
            {"b": 1, "referenced": 2},
            "unevaluatedProperties",
        )


class TestUnevaluatedItemAnnotations(EvaluationAnnotationMixin, TestCase):
    """
    Array corpus: only successful branches may evaluate item indexes.
    """

    def test_anyOf_failing_branch_does_not_evaluate_its_indexes(self):
        # Branch 0 needs at least four items; on a shorter instance it
        # fails even though its "prefixItems" declares indexes 0..2.
        # Branch 1 passes and evaluates only index 0. The negative
        # array is kept short enough that the failing branch alone
        # would cover every present index, so any leak is observable.
        schema = {
            "type": "array",
            "anyOf": [
                {"minItems": 4, "prefixItems": [{}, {}, {}]},
                {"prefixItems": [{}]},
            ],
            "unevaluatedItems": False,
        }
        self.assertValid(schema, [1])
        # Under correct rules only index 0 is evaluated; indexes 1 and
        # 2 are named solely by the failing branch 0.
        self.assertUnevaluated(
            schema,
            [1, 2, 3],
            "unevaluatedItems",
        )

    def test_anyOf_several_passing_branches_union_their_indexes(self):
        # Both branches pass for a two-item array; the evaluated indexes
        # are the union {0, 1}.
        schema = {
            "type": "array",
            "anyOf": [
                {"prefixItems": [{}]},
                {"prefixItems": [{}, {}]},
            ],
            "unevaluatedItems": False,
        }
        self.assertValid(schema, [1, 2])
        self.assertUnevaluated(
            schema,
            [1, 2, 3],
            "unevaluatedItems",
        )

    def test_oneOf_single_passing_branch_evaluates_only_its_indexes(self):
        # Branch 0 only matches arrays of at least three items and
        # evaluates indexes 0..2; branch 1 only matches arrays of at
        # most two items and evaluates index 0. The length guards make
        # the branches exclusive, so each instance has exactly one
        # passing branch.
        schema = {
            "type": "array",
            "oneOf": [
                {"minItems": 3, "prefixItems": [{}, {}, {}]},
                {"maxItems": 2, "prefixItems": [{}]},
            ],
            "unevaluatedItems": False,
        }
        # Each positive example matches exactly one branch and every
        # present item is evaluated by that branch.
        self.assertValid(schema, [1, 2, 3])
        self.assertValid(schema, [1])
        # The two-item array matches only branch 1; index 1 is named
        # solely by the not-taken branch 0 and must stay unevaluated.
        self.assertUnevaluated(
            schema,
            [1, 2],
            "unevaluatedItems",
        )

    def test_oneOf_multiple_passing_branches_still_rejects_extra_items(self):
        # A four-item array satisfies both branches, so "oneOf" fails.
        # The extra items must not be laundered into evaluated ones:
        # alongside the "oneOf" error, unevaluatedItems must fire for
        # the indexes beyond what both branches declare.
        schema = {
            "type": "array",
            "oneOf": [
                {"minItems": 4, "prefixItems": [{}, {}, {}]},
                {"prefixItems": [{}]},
            ],
            "unevaluatedItems": False,
        }
        validator = self.validator_for(schema)
        instance = [1, 2, 3, 4]
        self.assertFalse(validator.is_valid(instance))
        errors = list(validator.iter_errors(instance))
        validators = {error.validator for error in errors}
        self.assertEqual(
            validators,
            {"oneOf", "unevaluatedItems"},
        )
        one_of_error = next(
            error for error in errors if error.validator == "oneOf"
        )
        self.assertEqual(list(one_of_error.absolute_path), [])
        with self.assertRaises(ValidationError):
            validator.validate(instance)

    def test_allOf_unions_indexes_of_every_required_subschema(self):
        # Both subschemas pass; together they evaluate indexes {0, 1}.
        schema = {
            "type": "array",
            "allOf": [
                {"prefixItems": [{}]},
                {"prefixItems": [{}, {}]},
            ],
            "unevaluatedItems": False,
        }
        self.assertValid(schema, [1, 2])
        self.assertUnevaluated(
            schema,
            [1, 2, 3],
            "unevaluatedItems",
        )

    def test_not_never_evaluates_items(self):
        # "not" rejects arrays containing "forbidden" but never
        # evaluates any index, so a second item is unevaluated.
        schema = {
            "type": "array",
            "not": {"contains": {"const": "forbidden"}},
            "prefixItems": [{}],
            "unevaluatedItems": False,
        }
        self.assertValid(schema, [1])
        self.assertUnevaluated(schema, [1, 2], "unevaluatedItems")
        self.assertFalse(
            self.validator_for(schema).is_valid(["forbidden"]),
        )

    def test_if_then_else_only_the_taken_branch_evaluates_indexes(self):
        # When "if" matches, "then" evaluates indexes 0 and 1;
        # otherwise "else" evaluates only index 0. The not-taken
        # branch's prefix must not count as evaluated.
        schema = {
            "type": "array",
            "if": {"contains": {"const": "switch"}},
            "then": {"prefixItems": [{}, {}]},
            "else": {"prefixItems": [{}]},
            "unevaluatedItems": False,
        }
        self.assertValid(schema, ["switch", 2])
        self.assertUnevaluated(
            schema,
            ["switch", 2, 3],
            "unevaluatedItems",
        )
        self.assertValid(schema, [1])
        self.assertUnevaluated(
            schema,
            [1, 2],
            "unevaluatedItems",
        )

    def test_ref_evaluates_indexes_from_the_referenced_schema(self):
        # The top-level reference resolves locally and evaluates
        # index 0.
        schema = {
            "type": "array",
            "$ref": ARRAY_TARGET_URI,
            "unevaluatedItems": False,
        }
        self.assertValid(schema, [1])
        self.assertUnevaluated(schema, [1, 2], "unevaluatedItems")

    def test_ref_inside_a_failing_branch_contributes_no_indexes(self):
        # Branch 0 references a schema evaluating indexes 0 and 1 but
        # also requires at least three items, so a one- or two-item
        # array fails it. Branch 1 passes on short arrays and evaluates
        # only index 0. On the two-item negative example, index 1 is
        # covered solely by the reference in the failing branch.
        schema = {
            "type": "array",
            "anyOf": [
                {"minItems": 3, "$ref": LONG_ARRAY_TARGET_URI},
                {"maxItems": 2, "prefixItems": [{}]},
            ],
            "unevaluatedItems": False,
        }
        self.assertValid(schema, [1])
        self.assertUnevaluated(schema, [1, 2], "unevaluatedItems")
