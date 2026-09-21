"""
Tests for the parts of jsonschema related to the :kw:`format` keyword.
"""

import copy
import pickle
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import TestCase

from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from jsonschema._format import ExceptionPolicy
from jsonschema.exceptions import FormatError
from jsonschema.validators import Draft4Validator

BOOM = ValueError("Boom!")
BANG = ZeroDivisionError("Bang!")


def boom(thing):
    if thing == "bang":
        raise BANG
    raise BOOM


def validation_failure(thing):
    try:
        raise BANG
    except ZeroDivisionError:
        raise ValidationError("from checker", cause=BANG) from BANG


def returns_true(thing):
    return True


class ChildFormatChecker(FormatChecker):
    pass


class TestFormatChecker(TestCase):
    def test_it_can_validate_no_formats(self):
        checker = FormatChecker(formats=())
        self.assertFalse(checker.checkers)

    def test_it_raises_a_key_error_for_unknown_formats(self):
        with self.assertRaises(KeyError):
            FormatChecker(formats=["o noes"])

    def test_it_can_register_cls_checkers(self):
        original = dict(FormatChecker.checkers)
        self.addCleanup(FormatChecker.checkers.pop, "boom")
        with self.assertWarns(DeprecationWarning):
            FormatChecker.cls_checks("boom")(boom)
        self.assertEqual(
            FormatChecker.checkers,
            dict(original, boom=(boom, ())),
        )

    def test_it_can_register_checkers(self):
        checker = FormatChecker()
        checker.checks("boom")(boom)
        self.assertEqual(
            checker.checkers,
            dict(FormatChecker.checkers, boom=(boom, ())),
        )

    def test_it_catches_registered_errors(self):
        checker = FormatChecker()
        checker.checks("boom", raises=type(BOOM))(boom)

        with self.assertRaises(FormatError) as cm:
            checker.check(instance=12, format="boom")

        self.assertIs(cm.exception.cause, BOOM)
        self.assertIs(cm.exception.__cause__, BOOM)
        self.assertIs(cm.exception.__context__, BOOM)
        self.assertEqual(str(cm.exception), "12 is not a 'boom'")

        # Unregistered errors should not be caught
        with self.assertRaises(type(BANG)):
            checker.check(instance="bang", format="boom")

    def test_format_error_causes_become_validation_error_causes(self):
        checker = FormatChecker()
        checker.checks("boom", raises=ValueError)(boom)
        validator = Draft4Validator({"format": "boom"}, format_checker=checker)

        with self.assertRaises(ValidationError) as cm:
            validator.validate("BOOM")

        self.assertIs(cm.exception.cause, BOOM)
        self.assertIs(cm.exception.__cause__, BOOM)

    def test_format_checkers_come_with_defaults(self):
        # This is bad :/ but relied upon.
        # The docs for quite awhile recommended people do things like
        # validate(..., format_checker=FormatChecker())
        # We should change that, but we can't without deprecation...
        checker = FormatChecker()
        with self.assertRaises(FormatError):
            checker.check(instance="not-an-ipv4", format="ipv4")

    def test_repr(self):
        checker = FormatChecker(formats=())
        checker.checks("foo")(lambda thing: True)  # pragma: no cover
        checker.checks("bar")(lambda thing: True)  # pragma: no cover
        checker.checks("baz")(lambda thing: True)  # pragma: no cover
        self.assertEqual(
            repr(checker),
            "<FormatChecker checkers=['bar', 'baz', 'foo']>",
        )

    def test_registration_is_instance_scoped(self):
        first = FormatChecker(formats=())
        second = FormatChecker(formats=())

        first.register("first", lambda thing: True)
        second.register("second", lambda thing: True)

        self.assertEqual(set(first.checkers), {"first"})
        self.assertEqual(set(second.checkers), {"second"})
        self.assertNotIn("first", FormatChecker.checkers)

    def test_subclass_registration_does_not_change_format_checker(self):
        parent = FormatChecker(formats=())
        child = ChildFormatChecker(formats=())

        child.register("child-only", returns_true)

        self.assertNotIn("child-only", parent.checkers)
        self.assertNotIn("child-only", FormatChecker.checkers)

    def test_checkers_is_a_snapshot(self):
        checker = FormatChecker(formats=())
        checker.register("thing", returns_true)
        snapshot = checker.checkers
        checker.unregister("thing")

        self.assertIn("thing", snapshot)
        self.assertNotIn("thing", checker.checkers)

    def test_registration_overwrite_lifecycle(self):
        checker = FormatChecker(formats=())
        original = lambda thing: True
        replacement = lambda thing: False

        checker.register("thing", original)
        with self.assertRaises(ValueError):
            checker.register("thing", replacement, overwrite=False)

        checker.register("thing", replacement)
        self.assertIs(checker.checkers["thing"][0], replacement)

        checker.unregister("thing")
        with self.assertRaises(KeyError):
            checker.unregister("thing")
        checker.unregister("thing", missing_ok=True)
        checker.check("value", "thing")

    def test_registered_context_restores_previous_lifecycle(self):
        checker = FormatChecker(formats=())
        original = lambda thing: True
        temporary = lambda thing: False
        checker.register("thing", original)

        with checker.registered("thing", temporary):
            self.assertIs(checker.checkers["thing"][0], temporary)
        self.assertIs(checker.checkers["thing"][0], original)

        with checker.registered("other", temporary):
            self.assertIn("other", checker.checkers)
        self.assertNotIn("other", checker.checkers)

    def test_undeclared_exceptions_raise_by_default(self):
        checker = FormatChecker(formats=())
        checker.register("boom", boom)

        with self.assertRaises(ValueError) as cm:
            checker.check(12, "boom")

        self.assertIs(cm.exception, BOOM)

    def test_undeclared_exceptions_can_be_mapped_to_invalid(self):
        checker = FormatChecker(
            formats=(),
            exception_policy=ExceptionPolicy.INVALID,
        )
        checker.register("boom", boom)

        with self.assertRaises(FormatError) as cm:
            checker.check(12, "boom")

        self.assertIs(cm.exception.cause, BOOM)
        self.assertIs(cm.exception.__cause__, BOOM)
        self.assertIs(cm.exception.__context__, BOOM)

        validator = Draft4Validator(
            {"format": "boom"},
            format_checker=checker,
        )
        with self.assertRaises(ValidationError) as validation_error:
            validator.validate(12)
        self.assertIs(validation_error.exception.cause, BOOM)
        self.assertIs(validation_error.exception.__cause__, BOOM)
        self.assertIs(validation_error.exception.__context__, BOOM)

    def test_undeclared_exceptions_can_be_ignored(self):
        checker = FormatChecker(
            formats=(),
            exception_policy=ExceptionPolicy.IGNORE,
        )
        checker.register("boom", boom)

        checker.check(12, "boom")
        self.assertTrue(checker.conforms(12, "boom"))

    def test_exception_policy_can_be_a_callable(self):
        seen = []

        def policy(instance, format, error):
            seen.append((instance, format, error))
            if isinstance(error, ValueError):
                return ExceptionPolicy.INVALID
            return ExceptionPolicy.RAISE

        checker = FormatChecker(formats=(), exception_policy=policy)
        checker.register("boom", boom)

        with self.assertRaises(FormatError):
            checker.check(12, "boom")
        with self.assertRaises(ZeroDivisionError):
            checker.check("bang", "boom")
        self.assertEqual(seen[0], (12, "boom", BOOM))

    def test_per_format_policy_overrides_default_policy(self):
        checker = FormatChecker(
            formats=(),
            exception_policy=ExceptionPolicy.RAISE,
        )
        checker.register(
            "boom",
            boom,
            exception_policy=ExceptionPolicy.INVALID,
        )

        with self.assertRaises(FormatError):
            checker.check(12, "boom")

    def test_validation_error_exception_is_reported_as_validation_error(self):
        checker = FormatChecker(formats=())
        checker.register(
            "thing",
            validation_failure,
            exception_policy=ExceptionPolicy.INVALID,
        )
        validator = Draft4Validator(
            {"format": "thing"},
            format_checker=checker,
        )

        with self.assertRaises(ValidationError) as cm:
            validator.validate("value")

        self.assertEqual(cm.exception.message, "from checker")
        self.assertIs(cm.exception.cause, BANG)
        self.assertIs(cm.exception.__cause__, BANG)
        self.assertIsInstance(cm.exception.__context__, ZeroDivisionError)
        with self.assertRaises(ValidationError):
            checker.check("value", "thing")

    def test_false_result_is_always_an_invalid_format(self):
        checker = FormatChecker(formats=())
        checker.register("thing", lambda thing: False)

        with self.assertRaises(FormatError):
            checker.check("value", "thing")

    def test_unknown_format_remains_ignored(self):
        checker = FormatChecker(formats=())
        validator = Draft4Validator(
            {"format": "unknown"},
            format_checker=checker,
        )

        validator.validate(object())

    def test_copy_and_unpickling_do_not_share_registries(self):
        checker = FormatChecker(formats=())
        checker.register("thing", returns_true)

        shallow = copy.copy(checker)
        deep = copy.deepcopy(checker)
        unpickled = pickle.loads(pickle.dumps(checker))

        checker.register("shared-risk", returns_true)
        for duplicate in (shallow, deep, unpickled):
            self.assertIn("thing", duplicate.checkers)
            self.assertNotIn("shared-risk", duplicate.checkers)

        unpickled.register("unpickled", returns_true)
        self.assertNotIn("unpickled", checker.checkers)

    def test_created_validator_owns_its_format_checker(self):
        from jsonschema.validators import create

        checker = FormatChecker(formats=())
        created = create(
            meta_schema=Draft202012Validator.META_SCHEMA,
            format_checker=checker,
        )

        self.assertIsNot(created.FORMAT_CHECKER, checker)
        checker.register("thing", returns_true)
        self.assertNotIn("thing", created.FORMAT_CHECKER.checkers)

    def test_created_validator_owns_its_mutable_class_mappings(self):
        from jsonschema.validators import create

        meta_schema = dict(Draft202012Validator.META_SCHEMA)
        keyword_validators = dict(Draft202012Validator.VALIDATORS)
        created = create(
            meta_schema=meta_schema,
            validators=keyword_validators,
            format_checker=FormatChecker(formats=()),
        )

        created.META_SCHEMA["mutated"] = True
        created.VALIDATORS["mutated"] = lambda *args: ()

        self.assertNotIn("mutated", meta_schema)
        self.assertNotIn("mutated", keyword_validators)
        self.assertNotIn("mutated", Draft202012Validator.META_SCHEMA)
        self.assertNotIn("mutated", Draft202012Validator.VALIDATORS)

    def test_validator_copy_owns_its_format_checker(self):
        checker = FormatChecker(formats=())
        checker.register("thing", returns_true)
        validator = Draft4Validator(
            {"format": "thing"},
            format_checker=checker,
        )

        shallow = copy.copy(validator)
        deep = copy.deepcopy(validator)
        unpickled = pickle.loads(pickle.dumps(checker))

        checker.register("shared-risk", returns_true)
        self.assertIn("thing", shallow.format_checker.checkers)
        self.assertIn("thing", deep.format_checker.checkers)
        self.assertIn("thing", unpickled.checkers)
        self.assertNotIn("shared-risk", shallow.format_checker.checkers)
        self.assertNotIn("shared-risk", deep.format_checker.checkers)
        self.assertNotIn("shared-risk", unpickled.checkers)

    def test_check_schema_uses_owned_format_checker(self):
        checker = Draft4Validator.FORMAT_CHECKER
        before = set(checker.checkers)

        checker.register("temporary-schema-format", returns_true)
        try:
            Draft4Validator.check_schema({"format": "unknown"})
        finally:
            checker.unregister("temporary-schema-format")

        self.assertEqual(set(checker.checkers), before)
        self.assertNotIn("temporary-schema-format", before)

    def test_concurrent_registration_and_validation_is_isolated(self):
        barrier = Barrier(2)
        disaster_inputs = (12, "bang", dict(), list(), object(), "x" * 10_000)

        def exercise(checker, own_name, other_name):
            checker.register(
                own_name,
                boom,
                exception_policy=ExceptionPolicy.INVALID,
            )
            own_validator = Draft4Validator(
                dict(format=own_name),
                format_checker=checker,
            )
            unknown_validator = Draft4Validator(
                dict(format="unknown"),
                format_checker=checker,
            )
            barrier.wait()
            for _ in range(250):
                checker.register(
                    own_name,
                    boom,
                    exception_policy=ExceptionPolicy.INVALID,
                )
                self.assertIn(own_name, checker.checkers)
                self.assertNotIn(other_name, checker.checkers)
                for disaster_input in disaster_inputs:
                    self.assertFalse(
                        own_validator.is_valid(disaster_input),
                    )
                    self.assertTrue(
                        unknown_validator.is_valid(disaster_input),
                    )
                checker.unregister(own_name)
                self.assertNotIn(own_name, checker.checkers)
                self.assertNotIn(other_name, checker.checkers)
                checker.register(
                    own_name,
                    boom,
                    exception_policy=ExceptionPolicy.INVALID,
                )
            return set(checker.checkers)

        first = FormatChecker(formats=())
        second = FormatChecker(formats=())

        with ThreadPoolExecutor(2) as executor:
            futures = [
                executor.submit(exercise, first, "first", "second"),
                executor.submit(exercise, second, "second", "first"),
            ]
            results = [future.result() for future in futures]

        self.assertEqual(results, [set(["first"]), set(["second"])])
        self.assertEqual(set(first.checkers), set(["first"]))
        self.assertEqual(set(second.checkers), set(["second"]))
