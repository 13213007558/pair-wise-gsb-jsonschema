=========================
Offline Schema Bundles
=========================

In some environments -- for example production networks which are not
permitted to reach the sites hosting a schema's resources -- it is useful
to *collect* every resource an entry schema needs ahead of time, and to
*load* that collection later without any network access.

`jsonschema.SchemaBundle` provides exactly this pair of operations:

* at build time, `jsonschema.SchemaBundle.collect` follows an entry
  schema's :kw:`$ref`, :kw:`$dynamicRef` and :kw:`$recursiveRef`
  references -- across relative references, nested :kw:`$id`\ s,
  anchors and dynamic anchors -- retrieving each referenced resource
  through a caller-provided function, and records the resources, their
  content digests, the root entry point and the dependency edges between
  them,

* at run time, `jsonschema.SchemaBundle.registry` rebuilds a
  `referencing.Registry` purely from the bundle, after verifying that no
  resource is missing and that every resource still matches its recorded
  digest.

Resources keep their original absolute URIs as their identity; references
are never rewritten into a single merged document, so dynamic scope
(:kw:`$dynamicRef` / :kw:`$dynamicAnchor`) behaves exactly as it did
against the original registry.


Collecting a bundle
-------------------

.. code-block:: python

    from jsonschema import SchemaBundle

    def retrieve(uri):
        ...  # fetch and return the deserialized JSON for `uri`

    bundle = SchemaBundle.collect(
        "https://schemas.example.com/entry.json",
        retrieve,
    )

    import json
    with open("bundle.json", "w") as f:
        json.dump(bundle.to_json(), f)

The retrieval function is injected by the caller and is the only I/O
performed during collection.
Circular references are deduplicated, and if two different documents claim
the same canonical URI, collection refuses to guess and raises
`jsonschema.bundling.ConflictingResourceError`.

References which only participate through :kw:`$dynamicRef` dynamic
scope cannot always be discovered statically.
Any such reference is reported via
`jsonschema.SchemaBundle.dynamic_boundaries`; if validation relies on
additional resources providing a matching :kw:`$dynamicAnchor`, pass
their URIs explicitly via the `extra_uris` argument to
`jsonschema.SchemaBundle.collect`.


Validating offline
------------------

.. code-block:: python

    import json

    from jsonschema import Draft202012Validator, SchemaBundle

    with open("bundle.json") as f:
        bundle = SchemaBundle.from_json(json.load(f))

    validator = Draft202012Validator(
        bundle.contents_of(bundle.root_uri),
        registry=bundle.registry(),
    )
    validator.validate(instance)

Loading verifies the bundle before any validation happens:

* if a resource recorded in the bundle is missing (a dependency edge
  points nowhere), `jsonschema.bundling.BundleIncompleteError` is
  raised -- an unresolved reference is never silently treated as valid,
* if a resource's contents no longer match the recorded digest,
  `jsonschema.bundling.BundleTamperedError` is raised.

The registry returned by `jsonschema.SchemaBundle.registry` has no
retrieval callback: it will never attempt to access the network to fill
in gaps.


API
---

.. currentmodule:: jsonschema.bundling

.. autoclass:: jsonschema.SchemaBundle
    :members:

.. autoexception:: BundleError

.. autoexception:: BundleIncompleteError

.. autoexception:: BundleTamperedError

.. autoexception:: ConflictingResourceError
