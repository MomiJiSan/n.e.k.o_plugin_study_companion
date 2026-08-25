from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fsrs_bridge import StudyFsrsRating, create_card, get_due_reviews, rate_answer


def test_scheduled_due_timestamp_is_the_review_queue_boundary() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    for rating in (
        StudyFsrsRating.Hard,
        StudyFsrsRating.Good,
        StudyFsrsRating.Easy,
    ):
        updated, schedule = rate_answer(create_card("topic", now), rating, now)
        due = datetime.fromisoformat(schedule["due"].replace("Z", "+00:00"))

        assert get_due_reviews([updated], due - timedelta(seconds=1)) == []
        assert get_due_reviews([updated], due)
