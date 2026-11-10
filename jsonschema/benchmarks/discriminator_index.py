"""
Benchmark for the optional ``oneOf`` candidate index.

Compares the indexed fast path against the unchanged exhaustive path using
only the standard library, so it runs without third-party benchmarking
dependencies.  For both a successful request and a failing (unknown
discriminator) request it records:

* the number of branch validations actually performed,
* wall-clock time over repeated runs,
* the extra memory retained by the index cache, measured with
  :mod:`tracemalloc`.

Run it with ``python -m jsonschema.benchmarks.discriminator_index``.
"""

from __future__ import annotations

from timeit import Timer
import tracemalloc

from jsonschema import Draft202012Validator, _discriminator

NUM_BRANCHES = 50
REPEAT = 2000


def build_schema(n):
    return {
        "oneOf": [
            {
                "properties": {"type": {"const": f"message-{index}"}},
                "required": ["type"],
            }
            for index in range(n)
        ],
    }


def measure(validator, instance):
    _discriminator.reset_stats()
    validator.is_valid(instance)
    return _discriminator.stats()


def timed(validator, instance, repeat=REPEAT):
    timer = Timer(lambda: validator.is_valid(instance))
    samples = timer.repeat(repeat=5, number=repeat)
    best = min(samples)
    return best / repeat * 1e6  # microseconds per validation


def memory_footprint(validator, instance):
    import gc
    import sys

    _discriminator.reset_cache()
    gc.collect()
    tracemalloc.start()
    validator.is_valid(instance)  # build and retain the cached index
    gc.collect()

    # Sum allocations attributable to this module: after a full GC these
    # are exactly the structures retained by the bounded index cache,
    # rather than transient construction objects.
    snapshot = tracemalloc.take_snapshot()
    retained = sum(
        stat.size
        for stat in snapshot.statistics("lineno")
        if any(
            "jsonschema/_discriminator.py" in frame.filename
            for frame in stat.traceback
        )
    )
    tracemalloc.stop()
    return retained


def main():
    schema = build_schema(NUM_BRANCHES)
    success = {"type": f"message-{NUM_BRANCHES - 1}"}
    failure = {"type": "unknown-message"}

    print(
        f"oneOf with {NUM_BRANCHES} branches, "
        f"timing is best of 5 x {REPEAT} runs",
    )
    print()

    for label, instance in (("success", success), ("failure", failure)):
        print(f"--- {label} request: {instance!r} ---")

        _discriminator.enable()
        _discriminator.reset_cache()
        indexed_validator = Draft202012Validator(schema)
        indexed_stats = measure(indexed_validator, instance)
        indexed_us = timed(indexed_validator, instance)
        indexed_mem = memory_footprint(indexed_validator, success)

        _discriminator.disable()
        plain_validator = Draft202012Validator(schema)
        plain_stats = measure(plain_validator, instance)
        plain_us = timed(plain_validator, instance)

        speedup = plain_us / indexed_us if indexed_us else float("inf")
        print(
            f"  exhaustive : {plain_stats['branch_visits']:>3} branch "
            f"visits, {plain_us:8.2f} us/validate",
        )
        print(
            f"  indexed    : {indexed_stats['branch_visits']:>3} branch "
            f"visits, {indexed_us:8.2f} us/validate "
            f"({speedup:.1f}x faster)",
        )
        print(
            f"  fast-path successes: {indexed_stats['fast_path_success']}, "
            f"generic fallbacks: {indexed_stats['fallbacks']}",
        )
        if label == "success":
            print(
                f"  retained index memory: {indexed_mem} bytes "
                "(bounded LRU, cap "
                f"{_discriminator._INDEX_CACHE_MAX_SIZE} schemas)",
            )
        print()

    _discriminator.enable()


if __name__ == "__main__":
    main()
