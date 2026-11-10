=====================================
Directional (Read/Write) Validation
=====================================

.. currentmodule:: jsonschema

A single schema often describes both sides of an API: clients *write*
some fields (e.g. a password) while servers *read back* others (e.g. a
generated ID). JSON Schema expresses this with the :kw:`readOnly` and
:kw:`writeOnly` annotations, but by itself the specification treats
them as hints only -- standard validators ignore them.

`jsonschema.validators.directional_validator_for` (and the lower-level
`jsonschema.validators.directional_profile`) add an explicit
*direction* profile on top of any validator class:

* ``direction="write"`` validates client input: properties annotated
  ``readOnly: true`` must not be provided.
* ``direction="read"`` validates server output: properties annotated
  ``writeOnly: true`` must not be present.

.. code-block:: python

    from jsonschema.validators import directional_validator_for

    account_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "readOnly": True},
            "username": {"type": "string"},
            "password": {"type": "string", "writeOnly": True},
        },
        "required": ["id", "username", "password"],
    }

    create_account = directional_validator_for(account_schema, "write")
    read_account = create_account.evolve(direction="read")

    create_account.is_valid({"username": "janedoe", "password": "s3cret"})
    # True -- the server-generated "id" is not expected from clients

    create_account.is_valid({"id": "forged", "username": "j", "password": "x"})
    # False -- clients may not send readOnly fields

    read_account.is_valid({"id": "u-123", "username": "janedoe"})
    # True -- the password is not expected in responses

    read_account.is_valid({"id": "u-123", "username": "j", "password": "x"})
    # False -- responses must not leak writeOnly secrets

Direction constraints compose with the rest of the schema as follows:

``required``
    Only applies to properties which may appear in the current
    direction. A ``readOnly`` property is never required from a client,
    and a ``writeOnly`` property is never required in a response.
    Properties without direction annotations are required as usual.

Nesting, arrays and ``$ref``
    Direction checks run wherever the :kw:`properties` keyword applies
    during normal validation, so nested objects and array items are
    covered, and annotations declared on a schema reached through
    :kw:`$ref` are honored.

Conditional subschemas
    :kw:`if` / :kw:`then` / :kw:`else`, :kw:`anyOf`, :kw:`oneOf` and
    :kw:`allOf` only contribute direction constraints when they
    actually apply to the instance being validated. A ``readOnly``
    declaration inside a branch which does not apply has no effect.

Conflicting annotations
    Declaring both ``readOnly: true`` and ``writeOnly: true`` on the
    same property is a configuration error: validation raises
    `jsonschema.exceptions.SchemaError` rather than silently picking a
    side.

Validation only ever reports errors (which carry the direction in their
message and the usual instance ``path``); validated instances are never
modified. Validators created without a direction, including all of the
standard versioned validators, behave exactly as before.

.. autofunction:: jsonschema.validators.directional_validator_for
    :noindex:

.. autofunction:: jsonschema.validators.directional_profile
    :noindex:
