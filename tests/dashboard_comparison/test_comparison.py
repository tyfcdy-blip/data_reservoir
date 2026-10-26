"""真实PostgreSQL隔离库验证：授权、两期聚合、排序分页、快照和失败回滚。"""

from datetime import date, datetime, timedelta
from decimal import Decimal
import os
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import pytest

from data_service import access, auth, db, datasets, ingest, period_comparison, query, query_services
from data_service.api import create_app
from data_service.config import Settings
from data_service.models import DatasetDefinition
from data_service.query_services import QueryServiceSave


SLUG = "period_fixture"


def publish(settings, day, rows):
    batch = ingest.create_batch(settings, SLUG, day, len(rows), "period-" + day.isoformat() + "-" + os.urandom(6).hex())
    for index in range(0, len(rows), 100):
        ingest.append_chunk(settings, batch["id"], index // 100,
                            [dict(record, dt=day.isoformat()) for record in rows[index:index + 100]])
    ingest.commit_batch(settings, batch["id"])
    return batch["id"]


def record(key, amount, refund="1", *, channel="淘宝", shop="甲店", word="文本"):
    return {"id": key, "channel": channel, "shop": shop, "word": word,
            "amount": amount, "refund": refund}


def save(settings, slug, definition, *, limit=500):
    query_services.save_service(settings, slug, QueryServiceSave.model_validate({
        "name": slug, "dataset_slug": SLUG, "definition": definition,
        "allowed_filter_fields": ["channel", "shop"], "max_limit": limit, "enabled": True, "revision": 0}))


@pytest.fixture(scope="module")
def fixture(tmp_path_factory):
    address = os.environ["DS_TEST_DATABASE_URL"]
    assert urlsplit(address).path == "/standalone_query_test", "拒绝操作非隔离数据库"
    source = tmp_path_factory.mktemp("comparison") / "sources.json"
    source.write_text('{"sources":[]}', encoding="utf-8")
    settings = Settings(database_url=address, admin_username="period_test",
                        admin_password="period-isolated-test-only!", source_config_path=str(source))
    with db.connect(settings) as connection:
        connection.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    db.migrate(settings)
    auth.initialize_admin(settings)
    datasets.register_dataset(settings, DatasetDefinition.model_validate({
        "slug": SLUG, "name": "环比隔离测试", "date_field": "dt", "key_fields": ["id"],
        "fields": [{"name": "id", "type": "string", "required": True},
                   {"name": "dt", "type": "date", "required": True, "dimension": True},
                   *[{"name": field, "type": "string", "dimension": True} for field in ["channel", "shop", "word"]],
                   {"name": "amount", "type": "decimal"}, {"name": "refund", "type": "decimal"}]}))
    aggregates = [{"field": "amount", "op": "sum", "alias": "sales"},
                  {"field": "refund", "op": "sum", "alias": "refund"}, {"op": "count", "alias": "rowsCount"}]
    template = {"aggregates": aggregates, "formulas": [{"alias": "refundRate", "expression": "refund / sales", "decimal_places": 6}],
                "require_complete": False}
    save(settings, "test_summary", template)
    save(settings, "test_shops", {**template, "group_by": ["channel", "shop"],
                                  "order_by": [{"field": "sales", "direction": "desc"}]})
    save(settings, "test_dates", {**template, "group_by": ["dt"]})
    save(settings, "test_detail", {"fields": ["id", "amount"], "require_complete": False})
    save(settings, "test_words", {"aggregates": [{"field": "word", "op": "min", "alias": "firstWord"},
                                                    {"field": "word", "op": "count_distinct", "alias": "wordCount"}],
                                  "require_complete": False})
    save(settings, "test_dictionary", {"group_by": ["shop"], "require_complete": False})
    save(settings, "test_fixed", {**template, "filters": [{"field": "channel", "op": "eq", "value": "淘宝"}]})
    save(settings, "test_result_filter", {**template, "group_by": ["channel", "shop"],
                                         "result_filters": [{"field": "sales", "op": "gte", "value": "100"}]})
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    day = today - timedelta(days=4)
    rows_before = [record("a", "100", "20"), record("b", "900", "90", channel="京东"),
                   record("z", "0", "0", shop="零店"), record("n", "-100", "5", shop="负店"),
                   record("u", None, None, shop="空店"), record("nil", "4", "2", channel=None, shop=None),
                   record("space", "8", "1", shop=" 甲店 ")]
    rows_after = [record("a", "200", "10"), record("b", "10", "1", channel="京东"),
                  record("z", "5", "1", shop="零店"), record("n", "-50", "5", shop="负店"),
                  record("u", None, None, shop="空店"), record("nil", "8", "2", channel=None, shop=None),
                  record("space", "16", "1", shop=" 甲店 "), record("new", "20", "1", shop="新店")]
    for current_day, rows in [(day - timedelta(days=2), rows_before), (day - timedelta(days=1), []),
                               (day, rows_after), (day + timedelta(days=1), [])]:
        publish(settings, current_day, rows)
    token = access.create_key(settings, access.KeyCreate.model_validate({
        "name": "只允许淘宝甲店", "grants": {}, "requests_per_minute": 600,
        "policy": {"services": {"test_summary": SLUG, "test_shops": SLUG},
                   "row_scopes": {SLUG: {"mode": "filtered", "groups": [{"conditions": [
                       {"field": "channel", "values": ["淘宝"]}, {"field": "shop", "values": ["甲店"]}]}]}}}}))
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        response = client.post("/api/v1/auth/login", json={"username": settings.admin_username, "password": settings.admin_password})
        assert response.status_code == 200
        yield {"settings": settings, "day": day, "today": today, "client": client,
               "admin": {"Authorization": "Bearer " + response.json()["data"]["token"]},
               "key": {"X-API-Key": token["api_key"]}}


def call(fixture, service="test_summary", *, headers=None, **changes):
    body = {"start_date": fixture["day"].isoformat(), "end_date": (fixture["day"] + timedelta(days=1)).isoformat(),
            "compare_previous_period": True, **changes}
    response = fixture["client"].post("/api/v1/services/" + service + "/query", json=body,
                                      headers=headers or fixture["admin"])
    assert response.status_code == 200, response.text
    return response.json()["data"]


def shop_filters(shop="甲店", channel="淘宝"):
    return [{"field": "channel", "op": "eq", "value": channel}, {"field": "shop", "op": "eq", "value": shop}]


@pytest.mark.parametrize("start,end,expected", [
    ("2026-10-22", "2026-10-22", ("2026-10-21", "2026-10-21")),
    ("2026-10-20", "2026-10-22", ("2026-10-17", "2026-10-19")),
    ("2024-03-01", "2024-03-02", ("2024-02-28", "2024-02-29")),
    ("2026-01-01", "2026-01-03", ("2025-12-29", "2025-12-31"))])
def test_calendar(start, end, expected):
    assert tuple(day.isoformat() for day in period_comparison.previous_period(date.fromisoformat(start), date.fromisoformat(end))) == expected


def test_legacy_response_identical_when_omitted(fixture):
    old = call(fixture, compare_previous_period=False)
    body = {"start_date": fixture["day"].isoformat(), "end_date": (fixture["day"] + timedelta(days=1)).isoformat()}
    response = fixture["client"].post("/api/v1/services/test_summary/query", json=body, headers=fixture["admin"])
    assert response.status_code == 200 and response.json()["data"] == old
    assert "comparison" not in old


def test_aggregate_ratio_not_average_and_same_filters(fixture):
    result = call(fixture, filters=shop_filters())
    comparison = result["comparison"]
    assert comparison["status"] == "ok" and comparison["coverage"]["complete"]
    metric = comparison["rows"][0]["metrics"]
    assert metric["sales"] == {"current": "200.000000", "previous": "100.000000", "delta": "100.000000", "relative_change": "1.000000", "status": "ok"}
    assert metric["refundRate"]["delta"] == "-0.150000"
    assert metric["refundRate"]["relative_change"] == "-0.750000"
    assert len(comparison["published_versions"]) == 2


def test_single_day(fixture):
    result = call(fixture, start_date=(fixture["day"] - timedelta(days=1)).isoformat(), end_date=(fixture["day"] - timedelta(days=1)).isoformat())
    assert result["comparison"]["previous_period"]["start_date"] == (fixture["day"] - timedelta(days=2)).isoformat()
    assert result["comparison"]["rows"][0]["metrics"]["sales"]["status"] == "current_missing"


@pytest.mark.parametrize("shop,status,previous,relative", [("零店", "previous_zero", "0.000000", None),
    ("负店", "ok", "-100.000000", "0.500000"), ("空店", "current_missing", None, None),
    ("新店", "previous_missing", None, None)])
def test_zero_negative_null_and_missing_group(fixture, shop, status, previous, relative):
    result = call(fixture, "test_shops", filters=shop_filters(shop))
    value = result["comparison"]["rows"][0]["metrics"]["sales"]
    assert value["status"] == status and value["previous"] == previous and value["relative_change"] == relative
    if status == "previous_zero":
        assert value["delta"] == "5.000000"


def test_current_top_matches_previous_non_top_and_offset(fixture):
    first = call(fixture, "test_shops", limit=1)
    assert first["rows"][0]["channel"] == "淘宝" and first["rows"][0]["shop"] == "甲店"
    assert first["comparison"]["rows"][0]["metrics"]["sales"]["previous"] == "100.000000"
    page = call(fixture, "test_shops", limit=1, offset=3)
    assert page["comparison"]["rows"][0]["group"] == {key: page["rows"][0][key] for key in ["channel", "shop"]}
    assert page["comparison"]["rows"][0]["metrics"]["sales"]["previous"] == "900.000000"


def test_null_and_whitespace_dimensions_are_exact(fixture):
    result = call(fixture, "test_shops", filters=[{"field": "shop", "op": "is_null", "value": True}])
    assert result["comparison"]["rows"][0]["group"] == {"channel": None, "shop": None}
    assert result["comparison"]["rows"][0]["metrics"]["sales"]["previous"] == "4.000000"
    spaced = call(fixture, "test_shops", filters=shop_filters(" 甲店 "))
    assert spaced["comparison"]["rows"][0]["metrics"]["sales"]["previous"] == "8.000000"


def test_empty_page_has_no_comparison_rows(fixture):
    result = call(fixture, "test_shops", offset=100)
    assert result["rows"] == [] and result["comparison"]["rows"] == [] and result["total"] == 8


def test_missing_dates_never_show_misleading_growth(fixture):
    result = call(fixture, start_date=(fixture["day"] - timedelta(days=1)).isoformat())
    assert result["comparison"]["status"] == "incomplete"
    assert len(result["comparison"]["coverage"]["missing_dates"]) == 2
    value = result["comparison"]["rows"][0]["metrics"]["sales"]
    assert value["status"] == "incomplete" and value["delta"] is None and value["relative_change"] is None


def test_current_missing_date_also_incomplete(fixture):
    result = call(fixture, start_date=(fixture["day"] + timedelta(days=1)).isoformat(), end_date=(fixture["day"] + timedelta(days=2)).isoformat())
    assert not result["coverage"]["complete"] and result["comparison"]["coverage"]["complete"]
    assert result["comparison"]["status"] == "incomplete"


def test_outside_retention_keeps_current(fixture):
    start = fixture["today"] - timedelta(days=179)
    result = call(fixture, start_date=start.isoformat(), end_date=start.isoformat())
    assert result["comparison"]["status"] == "out_of_retention" and result["comparison"]["published_versions"] == []
    assert result["rows"][0]["sales"] is None


@pytest.mark.parametrize("service", ["test_dates", "test_detail", "test_dictionary"])
def test_unsupported_modes_rejected(fixture, service):
    response = fixture["client"].post("/api/v1/services/" + service + "/query", headers=fixture["admin"], json={
        "start_date": fixture["day"].isoformat(), "end_date": fixture["day"].isoformat(), "compare_previous_period": True})
    assert response.status_code == 422 and response.json()["error_code"] == "COMPARISON_NOT_SUPPORTED"


@pytest.mark.parametrize("invalid", ["true", 1, None, {}, []])
def test_compare_flag_is_strict(fixture, invalid):
    response = fixture["client"].post("/api/v1/services/test_summary/query", headers=fixture["admin"], json={
        "start_date": fixture["day"].isoformat(), "end_date": fixture["day"].isoformat(), "compare_previous_period": invalid})
    assert response.status_code == 422


def test_non_numeric_min_max_not_converted_to_decimal(fixture):
    result = call(fixture, "test_words")
    assert result["rows"][0]["firstWord"] == "文本"
    assert list(result["comparison"]["rows"][0]["metrics"]) == ["wordCount"]


def test_same_row_scope_before_both_aggregations(fixture):
    result = call(fixture, headers=fixture["key"])
    assert result["rows"][0]["sales"] == "200.000000"
    assert result["comparison"]["rows"][0]["metrics"]["sales"]["previous"] == "100.000000"
    assert sum(row["row_count"] for row in result["comparison"]["published_versions"]) == 1
    conflicting = call(fixture, headers=fixture["key"], filters=shop_filters(channel="京东"))
    assert conflicting["rows"][0]["sales"] is None
    assert conflicting["comparison"]["rows"][0]["metrics"]["sales"]["previous"] is None


def test_permission_service_and_unknown_filters_stay_enforced(fixture):
    body = {"start_date": fixture["day"].isoformat(), "end_date": fixture["day"].isoformat(), "compare_previous_period": True}
    denied = fixture["client"].post("/api/v1/services/test_words/query", headers=fixture["key"], json=body)
    assert denied.status_code == 403
    denied = fixture["client"].post("/api/v1/services/test_summary/query", headers=fixture["key"], json={**body,
        "filters": [{"field": "id", "op": "eq", "value": "a"}]})
    assert denied.status_code == 422


def test_fixed_filters_and_result_filters_apply_to_previous(fixture):
    fixed = call(fixture, "test_fixed", filters=[{"field": "shop", "op": "eq", "value": "甲店"}])
    assert fixed["comparison"]["rows"][0]["metrics"]["sales"]["previous"] == "100.000000"
    result = call(fixture, "test_result_filter")
    assert result["total"] == 1 and len(result["comparison"]["rows"]) == 1


def test_snapshot_pins_previous_publish(fixture, monkeypatch):
    day = fixture["today"] - timedelta(days=20)
    previous_id = publish(fixture["settings"], day - timedelta(days=1), [record("one", "10")])
    publish(fixture["settings"], day, [record("one", "20")])
    original = period_comparison._matched_previous_rows
    def replace_after_coverage(*args, **kwargs):
        publish(fixture["settings"], day - timedelta(days=1), [record("one", "999")])
        return original(*args, **kwargs)
    monkeypatch.setattr(period_comparison, "_matched_previous_rows", replace_after_coverage)
    result = call(fixture, start_date=day.isoformat(), end_date=day.isoformat())
    assert result["comparison"]["published_versions"][0]["batch_id"] == str(previous_id)
    assert result["comparison"]["rows"][0]["metrics"]["sales"]["previous"] == "10.000000"


def test_comparison_failure_rolls_back_and_next_request_works(fixture, monkeypatch):
    original = period_comparison._matched_previous_rows
    def fail(*args, **kwargs):
        args[0].execute("SELECT 1/0")
    monkeypatch.setattr(period_comparison, "_matched_previous_rows", fail)
    response = fixture["client"].post("/api/v1/services/test_summary/query", headers=fixture["admin"], json={
        "start_date": fixture["day"].isoformat(), "end_date": fixture["day"].isoformat(), "compare_previous_period": True})
    assert response.status_code == 503
    monkeypatch.setattr(period_comparison, "_matched_previous_rows", original)
    assert call(fixture)["comparison"]["status"] == "ok"
    with db.connect(fixture["settings"]) as connection:
        abandoned = connection.execute("SELECT count(*) AS n FROM pg_stat_activity WHERE datname=current_database() AND state='idle in transaction (aborted)'").fetchone()
        assert abandoned["n"] == 0


def test_combined_response_budget_includes_comparison(fixture, monkeypatch):
    old = call(fixture, compare_previous_period=False)
    maximum = query.ENVELOPE_BYTES + query._json_size(old) + 100
    monkeypatch.setattr(query, "MAX_QUERY_BYTES", maximum)
    response = fixture["client"].post("/api/v1/services/test_summary/query", headers=fixture["admin"], json={
        "start_date": fixture["day"].isoformat(), "end_date": (fixture["day"] + timedelta(days=1)).isoformat(), "compare_previous_period": True})
    assert response.status_code == 413


def test_decimal_precision_and_negative_base():
    result = period_comparison.metric_comparison("99999999999999999999999999999999.000002", "99999999999999999999999999999999.000001",
                                                period_status="ok", previous_exists=True)
    assert result["delta"] == "0.000001" and result["relative_change"] == "0.000000"
    result = period_comparison.metric_comparison("-10.000000", "-20.000000", period_status="ok", previous_exists=True)
    assert Decimal(result["relative_change"]) == Decimal("0.5")


def test_large_page_single_previous_query(fixture, monkeypatch):
    day = fixture["today"] - timedelta(days=25)
    publish(fixture["settings"], day - timedelta(days=1), [record(str(index), str(1000-index), shop=f"店{index:04}") for index in range(600)])
    publish(fixture["settings"], day, [record(str(index), str(index), shop=f"店{index:04}") for index in range(600)])
    calls = []
    original = period_comparison._matched_previous_rows
    def traced(*args, **kwargs):
        calls.append(len(args[4]))
        return original(*args, **kwargs)
    monkeypatch.setattr(period_comparison, "_matched_previous_rows", traced)
    result = call(fixture, "test_shops", start_date=day.isoformat(), end_date=day.isoformat(), limit=500)
    assert calls == [500] and result["total"] == 600
    for current, comparison in zip(result["rows"], result["comparison"]["rows"], strict=True):
        index = int(current["shop"][1:])
        assert Decimal(comparison["metrics"]["sales"]["previous"]) == 1000-index


@pytest.mark.parametrize("current,previous,status,delta,relative", [
    ("10", None, "previous_missing", None, None),
    ("0", "0", "previous_zero", "0", None),
    ("-20", "-10", "ok", "-10", "-1.000000"),
    ("0", "10", "ok", "-10", "-1.000000"),
    ("10", "10", "ok", "0", "0.000000")])
def test_metric_edge_cases(current, previous, status, delta, relative):
    result = period_comparison.metric_comparison(current, previous, period_status="ok", previous_exists=True)
    assert result["status"] == status and result["delta"] == delta and result["relative_change"] == relative


def test_outside_retention_skips_previous_records(fixture, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("超保留窗不应执行上期商品查询")
    monkeypatch.setattr(period_comparison, "_matched_previous_rows", forbidden)
    first = fixture["today"] - timedelta(days=179)
    assert call(fixture, start_date=first.isoformat(), end_date=first.isoformat())["comparison"]["status"] == "out_of_retention"


def test_timeout_recovers_and_does_not_leak_internal_error(fixture, monkeypatch):
    original = period_comparison._matched_previous_rows
    def delayed(*args, **kwargs):
        args[0].execute("SET LOCAL statement_timeout = '5ms'")
        args[0].execute("SELECT pg_sleep(0.05)")
    monkeypatch.setattr(period_comparison, "_matched_previous_rows", delayed)
    response = fixture["client"].post("/api/v1/services/test_summary/query", headers=fixture["admin"], json={
        "start_date": fixture["day"].isoformat(), "end_date": fixture["day"].isoformat(), "compare_previous_period": True})
    assert response.status_code == 504 and response.json()["error_code"] == "QUERY_TIMEOUT"
    assert "pg_sleep" not in response.text
    monkeypatch.setattr(period_comparison, "_matched_previous_rows", original)
    assert call(fixture)["comparison"]["status"] == "ok"


def test_sql_filter_value_cannot_change_authorization(fixture):
    result = call(fixture, headers=fixture["key"], filters=shop_filters("甲店' OR TRUE --"))
    assert result["rows"][0]["rowsCount"] == 0
    assert result["comparison"]["rows"][0]["metrics"]["rowsCount"]["previous"] == 0
    assert call(fixture, headers=fixture["key"])["rows"][0]["sales"] == "200.000000"


def test_date_window_maximum_and_future_rejected(fixture):
    for first, last in [(fixture["today"] - timedelta(days=180), fixture["today"]),
                        (fixture["today"], fixture["today"] + timedelta(days=1))]:
        response = fixture["client"].post("/api/v1/services/test_summary/query", headers=fixture["admin"], json={
            "start_date": first.isoformat(), "end_date": last.isoformat(), "compare_previous_period": True})
        assert response.status_code == 422
