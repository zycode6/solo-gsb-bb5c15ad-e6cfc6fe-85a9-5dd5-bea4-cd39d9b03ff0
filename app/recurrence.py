"""Shared recurrence kernel: candidate slots -> exclusions -> overrides.

Both public recurrence views of the service are projections of *one*
computation implemented here:

1. **candidate generation** -- the authored ``DTSTART`` / ``RRULE`` /
   ``RDATE`` properties of a series' master VEVENT produce candidate
   occurrence slots inside a duration-padded UTC query window;
2. **exclusion** -- ``EXDATE`` values matching a slot mark it excluded;
3. **override** -- every ``RECURRENCE-ID`` component is applied to the slot
   it names (reschedule, in-place modification or cancellation), creating
   an *orphan* candidate when the series never generates that slot;
4. **final instance** -- the effective slot/STATUS after steps 2 and 3.

:mod:`app.expander` projects only the instances that survive the chain
(overlap-filtered, ``(start, uid, recurrence_id)`` sorted);
:mod:`app.provenance` projects the same resolved candidates into
generation/exclusion/override/final-state records sorted by effective time.
Because both endpoints consume the identical :class:`ResolvedCandidate`
result, their view of one ICS cannot drift.

Timezones are interpreted as IANA names via zoneinfo, so DST transitions
are honored automatically.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rruleset, rrulestr
from icalendar import Calendar

UTC = dt.timezone.utc

# Safety bound: a single UID group may not generate more candidate
# occurrences than this inside the (duration-padded) query window.
MAX_OCCURRENCES_PER_UID = 10_000


class ExpansionError(Exception):
    """Invalid calendar input; surfaced to the client as HTTP 422."""


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


@dataclass
class Instance:
    """A fully resolved, surviving event instance (times normalized to UTC)."""

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


@dataclass
class ResolvedCandidate:
    """One slot after the full candidate -> exclude -> override pipeline.

    All datetimes are aware UTC. ``scheduled_start`` is None only for an
    orphan override when the series has no master (no duration to infer a
    slot from). ``final_*`` describe the effective instance:

    * surviving occurrence / non-cancelled override -> real times;
    * cancelled via override -> None times, ``final_status == "CANCELLED"``;
    * removed by EXDATE without a resurrecting override -> everything None;
    * STATUS:CANCELLED master without override -> None times, CANCELLED.
    """

    uid: str
    recurrence_id: dt.datetime
    generated_by: tuple[GenerationStep, ...]
    excluded_by: tuple[dt.datetime, ...]  # matching EXDATE instants
    override: OverrideRecord | None
    scheduled_start: dt.datetime | None
    scheduled_end: dt.datetime | None
    final_start: dt.datetime | None
    final_end: dt.datetime | None
    final_status: str | None
    # Presentation state in effect for the surviving instance; mirrors the
    # fields /expand and the alarms/attendance projections render from.
    summary: str | None
    all_day: bool
    alarms: list = field(default_factory=list)
    attendees: list = field(default_factory=list)
    transp: str = "OPAQUE"
    recurring: bool = True  # False only for a plain (non-recurring) master
    from_override: bool = False  # the surviving instance comes from an override


@dataclass(frozen=True)
class SeriesResult:
    """Resolved candidates for every slot of one UID (unfiltered, unsorted)."""

    uid: str
    window_start: dt.datetime
    window_end: dt.datetime
    candidates: tuple[ResolvedCandidate, ...]


# ---------------------------------------------------------------------------
# Calendar parsing and UID grouping
# ---------------------------------------------------------------------------


def parse_calendar(ics_text: str) -> Calendar:
    try:
        return Calendar.from_ical(ics_text)
    except Exception as exc:
        raise ExpansionError(f"invalid iCalendar input: {exc}") from exc


def group_events(calendar: Calendar) -> dict[str, list]:
    """All VEVENTs grouped by their required UID, in first-seen order."""
    groups: dict[str, list] = {}
    for component in calendar.walk("VEVENT"):
        uid = component.get("UID")
        if uid is None:
            raise ExpansionError("VEVENT is missing the required UID property")
        groups.setdefault(str(uid), []).append(component)
    return groups


def resolve_calendar(
    calendar: Calendar, window_start: dt.datetime, window_end: dt.datetime
) -> list[SeriesResult]:
    """Resolve every UID group of an already parsed calendar."""
    return [
        resolve_group(uid, events, window_start, window_end)
        for uid, events in group_events(calendar).items()
    ]


# ---------------------------------------------------------------------------
# One UID group: master + overrides
# ---------------------------------------------------------------------------


def resolve_group(
    uid: str, events: list, window_start: dt.datetime, window_end: dt.datetime
) -> SeriesResult:
    """Run the candidate -> exclude -> override kernel for one UID group."""
    for event in events:
        validate_tzids(event)

    masters = [e for e in events if "RECURRENCE-ID" not in e]
    overrides = [e for e in events if "RECURRENCE-ID" in e]
    master = masters[0] if masters else None

    ctx = _MasterContext.build(master, window_start, window_end)
    candidates: dict[dt.datetime, ResolvedCandidate] = {}

    for when in ctx.generated_slots:
        scheduled_end = when + ctx.duration
        excluded = (
            tuple(sorted(exd for exd in ctx.exdate_values if exd == when))
            if ctx.recurring
            else ()
        )
        if excluded:
            # EXDATE removes the slot; a later override may still resurrect
            # it (handled in the override pass below).
            final_start = final_end = final_status = None
        elif ctx.master_status == "CANCELLED":
            # A STATUS:CANCELLED master suppresses the whole series; the
            # effective STATUS is still reported (with no times), unless an
            # override resurrects this specific slot.
            final_start = final_end = None
            final_status = "CANCELLED"
        else:
            final_start, final_end = when, scheduled_end
            final_status = ctx.master_status
        candidates[when] = ResolvedCandidate(
            uid=uid,
            recurrence_id=when,
            generated_by=tuple(ctx.sources(when)),
            excluded_by=excluded,
            override=None,
            scheduled_start=when,
            scheduled_end=scheduled_end,
            final_start=final_start,
            final_end=final_end,
            final_status=final_status,
            summary=ctx.master_summary,
            all_day=ctx.all_day,
            alarms=list(ctx.master_alarms),
            attendees=list(ctx.master_attendees),
            transp=ctx.master_transp,
            recurring=ctx.recurring,
            from_override=False,
        )

    # Overrides replace the occurrence at their RECURRENCE-ID, in source
    # order: the last component for one RID wins (dict assignment, same
    # semantics as the generated-slot pass). A RID without a generated slot
    # becomes an orphan candidate.
    for override in overrides:
        rid = to_utc(recurrence_id_of(override, master))
        record = _override_record(override, rid, ctx.duration)
        candidate = candidates.get(rid)
        if candidate is None:
            candidate = ResolvedCandidate(
                uid=uid,
                recurrence_id=rid,
                generated_by=tuple(ctx.sources(rid)),
                excluded_by=tuple(
                    sorted(exd for exd in ctx.exdate_values if exd == rid)
                ),
                override=None,
                scheduled_start=(rid if ctx.duration is not None else None),
                scheduled_end=(
                    rid + ctx.duration if ctx.duration is not None else None
                ),
                final_start=None,
                final_end=None,
                final_status=None,
                summary=ctx.master_summary,
                all_day=ctx.all_day,
                alarms=[],
                attendees=list(ctx.master_attendees),
                transp=ctx.master_transp,
                recurring=ctx.recurring,
                from_override=True,
            )

        if record.action == OVR_CANCELLED:
            # A CANCELLED override removes the instance: no effective times,
            # but the effective STATUS is still CANCELLED.
            candidate.final_start = None
            candidate.final_end = None
        else:
            candidate.final_start = record.start
            candidate.final_end = record.end
            candidate.summary = (
                text_of(override, "SUMMARY")
                if "SUMMARY" in override
                else ctx.master_summary
            )
            _, _, override_all_day = event_times(
                override, default_duration=ctx.duration
            )
            candidate.all_day = override_all_day
            candidate.alarms = [
                c for c in override.subcomponents if c.name == "VALARM"
            ]
            # An override declaring any ATTENDEE replaces the master's list
            # wholesale; one without keeps inheriting the master's.
            override_attendees = props_of(override, "ATTENDEE")
            if override_attendees:
                candidate.attendees = override_attendees
            # Same rule for TRANSP: an explicit declaration replaces the
            # inherited value, an absent one keeps the master's.
            if "TRANSP" in override:
                candidate.transp = transp_of(override)
        candidate.final_status = record.status
        candidate.override = record
        candidate.from_override = True
        candidates[rid] = candidate

    return SeriesResult(
        uid=uid,
        window_start=window_start,
        window_end=window_end,
        candidates=tuple(candidates.values()),
    )


# ---------------------------------------------------------------------------
# Master context: candidate generation and generation attribution
# ---------------------------------------------------------------------------


class _MasterContext:
    """Precomputed master-derived generation state for one UID group."""

    def __init__(self):
        self.duration: dt.timedelta | None = None
        self.master_status = "CONFIRMED"
        self.master_summary: str | None = None
        self.master_transp = "OPAQUE"
        self.master_alarms: list = []
        self.master_attendees: list = []
        self.all_day = False
        self.master_start: dt.datetime | None = None  # authored frame/naive UTC
        self.dtstart_utc: dt.datetime | None = None
        self.recurring = False
        self.has_master = False
        # One (generation step, rruleset-without-EXDATE) per RRULE property.
        self.rule_frames: list[tuple[GenerationStep, object]] = []
        # UTC hits of each RRULE within the duration-padded query frame.
        self.frame_hits: list[tuple[GenerationStep, set[dt.datetime]]] = []
        self.rdate_values: set[dt.datetime] = set()
        self.exdate_values: set[dt.datetime] = set()
        self.after = None
        self.before = None
        self.generated_slots: set[dt.datetime] = set()

    @classmethod
    def build(cls, master, window_start, window_end) -> "_MasterContext":
        ctx = cls()
        if master is None:
            return ctx
        ctx.has_master = True

        m_start, m_end, m_all_day = event_times(master)
        ctx.master_start = m_start
        ctx.dtstart_utc = to_utc(m_start)
        ctx.duration = m_end - m_start
        ctx.all_day = m_all_day
        ctx.master_status = status_of(master)
        ctx.master_summary = text_of(master, "SUMMARY")
        ctx.master_transp = transp_of(master)
        ctx.master_alarms = [c for c in master.subcomponents if c.name == "VALARM"]
        ctx.master_attendees = props_of(master, "ATTENDEE")

        rrule_props = props_of(master, "RRULE")
        rdate_props = props_of(master, "RDATE")
        exdate_props = props_of(master, "EXDATE")
        ctx.recurring = bool(rrule_props or rdate_props)

        def normalized(prop):
            values = []
            for value in datetimes_of(prop, m_start):
                # dateutil rejects mixing floating and timezone-bound values
                # inside one ruleset (TypeError -> 422 in the expander);
                # enforce the same awareness rule whenever the values feed a
                # ruleset, so both projections share the strict behavior
                # instead of silently equating UTC instants. (A non-recurring
                # master ignores RDATE/EXDATE for generation, so its values
                # are only normalized for orphan-override attribution below.)
                if ctx.recurring and (value.tzinfo is None) != (
                    m_start.tzinfo is None
                ):
                    raise ExpansionError(
                        "cannot expand recurrence: can't compare offset-naive "
                        "and offset-aware datetimes"
                    )
                values.append(to_utc(value))
            return values

        for prop in rdate_props:
            ctx.rdate_values.update(normalized(prop))
        for prop in exdate_props:
            ctx.exdate_values.update(normalized(prop))

        if not ctx.recurring:
            # A non-recurring master ignores RDATE/EXDATE exactly like the
            # historical expander; its single occurrence is DTSTART itself.
            ctx.generated_slots = {ctx.dtstart_utc}
            return ctx

        ctx.rule_frames = _build_rule_frames(rrule_props, m_start)
        ctx.after, ctx.before = frame_bounds(
            m_start, ctx.duration, window_start, window_end
        )
        for step, rset in ctx.rule_frames:
            ctx.frame_hits.append(
                (step, hits_in_frame(rset, ctx.after, ctx.before))
            )

        # The candidate set is the union of every RRULE's in-frame hits,
        # the authored RDATEs and DTSTART itself (per RFC 5545 DTSTART
        # always belongs to the set), restricted to the duration-padded
        # frame. EXDATE slots are kept too: exclusion is a separate pass so
        # provenance can report the chain and an override can resurrect.
        generated: set[dt.datetime] = set()
        for _, hits in ctx.frame_hits:
            generated.update(hits)
        generated.update(
            value
            for value in ctx.rdate_values
            if in_frame(value, m_start, ctx.after, ctx.before)
        )
        if in_frame(ctx.dtstart_utc, m_start, ctx.after, ctx.before):
            generated.add(ctx.dtstart_utc)
        # Slots removed solely by EXDATE stay in the candidate set; a stray
        # EXDATE naming no generated slot is not a candidate (it has no
        # generation source).
        for exd in sorted(ctx.exdate_values):
            if (
                in_frame(exd, m_start, ctx.after, ctx.before)
                and ctx._sources(exd)
            ):
                generated.add(exd)
        if len(generated) > MAX_OCCURRENCES_PER_UID:
            raise ExpansionError(_too_many_message())
        ctx.generated_slots = generated
        return ctx

    def sources(self, when_utc: dt.datetime) -> list[GenerationStep]:
        """Every authored property generating slot ``when_utc``.

        Canonical order: RRULE properties (source order), then RDATE, then
        DTSTART alone. An RDATE duplicating an RRULE hit therefore lists
        both, while the DTSTART seed of an RRULE series is covered by the
        RRULE and is not listed separately.
        """
        if (
            self.has_master
            and self.recurring
            and not in_frame(when_utc, self.master_start, self.after, self.before)
        ):
            # Orphan override for a slot the padded window never reaches:
            # ask each RRULE directly for that one instant instead of the
            # frame-relative hit sets.
            steps = [
                step
                for step, rset in self.rule_frames
                if rule_contains(rset, localize(when_utc, self.master_start))
            ]
            if when_utc in self.rdate_values:
                steps.append(GenerationStep(kind=GEN_RDATE))
            if not steps and when_utc == self.dtstart_utc:
                steps.append(GenerationStep(kind=GEN_DTSTART))
            return steps
        return self._sources(when_utc)

    def _sources(self, when_utc: dt.datetime) -> list[GenerationStep]:
        steps: list[GenerationStep] = []
        for step, hits in self.frame_hits:
            if when_utc in hits:
                steps.append(step)
        if when_utc in self.rdate_values:
            steps.append(GenerationStep(kind=GEN_RDATE))
        if not steps and when_utc == self.dtstart_utc:
            steps.append(GenerationStep(kind=GEN_DTSTART))
        return steps


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


def frame_bounds(
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


def hits_in_frame(rset, after, before) -> set[dt.datetime]:
    """UTC occurrence instants one RRULE generates within the padded frame."""
    hits: set[dt.datetime] = set()
    try:
        for occ in rset.between(after, before, inc=True):
            hits.add(to_utc(occ))
            if len(hits) > MAX_OCCURRENCES_PER_UID:
                raise ExpansionError(_too_many_message())
    except ExpansionError:
        raise
    except (ValueError, TypeError) as exc:
        raise ExpansionError(f"cannot expand recurrence: {exc}") from exc
    return hits


def in_frame(when_utc, master_start, after, before) -> bool:
    """True when UTC instant ``when_utc`` lies within the padded local frame."""
    if master_start.tzinfo is None:
        local = when_utc.replace(tzinfo=None)
    else:
        local = when_utc.astimezone(master_start.tzinfo)
    return after <= local <= before


def localize(when_utc: dt.datetime, master_start: dt.datetime):
    """Project a UTC instant into the RRULE's local frame."""
    if master_start.tzinfo is None:  # floating: naive UTC
        return when_utc.replace(tzinfo=None)
    return when_utc.astimezone(master_start.tzinfo)


def rule_contains(rset, local) -> bool:
    """True when an rruleset generates exactly ``local`` (point membership)."""
    try:
        return bool(rset.between(local, local, inc=True))
    except (ValueError, TypeError) as exc:
        raise ExpansionError(f"cannot expand recurrence: {exc}") from exc


def _override_record(
    override, rid: dt.datetime, master_duration: dt.timedelta | None
) -> OverrideRecord:
    """Build the override record for one RECURRENCE-ID component."""
    status = status_of(override)
    if status == "CANCELLED":
        return OverrideRecord(
            action=OVR_CANCELLED,
            recurrence_id=rid,
            start=None,
            end=None,
            status=status,
        )
    start, end, _ = event_times(override, default_duration=master_duration)
    start = to_utc(start)
    end = to_utc(end)
    action = OVR_RESCHEDULED if start != rid else OVR_MODIFIED
    return OverrideRecord(
        action=action, recurrence_id=rid, start=start, end=end, status=status
    )


# ---------------------------------------------------------------------------
# Property helpers
# ---------------------------------------------------------------------------


def props_of(event, name: str) -> list:
    """All properties named ``name`` (icalendar keeps repeated properties)."""
    out = []
    for key, value in event.property_items():
        if key == name:
            out.extend(value if isinstance(value, list) else [value])
    return out


def validate_tzids(event) -> None:
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


def event_times(
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


def recurrence_id_of(event, master) -> dt.datetime:
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
            tz = event_times(master)[0].tzinfo
        return dt.datetime.combine(value, dt.time.min, tzinfo=tz)
    raise ExpansionError("unsupported RECURRENCE-ID value type")


def datetimes_of(prop, ref_start: dt.datetime) -> list[dt.datetime]:
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


def status_of(event) -> str:
    return str(event.get("STATUS", "CONFIRMED")).upper()


def transp_of(event) -> str:
    return str(event.get("TRANSP", "OPAQUE")).upper()


def text_of(event, name: str) -> str | None:
    value = event.get(name)
    return str(value) if value is not None else None


def to_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:  # floating time: interpreted as UTC
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def overlaps(
    start: dt.datetime, end: dt.datetime, window_start: dt.datetime, window_end: dt.datetime
) -> bool:
    if end > start:
        return start < window_end and end > window_start
    # Zero-duration event: in the window iff its instant is.
    return window_start <= start < window_end


def _too_many_message() -> str:
    return (
        f"recurrence expands to more than {MAX_OCCURRENCES_PER_UID} "
        "occurrences within the query window"
    )
