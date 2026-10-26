"""明确选择的文本字段空白转换，核心校验保持严格。"""
from datetime import date
import json
import httpx
import pytest
from pydantic import ValidationError
from data_service.config import Settings
from data_service.datasets import validate_value
from data_service.errors import ServiceError
from data_service.models import FieldDefinition
from data_service.source import SourceConfig, SourceClient, SourceError


def configured(**changes):
    return SourceConfig(dataset_slug="orders", url="https://source.test/data", **changes)


def decode(rows, **changes):
    settings = Settings(database_url="postgresql://unused/unused", allowed_source_hosts=["source.test"])
    with SourceClient(configured(**changes), settings,
                      transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": rows}))) as client:
        return client.query_page(date(2026, 10, 21), 0)


@pytest.mark.parametrize("invalid", [None, "owner", ["owner", "owner"], ["bad.name"], ["owner\n"],
                                    ["_private"], [""], [1], [True], ["x" * 64], [f"c{i}" for i in range(81)]])
def test_conversion_list_rejects_invalid(invalid):
    with pytest.raises(ValidationError):
        configured(text_whitespace_fields=invalid)


def test_optional_default_isolated_and_legacy_unchanged():
    first = configured()
    first.text_whitespace_fields.append("owner")
    assert configured().text_whitespace_fields == []
    original = {"owner": "甲\r\n乙\t丙", "other": "丁\n戊"}
    assert decode([original]) == [original]
    with pytest.raises(ServiceError):
        validate_value(FieldDefinition(name="owner", type="string"), original["owner"])


def test_only_selected_target_after_mapping_preserves_null_and_existing_spaces():
    result = decode([{"source_owner": " 甲\r\n乙\r丙\n丁\t戊  ", "comment": "保留\n原文"},
                     {"source_owner": None, "comment": None}],
                    field_mapping={"owner": "source_owner", "other": "comment"}, text_whitespace_fields=["owner"])
    assert result == [{"owner": " 甲 乙 丙 丁 戊  ", "other": "保留\n原文"}, {"owner": None, "other": None}]
    assert validate_value(FieldDefinition(name="owner", type="string"), result[0]["owner"]) == " 甲 乙 丙 丁 戊  "


@pytest.mark.parametrize("value", [0, 1.5, True, [], {}])
def test_non_text_not_silently_stringified(value):
    with pytest.raises(SourceError, match="只接受字符串"):
        decode([{"owner": value}], text_whitespace_fields=["owner"])


def test_missing_conversion_field_rejected():
    with pytest.raises(SourceError) as caught:
        decode([{}], text_whitespace_fields=["owner"])
    assert caught.value.code == "RESPONSE_SCHEMA_CHANGED"


@pytest.mark.parametrize("bad", ["\x00", "\x01", "\x0b", "\x1f"])
def test_other_controls_preserved_then_rejected_by_dataset(bad):
    result = decode([{"owner": "甲" + bad + "\n乙"}], text_whitespace_fields=["owner"])[0]["owner"]
    assert result == "甲" + bad + " 乙"
    with pytest.raises(ServiceError):
        validate_value(FieldDefinition(name="owner", type="string"), result)


def test_serialization_roundtrip_keeps_explicit_fields():
    config = configured(text_whitespace_fields=["owner"])
    assert SourceConfig.model_validate(json.loads(config.model_dump_json())).text_whitespace_fields == ["owner"]


def test_del_character_keeps_existing_dataset_behavior():
    # 既有string校验禁止C0和代理字符，但接受DEL；本次仅转换已确认的三类空白，不改公共类型协议。
    original = "甲\x7f\n乙"
    result = decode([{"owner": original}], text_whitespace_fields=["owner"])[0]["owner"]
    assert result == "甲\x7f 乙"
    assert validate_value(FieldDefinition(name="owner", type="string"), result) == result
