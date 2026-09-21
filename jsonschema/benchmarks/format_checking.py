from pyperf import Runner

from jsonschema import Draft202012Validator, FormatChecker

checker = FormatChecker(formats=())
checker.checks("uuid")(lambda value: len(value) == 36)
validator = Draft202012Validator(
    {"format": "uuid"},
    format_checker=checker,
)
instance = "12345678-1234-1234-1234-123456789012"


if __name__ == "__main__":
    Runner().bench_func("format checking", validator.is_valid, instance)
