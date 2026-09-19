"""Per-occurrence provenance chains for one VEVENT series (one ``UID``).

This is the provenance projection of the shared recurrence kernel in
:mod:`app.recurrence`. The kernel runs, once per UID group, the whole
pipeline both endpoints are views of:

1. **generation** -- the authored ``DTSTART`` / ``RRULE`` / ``RDATE``
   properties that produce an occurrence's scheduled slot;
2. **exclusion** -- every ``EXDATE`` matching that slot;
3. **override** -- the ``RECURRENCE-ID`` component that *reschedules* the
   occurrence (possibly keeping its start, e.g. a summary-only edit),
   *cancels* it, or an orphan override that names a slot the series never
   generates;
4. **result** -- the effective ``DTSTART``/``DTEND``/``STATUS`` after every
   transformation. ``final_start``/``final_end`` are ``null`` when the
   occurrence does not survive; ``final_status`` still reports ``CANCELLED``
   for a cancellation and is ``null`` only for an unresurrected ``EXDATE``.

:mod:`app.expander` projects the same kernel result down to the surviving
instances, so /expand and /provenance can never disagree about one ICS.

Candidates are returned when **either** their scheduled slot **or** their
final (post-override) slot overlaps the window -- the same "moved in from
outside / moved out" judgment the expander applies per occurrence. The
list is stably sorted by the effective instance time (final start, else
scheduled start), then ``RECURRENCE-ID``.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .recurrence import (
    ExpansionError,
    GenerationStep,
    OverrideRecord,
    ResolvedCandidate,
    SeriesResult,
    group_events,
    overlaps,
    parse_calendar,
    resolve_group,
)

# Re-exported for callers/tests that import the generation/override records
# from the provenance module (the HTTP layer does).
__all__ = [
    "GenerationStep",
    "OverrideRecord",
    "OccurrenceProvenance",
    "ProvenanceError",
    "SeriesProvenance",
    "provenance_for_uid",
]


class ProvenanceError(Exception):
    """The provenance request cannot be answered; HTTP layer maps this to 422."""


@dataclass(frozen=True)
class OccurrenceProvenance:
    """The provenance chain of one candidate occurrence."""

    recurrence_id: dt.datetime  # the scheduled slot's start, aware UTC
    generated_by: tuple[GenerationStep, ...]
    excluded_by: tuple[dt.datetime, ...]  # matching EXDATE instants, aware UTC
    override: OverrideRecord | None
    scheduled_start: dt.datetime | None  # None only for master-less orphans
    scheduled_end: dt.datetime | None
    # Effective DTSTART/DTEND after EXDATE/override processing; None when
    # the occurrence does not survive (EXDATE-excluded, CANCELLED master or
    # CANCELLED override). final_status stays CANCELLED for a cancellation
    # and is None only for an EXDATE exclusion without resurrecting override.
    final_start: dt.datetime | None
    final_end: dt.datetime | None
    final_status: str | None


@dataclass(frozen=True)
class SeriesProvenance:
    """Provenance chains for every candidate occurrence of one UID."""

    uid: str
    window_start: dt.datetime
    window_end: dt.datetime
    occurrences: tuple[OccurrenceProvenance, ...]


def provenance_for_uid(
    ics_text: str,
    uid: str,
    window_start: dt.datetime,
    window_end: dt.datetime,
) -> SeriesProvenance:
    """Parse ``ics_text`` and build provenance chains for the series ``uid``.

    ``ExpansionError`` (bad calendar) and :class:`ProvenanceError` (unknown
    UID) propagate to the HTTP layer, which maps both to 422.
    """
    calendar = parse_calendar(ics_text)
    groups = group_events(calendar)
    events = groups.get(uid)
    if events is None:
        raise ProvenanceError(f"no VEVENT with UID {uid!r}")

    series: SeriesResult = resolve_group(uid, events, window_start, window_end)
    return _project_series(series)


# ---------------------------------------------------------------------------
# Projection: resolved kernel candidates -> provenance chains
# ---------------------------------------------------------------------------


def _project_series(series: SeriesResult) -> SeriesProvenance:
    occurrences: list[OccurrenceProvenance] = []
    for candidate in series.candidates:
        chain = _project_candidate(candidate)
        if _candidate_in_window(
            chain, series.window_start, series.window_end
        ):
            occurrences.append(chain)
    occurrences.sort(key=lambda c: (_sort_start(c), c.recurrence_id))
    return SeriesProvenance(
        uid=series.uid,
        window_start=series.window_start,
        window_end=series.window_end,
        occurrences=tuple(occurrences),
    )


def _project_candidate(
    candidate: ResolvedCandidate,
) -> OccurrenceProvenance:
    """Project one resolved slot; every kernel candidate becomes a chain."""
    return OccurrenceProvenance(
        recurrence_id=candidate.recurrence_id,
        generated_by=candidate.generated_by,
        excluded_by=candidate.excluded_by,
        override=candidate.override,
        scheduled_start=candidate.scheduled_start,
        scheduled_end=candidate.scheduled_end,
        final_start=candidate.final_start,
        final_end=candidate.final_end,
        final_status=candidate.final_status,
    )


def _candidate_in_window(
    candidate: OccurrenceProvenance,
    window_start: dt.datetime,
    window_end: dt.datetime,
) -> bool:
    """Included when the scheduled or the final slot overlaps the window."""
    if candidate.scheduled_start is not None and overlaps(
        candidate.scheduled_start,
        candidate.scheduled_end,
        window_start,
        window_end,
    ):
        return True
    if candidate.final_start is not None and overlaps(
        candidate.final_start, candidate.final_end, window_start, window_end
    ):
        return True
    return False


def _sort_start(candidate: OccurrenceProvenance) -> dt.datetime:
    """Effective instance time: rescheduled occurrences sort at new time."""
    if candidate.final_start is not None:
        return candidate.final_start
    if candidate.scheduled_start is not None:
        return candidate.scheduled_start
    return candidate.recurrence_id
