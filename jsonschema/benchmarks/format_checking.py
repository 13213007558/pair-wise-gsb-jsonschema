"""
Instance-level format checking should not add meaningful overhead.

Compares validating a document whose schema uses the :kw:`format`
keyword:

* without a format checker (formats ignored, per the specification),
* with the draft's default format checker,
* with a private `FormatChecker` instance carrying custom
  `uuid`/`ulid`/`time` checkers registered on the instance only
  (see `docs/examples/custom_format_checkers.py`).
"""

from datetime import time
from uuid import UUID
import re

from pyperf import Runner

from jsonschema import Draft202012Validator, FormatChecker

_ULID = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}", re.IGNORECASE)

SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"format": "uuid"},
        "event": {"format": "ulid"},
        "at": {"format": "time"},
    },
}
INSTANCE = {
    "id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
    "event": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
    "at": "13:37:00Z",
}

custom = FormatChecker(formats=())


@custom.register("uuid", raises=ValueError)
def is_uuid(instance):
    if not isinstance(instance, str):
        return True
    UUID(instance)
    return True


@custom.register("ulid")
def is_ulid(instance):
    if not isinstance(instance, str):
        return True
    return bool(_ULID.fullmatch(instance))


@custom.register("time", raises=ValueError)
def is_time(instance):
    if not isinstance(instance, str):
        return True
    time.fromisoformat(instance.replace("Z", "+00:00"))
    return True


no_checker = Draft202012Validator(SCHEMA)
default_checker = Draft202012Validator(
    SCHEMA, format_checker=Draft202012Validator.FORMAT_CHECKER,
)
custom_checker = Draft202012Validator(SCHEMA, format_checker=custom)

if __name__ == "__main__":
    runner = Runner()

    runner.bench_func(
        "no format checker",
        lambda: no_checker.is_valid(INSTANCE),
    )
    runner.bench_func(
        "default format checker",
        lambda: default_checker.is_valid(INSTANCE),
    )
    runner.bench_func(
        "instance-level custom checker",
        lambda: custom_checker.is_valid(INSTANCE),
    )
