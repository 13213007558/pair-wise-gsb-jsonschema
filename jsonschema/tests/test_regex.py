"""
Tests for the optional, bounded regex execution interface.
"""

from __future__ import annotations

from collections import deque
from unittest import TestCase
import multiprocessing
import re
import signal
import threading
import time

from jsonschema import (
    BoundedRegexExecutor,
    Draft202012Validator,
    Draft7Validator,
    RegexBudget,
    RegexError,
    RegexExecutor,
    RegexExecutorError,
    RegexSyntaxError,
    RegexTimeout,
)


class FaultInjectingExecutor(RegexExecutor):
    """
    A deterministic executor for fault-injection tests without processes.
    """

    def __init__(self, failure):
        self._failure = failure
        self.calls = 0
        self.closed = False

    def search(self, pattern, string):
        self.calls += 1
        if self._failure is not None:
            raise self._failure
        return bool(re.search(pattern, string))

    def close(self):
        self.closed = True


def validate(validator, instance):
    return list(validator.iter_errors(instance))


class RegexInterfaceTests(TestCase):
    def test_executor_is_optional_and_default_uses_re(self):
        validator = Draft202012Validator({"pattern": "^a"})
        self.assertIsNone(validator.regex_executor)
        self.assertTrue(validator.is_valid("abc"))
        self.assertFalse(validator.is_valid("xbc"))

    def test_evolve_preserves_the_executor(self):
        executor = FaultInjectingExecutor(None)
        validator = Draft202012Validator(
            {"pattern": "^a"},
            regex_executor=executor,
        )
        evolved = validator.evolve(schema={"pattern": "^b"})
        self.assertIs(evolved.regex_executor, executor)

    def test_timeout_is_not_treated_as_non_matching(self):
        executor = FaultInjectingExecutor(RegexTimeout("call timeout"))
        validator = Draft202012Validator(
            {"pattern": "^a"},
            regex_executor=executor,
        )
        with self.assertRaises(RegexTimeout):
            validate(validator, "anything")
        self.assertEqual(executor.calls, 1)

    def test_executor_failure_is_distinguishable_from_timeout(self):
        executor = FaultInjectingExecutor(
            RegexExecutorError("a regex worker process exited unexpectedly"),
        )
        validator = Draft202012Validator(
            {"pattern": "^a"},
            regex_executor=executor,
        )
        with self.assertRaises(RegexExecutorError):
            validate(validator, "anything")

    def test_syntax_error_is_distinguishable(self):
        executor = FaultInjectingExecutor(
            RegexSyntaxError("invalid regular expression syntax"),
        )
        validator = Draft202012Validator(
            {"pattern": "^a"},
            regex_executor=executor,
        )
        with self.assertRaises(RegexSyntaxError):
            validate(validator, "anything")

    def test_failure_types_are_separable(self):
        timeout = RegexTimeout("call timeout")
        backend = RegexExecutorError("backend")
        syntax = RegexSyntaxError("syntax")
        for error in (timeout, backend, syntax):
            self.assertIsInstance(error, RegexError)
        self.assertNotIsInstance(
            timeout, (RegexExecutorError, RegexSyntaxError),
        )
        self.assertNotIsInstance(backend, (RegexTimeout, RegexSyntaxError))
        self.assertNotIsInstance(syntax, (RegexTimeout, RegexExecutorError))

    def test_pattern_properties_timeout_keeps_rejection_boundary(self):
        executor = FaultInjectingExecutor(RegexTimeout("call timeout"))
        schema = {
            "patternProperties": {"^x": {}},
            "additionalProperties": False,
        }
        validator = Draft202012Validator(schema, regex_executor=executor)
        with self.assertRaises(RegexTimeout):
            validate(validator, {"x-secret": 1, "other": 2})

    def test_additional_properties_still_rejected_after_matching_pattern(self):
        executor = FaultInjectingExecutor(None)
        schema = {
            "patternProperties": {"^x": {}},
            "additionalProperties": False,
        }
        validator = Draft202012Validator(schema, regex_executor=executor)
        errors = validate(validator, {"x-ok": 1, "nope": 2})
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].path, deque([]))
        self.assertIn("nope", errors[0].message)

    def test_unevaluated_properties_boundary_survives_timeout(self):
        executor = FaultInjectingExecutor(RegexTimeout("call timeout"))
        schema = {
            "allOf": [{"patternProperties": {"^x": {}}}],
            "unevaluatedProperties": False,
        }
        validator = Draft202012Validator(schema, regex_executor=executor)
        with self.assertRaises(RegexTimeout):
            validate(validator, {"x-secret": 1})

    def test_error_message_contains_location_only(self):
        executor = FaultInjectingExecutor(RegexTimeout("call timeout"))
        schema = {
            "properties": {
                "outer": {
                    "properties": {
                        "inner": {"pattern": "^a"},
                    },
                },
            },
        }
        validator = Draft202012Validator(schema, regex_executor=executor)
        instance = {"outer": {"inner": "sensitive-value-1234"}}
        with self.assertRaises(RegexError) as caught:
            validate(validator, instance)
        rendered = str(caught.exception)
        self.assertIn("pattern", rendered)
        self.assertIn("outer", rendered)
        self.assertIn("inner", rendered)
        self.assertNotIn("sensitive-value-1234", rendered)
        self.assertNotIn("^a", rendered)
        self.assertEqual(caught.exception.path, ("outer", "inner"))
        self.assertEqual(caught.exception.keyword, "pattern")


class BoundedRegexExecutorTests(TestCase):
    def test_matches_and_non_matches(self):
        executor = BoundedRegexExecutor(
            RegexBudget(call_timeout=5, total_timeout=10),
        )
        try:
            validator = Draft202012Validator(
                {"pattern": "^a"},
                regex_executor=executor,
            )
            self.assertTrue(validator.is_valid("abc"))
            self.assertFalse(validator.is_valid("xbc"))
        finally:
            executor.close()

    def test_real_syntax_error_raises_regex_syntax_error(self):
        executor = BoundedRegexExecutor(
            RegexBudget(call_timeout=5, total_timeout=10),
        )
        try:
            validator = Draft202012Validator(
                {"pattern": "("},
                regex_executor=executor,
            )
            with self.assertRaises(RegexSyntaxError) as caught:
                validate(validator, "abc")
            self.assertIsInstance(caught.exception.__cause__, re.error)
        finally:
            executor.close()

    def test_call_quota_is_enforced_per_validation(self):
        executor = BoundedRegexExecutor(
            RegexBudget(call_timeout=5, total_timeout=10, max_calls=2),
        )
        try:
            schema = {"patternProperties": {"^a": {}, "^b": {}, "^c": {}}}
            validator = Draft202012Validator(schema, regex_executor=executor)
            with self.assertRaises(RegexTimeout) as caught:
                validate(validator, {"abc": 1})
            self.assertEqual(caught.exception.reason, "call quota exceeded")
        finally:
            executor.close()

    def test_budget_resets_between_validation_runs(self):
        executor = BoundedRegexExecutor(
            RegexBudget(call_timeout=5, total_timeout=10, max_calls=1),
        )
        try:
            validator = Draft202012Validator(
                {"pattern": "^a"},
                regex_executor=executor,
            )
            self.assertTrue(validator.is_valid("abc"))
            self.assertTrue(validator.is_valid("abc"))
        finally:
            executor.close()

    def test_total_timeout_budget_is_enforced(self):
        executor = BoundedRegexExecutor(
            RegexBudget(call_timeout=5, total_timeout=0.01),
        )
        try:
            validator = Draft202012Validator(
                {"pattern": "^a"},
                regex_executor=executor,
            )
            time.sleep(0.03)
            with self.assertRaises(RegexTimeout) as caught:
                validate(validator, "abc")
            self.assertEqual(caught.exception.reason, "total timeout")
        finally:
            executor.close()

    def test_invalid_budget_arguments_are_rejected(self):
        with self.assertRaises(ValueError):
            RegexBudget(call_timeout=0, total_timeout=10)
        with self.assertRaises(ValueError):
            RegexBudget(call_timeout=1, total_timeout=0)
        with self.assertRaises(ValueError):
            RegexBudget(call_timeout=1, total_timeout=1, max_calls=-1)
        with self.assertRaises(ValueError):
            BoundedRegexExecutor(
                RegexBudget(call_timeout=1, total_timeout=1),
                max_workers=0,
            )

    def test_close_is_idempotent(self):
        executor = BoundedRegexExecutor(
            RegexBudget(call_timeout=5, total_timeout=10),
        )
        executor.close()
        executor.close()

    def test_search_after_close_fails_cleanly(self):
        executor = BoundedRegexExecutor(
            RegexBudget(call_timeout=5, total_timeout=10),
        )
        executor.close()
        with self.assertRaises(RegexExecutorError):
            executor.search("^a", "abc")

    def test_concurrency_is_capped_at_max_workers(self):
        context = multiprocessing.get_context("spawn")
        max_workers = 2
        executor = BoundedRegexExecutor(
            RegexBudget(call_timeout=5, total_timeout=30),
            max_workers=max_workers,
            context=context,
        )
        observed = []
        barrier = threading.Barrier(6)
        lock = threading.Lock()
        # A few milliseconds of real backtracking so workers stay busy
        # long enough for the other threads to observe the cap.
        slow_input = "a" * 20 + "!"

        def worker():
            barrier.wait()
            with lock:
                observed.append(executor._pool.in_flight)
            executor.search(r"(a+)+$", slow_input)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
            self.assertTrue(all(not t.is_alive() for t in threads))
            self.assertLessEqual(max(observed), max_workers)
            self.assertGreaterEqual(min(observed), 0)
        finally:
            executor.close()
        self.assertEqual(multiprocessing.active_children(), [])


class CatastrophicBacktrackingTests(TestCase):
    """
    A real catastrophic-backtracking regex, with a hard signal-based
    backstop so a regression that loses the process boundary fails the
    test instead of hanging the whole suite.
    """

    HARD_TIMEOUT_SECONDS = 20

    def _run_with_hard_timeout(self, fn):
        def handler(signum, frame):
            raise AssertionError(
                "validation did not return within the hard timeout; "
                "a worker process was likely left running unstoppably",
            )

        previous = signal.signal(signal.SIGALRM, handler)
        signal.alarm(self.HARD_TIMEOUT_SECONDS)
        try:
            return fn()
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)

    def test_catastrophic_pattern_times_out_and_leaves_no_workers(self):
        context = multiprocessing.get_context("spawn")
        executor = BoundedRegexExecutor(
            RegexBudget(call_timeout=0.5, total_timeout=10),
            max_workers=2,
            context=context,
        )
        pattern = r"(a+)+$"
        instance = "a" * 28 + "!"

        def exercise():
            try:
                validator = Draft7Validator(
                    {"pattern": pattern},
                    regex_executor=executor,
                )
                started = time.monotonic()
                with self.assertRaises(RegexTimeout) as caught:
                    validate(validator, instance)
                elapsed = time.monotonic() - started
                self.assertLess(elapsed, 5.0)
                self.assertEqual(caught.exception.reason, "call timeout")
                self.assertNotIn(instance, str(caught.exception))
                self.assertNotIn(pattern, str(caught.exception))
            finally:
                executor.close()

        self._run_with_hard_timeout(exercise)
        self.assertEqual(multiprocessing.active_children(), [])

    def test_worker_is_replaced_after_a_timeout(self):
        context = multiprocessing.get_context("spawn")
        executor = BoundedRegexExecutor(
            RegexBudget(call_timeout=0.3, total_timeout=10),
            max_workers=1,
            context=context,
        )
        try:
            slow = Draft7Validator(
                {"pattern": r"(a+)+$"},
                regex_executor=executor,
            )
            with self.assertRaises(RegexTimeout):
                validate(slow, "a" * 28 + "!")

            fast = Draft7Validator(
                {"pattern": "^a"},
                regex_executor=executor,
            )
            self.assertTrue(fast.is_valid("abc"))
            self.assertFalse(fast.is_valid("xbc"))
        finally:
            executor.close()
        self.assertEqual(multiprocessing.active_children(), [])

    def test_cancellation_via_generator_close_reaps_workers(self):
        context = multiprocessing.get_context("spawn")
        executor = BoundedRegexExecutor(
            RegexBudget(call_timeout=0.3, total_timeout=10),
            context=context,
        )
        try:
            validator = Draft7Validator(
                {"pattern": r"(a+)+$"},
                regex_executor=executor,
            )
            generator = validator.iter_errors("a" * 28 + "!")
            with self.assertRaises(RegexTimeout):
                next(generator)
        finally:
            executor.close()
        self.assertEqual(multiprocessing.active_children(), [])
