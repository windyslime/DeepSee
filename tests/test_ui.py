import pytest

from deepsee.pipeline.ui import parse_structured


def test_parse_plain_json():
    result = parse_structured('{"is_ui": true, "reason": "r"}')
    assert result == {"is_ui": True, "reason": "r"}


def test_parse_with_json_fence():
    text = '```json\n{"is_ui": false}\n```'
    assert parse_structured(text) == {"is_ui": False}


def test_parse_with_surrounding_prose():
    text = '好的,分析如下:\n{"is_ui": true}\n以上是结果'
    assert parse_structured(text) == {"is_ui": True}


def test_parse_nested_json():
    text = '{"analysis": {"elements": [{"id": 1}]}}'
    result = parse_structured(text)
    assert result["analysis"]["elements"][0]["id"] == 1


def test_parse_invalid_returns_none():
    assert parse_structured("not json at all") is None


def test_parse_empty_returns_none():
    assert parse_structured("") is None
    assert parse_structured(None) is None


def test_parse_non_dict_returns_none():
    assert parse_structured("[1, 2, 3]") is None
