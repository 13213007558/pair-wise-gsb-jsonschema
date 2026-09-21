"""
Tests for the parts of jsonschema related to the :kw:`format` keyword.
"""

from concurrent.futures import ThreadPoolExecutor
from unittest import TestCase
import copy
import pickle
import threading

from jsonschema import FormatChecker, FormatExceptionPolicy, ValidationError
from jsonschema.exceptions import FormatError
from jsonschema.validators import Draft4Validator

BOOM = ValueError("Boom!")
BANG = ZeroDivisionError("Bang!")


def boom(thing):
    if thing == "bang":
        raise BANG
    raise BOOM


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


class TestRegistrationLifecycle(TestCase):
    def test_register_directly_and_as_decorator(self):
        checker = FormatChecker(formats=())
        checker.register("boom", boom, raises=ValueError)

        @checker.register("bang")
        def bang(thing):
            return True

        self.assertEqual(
            checker.checkers,
            {"boom": (boom, ValueError), "bang": (bang, ())},
        )

    def test_reregistering_a_format_overrides_it(self):
        checker = FormatChecker(formats=())
        checker.checks("boom")(boom)
        checker.checks("boom", raises=ValueError)(str)
        self.assertEqual(checker.checkers, {"boom": (str, ValueError)})

    def test_unregister_removes_and_returns_the_checker(self):
        checker = FormatChecker(formats=())
        checker.checks("boom")(boom)
        self.assertIs(checker.unregister("boom"), boom)
        self.assertEqual(checker.checkers, {})
        # Unregistered formats are simply not checked.
        checker.check(instance=12, format="boom")

    def test_unregistering_an_unknown_format_raises_key_error(self):
        checker = FormatChecker(formats=())
        with self.assertRaises(KeyError):
            checker.unregister("boom")

    def test_registration_is_bounded_to_the_instance(self):
        one, two = FormatChecker(formats=()), FormatChecker(formats=())

        # Interleaved registration, overriding, and unregistration.
        one.checks("a")(boom)
        two.checks("a")(str)
        one.checks("b")(boom)
        two.unregister("a")
        one.unregister("a")

        self.assertEqual(set(one.checkers), {"b"})
        self.assertEqual(set(two.checkers), set())
        # The class-level defaults were not polluted.
        self.assertNotIn("a", FormatChecker.checkers)
        self.assertNotIn("b", FormatChecker.checkers)

    def test_checker_failures_do_not_change_other_instances(self):
        class ExplodingLibraryError(Exception):
            pass

        def explode(thing):
            raise ExplodingLibraryError("bug in the external library")

        policy = FormatExceptionPolicy(on_unexpected="error")
        fragile = FormatChecker(formats=(), exception_policy=policy)
        fragile.checks("explosive")(explode)
        stable = FormatChecker()
        before = stable.snapshot()

        with self.assertRaises(FormatError):
            fragile.check(instance=object(), format="explosive")

        # Neither the other instance nor the fragile registry changed.
        self.assertEqual(stable.snapshot(), before)
        self.assertEqual(set(fragile.checkers), {"explosive"})


class TestFormatExceptionPolicy(TestCase):
    def test_invalid_actions_are_rejected(self):
        with self.assertRaises(ValueError):
            FormatExceptionPolicy(on_unexpected="explode")
        with self.assertRaises(ValueError):
            FormatExceptionPolicy(on_declared="explode")
        with self.assertRaises(ValueError):
            FormatExceptionPolicy(on_false="explode")

    def test_on_false_cannot_raise(self):
        with self.assertRaises(ValueError):
            FormatExceptionPolicy(on_false="raise")

    def test_falsy_results_can_be_ignored(self):
        policy = FormatExceptionPolicy(on_false="ignore")
        checker = FormatChecker(formats=(), exception_policy=policy)
        checker.checks("never")(lambda thing: False)
        checker.check(instance="anything", format="never")
        self.assertTrue(checker.conforms("anything", "never"))

    def test_declared_exceptions_can_propagate(self):
        policy = FormatExceptionPolicy(on_declared="raise")
        checker = FormatChecker(formats=(), exception_policy=policy)
        checker.checks("boom", raises=type(BOOM))(boom)
        with self.assertRaises(type(BOOM)):
            checker.check(instance=12, format="boom")

    def test_declared_exceptions_can_be_ignored(self):
        policy = FormatExceptionPolicy(on_declared="ignore")
        checker = FormatChecker(formats=(), exception_policy=policy)
        checker.checks("boom", raises=type(BOOM))(boom)
        checker.check(instance=12, format="boom")
        self.assertTrue(checker.conforms(12, "boom"))

    def test_unexpected_exceptions_can_become_format_errors(self):
        policy = FormatExceptionPolicy(on_unexpected="error")
        checker = FormatChecker(formats=(), exception_policy=policy)
        checker.checks("boom")(boom)  # nothing declared, BOOM is unexpected

        with self.assertRaises(FormatError) as cm:
            checker.check(instance=12, format="boom")

        self.assertIs(cm.exception.cause, BOOM)
        self.assertIs(cm.exception.__cause__, BOOM)
        self.assertIsNotNone(cm.exception.__context__)
        self.assertEqual(str(cm.exception), "12 is not a 'boom'")

    def test_unexpected_exceptions_can_be_ignored(self):
        policy = FormatExceptionPolicy(on_unexpected="ignore")
        checker = FormatChecker(formats=(), exception_policy=policy)
        checker.checks("boom")(boom)
        checker.check(instance=12, format="boom")
        self.assertTrue(checker.conforms(12, "boom"))

    def test_unexpected_exceptions_propagate_by_default(self):
        checker = FormatChecker(formats=())
        checker.checks("boom")(boom)
        with self.assertRaises(type(BOOM)):
            checker.check(instance=12, format="boom")

    def test_checker_raising_validation_error_preserves_context(self):
        cause = ValidationError("inner", context=[ValidationError("sub")])

        def raises_validation_error(thing):
            raise cause

        checker = FormatChecker(formats=())
        checker.checks("ve", raises=ValidationError)(raises_validation_error)

        with self.assertRaises(FormatError) as cm:
            checker.check(instance="x", format="ve")

        self.assertIs(cm.exception.cause, cause)
        self.assertEqual(len(cm.exception.cause.context), 1)

    def test_undeclared_validation_error_uses_the_unexpected_policy(self):
        policy = FormatExceptionPolicy(on_unexpected="error")
        checker = FormatChecker(formats=(), exception_policy=policy)
        checker.checks("ve")(lambda thing: (_ for _ in ()).throw(
            ValidationError("nope"),
        ))
        with self.assertRaises(FormatError) as cm:
            checker.check(instance="x", format="ve")
        self.assertIsInstance(cm.exception.cause, ValidationError)

    def test_policy_applies_through_the_validator(self):
        policy = FormatExceptionPolicy(on_unexpected="error")
        checker = FormatChecker(formats=(), exception_policy=policy)
        checker.checks("boom")(boom)
        validator = Draft4Validator({"format": "boom"}, format_checker=checker)

        with self.assertRaises(ValidationError) as cm:
            validator.validate(12)

        self.assertIs(cm.exception.cause, BOOM)
        self.assertIs(cm.exception.__cause__, BOOM)


class TestFormatCheckerSnapshots(TestCase):
    def test_snapshot_is_immutable_and_point_in_time(self):
        checker = FormatChecker(formats=())
        checker.checks("boom")(boom)

        snapshot = checker.snapshot()
        self.assertEqual(set(snapshot), {"boom"})
        with self.assertRaises(TypeError):
            snapshot["other"] = (boom, ())

        checker.unregister("boom")
        self.assertIn("boom", snapshot)
        self.assertNotIn("boom", checker.snapshot())

    def test_check_uses_a_consistent_view_of_the_registry(self):
        checker = FormatChecker(formats=())
        checker.checks("boom")(boom)
        checker.unregister("boom")
        # No KeyError from a read-modified registry; the format is unknown.
        checker.check(instance=12, format="boom")


class TestFormatCheckerCopying(TestCase):
    def test_copy_has_an_independent_registry(self):
        checker = FormatChecker(formats=())
        checker.checks("boom")(boom)

        for clone in (copy.copy(checker), copy.deepcopy(checker)):
            self.assertEqual(clone.checkers, checker.checkers)
            self.assertIsNot(clone.checkers, checker.checkers)
            clone.unregister("boom")
            self.assertIn("boom", checker.checkers)

    def test_pickle_roundtrip_has_an_independent_registry(self):
        policy = FormatExceptionPolicy(on_unexpected="error")
        checker = FormatChecker(formats=(), exception_policy=policy)
        checker.checks("boom", raises=ValueError)(boom)

        clone = pickle.loads(pickle.dumps(checker))
        self.assertEqual(clone.checkers, checker.checkers)
        self.assertIsNot(clone.checkers, checker.checkers)
        self.assertEqual(clone.exception_policy, policy)

        clone.unregister("boom")
        self.assertIn("boom", checker.checkers)

        with self.assertRaises(FormatError) as cm:
            checker.check(instance=12, format="boom")
        self.assertIs(cm.exception.cause, BOOM)


class TestFormatCheckerConcurrency(TestCase):
    def test_concurrent_validation_and_registration(self):
        checker = FormatChecker(formats=())
        checker.checks("even")(lambda thing: thing % 2 == 0)
        validator = Draft4Validator({"format": "even"}, format_checker=checker)
        other = FormatChecker(formats=())
        failures = []

        def validate_repeatedly():
            try:
                for _ in range(500):
                    validator.validate(4)
                    assert not validator.is_valid(3)
                    Draft4Validator.check_schema({"format": "even"})
            except Exception as error:  # pragma: no cover
                failures.append(error)

        def register_and_unregister(prefix):
            try:
                for i in range(500):
                    name = f"{prefix}-{i % 8}"
                    checker.register(name, lambda thing: True)
                    checker.unregister(name)
                    other.checks(prefix)(lambda thing: True)
                    other.unregister(prefix)
            except Exception as error:  # pragma: no cover
                failures.append(error)

        with ThreadPoolExecutor(max_workers=8) as executor:
            for _ in range(4):
                executor.submit(validate_repeatedly)
            for worker in range(4):
                executor.submit(register_and_unregister, f"extra{worker}")

        self.assertEqual(failures, [])
        # The churn did not leak formats into either registry...
        self.assertEqual(set(checker.checkers), {"even"})
        self.assertEqual(set(other.checkers), set())
        # ... nor into the validator class's own checker.
        self.assertFalse(
            any(
                name.startswith("extra")
                for name in Draft4Validator.FORMAT_CHECKER.checkers
            ),
        )

    def test_validators_do_not_share_mutable_registration_state(self):
        fresh = FormatChecker()
        fresh.checks("private")(boom)

        self.assertNotIn("private", Draft4Validator.FORMAT_CHECKER.checkers)
        self.assertNotIn("private", FormatChecker.checkers)

        validator = Draft4Validator(
            {"format": "private"}, format_checker=fresh,
        )
        clone = copy.copy(validator)
        fresh.unregister("private")
        # Both validators see the unregistration consistently, without
        # affecting the class-level checker of the validator itself.
        self.assertNotIn("private", Draft4Validator.FORMAT_CHECKER.checkers)
        self.assertIs(clone.format_checker, validator.format_checker)
