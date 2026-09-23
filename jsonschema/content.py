"""
Explicit, opt-in validation of the ``content*`` keywords.

By default, `jsonschema` follows the JSON Schema specification and treats
:kw:`contentEncoding`, :kw:`contentMediaType` and :kw:`contentSchema`
as annotations only -- they never affect validation.

This module implements an *opt-in* extension for callers who do want the
decoded contents of a string checked. It deliberately supports only two
strict, local decoding steps:

* strict Base64 (RFC 4648, standard alphabet, correct padding required)
* UTF-8 encoded JSON (with duplicate object keys rejected)

No arbitrary encodings are invoked and no content is ever retrieved from
the network. Anything else (another encoding, another media type, a
declared non-UTF-8 charset) is left alone and keeps its default
annotation-only behavior.

Decoding is bounded: a single validation run shares a maximum decode
depth and a total budget of decoded bytes, in addition to a per-string
size limit, so hostile instances cannot exhaust memory or recurse
forever. The instance is never mutated -- a string which successfully
decodes and validates stays a string.

Enable the extension by extending any validator class:

.. code:: python

    from jsonschema import Draft202012Validator
    from jsonschema.content import extend_with_content_validation

    Validator = extend_with_content_validation(Draft202012Validator)
    validator = Validator(schema)

Errors raised for the *decoded* document are instances of
`ContentError` (itself a `jsonschema.ValidationError`), so callers can
tell them apart from ordinary errors on the surrounding instance:

.. code:: python

    for error in validator.iter_errors(instance):
        if isinstance(error, ContentError):
            # error.path          -> location of the encoded string
            # error.decode_path   -> intermediate decode layers
            # error.content_path  -> JSON Pointer within the decoded doc
            handle_content_error(error)
        else:
            handle_ordinary_error(error)
"""
from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from contextlib import closing
from contextvars import ContextVar
from typing import Any
import base64
import binascii
import json

from attrs import define

from jsonschema import exceptions, validators as _validators

__all__ = [
    "ContentBudgetExceededError",
    "ContentConfig",
    "ContentDecodingError",
    "ContentDepthExceededError",
    "ContentError",
    "ContentSchemaError",
    "ContentTooLargeError",
    "DuplicateKeyError",
    "InvalidBase64DataError",
    "InvalidBase64PaddingError",
    "InvalidJSONError",
    "InvalidUTF8Error",
    "extend_with_content_validation",
]


@define(frozen=True)
class ContentConfig:
    """
    Limits shared by all content decoding within a single validation run.

    Arguments:

        max_depth:

            the maximum number of nested decoding layers. A decoded
            document may itself contain strings with ``content*``
            keywords; each such level consumes one unit of depth.

        max_decoded_bytes:

            the maximum size, in bytes, of any single decoded string.

        max_total_decoded_bytes:

            the maximum cumulative size, in bytes, of *all* strings
            decoded while validating one instance. Nested and sibling
            encoded strings share this budget.
    """

    max_depth: int = 8
    max_decoded_bytes: int = 8_388_608
    max_total_decoded_bytes: int = 33_554_432


def _json_pointer(segments: Iterable[str | int]) -> str:
    """
    Render path segments as an RFC 6901 JSON Pointer.
    """
    segments = list(segments)
    if not segments:
        return ""
    return "/" + "/".join(
        str(segment).replace("~", "~0").replace("/", "~1")
        for segment in segments
    )


class ContentError(exceptions.ValidationError):
    """
    An error encountered while validating encoded content.

    This is the base class of all errors produced by the opt-in content
    validation in this module, and is itself a
    `jsonschema.ValidationError`, so existing error handling keeps
    working while callers can distinguish content errors via
    `isinstance`.

    Besides the usual error attributes:

    * `path` (as usual) locates the *encoded string* within the
      original instance -- the outer field position.
    * `decode_path` is a list of intermediate decode layers, outermost
      first. Each layer is the path, within the enclosing decoded
      document, of the next nested encoded string. It is empty for
      content decoded directly from the original instance.
    * `content_path` is the path within the innermost decoded document
      at which the error occurred.
    """

    def __init__(
        self,
        message: str,
        *,
        content_path: Iterable[str | int] = (),
        decode_path: Iterable[Iterable[str | int]] = (),
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.content_path = deque(content_path)
        self.decode_path = [tuple(layer) for layer in decode_path]

    @property
    def decode_depth(self) -> int:
        """
        The number of decoding layers between the instance and the error.
        """
        return len(self.decode_path) + 1

    @property
    def content_pointer(self) -> str:
        """
        The location of the error within the decoded document, as a pointer.
        """
        return _json_pointer(self.content_path)

    @property
    def decode_pointers(self) -> list[str]:
        """
        Each intermediate decode layer, rendered as a JSON Pointer.
        """
        return [_json_pointer(layer) for layer in self.decode_path]


class ContentDecodingError(ContentError):
    """
    The encoded string could not be decoded into a JSON document.
    """


class InvalidBase64PaddingError(ContentDecodingError):
    """
    The string's base64 padding is missing, incorrect or misplaced.
    """


class InvalidBase64DataError(ContentDecodingError):
    """
    The string contains characters outside the strict base64 alphabet.
    """


class InvalidUTF8Error(ContentDecodingError):
    """
    The decoded bytes are not valid UTF-8.

    Decoding never falls back to lossy replacement characters.
    """


class InvalidJSONError(ContentDecodingError):
    """
    The decoded text is not syntactically valid JSON.
    """


class DuplicateKeyError(ContentDecodingError):
    """
    The decoded JSON document contains a duplicate object key.
    """

    def __init__(self, message: str, *, key: str, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.key = key


class ContentTooLargeError(ContentDecodingError):
    """
    A single decoded string exceeds `ContentConfig.max_decoded_bytes`.
    """


class ContentDepthExceededError(ContentDecodingError):
    """
    Decoding would exceed `ContentConfig.max_depth` nested layers.
    """


class ContentBudgetExceededError(ContentDecodingError):
    """
    Decoding would exceed `ContentConfig.max_total_decoded_bytes`.
    """


class ContentSchemaError(ContentError):
    """
    The decoded document does not validate against `contentSchema`.

    The original error raised within the decoded document is available
    as the `cause` (and `__cause__`) of this error.
    """


class _DuplicateKey(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


class _Budget:
    """
    Mutable per-validation-run state shared across decoding layers.
    """

    def __init__(self, config: ContentConfig) -> None:
        self.depth = 0
        self.remaining_bytes = config.max_total_decoded_bytes


_BUDGET: ContextVar[_Budget | None] = ContextVar(
    "jsonschema_content_budget",
    default=None,
)


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _is_supported_media_type(media_type: Any) -> bool:
    if not isinstance(media_type, str):
        return False
    essence, _, parameters = media_type.partition(";")
    if essence.strip().lower() != "application/json":
        return False
    for parameter in parameters.split(";"):
        name, _, value = parameter.partition("=")
        if name.strip().lower() == "charset":
            return value.strip().strip('"').lower() == "utf-8"
    return True


def _b64decode(instance: str) -> bytes:
    try:
        data = instance.encode("ascii")
    except UnicodeEncodeError:
        raise InvalidBase64DataError(
            "Content is not valid base64: non-ASCII characters are present",
        ) from None
    try:
        return base64.b64decode(data, validate=True)
    except binascii.Error as error:
        reason = str(error)
        if "padding" in reason:
            raise InvalidBase64PaddingError(
                f"Content has invalid base64 padding: {reason}",
            ) from None
        raise InvalidBase64DataError(
            f"Content is not valid base64: {reason}",
        ) from None


def _utf8(data: bytes) -> str:
    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise InvalidUTF8Error(
            f"Decoded content is not valid UTF-8: {error}",
        ) from None


def _parse_json(text: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=_object_without_duplicates)
    except _DuplicateKey as error:
        raise DuplicateKeyError(
            f"Decoded JSON object contains the duplicate key {error.key!r}",
            key=error.key,
        ) from None
    except json.JSONDecodeError as error:
        raise InvalidJSONError(f"Content is not valid JSON: {error}") from None


def _validate_content(
    validator,
    content_schema,
    instance: str,
    config: ContentConfig,
    budget: _Budget,
    encoding: str | None,
):
    if budget.depth >= config.max_depth:
        yield ContentDepthExceededError(
            "Decoding the content would exceed the configured maximum "
            f"decode depth of {config.max_depth}",
        )
        return

    try:
        if encoding == "base64":
            decoded = _b64decode(instance)
        else:
            decoded = instance.encode("utf-8")
    except UnicodeEncodeError as error:
        yield InvalidUTF8Error(f"Content is not valid UTF-8: {error}")
        return
    except ContentDecodingError as error:
        yield error
        return

    size = len(decoded)
    if size > config.max_decoded_bytes:
        yield ContentTooLargeError(
            f"Decoded content is {size} bytes, which exceeds the "
            f"configured limit of {config.max_decoded_bytes} bytes",
        )
        return
    if size > budget.remaining_bytes:
        yield ContentBudgetExceededError(
            f"Decoding {size} bytes of content would exceed the "
            "configured total budget of "
            f"{config.max_total_decoded_bytes} decoded bytes",
        )
        return
    budget.remaining_bytes -= size

    try:
        text = _utf8(decoded) if encoding == "base64" else instance
        document = _parse_json(text)
    except ContentDecodingError as error:
        yield error
        return

    budget.depth += 1
    try:
        errors = validator.descend(instance=document, schema=content_schema)
        for error in errors:
            if isinstance(error, ContentError):
                # An error from a *nested* encoded string. The path it
                # accumulated so far is the location of that string
                # within the document we just decoded, so record it as
                # the next (outermost) decode layer and let the normal
                # machinery rebuild the outer instance path.
                error.decode_path.insert(0, tuple(error.path))
                error.path.clear()
                yield error
            else:
                yield ContentSchemaError(
                    error.message,
                    content_path=error.path,
                    cause=error,
                    context=error.context,
                    validator=error.validator,
                    validator_value=error.validator_value,
                    instance=error.instance,
                    schema=error.schema,
                    schema_path=error.schema_path,
                )
    finally:
        budget.depth -= 1


def _content_keyword(config: ContentConfig):
    def contentSchema(validator, content_schema, instance, schema):
        if not validator.is_type(instance, "string"):
            return
        if not _is_supported_media_type(schema.get("contentMediaType")):
            return
        encoding = schema.get("contentEncoding")
        if encoding is not None and encoding != "base64":
            # Unsupported encodings are never handed to arbitrary
            # decoders; they keep their annotation-only behavior.
            return

        budget = _BUDGET.get()
        token = None
        if budget is None:
            # Not running under extend_with_content_validation's
            # wrapper (e.g. an evolve()d underlying validator): fall
            # back to a budget scoped to this encoded string's subtree.
            budget = _Budget(config)
            token = _BUDGET.set(budget)
        try:
            yield from _validate_content(
                validator, content_schema, instance, config, budget, encoding,
            )
        finally:
            if token is not None:
                _BUDGET.reset(token)

    return contentSchema


def extend_with_content_validation(
    validator,
    *,
    config: ContentConfig | None = None,
):
    """
    Extend a validator class to validate the ``content*`` keywords.

    The returned class wraps an extension of `validator` (via
    `jsonschema.validators.extend`) and behaves exactly like it, except
    that schemas combining ``contentSchema`` with
    ``contentMediaType: application/json`` (and optionally
    ``contentEncoding: base64``) have the decoded content validated
    against ``contentSchema``. All other combinations of the
    ``content*`` keywords keep their default annotation-only behavior.

    Each call of `iter_errors` (or `validate` / `is_valid`) on the
    returned class gets a fresh decoding budget from `config`, shared
    by all (nested and sibling) encoded strings in the instance.

    Arguments:

        validator:

            the validator class to extend, e.g.
            `jsonschema.Draft202012Validator`

        config:

            a `ContentConfig` with the decoding limits shared by each
            validation run. Uses defaults if unprovided.

    Returns:

        a new validator class implementing
        `jsonschema.protocols.Validator` by composition

    .. code:: python

        import base64
        import json

        from jsonschema import Draft202012Validator
        from jsonschema.content import (
            ContentError,
            extend_with_content_validation,
        )

        Validator = extend_with_content_validation(Draft202012Validator)

        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 3},
                "payload": {
                    "type": "string",
                    "contentEncoding": "base64",
                    "contentMediaType": "application/json",
                    "contentSchema": {
                        "type": "object",
                        "required": ["id"],
                    },
                },
            },
        }
        instance = {
            "name": "x",  # too short: an ordinary validation error
            "payload": base64.b64encode(
                json.dumps({"other": 1}).encode(),
            ).decode(),  # missing "id": a content error
        }

        for error in Validator(schema).iter_errors(instance):
            if isinstance(error, ContentError):
                print("content error:", error.content_pointer)
            else:
                print("ordinary error:", list(error.path), error.message)

    The ordinary error (``minLength`` on ``name``) and the content
    error (``required`` within the decoded ``payload``) are reported
    side by side, and are told apart with `isinstance` against
    `ContentError`.
    """
    if config is None:
        config = ContentConfig()
    extended = _validators.extend(
        validator,
        validators={"contentSchema": _content_keyword(config)},
    )

    class ContentValidator:

        VALIDATORS = extended.VALIDATORS  # noqa: RUF012
        META_SCHEMA = extended.META_SCHEMA  # noqa: RUF012
        TYPE_CHECKER = extended.TYPE_CHECKER
        FORMAT_CHECKER = extended.FORMAT_CHECKER
        ID_OF = extended.ID_OF

        def __init__(self, schema, *args, **kwargs):
            self._validator = extended(schema, *args, **kwargs)

        @classmethod
        def check_schema(cls, schema, **kwargs):
            extended.check_schema(schema, **kwargs)

        def evolve(self, **changes):
            evolved = object.__new__(type(self))
            evolved._validator = self._validator.evolve(**changes)
            return evolved

        def iter_errors(self, instance):
            token = _BUDGET.set(_Budget(config))
            try:
                yield from self._validator.iter_errors(instance)
            finally:
                _BUDGET.reset(token)

        def validate(self, instance):
            with closing(self.iter_errors(instance)) as errors:
                for error in errors:
                    raise error

        def is_valid(self, instance):
            with closing(self.iter_errors(instance)) as errors:
                return next(errors, None) is None

        def __getattr__(self, name):
            return getattr(self._validator, name)

    ContentValidator.__name__ = ContentValidator.__qualname__ = (
        f"ContentValidating{validator.__name__}"
    )
    return ContentValidator
