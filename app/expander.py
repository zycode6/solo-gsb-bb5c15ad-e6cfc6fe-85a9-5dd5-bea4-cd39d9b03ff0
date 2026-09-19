"""Core iCalendar recurrence expansion logic -- /expand projection.

The candidate-slot -> exclusion -> override -> final-instance pipeline
lives in the shared kernel :mod:`app.recurrence`. This module projects the
kernel's resolved candidates into concrete surviving instances:

* occurrences removed by EXDATE, cancelled (STATUS:CANCELLED master or
  RECURRENCE-ID override) or never generated are dropped;
* everything is normalized to UTC (floating times are read as UTC) and
  filtered against the query window, then sorted by (start, uid, rid).

Timezones are interpreted as IANA names via zoneinfo, so DST transitions
are honored automatically.

The property helpers and constants used by sibling engines (alarms,
attendance) are re-exported here from the kernel under their legacy names.
"""

from __future__ import annotations

import datetime as dt

from icalendar import Calendar

from .recurrence import (
    MAX_OCCURRENCES_PER_UID,
    UTC,
    ExpansionError,
    Instance,
    SeriesResult,
    overlaps as _overlaps,
    parse_calendar as _parse_calendar,
    props_of as _props,
    resolve_calendar,
    to_utc as _to_utc,
)

__all__ = [
    "MAX_OCCURRENCES_PER_UID",
    "UTC",
    "ExpansionError",
    "Instance",
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
    results = resolve_calendar(calendar, window_start, window_end)
    instances: list[Instance] = []
    for series in results:
        instances.extend(_project_instances(series))
    no_rid = dt.datetime.min.replace(tzinfo=UTC)
    instances = [
        i for i in instances if _overlaps(i.start, i.end, window_start, window_end)
    ]
    instances.sort(key=lambda i: (i.start, i.uid, i.recurrence_id or no_rid))
    return instances


def _project_instances(series: SeriesResult) -> list[Instance]:
    """Project one UID group's resolved candidates to surviving instances.

    The kernel keeps EXDATE-excluded and cancelled slots so provenance can
    explain them; /expand only reports slots with a real effective time.
    The override always replaces the original occurrence (reschedule,
    in-place edit or cancellation), and an orphan override is included.
    """
    instances: list[Instance] = []
    for candidate in series.candidates:
        if candidate.final_start is None:
            continue
        instances.append(
            Instance(
                uid=candidate.uid,
                recurrence_id=(
                    candidate.recurrence_id
                    if candidate.recurring or candidate.from_override
                    else None
                ),
                start=candidate.final_start,
                end=candidate.final_end,
                summary=candidate.summary,
                status=candidate.final_status or "CONFIRMED",
                all_day=candidate.all_day,
                alarms=list(candidate.alarms),
                attendees=list(candidate.attendees),
                transp=candidate.transp,
            )
        )
    return instances
