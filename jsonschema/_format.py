from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from datetime import date, datetime
from uuid import UUID
import ipaddress
import re
import string
import warnings

from jsonschema.exceptions import (
    FormatError,
    FrozenFormatCheckerError,
    StrictFormatError,
)

#: A format checker callable.
_FormatCheckCallable = Callable[[object], bool]
_RaisesType = type[Exception] | tuple[type[Exception], ...]

_RE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$", re.ASCII)

#: The formats recognized by JSON Schema drafts which rely on an optional
#: third-party package, mapped to the package(s) which provide their checker.
#:
#: A value lists the packages that can supply the checker; a format is
#: considered available if any of them is installed. Formats implemented using
#: only the standard library are deliberately absent and are always available.
_FORMAT_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "color": ("webcolors",),
    "date-time": ("rfc3339_validator",),
    "time": ("rfc3339_validator",),
    "duration": ("isoduration",),
    "hostname": ("fqdn",),
    "host-name": ("fqdn",),
    "idn-hostname": ("idna",),
    "iri": ("rfc3987", "rfc3987_syntax"),
    "iri-reference": ("rfc3987", "rfc3987_syntax"),
    "json-pointer": ("jsonpointer",),
    "relative-json-pointer": ("jsonpointer",),
    "uri": ("rfc3987", "rfc3986_validator"),
    "uri-reference": ("rfc3987", "rfc3986_validator"),
    "uri-template": ("uri_template",),
}

#: Every format name defined across the supported JSON Schema drafts.
#: Standard-library-only formats are included alongside the optional ones
#: above so that a declared format can be classified as either a known (but
#: possibly uninstalled) format or a genuinely unknown one.
_KNOWN_FORMATS: frozenset[str] = frozenset(
    {
        "color",
        "date",
        "date-time",
        "duration",
        "email",
        "hostname",
        "host-name",
        "idn-email",
        "idn-hostname",
        "ip-address",
        "ipv4",
        "ipv6",
        "iri",
        "iri-reference",
        "json-pointer",
        "regex",
        "relative-json-pointer",
        "time",
        "uri",
        "uri-reference",
        "uri-template",
        "uuid",
    },
)


def _is_installed(package: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(package) is not None


def missing_dependency(format: str) -> str | None:
    """
    Return the name of a missing optional package for the given known format.

    Returns ``None`` if the format needs no optional package, or if one of the
    packages that can supply its checker is already installed.
    """
    packages = _FORMAT_DEPENDENCIES.get(format)
    if not packages:
        return None
    for package in packages:
        if _is_installed(package):
            return None
    return packages[0]


class FormatChecker:
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

    """

    checkers: dict[
        str,
        tuple[_FormatCheckCallable, _RaisesType],
    ] = {}  # noqa: RUF012

    def __init__(self, formats: Iterable[str] | None = None):
        if formats is None:
            formats = self.checkers.keys()
        self.checkers = {k: self.checkers[k] for k in formats}
        self._frozen = False

    def __repr__(self):
        return f"<FormatChecker checkers={sorted(self.checkers)}>"

    def checks[F: _FormatCheckCallable](
        self,
        format: str,
        raises: _RaisesType = (),
    ) -> Callable[[F], F]:
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

        """

        def _checks(func: F) -> F:
            if self._frozen:
                raise FrozenFormatCheckerError(
                    f"Cannot register format {format!r}: this FormatChecker "
                    "is frozen and its set of checkers cannot change.",
                )
            self.checkers[format] = (func, raises)
            return func

        return _checks

    def freeze(self) -> FormatChecker:
        """
        Mark this checker as frozen, returning the checker itself.

        A frozen checker rejects further `checks` registrations. Freezing the
        checker prepared for a strict validator pins the exact set of formats
        that validator will enforce, independent of later global registrations.
        """
        self._frozen = True
        return self

    @property
    def frozen(self) -> bool:
        """Whether this checker no longer accepts new registrations."""
        return self._frozen

    def strict_for(
        self,
        schema,
        resolver,
        applicable_validators,
        *,
        annotation_only: Iterable[str] = (),
    ) -> FormatChecker:
        """
        Prepare a frozen checker covering every format the schema declares.

        The schema is walked following ``$ref`` (and related) keywords through
        the supplied ``resolver`` so that references into the local registry
        are inspected as well.

        Every genuinely declared :kw:`format` keyword must have a usable
        checker, otherwise `jsonschema.exceptions.StrictFormatError` is raised
        while preparing the validator rather than being silently ignored.

        A new frozen checker is returned; the checker this method is called on
        is left untouched, so it can keep serving other validators. Checkers
        already registered on this instance (including user-defined formats)
        are carried over and count as available.

        Arguments:

            schema:

                the root schema whose formats will be enforced

            resolver:

                a ``referencing`` resolver rooted at ``schema``, used to
                resolve references into the local registry

            applicable_validators:

                the validator class' ``applicable_validators`` callable, used
                to honor draft-specific rules such as ignoring the siblings of
                a ``$ref`` keyword

            annotation_only:

                known formats that are allowed to remain mere annotations; a
                declaration of one of these does not require a checker and no
                error is raised for it

        """
        annotation_only = frozenset(annotation_only)
        declared = _find_declared_formats(
            schema=schema,
            resolver=resolver,
            applicable_validators=applicable_validators,
        )

        for format, paths in declared.items():
            # A checker already registered on this FormatChecker (including a
            # user-defined one) is always usable.
            if format in self.checkers:
                continue
            dependency = missing_dependency(format)
            if dependency is not None:
                if format in annotation_only:
                    # A known format explicitly allowed to remain a mere
                    # annotation does not require its (missing) checker.
                    continue
                raise StrictFormatError(
                    format,
                    StrictFormatError.MISSING_DEPENDENCY,
                    schema_path=paths[0],
                    dependency=dependency,
                )
            if format not in _KNOWN_FORMATS:
                # Annotation-only only makes sense for a known format; an
                # unrecognized name is always a configuration error.
                raise StrictFormatError(
                    format,
                    StrictFormatError.UNKNOWN,
                    schema_path=paths[0],
                )

        # Pin exactly the enforced formats on a separate frozen checker.
        # Annotation-only formats and any format the schema never declares are
        # intentionally left out, and later registrations elsewhere cannot
        # affect the result.
        prepared = type(self)(formats=())
        prepared.checkers = {
            format: self.checkers[format]
            for format in declared
            if format in self.checkers and format not in annotation_only
        }
        return prepared.freeze()

    @classmethod
    def cls_checks[F: _FormatCheckCallable](
        cls,
        format: str,
        raises: _RaisesType = (),
    ) -> Callable[[F], F]:
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
    def _cls_checks[F: _FormatCheckCallable](
        cls,
        format: str,
        raises: _RaisesType = (),
    ) -> Callable[[F], F]:
        def _checks(func: F) -> F:
            cls.checkers[format] = (func, raises)
            return func

        return _checks

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
        if format not in self.checkers:
            return

        func, raises = self.checkers[format]
        result, cause = None, None
        try:
            result = func(instance)
        except raises as e:
            cause = e
        if not result:
            raise FormatError(f"{instance!r} is not a {format!r}", cause=cause)

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
        except FormatError:
            return False
        else:
            return True


#: Applicator keywords whose value is a single (possibly boolean) subschema.
_SINGLE_SUBSCHEMA_KEYWORDS: frozenset[str] = frozenset(
    {
        "additionalItems",
        "additionalProperties",
        "contains",
        "contentSchema",
        "not",
        "propertyNames",
        "unevaluatedItems",
        "unevaluatedProperties",
    },
)

#: Applicator keywords whose value is an array of subschemas.
_ARRAY_SUBSCHEMA_KEYWORDS: frozenset[str] = frozenset(
    {"allOf", "anyOf", "oneOf", "prefixItems"},
)

#: Applicator keywords whose value maps names to subschemas.
_OBJECT_SUBSCHEMA_KEYWORDS: frozenset[str] = frozenset(
    {
        "$defs",
        "definitions",
        "dependencies",
        "dependentSchemas",
        "properties",
        "patternProperties",
    },
)

#: Applicator keywords whose value is either a single subschema or an array
#: of subschemas (draft 3 ``extends`` and the older tuple ``items`` form).
_ITEMS_KEYWORDS: frozenset[str] = frozenset({"items", "extends"})

#: Reference keywords whose value is a URI reference to another schema.
_REFERENCE_KEYWORDS: frozenset[str] = frozenset(
    {"$ref", "$recursiveRef", "$dynamicRef"},
)


def _find_declared_formats(
    schema,
    resolver,
    applicable_validators,
) -> dict[str, list[tuple]]:
    """
    Walk the schema and collect every genuinely declared :kw:`format`.

    Returns a mapping from format name to the schema paths (each a tuple of
    property names and array indices) where that format was declared. A
    ``format`` value that merely appears inside a :kw:`const` or :kw:`enum`
    keyword is treated as ordinary instance data and is not collected.

    References are followed through ``resolver`` (including references into
    the local registry), and already-visited schema objects are tracked by
    identity so that cyclic references cannot cause unbounded recursion.
    """
    import referencing.exceptions

    declared: dict[str, list[tuple]] = {}
    seen: set[int] = set()
    pending: list[tuple[object, tuple, object]] = [(schema, (), resolver)]

    while pending:
        current, path, current_resolver = pending.pop()
        if not isinstance(current, Mapping) or id(current) in seen:
            continue
        seen.add(id(current))

        for keyword, value in applicable_validators(current):
            keyword_path = path + (keyword,)

            if keyword == "format":
                # Only a genuine `format` keyword that survives the draft's
                # applicable-keyword rules counts. A string value named
                # `format` nested inside `const`/`enum` data never reaches
                # here.
                if isinstance(value, str):
                    declared.setdefault(value, []).append(keyword_path)
            elif keyword in _REFERENCE_KEYWORDS:
                if not isinstance(value, str):
                    continue
                try:
                    resolved = current_resolver.lookup(value)
                except referencing.exceptions.Unresolvable:
                    # An unresolvable reference is reported (if at all) while
                    # validating instances, not while preparing a checker.
                    continue
                pending.append(
                    (resolved.contents, keyword_path, resolved.resolver),
                )
            elif keyword in _SINGLE_SUBSCHEMA_KEYWORDS:
                pending.append((value, keyword_path, current_resolver))
            elif (
                keyword in _ARRAY_SUBSCHEMA_KEYWORDS
                or keyword in _ITEMS_KEYWORDS
            ):
                if isinstance(value, Mapping):
                    pending.append((value, keyword_path, current_resolver))
                elif (
                    isinstance(value, Sequence)
                    and not isinstance(value, str)
                ):
                    for index, subschema in enumerate(value):
                        pending.append(
                            (
                                subschema,
                                keyword_path + (index,),
                                current_resolver,
                            ),
                        )
            elif (
                keyword in _OBJECT_SUBSCHEMA_KEYWORDS
                and isinstance(value, Mapping)
            ):
                for name, subschema in value.items():
                    pending.append(
                        (
                            subschema,
                            keyword_path + (name,),
                            current_resolver,
                        ),
                    )

    return declared


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


def _checks_drafts[F: _FormatCheckCallable](
    name=None,
    draft3=None,
    draft4=None,
    draft6=None,
    draft7=None,
    draft201909=None,
    draft202012=None,
    raises=(),
) -> Callable[[F], F]:
    draft3 = draft3 or name
    draft4 = draft4 or name
    draft6 = draft6 or name
    draft7 = draft7 or name
    draft201909 = draft201909 or name
    draft202012 = draft202012 or name

    def wrap(func: F) -> F:
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

        # A non-negative-integer prefix of ASCII digits (%x30-39), which is
        # either "0" or has no leading "0".
        rest = instance.lstrip(string.digits)
        prefix = instance[: len(instance) - len(rest)]
        if not prefix:
            return False
        if len(prefix) > 1 and prefix[0] == "0":
            return False
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
    return str(UUID(instance)) == instance.lower()
