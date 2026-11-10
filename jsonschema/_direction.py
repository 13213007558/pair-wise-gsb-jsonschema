"""
Directional (read/write) validation profiles.

This module implements explicit *direction* profiles on top of the
standard validators, allowing a single schema to validate both client
requests (the ``"write"`` direction) and server responses (the
``"read"`` direction):

* in the ``"write"`` direction, properties annotated ``readOnly: true``
  must not be provided by the client
* in the ``"read"`` direction, properties annotated ``writeOnly: true``
  must not be returned by the server

The rules compose with the rest of the schema as follows:

* ``required`` only applies to properties which may appear in the
  current direction. A ``readOnly`` property is never required from a
  client, and a ``writeOnly`` property is never required in a response.
  Properties without direction annotations are required as usual.
* Nested objects, arrays and ``$ref`` behave as usual: direction checks
  run wherever the ``properties`` keyword applies during normal
  validation, and annotations declared on a schema reached through
  ``$ref`` are honored.
* Conditional subschemas (``if`` / ``then`` / ``else``, ``anyOf``,
  ``oneOf``, ``allOf``) only contribute direction constraints when they
  actually apply to the instance being validated. A ``readOnly``
  declaration inside a branch which does not apply has no effect.
* Declaring both ``readOnly: true`` and ``writeOnly: true`` on the same
  property is a configuration error and raises
  `jsonschema.exceptions.SchemaError` rather than silently picking a
  side.

Validation only ever reports errors; instances are never modified.

Standard validators are completely unaffected: the keyword functions
here are only used by directional profiles created via
`jsonschema.validators.directional_profile`, and they delegate to the
standard behavior whenever no direction is set on the validator.
"""

from __future__ import annotations

import referencing.exceptions

from jsonschema import _keywords, exceptions

READ = "read"
WRITE = "write"
DIRECTIONS = (READ, WRITE)


def _annotations(validator, subschema):
    """
    Return the effective ``(readOnly, writeOnly)`` annotations.

    Annotations are read from the subschema itself, following ``$ref``
    chains (via the validator's resolver) so that e.g.
    ``{"$ref": "#/$defs/server_id"}`` picks up annotations declared on
    the referenced schema. Unresolvable references are left to normal
    ``$ref`` validation to report.
    """
    read_only = write_only = False
    seen = set()
    current = subschema
    while isinstance(current, dict):
        read_only = bool(current.get("readOnly", False))
        write_only = bool(current.get("writeOnly", False))
        if read_only or write_only:
            break
        ref = current.get("$ref")
        if not isinstance(ref, str) or ref in seen:
            break
        seen.add(ref)
        resolver = getattr(validator, "_resolver", None)
        if resolver is None:
            break
        try:
            resolved = resolver.lookup(ref)
        except referencing.exceptions.Unresolvable:
            break
        current = resolved.contents
    return read_only, write_only


def _check_conflict(property, read_only, write_only):
    if read_only and write_only:
        raise exceptions.SchemaError(
            f"{property!r} is annotated both readOnly and writeOnly, "
            "so it can never appear in either direction. "
            "Fix the schema to pick exactly one direction.",
        )


def _disallowed_error(property, direction):
    if direction == WRITE:
        reason = "read-only and must not be provided by a client"
    else:
        reason = "write-only and must not be returned by a server"
    return exceptions.ValidationError(
        f"{property!r} is {reason} ({direction} direction)",
        path=[property],
    )


def properties(validator, properties, instance, schema):
    """
    Validate ``properties``, enforcing the direction of the validator.

    Behaves exactly like the standard ``properties`` keyword when the
    validator has no direction set.
    """
    direction = validator.direction
    if direction is None:
        yield from _keywords.properties(validator, properties, instance, schema)
        return

    if not validator.is_type(instance, "object"):
        return

    for property, subschema in properties.items():
        read_only, write_only = _annotations(validator, subschema)
        _check_conflict(property, read_only, write_only)

        if property not in instance:
            continue
        if direction == WRITE and read_only:
            yield _disallowed_error(property, direction)
            continue
        if direction == READ and write_only:
            yield _disallowed_error(property, direction)
            continue
        yield from validator.descend(
            instance[property],
            subschema,
            path=property,
            schema_path=property,
        )


def required(validator, required, instance, schema):
    """
    Validate ``required``, skipping properties disallowed in this direction.

    A property which may not appear in the validator's direction (a
    ``readOnly`` property in the write direction, or a ``writeOnly``
    property in the read direction) is not required. Behaves exactly
    like the standard ``required`` keyword when the validator has no
    direction set.
    """
    direction = validator.direction
    if direction is None:
        yield from _keywords.required(validator, required, instance, schema)
        return

    if not validator.is_type(instance, "object"):
        return

    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        properties = {}

    for property in required:
        subschema = properties.get(property)
        if subschema is not None:
            read_only, write_only = _annotations(validator, subschema)
            _check_conflict(property, read_only, write_only)
            if direction == WRITE and read_only:
                continue
            if direction == READ and write_only:
                continue
        if property not in instance:
            yield exceptions.ValidationError(
                f"{property!r} is a required property",
            )
