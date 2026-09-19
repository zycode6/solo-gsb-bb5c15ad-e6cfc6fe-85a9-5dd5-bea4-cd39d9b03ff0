"""Shared recurrence kernel: candidate slots → exclusion → override → final.

This module is the single recurrence computation chain behind both
recurrence endpoints:

* :mod:`app.expander` (/expand) projects the surviving *instances*;
* :mod:`app.provenance` (/provenance) projects the per-occurrence
  *provenance chain* (generation, exclusion, override, final state).

Both are projections of :func:`compute_candidates`, so the two endpoints
cannot drift apart: one ICS expanded through either endpoint is judged by
the same candidate set, the same EXDATE handling, the same RECURRENCE-ID
override application, the same timezone windowing and the same per-UID
safety cap.

The kernel pipeline for one UID group (master + RECURRENCE-ID overrides):

1. **candidate slots** — DTSTART/RRULE/RDATE generate scheduled slots
   inside the query window padded backwards by the event duration (so
   events starting before the window but overlapping it still appear);
2. **exclusion** — EXDATE marks the slots it removes (excluded slots stay
   visible as candidates so their exclusion can be reported);
3. **override** — a RECURRENCE-ID component replaces its slot: reschedules
   it, modifies it in place, or cancels it (STATUS:CANCELLED); an override
   naming a slot the series never generates becomes an orphan candidate;
4. **final instances** — each candidate carries its effective
   DTSTART/DTEND/STATUS plus the materialized :class:`Instance` when it
   survives.

Timezones are interpreted as IANA names via zoneinfo, so DST transitions
are honored automatically. Everything is normalized to UTC.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rruleset, rrulestr
from icalendar import Calendar

UTC = dt.timezone.utc

# Safety bound: a single UID group may not expand to more candidate
# occurrences than this inside the (duration-padded) query window.
MAX_OCCURRENCES_PER_UID = 10_000


class ExpansionError(Exception):
    """Invalid calendar input; surfaced to the client as HTTP 422."""


@dataclass
class Instance:
    """One concrete occurrence of a VEVENT, normalized to UTC."""

    uid: str
    recurrence_id: dt.datetime | None  # aware UTC; None for non-recurring events
    start: dt.datetime  # aware UTC
    end: dt.datetime  # aware UTC
    summary: str | None
    status: str
    # True when the effective DTSTART is DATE-valued (an all-day event).
    # DATE values carry no timezone: the expander keeps their authored
    # calendar date as a floating midnight (normalized to UTC), so callers
    # rendering local agendas must recover that date instead of projecting
    # it through a display timezone.
    all_day: bool = False
    # Raw icalendar VALARM components carried by the event that produced
    # this instance: a master's alarms apply to every recurring instance,
    # while a RECURRENCE-ID override uses only its own (possibly empty).
    alarms: list = field(default_factory=list)
    # Raw icalendar ATTENDEE properties in effect for this instance. A
    # recurring instance inherits the master's attendees; a RECURRENCE-ID
    # override that declares any ATTENDEE replaces the whole list (an
    # override without ATTENDEE keeps inheriting the master's).
    attendees: list = field(default_factory=list)
    # Effective TRANSP value ("OPAQUE" blocks time, "TRANSPARENT" does
    # not). A recurring instance inherits the master event's TRANSP; a
    # RECURRENCE-ID override that declares TRANSP explicitly replaces the
    # inherited value for its instance.
    transp: str = "OPAQUE"


# Generation kinds, also used as the stable listing order.
GEN_RRULE = "RRULE"
GEN_RDATE = "RDATE"
GEN_DTSTART = "DTSTART"

# Override actions.
OVR_RESCHEDULED = "RESCHEDULED"  # non-cancelled override, new slot differs
OVR_MODIFIED = "MODIFIED"  # non-cancelled override keeping the same slot
OVR_CANCELLED = "CANCELLED"  # STATUS:CANCELLED override


@dataclass(frozen=True)
class GenerationStep:
    """One authored property that generates an occurrence slot."""

    kind: str  # RRULE | RDATE | DTSTART
    # The unfolded RRULE text (e.g. 'FREQ=DAILY;COUNT=3'); None otherwise.
    rrule: str | None = None


@dataclass(frozen=True)
class OverrideRecord:
    """The RECURRENCE-ID component applied to an occurrence slot."""

    action: str  # RESCHEDULED | MODIFIED | CANCELLED
    recurrence_id: dt.datetime  # aware UTC
    start: dt.datetime | None  # aware UTC; None when cancelled
    end: dt.datetime | None  # aware UTC; None when cancelled
    status: str


@dataclass(frozen=True)
class CandidateSlot:
    """One candidate occurrence after generation → exclusion → override.

    ``recurrence_id`` is the scheduled slot's start — the occurrence's
    identity. ``final_*`` describe the effective DTSTART/DTEND/STATUS after
    every transformation; the times are None when the occurrence does not
    survive (EXDATE-excluded, CANCELLED master or CANCELLED override).
    ``final_status`` still reports CANCELLED for a cancellation and is None
    only for an unresurrected EXDATE. ``instance`` is the materialized
    surviving occurrence, or None when the slot does not survive.
    """

    uid: str
    recurrence_id: dt.datetime  # aware UTC
    generated_by: tuple[GenerationStep, ...]
    excluded_by: tuple[dt.datetime, ...]  # matching EXDATE instants, aware UTC
    override: OverrideRecord | None
    scheduled_start: dt.datetime | None  # None only for master-less orphans
    scheduled_end: dt.datetime | None
    final_start: dt.datetime | None
    final_end: dt.datetime | None
    final_status: str | None
    instance: Instance | None


def group_events_by_uid(calendar: Calendar) -> dict[str, list]:
    """Group VEVENT components by UID; every VEVENT must declare one."""
    groups: dict[str, list] = {}
    for component in calendar.walk("VEVENT"):
        uid = component.get("UID")
        if uid is None:
            raise ExpansionError("VEVENT is missing the required UID property")
        groups.setdefault(str(uid), []).append(component)
    return groups


def compute_candidates(
    uid: str, events: list, window_start: dt.datetime, window_end: dt.datetime
) -> list[CandidateSlot]:
    """Run the full recurrence pipeline for one UID group.

    Returns every candidate slot generated inside the duration-padded
    window plus every override candidate, unordered; callers apply their
    own window filter and ordering when projecting.
    """
    for event in events:
        _validate_tzids(event)

    masters = [e for e in events if "RECURRENCE-ID" not in e]
    overrides = [e for e in events if "RECURRENCE-ID" in e]
    master = masters[0] if masters else None

    candidates: dict[dt.datetime, CandidateSlot] = {}
    state = _MasterExpansion(master, window_start, window_end) if master else None
    if state is not None:
        for when in state.generated_starts:
            candidates[when] = state.base_candidate(uid, when)

    # Overrides replace the occurrence at their RECURRENCE-ID, in source
    # order: the last component for one RID wins. A RID without a generated
    # slot becomes an orphan candidate.
    for override in overrides:
        rid = _to_utc(_recurrence_id(override, master))
        candidate = candidates.get(rid)
        if candidate is None:
            candidate = _orphan_candidate(uid, rid, state)
        record, instance = _apply_override(uid, override, rid, state)
        candidates[rid] = CandidateSlot(
            uid=uid,
            recurrence_id=candidate.recurrence_id,
            generated_by=candidate.generated_by,
            excluded_by=candidate.excluded_by,
            override=record,
            scheduled_start=candidate.scheduled_start,
            scheduled_end=candidate.scheduled_end,
            # A CANCELLED override removes the instance: no effective times
            # (record.start/end are None), but the STATUS is still reported.
            final_start=record.start,
            final_end=record.end,
            final_status=record.status,
            instance=instance,
        )

    return list(candidates.values())


# ---------------------------------------------------------------------------
# Master expansion: candidate slots, exclusion, generation attribution
# ---------------------------------------------------------------------------


class _MasterExpansion:
    """Expansion state derived from the master VEVENT of one UID group."""

    def __init__(
        self, master, window_start: dt.datetime, window_end: dt.datetime
    ) -> None:
        self.summary = _text(master, "SUMMARY")
        self.status = _status(master)
        self.transp = _transp(master)
        self.alarms = [c for c in master.subcomponents if c.name == "VALARM"]
        self.attendees = _props(master, "ATTENDEE")
        start, end, self.all_day = _event_times(master)
        self.start = start
        self.duration = end - start
        self.dtstart_utc = _to_utc(start)

        rrule_props = _props(master, "RRULE")
        rdate_props = _props(master, "RDATE")
        exdate_props = _props(master, "EXDATE")
        self.recurring = bool(rrule_props or rdate_props)

        self.rdate_values: set[dt.datetime] = set()
        for prop in rdate_props:
            self.rdate_values.update(
                _to_utc(value) for value in _datetimes(prop, start)
            )
        self.exdate_values: set[dt.datetime] = set()
        for prop in exdate_props:
            self.exdate_values.update(
                _to_utc(value) for value in _datetimes(prop, start)
            )

        self.rule_frames: list[tuple[GenerationStep, rruleset]] = []
        self.frame_hits: list[tuple[GenerationStep, set[dt.datetime]]] = []
        self.frame_bounds: tuple | None = None
        if self.recurring:
            self.rule_frames = _build_rule_frames(rrule_props, start)
            self.frame_bounds = _frame_bounds(
                start, self.duration, window_start, window_end
            )
            after, before = self.frame_bounds
            for step, rset in self.rule_frames:
                self.frame_hits.append((step, _hits_in_frame(rset, after, before)))

            # Surviving occurrences come from the combined recurrence set
            # (RRULEs + RDATEs + DTSTART - EXDATEs), the exact set /expand
            # projects.
            survivors, _ = _expand_master(
                master, start, self.duration, window_start, window_end
            )
            generated = {_to_utc(occ) for occ in survivors}

            # Re-add slots removed solely by EXDATE so their exclusion can
            # be reported; a stray EXDATE naming no generated slot is not a
            # candidate (it has no generation source).
            for exd in sorted(self.exdate_values):
                if _in_frame(exd, start, after, before) and self._sources(exd):
                    generated.add(exd)
                    if len(generated) > MAX_OCCURRENCES_PER_UID:
                        raise ExpansionError(
                            f"recurrence expands to more than "
                            f"{MAX_OCCURRENCES_PER_UID} occurrences within "
                            "the query window"
                        )
            self.generated_starts = generated
        else:
            # A non-recurring master ignores RDATE/EXDATE; its single
            # occurrence is DTSTART itself.
            self.generated_starts = {self.dtstart_utc}

    def base_candidate(self, uid: str, when: dt.datetime) -> CandidateSlot:
        """Candidate for one generated slot, before overrides apply."""
        scheduled_end = when + self.duration
        excluded = (
            tuple(sorted(exd for exd in self.exdate_values if exd == when))
            if self.recurring
            else ()
        )
        if excluded or self.status == "CANCELLED":
            # EXDATE removes the slot (a later override may still resurrect
            # it); a STATUS:CANCELLED master suppresses the whole series,
            # but the effective STATUS is still reported.
            final_start = final_end = None
            final_status = None if excluded else self.status
            instance = None
        else:
            final_start, final_end, final_status = when, scheduled_end, self.status
            instance = Instance(
                uid=uid,
                recurrence_id=when if self.recurring else None,
                start=when,
                end=scheduled_end,
                summary=self.summary,
                status=self.status,
                all_day=self.all_day,
                alarms=self.alarms,
                attendees=self.attendees,
                transp=self.transp,
            )
        return CandidateSlot(
            uid=uid,
            recurrence_id=when,
            generated_by=tuple(self.sources_for(when)),
            excluded_by=excluded,
            override=None,
            scheduled_start=when,
            scheduled_end=scheduled_end,
            final_start=final_start,
            final_end=final_end,
            final_status=final_status,
            instance=instance,
        )

    def sources_for(self, when_utc: dt.datetime) -> list[GenerationStep]:
        """Generation sources for any instant, with a point lookup outside frame."""
        after, before = self.frame_bounds if self.frame_bounds else (None, None)
        if self.recurring and not _in_frame(when_utc, self.start, after, before):
            # Orphan override for a slot the padded window never reaches:
            # ask each RRULE directly for that one instant instead of the
            # frame.
            steps = [
                step
                for step, rset in self.rule_frames
                if _rule_contains(rset, _localize(when_utc, self.start))
            ]
            if when_utc in self.rdate_values:
                steps.append(GenerationStep(kind=GEN_RDATE))
            if not steps and when_utc == self.dtstart_utc:
                steps.append(GenerationStep(kind=GEN_DTSTART))
            return steps
        return self._sources(when_utc)

    def _sources(self, when_utc: dt.datetime) -> list[GenerationStep]:
        """Generation sources for an instant known to lie inside the frame.

        Every authored property that generates the slot is reported, in
        canonical order: RRULE properties (source order), then RDATE, then
        DTSTART alone. An RDATE duplicating an RRULE hit therefore lists
        both, while the DTSTART seed of an RRULE series is covered by the
        RRULE and is not listed separately.
        """
        steps: list[GenerationStep] = []
        for step, hits in self.frame_hits:
            if when_utc in hits:
                steps.append(step)
        if when_utc in self.rdate_values:
            steps.append(GenerationStep(kind=GEN_RDATE))
        if not steps and when_utc == self.dtstart_utc:
            steps.append(GenerationStep(kind=GEN_DTSTART))
        return steps


def _orphan_candidate(
    uid: str, rid: dt.datetime, state: _MasterExpansion | None
) -> CandidateSlot:
    """Candidate for an override RID the series does not generate."""
    scheduled_start = scheduled_end = None
    sources: tuple[GenerationStep, ...] = ()
    excluded: tuple[dt.datetime, ...] = ()
    if state is not None:
        # Scheduled times are inferred from the master's duration.
        scheduled_start, scheduled_end = rid, rid + state.duration
        sources = tuple(state.sources_for(rid))
        excluded = tuple(sorted(exd for exd in state.exdate_values if exd == rid))
    return CandidateSlot(
        uid=uid,
        recurrence_id=rid,
        generated_by=sources,
        excluded_by=excluded,
        override=None,
        scheduled_start=scheduled_start,
        scheduled_end=scheduled_end,
        final_start=None,
        final_end=None,
        final_status=None,
        instance=None,
    )


def _apply_override(
    uid: str, override, rid: dt.datetime, state: _MasterExpansion | None
) -> tuple[OverrideRecord, "Instance | None"]:
    """Build the override record and replacement instance for one
    RECURRENCE-ID component."""
    status = _status(override)
    if status == "CANCELLED":
        record = OverrideRecord(
            action=OVR_CANCELLED,
            recurrence_id=rid,
            start=None,
            end=None,
            status=status,
        )
        return record, None
    duration = state.duration if state is not None else None
    start, end, all_day = _event_times(override, default_duration=duration)
    start = _to_utc(start)
    end = _to_utc(end)
    action = OVR_RESCHEDULED if start != rid else OVR_MODIFIED
    record = OverrideRecord(
        action=action, recurrence_id=rid, start=start, end=end, status=status
    )
    summary = _text(override, "SUMMARY")
    override_attendees = _props(override, "ATTENDEE")
    instance = Instance(
        uid=uid,
        recurrence_id=rid,
        start=start,
        end=end,
        summary=summary if summary is not None else (state.summary if state else None),
        status=status,
        all_day=all_day,
        alarms=[c for c in override.subcomponents if c.name == "VALARM"],
        # An override declaring any ATTENDEE replaces the master's list
        # wholesale; one without keeps inheriting the master's.
        attendees=(
            override_attendees
            if override_attendees
            else (state.attendees if state else [])
        ),
        # Same rule for TRANSP: an explicit declaration replaces the
        # inherited value, an absent one keeps the master's.
        transp=(
            _transp(override)
            if "TRANSP" in override
            else (state.transp if state else "OPAQUE")
        ),
    )
    return record, instance


# ---------------------------------------------------------------------------
# RRULE / RDATE frames and generation attribution
# ---------------------------------------------------------------------------


def _expand_master(
    event,
    start: dt.datetime,
    duration: dt.timedelta,
    window_start: dt.datetime,
    window_end: dt.datetime,
) -> tuple[list[dt.datetime], bool]:
    """Expand DTSTART/RRULE/RDATE/EXDATE. Returns (occurrences, recurring).

    Occurrence starts are generated in the window padded backwards by the
    event duration, so events starting before the window but overlapping
    it are still produced. Final window filtering happens in the caller.
    """
    rrule_props = _props(event, "RRULE")
    rdate_props = _props(event, "RDATE")
    exdate_props = _props(event, "EXDATE")
    recurring = bool(rrule_props or rdate_props)
    if not recurring:
        return [start], False

    rset = rruleset()
    for prop in rrule_props:
        rule_text = prop.to_ical().decode()
        try:
            rset.rrule(rrulestr(rule_text, dtstart=start))
        except ExpansionError:
            raise
        except Exception as exc:
            raise ExpansionError(f"invalid RRULE {rule_text!r}: {exc}") from exc
    for prop in rdate_props:
        for value in _datetimes(prop, start):
            rset.rdate(value)
    # Per RFC 5545 the DTSTART itself always belongs to the recurrence set
    # (an rruleset with only RDATEs would otherwise drop it). Duplicates
    # collapse later via the per-UID dict keyed on occurrence start.
    rset.rdate(start)
    for prop in exdate_props:
        for value in _datetimes(prop, start):
            rset.exdate(value)

    if start.tzinfo is None:  # floating time: interpreted as UTC
        after = (window_start - duration).replace(tzinfo=None)
        before = window_end.replace(tzinfo=None)
    else:
        after = (window_start - duration).astimezone(start.tzinfo)
        before = window_end.astimezone(start.tzinfo)

    try:
        occurrences: list[dt.datetime] = []
        for occ in rset.between(after, before, inc=True):
            occurrences.append(occ)
            if len(occurrences) > MAX_OCCURRENCES_PER_UID:
                raise ExpansionError(
                    f"recurrence expands to more than {MAX_OCCURRENCES_PER_UID} "
                    "occurrences within the query window"
                )
    except ExpansionError:
        raise
    except (ValueError, TypeError) as exc:
        raise ExpansionError(f"cannot expand recurrence: {exc}") from exc
    return occurrences, True


def _build_rule_frames(rrule_props, start) -> list[tuple[GenerationStep, rruleset]]:
    """One (generation step, rruleset-without-EXDATE) per RRULE property."""
    frames: list[tuple[GenerationStep, rruleset]] = []
    for prop in rrule_props:
        rule_text = prop.to_ical().decode()
        rset = rruleset()
        try:
            rset.rrule(rrulestr(rule_text, dtstart=start))
        except ExpansionError:
            raise
        except Exception as exc:
            raise ExpansionError(f"invalid RRULE {rule_text!r}: {exc}") from exc
        frames.append((GenerationStep(kind=GEN_RRULE, rrule=rule_text), rset))
    return frames


def _frame_bounds(
    start: dt.datetime,
    duration: dt.timedelta,
    window_start: dt.datetime,
    window_end: dt.datetime,
) -> tuple:
    """Duration-padded query bounds expressed in the master's local frame."""
    if start.tzinfo is None:  # floating time: interpreted as UTC
        return (window_start - duration).replace(tzinfo=None), window_end.replace(
            tzinfo=None
        )
    return (window_start - duration).astimezone(start.tzinfo), window_end.astimezone(
        start.tzinfo
    )


def _hits_in_frame(rset, after, before) -> set[dt.datetime]:
    """UTC occurrence instants one RRULE generates within the padded frame."""
    hits: set[dt.datetime] = set()
    try:
        for occ in rset.between(after, before, inc=True):
            hits.add(_to_utc(occ))
            if len(hits) > MAX_OCCURRENCES_PER_UID:
                raise ExpansionError(
                    f"recurrence expands to more than {MAX_OCCURRENCES_PER_UID} "
                    "occurrences within the query window"
                )
    except ExpansionError:
        raise
    except (ValueError, TypeError) as exc:
        raise ExpansionError(f"cannot expand recurrence: {exc}") from exc
    return hits


def _in_frame(when_utc, master_start, after, before) -> bool:
    """True when UTC instant ``when_utc`` lies within the padded local frame."""
    if master_start.tzinfo is None:
        local = when_utc.replace(tzinfo=None)
    else:
        local = when_utc.astimezone(master_start.tzinfo)
    return after <= local <= before


def _localize(when_utc: dt.datetime, master_start: dt.datetime):
    """Project a UTC instant into the RRULE's local frame."""
    if master_start.tzinfo is None:  # floating: naive UTC
        return when_utc.replace(tzinfo=None)
    return when_utc.astimezone(master_start.tzinfo)


def _rule_contains(rset, local) -> bool:
    """True when an rruleset generates exactly ``local`` (point membership)."""
    try:
        return bool(rset.between(local, local, inc=True))
    except (ValueError, TypeError) as exc:
        raise ExpansionError(f"cannot expand recurrence: {exc}") from exc


# ---------------------------------------------------------------------------
# Property helpers
# ---------------------------------------------------------------------------


def _parse_calendar(ics_text: str) -> Calendar:
    try:
        return Calendar.from_ical(ics_text)
    except Exception as exc:
        raise ExpansionError(f"invalid iCalendar input: {exc}") from exc


def _props(event, name: str) -> list:
    """All properties named ``name`` (icalendar keeps repeated properties)."""
    out = []
    for key, value in event.property_items():
        if key == name:
            out.extend(value if isinstance(value, list) else [value])
    return out


def _validate_tzids(event) -> None:
    """Every TZID must be a known IANA timezone, otherwise 422."""
    for _, value in event.property_items():
        for prop in (value if isinstance(value, list) else [value]):
            params = getattr(prop, "params", None)
            if params and "TZID" in params:
                tzid = str(params["TZID"])
                try:
                    ZoneInfo(tzid)
                except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
                    raise ExpansionError(f"unknown timezone: {tzid!r}") from exc


def _event_times(
    event, default_duration: dt.timedelta | None = None
) -> tuple[dt.datetime, dt.datetime, bool]:
    start, start_is_date = _start_time(event)
    if "DTEND" in event:
        value = event["DTEND"].dt
        if isinstance(value, dt.datetime):
            end = value
        elif isinstance(value, dt.date):
            end = dt.datetime.combine(value, dt.time.min, tzinfo=start.tzinfo)
        else:
            raise ExpansionError("unsupported DTEND value type")
    elif "DURATION" in event:
        end = start + event["DURATION"].dt
    elif default_duration is not None:
        # Override without DTEND/DURATION: keep the master's duration.
        end = start + default_duration
    elif start_is_date:
        # RFC 5545: a DATE-valued DTSTART without DTEND lasts the whole day.
        end = start + dt.timedelta(days=1)
    else:
        end = start
    return start, end, start_is_date


def _start_time(event) -> tuple[dt.datetime, bool]:
    prop = event.get("DTSTART")
    if prop is None:
        raise ExpansionError(
            f"VEVENT (UID {event.get('UID', '?')}) is missing DTSTART"
        )
    value = prop.dt
    if isinstance(value, dt.datetime):
        return value, False
    if isinstance(value, dt.date):
        tzid = prop.params.get("TZID")
        tz = ZoneInfo(str(tzid)) if tzid else None
        return dt.datetime.combine(value, dt.time.min, tzinfo=tz), True
    raise ExpansionError("unsupported DTSTART value type")


def _recurrence_id(event, master) -> dt.datetime:
    prop = event["RECURRENCE-ID"]
    rng = prop.params.get("RANGE")
    if rng is not None and str(rng).upper() not in ("THISANDPRIOR",):
        raise ExpansionError(f"RECURRENCE-ID RANGE={rng} is not supported")
    value = prop.dt
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, dt.date):
        tz = None
        if master is not None:
            tz = _event_times(master)[0].tzinfo
        return dt.datetime.combine(value, dt.time.min, tzinfo=tz)
    raise ExpansionError("unsupported RECURRENCE-ID value type")


def _datetimes(prop, ref_start: dt.datetime) -> list[dt.datetime]:
    """Normalize an RDATE/EXDATE property to a list of datetimes."""
    out = []
    for item in prop.dts:
        value = item.dt
        if isinstance(value, dt.datetime):
            out.append(value)
        elif isinstance(value, dt.date):
            out.append(
                dt.datetime.combine(value, dt.time.min, tzinfo=ref_start.tzinfo)
            )
        else:
            raise ExpansionError(
                f"unsupported {prop.name} value type: {type(value).__name__}"
            )
    return out


def _status(event) -> str:
    return str(event.get("STATUS", "CONFIRMED")).upper()


def _transp(event) -> str:
    return str(event.get("TRANSP", "OPAQUE")).upper()


def _text(event, name: str) -> str | None:
    value = event.get(name)
    return str(value) if value is not None else None


def _to_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:  # floating time: interpreted as UTC
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _overlaps(
    start: dt.datetime, end: dt.datetime, window_start: dt.datetime, window_end: dt.datetime
) -> bool:
    if end > start:
        return start < window_end and end > window_start
    # Zero-duration event: in the window iff its instant is.
    return window_start <= start < window_end
