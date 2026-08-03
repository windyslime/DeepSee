import pytest

from deepsee.pipeline.ui import normalize_ui_map, parse_structured


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


def test_parse_string_with_braces():
    text = '{"is_ui": false, "analysis": "把 {a} 改为 {b}"}'
    assert parse_structured(text)["analysis"] == "把 {a} 改为 {b}"


def test_parse_string_with_unbalanced_brace():
    text = '{"analysis": "进度 50% {"}'
    result = parse_structured(text)
    assert result is not None
    assert result["analysis"] == "进度 50% {"


def test_parse_string_with_escaped_quote():
    text = '{"a": "他说 \\"你好\\" {x}"}'
    result = parse_structured(text)
    assert result is not None
    assert result["a"] == '他说 "你好" {x}'


def test_normalize_elements_non_list_becomes_empty():
    data = normalize_ui_map({"ui_type": "web_page", "elements": 1})
    assert data["elements"] == []


def test_normalize_elements_filters_non_dicts():
    data = normalize_ui_map({"elements": [{"id": 1}, "junk", None]})
    assert data["elements"] == [{"id": 1}]


def test_normalize_target_found_non_bool_ignored():
    data = normalize_ui_map({"target_found": "false"})
    assert data["target_found"] is None


def test_normalize_non_str_fields_blanked():
    data = normalize_ui_map(
        {"layout": 123, "rescreenshot_advice": None, "answer_to_user": 1}
    )
    assert data["layout"] == ""
    assert data["rescreenshot_advice"] == ""
    assert data["answer_to_user"] == ""


def test_normalize_keeps_valid_fields():
    data = normalize_ui_map(
        {"ui_type": "web_page", "target_found": False, "elements": [{"id": 2}]}
    )
    assert data["ui_type"] == "web_page"
    assert data["target_found"] is False
    assert data["elements"] == [{"id": 2}]
