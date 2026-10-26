"""每日多时间模型边界；不接触数据库或真实来源。"""

import pytest
from pydantic import ValidationError

from data_service.source_registry import SourceSave


def make_payload(**changes):
    return SourceSave.model_validate({
        "title": "测试来源", "config": {"dataset_slug": "orders", "url": "https://source.test/rows"},
        **changes,
    })


@pytest.mark.parametrize("times", [
    None, [], ["03:00", "03:00"], ["3:00"], ["03:0"], ["24:00"], ["23:60"], ["-1:00"],
    [" 03:00"], ["03:00 "], ["03:00:00"], ["03:00Z"], ["０３:００"], ["03：00"],
    ["03:00\n"], [None], [300], "03:00", ("03:00",), [f"00:{index:02d}" for index in range(25)],
])
def test_schedule_times_rejects_invalid_values(times):
    with pytest.raises(ValidationError):
        make_payload(schedule_times=times)


def test_schedule_times_sorted_and_hour_compatible():
    request = make_payload(schedule_times=["23:59", "09:45", "00:00", "03:15"], schedule_hour=20)
    assert request.schedule_times == ["00:00", "03:15", "09:45", "23:59"]
    assert request.schedule_hour == 0


def test_schedule_allows_24_different_times():
    request = make_payload(schedule_times=[f"{hour:02d}:30" for hour in reversed(range(24))])
    assert len(request.schedule_times) == 24 and request.schedule_times[0] == "00:30"


def test_legacy_hour_and_omission_remain_distinguishable():
    default = make_payload()
    legacy = make_payload(schedule_hour=8)
    assert default.schedule_times is None and default.write_mode is None
    assert "schedule_hour" not in default.model_fields_set
    assert legacy.schedule_hour == 8 and legacy.schedule_times is None
    assert "schedule_hour" in legacy.model_fields_set


@pytest.mark.parametrize("write_mode", [None, "", "merge", "UPSERT", " upsert", True, 1, [], {}])
def test_write_mode_rejects_unknown_choice(write_mode):
    with pytest.raises(ValidationError):
        make_payload(write_mode=write_mode)


@pytest.mark.parametrize("write_mode", ["replace_day", "upsert"])
def test_write_mode_accepts_supported_choice(write_mode):
    assert make_payload(write_mode=write_mode).write_mode == write_mode


def test_source_request_schema_excludes_explicit_null_for_new_fields():
    schema = SourceSave.model_json_schema()["properties"]
    assert schema["schedule_times"]["type"] == "array"
    assert schema["write_mode"]["type"] == "string"
    assert schema["write_mode"]["enum"] == ["replace_day", "upsert"]
