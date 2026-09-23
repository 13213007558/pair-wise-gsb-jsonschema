"""
Opt-in validation of the JSON Schema ``content*`` keywords.

By default, `jsonschema` follows the JSON Schema specification and treats
:kw:`contentEncoding`, :kw:`contentMediaType` and :kw:`contentSchema` as
annotations which do not affect validation in any way.

This module provides an explicitly opt-in extension,
`extend_with_content_validation`, which creates a validator class that
enforces them for the supported combination of::

    "contentEncoding": "base64",
    "contentMediaType": "application/json"

For such strings, the extension strictly base64-decodes the instance,
strictly UTF-8-decodes and JSON-parses the result (rejecting duplicate
object keys rather than silently keeping the last one), and then validates
the decoded document against the schema's :kw:`contentSchema`.

Only the combination above is interpreted. No other content encodings or
media types are handled, and nothing is ever retrieved over the network --
the content string itself is never treated as a URI. References inside
:kw:`contentSchema` resolve against the schema resource they appear in,
exactly as references anywhere else in the schema do.

Instances are never mutated: the original string stays a string, whether
or not its decoded content is valid.
"""

from __future__ import annotations

from collections import deque
from contextvars import ContextVar
from typing import NamedTuple
import base64
import binascii
import json

from jsonschema.exceptions import ValidationError
from jsonschema.validators import extend

__all__ = ["ContentValidationError", "extend_with_content_validation"]


_CONTENT_KEYWORDS = frozenset(
    ["contentEncoding", "contentMediaType", "contentSchema"],
)


def _escape_pointer_segment(segment):
    return str(segment).replace("~", "~0").replace("/", "~1")


class ContentValidationError(ValidationError):
    """
    The decoded content of a string instance failed validation.

    This is a subclass of `jsonschema.ValidationError`, so existing
    ``except ValidationError`` handlers and error-reporting code keep
    working. Callers can tell decoded-content errors apart from ordinary
    ones with ``isinstance(error, ContentValidationError)``.

    In addition to the usual error attributes:

    * `error.path` still locates the *string* within the original
      (encoded) instance, as usual.

    * `error.kind` says what went wrong; one of the class-level
      constants below.

    * `error.content_layers` locates the error within the decoded
      content. It holds one tuple of JSON-pointer segments per decoding
      layer that was entered: ``(("nested",), ("token",))`` means the
      first decoded document had a content string at ``/nested`` whose
      own decoded document failed at ``/token``.
    """

    INVALID_BASE64 = "invalid-base64"
    INVALID_UTF8 = "invalid-utf-8"
    INVALID_JSON = "invalid-json"
    DUPLICATE_KEY = "duplicate-key"
    TOO_LARGE = "content-too-large"
    BUDGET_EXCEEDED = "decoded-byte-budget-exceeded"
    DEPTH_EXCEEDED = "max-decode-depth-exceeded"
    SCHEMA = "content-schema"

    def __init__(self, message, *, kind, content_layers=(), **kwargs):
        super().__init__(message, **kwargs)
        self.kind = kind
        self.content_layers = tuple(tuple(layer) for layer in content_layers)

    @property
    def content_layer(self):
        """
        The decoding layer the error occurred in.

        Layer ``0`` is the encoded string instance itself, layer ``1`` is
        the document obtained by decoding it, and so on.
        """
        return len(self.content_layers)

    @property
    def content_pointers(self):
        """
        JSON Pointers locating the error within each decoded document.

        One pointer is returned per decoding layer, outermost first.
        """
        return tuple(
            "".join(
                "/" + _escape_pointer_segment(segment) for segment in layer
            )
            for layer in self.content_layers
        )


class _DuplicateKey(Exception):
    def __init__(self, key):
        super().__init__(key)
        self.key = key


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


class _ContentConfig(NamedTuple):
    max_content_bytes: int
    max_decode_depth: int
    max_total_decoded_bytes: int


class _ContentContext:
    """
    Decoding state shared by one tree of nested content validations.
    """

    __slots__ = ("depth", "remaining_bytes")

    def __init__(self, remaining_bytes):
        self.depth = 0
        self.remaining_bytes = remaining_bytes


_ACTIVE_CONTEXT = ContextVar(
    "jsonschema.content.context",
    default=None,
)


def _passes_own_constraints(validator, instance, schema):
    """
    Check the string against its own ordinary (non-content) constraints.
    """
    siblings = {
        key: value
        for key, value in schema.items()
        if key not in _CONTENT_KEYWORDS
    }
    if not siblings:
        return True
    return next(validator.descend(instance, siblings), None) is None


def _as_content_error(error):
    """
    Re-express an error from within a decoded document as a content error.

    The error's (document-relative) path becomes the outermost decoding
    layer, leaving `error.path` to be populated with the location of
    the encoded string by the normal validation machinery.
    """
    layer = tuple(error.path)
    context = [_as_content_error(each) for each in error.context]
    if isinstance(error, ContentValidationError):
        error.path = error.relative_path = deque()
        error.content_layers = (layer, *error.content_layers)
        error.context = context
        for each in context:
            each.parent = error
        return error
    return ContentValidationError(
        error.message,
        kind=ContentValidationError.SCHEMA,
        content_layers=(layer,),
        validator=error.validator,
        validator_value=error.validator_value,
        instance=error.instance,
        schema=error.schema,
        schema_path=error.schema_path,
        cause=error.cause,
        context=context,
    )


def _decode_and_validate(validator, instance, schema, context, config):
    try:
        raw = base64.b64decode(instance, validate=True)
    except (binascii.Error, ValueError) as error:
        yield ContentValidationError(
            f"Content is not valid base64 ({error})",
            kind=ContentValidationError.INVALID_BASE64,
            instance=instance,
            schema=schema,
        )
        return

    if len(raw) > config.max_content_bytes:
        yield ContentValidationError(
            f"Decoded content is {len(raw)} bytes, exceeding the "
            f"configured maximum of {config.max_content_bytes} bytes",
            kind=ContentValidationError.TOO_LARGE,
            instance=instance,
            schema=schema,
        )
        return

    context.remaining_bytes -= len(raw)
    if context.remaining_bytes < 0:
        yield ContentValidationError(
            "Decoding content exceeds the configured total decoded "
            f"byte budget of {config.max_total_decoded_bytes} bytes",
            kind=ContentValidationError.BUDGET_EXCEEDED,
            instance=instance,
            schema=schema,
        )
        return

    if schema.get("contentMediaType") != "application/json":
        # Only the encoding itself is checkable for other media types.
        return

    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        yield ContentValidationError(
            f"Decoded content is not valid UTF-8 ({error})",
            kind=ContentValidationError.INVALID_UTF8,
            instance=instance,
            schema=schema,
        )
        return

    try:
        document = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except _DuplicateKey as error:
        yield ContentValidationError(
            f"Decoded content contains a duplicate key {error.key!r}",
            kind=ContentValidationError.DUPLICATE_KEY,
            instance=instance,
            schema=schema,
        )
        return
    except json.JSONDecodeError as error:
        yield ContentValidationError(
            f"Decoded content is not valid JSON ({error})",
            kind=ContentValidationError.INVALID_JSON,
            instance=instance,
            schema=schema,
        )
        return

    content_schema = schema.get("contentSchema")
    if content_schema is None:
        return

    for error in validator.descend(document, content_schema):
        yield _as_content_error(error)


def _check_content(validator, instance, schema, config):
    if schema.get("contentEncoding") != "base64":
        return
    if not validator.is_type(instance, "string"):
        return

    # The string itself is checked against its own ordinary constraints
    # first; only if it passes do we look at its decoded content.
    if not _passes_own_constraints(validator, instance, schema):
        return

    context = _ACTIVE_CONTEXT.get()
    owns_context = context is None
    if owns_context:
        context = _ContentContext(config.max_total_decoded_bytes)
        token = _ACTIVE_CONTEXT.set(context)
    try:
        if context.depth >= config.max_decode_depth:
            yield ContentValidationError(
                "Content exceeds the configured maximum decoding "
                f"depth of {config.max_decode_depth}",
                kind=ContentValidationError.DEPTH_EXCEEDED,
                instance=instance,
                schema=schema,
            )
            return
        context.depth += 1
        try:
            yield from _decode_and_validate(
                validator,
                instance,
                schema,
                context,
                config,
            )
        finally:
            context.depth -= 1
    finally:
        if owns_context:
            _ACTIVE_CONTEXT.reset(token)


def extend_with_content_validation(
    Validator,
    *,
    max_content_bytes=2**20,
    max_decode_depth=4,
    max_total_decoded_bytes=4 * 2**20,
):
    """
    Create a validator class which enforces the ``content*`` keywords.

    The returned class behaves exactly like the one it is given, except
    that strings declaring ``contentEncoding: base64`` are strictly
    decoded, and -- when additionally declaring
    ``contentMediaType: application/json`` -- their decoded JSON document
    is checked for validity and then validated against the schema's
    :kw:`contentSchema`. All other encodings and media types remain mere
    annotations, as does the whole trio of keywords for any validator
    which has not been extended.

    Content failures are reported as `ContentValidationError`, a
    `jsonschema.ValidationError` subclass, so callers can distinguish
    them from ordinary errors on the raw instance::

        import base64
        import json

        from jsonschema import Draft202012Validator
        from jsonschema.content import (
            ContentValidationError,
            extend_with_content_validation,
        )

        ContentValidator = extend_with_content_validation(
            Draft202012Validator,
        )

        schema = {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "minimum": 0},
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
            "count": -1,
            "payload": base64.b64encode(
                json.dumps({"name": "x"}).encode(),
            ).decode(),
        }

        for error in ContentValidator(schema).iter_errors(instance):
            if isinstance(error, ContentValidationError):
                print("content:", error.kind, error.content_pointers)
            else:
                print("ordinary:", error.validator, list(error.path))

    which prints::

        ordinary: minimum ['count']
        content: content-schema ('',)

    -- one ordinary error on the raw instance (``count`` is negative),
    and one error on the decoded content of ``payload`` (its document
    is missing ``id`` at its root, i.e. JSON pointer ``''``).

    Arguments:

        Validator (jsonschema.protocols.Validator):

            the validator class to extend

        max_content_bytes (int):

            the maximum allowed size of any single decoded content
            string, in bytes

        max_decode_depth (int):

            the maximum allowed nesting of content strings within
            decoded documents

        max_total_decoded_bytes (int):

            the total decoded byte budget shared by one tree of nested
            content validations

    """
    if max_decode_depth < 1:
        raise ValueError("max_decode_depth must be at least 1")
    config = _ContentConfig(
        max_content_bytes=max_content_bytes,
        max_decode_depth=max_decode_depth,
        max_total_decoded_bytes=max_total_decoded_bytes,
    )

    def contentEncoding(validator, contentEncoding, instance, schema):
        if "contentSchema" in schema:
            # The contentSchema driver performs the combined checks.
            return
        yield from _check_content(validator, instance, schema, config)

    def contentSchema(validator, contentSchema, instance, schema):
        yield from _check_content(validator, instance, schema, config)

    return extend(
        Validator,
        validators={
            "contentEncoding": contentEncoding,
            "contentSchema": contentSchema,
        },
    )
