"""Unit tests for the vision response parsing (no network)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from vision_client import _extract_json, _normalize_tags  # noqa: E402


def test_extract_plain_json():
    assert _extract_json('{"description": "a cat", "tags": ["cat"]}')["description"] == "a cat"


def test_extract_json_with_fences_and_prose():
    raw = 'Sure!\n```json\n{"description": "dog", "tags": ["dog", "pet"]}\n```'
    data = _extract_json(raw)
    assert data["tags"] == ["dog", "pet"]


def test_normalize_tags_dedup_lowercase_and_cap():
    tags = _normalize_tags(["Cat", "cat", "#Pet", "Dog", "Bird"], max_tags=3)
    assert tags == ["cat", "pet", "dog"]


def test_normalize_tags_from_string():
    assert _normalize_tags("sky, Clouds; sunset", max_tags=10) == ["sky", "clouds", "sunset"]
