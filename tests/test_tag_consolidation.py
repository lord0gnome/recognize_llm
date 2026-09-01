"""Unit tests for the pure tag-consolidation logic (no network, no Nextcloud)."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
os.environ.setdefault("APP_ID", "recognize_llm")
os.environ.setdefault("APP_PERSISTENT_STORAGE", tempfile.mkdtemp())

import tag_consolidation as tc  # noqa: E402


def test_filter_vocabulary_exclusions():
    tags = [
        (1, "Tagged by recognize v3.0.0", False, False),
        (2, "person:Guillaume Richard", True, True),
        (3, "outdoors", True, True),
        (4, "Nature", True, True),
        (5, "  ", True, True),
        (6, "weird:colon", True, True),
    ]
    assert tc.filter_vocabulary(tags, include_uppercase=False) == [(3, "outdoors")]
    assert tc.filter_vocabulary(tags, include_uppercase=True) == [(3, "outdoors"), (4, "Nature")]


def test_chunk_vocabulary_keeps_variants_adjacent():
    vocab = [("zebra", 1), ("tree", 100), ("apple", 5), ("trees", 20), ("Trees house", 2)]
    chunks = tc.chunk_vocabulary(vocab, size=3)
    assert [n for n, _ in chunks[0]] == ["apple", "tree", "trees"]
    assert [n for n, _ in chunks[1]] == ["Trees house", "zebra"]
    assert tc.chunk_vocabulary([], size=3) == []


def test_parse_merge_response_key_tolerance():
    data = {"merges": [
        {"from": "trees", "to": "tree", "why": "plural"},
        {"source": "ropes", "canonical": "rope", "reason": "plural"},
        {"from": "", "to": "x"},
        "garbage",
    ]}
    assert tc.parse_merge_response(data) == [
        ("trees", "tree", "plural"), ("ropes", "rope", "plural")]
    assert tc.parse_merge_response({}) == []


def test_sanitize_drops_bad_pairs():
    vocab = {"trees", "tree", "leaves", "cars", "person", "rock-climbing", "Dining", "dining"}
    pairs = [
        ("trees", "tree", "plural"),              # ok, both in vocab
        ("leaves", "leaf", "plural"),             # ok, safe singular fold (leaf not in vocab)
        ("cars", "vehicle", "categorize"),        # canonical absent, not a variant -> drop
        ("person", "person", "self"),             # self-map -> drop
        ("ghost", "tree", "hallucinated"),        # source not in vocab -> drop
        ("person:Bob", "person", "excluded"),     # excluded name -> drop
        ("rock-climbing", "rock climbing", "hyphen"),  # safe hyphen fold
        ("Dining", "dining", "case"),             # case fold
        ("trees", "leaves", "duplicate source"),  # second mapping of same source -> drop
    ]
    out = tc.sanitize_pairs(pairs, vocab, include_uppercase=True)
    assert out == [
        ("trees", "tree", "plural"),
        ("leaves", "leaf", "plural"),
        ("rock-climbing", "rock climbing", "hyphen"),
        ("Dining", "dining", "case"),
    ]
    # With uppercase excluded, an uppercase canonical is refused.
    assert tc.sanitize_pairs([("dining", "Dining", "case")], vocab, include_uppercase=False) == []


def test_resolve_mapping_chains_and_cycles():
    counts = {"a": 1, "b": 5, "c": 50, "x": 10, "y": 2}
    pairs = [("a", "b", ""), ("b", "c", ""), ("x", "y", ""), ("y", "x", "")]
    resolved = tc.resolve_mapping(pairs, counts)
    assert resolved["a"] == "c" and resolved["b"] == "c"
    assert resolved["y"] == "x" and "x" not in resolved  # cycle: x wins on count
    assert not (set(resolved) & set(resolved.values()))


def test_resolve_three_cycle():
    pairs = [("a", "b", ""), ("b", "c", ""), ("c", "a", "")]
    resolved = tc.resolve_mapping(pairs, {"a": 1, "b": 9, "c": 3})
    assert resolved == {"a": "b", "c": "b"}


def test_compose_aliases_folds_rechains():
    existing = {"x": "a", "q": "r"}
    new = {"a": "b"}
    out = tc.compose_aliases(existing, new)
    assert out == {"x": "b", "a": "b", "q": "r"}
    assert not (set(out) & set(out.values()))


def test_compose_aliases_skips_cycle():
    # A later run proposing the reverse of an established alias must not break the map.
    out = tc.compose_aliases({"trees": "tree"}, {"tree": "trees"})
    assert out == {"trees": "tree"}
    out = tc.compose_aliases({"a": "b", "b": "c"}, {"c": "a", "x": "a"})
    assert out == {"a": "c", "b": "c", "x": "c"}
    assert not (set(out) & set(out.values()))


def test_apply_aliases_dedup_and_passthrough():
    aliases = {"trees": "tree", "woods": "forest"}
    tags = ["trees", "tree", "woods", "sunset", "person:Bob", "trees"]
    assert tc.apply_aliases(tags, aliases) == ["tree", "forest", "sunset", "person:Bob"]
    assert tc.apply_aliases([], aliases) == []


def test_safe_variant_fold():
    f = tc._safe_variant_fold
    assert f("leaves", "leaf") and f("boxes", "box") and f("cities", "city")
    assert f("rock-climbing", "rock climbing") and f("Dining", "dining")
    assert f("tree", "trees")  # either direction
    assert not f("cars", "vehicle") and not f("poodle", "dog")
