from concurrent.futures import ThreadPoolExecutor
from unittest import TestCase
import textwrap

import jsonpath_ng

from jsonschema import exceptions
from jsonschema.validators import _LATEST_VERSION


class TestBestMatch(TestCase):
    def best_match_of(self, instance, schema):
        errors = list(_LATEST_VERSION(schema).iter_errors(instance))
        msg =  f"No errors found for {instance} under {schema!r}!"
        self.assertTrue(errors, msg=msg)

        best = exceptions.best_match(iter(errors))
        reversed_best = exceptions.best_match(reversed(errors))

        self.assertEqual(
            best._contents(),
            reversed_best._contents(),
            f"No consistent best match!\nGot: {best}\n\nThen: {reversed_best}",
        )
        return best

    def test_shallower_errors_are_better_matches(self):
        schema = {
            "properties": {
                "foo": {
                    "minProperties": 2,
                    "properties": {"bar": {"type": "object"}},
                },
            },
        }
        best = self.best_match_of(instance={"foo": {"bar": []}}, schema=schema)
        self.assertEqual(best.validator, "minProperties")

    def test_oneOf_and_anyOf_are_weak_matches(self):
        """
        A property you *must* match is probably better than one you have to
        match a part of.
        """

        schema = {
            "minProperties": 2,
            "anyOf": [{"type": "string"}, {"type": "number"}],
            "oneOf": [{"type": "string"}, {"type": "number"}],
        }
        best = self.best_match_of(instance={}, schema=schema)
        self.assertEqual(best.validator, "minProperties")

    def test_if_the_most_relevant_error_is_anyOf_it_is_traversed(self):
        """
        If the most relevant error is an anyOf, then we traverse its context
        and select the otherwise *least* relevant error, since in this case
        that means the most specific, deep, error inside the instance.

        I.e. since only one of the schemas must match, we look for the most
        relevant one.
        """

        schema = {
            "properties": {
                "foo": {
                    "anyOf": [
                        {"type": "string"},
                        {"properties": {"bar": {"type": "array"}}},
                    ],
                },
            },
        }
        best = self.best_match_of(instance={"foo": {"bar": 12}}, schema=schema)
        self.assertEqual(best.validator_value, "array")

    def test_no_anyOf_traversal_for_equally_relevant_errors(self):
        """
        We don't traverse into an anyOf (as above) if all of its context errors
        seem to be equally "wrong" against the instance.
        """

        schema = {
            "anyOf": [
                {"type": "string"},
                {"type": "integer"},
                {"type": "object"},
            ],
        }
        best = self.best_match_of(instance=[], schema=schema)
        self.assertEqual(best.validator, "anyOf")

    def test_anyOf_traversal_for_single_equally_relevant_error(self):
        """
        We *do* traverse anyOf with a single nested error, even though it is
        vacuously equally relevant to itself.
        """

        schema = {
            "anyOf": [
                {"type": "string"},
            ],
        }
        best = self.best_match_of(instance=[], schema=schema)
        self.assertEqual(best.validator, "type")

    def test_anyOf_traversal_for_single_sibling_errors(self):
        """
        We *do* traverse anyOf with a single subschema that fails multiple
        times (e.g. on multiple items).
        """

        schema = {
            "anyOf": [
                {"items": {"const": 37}},
            ],
        }
        best = self.best_match_of(instance=[12, 12], schema=schema)
        self.assertEqual(best.validator, "const")

    def test_anyOf_traversal_for_non_type_matching_sibling_errors(self):
        """
        We *do* traverse anyOf with multiple subschemas when one does not type
        match.
        """

        schema = {
            "anyOf": [
                {"type": "object"},
                {"items": {"const": 37}},
            ],
        }
        best = self.best_match_of(instance=[12, 12], schema=schema)
        self.assertEqual(best.validator, "const")

    def test_if_the_most_relevant_error_is_oneOf_it_is_traversed(self):
        """
        If the most relevant error is an oneOf, then we traverse its context
        and select the otherwise *least* relevant error, since in this case
        that means the most specific, deep, error inside the instance.

        I.e. since only one of the schemas must match, we look for the most
        relevant one.
        """

        schema = {
            "properties": {
                "foo": {
                    "oneOf": [
                        {"type": "string"},
                        {"properties": {"bar": {"type": "array"}}},
                    ],
                },
            },
        }
        best = self.best_match_of(instance={"foo": {"bar": 12}}, schema=schema)
        self.assertEqual(best.validator_value, "array")

    def test_no_oneOf_traversal_for_equally_relevant_errors(self):
        """
        We don't traverse into an oneOf (as above) if all of its context errors
        seem to be equally "wrong" against the instance.
        """

        schema = {
            "oneOf": [
                {"type": "string"},
                {"type": "integer"},
                {"type": "object"},
            ],
        }
        best = self.best_match_of(instance=[], schema=schema)
        self.assertEqual(best.validator, "oneOf")

    def test_oneOf_traversal_for_single_equally_relevant_error(self):
        """
        We *do* traverse oneOf with a single nested error, even though it is
        vacuously equally relevant to itself.
        """

        schema = {
            "oneOf": [
                {"type": "string"},
            ],
        }
        best = self.best_match_of(instance=[], schema=schema)
        self.assertEqual(best.validator, "type")

    def test_oneOf_traversal_for_single_sibling_errors(self):
        """
        We *do* traverse oneOf with a single subschema that fails multiple
        times (e.g. on multiple items).
        """

        schema = {
            "oneOf": [
                {"items": {"const": 37}},
            ],
        }
        best = self.best_match_of(instance=[12, 12], schema=schema)
        self.assertEqual(best.validator, "const")

    def test_oneOf_traversal_for_non_type_matching_sibling_errors(self):
        """
        We *do* traverse oneOf with multiple subschemas when one does not type
        match.
        """

        schema = {
            "oneOf": [
                {"type": "object"},
                {"items": {"const": 37}},
            ],
        }
        best = self.best_match_of(instance=[12, 12], schema=schema)
        self.assertEqual(best.validator, "const")

    def test_if_the_most_relevant_error_is_allOf_it_is_traversed(self):
        """
        Now, if the error is allOf, we traverse but select the *most* relevant
        error from the context, because all schemas here must match anyways.
        """

        schema = {
            "properties": {
                "foo": {
                    "allOf": [
                        {"type": "string"},
                        {"properties": {"bar": {"type": "array"}}},
                    ],
                },
            },
        }
        best = self.best_match_of(instance={"foo": {"bar": 12}}, schema=schema)
        self.assertEqual(best.validator_value, "string")

    def test_nested_context_for_oneOf(self):
        """
        We traverse into nested contexts (a oneOf containing an error in a
        nested oneOf here).
        """

        schema = {
            "properties": {
                "foo": {
                    "oneOf": [
                        {"type": "string"},
                        {
                            "oneOf": [
                                {"type": "string"},
                                {
                                    "properties": {
                                        "bar": {"type": "array"},
                                    },
                                },
                            ],
                        },
                    ],
                },
            },
        }
        best = self.best_match_of(instance={"foo": {"bar": 12}}, schema=schema)
        self.assertEqual(best.validator_value, "array")

    def test_it_prioritizes_matching_types(self):
        schema = {
            "properties": {
                "foo": {
                    "anyOf": [
                        {"type": "array", "minItems": 2},
                        {"type": "string", "minLength": 10},
                    ],
                },
            },
        }
        best = self.best_match_of(instance={"foo": "bar"}, schema=schema)
        self.assertEqual(best.validator, "minLength")

        reordered = {
            "properties": {
                "foo": {
                    "anyOf": [
                        {"type": "string", "minLength": 10},
                        {"type": "array", "minItems": 2},
                    ],
                },
            },
        }
        best = self.best_match_of(instance={"foo": "bar"}, schema=reordered)
        self.assertEqual(best.validator, "minLength")

    def test_it_prioritizes_matching_union_types(self):
        schema = {
            "properties": {
                "foo": {
                    "anyOf": [
                        {"type": ["array", "object"], "minItems": 2},
                        {"type": ["integer", "string"], "minLength": 10},
                    ],
                },
            },
        }
        best = self.best_match_of(instance={"foo": "bar"}, schema=schema)
        self.assertEqual(best.validator, "minLength")

        reordered = {
            "properties": {
                "foo": {
                    "anyOf": [
                        {"type": "string", "minLength": 10},
                        {"type": "array", "minItems": 2},
                    ],
                },
            },
        }
        best = self.best_match_of(instance={"foo": "bar"}, schema=reordered)
        self.assertEqual(best.validator, "minLength")

    def test_boolean_schemas(self):
        schema = {"properties": {"foo": False}}
        best = self.best_match_of(instance={"foo": "bar"}, schema=schema)
        self.assertIsNone(best.validator)

    def test_one_error(self):
        validator = _LATEST_VERSION({"minProperties": 2})
        error, = validator.iter_errors({})
        self.assertEqual(
            exceptions.best_match(validator.iter_errors({})).validator,
            "minProperties",
        )

    def test_no_errors(self):
        validator = _LATEST_VERSION({})
        self.assertIsNone(exceptions.best_match(validator.iter_errors({})))


class TestByRelevance(TestCase):
    def test_short_paths_are_better_matches(self):
        shallow = exceptions.ValidationError("Oh no!", path=["baz"])
        deep = exceptions.ValidationError("Oh yes!", path=["foo", "bar"])
        match = max([shallow, deep], key=exceptions.relevance)
        self.assertIs(match, shallow)

        match = max([deep, shallow], key=exceptions.relevance)
        self.assertIs(match, shallow)

    def test_global_errors_are_even_better_matches(self):
        shallow = exceptions.ValidationError("Oh no!", path=[])
        deep = exceptions.ValidationError("Oh yes!", path=["foo"])

        errors = sorted([shallow, deep], key=exceptions.relevance)
        self.assertEqual(
            [list(error.path) for error in errors],
            [["foo"], []],
        )

        errors = sorted([deep, shallow], key=exceptions.relevance)
        self.assertEqual(
            [list(error.path) for error in errors],
            [["foo"], []],
        )

    def test_weak_keywords_are_lower_priority(self):
        weak = exceptions.ValidationError("Oh no!", path=[], validator="a")
        normal = exceptions.ValidationError("Oh yes!", path=[], validator="b")

        best_match = exceptions.by_relevance(weak="a")

        match = max([weak, normal], key=best_match)
        self.assertIs(match, normal)

        match = max([normal, weak], key=best_match)
        self.assertIs(match, normal)

    def test_strong_keywords_are_higher_priority(self):
        weak = exceptions.ValidationError("Oh no!", path=[], validator="a")
        normal = exceptions.ValidationError("Oh yes!", path=[], validator="b")
        strong = exceptions.ValidationError("Oh fine!", path=[], validator="c")

        best_match = exceptions.by_relevance(weak="a", strong="c")

        match = max([weak, normal, strong], key=best_match)
        self.assertIs(match, strong)

        match = max([strong, normal, weak], key=best_match)
        self.assertIs(match, strong)


class TestRelevanceStrategy(TestCase):
    def test_explainable_weighted_criteria(self):
        grandchild = exceptions.ValidationError(
            "deep",
            path=["leaf"],
            schema_path=["type"],
            validator="type",
        )
        child = exceptions.ValidationError(
            "child",
            context=[grandchild],
            schema_path=[0],
        )
        parent = exceptions.ValidationError(
            "parent",
            context=[child],
            path=["root"],
            schema_path=["anyOf"],
        )

        strategy = exceptions.by_relevance(
            weights=exceptions.RelevanceWeights(
                instance_path=0,
                schema_path=100,
                validator_priority=50,
                error_depth=10,
                type_match=0,
                custom=1,
            ),
            validator_priority={"type": 37},
            custom_weight=lambda error: len(error.message),
        )
        score = strategy(grandchild)

        self.assertIs(grandchild.parent.parent, parent)
        self.assertEqual(
            [
                (criterion.name, criterion.weight)
                for criterion in score.explanation
            ],
            [
                ("schema_path", 100),
                ("validator_priority", 50),
                ("error_depth", 10),
                ("custom", 1),
                ("context_size", 0),
                ("cause", 0),
                ("payload", 0),
                ("input_order", 0),
            ],
        )
        self.assertEqual(
            score.rank[0],
            (-3, ((1, "anyOf"), (0, "0"), (1, "type"))),
        )
        self.assertEqual(score.rank[1], 37)
        self.assertEqual(score.rank[2], -2)
        self.assertEqual(score.rank[3], 4)

    def test_validator_priority_is_stable_regardless_of_messages(self):
        schema = {"minLength": 5, "pattern": "z"}
        errors = list(_LATEST_VERSION(schema).iter_errors("a"))
        strategy = exceptions.by_relevance(
            validator_priority={"pattern": 10},
        )

        forward = exceptions.best_match(iter(errors), key=strategy)
        backward = exceptions.best_match(reversed(errors), key=strategy)

        self.assertEqual(forward.validator, "pattern")
        self.assertEqual(backward.validator, "pattern")
        self.assertEqual(forward.json_path, backward.json_path)

    def test_schema_path_weight_distinguishes_union_branches(self):
        schema = {
            "anyOf": [
                {"properties": {"first": {"type": "string"}}},
                {"properties": {"second": {"type": "integer"}}},
            ],
        }
        instance = {"first": 1, "second": "wrong"}
        errors = list(_LATEST_VERSION(schema).iter_errors(instance))
        strategy = exceptions.by_relevance(
            weights=exceptions.RelevanceWeights(schema_path=10),
        )

        best = exceptions.best_match(iter(errors), key=strategy)
        reversed_best = exceptions.best_match(reversed(errors), key=strategy)

        self.assertEqual(list(best.path), ["first"])
        self.assertEqual(best._contents(), reversed_best._contents())
        self.assertEqual(
            [criterion.name for criterion in strategy(best).explanation],
            [
                "instance_path",
                "custom",
                "validator_priority",
                "error_depth",
                "schema_path",
                "type_match",
                "context_size",
                "cause",
                "payload",
                "input_order",
            ],
        )

    def test_custom_weight_controls_best_match(self):
        schema = {
            "anyOf": [
                {"properties": {"first": {"type": "string"}}},
                {"properties": {"second": {"type": "integer"}}},
            ],
        }
        instance = {"first": 1, "second": "wrong"}
        strategy = exceptions.by_relevance(
            weights=exceptions.RelevanceWeights(instance_path=0),
            custom_weight=lambda error: (
                -(
                    bool(error.absolute_path)
                    and error.absolute_path[-1] == "second"
                )
            ),
        )

        best = exceptions.best_match(
            _LATEST_VERSION(schema).iter_errors(instance),
            key=strategy,
        )

        self.assertEqual(list(best.path), ["second"])

    def test_equal_union_contexts_are_not_deduplicated(self):
        schema = {"anyOf": [{"type": "string"}, {"type": "string"}]}
        errors = list(_LATEST_VERSION(schema).iter_errors(12))
        error, = errors

        self.assertEqual(error.validator, "anyOf")
        self.assertEqual(len(error.context), 2)
        self.assertIsNot(error.context[0], error.context[1])
        self.assertTrue(
            exceptions.relevance(error.context[0]).same_rank(
                exceptions.relevance(error.context[1]),
            ),
        )
        self.assertIs(exceptions.best_match(errors), error)
        self.assertIs(
            exceptions.best_match(
                errors,
                key=lambda each: exceptions.relevance(each),
            ),
            error,
        )
        self.assertEqual(len(error.context), 2)
        self.assertTrue(all(child.parent is error for child in error.context))

    def test_context_ties_can_descend_to_the_deterministic_best_error(self):
        schema = {"anyOf": [{"type": "string"}, {"type": "string"}]}
        errors = list(_LATEST_VERSION(schema).iter_errors(12))
        strategy = exceptions.by_relevance(context_tie="best")

        best = exceptions.best_match(errors, key=strategy)

        self.assertEqual(best.validator, "type")
        self.assertIs(best.parent, errors[0])
        self.assertEqual(len(errors[0].context), 2)

    def test_disabled_custom_weight_is_not_called(self):
        def explode(error):  # pragma: no cover
            raise AssertionError("disabled custom weight was called")

        strategy = exceptions.by_relevance(
            weights=exceptions.RelevanceWeights(custom=0),
            custom_weight=explode,
        )
        score = strategy(exceptions.ValidationError("message"))

        self.assertNotIn(
            "custom",
            [criterion.name for criterion in score.explanation],
        )

    def test_best_match_scores_each_context_error_once_per_level(self):
        scored = []

        def custom_weight(error):
            scored.append(error)
            return 0

        children = [
            exceptions.ValidationError("a", path=["a"], validator="a"),
            exceptions.ValidationError("b", path=["b"], validator="b"),
        ]
        parent = exceptions.ValidationError("parent", context=children)
        strategy = exceptions.by_relevance(custom_weight=custom_weight)

        exceptions.best_match([parent], key=strategy)

        self.assertEqual(scored, [parent, *children])

    def test_context_tree_and_serialization_are_preserved(self):
        schema = {
            "dependentSchemas": {
                "触发": {
                    "properties": {
                        "列表": {
                            "items": {"items": {"type": "string"}},
                        },
                    },
                },
            },
        }
        instance = {"触发": True, "列表": [[12]]}
        errors = list(_LATEST_VERSION(schema).iter_errors(instance))
        error, = errors
        before = (
            error.json_path,
            tuple(error.absolute_path),
            tuple(error.absolute_schema_path),
            str(error),
            error.cause,
            len(error.context),
        )

        strategy = exceptions.by_relevance(
            weights=exceptions.RelevanceWeights(schema_path=10),
        )
        best = exceptions.best_match(errors, key=strategy)
        sorted(errors, key=strategy)

        after = (
            error.json_path,
            tuple(error.absolute_path),
            tuple(error.absolute_schema_path),
            str(error),
            error.cause,
            len(error.context),
        )
        self.assertIs(best, error)
        self.assertEqual(after, before)
        self.assertEqual(error.json_path, "$['列表'][0][0]")

    def test_recursive_ref_nested_arrays_and_parallel_errors_are_stable(self):
        schema = {
            "$defs": {
                "node": {
                    "type": "object",
                    "properties": {
                        "名称": {"type": "string"},
                        "孩子": {"$ref": "#/$defs/node"},
                        "矩阵": {
                            "items": {"items": {"type": "integer"}},
                        },
                    },
                    "required": ["名称"],
                },
            },
            "$ref": "#/$defs/node",
        }
        instance = {
            "名称": "根",
            "孩子": {
                "名称": "子",
                "孩子": {"名称": 12, "矩阵": [["对"], ["错"]]},
            },
        }
        strategy = exceptions.by_relevance(
            weights=exceptions.RelevanceWeights(
                instance_path=0,
                schema_path=10,
            ),
            custom_weight=lambda error: (
                10 * len(error.absolute_path)
                + (
                    error.absolute_path[-2]
                    if len(error.absolute_path) >= 2
                    and isinstance(error.absolute_path[-2], int)
                    else 0
                )
            ),
        )

        def result():
            validator = _LATEST_VERSION(schema)
            errors = list(validator.iter_errors(instance))
            best = exceptions.best_match(iter(errors), key=strategy)
            reversed_best = exceptions.best_match(
                reversed(errors),
                key=strategy,
            )
            return (
                [
                    (
                        error.validator,
                        tuple(error.absolute_path),
                        tuple(error.absolute_schema_path),
                        error.json_path,
                        len(error.context),
                    )
                    for error in errors
                ],
                best.validator,
                best.json_path,
                reversed_best.validator,
                reversed_best.json_path,
            )

        expected = result()
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: result(), range(8)))

        self.assertEqual(results, [expected] * 8)
        self.assertEqual(expected[1], "type")
        self.assertEqual(expected[2], "$['孩子']['孩子']['矩阵'][1][0]")


class TestErrorTree(TestCase):
    def test_it_knows_how_many_total_errors_it_contains(self):
        # FIXME: #442
        errors = [
            exceptions.ValidationError("Something", validator=i)
            for i in range(8)
        ]
        tree = exceptions.ErrorTree(errors)
        self.assertEqual(tree.total_errors, 8)

    def test_it_contains_an_item_if_the_item_had_an_error(self):
        errors = [exceptions.ValidationError("a message", path=["bar"])]
        tree = exceptions.ErrorTree(errors)
        self.assertIn("bar", tree)

    def test_it_does_not_contain_an_item_if_the_item_had_no_error(self):
        errors = [exceptions.ValidationError("a message", path=["bar"])]
        tree = exceptions.ErrorTree(errors)
        self.assertNotIn("foo", tree)

    def test_keywords_that_failed_appear_in_errors_dict(self):
        error = exceptions.ValidationError("a message", validator="foo")
        tree = exceptions.ErrorTree([error])
        self.assertEqual(tree.errors, {"foo": error})

    def test_it_creates_a_child_tree_for_each_nested_path(self):
        errors = [
            exceptions.ValidationError("a bar message", path=["bar"]),
            exceptions.ValidationError("a bar -> 0 message", path=["bar", 0]),
        ]
        tree = exceptions.ErrorTree(errors)
        self.assertIn(0, tree["bar"])
        self.assertNotIn(1, tree["bar"])

    def test_children_have_their_errors_dicts_built(self):
        e1, e2 = (
            exceptions.ValidationError("1", validator="foo", path=["bar", 0]),
            exceptions.ValidationError("2", validator="quux", path=["bar", 0]),
        )
        tree = exceptions.ErrorTree([e1, e2])
        self.assertEqual(tree["bar"][0].errors, {"foo": e1, "quux": e2})

    def test_multiple_errors_with_instance(self):
        e1, e2 = (
            exceptions.ValidationError(
                "1",
                validator="foo",
                path=["bar", "bar2"],
                instance="i1"),
            exceptions.ValidationError(
                "2",
                validator="quux",
                path=["foobar", 2],
                instance="i2"),
        )
        exceptions.ErrorTree([e1, e2])

    def test_it_does_not_contain_subtrees_that_are_not_in_the_instance(self):
        error = exceptions.ValidationError("123", validator="foo", instance=[])
        tree = exceptions.ErrorTree([error])

        with self.assertRaises(IndexError):
            tree[0]

    def test_if_its_in_the_tree_anyhow_it_does_not_raise_an_error(self):
        """
        If a keyword refers to a path that isn't in the instance, the
        tree still properly returns a subtree for that path.
        """

        error = exceptions.ValidationError(
            "a message", validator="foo", instance={}, path=["foo"],
        )
        tree = exceptions.ErrorTree([error])
        self.assertIsInstance(tree["foo"], exceptions.ErrorTree)

    def test_iter(self):
        e1, e2 = (
            exceptions.ValidationError(
                "1",
                validator="foo",
                path=["bar", "bar2"],
                instance="i1"),
            exceptions.ValidationError(
                "2",
                validator="quux",
                path=["foobar", 2],
                instance="i2"),
        )
        tree = exceptions.ErrorTree([e1, e2])
        self.assertEqual(set(tree), {"bar", "foobar"})

    def test_repr_single(self):
        error = exceptions.ValidationError(
            "1",
            validator="foo",
            path=["bar", "bar2"],
            instance="i1",
        )
        tree = exceptions.ErrorTree([error])
        self.assertEqual(repr(tree), "<ErrorTree (1 total error)>")

    def test_repr_multiple(self):
        e1, e2 = (
            exceptions.ValidationError(
                "1",
                validator="foo",
                path=["bar", "bar2"],
                instance="i1"),
            exceptions.ValidationError(
                "2",
                validator="quux",
                path=["foobar", 2],
                instance="i2"),
        )
        tree = exceptions.ErrorTree([e1, e2])
        self.assertEqual(repr(tree), "<ErrorTree (2 total errors)>")

    def test_repr_empty(self):
        tree = exceptions.ErrorTree([])
        self.assertEqual(repr(tree), "<ErrorTree (0 total errors)>")


class TestErrorInitReprStr(TestCase):
    def make_error(self, **kwargs):
        defaults = dict(
            message="hello",
            validator="type",
            validator_value="string",
            instance=5,
            schema={"type": "string"},
        )
        defaults.update(kwargs)
        return exceptions.ValidationError(**defaults)

    def assertShows(self, expected, **kwargs):
        expected = textwrap.dedent(expected).rstrip("\n")

        error = self.make_error(**kwargs)
        message_line, _, rest = str(error).partition("\n")
        self.assertEqual(message_line, error.message)
        self.assertEqual(rest, expected)

    def test_it_calls_super_and_sets_args(self):
        error = self.make_error()
        self.assertGreater(len(error.args), 1)

    def test_repr(self):
        self.assertEqual(
            repr(exceptions.ValidationError(message="Hello!")),
            "<ValidationError: 'Hello!'>",
        )

    def test_unset_error(self):
        error = exceptions.ValidationError("message")
        self.assertEqual(str(error), "message")

        kwargs = {
            "validator": "type",
            "validator_value": "string",
            "instance": 5,
            "schema": {"type": "string"},
        }
        # Just the message should show if any of the attributes are unset
        for attr in kwargs:
            k = dict(kwargs)
            del k[attr]
            error = exceptions.ValidationError("message", **k)
            self.assertEqual(str(error), "message")

    def test_empty_paths(self):
        self.assertShows(
            """
            Failed validating 'type' in schema:
                {'type': 'string'}

            On instance:
                5
            """,
            path=[],
            schema_path=[],
        )

    def test_one_item_paths(self):
        self.assertShows(
            """
            Failed validating 'type' in schema:
                {'type': 'string'}

            On instance[0]:
                5
            """,
            path=[0],
            schema_path=["items"],
        )

    def test_multiple_item_paths(self):
        self.assertShows(
            """
            Failed validating 'type' in schema['items'][0]:
                {'type': 'string'}

            On instance[0]['a']:
                5
            """,
            path=[0, "a"],
            schema_path=["items", 0, 1],
        )

    def test_uses_pprint(self):
        self.assertShows(
            """
            Failed validating 'maxLength' in schema:
                {0: 0,
                 1: 1,
                 2: 2,
                 3: 3,
                 4: 4,
                 5: 5,
                 6: 6,
                 7: 7,
                 8: 8,
                 9: 9,
                 10: 10,
                 11: 11,
                 12: 12,
                 13: 13,
                 14: 14,
                 15: 15,
                 16: 16,
                 17: 17,
                 18: 18,
                 19: 19}

            On instance:
                [0,
                 1,
                 2,
                 3,
                 4,
                 5,
                 6,
                 7,
                 8,
                 9,
                 10,
                 11,
                 12,
                 13,
                 14,
                 15,
                 16,
                 17,
                 18,
                 19,
                 20,
                 21,
                 22,
                 23,
                 24]
            """,
            instance=list(range(25)),
            schema=dict(zip(range(20), range(20))),
            validator="maxLength",
        )

    def test_does_not_reorder_dicts(self):
        self.assertShows(
            """
            Failed validating 'type' in schema:
                {'do': 3, 'not': 7, 'sort': 37, 'me': 73}

            On instance:
                {'here': 73, 'too': 37, 'no': 7, 'sorting': 3}
            """,
            schema={
                "do": 3,
                "not": 7,
                "sort": 37,
                "me": 73,
            },
            instance={
                "here": 73,
                "too": 37,
                "no": 7,
                "sorting": 3,
            },
        )

    def test_str_works_with_instances_having_overriden_eq_operator(self):
        """
        Check for #164 which rendered exceptions unusable when a
        `ValidationError` involved instances with an `__eq__` method
        that returned truthy values.
        """

        class DontEQMeBro:
            def __eq__(this, other):  # pragma: no cover
                self.fail("Don't!")

            def __ne__(this, other):  # pragma: no cover
                self.fail("Don't!")

        instance = DontEQMeBro()
        error = exceptions.ValidationError(
            "a message",
            validator="foo",
            instance=instance,
            validator_value="some",
            schema="schema",
        )
        self.assertIn(repr(instance), str(error))


class TestHashable(TestCase):
    def test_hashable(self):
        {exceptions.ValidationError("")}
        {exceptions.SchemaError("")}


class TestJsonPathRendering(TestCase):
    def validate_json_path_rendering(self, property_name, expected_path):
        error = exceptions.ValidationError(
            path=[property_name],
            message="1",
            validator="foo",
            instance="i1",
        )

        rendered_json_path = error.json_path
        self.assertEqual(rendered_json_path, expected_path)

        re_parsed_name = jsonpath_ng.parse(rendered_json_path).right.fields[0]
        self.assertEqual(re_parsed_name, property_name)

    def test_basic(self):
        self.validate_json_path_rendering("x", "$.x")

    def test_empty(self):
        self.validate_json_path_rendering("", "$['']")

    def test_number(self):
        self.validate_json_path_rendering("1", "$['1']")

    def test_period(self):
        self.validate_json_path_rendering(".", "$['.']")

    def test_single_quote(self):
        self.validate_json_path_rendering("'", r"$['\'']")

    def test_space(self):
        self.validate_json_path_rendering(" ", "$[' ']")

    def test_backslash(self):
        self.validate_json_path_rendering("\\", r"$['\\']")

    def test_backslash_single_quote(self):
        self.validate_json_path_rendering(r"\'", r"$['\\\'']")

    def test_underscore(self):
        self.validate_json_path_rendering("_", r"$['_']")

    def test_double_quote(self):
        self.validate_json_path_rendering('"', """$['"']""")

    def test_hyphen(self):
        self.validate_json_path_rendering("-", "$['-']")

    def test_json_path_injection(self):
        self.validate_json_path_rendering("a[0]", "$['a[0]']")

    def test_open_bracket(self):
        self.validate_json_path_rendering("[", "$['[']")
