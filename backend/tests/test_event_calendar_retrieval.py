"""Historical BM25 corpus must exclude observations after the query cutoff."""
import pytest

from app.services import event_calendar_service as service
from app.storage import event_calendar_store as store


OBSERVED = "2026-03-10T10:00:00+00:00"
CUTOFF = "2026-03-11T10:00:00+00:00"
FUTURE = "2026-03-12T10:00:00+00:00"
QUERY = "ambermacro cobaltmacro"


def event(key, title, description=""):
    return {
        "source_id": "official_test", "upstream_id": key,
        "source": "Official publisher", "source_url": "https://www.bls.gov/schedule/",
        "title": title, "description": description, "region": "US", "currency": "USD",
        "category": "other", "importance": 3, "scheduled_at": "2026-03-15T12:30:00+00:00",
        "timezone": "America/New_York", "all_day": False, "status": "scheduled",
        "actual": None, "forecast": None, "previous": None, "unit": None,
    }


def snapshot():
    chunks = store.matching_chunks(QUERY, as_of=CUTOFF)
    context = service.build_agent_context("research", QUERY, as_of=CUTOFF, user_id=921)
    return {
        "ranking": [(revision, value["rank"], value["snippet"]) for revision, value in chunks.items()],
        "items": [(row["id"], row["revision_id"], row["title"], row["snippet"])
                  for row in context["items"]],
    }


@pytest.mark.parametrize("future_change", ["new_long_documents", "existing_event_revision", "both"])
def test_historical_ranks_and_agent_selection_do_not_change_with_future_corpus(future_change):
    store.update_config(921, {"top_k": 1})
    store.ingest_events([
        event("alpha", "ambermacro ambermacro", "Known at the historical cutoff."),
        event("beta", "cobaltmacro cobaltmacro", "Known at the historical cutoff."),
        event("control", "unrelatedcontrol", "An unrelated but visible document."),
    ], observed_at=OBSERVED)
    before = snapshot()
    assert len(before["ranking"]) == 2
    assert len(before["items"]) == 1

    future_rows = []
    if future_change in {"new_long_documents", "both"}:
        future_rows.extend(event(f"future-{index}", "ambermacro", "ambermacro " * 450)
                           for index in range(12))
    if future_change in {"existing_event_revision", "both"}:
        future_rows.append(event("alpha", "ambermacro revised after cutoff", "ambermacro " * 450))
    store.ingest_events(future_rows, observed_at=FUTURE)

    after = snapshot()
    # Equal ranking includes the exact BM25 score, not just membership: future
    # rows must not change IDF, average length, tie breaks, or top-k selection.
    assert after == before
    assert len(store.visible_events(as_of=CUTOFF)) == 3
    assert all("future-" not in row["upstream_id"] for row in store.visible_events(as_of=CUTOFF))
    assert store.matching_chunks("revised", as_of=CUTOFF) == {}


def test_historical_query_remains_stable_after_repeated_future_revisions():
    store.ingest_events([event("alpha", "ambermacro"), event("beta", "cobaltmacro")], observed_at=OBSERVED)
    before = snapshot()
    for number in range(1, 6):
        store.ingest_events([
            event("alpha", f"ambermacro changed version {number}", "ambermacro " * (number * 80))
        ], observed_at=store.timestamp(FUTURE) + number)
    assert snapshot() == before
    assert len(store.visible_events(as_of=CUTOFF)) == 2
