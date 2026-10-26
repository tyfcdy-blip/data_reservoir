"""4万行合成数据样本；仅报告实际规模，不推断完整历史容量。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
import json
from pathlib import Path
import statistics
from time import perf_counter
import tracemalloc

from data_service.datasets import register_dataset
from data_service.ingest import append_chunk, commit_batch, create_batch
from data_service.models import DatasetDefinition, QueryRequest
from data_service.query import run_query


def test_40000_rows_and_five_parallel_queries(independent_settings, dataset_definitions, day):
    definition = register_dataset(independent_settings, DatasetDefinition.model_validate(dataset_definitions[0]))
    slug = definition["slug"]
    batch = create_batch(independent_settings, slug, day, 40000, None)
    started = perf_counter()
    tracemalloc.start()
    for chunk_index in range(40):
        first = chunk_index * 1000
        records = [{"dt": str(day), "product_id": f"{number:08d}", "shop": f"S{number % 100:03d}",
                    "amount": "0.010001", "units": 1} for number in range(first, first + 1000)]
        append_chunk(independent_settings, batch["id"], chunk_index, records)
    commit_batch(independent_settings, batch["id"])
    _, traced_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    ingest_seconds = perf_counter() - started
    today = day + timedelta(days=1)
    for offset in range(180):
        business_date = today - timedelta(days=offset)
        if business_date == day:
            continue
        empty = create_batch(independent_settings, slug, business_date, 0, None)
        commit_batch(independent_settings, empty["id"])

    samples = {}
    for span in [1, 30, 180]:
        end = day if span == 1 else today
        start = end - timedelta(days=span - 1)
        payload = QueryRequest(start_date=start, end_date=end, group_by=["shop"],
                               aggregates=[{"op": "sum", "field": "amount", "alias": "amount_sum"},
                                           {"op": "count", "alias": "row_count"}], limit=100)

        def timed_query(_iteration, payload=payload):
            tick = perf_counter()
            result = run_query(independent_settings, slug, payload)
            elapsed = (perf_counter() - tick) * 1000
            assert result["total"] == 100 and result["coverage"]["complete"] is True
            assert sum(row["row_count"] for row in result["rows"]) == 40000
            assert sum(Decimal(row["amount_sum"]) for row in result["rows"]) == Decimal("400.040000")
            return elapsed

        with ThreadPoolExecutor(max_workers=5) as executor:
            durations = list(executor.map(timed_query, range(15)))
        samples[str(span)] = {"samples_ms": durations, "median_ms": statistics.median(durations),
                              "p95_ms": sorted(durations)[14], "query_count": len(durations), "concurrency": 5}
    report = {"dataset": slug, "rows": 40000, "nonempty_days": 1, "published_empty_days": 179,
              "groups": 100, "ingest_seconds_with_tracemalloc": ingest_seconds,
              "python_traced_peak_bytes": traced_peak, "memory_measurement": "Python tracemalloc分配峰值，不是进程RSS或数据库内存",
              "query_samples": samples,
              "limitation": "本机独立PostgreSQL、1个有数据日+179个空发布日；未验证720万行历史或服务器硬件性能，不代表生产SLO"}
    destination = Path(__file__).resolve().parents[2] / "evidence" / "independent" / "performance.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
