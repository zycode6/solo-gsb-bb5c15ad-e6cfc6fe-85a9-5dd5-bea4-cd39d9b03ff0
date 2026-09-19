"""Concrete-instance projection of the shared recurrence kernel.

The recurrence computation itself — RRULE/RDATE/EXDATE expansion, EXDATE
exclusion, RECURRENCE-ID overrides, timezone windowing and the per-UID
safety cap — lives in :mod:`app.recurrence`. This module only projects the
surviving instances the /expand endpoint returns: candidates whose final
(post-override) slot overlaps the UTC query window, sorted by
(start, uid, recurrence-id).

``ExpansionError``, ``Instance`` and ``MAX_OCCURRENCES_PER_UID`` remain
importable from here for backwards compatibility.
"""

from __future__ import annotations

import datetime as dt

from icalendar import Calendar

from .recurrence import (
    MAX_OCCURRENCES_PER_UID,
    UTC,
    ExpansionError,
    Instance,
    _overlaps,
    _parse_calendar,
    compute_candidates,
    group_events_by_uid,
)

__all__ = [
    "ExpansionError",
    "Instance",
    "MAX_OCCURRENCES_PER_UID",
    "expand_calendar",
    "expand_parsed_calendar",
]


def expand_calendar(
    ics_text: str, window_start: dt.datetime, window_end: dt.datetime
) -> list[Instance]:
    """Expand all VEVENTs in ``ics_text`` and return instances overlapping
    the UTC window [window_start, window_end), sorted by (start, uid, rid)."""
    calendar = _parse_calendar(ics_text)
    return expand_parsed_calendar(calendar, window_start, window_end)


def expand_parsed_calendar(
    calendar: Calendar, window_start: dt.datetime, window_end: dt.datetime
) -> list[Instance]:
    """Like :func:`expand_calendar` but for an already parsed calendar."""
    instances: list[Instance] = []
    for uid, events in group_events_by_uid(calendar).items():
        for slot in compute_candidates(uid, events, window_start, window_end):
            instance = slot.instance
            if instance is not None and _overlaps(
                instance.start, instance.end, window_start, window_end
            ):
                instances.append(instance)

    no_rid = dt.datetime.min.replace(tzinfo=UTC)
    instances.sort(key=lambda i: (i.start, i.uid, i.recurrence_id or no_rid))
    return instances
