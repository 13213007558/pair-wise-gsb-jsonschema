.. currentmodule:: jsonschema

.. _regex-execution:

============================
Bounded Regular Expressions
============================

The ``pattern`` and ``patternProperties`` keywords run regular
expressions whose syntax and matching behavior are controlled by the
schema author. When schemas come from an external team, a pattern can
trigger catastrophic backtracking. Python's :mod:`re` module cannot be
interrupted once a search starts, so running it on a request thread can
block that thread indefinitely. Moving the search to another thread with
a timer does not help: the worker thread keeps running unstoppably.

jsonschema therefore defines an optional execution interface for the
regular expressions used by these keywords. Without an executor,
validation keeps its original semantics and calls :func:`re.search`
directly.

.. autoclass:: RegexExecutor
    :members:

.. autoclass:: RegexBudget
    :members:

A validator is given an executor via the keyword-only
``regex_executor`` argument. The same executor instance is shared across
every subschema the validator descends into::

    from jsonschema import (
        BoundedRegexExecutor,
        Draft202012Validator,
        RegexBudget,
    )

    executor = BoundedRegexExecutor(
        RegexBudget(call_timeout=0.25, total_timeout=5, max_calls=200),
    )
    validator = Draft202012Validator(schema, regex_executor=executor)
    try:
        validator.validate(instance)
    finally:
        executor.close()

The executor can also be used as a context manager so its worker
processes are always reclaimed::

    with BoundedRegexExecutor(
        RegexBudget(call_timeout=0.25, total_timeout=5),
    ) as executor:
        Draft202012Validator(schema, regex_executor=executor).validate(
            instance,
        )

Restricted execution
--------------------

`BoundedRegexExecutor` is a standard-library-only example of a
restricted executor. It uses no third-party regular-expression
dependency. Each search runs in an isolated worker process:

* ``call_timeout`` bounds a single ``re.search``. A stuck worker is
  terminated and replaced, so the bound is hard rather than advisory.
* ``total_timeout`` bounds the cumulative regex wall-clock time of one
  whole validation run. The budget resets for each top-level
  ``iter_errors`` call and never leaks between documents.
* ``max_calls`` bounds the number of searches in one validation run.
* ``max_workers`` caps how many worker processes run concurrently.

Failures are reported with distinct exception types and always abort
validation. They are never collapsed into a "does not match" result and
never cause a keyword to be skipped, so the rejection boundary around
``patternProperties`` and the subsequent ``additionalProperties`` /
``unevaluatedProperties`` keywords is preserved:

.. autoclass:: RegexError
    :members:

.. autoclass:: RegexTimeout
    :members:

.. autoclass:: RegexExecutorError
    :members:

.. autoclass:: RegexSyntaxError
    :members:

Error messages intentionally contain no pattern text and no instance
value. Use the ``keyword`` and ``path`` attributes to locate the
offending schema keyword within the validated document.

.. autoclass:: BoundedRegexExecutor
    :members:
