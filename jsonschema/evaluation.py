"""
Explicit evaluation of instances against JSON Schema Draft 2020-12.

Unlike `jsonschema.Validator.iter_errors`, which only yields failures,
evaluating an instance here produces a complete, JSON-serializable result
tree. Each node of the tree records where the evaluation happened (both the
instance location, as a JSON Pointer, and the schema location, including the
canonical resource location across `$ref` / `$dynamicRef`), whether it
succeeded, which errors explain a rejection, and which annotations (such as
the set of evaluated properties or items) were effectively produced.

Evaluation is a single pass: the tree is built while validating, so custom
keywords participate directly in the one evaluation instead of being
re-run afterwards to produce a report.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import unquote, urldefrag, urljoin
import re

from attrs import define, field
from jsonschema_specifications import REGISTRY as SPECIFICATIONS
import referencing.exceptions
import referencing.jsonschema

from jsonschema import _keywords, _types, _utils, exceptions

__all__ = [
    "Draft202012Evaluator",
    "EvaluationNode",
    "KeywordContext",
    "KeywordResult",
    "evaluate",
]

_SPECIFICATION = referencing.jsonschema.DRAFT202012

_EVALUATED_PROPERTIES = "evaluatedProperties"
_EVALUATED_ITEMS = "evaluatedItems"
_EVALUATED_ANNOTATIONS = {_EVALUATED_PROPERTIES, _EVALUATED_ITEMS}


def _escape(segment):
    return str(segment).replace("~", "~0").replace("/", "~1")


def _unescape(segment):
    return segment.replace("~1", "/").replace("~0", "~")


def _pointer(path):
    if not path:
        return ""
    return "/" + "/".join(_escape(each) for each in path)


def _absolute(base_uri, path):
    return f"{base_uri}#{_pointer(path)}"


@define
class EvaluationNode:
    """
    One node in an evaluation result tree.

    A node corresponds either to the application of a (sub)schema to an
    instance (``keyword`` is `None`) or to the application of a single
    keyword within that schema.

    All locations are JSON Pointers. ``schema_location`` is relative to the
    schema resource the node lives in, while ``absolute_schema_location``
    includes the canonical URI of that resource, so nodes reached through
    ``$ref`` / ``$dynamicRef`` record both the referencing location (on the
    reference keyword node) and the real resource location (on the child
    node of the referenced schema).
    """

    valid: bool
    instance_location: str
    schema_location: str
    absolute_schema_location: str
    keyword: str | None = None
    errors: list[str] = field(factory=list)
    annotations: dict[str, Any] = field(factory=dict)
    children: list[EvaluationNode] = field(factory=list)
    _evaluated_properties: set[str] = field(
        init=False, factory=set, repr=False, eq=False,
    )
    _evaluated_items: set[int] = field(
        init=False, factory=set, repr=False, eq=False,
    )

    def _freeze_annotations(self):
        if self._evaluated_properties:
            self.annotations[_EVALUATED_PROPERTIES] = sorted(
                self._evaluated_properties,
            )
        if self._evaluated_items:
            self.annotations[_EVALUATED_ITEMS] = sorted(self._evaluated_items)

    def to_dict(self):
        """
        Return a JSON-serializable representation of the result tree.
        """
        return {
            "valid": self.valid,
            "keyword": self.keyword,
            "instanceLocation": self.instance_location,
            "schemaLocation": self.schema_location,
            "absoluteSchemaLocation": self.absolute_schema_location,
            "errors": list(self.errors),
            "annotations": dict(self.annotations),
            "children": [child.to_dict() for child in self.children],
        }


@define
class KeywordResult:
    """
    The outcome of a custom keyword, returned to the evaluator.

    Custom keyword callables may return one of these (or `None`, which is
    equivalent to a valid result with no annotations) to attach errors,
    annotations and child nodes to their keyword node in the result tree.
    Returning the annotation names ``evaluatedProperties`` or
    ``evaluatedItems`` makes the custom keyword participate in
    ``unevaluatedProperties`` / ``unevaluatedItems`` computations.
    """

    valid: bool = True
    errors: list[str] = field(factory=list)
    annotations: dict[str, Any] = field(factory=dict)
    children: list[EvaluationNode] = field(factory=list)


@define
class KeywordContext:
    """
    The context passed to custom keyword callables during evaluation.
    """

    evaluator: Draft202012Evaluator
    instance_path: tuple
    schema_path: tuple
    base_uri: str
    _resolver: Any = field(repr=False)

    @property
    def instance_location(self):
        """
        The JSON Pointer to the instance currently being evaluated.
        """
        return _pointer(self.instance_path)

    @property
    def schema_location(self):
        """
        The JSON Pointer to the schema object containing the keyword.
        """
        return _pointer(self.schema_path)

    def descend(self, instance, schema, path=None, schema_path=None):
        """
        Evaluate a subschema against (part of) the instance.

        Returns the child `EvaluationNode` so it can be attached to a
        `KeywordResult`.
        """
        return self.evaluator._evaluate_schema(
            schema,
            instance,
            resolver=self._resolver,
            instance_path=(
                self.instance_path if path is None
                else (*self.instance_path, path)
            ),
            schema_path=(
                self.schema_path if schema_path is None
                else (*self.schema_path, schema_path)
            ),
            base_uri=self.base_uri,
        )


_DELEGATED = {
    "const": _keywords.const,
    "dependentRequired": _keywords.dependentRequired,
    "enum": _keywords.enum,
    "exclusiveMaximum": _keywords.exclusiveMaximum,
    "exclusiveMinimum": _keywords.exclusiveMinimum,
    "format": _keywords.format,
    "maxItems": _keywords.maxItems,
    "maxLength": _keywords.maxLength,
    "maxProperties": _keywords.maxProperties,
    "maximum": _keywords.maximum,
    "minItems": _keywords.minItems,
    "minLength": _keywords.minLength,
    "minProperties": _keywords.minProperties,
    "minimum": _keywords.minimum,
    "multipleOf": _keywords.multipleOf,
    "pattern": _keywords.pattern,
    "required": _keywords.required,
    "type": _keywords.type,
    "uniqueItems": _keywords.uniqueItems,
}


def _leaf_keyword(evaluator, keyword, value, instance, schema, node):
    func = _DELEGATED[keyword]
    for error in func(evaluator, value, instance, schema) or ():
        node.errors.append(error.message)
    node.valid = not node.errors
    return node

def _properties(
    evaluator, properties, instance, schema,
    *, resolver, instance_path, schema_path, base_uri,
):
    node = evaluator._keyword_node(
        "properties", instance_path, schema_path, base_uri,
    )
    if not evaluator.is_type(instance, "object"):
        return node
    for property, subschema in properties.items():
        if property not in instance:
            continue
        child = evaluator._evaluate_schema(
            subschema,
            instance[property],
            resolver=resolver,
            instance_path=(*instance_path, property),
            schema_path=(*schema_path, "properties", property),
            base_uri=base_uri,
        )
        node.children.append(child)
        node._evaluated_properties.add(property)
        if not child.valid:
            node.valid = False
    node._freeze_annotations()
    return node


def _pattern_properties(
    evaluator, patternProperties, instance, schema,
    *, resolver, instance_path, schema_path, base_uri,
):
    node = evaluator._keyword_node(
        "patternProperties", instance_path, schema_path, base_uri,
    )
    if not evaluator.is_type(instance, "object"):
        return node
    for pattern, subschema in patternProperties.items():
        for property, value in instance.items():
            if not re.search(pattern, property):
                continue
            child = evaluator._evaluate_schema(
                subschema,
                value,
                resolver=resolver,
                instance_path=(*instance_path, property),
                schema_path=(*schema_path, "patternProperties", pattern),
                base_uri=base_uri,
            )
            node.children.append(child)
            node._evaluated_properties.add(property)
            if not child.valid:
                node.valid = False
    node._freeze_annotations()
    return node


def _additional_properties(
    evaluator, aP, instance, schema,
    *, resolver, instance_path, schema_path, base_uri,
):
    node = evaluator._keyword_node(
        "additionalProperties", instance_path, schema_path, base_uri,
    )
    if not evaluator.is_type(instance, "object"):
        return node

    extras = set(_utils.find_additional_properties(instance, schema))
    if aP is False:
        if extras:
            if "patternProperties" in schema:
                verb = "does" if len(extras) == 1 else "do"
                joined = ", ".join(repr(each) for each in sorted(extras))
                patterns = ", ".join(
                    repr(each) for each in sorted(schema["patternProperties"])
                )
                error = (
                    f"{joined} {verb} not match any of the regexes: "
                    f"{patterns}"
                )
            else:
                found, verb = _utils.extras_msg(sorted(extras, key=str))
                error = (
                    "Additional properties are not allowed "
                    f"({found} {verb} unexpected)"
                )
            node.errors.append(error)
            node.valid = False
        return node

    for property in instance:
        if property not in extras:
            continue
        child = evaluator._evaluate_schema(
            aP,
            instance[property],
            resolver=resolver,
            instance_path=(*instance_path, property),
            schema_path=(*schema_path, "additionalProperties"),
            base_uri=base_uri,
        )
        node.children.append(child)
        if child.valid:
            node._evaluated_properties.add(property)
        else:
            node.valid = False
    node._freeze_annotations()
    return node


def _property_names(
    evaluator, propertyNames, instance, schema,
    *, resolver, instance_path, schema_path, base_uri,
):
    node = evaluator._keyword_node(
        "propertyNames", instance_path, schema_path, base_uri,
    )
    if not evaluator.is_type(instance, "object"):
        return node
    for property in instance:
        child = evaluator._evaluate_schema(
            propertyNames,
            property,
            resolver=resolver,
            instance_path=instance_path,
            schema_path=(*schema_path, "propertyNames"),
            base_uri=base_uri,
        )
        node.children.append(child)
        if not child.valid:
            node.valid = False
    return node


def _prefix_items(
    evaluator, prefixItems, instance, schema,
    *, resolver, instance_path, schema_path, base_uri,
):
    node = evaluator._keyword_node(
        "prefixItems", instance_path, schema_path, base_uri,
    )
    if not evaluator.is_type(instance, "array"):
        return node
    for (index, item), subschema in zip(enumerate(instance), prefixItems):
        child = evaluator._evaluate_schema(
            subschema,
            item,
            resolver=resolver,
            instance_path=(*instance_path, index),
            schema_path=(*schema_path, "prefixItems", index),
            base_uri=base_uri,
        )
        node.children.append(child)
        node._evaluated_items.add(index)
        if not child.valid:
            node.valid = False
    node._freeze_annotations()
    return node


def _items(
    evaluator, items, instance, schema,
    *, resolver, instance_path, schema_path, base_uri,
):
    node = evaluator._keyword_node(
        "items", instance_path, schema_path, base_uri,
    )
    if not evaluator.is_type(instance, "array"):
        return node

    prefix = len(schema.get("prefixItems", []))
    total = len(instance)
    extra = total - prefix
    if extra <= 0:
        return node

    if items is False:
        rest = instance[prefix:] if extra != 1 else instance[prefix]
        item = "items" if prefix != 1 else "item"
        node.errors.append(
            f"Expected at most {prefix} {item} but found {extra} "
            f"extra: {rest!r}",
        )
        node.valid = False
        return node

    for index in range(prefix, total):
        child = evaluator._evaluate_schema(
            items,
            instance[index],
            resolver=resolver,
            instance_path=(*instance_path, index),
            schema_path=(*schema_path, "items"),
            base_uri=base_uri,
        )
        node.children.append(child)
        if child.valid:
            node._evaluated_items.add(index)
        else:
            node.valid = False
    node._freeze_annotations()
    return node


def _contains(
    evaluator, contains, instance, schema,
    *, resolver, instance_path, schema_path, base_uri,
):
    node = evaluator._keyword_node(
        "contains", instance_path, schema_path, base_uri,
    )
    if not evaluator.is_type(instance, "array"):
        return node

    matches = 0
    min_contains = schema.get("minContains", 1)
    max_contains = schema.get("maxContains", len(instance))
    for index, each in enumerate(instance):
        child = evaluator._evaluate_schema(
            contains,
            each,
            resolver=resolver,
            instance_path=(*instance_path, index),
            schema_path=(*schema_path, "contains"),
            base_uri=base_uri,
        )
        node.children.append(child)
        if child.valid:
            matches += 1
            node._evaluated_items.add(index)

    if matches > max_contains:
        node.errors.append(
            "Too many items match the given schema "
            f"(expected at most {max_contains})",
        )
        node.valid = False
    elif matches < min_contains:
        if not matches:
            node.errors.append(
                f"{instance!r} does not contain items "
                "matching the given schema",
            )
        else:
            node.errors.append(
                "Too few items match the given schema (expected at least "
                f"{min_contains} but only {matches} matched)",
            )
        node.valid = False
    node._freeze_annotations()
    return node


def _all_of(
    evaluator, allOf, instance, schema,
    *, resolver, instance_path, schema_path, base_uri,
):
    node = evaluator._keyword_node(
        "allOf", instance_path, schema_path, base_uri,
    )
    for index, subschema in enumerate(allOf):
        child = evaluator._evaluate_schema(
            subschema,
            instance,
            resolver=resolver,
            instance_path=instance_path,
            schema_path=(*schema_path, "allOf", index),
            base_uri=base_uri,
        )
        node.children.append(child)
        if child.valid:
            node._evaluated_properties |= child._evaluated_properties
            node._evaluated_items |= child._evaluated_items
        else:
            node.valid = False
    node._freeze_annotations()
    return node


def _any_of(
    evaluator, anyOf, instance, schema,
    *, resolver, instance_path, schema_path, base_uri,
):
    node = evaluator._keyword_node(
        "anyOf", instance_path, schema_path, base_uri,
    )
    for index, subschema in enumerate(anyOf):
        child = evaluator._evaluate_schema(
            subschema,
            instance,
            resolver=resolver,
            instance_path=instance_path,
            schema_path=(*schema_path, "anyOf", index),
            base_uri=base_uri,
        )
        node.children.append(child)
        if child.valid:
            node._evaluated_properties |= child._evaluated_properties
            node._evaluated_items |= child._evaluated_items
    if not any(child.valid for child in node.children):
        node.valid = False
        node.errors.append(
            f"{instance!r} is not valid under any of the given schemas",
        )
    node._freeze_annotations()
    return node


def _one_of(
    evaluator, oneOf, instance, schema,
    *, resolver, instance_path, schema_path, base_uri,
):
    node = evaluator._keyword_node(
        "oneOf", instance_path, schema_path, base_uri,
    )
    valids = []
    for index, subschema in enumerate(oneOf):
        child = evaluator._evaluate_schema(
            subschema,
            instance,
            resolver=resolver,
            instance_path=instance_path,
            schema_path=(*schema_path, "oneOf", index),
            base_uri=base_uri,
        )
        node.children.append(child)
        if child.valid:
            valids.append(index)
    if not valids:
        node.valid = False
        node.errors.append(
            f"{instance!r} is not valid under any of the given schemas",
        )
    elif len(valids) > 1:
        node.valid = False
        node.errors.append(
            f"{instance!r} is valid under {len(valids)} of the given "
            f"schemas (indexes {valids}), expected exactly one",
        )
    else:
        only = node.children[valids[0]]
        node._evaluated_properties |= only._evaluated_properties
        node._evaluated_items |= only._evaluated_items
    node._freeze_annotations()
    return node


def _not(
    evaluator, not_schema, instance, schema,
    *, resolver, instance_path, schema_path, base_uri,
):
    node = evaluator._keyword_node("not", instance_path, schema_path, base_uri)
    child = evaluator._evaluate_schema(
        not_schema,
        instance,
        resolver=resolver,
        instance_path=instance_path,
        schema_path=(*schema_path, "not"),
        base_uri=base_uri,
    )
    node.children.append(child)
    if child.valid:
        node.valid = False
        node.errors.append(
            f"{instance!r} should not be valid under {not_schema!r}",
        )
    return node


def _dependent_schemas(
    evaluator, dependentSchemas, instance, schema,
    *, resolver, instance_path, schema_path, base_uri,
):
    node = evaluator._keyword_node(
        "dependentSchemas", instance_path, schema_path, base_uri,
    )
    if not evaluator.is_type(instance, "object"):
        return node
    for property, subschema in dependentSchemas.items():
        if property not in instance:
            continue
        child = evaluator._evaluate_schema(
            subschema,
            instance,
            resolver=resolver,
            instance_path=instance_path,
            schema_path=(*schema_path, "dependentSchemas", property),
            base_uri=base_uri,
        )
        node.children.append(child)
        if child.valid:
            node._evaluated_properties |= child._evaluated_properties
            node._evaluated_items |= child._evaluated_items
        else:
            node.valid = False
    node._freeze_annotations()
    return node


def _identity_find(root, target):
    if root is target:
        return ()
    if isinstance(root, dict):
        for key, value in root.items():
            found = _identity_find(value, target)
            if found is not None:
                return (key, *found)
    elif isinstance(root, list):
        for index, value in enumerate(root):
            found = _identity_find(value, target)
            if found is not None:
                return (index, *found)
    return None


def _locate_resource(resolver, contents):
    """
    Find the resource URI and JSON Pointer at which ``contents`` lives.

    Anchor-based references (including ``$dynamicRef``, whose target may
    be overridden by an outer resource in the dynamic scope) do not carry
    their target location in the reference itself, so the resolved schema
    object is located by identity within the candidate resources.
    """
    candidates = [resolver._base_uri]
    candidates += [uri for uri, _ in resolver.dynamic_scope()]
    for uri in candidates:
        try:
            resource = resolver._registry[uri]
        except KeyError:
            continue
        path = _identity_find(resource.contents, contents)
        if path is not None:
            return uri, path
    return resolver._base_uri, ()


def _make_reference(keyword):
    def _reference(
        evaluator, ref, instance, schema,
        *, resolver, instance_path, schema_path, base_uri,
    ):
        node = evaluator._keyword_node(
            keyword, instance_path, schema_path, base_uri,
        )
        try:
            resolved = resolver.lookup(ref)
        except referencing.exceptions.Unresolvable as err:
            raise exceptions._WrappedReferencingError(err) from err

        _, fragment = urldefrag(urljoin(base_uri, ref))
        if fragment.startswith("/"):
            target_base = resolved.resolver._base_uri
            target_path = tuple(
                _unescape(unquote(each)) for each in fragment.split("/")[1:]
            )
        elif (
            isinstance(resolved.contents, dict)
            and "$id" in resolved.contents
        ):
            target_base, target_path = resolved.resolver._base_uri, ()
        else:
            target_base, target_path = _locate_resource(
                resolver, resolved.contents,
            )

        child = evaluator._evaluate_schema(
            resolved.contents,
            instance,
            resolver=resolved.resolver,
            instance_path=instance_path,
            schema_path=target_path,
            base_uri=target_base,
            enter_resource=False,
        )
        node.children.append(child)
        node.valid = child.valid
        if child.valid:
            node._evaluated_properties |= child._evaluated_properties
            node._evaluated_items |= child._evaluated_items
        node._freeze_annotations()
        return node
    return _reference


def _unevaluated_properties(
    evaluator, uP, instance, schema,
    *, evaluated, resolver, instance_path, schema_path, base_uri,
):
    node = evaluator._keyword_node(
        "unevaluatedProperties", instance_path, schema_path, base_uri,
    )
    if not evaluator.is_type(instance, "object"):
        return node

    unevaluated = [each for each in instance if each not in evaluated]
    if uP is False:
        if unevaluated:
            found, verb = _utils.extras_msg(sorted(unevaluated, key=str))
            node.errors.append(
                "Unevaluated properties are not allowed "
                f"({found} {verb} unexpected)",
            )
            node.valid = False
        return node

    invalid = []
    for property in unevaluated:
        child = evaluator._evaluate_schema(
            uP,
            instance[property],
            resolver=resolver,
            instance_path=(*instance_path, property),
            schema_path=(*schema_path, "unevaluatedProperties"),
            base_uri=base_uri,
        )
        node.children.append(child)
        if child.valid:
            node._evaluated_properties.add(property)
        else:
            invalid.append(property)
    if invalid:
        found, verb = _utils.extras_msg(invalid)
        node.errors.append(
            "Unevaluated properties are not valid under the given schema "
            f"({found} {verb} unevaluated and invalid)",
        )
        node.valid = False
    node._freeze_annotations()
    return node


def _unevaluated_items(
    evaluator, uI, instance, schema,
    *, evaluated, resolver, instance_path, schema_path, base_uri,
):
    node = evaluator._keyword_node(
        "unevaluatedItems", instance_path, schema_path, base_uri,
    )
    if not evaluator.is_type(instance, "array"):
        return node

    unevaluated = [
        index for index in range(len(instance)) if index not in evaluated
    ]
    if uI is False:
        if unevaluated:
            items = [instance[index] for index in unevaluated]
            found, verb = _utils.extras_msg(items)
            node.errors.append(
                "Unevaluated items are not allowed "
                f"({found} {verb} unexpected)",
            )
            node.valid = False
        return node

    invalid = []
    for index in unevaluated:
        child = evaluator._evaluate_schema(
            uI,
            instance[index],
            resolver=resolver,
            instance_path=(*instance_path, index),
            schema_path=(*schema_path, "unevaluatedItems"),
            base_uri=base_uri,
        )
        node.children.append(child)
        if child.valid:
            node._evaluated_items.add(index)
        else:
            invalid.append(index)
    if invalid:
        node.errors.append(
            "Unevaluated items are not valid under the given schema "
            f"(indexes {invalid} are unevaluated and invalid)",
        )
        node.valid = False
    node._freeze_annotations()
    return node


_APPLICATORS = {
    "$dynamicRef": _make_reference("$dynamicRef"),
    "$ref": _make_reference("$ref"),
    "additionalProperties": _additional_properties,
    "allOf": _all_of,
    "anyOf": _any_of,
    "contains": _contains,
    "dependentSchemas": _dependent_schemas,
    "items": _items,
    "not": _not,
    "oneOf": _one_of,
    "patternProperties": _pattern_properties,
    "prefixItems": _prefix_items,
    "properties": _properties,
    "propertyNames": _property_names,
}


class Draft202012Evaluator:
    """
    Evaluate instances against a Draft 2020-12 schema, producing trees.

    Arguments:

        schema:

            the schema to evaluate instances against

        registry:

            a `referencing.Registry` used to resolve references. If
            unprovided, the registry of official specification resources
            is used.

        format_checker:

            a `jsonschema.FormatChecker` used when evaluating the
            ``format`` keyword

        extra_keywords:

            a mapping of custom keyword names to callables. Each callable
            is invoked once per occurrence of its keyword, as
            ``callable(context, keyword_value, instance, schema)`` where
            ``context`` is a `KeywordContext`, and must return a
            `KeywordResult` or `None`. Custom keywords are evaluated in
            the same single pass as built-in keywords, so their outcomes
            appear in the result tree without any re-validation.

    """

    _APPLICATORS = _APPLICATORS

    def __init__(
        self,
        schema,
        *,
        registry=None,
        format_checker=None,
        extra_keywords=None,
    ):
        self.schema = schema
        self.format_checker = format_checker
        self._type_checker = _types.draft202012_type_checker
        if registry is None:
            registry = SPECIFICATIONS
        else:
            registry = SPECIFICATIONS.combine(registry)
        resource = _SPECIFICATION.create_resource(schema)
        self._resolver = registry.resolver_with_root(resource)
        self._extra_keywords = dict(extra_keywords or {})

    def __repr__(self):
        return f"Draft202012Evaluator(schema={self.schema!r})"

    def is_type(self, instance, type):
        """
        Check whether the instance is of the given JSON Schema type.
        """
        try:
            return self._type_checker.is_type(instance, type)
        except exceptions.UndefinedTypeCheck:
            raise exceptions.UnknownType(
                type, instance, self.schema,
            ) from None

    def evaluate(self, instance):
        """
        Evaluate the instance, returning the root `EvaluationNode`.
        """
        return self._evaluate_schema(
            self.schema,
            instance,
            resolver=self._resolver,
            instance_path=(),
            schema_path=(),
            base_uri=self._resolver._base_uri,
            enter_resource=False,
        )

    def _schema_node(self, instance_path, schema_path, base_uri):
        return EvaluationNode(
            valid=True,
            keyword=None,
            instance_location=_pointer(instance_path),
            schema_location=_pointer(schema_path),
            absolute_schema_location=_absolute(base_uri, schema_path),
        )

    def _keyword_node(self, keyword, instance_path, schema_path, base_uri):
        schema_path = (*schema_path, keyword)
        return EvaluationNode(
            valid=True,
            keyword=keyword,
            instance_location=_pointer(instance_path),
            schema_location=_pointer(schema_path),
            absolute_schema_location=_absolute(base_uri, schema_path),
        )

    def _evaluate_schema(
        self,
        schema,
        instance,
        *,
        resolver,
        instance_path,
        schema_path,
        base_uri,
        enter_resource=True,
    ):
        if schema is True:
            return self._schema_node(instance_path, schema_path, base_uri)
        if schema is False:
            node = self._schema_node(instance_path, schema_path, base_uri)
            node.valid = False
            node.errors.append(f"False schema does not allow {instance!r}")
            return node

        if enter_resource:
            resource = _SPECIFICATION.create_resource(schema)
            sub_resolver = resolver.in_subresource(resource)
            if sub_resolver is not resolver:
                resolver = sub_resolver
                base_uri = resolver._base_uri
                schema_path = ()

        node = self._schema_node(instance_path, schema_path, base_uri)
        location = dict(
            resolver=resolver,
            instance_path=instance_path,
            schema_path=schema_path,
            base_uri=base_uri,
        )

        deferred = []
        for keyword, value in schema.items():
            if keyword in ("unevaluatedProperties", "unevaluatedItems"):
                deferred.append((keyword, value))
                continue
            if keyword in ("then", "else"):
                continue  # evaluated together with "if"
            if keyword == "if":
                children = self._if_group(value, instance, schema, **location)
            else:
                child = self._evaluate_keyword(
                    keyword, value, instance, schema, **location,
                )
                children = [child] if child is not None else []
            for child in children:
                node.children.append(child)
                if child.valid:
                    node._evaluated_properties |= child._evaluated_properties
                    node._evaluated_items |= child._evaluated_items
                else:
                    node.valid = False

        for keyword, value in deferred:
            if keyword == "unevaluatedProperties":
                child = _unevaluated_properties(
                    self, value, instance, schema,
                    evaluated=node._evaluated_properties, **location,
                )
            else:
                child = _unevaluated_items(
                    self, value, instance, schema,
                    evaluated=node._evaluated_items, **location,
                )
            node.children.append(child)
            if child.valid:
                node._evaluated_properties |= child._evaluated_properties
                node._evaluated_items |= child._evaluated_items
            else:
                node.valid = False

        for child in node.children:
            if not child.valid:
                continue
            for key, val in child.annotations.items():
                if key not in _EVALUATED_ANNOTATIONS:
                    node.annotations[key] = val
        node._freeze_annotations()
        return node

    def _evaluate_keyword(
        self,
        keyword,
        value,
        instance,
        schema,
        *,
        resolver,
        instance_path,
        schema_path,
        base_uri,
    ):
        applicator = self._APPLICATORS.get(keyword)
        if applicator is not None:
            return applicator(
                self, value, instance, schema,
                resolver=resolver,
                instance_path=instance_path,
                schema_path=schema_path,
                base_uri=base_uri,
            )
        if keyword in _DELEGATED:
            node = self._keyword_node(
                keyword, instance_path, schema_path, base_uri,
            )
            return _leaf_keyword(self, keyword, value, instance, schema, node)
        custom = self._extra_keywords.get(keyword)
        if custom is not None:
            return self._custom_keyword(
                custom, keyword, value, instance, schema,
                resolver=resolver,
                instance_path=instance_path,
                schema_path=schema_path,
                base_uri=base_uri,
            )
        return None

    def _custom_keyword(
        self,
        func,
        keyword,
        value,
        instance,
        schema,
        *,
        resolver,
        instance_path,
        schema_path,
        base_uri,
    ):
        node = self._keyword_node(
            keyword, instance_path, schema_path, base_uri,
        )
        context = KeywordContext(
            evaluator=self,
            instance_path=instance_path,
            schema_path=schema_path,
            base_uri=base_uri,
            resolver=resolver,
        )
        result = func(context, value, instance, schema)
        if result is None:
            return node
        node.valid = result.valid
        node.errors.extend(result.errors)
        node.children.extend(result.children)
        for key, val in result.annotations.items():
            if key == _EVALUATED_PROPERTIES:
                node._evaluated_properties.update(val)
            elif key == _EVALUATED_ITEMS:
                node._evaluated_items.update(val)
            else:
                node.annotations[key] = val
        node._freeze_annotations()
        return node

    def _if_group(
        self,
        if_schema,
        instance,
        schema,
        *,
        resolver,
        instance_path,
        schema_path,
        base_uri,
    ):
        if_node = self._keyword_node(
            "if", instance_path, schema_path, base_uri,
        )
        condition = self._evaluate_schema(
            if_schema,
            instance,
            resolver=resolver,
            instance_path=instance_path,
            schema_path=(*schema_path, "if"),
            base_uri=base_uri,
        )
        if_node.children.append(condition)
        if condition.valid:
            if_node._evaluated_properties |= condition._evaluated_properties
            if_node._evaluated_items |= condition._evaluated_items
        if_node._freeze_annotations()

        nodes = [if_node]
        branch = "then" if condition.valid else "else"
        if branch in schema:
            branch_node = self._keyword_node(
                branch, instance_path, schema_path, base_uri,
            )
            child = self._evaluate_schema(
                schema[branch],
                instance,
                resolver=resolver,
                instance_path=instance_path,
                schema_path=(*schema_path, branch),
                base_uri=base_uri,
            )
            branch_node.children.append(child)
            branch_node.valid = child.valid
            if child.valid:
                branch_node._evaluated_properties.update(
                    child._evaluated_properties,
                )
                branch_node._evaluated_items.update(child._evaluated_items)
            branch_node._freeze_annotations()
            nodes.append(branch_node)
        return nodes


def evaluate(instance, schema, **kwargs):
    """
    Evaluate ``instance`` against the Draft 2020-12 ``schema``.

    This is a convenience for creating a `Draft202012Evaluator` and
    evaluating a single instance with it.
    """
    return Draft202012Evaluator(schema, **kwargs).evaluate(instance)
