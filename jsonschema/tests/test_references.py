"""
Tests for the configurable reference retrieval policy.
"""
from __future__ import annotations

import threading
import time

from referencing import Registry
import pytest

from jsonschema import Draft202012Validator, exceptions
from jsonschema._references import (
    EventKind,
    RetrievedDocument,
    RetrievalBudget,
    RetrievalPolicy,
    RetrievalRequest,
    normalize_uri,
)


INTEGER = {"type": "integer"}
NUMBER = {"type": "number"}


@pytest.fixture
def make_validator():
    def _make(schema=None, retrieve=None, **policy_kwargs):
        policy = RetrievalPolicy(
            retrieve=retrieve,
            tenant=policy_kwargs.pop("tenant", "tenant"),
            **policy_kwargs,
        )
        validator = Draft202012Validator(
            schema
            if schema is not None
            else {"$ref": "https://example.com/a"},
            registry=Registry(),
            retrieval_policy=policy,
        )
        return validator, policy

    return _make


class TestNormalization:
    def test_fragments_do_not_change_the_document_key(self):
        assert normalize_uri("https://E.com/a#/x") == "https://e.com/a"
        assert normalize_uri("https://e.com/a#") == "https://e.com/a"

    def test_scheme_and_host_are_lowercased(self):
        assert (
            normalize_uri("HTTPS://EXAMPLE.com/x")
            == "https://example.com/x"
        )

    def test_invalid_uri_raises_a_distinguishable_error(self):
        with pytest.raises(exceptions.InvalidReference):
            normalize_uri(123)


class TestBasicRetrieval:
    def test_retrieved_schema_is_used(self, make_validator):
        validator, _ = make_validator(retrieve=lambda uri: INTEGER)
        assert validator.is_valid(4)
        assert not validator.is_valid("foo")

    def test_retriever_receives_request(self, make_validator):
        seen = []

        def retrieve(uri, request):
            seen.append((uri, type(request)) )
            return INTEGER

        validator, _ = make_validator(retrieve=retrieve)
        validator.validate(4)
        assert seen[0][1] is RetrievalRequest

    def test_registry_resources_are_preferred_over_retrieval(self):
        policy = RetrievalPolicy(
            retrieve=lambda uri: NUMBER,
            tenant="t",
        )
        from referencing import Resource
        import referencing.jsonschema

        registry = Registry().with_resource(
            "https://example.com/a",
            Resource(
                contents=INTEGER,
                specification=referencing.jsonschema.DRAFT202012,
            ),
        )
        validator = Draft202012Validator(
            {"$ref": "https://example.com/a"},
            registry=registry,
            retrieval_policy=policy,
        )
        assert validator.is_valid(4)
        assert not validator.is_valid(4.5)

    def test_retrieve_exception_is_chained(self, make_validator):
        boom = RuntimeError("network down")

        def retrieve(uri):
            raise boom

        validator, _ = make_validator(retrieve=retrieve)
        with pytest.raises(exceptions.ValidationError) as exc_info:
            validator.validate(4)
        error = exc_info.value
        assert isinstance(error.cause, exceptions.ReferenceRetrievalError)
        assert error.cause.cause is boom
        assert error.validator == "$ref"


class TestCaching:
    def test_concurrent_refs_retrieve_once_per_request(self):
        calls = []
        entered = threading.Event()
        release = threading.Event()

        def retrieve(uri):
            calls.append(uri)
            entered.set()
            release.wait(2)
            return INTEGER

        policy = RetrievalPolicy(
            retrieve=retrieve,
            tenant="t",
            budget=RetrievalBudget(max_connections=4),
        )
        registry = policy.configure_registry(Registry())
        results = []

        def run():
            try:
                with policy.request():
                    results.append(
                        registry.get_or_retrieve("https://example.com/shared"),
                    )
            except Exception as exc:  # pragma: no cover
                results.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        assert entered.wait(2)
        time.sleep(0.1)
        release.set()
        for thread in threads:
            thread.join(3)
        assert calls == ["https://example.com/shared"]
        assert len(results) == 2
        assert all(result.value.contents == INTEGER for result in results)

    def test_ttl_expiry_forces_a_refresh(self):
        calls = []

        def retrieve(uri):
            calls.append(uri)
            return INTEGER

        policy = RetrievalPolicy(
            retrieve=retrieve,
            tenant="t",
            ttl=0.01,
        )
        validator = Draft202012Validator(
            {"$ref": "https://example.com/a"},
            registry=Registry(),
            retrieval_policy=policy,
        )
        validator.is_valid(4)
        validator.is_valid(4)
        assert len(calls) == 1
        time.sleep(0.02)
        validator.is_valid(4)
        assert len(calls) == 2

    def test_invalidate_drops_a_specific_uri(self):
        calls = []
        policy = RetrievalPolicy(
            retrieve=lambda uri: calls.append(uri) or INTEGER,
            tenant="t",
        )
        validator = Draft202012Validator(
            {"$ref": "https://example.com/a"},
            registry=Registry(),
            retrieval_policy=policy,
        )
        validator.is_valid(4)
        validator.is_valid(4)
        policy.invalidate("https://example.com/a#")
        validator.is_valid(4)
        assert len(calls) == 2

    def test_distinct_tenants_do_not_share_cache(self):
        calls_a = []
        calls_b = []
        policy_a = RetrievalPolicy(
            retrieve=lambda uri: calls_a.append(uri) or INTEGER,
            tenant="a",
        )
        policy_b = RetrievalPolicy(
            retrieve=lambda uri: calls_b.append(uri) or INTEGER,
            tenant="b",
        )
        kwargs = (Registry(), )
        validator_a = Draft202012Validator(
            {"$ref": "https://example.com/a"},
            registry=kwargs[0],
            retrieval_policy=policy_a,
        )
        validator_b = Draft202012Validator(
            {"$ref": "https://example.com/a"},
            registry=kwargs[0],
            retrieval_policy=policy_b,
        )
        validator_a.is_valid(4)
        validator_b.is_valid(4)
        assert calls_a == calls_b == ["https://example.com/a"]


class TestBudgets:
    def test_document_budget_is_exhausted(self, make_validator):
        documents = {
            "https://example.com/d1": INTEGER,
            "https://example.com/d2": INTEGER,
        }

        validator, policy = make_validator(
            schema={
                "allOf": [
                    {"$ref": "https://example.com/d1"},
                    {"$ref": "https://example.com/d2"},
                ],
            },
            retrieve=lambda uri: documents[uri],
            budget=RetrievalBudget(max_documents=1),
        )
        errors = list(validator.iter_errors({}))
        budget_errors = [
            error
            for error in errors
            if isinstance(error.cause, exceptions.DocumentBudgetExceeded)
        ]
        assert budget_errors
        cause = budget_errors[0].cause
        assert cause.budget == exceptions.BudgetExceeded.DOCUMENTS
        assert cause.limit == 1

    def test_byte_budget_is_exhausted(self, make_validator):
        raw = '{"type": "integer", "padding": "' + "a" * 70 + '"}'
        def retrieve(uri):
            return RetrievedDocument(raw=raw)

        validator, _ = make_validator(
            retrieve=retrieve,
            budget=RetrievalBudget(max_bytes=10, max_documents=None),
        )
        errors = list(validator.iter_errors(4))
        budget_errors = [
            error
            for error in errors
            if isinstance(error.cause, exceptions.ByteBudgetExceeded)
        ]
        assert budget_errors
        cause = budget_errors[0].cause
        assert cause.budget == exceptions.BudgetExceeded.BYTES
        assert cause.document_size > 10

    def test_connection_budget_is_exhausted_at_the_boundary(self):
        entered = threading.Event()
        release = threading.Event()
        outcomes = []

        def retrieve(uri):
            entered.set()
            release.wait(2)
            return INTEGER

        policy = RetrievalPolicy(
            retrieve=retrieve,
            tenant="t",
            timeout=5,
            budget=RetrievalBudget(max_connections=1),
        )

        def first():
            with policy.request() as request:
                try:
                    policy.retrieve_resource("https://example.com/a")
                except Exception as exc:  # pragma: no cover
                    outcomes.append(("first", exc))

        def second():
            assert entered.wait(2)
            with policy.request():
                try:
                    policy.retrieve_resource("https://example.com/b")
                except Exception as exc:
                    outcomes.append(("second", exc))

        threads = [threading.Thread(target=first), threading.Thread(target=second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
        release.set()
        assert [name for name, _ in outcomes] == ["second"]
        assert isinstance(
            outcomes[0][1],
            exceptions.ConnectionBudgetExceeded,
        )
        assert outcomes[0][1].budget == "connections"

    def test_max_depth_is_reported_on_validation_errors(self):
        schema = {
            "$id": "https://example.com/tree",
            "type": "object",
            "properties": {"child": {"$ref": "#"}},
        }
        policy = RetrievalPolicy(
            retrieve=lambda uri: schema,
            tenant="t",
            budget=RetrievalBudget(max_depth=3),
        )
        validator = Draft202012Validator(
            schema,
            registry=Registry(),
            retrieval_policy=policy,
        )
        instance = {"child": {"child": {"child": {"child": {}}}}}
        errors = list(validator.iter_errors(instance))
        depth_errors = [
            error
            for error in errors
            if isinstance(error.cause, exceptions.MaxDepthExceeded)
        ]
        assert depth_errors
        error = depth_errors[0]
        assert error.cause.budget == exceptions.BudgetExceeded.DEPTH
        assert error.validator == "$ref"
        assert list(error.path)
        assert list(error.schema_path)

    def test_cyclic_reference_does_not_terminate_validation_thread(self):
        cyclic_a = {
            "$id": "https://example.com/a",
            "$ref": "https://example.com/b",
        }
        cyclic_b = {
            "$id": "https://example.com/b",
            "$ref": "https://example.com/a",
        }
        documents = {
            "https://example.com/a": cyclic_a,
            "https://example.com/b": cyclic_b,
        }
        policy = RetrievalPolicy(
            retrieve=lambda uri: documents[uri],
            tenant="t",
        )
        validator = Draft202012Validator(
            cyclic_a,
            registry=Registry(),
            retrieval_policy=policy,
        )
        errors = list(validator.iter_errors({}))
        cycle_errors = [
            error
            for error in errors
            if isinstance(error.cause, exceptions.CyclicReferenceError)
        ]
        assert cycle_errors
        assert any(uri.endswith("/a") for uri in cycle_errors[0].cause.chain)

    def test_legitimate_recursive_schema_still_validates(self):
        schema = {
            "$id": "https://example.com/recursive",
            "type": "object",
            "properties": {"child": {"$ref": "#"}},
        }
        policy = RetrievalPolicy(
            retrieve=lambda uri: schema,
            tenant="t",
            budget=RetrievalBudget(max_depth=100),
        )
        validator = Draft202012Validator(
            schema,
            registry=Registry(),
            retrieval_policy=policy,
        )
        instance = {"child": {"child": {}}}
        assert validator.is_valid(instance)


class TestRedirectsAndMediaTypes:
    def test_redirects_are_followed_and_audited(self):
        def retrieve(uri, request):
            if uri.endswith("/old"):
                return RetrievedDocument(redirect_to="https://example.com/new")
            return INTEGER

        policy = RetrievalPolicy(retrieve=retrieve, tenant="t")
        with policy.request() as request:
            resource, canonical = policy.retrieve_resource(
                "https://example.com/old",
            )
        assert resource.contents == INTEGER
        assert canonical == normalize_uri("https://example.com/new")
        kinds = [event.kind for event in request.audit()]
        assert EventKind.REDIRECT in kinds
        assert EventKind.RETRIEVE_OK in kinds

    def test_redirect_cycle_raises_cyclic_error(self):
        def retrieve(uri):
            if uri.endswith("/a"):
                return RetrievedDocument(redirect_to="https://example.com/b")
            return RetrievedDocument(redirect_to="https://example.com/a")

        policy = RetrievalPolicy(retrieve=retrieve, tenant="t")
        with pytest.raises(exceptions.CyclicReferenceError):
            policy.retrieve_resource("https://example.com/a")

    def test_too_many_redirects_raises_redirect_budget(self):
        counter = {"n": 0}

        def retrieve(uri):
            counter["n"] += 1
            return RetrievedDocument(
                redirect_to=f"https://example.com/r/{counter['n']}",
            )

        policy = RetrievalPolicy(
            retrieve=retrieve,
            tenant="t",
            budget=RetrievalBudget(max_redirects=2),
        )
        with pytest.raises(exceptions.RedirectBudgetExceeded) as exc_info:
            policy.retrieve_resource("https://example.com/r/0")
        assert exc_info.value.budget == exceptions.BudgetExceeded.REDIRECTS

    def test_unsupported_media_type_is_rejected(self):
        def retrieve(uri):
            return RetrievedDocument(
                contents=INTEGER,
                raw="{}",
                media_type="text/html",
            )

        policy = RetrievalPolicy(
            retrieve=retrieve,
            tenant="t",
            allowed_media_types={"application/json"},
        )
        with pytest.raises(exceptions.UnsupportedMediaType) as exc_info:
            policy.retrieve_resource("https://example.com/a")
        assert exc_info.value.media_type == "text/html"


class TestCancellationAndTimeouts:
    def test_cancellation_aborts_without_poisoning_cache(self):
        started = threading.Event()
        release = threading.Event()
        active_request = []

        def retrieve(uri, request):
            started.set()
            active_request.append(request)
            release.wait(2)
            raise RuntimeError("released after cancel")

        policy = RetrievalPolicy(retrieve=retrieve, tenant="t", timeout=5)
        outcome = []

        def run():
            with policy.request() as active:
                try:
                    policy.retrieve_resource("https://example.com/a")
                except Exception as exc:
                    outcome.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        assert started.wait(2)
        active_request[0].cancel()
        release.set()
        thread.join(3)
        assert outcome
        assert isinstance(outcome[0], exceptions.ReferenceCancelled)
        assert not policy._cache

    def test_timeout_does_not_write_partial_result(self):
        def retrieve(uri):
            time.sleep(0.3)
            return INTEGER

        policy = RetrievalPolicy(
            retrieve=retrieve,
            tenant="t",
            timeout=0.05,
            budget=RetrievalBudget(),
        )
        with pytest.raises(exceptions.ReferenceTimeout):
            policy.retrieve_resource("https://example.com/slow")
        # The cache entry appears only if the leader completed in time; give
        # the background fetch a moment to finish and verify it never caches.
        time.sleep(0.4)
        assert "https://example.com/slow" not in policy._cache

    def test_retriever_sees_request_context_in_worker_thread(self):
        seen = []

        def retrieve(uri, request):
            seen.append(request.context.get("trace"))
            return RetrievedDocument(
                raw='{"type": "integer"}',
                media_type="application/json",
            )

        policy = RetrievalPolicy(retrieve=retrieve, tenant="t", timeout=5)
        with policy.request(context={"trace": "trace-123"}):
            resource, _ = policy.retrieve_resource(
                "https://example.com/a",
            )
        assert seen == ["trace-123"]
        assert resource.contents == INTEGER



class TestAudit:
    def test_request_audit_records_each_budgeted_retrieval(self):
        def retrieve(uri):
            return INTEGER

        policy = RetrievalPolicy(retrieve=retrieve, tenant="t")
        validator = Draft202012Validator(
            {"$ref": "https://example.com/a"},
            registry=Registry(),
            retrieval_policy=policy,
        )
        with policy.request() as request:
            validator = Draft202012Validator(
                {"$ref": "https://example.com/a"},
                registry=Registry(),
                retrieval_policy=policy,
            )
            validator.validate(4)
            validator.validate(4)
        kinds = [event.kind for event in request.audit()]
        assert EventKind.RETRIEVE_OK in kinds
        assert EventKind.CACHE_HIT in kinds
        assert request.documents == 1


class TestLegacyRefResolver:
    def test_legacy_resolver_does_not_bypass_the_budget(self):
        from jsonschema.validators import _RefResolver

        policy = RetrievalPolicy(
            retrieve=lambda uri: INTEGER,
            tenant="legacy",
            budget=RetrievalBudget(max_documents=1),
        )
        resolver = _RefResolver.from_schema(
            {},
            retrieval_policy=policy,
        )
        scope, resolved = resolver.resolve("https://example.com/a")
        assert resolved == INTEGER

        policy = RetrievalPolicy(
            retrieve=lambda uri: INTEGER,
            tenant="legacy2",
            budget=RetrievalBudget(max_bytes=1),
        )
        resolver = _RefResolver.from_schema({}, retrieval_policy=policy)
        with pytest.raises(Exception):
            resolver.resolve("https://example.com/a")


class TestDynamicRefsAndAnchors:
    def test_dynamic_anchor_resolution_within_a_fetched_document(self):
        tree = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://example.com/tree",
            "$dynamicAnchor": "node",
            "type": "object",
            "properties": {
                "data": {"type": "integer"},
                "children": {
                    "type": "array",
                    "items": {"$dynamicRef": "#node"},
                },
            },
        }
        policy = RetrievalPolicy(retrieve=lambda uri: tree, tenant="t")
        validator = Draft202012Validator(
            {"$ref": "https://example.com/tree"},
            registry=Registry(),
            retrieval_policy=policy,
        )
        valid_instance = {"children": [{"data": 1}, {"data": 2}]}
        invalid_instance = {"children": [{"data": "no"}]}
        assert validator.is_valid(valid_instance)
        assert not validator.is_valid(invalid_instance)

    def test_fragment_pointer_and_named_anchor_remain_distinguishable(self):
        from referencing import Resource
        import referencing.jsonschema

        document = {
            "$id": "https://example.com/doc",
            "$defs": {
                "a": {"$anchor": "alpha", "type": "integer"},
                "b": {"type": "string"},
            },
        }
        registry = Registry().with_resource(
            "https://example.com/doc",
            Resource(
                contents=document,
                specification=referencing.jsonschema.DRAFT202012,
            ),
        )
        policy = RetrievalPolicy(retrieve=lambda uri: INTEGER, tenant="t")
        validator = Draft202012Validator(
            {"$ref": "https://example.com/doc#alpha"},
            registry=registry,
            retrieval_policy=policy,
        )
        assert validator.is_valid(3)
        assert not validator.is_valid("x")

    def test_validators_do_not_share_mutable_state_across_requests(self):
        policy = RetrievalPolicy(retrieve=lambda uri: INTEGER, tenant="t")
        schema = {"$ref": "https://example.com/a"}
        first = Draft202012Validator(
            schema,
            registry=Registry(),
            retrieval_policy=policy,
        )
        second = Draft202012Validator(
            schema,
            registry=Registry(),
            retrieval_policy=policy,
        )
        assert first._resolver is not second._resolver
        errors = []

        def validate(validator, value):
            with policy.request():
                errors.extend(validator.iter_errors(value))

        threads = [
            threading.Thread(target=validate, args=(first, "a")),
            threading.Thread(target=validate, args=(second, 7)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
        assert len(errors) == 1
        assert errors[0].message == "'a' is not of type 'integer'"
