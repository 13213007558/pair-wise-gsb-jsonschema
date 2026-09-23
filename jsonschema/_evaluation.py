"""
Evaluation of instances against schemas, producing serializable result trees.

Unlike iter_errors, which only yields failures, the evaluation performed
here records every (sub)schema application: where it happened, whether it
succeeded, which annotations it produced, and which properties or items
it evaluated. The result is a tree of EvaluationNode objects which can be
serialized to JSON.

The evaluation semantics (annotation collection, in-place applicators,
unevaluatedProperties / unevaluatedItems) follow Draft 2020-12.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urldefrag, urljoin
import re

from attrs import define, field
import referencing.exceptions

from jsonschema._utils import extras_msg, find_additional_properties
from jsonschema.exceptions import _WrappedReferencingError

# Keywords whose subschema evaluations never contribute annotations or
# evaluated properties/items to their parent schema.
_NO_ANNOTATION_CONTRIBUTION = frozenset({"if", "not"})

# Keywords which only ever produce annotations, never assertion errors.
_ANNOTATION_KEYWORDS = frozenset({
    "title",
    "description",
    "default",
    "deprecated",
    "examples",
    "readOnly",
    "writeOnly",
    "contentEncoding",
    "contentMediaType",
    "contentSchema",
    "$comment",
})

# Keywords which are purely structural and produce no node of their own.
_IGNORED_KEYWORDS = frozenset({
    "$id",
    "$schema",
    "$anchor",
    "$dynamicAnchor",
    "$recursiveAnchor",
    "$defs",
    "definitions",
    "$vocabulary",
})

# Keywords which run after all sibling keywords, once their evaluated
# properties / items have been collected.
_UNEVALUATED = frozenset({"unevaluatedProperties", "unevaluatedItems"})

_UNSET = object()


def _escape(segment):
    return str(segment).replace("~", "~0").replace("/", "~1")


def _unescape(segment):
    return segment.replace("~1", "/").replace("~0", "~")


def _pointer(segments):
    if not segments:
        return ""
    return "/" + "/".join(_escape(segment) for segment in segments)


def _absolute_location(uri, path):
    return f"{uri}#{_pointer(path)}"


def _find_anchor(contents, anchor):
    """
    Find the JSON pointer to the subschema declaring the given anchor.
    """
    stack = [((), contents)]
    while stack:
        path, subschema = stack.pop()
        if isinstance(subschema, Mapping):
            if anchor in (
                subschema.get("$anchor"),
                subschema.get("$dynamicAnchor"),
            ):
                return path
            for key, value in subschema.items():
                stack.append(((*path, key), value))
        elif isinstance(subschema, list):
            for index, value in enumerate(subschema):
                stack.append(((*path, index), value))
    return None


@define
class EvaluationNode:
    """
    A single node in an evaluation result tree.

    Each node records one keyword application (or, when the keyword
    attribute is None, the application of an entire subschema) against
    an instance location. Nodes are serializable via to_dict.
    """

    keyword: str | None
    instance_location: str
    schema_location: str
    absolute_schema_location: str
    valid: bool = True
    annotations: dict[str, Any] = field(factory=dict)
    errors: list[str] = field(factory=list)
    children: list = field(factory=list)
    evaluated_properties: set = field(factory=set)
    evaluated_items: set = field(factory=set)

    def to_dict(self):
        """
        Return a JSON-serializable representation of this subtree.
        """
        result = {
            "valid": self.valid,
            "instanceLocation": self.instance_location,
            "keywordLocation": self.schema_location,
            "absoluteKeywordLocation": self.absolute_schema_location,
        }
        if self.keyword is not None:
            result["keyword"] = self.keyword
        if self.errors:
            result["errors"] = list(self.errors)
        if self.annotations:
            result["annotations"] = dict(self.annotations)
        if self.evaluated_properties:
            result["evaluatedProperties"] = sorted(
                self.evaluated_properties, key=str,
            )
        if self.evaluated_items:
            result["evaluatedItems"] = sorted(self.evaluated_items)
        if self.children:
            result["children"] = [each.to_dict() for each in self.children]
        return result

    def __json__(self):
        return self.to_dict()


@define
class _Context:
    """
    The state tracked while evaluating one (sub)schema against an instance.
    """

    validator: Any
    specification: Any
    instance: Any
    schema: Any
    instance_path: tuple = ()
    schema_path: tuple = ()
    resource_uri: str = ""
    resource_path: tuple | None = ()

    def node(self, keyword):
        if keyword is None:
            schema_path = self.schema_path
            resource_path = self.resource_path
        else:
            schema_path = (*self.schema_path, keyword)
            if self.resource_path is None:
                resource_path = None
            else:
                resource_path = (*self.resource_path, keyword)
        if resource_path is None:
            absolute = self.resource_uri
        else:
            absolute = _absolute_location(self.resource_uri, resource_path)
        return EvaluationNode(
            keyword=keyword,
            instance_location=_pointer(self.instance_path),
            schema_location=_pointer(schema_path),
            absolute_schema_location=absolute,
        )


def evaluate(validator, instance, specification):
    """
    Evaluate an instance against the schema of the given validator.

    Returns the root EvaluationNode of the result tree. The evaluation
    is performed exactly once: each keyword validator (including custom
    ones) is called a single time per applicable (schema, instance) pair.
    """
    schema = validator.schema
    resource_uri = ""
    if isinstance(schema, Mapping):
        resource_uri = validator.ID_OF(schema) or ""
    base_uri = getattr(validator._resolver, "_base_uri", None)
    if base_uri:
        resource_uri = base_uri
    context = _Context(
        validator=validator,
        specification=specification,
        instance=instance,
        schema=schema,
        resource_uri=resource_uri,
    )
    return _evaluate_schema(context)


def _evaluate_schema(context):
    node = context.node(keyword=None)
    schema = context.schema

    if schema is True:
        return node
    if schema is False:
        node.valid = False
        node.errors.append(
            f"False schema does not allow {context.instance!r}",
        )
        return node

    deferred = []
    for keyword, value in context.validator._APPLICABLE_VALIDATORS(schema):
        if keyword in _IGNORED_KEYWORDS:
            continue
        if keyword in _ANNOTATION_KEYWORDS:
            node.annotations[keyword] = value
            continue
        if keyword in _UNEVALUATED:
            deferred.append((keyword, value))
            continue
        if keyword in {"then", "else"}:
            continue  # handled together with the if keyword
        handler = _APPLICATORS.get(keyword)
        if handler is None:
            children = [_generic_keyword(context, keyword, value)]
        else:
            children = handler(context, keyword, value)
        for child in children:
            _absorb(node, child)

    for keyword, value in deferred:
        _absorb(node, _unevaluated(context, keyword, value, node))

    return node


def _absorb(node, child):
    """
    Absorb a keyword-level child node into a schema-level node.
    """
    node.children.append(child)
    if not child.valid:
        node.valid = False
    elif child.keyword not in _NO_ANNOTATION_CONTRIBUTION:
        node.annotations.update(child.annotations)
        node.evaluated_properties.update(child.evaluated_properties)
        node.evaluated_items.update(child.evaluated_items)


def _descend(
    context,
    subschema,
    schema_segments,
    instance=_UNSET,
    instance_segment=_UNSET,
    resolver=None,
    resource_uri=None,
    resource_path=_UNSET,
):
    """
    Evaluate a subschema, returning its schema-level node.
    """
    if instance is not _UNSET:
        new_instance = instance
        instance_path = context.instance_path
    elif instance_segment is not _UNSET:
        new_instance = context.instance[instance_segment]
        instance_path = (*context.instance_path, instance_segment)
    else:
        new_instance = context.instance
        instance_path = context.instance_path

    if resource_uri is None:
        resource_uri = context.resource_uri
        if context.resource_path is None:
            resource_path = None
        else:
            resource_path = context.resource_path + tuple(schema_segments)
            if isinstance(subschema, Mapping):
                id_ = context.validator.ID_OF(subschema)
                if id_ is not None:
                    resource_uri = urljoin(resource_uri, id_)
                    resource_path = ()

    if isinstance(subschema, bool) or context.validator._resolver is None:
        evolved = context.validator.evolve(schema=subschema)
    else:
        if resolver is None:
            resolver = context.validator._resolver.in_subresource(
                context.specification.create_resource(subschema),
            )
        evolved = context.validator.evolve(
            schema=subschema, _resolver=resolver,
        )

    return _evaluate_schema(_Context(
        validator=evolved,
        specification=context.specification,
        instance=new_instance,
        schema=subschema,
        instance_path=instance_path,
        schema_path=context.schema_path + tuple(schema_segments),
        resource_uri=resource_uri,
        resource_path=resource_path,
    ))


def _generic_keyword(context, keyword, value):
    """
    Evaluate a non-applicator keyword via the validator's own function.

    This is also how custom keywords (registered via extend) join the
    result tree: their validator function is called once, here, during
    the single evaluation pass, and its errors attach to the node.
    """
    node = context.node(keyword)
    function = context.validator.VALIDATORS.get(keyword)
    if function is None:
        return node
    if keyword == "format" and context.validator.format_checker is None:
        node.annotations[keyword] = value
        return node
    errors = function(
        context.validator, value, context.instance, context.schema,
    ) or ()
    for error in errors:
        node.errors.append(error.message)
    if node.errors:
        node.valid = False
    return node


def _merge_same_instance(node, child):
    """
    Merge results of a subschema applied to the same instance location.
    """
    node.children.append(child)
    if child.valid:
        node.annotations.update(child.annotations)
        node.evaluated_properties.update(child.evaluated_properties)
        node.evaluated_items.update(child.evaluated_items)
    else:
        node.valid = False


def _properties(context, keyword, properties):
    node = context.node(keyword)
    if not context.validator.is_type(context.instance, "object"):
        return [node]
    for name, subschema in properties.items():
        if name in context.instance:
            child = _descend(
                context, subschema, [keyword, name], instance_segment=name,
            )
            node.children.append(child)
            node.evaluated_properties.add(name)
            if not child.valid:
                node.valid = False
    return [node]


def _pattern_properties(context, keyword, pattern_properties):
    node = context.node(keyword)
    if not context.validator.is_type(context.instance, "object"):
        return [node]
    for pattern, subschema in pattern_properties.items():
        for key in context.instance:
            if re.search(pattern, key):
                child = _descend(
                    context, subschema, [keyword, pattern],
                    instance_segment=key,
                )
                node.children.append(child)
                node.evaluated_properties.add(key)
                if not child.valid:
                    node.valid = False
    return [node]


def _additional_properties(context, keyword, additional):
    node = context.node(keyword)
    if not context.validator.is_type(context.instance, "object"):
        return [node]
    extras = set(find_additional_properties(context.instance, context.schema))
    node.evaluated_properties.update(extras)
    if isinstance(additional, Mapping):
        for key in sorted(extras, key=str):
            child = _descend(
                context, additional, [keyword], instance_segment=key,
            )
            node.children.append(child)
            if not child.valid:
                node.valid = False
    elif not additional and extras:
        node.valid = False
        if "patternProperties" in context.schema:
            verb = "does" if len(extras) == 1 else "do"
            joined = ", ".join(repr(each) for each in sorted(extras))
            patterns = ", ".join(
                repr(each)
                for each in sorted(context.schema["patternProperties"])
            )
            error = f"{joined} {verb} not match any of the regexes: {patterns}"
            node.errors.append(error)
        else:
            error = "Additional properties are not allowed (%s %s unexpected)"
            node.errors.append(error % extras_msg(sorted(extras, key=str)))
    return [node]


def _property_names(context, keyword, property_names):
    node = context.node(keyword)
    if not context.validator.is_type(context.instance, "object"):
        return [node]
    for key in context.instance:
        child = _descend(context, property_names, [keyword], instance=key)
        node.children.append(child)
        if not child.valid:
            node.valid = False
    return [node]


def _prefix_items(context, keyword, prefix_items):
    node = context.node(keyword)
    if not context.validator.is_type(context.instance, "array"):
        return [node]
    for index, subschema in enumerate(prefix_items):
        if index >= len(context.instance):
            break
        child = _descend(
            context, subschema, [keyword, index], instance_segment=index,
        )
        node.children.append(child)
        node.evaluated_items.add(index)
        if not child.valid:
            node.valid = False
    return [node]


def _items(context, keyword, items):
    if isinstance(items, list):
        # Pre-2020-12 array form: defer to the validator's own function.
        return [_generic_keyword(context, keyword, items)]
    node = context.node(keyword)
    if not context.validator.is_type(context.instance, "array"):
        return [node]
    prefix = len(context.schema.get("prefixItems", []))
    total = len(context.instance)
    extra = total - prefix
    if extra <= 0:
        return [node]
    node.evaluated_items.update(range(prefix, total))
    if items is False:
        if extra == 1:
            rest = context.instance[prefix]
        else:
            rest = context.instance[prefix:]
        item = "items" if prefix != 1 else "item"
        node.valid = False
        node.errors.append(
            f"Expected at most {prefix} {item} but found {extra} "
            f"extra: {rest!r}",
        )
    elif isinstance(items, Mapping):
        for index in range(prefix, total):
            child = _descend(
                context, items, [keyword], instance_segment=index,
            )
            node.children.append(child)
            if not child.valid:
                node.valid = False
    return [node]


def _contains(context, keyword, contains):
    node = context.node(keyword)
    if not context.validator.is_type(context.instance, "array"):
        return [node]
    min_contains = context.schema.get("minContains", 1)
    max_contains = context.schema.get("maxContains", len(context.instance))
    matches = 0
    for index in range(len(context.instance)):
        child = _descend(
            context, contains, [keyword], instance_segment=index,
        )
        node.children.append(child)
        if child.valid:
            matches += 1
            node.evaluated_items.add(index)
    if matches > max_contains:
        node.valid = False
        node.errors.append(
            "Too many items match the given schema "
            f"(expected at most {max_contains})",
        )
    elif matches < min_contains:
        node.valid = False
        if not matches:
            node.errors.append(
                f"{context.instance!r} does not contain items "
                "matching the given schema",
            )
        else:
            node.errors.append(
                "Too few items match the given schema (expected at least "
                f"{min_contains})",
            )
    return [node]


def _all_of(context, keyword, all_of):
    node = context.node(keyword)
    for index, subschema in enumerate(all_of):
        child = _descend(context, subschema, [keyword, index])
        _merge_same_instance(node, child)
    return [node]


def _any_of(context, keyword, any_of):
    node = context.node(keyword)
    for index, subschema in enumerate(any_of):
        child = _descend(context, subschema, [keyword, index])
        node.children.append(child)
        if child.valid:
            # Only successful branches contribute annotations and
            # evaluated properties/items; failed branches never leak.
            node.annotations.update(child.annotations)
            node.evaluated_properties.update(child.evaluated_properties)
            node.evaluated_items.update(child.evaluated_items)
    if not any(child.valid for child in node.children):
        node.valid = False
        node.errors.append(
            f"{context.instance!r} is not valid under any of the "
            "given schemas",
        )
    return [node]


def _one_of(context, keyword, one_of):
    node = context.node(keyword)
    valid_indexes = []
    for index, subschema in enumerate(one_of):
        child = _descend(context, subschema, [keyword, index])
        node.children.append(child)
        if child.valid:
            valid_indexes.append(index)
    if len(valid_indexes) == 1:
        child = node.children[valid_indexes[0]]
        node.annotations.update(child.annotations)
        node.evaluated_properties.update(child.evaluated_properties)
        node.evaluated_items.update(child.evaluated_items)
    elif not valid_indexes:
        node.valid = False
        node.errors.append(
            f"{context.instance!r} is not valid under any of the "
            "given schemas",
        )
    else:
        node.valid = False
        indexes = ", ".join(str(each) for each in valid_indexes)
        node.errors.append(
            f"{context.instance!r} is valid under each of schemas {indexes}",
        )
    return [node]


def _not(context, keyword, not_schema):
    node = context.node(keyword)
    child = _descend(context, not_schema, [keyword])
    node.children.append(child)
    if child.valid:
        node.valid = False
        node.errors.append(
            f"{context.instance!r} should not be valid under {not_schema!r}",
        )
    return [node]


def _if(context, keyword, if_schema):
    if_node = context.node(keyword)
    condition = _descend(context, if_schema, [keyword])
    if_node.children.append(condition)
    nodes = [if_node]
    follow = "then" if condition.valid else "else"
    if follow in context.schema:
        follow_node = context.node(follow)
        child = _descend(context, context.schema[follow], [follow])
        _merge_same_instance(follow_node, child)
        nodes.append(follow_node)
    return nodes


def _dependent_schemas(context, keyword, dependent_schemas):
    node = context.node(keyword)
    if not context.validator.is_type(context.instance, "object"):
        return [node]
    for dependency, subschema in dependent_schemas.items():
        if dependency in context.instance:
            child = _descend(context, subschema, [keyword, dependency])
            _merge_same_instance(node, child)
    return [node]


def _resolve_location(context, resolved, ref):
    """
    Compute the canonical resource URI and pointer of a resolved reference.
    """
    resolver = resolved.resolver
    base_uri = getattr(resolver, "_base_uri", context.resource_uri)
    _, fragment = urldefrag(urljoin(context.resource_uri, ref))
    if fragment.startswith("/"):
        segments = tuple(
            _unescape(each) for each in fragment.split("/")[1:]
        )
        return base_uri, segments
    if not fragment:
        return base_uri, ()
    # A plain-name fragment ($anchor / $dynamicAnchor): locate the anchor
    # within the resolved resource to recover its JSON pointer.
    try:
        root = resolver.lookup("").contents
    except referencing.exceptions.Unresolvable:
        return f"{base_uri}#{fragment}", None
    path = _find_anchor(root, fragment)
    if path is None:
        return f"{base_uri}#{fragment}", None
    return base_uri, path


def _ref(context, keyword, ref):
    if context.validator._resolver is None:
        # Legacy RefResolver-based validation: defer to the validator's
        # own keyword function.
        return [_generic_keyword(context, keyword, ref)]
    node = context.node(keyword)
    try:
        resolved = context.validator._resolver.lookup(ref)
    except referencing.exceptions.Unresolvable as error:
        raise _WrappedReferencingError(error) from error
    resource_uri, resource_path = _resolve_location(context, resolved, ref)
    child = _descend(
        context, resolved.contents, [keyword],
        resolver=resolved.resolver,
        resource_uri=resource_uri,
        resource_path=resource_path,
    )
    _merge_same_instance(node, child)
    return [node]


def _unevaluated(context, keyword, value, schema_node):
    """
    Evaluate unevaluatedProperties / unevaluatedItems.

    Runs after all sibling keywords, using the evaluated properties and
    items collected on the schema-level node.
    """
    node = context.node(keyword)
    instance = context.instance
    if keyword == "unevaluatedProperties":
        if not context.validator.is_type(instance, "object"):
            return node
        extras = [
            key for key in instance
            if key not in schema_node.evaluated_properties
        ]
        node.evaluated_properties.update(extras)
        if value is False:
            if extras:
                node.valid = False
                error = (
                    "Unevaluated properties are not allowed "
                    "(%s %s unexpected)"
                )
                node.errors.append(
                    error % extras_msg(sorted(extras, key=str)),
                )
        elif value is not True:
            invalid = []
            for key in extras:
                child = _descend(
                    context, value, [keyword, key], instance_segment=key,
                )
                node.children.append(child)
                if not child.valid:
                    node.valid = False
                    invalid.append(key)
            if invalid:
                error = (
                    "Unevaluated properties are not valid under "
                    "the given schema (%s %s unevaluated and invalid)"
                )
                node.errors.append(error % extras_msg(invalid))
    else:
        if not context.validator.is_type(instance, "array"):
            return node
        extras = [
            index for index in range(len(instance))
            if index not in schema_node.evaluated_items
        ]
        node.evaluated_items.update(extras)
        if value is False:
            if extras:
                node.valid = False
                error = "Unevaluated items are not allowed (%s %s unexpected)"
                items = [instance[index] for index in extras]
                node.errors.append(error % extras_msg(items))
        elif value is not True:
            invalid = []
            for index in extras:
                child = _descend(
                    context, value, [keyword], instance_segment=index,
                )
                node.children.append(child)
                if not child.valid:
                    node.valid = False
                    invalid.append(instance[index])
            if invalid:
                error = (
                    "Unevaluated items are not valid under "
                    "the given schema (%s %s unevaluated and invalid)"
                )
                node.errors.append(error % extras_msg(invalid))
    return node


_APPLICATORS = {
    "properties": _properties,
    "patternProperties": _pattern_properties,
    "additionalProperties": _additional_properties,
    "propertyNames": _property_names,
    "prefixItems": _prefix_items,
    "items": _items,
    "contains": _contains,
    "allOf": _all_of,
    "anyOf": _any_of,
    "oneOf": _one_of,
    "not": _not,
    "if": _if,
    "dependentSchemas": _dependent_schemas,
    "$ref": _ref,
    "$dynamicRef": _ref,
}
