from __future__ import annotations

from contextlib import contextmanager, suppress
from datetime import date, datetime
from enum import Enum, auto
from uuid import UUID
import copy
import ipaddress
import re
import threading
import typing
import warnings

from jsonschema.exceptions import FormatError, ValidationError

_FormatCheckCallable = typing.Callable[[object], bool]
#: A format checker callable.
_F = typing.TypeVar("_F", bound=_FormatCheckCallable)
_RaisesType = typing.Union[type[Exception], tuple[type[Exception], ...]]


class ExceptionPolicy(Enum):
    """What a FormatChecker does with an undeclared checker exception."""

    RAISE = auto()
    INVALID = auto()
    IGNORE = auto()


_ExceptionPolicy = typing.Union[
    ExceptionPolicy,
    typing.Callable[
        [object, str, Exception],
        typing.Union[bool, ExceptionPolicy],
    ],
]


def _default_exception_policy(
    instance: object,
    format: str,
    error: Exception,
) -> ExceptionPolicy:
    if isinstance(error, ValidationError):
        return ExceptionPolicy.INVALID
    return ExceptionPolicy.RAISE


class _FormatCheckerMeta(type):
    _class_checkers: dict[
        str,
        tuple[_FormatCheckCallable, _RaisesType],
    ]

    @property
    def checkers(cls) -> dict[str, tuple[_FormatCheckCallable, _RaisesType]]:
        return cls._class_checkers

_RE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$", re.ASCII)


class FormatChecker(metaclass=_FormatCheckerMeta):
    """
    A ``format`` property checker.

    JSON Schema does not mandate that the ``format`` property actually do any
    validation. If validation is desired however, instances of this class can
    be hooked into validators to enable format validation.

    `FormatChecker` objects always return ``True`` when asked about
    formats that they do not know how to validate.

    To add a check for a custom format use the `FormatChecker.checks`
    decorator.

    Arguments:

        formats:

            The known formats to validate. This argument can be used to
            limit which formats will be used during validation.

        exception_policy:

            Controls exceptions not listed in a checker's raises.
            Built-in policies are RAISE, INVALID, and IGNORE. A callable
            receives the instance, format, and exception and returns one.

    """

    _class_checkers: dict[
        str,
        tuple[_FormatCheckCallable, _RaisesType],
    ] = {}  # noqa: RUF012
    _class_lock = threading.RLock()
    RAISE = ExceptionPolicy.RAISE
    INVALID = ExceptionPolicy.INVALID
    IGNORE = ExceptionPolicy.IGNORE

    def __init__(
        self,
        formats: typing.Iterable[str] | None = None,
        exception_policy: _ExceptionPolicy = _default_exception_policy,
    ):
        cls = type(self)
        with cls._class_lock:
            class_checkers = dict(cls._class_checkers)
            selected_formats = (
                class_checkers.keys() if formats is None else formats
            )
            self._checkers = {
                name: class_checkers[name] for name in selected_formats
            }

        self.exception_policy = exception_policy
        self._exception_policies: dict[str, _ExceptionPolicy] = {}
        self._lock = threading.RLock()

    @property
    def checkers(self) -> dict[str, tuple[_FormatCheckCallable, _RaisesType]]:
        """Return a point-in-time copy of the checker registry."""
        with self._lock:
            return dict(self._checkers)

    def __copy__(self):
        with self._lock:
            checker = type(self)(
                formats=(),
                exception_policy=self.exception_policy,
            )
            checker._checkers = dict(self._checkers)
            checker._exception_policies = dict(self._exception_policies)
            return checker

    def __deepcopy__(self, memo):
        with self._lock:
            checker = type(self)(
                formats=(),
                exception_policy=copy.deepcopy(self.exception_policy, memo),
            )
            checker._checkers = copy.deepcopy(self._checkers, memo)
            checker._exception_policies = copy.deepcopy(
                self._exception_policies,
                memo,
            )
            return checker

    def __getstate__(self):
        with self._lock:
            return {
                "checkers": dict(self._checkers),
                "exception_policy": self.exception_policy,
                "exception_policies": dict(self._exception_policies),
            }

    def __setstate__(self, state):
        self._checkers = dict(state["checkers"])
        self.exception_policy = state["exception_policy"]
        self._exception_policies = dict(state["exception_policies"])
        self._lock = threading.RLock()

    def __repr__(self):
        return f"<FormatChecker checkers={sorted(self.checkers)}>"

    def register(
        self,
        format: str,
        func: _FormatCheckCallable,
        raises: _RaisesType = (),
        *,
        exception_policy: _ExceptionPolicy | None = None,
        overwrite: bool = True,
    ) -> None:
        """Register func as the checker for format."""
        with self._lock:
            if not overwrite and format in self._checkers:
                raise ValueError(
                    f"A checker for {format!r} is already registered",
                )
            self._checkers[format] = (func, raises)
            if exception_policy is None:
                self._exception_policies.pop(format, None)
            else:
                self._exception_policies[format] = exception_policy

    def unregister(self, format: str, *, missing_ok: bool = False) -> None:
        """Remove the checker registered for format."""
        with self._lock:
            try:
                del self._checkers[format]
            except KeyError:
                if not missing_ok:
                    raise
            self._exception_policies.pop(format, None)

    @contextmanager
    def registered(
        self,
        format: str,
        func: _FormatCheckCallable,
        raises: _RaisesType = (),
        *,
        exception_policy: _ExceptionPolicy | None = None,
    ):
        """Temporarily register a checker for the duration of a block."""
        with self._lock:
            previous_policy = self._exception_policies.get(format)
            had_previous = format in self._checkers
            previous = self._checkers.get(format)
            self._checkers[format] = (func, raises)
            if exception_policy is None:
                self._exception_policies.pop(format, None)
            else:
                self._exception_policies[format] = exception_policy
        try:
            yield
        finally:
            with self._lock:
                if had_previous:
                    assert previous is not None
                    self._checkers[format] = previous
                    if previous_policy is not None:
                        self._exception_policies[format] = previous_policy
                    else:
                        self._exception_policies.pop(format, None)
                else:
                    self.unregister(format, missing_ok=True)

    def checks(
        self,
        format: str,
        raises: _RaisesType = (),
        *,
        exception_policy: _ExceptionPolicy | None = None,
        overwrite: bool = True,
    ) -> typing.Callable[[_F], _F]:
        """
        Register a decorated function as validating a new format.

        Arguments:

            format:

                The format that the decorated function will check.

            raises:

                The exception(s) raised by the decorated function when an
                invalid instance is found.

                The exception object will be accessible as the
                `jsonschema.exceptions.ValidationError.cause` attribute of the
                resulting validation error.

            exception_policy:

                An optional policy specific to this format.

            overwrite:

                Whether an existing checker for this format is replaced.

        """

        def _checks(func: _F) -> _F:
            self.register(
                format,
                func,
                raises,
                exception_policy=exception_policy,
                overwrite=overwrite,
            )
            return func

        return _checks

    @classmethod
    def cls_checks(
        cls, format: str, raises: _RaisesType = (),
    ) -> typing.Callable[[_F], _F]:
        warnings.warn(
            (
                "FormatChecker.cls_checks is deprecated. Call "
                "FormatChecker.checks on a specific FormatChecker instance "
                "instead."
            ),
            DeprecationWarning,
            stacklevel=2,
        )
        return cls._cls_checks(format=format, raises=raises)

    @classmethod
    def _cls_checks(
        cls,
        format: str,
        raises: _RaisesType = (),
    ) -> typing.Callable[[_F], _F]:
        def _checks(func: _F) -> _F:
            with cls._class_lock:
                cls._class_checkers[format] = (func, raises)
            return func

        return _checks

    def _snapshot(
        self,
    ) -> tuple[
        dict[str, tuple[_FormatCheckCallable, _RaisesType]],
        dict[str, _ExceptionPolicy],
        _ExceptionPolicy,
    ]:
        with self._lock:
            return (
                dict(self._checkers),
                dict(self._exception_policies),
                self.exception_policy,
            )

    def _handle_exception(
        self,
        error: Exception,
        instance: object,
        format: str,
        policy: _ExceptionPolicy,
    ) -> None:
        if callable(policy):
            outcome = policy(instance, format, error)
            if outcome is True:
                outcome = ExceptionPolicy.IGNORE
            elif outcome is False:
                outcome = ExceptionPolicy.INVALID
        else:
            outcome = policy

        if outcome is ExceptionPolicy.RAISE:
            raise error
        if outcome is ExceptionPolicy.IGNORE:
            return
        if isinstance(error, ValidationError):
            raise error
        raise FormatError(
            f"{instance!r} is not a {format!r}",
            cause=error,
        ) from error

    def check(self, instance: object, format: str) -> None:
        """
        Check whether the instance conforms to the given format.

        Arguments:

            instance (*any primitive type*, i.e. str, number, bool):

                The instance to check

            format:

                The format that instance should conform to

        Raises:

            FormatError:

                if the instance does not conform to ``format``

        """
        checkers, policies, default_policy = self._snapshot()
        entry = checkers.get(format)
        if entry is None:
            return

        func, raises = entry
        result, cause = None, None
        try:
            result = func(instance)
        except raises as e:
            cause = e
        except Exception as e:
            self._handle_exception(
                e,
                instance,
                format,
                policies.get(format, default_policy),
            )
            return
        if not result:
            error = FormatError(
                f"{instance!r} is not a {format!r}",
                cause=cause,
            )
            error.__context__ = cause
            raise error

    def conforms(self, instance: object, format: str) -> bool:
        """
        Check whether the instance conforms to the given format.

        Arguments:

            instance (*any primitive type*, i.e. str, number, bool):

                The instance to check

            format:

                The format that instance should conform to

        Returns:

            bool: whether it conformed

        """
        try:
            self.check(instance, format)
        except (FormatError, ValidationError):
            return False
        else:
            return True


draft3_format_checker = FormatChecker()
draft4_format_checker = FormatChecker()
draft6_format_checker = FormatChecker()
draft7_format_checker = FormatChecker()
draft201909_format_checker = FormatChecker()
draft202012_format_checker = FormatChecker()

_draft_checkers: dict[str, FormatChecker] = dict(
    draft3=draft3_format_checker,
    draft4=draft4_format_checker,
    draft6=draft6_format_checker,
    draft7=draft7_format_checker,
    draft201909=draft201909_format_checker,
    draft202012=draft202012_format_checker,
)


def _checks_drafts(
    name=None,
    draft3=None,
    draft4=None,
    draft6=None,
    draft7=None,
    draft201909=None,
    draft202012=None,
    raises=(),
) -> typing.Callable[[_F], _F]:
    draft3 = draft3 or name
    draft4 = draft4 or name
    draft6 = draft6 or name
    draft7 = draft7 or name
    draft201909 = draft201909 or name
    draft202012 = draft202012 or name

    def wrap(func: _F) -> _F:
        if draft3:
            func = _draft_checkers["draft3"].checks(draft3, raises)(func)
        if draft4:
            func = _draft_checkers["draft4"].checks(draft4, raises)(func)
        if draft6:
            func = _draft_checkers["draft6"].checks(draft6, raises)(func)
        if draft7:
            func = _draft_checkers["draft7"].checks(draft7, raises)(func)
        if draft201909:
            func = _draft_checkers["draft201909"].checks(draft201909, raises)(
                func,
            )
        if draft202012:
            func = _draft_checkers["draft202012"].checks(draft202012, raises)(
                func,
            )

        # Oy. This is bad global state, but relied upon for now, until
        # deprecation. See #519 and test_format_checkers_come_with_defaults
        FormatChecker._cls_checks(
            draft202012 or draft201909 or draft7 or draft6 or draft4 or draft3,
            raises,
        )(func)
        return func

    return wrap


@_checks_drafts(name="idn-email")
@_checks_drafts(name="email")
def is_email(instance: object) -> bool:
    if not isinstance(instance, str):
        return True
    return "@" in instance


@_checks_drafts(
    draft3="ip-address",
    draft4="ipv4",
    draft6="ipv4",
    draft7="ipv4",
    draft201909="ipv4",
    draft202012="ipv4",
    raises=ipaddress.AddressValueError,
)
def is_ipv4(instance: object) -> bool:
    if not isinstance(instance, str):
        return True
    return bool(ipaddress.IPv4Address(instance))


@_checks_drafts(name="ipv6", raises=ipaddress.AddressValueError)
def is_ipv6(instance: object) -> bool:
    if not isinstance(instance, str):
        return True
    address = ipaddress.IPv6Address(instance)
    return not getattr(address, "scope_id", "")


with suppress(ImportError):
    from fqdn import FQDN

    @_checks_drafts(
        draft3="host-name",
        draft4="hostname",
        draft6="hostname",
        draft7="hostname",
        draft201909="hostname",
        draft202012="hostname",
        # fqdn.FQDN("") raises a ValueError due to a bug
        # however, it's not clear when or if that will be fixed, so catch it
        # here for now
        raises=ValueError,
    )
    def is_host_name(instance: object) -> bool:
        if not isinstance(instance, str):
            return True
        return FQDN(instance, min_labels=1).is_valid


with suppress(ImportError):
    # The built-in `idna` codec only implements RFC 3890, so we go elsewhere.
    import idna

    @_checks_drafts(
        draft7="idn-hostname",
        draft201909="idn-hostname",
        draft202012="idn-hostname",
        raises=(idna.IDNAError, UnicodeError),
    )
    def is_idn_host_name(instance: object) -> bool:
        if not isinstance(instance, str):
            return True
        idna.encode(instance)
        return True


try:
    import rfc3987
except ImportError:
    with suppress(ImportError):
        from rfc3986_validator import validate_rfc3986

        @_checks_drafts(name="uri")
        def is_uri(instance: object) -> bool:
            if not isinstance(instance, str):
                return True
            return validate_rfc3986(instance, rule="URI")

        @_checks_drafts(
            draft6="uri-reference",
            draft7="uri-reference",
            draft201909="uri-reference",
            draft202012="uri-reference",
            raises=ValueError,
        )
        def is_uri_reference(instance: object) -> bool:
            if not isinstance(instance, str):
                return True
            return validate_rfc3986(instance, rule="URI_reference")

    with suppress(ImportError):
        from rfc3987_syntax import is_valid_syntax as _rfc3987_is_valid_syntax

        @_checks_drafts(
            draft7="iri",
            draft201909="iri",
            draft202012="iri",
            raises=ValueError,
        )
        def is_iri(instance: object) -> bool:
            if not isinstance(instance, str):
                return True
            return _rfc3987_is_valid_syntax("iri", instance)

        @_checks_drafts(
            draft7="iri-reference",
            draft201909="iri-reference",
            draft202012="iri-reference",
            raises=ValueError,
        )
        def is_iri_reference(instance: object) -> bool:
            if not isinstance(instance, str):
                return True
            return _rfc3987_is_valid_syntax("iri_reference", instance)

else:

    @_checks_drafts(
        draft7="iri",
        draft201909="iri",
        draft202012="iri",
        raises=ValueError,
    )
    def is_iri(instance: object) -> bool:
        if not isinstance(instance, str):
            return True
        return rfc3987.parse(instance, rule="IRI")

    @_checks_drafts(
        draft7="iri-reference",
        draft201909="iri-reference",
        draft202012="iri-reference",
        raises=ValueError,
    )
    def is_iri_reference(instance: object) -> bool:
        if not isinstance(instance, str):
            return True
        return rfc3987.parse(instance, rule="IRI_reference")

    @_checks_drafts(name="uri", raises=ValueError)
    def is_uri(instance: object) -> bool:
        if not isinstance(instance, str):
            return True
        return rfc3987.parse(instance, rule="URI")

    @_checks_drafts(
        draft6="uri-reference",
        draft7="uri-reference",
        draft201909="uri-reference",
        draft202012="uri-reference",
        raises=ValueError,
    )
    def is_uri_reference(instance: object) -> bool:
        if not isinstance(instance, str):
            return True
        return rfc3987.parse(instance, rule="URI_reference")


with suppress(ImportError):
    from rfc3339_validator import validate_rfc3339

    @_checks_drafts(name="date-time")
    def is_datetime(instance: object) -> bool:
        if not isinstance(instance, str):
            return True
        return validate_rfc3339(instance.upper())

    @_checks_drafts(
        draft7="time",
        draft201909="time",
        draft202012="time",
    )
    def is_time(instance: object) -> bool:
        if not isinstance(instance, str):
            return True
        return is_datetime("1970-01-01T" + instance)


@_checks_drafts(name="regex", raises=re.error)
def is_regex(instance: object) -> bool:
    if not isinstance(instance, str):
        return True
    return bool(re.compile(instance))


@_checks_drafts(
    draft3="date",
    draft7="date",
    draft201909="date",
    draft202012="date",
    raises=ValueError,
)
def is_date(instance: object) -> bool:
    if not isinstance(instance, str):
        return True
    return bool(_RE_DATE.fullmatch(instance) and date.fromisoformat(instance))


@_checks_drafts(draft3="time", raises=ValueError)
def is_draft3_time(instance: object) -> bool:
    if not isinstance(instance, str):
        return True
    return bool(datetime.strptime(instance, "%H:%M:%S"))  # noqa: DTZ007


with suppress(ImportError):
    import webcolors

    @_checks_drafts(draft3="color", raises=(ValueError, TypeError))
    def is_css21_color(instance: object) -> bool:
        if isinstance(instance, str):
            try:
                webcolors.name_to_hex(instance)
            except ValueError:
                webcolors.normalize_hex(instance.lower())
        return True


with suppress(ImportError):
    import jsonpointer

    @_checks_drafts(
        draft6="json-pointer",
        draft7="json-pointer",
        draft201909="json-pointer",
        draft202012="json-pointer",
        raises=jsonpointer.JsonPointerException,
    )
    def is_json_pointer(instance: object) -> bool:
        if not isinstance(instance, str):
            return True
        return bool(jsonpointer.JsonPointer(instance))

    # TODO: I don't want to maintain this, so it
    #       needs to go either into jsonpointer (pending
    #       https://github.com/stefankoegl/python-json-pointer/issues/34) or
    #       into a new external library.
    @_checks_drafts(
        draft7="relative-json-pointer",
        draft201909="relative-json-pointer",
        draft202012="relative-json-pointer",
        raises=jsonpointer.JsonPointerException,
    )
    def is_relative_json_pointer(instance: object) -> bool:
        # Definition taken from:
        # https://tools.ietf.org/html/draft-handrews-relative-json-pointer-01#section-3
        if not isinstance(instance, str):
            return True
        if not instance:
            return False

        non_negative_integer, rest = [], ""
        for i, character in enumerate(instance):
            if character.isdigit():
                # digits with a leading "0" are not allowed
                if i > 0 and int(instance[i - 1]) == 0:
                    return False

                non_negative_integer.append(character)
                continue

            if not non_negative_integer:
                return False

            rest = instance[i:]
            break
        return (rest == "#") or bool(jsonpointer.JsonPointer(rest))


with suppress(ImportError):
    import uri_template

    @_checks_drafts(
        draft6="uri-template",
        draft7="uri-template",
        draft201909="uri-template",
        draft202012="uri-template",
    )
    def is_uri_template(instance: object) -> bool:
        if not isinstance(instance, str):
            return True
        return uri_template.validate(instance)


with suppress(ImportError):
    import isoduration

    @_checks_drafts(
        draft201909="duration",
        draft202012="duration",
        raises=isoduration.DurationParsingException,
    )
    def is_duration(instance: object) -> bool:
        if not isinstance(instance, str):
            return True
        isoduration.parse_duration(instance)
        # FIXME: See bolsote/isoduration#25 and bolsote/isoduration#21
        return instance.endswith(tuple("DMYWHMS"))


@_checks_drafts(
    draft201909="uuid",
    draft202012="uuid",
    raises=ValueError,
)
def is_uuid(instance: object) -> bool:
    if not isinstance(instance, str):
        return True
    UUID(instance)
    return all(instance[position] == "-" for position in (8, 13, 18, 23))
