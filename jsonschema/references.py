"""
Public API for the configurable reference retrieval policy.

Example:

    .. code:: python

        from referencing import Registry
        from jsonschema import Draft202012Validator
        from jsonschema.references import RetrievalBudget, RetrievalPolicy

        def retrieve(uri, request):
            ...  # fetch the document, observing request.context

        policy = RetrievalPolicy(
            retrieve,
            tenant="tenant-a",
            budget=RetrievalBudget(max_documents=16, max_bytes=1_000_000),
        )
        validator = Draft202012Validator(
            schema,
            registry=Registry(),
            retrieval_policy=policy,
        )
"""
from jsonschema._references import (
    EventKind,
    RetrievedDocument,
    RetrievalBudget,
    RetrievalEvent,
    RetrievalPolicy,
    RetrievalRequest,
    current_request,
    normalize_uri,
)
from jsonschema.exceptions import (
    BudgetExceeded,
    ByteBudgetExceeded,
    ConnectionBudgetExceeded,
    CyclicReferenceError,
    DocumentBudgetExceeded,
    InvalidReference,
    MaxDepthExceeded,
    RedirectBudgetExceeded,
    ReferenceCancelled,
    ReferenceRetrievalError,
    ReferenceTimeout,
    UnsupportedMediaType,
)

__all__ = [
    "EventKind",
    "RetrievedDocument",
    "RetrievalBudget",
    "RetrievalEvent",
    "RetrievalPolicy",
    "RetrievalRequest",
    "current_request",
    "normalize_uri",
    "BudgetExceeded",
    "ByteBudgetExceeded",
    "ConnectionBudgetExceeded",
    "CyclicReferenceError",
    "DocumentBudgetExceeded",
    "InvalidReference",
    "MaxDepthExceeded",
    "RedirectBudgetExceeded",
    "ReferenceCancelled",
    "ReferenceRetrievalError",
    "ReferenceTimeout",
    "UnsupportedMediaType",
]
