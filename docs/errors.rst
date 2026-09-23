==========================
Handling Validation Errors
==========================

.. currentmodule:: jsonschema.exceptions

When an invalid instance is encountered, a `ValidationError` will be
raised or returned, depending on which method or function is used.

.. autoexception:: ValidationError
    :noindex:

    The information carried by an error roughly breaks down into:

    ===============  =================  ========================
     What Happened   Why Did It Happen  What Was Being Validated
    ===============  =================  ========================
    `message`        `context`          `instance`

                     `cause`            `json_path`

                                        `path`

                                        `schema`

                                        `schema_path`

                                        `validator`

                                        `validator_value`
    ===============  =================  ========================


    .. attribute:: message

        A human readable message explaining the error.

    .. attribute:: validator

        The name of the failed `keyword
        <https://json-schema.org/draft/2020-12/json-schema-validation.html#name-a-vocabulary-for-structural>`_.

    .. attribute:: validator_value

        The associated value for the failed keyword in the schema.

    .. attribute:: schema

        The full schema that this error came from. This is potentially a
        subschema from within the schema that was passed in originally,
        or even an entirely different schema if a :kw:`$ref` was
        followed.

    .. attribute:: relative_schema_path

        A `collections.deque` containing the path to the failed keyword
        within the schema.

    .. attribute:: absolute_schema_path

        A `collections.deque` containing the path to the failed
        keyword within the schema, but always relative to the
        *original* schema as opposed to any subschema (i.e. the one
        originally passed into a validator class, *not* `schema`\).

    .. attribute:: schema_path

        Same as `relative_schema_path`.

    .. attribute:: relative_path

        A `collections.deque` containing the path to the
        offending element within the instance. The deque can be empty if
        the error happened at the root of the instance.

    .. attribute:: absolute_path

        A `collections.deque` containing the path to the
        offending element within the instance. The absolute path
        is always relative to the *original* instance that was
        validated (i.e. the one passed into a validation method, *not*
        `instance`\). The deque can be empty if the error happened
        at the root of the instance.

    .. attribute:: json_path

        A `JSON path <https://goessner.net/articles/JsonPath/index.html>`_
        to the offending element within the instance.

    .. attribute:: path

        Same as `relative_path`.

    .. attribute:: instance

        The instance that was being validated. This will differ from
        the instance originally passed into ``validate`` if the
        validator object was in the process of validating a (possibly
        nested) element within the top-level instance. The path within
        the top-level instance (i.e. `ValidationError.path`) could
        be used to find this object, but it is provided for convenience.

    .. attribute:: context

        If the error was caused by errors in subschemas, the list of errors
        from the subschemas will be available on this property. The
        `schema_path` and `path` of these errors will be relative
        to the parent error.

    .. attribute:: cause

        If the error was caused by a *non*-validation error, the
        exception object will be here. Currently this is only used
        for the exception raised by a failed format checker in
        `jsonschema.FormatChecker.check`.

    .. attribute:: parent

        A validation error which this error is the `context` of.
        ``None`` if there wasn't one.


In case an invalid schema itself is encountered, a `SchemaError` is
raised.

.. autoexception:: SchemaError
    :noindex:

    The same attributes are present as for `ValidationError`\s.


These attributes can be clarified with a short example:

.. testcode::

    schema = {
        "items": {
            "anyOf": [
                {"type": "string", "maxLength": 2},
                {"type": "integer", "minimum": 5}
            ]
        }
    }
    instance = [{}, 3, "foo"]
    v = Draft202012Validator(schema)
    errors = sorted(v.iter_errors(instance), key=lambda e: e.path)

The error messages in this situation are not very helpful on their own.

.. testcode::

    for error in errors:
        print(error.message)

outputs:

.. testoutput::

    {} is not valid under any of the given schemas
    3 is not valid under any of the given schemas
    'foo' is not valid under any of the given schemas

If we look at `ValidationError.path` on each of the errors, we can find
out which elements in the instance correspond to each of the errors. In
this example, `ValidationError.path` will have only one element, which
will be the index in our list.

.. testcode::

    for error in errors:
        print(list(error.path))

.. testoutput::

    [0]
    [1]
    [2]

Since our schema contained nested subschemas, it can be helpful to look at
the specific part of the instance and subschema that caused each of the errors.
This can be seen with the `ValidationError.instance` and
`ValidationError.schema` attributes.

With keywords like :kw:`anyOf`, the `ValidationError.context`
attribute can be used to see the sub-errors which caused the failure. Since
these errors actually came from two separate subschemas, it can be helpful to
look at the `ValidationError.schema_path` attribute as well to see where
exactly in the schema each of these errors come from. In the case of sub-errors
from the `ValidationError.context` attribute, this path will be relative
to the `ValidationError.schema_path` of the parent error.

.. testcode::

    for error in errors:
        for suberror in sorted(error.context, key=lambda e: e.schema_path):
            print(list(suberror.schema_path), suberror.message, sep=", ")

.. testoutput::

    [0, 'type'], {} is not of type 'string'
    [1, 'type'], {} is not of type 'integer'
    [0, 'type'], 3 is not of type 'string'
    [1, 'minimum'], 3 is less than the minimum of 5
    [0, 'maxLength'], 'foo' is too long
    [1, 'type'], 'foo' is not of type 'integer'

The string representation of an error combines some of these attributes for
easier debugging.

.. testcode::

    print(errors[1])

.. testoutput::

    3 is not valid under any of the given schemas

    Failed validating 'anyOf' in schema['items']:
        {'anyOf': [{'type': 'string', 'maxLength': 2},
                   {'type': 'integer', 'minimum': 5}]}

    On instance[1]:
        3


ErrorTrees
----------

If you want to programmatically query which validation keywords
failed when validating a given instance, you may want to do so using
`jsonschema.exceptions.ErrorTree` objects.

.. autoclass:: jsonschema.exceptions.ErrorTree
    :noindex:
    :members:
    :special-members:
    :exclude-members: __dict__,__weakref__

    .. attribute:: errors

        The mapping of validation keywords to the error objects (usually `jsonschema.exceptions.ValidationError`\s) at this level of the tree.

Consider the following example:

.. testcode::

    schema = {
        "type" : "array",
        "items" : {"type" : "number", "enum" : [1, 2, 3]},
        "minItems" : 3,
    }
    instance = ["spam", 2]

For clarity's sake, the given instance has three errors under this schema:

.. testcode::

    v = Draft202012Validator(schema)
    for error in sorted(v.iter_errors(["spam", 2]), key=str):
        print(error.message)

.. testoutput::

    'spam' is not of type 'number'
    'spam' is not one of [1, 2, 3]
    ['spam', 2] is too short

Let's construct an `jsonschema.exceptions.ErrorTree` so that we
can query the errors a bit more easily than by just iterating over the
error objects.

.. testcode::

    from jsonschema.exceptions import ErrorTree
    tree = ErrorTree(v.iter_errors(instance))

As you can see, `jsonschema.exceptions.ErrorTree` takes an iterable of `ValidationError`\s when constructing a tree so you can directly pass it the return value of a validator's `jsonschema.protocols.Validator.iter_errors` method.

`ErrorTree`\s support a number of useful operations. The first one we
might want to perform is to check whether a given element in our instance
failed validation. We do so using the :keyword:`in` operator:

.. doctest::

    >>> 0 in tree
    True

    >>> 1 in tree
    False

The interpretation here is that the 0th index into the instance (``"spam"``)
did have an error (in fact it had 2), while the 1th index (``2``) did not (i.e.
it was valid).

If we want to see which errors a child had, we index into the tree and look at
the `ErrorTree.errors` attribute.

.. doctest::

    >>> sorted(tree[0].errors)
    ['enum', 'type']

Here we see that the :kw:`enum` and :kw:`type` keywords failed for
index ``0``. In fact `ErrorTree.errors` is a dict, whose values are the
`ValidationError`\s, so we can get at those directly if we want them.

.. doctest::

    >>> print(tree[0].errors["type"].message)
    'spam' is not of type 'number'

Of course this means that if we want to know if a given validation
keyword failed for a given index, we check for its presence in
`ErrorTree.errors`:

.. doctest::

    >>> "enum" in tree[0].errors
    True

    >>> "minimum" in tree[0].errors
    False

Finally, if you were paying close enough attention, you'll notice that
we haven't seen our :kw:`minItems` error appear anywhere yet. This is
because :kw:`minItems` is an error that applies globally to the instance
itself. So it appears in the root node of the tree.

.. doctest::

    >>> "minItems" in tree.errors
    True

That's all you need to know to use error trees.

To summarize, each tree contains child trees that can be accessed by
indexing the tree to get the corresponding child tree for a given
index into the instance. Each tree and child has a `ErrorTree.errors`
attribute, a dict, that maps the failed validation keyword to the
corresponding validation error.


best_match and relevance
------------------------

The `best_match` function is a simple but useful function for attempting
to guess the most relevant error in a given bunch.

.. doctest::

        >>> from jsonschema import Draft202012Validator
        >>> from jsonschema.exceptions import best_match

        >>> schema = {
        ...     "type": "array",
        ...     "minItems": 3,
        ... }
        >>> print(best_match(Draft202012Validator(schema).iter_errors(11)).message)
        11 is not of type 'array'


.. autofunction:: best_match
    :noindex:


.. function:: relevance(validation_error)
    :noindex:

    A key function that sorts errors based on heuristic relevance.

    If you want to sort a bunch of errors entirely, you can use
    this function to do so. Using this function as a key to e.g.
    `sorted` or `max` will cause more relevant errors to be
    considered greater than less relevant ones.

    Within the different validation keywords that can fail, this
    function considers :kw:`anyOf` and :kw:`oneOf` to be *weak*
    validation errors, and will sort them lower than other errors at the
    same level in the instance.

    If you want to change the set of weak [or strong] validation
    keywords you can create a custom version of this function with
    `by_relevance` and provide a different set of each.

.. doctest::

    >>> schema = {
    ...     "properties": {
    ...         "name": {"type": "string"},
    ...         "phones": {
    ...             "properties": {
    ...                 "home": {"type": "string"}
    ...             },
    ...         },
    ...     },
    ... }
    >>> instance = {"name": 123, "phones": {"home": [123]}}
    >>> errors = Draft202012Validator(schema).iter_errors(instance)
    >>> [
    ...     e.path[-1]
    ...     for e in sorted(errors, key=exceptions.relevance)
    ... ]
    ['home', 'name']


.. autofunction:: by_relevance
    :noindex:


Configurable relevance strategies
---------------------------------

The `by_relevance` function returns a `RelevanceStrategy`. The strategy
scores errors without mutating them, so `ValidationError.context`, `cause`,
`json_path`, and the rest of the original error tree remain intact. In
particular, equal errors from different :kw:`anyOf` or :kw:`oneOf`
branches are never merged or removed.

The primary ranking contains these weighted criteria:

======================  ========  =====================================
Criterion               Default   Preference
======================  ========  =====================================
instance_path           1000000   Shallower, then lexicographically
                                  earlier absolute instance path
custom                  1000      Higher value from custom_weight
validator_priority      100       Higher configured keyword priority
error_depth             10        Shallower in the error context tree
type_match              1         The schema's declared type matches the
                                  instance
schema_path             0         Shallower, then lexicographically
                                  earlier absolute schema path
======================  ========  =====================================

Criteria are ordered by descending weight. Equal weights are ordered by
criterion name to keep the ranking deterministic. Set a weight to zero to
remove that criterion from the primary ranking.

If the weighted criteria tie, the following deterministic tie-breakers are
used in order:

#. schema_path, when its primary weight is zero
#. error_depth, when its primary weight is zero
#. context_size
#. cause type name
#. a structural key containing the instance, schema, and validator value
#. input order, for errors which are structurally indistinguishable

Error messages are never used for ranking or tie-breaking. The structural
payload key is computed lazily, only if all earlier criteria tie.

Every score exposes its ordered criteria through
`RelevanceScore.explanation`, making the chosen order inspectable:

.. doctest::

    >>> import pprint
    >>> from jsonschema import Draft202012Validator, exceptions
    >>> from jsonschema.exceptions import best_match
    >>> strategy = exceptions.by_relevance(
    ...     weights=exceptions.RelevanceWeights(schema_path=10),
    ...     validator_priority={"required": 10},
    ... )
    >>> error = next(Draft202012Validator({"required": ["x"]}).iter_errors({}))
    >>> names = [criterion.name for criterion in strategy(error).explanation]
    >>> pprint.pprint(names)
    ['instance_path',
     'custom',
     'validator_priority',
     'error_depth',
     'schema_path',
     'type_match',
     'context_size',
     'cause',
     'payload',
     'input_order']

Pass the strategy as the `best_match` key to use the same ordering while
descending into error contexts:

.. doctest::

    >>> schema = {"anyOf": [{"type": "string"}, {"type": "integer"}]}
    >>> errors = Draft202012Validator(schema).iter_errors([])
    >>> best_match(errors, key=strategy).validator
    'type'

By default, equally ranked context errors return their parent error, which
preserves the historical union-error behavior. Set `context_tie` to
"best" to descend to the deterministic best child instead.

.. autoclass:: RelevanceWeights
    :noindex:

.. autoclass:: RelevanceStrategy
    :noindex:

.. autoclass:: RelevanceScore
    :noindex:

.. autoclass:: RelevanceCriterion
    :noindex:
