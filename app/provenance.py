"""Per-occurrence provenance projection of the shared recurrence kernel.

While :mod:`app.expander` only returns the instances that survive every
recurrence transformation, this module explains *how* each candidate
occurrence of one series (one ``UID``) reached its final shape inside a
UTC window:

1. **generation** — the authored ``DTSTART`` / ``RRULE`` / ``RDATE``
   properties that produce the occurrence's scheduled slot;
2. **exclusion** — every ``EXDATE`` matching that slot;
3. **override** — the ``RECURRENCE-ID`` component that *reschedules* the
   occurrence (possibly keeping its start, e.g. a summary-only edit),
   *cancels* it, or an orphan override that names a slot the series never
   generates;
4. **result** — the effective ``DTSTART``/``DTEND``/``STATUS`` after every
   transformation. ``final_start``/``final_end`` are ``null`` when the
   occurrence does not survive; ``final_status`` still reports ``CANCELLED``
   for a cancellation and is ``null`` only for an unresurrected ``EXDATE``.

The chains are projected from the same candidate set /expand projects its
instances from (:func:`app.recurrence.compute_candidates`), so the two
endpoints always agree on which occurrences exist and where they finally
land. Candidates are returned when **either** their scheduled slot **or**
their final (post-override) slot overlaps the window — the same "moved in
from outside / moved out" judgment the expander applies per occurrence.
The list is stably sorted by the effective instance time (final start,
else scheduled start), then ``RECURRENCE-ID``.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .recurrence import (
    CandidateSlot,
    ExpansionError,
    GenerationStep,
    OverrideRecord,
    _overlaps,
    _parse_calendar,
    compute_candidates,
    group_events_by_uid,
)

__all__ = [
    "GenerationStep",
    "OccurrenceProvenance",
    "OverrideRecord",
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
    calendar = _parse_calendar(ics_text)
    events = group_events_by_uid(calendar).get(uid)
    if events is None:
        raise ProvenanceError(f"no VEVENT with UID {uid!r}")

    slots = [
        slot
        for slot in compute_candidates(uid, events, window_start, window_end)
        if _candidate_in_window(slot, window_start, window_end)
    ]
    slots.sort(key=lambda c: (_sort_start(c), c.recurrence_id))

    return SeriesProvenance(
        uid=uid,
        window_start=window_start,
        window_end=window_end,
        occurrences=tuple(_occurrence_provenance(slot) for slot in slots),
    )


# ---------------------------------------------------------------------------
# Projection: kernel candidates -> provenance chains
# ---------------------------------------------------------------------------


def _occurrence_provenance(slot: CandidateSlot) -> OccurrenceProvenance:
    """Project one kernel candidate to its provenance chain."""
    return OccurrenceProvenance(
        recurrence_id=slot.recurrence_id,
        generated_by=slot.generated_by,
        excluded_by=slot.excluded_by,
        override=slot.override,
        scheduled_start=slot.scheduled_start,
        scheduled_end=slot.scheduled_end,
        final_start=slot.final_start,
        final_end=slot.final_end,
        final_status=slot.final_status,
    )


def _candidate_in_window(
    candidate: CandidateSlot,
    window_start: dt.datetime,
    window_end: dt.datetime,
) -> bool:
    """Included when the scheduled or the final slot overlaps the window."""
    if candidate.scheduled_start is not None and _overlaps(
        candidate.scheduled_start,
        candidate.scheduled_end,
        window_start,
        window_end,
    ):
        return True
    if candidate.final_start is not None and _overlaps(
        candidate.final_start, candidate.final_end, window_start, window_end
    ):
        return True
    return False


def _sort_start(candidate: CandidateSlot) -> dt.datetime:
    """Effective instance time: rescheduled occurrences sort at new time."""
    if candidate.final_start is not None:
        return candidate.final_start
    if candidate.scheduled_start is not None:
        return candidate.scheduled_start
    return candidate.recurrence_id
