"""
Validation errors, and some surrounding helpers.
"""
from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Mapping
from functools import total_ordering
from pprint import pformat
from textwrap import dedent, indent
from typing import TYPE_CHECKING, Any, ClassVar, NamedTuple
import re
import warnings

from attrs import define, field
from referencing.exceptions import Unresolvable as _Unresolvable

from jsonschema import _utils

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, MutableMapping, Sequence

    from jsonschema import _types

WEAK_MATCHES: frozenset[str] = frozenset(["anyOf", "oneOf"])
STRONG_MATCHES: frozenset[str] = frozenset()

_JSON_PATH_COMPATIBLE_PROPERTY_PATTERN = re.compile("^[a-zA-Z][a-zA-Z0-9_]*$")

_unset = _utils.Unset()


def _pretty(thing: Any, prefix: str):
    """
    Format something for an error message as prettily as we currently can.
    """
    return indent(pformat(thing, width=72, sort_dicts=False), prefix).lstrip()


def __getattr__(name):
    if name == "RefResolutionError":
        warnings.warn(
            _RefResolutionError._DEPRECATION_MESSAGE,
            DeprecationWarning,
            stacklevel=2,
        )
        return _RefResolutionError
    raise AttributeError(f"module {__name__} has no attribute {name}")


class _Error(Exception):

    _word_for_schema_in_error_message: ClassVar[str]
    _word_for_instance_in_error_message: ClassVar[str]

    def __init__(
        self,
        message: str,
        validator: str = _unset,  # type: ignore[assignment]
        path: Iterable[str | int] = (),
        cause: Exception | None = None,
        context=(),
        validator_value: Any = _unset,
        instance: Any = _unset,
        schema: Mapping[str, Any] | bool = _unset,  # type: ignore[assignment]
        schema_path: Iterable[str | int] = (),
        parent: _Error | None = None,
        type_checker: _types.TypeChecker = _unset,  # type: ignore[assignment]
    ) -> None:
        super().__init__(
            message,
            validator,
            path,
            cause,
            context,
            validator_value,
            instance,
            schema,
            schema_path,
            parent,
        )
        self.message = message
        self.path = self.relative_path = deque(path)
        self.schema_path = self.relative_schema_path = deque(schema_path)
        self.context = list(context)
        self.cause = self.__cause__ = cause
        self.validator = validator
        self.validator_value = validator_value
        self.instance = instance
        self.schema = schema
        self.parent = parent
        self._type_checker = type_checker

        for error in context:
            error.parent = self

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}: {self.message!r}>"

    def __str__(self) -> str:
        essential_for_verbose = (
            self.validator, self.validator_value, self.instance, self.schema,
        )
        if any(m is _unset for m in essential_for_verbose):
            return self.message

        schema_path = _utils.format_as_index(
            container=self._word_for_schema_in_error_message,
            indices=list(self.relative_schema_path)[:-1],
        )
        instance_path = _utils.format_as_index(
            container=self._word_for_instance_in_error_message,
            indices=self.relative_path,
        )
        prefix = 16 * " "

        return dedent(
            f"""\
            {self.message}

            Failed validating {self.validator!r} in {schema_path}:
                {_pretty(self.schema, prefix=prefix)}

            On {instance_path}:
                {_pretty(self.instance, prefix=prefix)}
            """.rstrip(),
        )

    @classmethod
    def create_from(cls, other: _Error):
        return cls(**other._contents())

    @property
    def absolute_path(self) -> Sequence[str | int]:
        parent = self.parent
        if parent is None:
            return self.relative_path

        path = deque(self.relative_path)
        path.extendleft(reversed(parent.absolute_path))
        return path

    @property
    def absolute_schema_path(self) -> Sequence[str | int]:
        parent = self.parent
        if parent is None:
            return self.relative_schema_path

        path = deque(self.relative_schema_path)
        path.extendleft(reversed(parent.absolute_schema_path))
        return path

    @property
    def json_path(self) -> str:
        path = "$"
        for elem in self.absolute_path:
            if isinstance(elem, int):
                path += "[" + str(elem) + "]"
            elif _JSON_PATH_COMPATIBLE_PROPERTY_PATTERN.match(elem):
                path += "." + elem
            else:
                escaped_elem = elem.replace("\\", "\\\\").replace("'", r"\'")
                path += "['" + escaped_elem + "']"
        return path

    def _set(
        self,
        type_checker: _types.TypeChecker | None = None,
        **kwargs: Any,
    ) -> None:
        if type_checker is not None and self._type_checker is _unset:
            self._type_checker = type_checker

        for k, v in kwargs.items():
            if getattr(self, k) is _unset:
                setattr(self, k, v)

    def _contents(self):
        attrs = (
            "message", "cause", "context", "validator", "validator_value",
            "path", "schema_path", "instance", "schema", "parent",
        )
        return {attr: getattr(self, attr) for attr in attrs}

    def _matches_type(self) -> bool:
        try:
            # We ignore this as we want to simply crash if this happens
            expected = self.schema["type"]  # type: ignore[index]
        except (KeyError, TypeError):
            return False

        if isinstance(expected, str):
            return self._type_checker.is_type(self.instance, expected)

        return any(
            self._type_checker.is_type(self.instance, expected_type)
            for expected_type in expected
        )


class ValidationError(_Error):
    """
    An instance was invalid under a provided schema.
    """

    _word_for_schema_in_error_message = "schema"
    _word_for_instance_in_error_message = "instance"


class SchemaError(_Error):
    """
    A schema was invalid under its corresponding metaschema.
    """

    _word_for_schema_in_error_message = "metaschema"
    _word_for_instance_in_error_message = "schema"


@define(slots=False)
class _RefResolutionError(Exception):  # noqa: PLW1641
    """
    A ref could not be resolved.
    """

    _DEPRECATION_MESSAGE = (
        "jsonschema.exceptions.RefResolutionError is deprecated as of version "
        "4.18.0. If you wish to catch potential reference resolution errors, "
        "directly catch referencing.exceptions.Unresolvable."
    )

    _cause: Exception

    def __eq__(self, other):
        if self.__class__ is not other.__class__:
            return NotImplemented  # pragma: no cover -- uncovered but deprecated  # noqa: E501
        return self._cause == other._cause

    def __str__(self) -> str:
        return str(self._cause)


class _WrappedReferencingError(_RefResolutionError, _Unresolvable):  # pragma: no cover -- partially uncovered but to be removed  # noqa: E501
    def __init__(self, cause: _Unresolvable):
        object.__setattr__(self, "_wrapped", cause)

    def __eq__(self, other):
        if other.__class__ is self.__class__:
            return self._wrapped == other._wrapped
        elif other.__class__ is self._wrapped.__class__:
            return self._wrapped == other
        return NotImplemented

    def __getattr__(self, attr):
        return getattr(self._wrapped, attr)

    def __hash__(self):
        return hash(self._wrapped)

    def __repr__(self):
        return f"<WrappedReferencingError {self._wrapped!r}>"

    def __str__(self):
        return f"{self._wrapped.__class__.__name__}: {self._wrapped}"


class UndefinedTypeCheck(Exception):
    """
    A type checker was asked to check a type it did not have registered.
    """

    def __init__(self, type: str) -> None:
        self.type = type

    def __str__(self) -> str:
        return f"Type {self.type!r} is unknown to this type checker"


class UnknownType(Exception):
    """
    A validator was asked to validate an instance against an unknown type.
    """

    def __init__(self, type, instance, schema):
        self.type = type
        self.instance = instance
        self.schema = schema

    def __str__(self):
        prefix = 16 * " "

        return dedent(
            f"""\
            Unknown type {self.type!r} for validator with schema:
                {_pretty(self.schema, prefix=prefix)}

            While checking instance:
                {_pretty(self.instance, prefix=prefix)}
            """.rstrip(),
        )


class FormatError(Exception):
    """
    Validating a format failed.
    """

    def __init__(self, message, cause=None):
        super().__init__(message, cause)
        self.message = message
        self.cause = self.__cause__ = cause

    def __str__(self):
        return self.message


class ErrorTree:
    """
    ErrorTrees make it easier to check which validations failed.
    """

    _instance = _unset

    def __init__(self, errors: Iterable[ValidationError] = ()):
        self.errors: MutableMapping[str, ValidationError] = {}
        self._contents: Mapping[str, ErrorTree] = defaultdict(self.__class__)

        for error in errors:
            container = self
            for element in error.path:
                container = container[element]
            container.errors[error.validator] = error

            container._instance = error.instance

    def __contains__(self, index: str | int):
        """
        Check whether ``instance[index]`` has any errors.
        """
        return index in self._contents

    def __getitem__(self, index):
        """
        Retrieve the child tree one level down at the given ``index``.

        If the index is not in the instance that this tree corresponds
        to and is not known by this tree, whatever error would be raised
        by ``instance.__getitem__`` will be propagated (usually this is
        some subclass of `LookupError`.
        """
        if self._instance is not _unset and index not in self:
            self._instance[index]
        return self._contents[index]

    def __setitem__(self, index: str | int, value: ErrorTree):
        """
        Add an error to the tree at the given ``index``.

        .. deprecated:: v4.20.0

            Setting items on an `ErrorTree` is deprecated without replacement.
            To populate a tree, provide all of its sub-errors when you
            construct the tree.
        """
        warnings.warn(
            "ErrorTree.__setitem__ is deprecated without replacement.",
            DeprecationWarning,
            stacklevel=2,
        )
        self._contents[index] = value  # type: ignore[index]

    def __iter__(self):
        """
        Iterate (non-recursively) over the indices in the instance with errors.
        """
        return iter(self._contents)

    def __len__(self):
        """
        Return the `total_errors`.
        """
        return self.total_errors

    def __repr__(self):
        total = len(self)
        errors = "error" if total == 1 else "errors"
        return f"<{self.__class__.__name__} ({total} total {errors})>"

    @property
    def total_errors(self):
        """
        The total number of errors in the entire tree, including children.
        """
        child_errors = sum(len(tree) for _, tree in self._contents.items())
        return len(self.errors) + child_errors


class RelevanceWeights(NamedTuple):
    """
    Weights for the criteria used to rank validation errors.

    A criterion with weight zero is omitted from the primary ranking. Some
    omitted criteria are still used as deterministic tie-breakers, as shown
    by RelevanceScore.explanation.
    """

    instance_path: int = 1_000_000
    schema_path: int = 0
    validator_priority: int = 100
    error_depth: int = 10
    type_match: int = 1
    custom: int = 1_000


class RelevanceCriterion(NamedTuple):
    """
    One explainable component of an error relevance score.
    """

    name: str
    weight: int
    value: Any
    preference: str


def _path_key(path: Iterable[str | int]) -> tuple[tuple[int, str], ...]:
    return tuple(
        (0, str(segment)) if isinstance(segment, int) else (1, str(segment))
        for segment in path
    )


def _stable_value(value: Any) -> Any:
    """
    Return a deterministic, comparable representation of a JSON-like value.
    """
    try:
        if isinstance(value, Mapping):
            items = (
                (_stable_value(key), _stable_value(item))
                for key, item in value.items()
            )
            return ("mapping", tuple(sorted(items)))
        if isinstance(value, (list, tuple, deque)):
            return ("sequence", tuple(_stable_value(each) for each in value))
        if isinstance(value, (set, frozenset)):
            values = (_stable_value(each) for each in value)
            return ("set", tuple(sorted(values)))
        if value is None or isinstance(value, (str, int, float, bool)):
            return ("value", type(value).__qualname__, repr(value))
        return ("object", type(value).__qualname__)
    except (RecursionError, RuntimeError, ValueError):
        return ("unrepresentable", type(value).__qualname__)


@total_ordering
@define(frozen=True, eq=False)
class _StablePayloadKey:
    """
    A message-independent final tie-breaker for otherwise identical errors.
    """

    _error: ValidationError
    _key: Any = field(init=False, default=None)

    @property
    def key(self) -> Any:
        key = self._key
        if key is None:
            key = (
                _stable_value(self._error.instance),
                _stable_value(self._error.schema),
                _stable_value(self._error.validator_value),
            )
            object.__setattr__(self, "_key", key)
        return key

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _StablePayloadKey):
            return NotImplemented
        return self.key == other.key

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, _StablePayloadKey):
            return NotImplemented
        return self.key < other.key

    def __repr__(self) -> str:
        return repr(self.key)


@total_ordering
@define(frozen=True, eq=False)
class RelevanceScore:
    """
    A relevance score and its explainable criteria.

    Scores compare using their complete deterministic tie-break table. The
    weighted ranking criteria remain separately available via rank, which
    lets best_match recognize equally relevant branches in an error context
    without discarding or merging those branches.
    """

    rank: tuple[Any, ...]
    tie_breakers: tuple[Any, ...]
    explanation: tuple[RelevanceCriterion, ...]

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RelevanceScore):
            return NotImplemented
        return (self.rank, self.tie_breakers) == (
            other.rank,
            other.tie_breakers,
        )

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, RelevanceScore):
            return NotImplemented
        return (self.rank, self.tie_breakers) < (
            other.rank,
            other.tie_breakers,
        )

    def same_rank(self, other: RelevanceScore) -> bool:
        """
        Return whether two scores have equal weighted ranking criteria.
        """
        return isinstance(other, RelevanceScore) and self.rank == other.rank


def _error_depth(error: ValidationError) -> int:
    depth = 0
    parent = error.parent
    while parent is not None:
        depth += 1
        parent = parent.parent
    return depth


def _zero_weight(error: ValidationError) -> int:
    return 0


@define(frozen=True)
class RelevanceStrategy:
    """
    A configurable strategy for ranking validation errors.

    The strategy is read-only with respect to errors: scoring never mutates
    ValidationError.context, parents, paths, causes, or any other error
    attribute.
    """

    weights: RelevanceWeights = field(factory=RelevanceWeights)
    validator_priority: Mapping[str, int] = field(factory=dict)
    custom_weight: Callable[[ValidationError], int] = _zero_weight
    context_tie: str = "parent"

    def __attrs_post_init__(self) -> None:
        if any(weight < 0 for weight in self.weights):
            raise ValueError("relevance weights must be non-negative")
        if self.context_tie not in {"parent", "best"}:
            raise ValueError("context_tie must be 'parent' or 'best'")
        object.__setattr__(
            self,
            "validator_priority",
            dict(self.validator_priority),
        )

    def __call__(self, error: ValidationError) -> RelevanceScore:
        return self.score(error)

    def score(self, error: ValidationError) -> RelevanceScore:
        """
        Score an error without mutating it or its context tree.
        """
        schema_path = tuple(error.absolute_schema_path)
        depth = _error_depth(error)
        values = {
            "schema_path": (-len(schema_path), _path_key(schema_path)),
            "error_depth": -depth,
        }
        if self.weights.instance_path:
            instance_path = tuple(error.absolute_path)
            values["instance_path"] = (
                -len(instance_path),
                _path_key(instance_path),
            )
        if self.weights.validator_priority:
            values["validator_priority"] = self.validator_priority.get(
                error.validator,
                1,
            )
        if self.weights.type_match:
            values["type_match"] = not error._matches_type()
        if self.weights.custom:
            values["custom"] = self.custom_weight(error)

        preferences = {
            "instance_path": "shallower, then lexicographically earlier",
            "schema_path": "shallower, then lexicographically earlier",
            "validator_priority": "higher configured priority",
            "error_depth": "shallower in the error context tree",
            "type_match": "errors whose declared type matches the instance",
            "custom": "higher custom weight",
        }

        criteria = [
            RelevanceCriterion(
                name=name,
                weight=weight,
                value=values[name],
                preference=preferences[name],
            )
            for name, weight in self.weights._asdict().items()
            if weight
        ]
        criteria.sort(
            key=lambda criterion: (-criterion.weight, criterion.name),
        )

        tie_breakers = []
        if not self.weights.schema_path:
            tie_breakers.append(
                RelevanceCriterion(
                    name="schema_path",
                    weight=0,
                    value=values["schema_path"],
                    preference=preferences["schema_path"],
                ),
            )
        if not self.weights.error_depth:
            tie_breakers.append(
                RelevanceCriterion(
                    name="error_depth",
                    weight=0,
                    value=values["error_depth"],
                    preference=preferences["error_depth"],
                ),
            )
        tie_breakers.extend(
            [
                RelevanceCriterion(
                    name="context_size",
                    weight=0,
                    value=len(error.context),
                    preference="more nested context",
                ),
                RelevanceCriterion(
                    name="cause",
                    weight=0,
                    value=(
                        ""
                        if error.cause is None
                        else type(error.cause).__qualname__
                    ),
                    preference="lexicographically later cause type",
                ),
                RelevanceCriterion(
                    name="payload",
                    weight=0,
                    value=_StablePayloadKey(error),
                    preference=(
                        "deterministic instance, schema, and validator value"
                    ),
                ),
            ],
        )

        return RelevanceScore(
            rank=tuple(criterion.value for criterion in criteria),
            tie_breakers=tuple(criterion.value for criterion in tie_breakers),
            explanation=tuple(
                criteria
                + tie_breakers
                + [
                    RelevanceCriterion(
                        name="input_order",
                        weight=0,
                        value=None,
                        preference="earlier input order",
                    ),
                ],
            ),
        )


def _validator_priority_mapping(
    weak: Iterable[str] | str,
    strong: Iterable[str] | str,
    overrides: Mapping[str, int] | None,
) -> dict[str, int]:
    if isinstance(weak, str):
        weak = (weak,)
    if isinstance(strong, str):
        strong = (strong,)

    priorities = {validator: 0 for validator in weak}
    priorities.update((validator, 2) for validator in strong)
    if overrides is not None:
        priorities.update(overrides)
    return priorities


def by_relevance(
    weak=WEAK_MATCHES,
    strong=STRONG_MATCHES,
    *,
    weights=None,
    validator_priority=None,
    custom_weight=None,
    context_tie="parent",
):
    """
    Create a key function that can be used to sort errors by relevance.

    Arguments:
        weak (set):
            a collection of validation keywords to consider to be
            "weak".  If there are two errors at the same level of the
            instance and one is in the set of weak validation keywords,
            the other error will take priority. By default, :kw:`anyOf`
            and :kw:`oneOf` are considered weak keywords and will be
            superseded by other same-level validation errors.

        strong (set):
            a collection of validation keywords to consider to be
            "strong"

        weights (RelevanceWeights):
            weights controlling which relevance criteria participate in the
            primary ranking, and in which order

        validator_priority (collections.abc.Mapping):
            explicit priorities for validation keywords, overriding weak and
            strong

        custom_weight (collections.abc.Callable):
            a callable receiving each error and returning an integer weight

        context_tie (str):
            whether best_match returns the parent or descends to the best
            deterministic child when context errors have equal primary ranks

    """
    if isinstance(weak, RelevanceStrategy):
        if (
            strong != STRONG_MATCHES
            or weights is not None
            or validator_priority is not None
            or custom_weight is not None
            or context_tie != "parent"
        ):
            raise TypeError(
                "cannot combine a relevance strategy with other options",
            )
        return weak

    if isinstance(weak, RelevanceWeights):
        if weights is not None:
            raise TypeError("weights were provided twice")
        weights = weak
        weak = WEAK_MATCHES

    if weights is None:
        weights = RelevanceWeights()
    elif isinstance(weights, Mapping):
        weights = RelevanceWeights(**weights)

    if custom_weight is None:
        custom_weight = _zero_weight

    return RelevanceStrategy(
        weights=weights,
        validator_priority=_validator_priority_mapping(
            weak,
            strong,
            validator_priority,
        ),
        custom_weight=custom_weight,
        context_tie=context_tie,
    )


relevance = by_relevance()
"""
A key function (e.g. to use with `sorted`) which sorts errors by relevance.

Example:

.. code:: python

    sorted(validator.iter_errors(12), key=jsonschema.exceptions.relevance)
"""


def best_match(errors, key=relevance):
    """
    Try to find an error that appears to be the best match among given errors.

    In general, errors that are higher up in the instance (i.e. for which
    `ValidationError.path` is shorter) are considered better matches,
    since they indicate "more" is wrong with the instance.

    If the resulting match is either :kw:`oneOf` or :kw:`anyOf`, the
    *opposite* assumption is made -- i.e. the deepest error is picked,
    since these keywords only need to match once, and any other errors
    may not be relevant.

    Arguments:
        errors (collections.abc.Iterable):

            the errors to select from. Do not provide a mixture of
            errors from different validation attempts (i.e. from
            different instances or schemas), since it won't produce
            sensical output.

        key (collections.abc.Callable):

            the key to use when sorting errors. See `relevance` and
            transitively `by_relevance` for more details (the default is
            to sort with the defaults of that function). Changing the
            default is only useful if you want to change the function
            that rates errors but still want the error context descent
            done by this function.

    Returns:
        the best matching error, or ``None`` if the iterable was empty

    .. note::

        This function is a heuristic. Its return value may change for a given
        set of inputs from version to version if better heuristics are added.

    """
    best = max(errors, key=key, default=None)
    if best is None:
        return

    while best.context:
        scored = [(key(error), error) for error in best.context]
        smallest_key, smallest = min(
            scored,
            key=lambda score_and_error: score_and_error[0],
        )
        if isinstance(key, RelevanceStrategy) and key.context_tie == "best":
            best = smallest
            continue

        if isinstance(smallest_key, RelevanceScore):
            tied = any(
                error is not smallest
                and other_key.same_rank(smallest_key)
                for other_key, error in scored
            )
        else:
            tied = any(
                error is not smallest and other_key == smallest_key
                for other_key, error in scored
            )
        if tied:
            return best
        best = smallest
    return best
