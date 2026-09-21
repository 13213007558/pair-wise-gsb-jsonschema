"""
Instance-level custom format checkers, with no global state involved.

This example shows how a backend can register its own `uuid`, `ulid`,
and `time` formats on a private `FormatChecker` instance, how an
exception policy keeps a fragile third-party checker from crashing the
validator, and how the register / override / unregister lifecycle stays
bounded to that one instance.

Nothing here mutates `FormatChecker.checkers`, the draft-specific
default checkers, or any other `FormatChecker` instance, so this code
is safe to run concurrently with unrelated validation elsewhere in the
process.

Run it directly:

.. code:: sh

    python docs/examples/custom_format_checkers.py
"""

from datetime import time
from uuid import UUID
import re

from jsonschema import (
    Draft202012Validator,
    FormatChecker,
    FormatExceptionPolicy,
    ValidationError,
)

# A ULID is 26 characters of Crockford base32; the first character is
# limited to 0-7 because the timestamp only fills 48 of the 130 bits.
_ULID = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}", re.IGNORECASE)

# A private checker instance with an explicit exception policy:
#
# * a checker returning False fails validation ("error" by default),
# * a checker raising one of its declared raises-exceptions fails
#   validation with the exception preserved as the error's cause,
# * a checker raising anything *else* -- e.g. a bug in an external
#   library it wraps -- is converted into a validation failure too,
#   instead of propagating and crashing the validator.
checker = FormatChecker(
    formats=(),  # start empty: exactly the formats below, nothing else
    exception_policy=FormatExceptionPolicy(on_unexpected="error"),
)


@checker.register("uuid", raises=ValueError)
def is_uuid(instance: object) -> bool:
    if not isinstance(instance, str):
        return True
    UUID(instance)
    return True


@checker.register("ulid")
def is_ulid(instance: object) -> bool:
    if not isinstance(instance, str):
        return True
    return bool(_ULID.fullmatch(instance))


@checker.register("time", raises=ValueError)
def is_time(instance: object) -> bool:
    if not isinstance(instance, str):
        return True
    time.fromisoformat(instance.replace("Z", "+00:00"))
    return True


def _fragile_external_library(hostname: str) -> bool:
    """
    A stand-in for a third-party library with a crashy edge case.

    Real-world examples are libraries which raise their own exception
    hierarchy (or plain RuntimeError) on inputs nobody anticipated.
    """
    if hostname == "crash":
        raise RuntimeError("unexpected bug in the external library")
    return "." in hostname


@checker.register("hostname")
def is_hostname(instance: object) -> bool:
    if not isinstance(instance, str):
        return True
    return _fragile_external_library(instance)


SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"format": "uuid"},
        "event": {"format": "ulid"},
        "at": {"format": "time"},
        "host": {"format": "hostname"},
    },
}

validator = Draft202012Validator(SCHEMA, format_checker=checker)


if __name__ == "__main__":
    # A fully valid document.
    validator.validate(
        {
            "id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
            "event": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "at": "13:37:00Z",
            "host": "example.com",
        },
    )

    # A checker returning False becomes a normal validation error.
    try:
        validator.validate({"event": "not-a-ulid"})
    except ValidationError as error:
        print(f"invalid ULID       -> {error.message}")

    # The fragile checker's unexpected RuntimeError becomes a validation
    # error too (per the policy), with the original exception preserved
    # as the cause -- the validator does not crash.
    try:
        validator.validate({"host": "crash"})
    except ValidationError as error:
        print(f"library bug        -> {error.message} (cause: {error.cause})")

    # Lifecycle: overriding replaces a checker on this instance only...
    checker.register("uuid", lambda instance: True)
    assert validator.is_valid({"id": "anything at all"})

    # ... and unregistering removes it, again on this instance only.
    checker.unregister("uuid")
    assert "uuid" not in checker.snapshot()

    # None of the above touched the process-wide defaults.
    assert "ulid" not in FormatChecker.checkers
    assert "ulid" not in Draft202012Validator.FORMAT_CHECKER.checkers
    print("defaults untouched -> ulid not in the class-level registry")
