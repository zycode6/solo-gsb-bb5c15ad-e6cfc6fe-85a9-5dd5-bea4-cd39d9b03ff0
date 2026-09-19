"""Consistency regression tests: one ICS through /expand and /provenance.

Both endpoints project the same shared recurrence kernel
(:func:`app.recurrence.compute_candidates`): /expand returns the surviving
instances, /provenance returns the per-occurrence chains. These tests lock
that relationship: the instances /expand returns must be exactly the
provenance occurrences that survive (``final_start`` set) and overlap the
window, with matching ``recurrence_id``/``start``/``end``/``status``.

Run from the repository root:  python -m pytest tests/test_expand_provenance_consistency.py -v
"""

import datetime as dt

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

CAL_HEAD = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//consistency//EN\r\n"
CAL_TAIL = "END:VCALENDAR\r\n"

UTC = dt.timezone.utc
NO_RID = dt.datetime.min.replace(tzinfo=UTC)


def cal(*events: str) -> str:
    return CAL_HEAD + "".join(events) + CAL_TAIL


def event(*lines: str) -> str:
    return "BEGIN:VEVENT\r\n" + "".join(line + "\r\n" for line in lines) + "END:VEVENT\r\n"


def expand(ics, window_start, window_end):
    return client.post(
        "/expand",
        json={"ics": ics, "window_start": window_start, "window_end": window_end},
    )


def provenance(ics, uid, window_start, window_end):
    return client.post(
        "/provenance",
        json={
            "ics": ics,
            "uid": uid,
            "window_start": window_start,
            "window_end": window_end,
        },
    )


def parse(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def overlaps(start: dt.datetime, end: dt.datetime, ws: dt.datetime, we: dt.datetime) -> bool:
    """Same overlap rule the kernel applies to final instance times."""
    if end > start:
        return start < we and end > ws
    return ws <= start < we  # zero-duration event: in the window iff its instant is


def assert_consistent(
    ics: str,
    uids: list[str],
    window_start: str,
    window_end: str,
    non_recurring_uids: tuple[str, ...] = (),
):
    """/expand instances == surviving /provenance occurrences in the window.

    ``non_recurring_uids`` lists series without RRULE/RDATE: their
    un-overridden instances carry ``recurrence_id: null`` in /expand, while
    /provenance still identifies the occurrence by its scheduled slot.
    """
    ws, we = parse(window_start), parse(window_end)
    expand_resp = expand(ics, window_start, window_end)
    assert expand_resp.status_code == 200
    body = expand_resp.json()
    assert body["count"] == len(body["instances"])

    expected = []
    for uid in uids:
        prov_resp = provenance(ics, uid, window_start, window_end)
        assert prov_resp.status_code == 200
        prov_body = prov_resp.json()
        assert prov_body["count"] == len(prov_body["occurrences"])
        for occ in prov_body["occurrences"]:
            if occ["final_start"] is None:
                continue  # EXDATE-excluded or cancelled: does not survive
            start, end = parse(occ["final_start"]), parse(occ["final_end"])
            if not overlaps(start, end, ws, we):
                continue  # explained by its scheduled slot, but finally outside
            if uid in non_recurring_uids and occ["override"] is None:
                recurrence_id = None
            else:
                recurrence_id = occ["recurrence_id"]
            expected.append(
                {
                    "uid": uid,
                    "recurrence_id": recurrence_id,
                    "start": occ["final_start"],
                    "end": occ["final_end"],
                    "status": occ["final_status"],
                }
            )
    expected.sort(
        key=lambda e: (
            parse(e["start"]),
            e["uid"],
            parse(e["recurrence_id"]) if e["recurrence_id"] else NO_RID,
        )
    )

    actual = [
        {key: inst[key] for key in ("uid", "recurrence_id", "start", "end", "status")}
        for inst in body["instances"]
    ]
    assert actual == expected


def by_rid(response) -> dict[str, dict]:
    return {o["recurrence_id"]: o for o in response.json()["occurrences"]}


def starts(response) -> list[str]:
    return [i["start"] for i in response.json()["instances"]]


JAN = ("2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z")


# ---------------------------------------------------------------------------
# EXDATE followed by a resurrecting override
# ---------------------------------------------------------------------------


def test_exdate_then_override_resurrection_is_consistent():
    ics = cal(
        event(
            "UID:evt-1",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=DAILY;COUNT=5",  # Jan 5..9
            "EXDATE:20260107T090000Z",  # Jan 7 slot removed
            "SUMMARY:daily",
        ),
        event(
            "UID:evt-1",
            "RECURRENCE-ID:20260107T090000Z",  # ...then resurrected, moved
            "DTSTART:20260107T150000Z",
            "DTEND:20260107T160000Z",
            "SUMMARY:daily (resurrected)",
        ),
    )
    # The chain records both the exclusion and the resurrecting override.
    prov = provenance(ics, "evt-1", *JAN)
    assert prov.status_code == 200
    item = by_rid(prov)["2026-01-07T09:00:00Z"]
    assert item["excluded_by"] == ["2026-01-07T09:00:00Z"]
    assert item["override"]["action"] == "RESCHEDULED"
    assert item["final_start"] == "2026-01-07T15:00:00Z"
    assert item["final_status"] == "CONFIRMED"

    # /expand shows the resurrected instance once, at its final time only.
    exp = expand(ics, *JAN)
    assert exp.status_code == 200
    assert starts(exp) == [
        "2026-01-05T09:00:00Z",
        "2026-01-06T09:00:00Z",
        "2026-01-07T15:00:00Z",
        "2026-01-08T09:00:00Z",
        "2026-01-09T09:00:00Z",
    ]
    resurrected = exp.json()["instances"][2]
    assert resurrected["recurrence_id"] == "2026-01-07T09:00:00Z"
    assert resurrected["summary"] == "daily (resurrected)"

    assert_consistent(ics, ["evt-1"], *JAN)


def test_exdate_without_override_stays_absent_in_both():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "RRULE:FREQ=DAILY;COUNT=3",
        "EXDATE:20260106T090000Z",
    ))
    prov = provenance(ics, "evt-1", *JAN)
    item = by_rid(prov)["2026-01-06T09:00:00Z"]
    assert item["excluded_by"] == ["2026-01-06T09:00:00Z"]
    assert item["final_start"] is None
    assert item["final_status"] is None
    exp = expand(ics, *JAN)
    assert starts(exp) == ["2026-01-05T09:00:00Z", "2026-01-07T09:00:00Z"]
    assert_consistent(ics, ["evt-1"], *JAN)


# ---------------------------------------------------------------------------
# Cross-window reschedules (moved out and moved in)
# ---------------------------------------------------------------------------


def test_cross_window_reschedule_is_consistent():
    ics = cal(
        event(
            "UID:evt-1",
            "DTSTART:20260105T090000Z",  # Mondays 09:00 UTC
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=WEEKLY;COUNT=10",  # Jan 5 .. Mar 9
        ),
        event(  # January occurrence moved out of the window, to March
            "UID:evt-1",
            "RECURRENCE-ID:20260112T090000Z",
            "DTSTART:20260310T090000Z",
            "DTEND:20260310T100000Z",
        ),
        event(  # March occurrence moved into the January window
            "UID:evt-1",
            "RECURRENCE-ID:20260302T090000Z",
            "DTSTART:20260115T120000Z",
            "DTEND:20260115T130000Z",
        ),
    )
    exp = expand(ics, *JAN)
    assert exp.status_code == 200
    assert starts(exp) == [
        "2026-01-05T09:00:00Z",
        "2026-01-15T12:00:00Z",  # moved in; judged by its final time
        "2026-01-19T09:00:00Z",
        "2026-01-26T09:00:00Z",
    ]

    prov = provenance(ics, "evt-1", *JAN)
    assert prov.status_code == 200
    items = by_rid(prov)
    # Moved out: still explained via its scheduled slot inside the window.
    moved_out = items["2026-01-12T09:00:00Z"]
    assert moved_out["override"]["action"] == "RESCHEDULED"
    assert moved_out["final_start"] == "2026-03-10T09:00:00Z"
    # Moved in: scheduled slot outside the window, final slot inside.
    moved_in = items["2026-03-02T09:00:00Z"]
    assert [g["kind"] for g in moved_in["generated_by"]] == ["RRULE"]
    assert moved_in["final_start"] == "2026-01-15T12:00:00Z"

    assert_consistent(ics, ["evt-1"], *JAN)


def test_moved_out_instance_matches_when_window_follows_it():
    # The same series queried around the *final* slot of the moved
    # occurrence: both endpoints must see it there instead.
    ics = cal(
        event(
            "UID:evt-1",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=WEEKLY;COUNT=3",
        ),
        event(
            "UID:evt-1",
            "RECURRENCE-ID:20260112T090000Z",
            "DTSTART:20260310T090000Z",
            "DTEND:20260310T100000Z",
        ),
    )
    march = ("2026-03-01T00:00:00Z", "2026-04-01T00:00:00Z")
    exp = expand(ics, *march)
    assert starts(exp) == ["2026-03-10T09:00:00Z"]
    assert exp.json()["instances"][0]["recurrence_id"] == "2026-01-12T09:00:00Z"
    assert_consistent(ics, ["evt-1"], *march)


# ---------------------------------------------------------------------------
# DST transitions
# ---------------------------------------------------------------------------


def test_dst_series_with_exdate_and_override_is_consistent():
    # America/New_York: EST (UTC-5) until 2026-03-08, then EDT (UTC-4).
    ics = cal(
        event(
            "UID:evt-1",
            "DTSTART;TZID=America/New_York:20260301T090000",
            "DTEND;TZID=America/New_York:20260301T100000",
            "RRULE:FREQ=WEEKLY;COUNT=4",  # Mar 1, 8, 15, 22 local 09:00
            "EXDATE;TZID=America/New_York:20260308T090000",  # Mar 8 removed
            "SUMMARY:weekly",
        ),
        event(
            "UID:evt-1",
            "RECURRENCE-ID;TZID=America/New_York:20260315T090000",
            "DTSTART;TZID=America/New_York:20260316T110000",  # next day, later
            "DTEND;TZID=America/New_York:20260316T120000",
        ),
    )
    window = ("2026-02-25T00:00:00Z", "2026-04-01T00:00:00Z")
    exp = expand(ics, *window)
    assert exp.status_code == 200
    assert starts(exp) == [
        "2026-03-01T14:00:00Z",  # EST
        "2026-03-16T15:00:00Z",  # rescheduled, EDT
        "2026-03-22T13:00:00Z",  # EDT
    ]

    prov = provenance(ics, "evt-1", *window)
    assert prov.status_code == 200
    items = by_rid(prov)
    assert list(items) == [
        "2026-03-01T14:00:00Z",
        "2026-03-08T13:00:00Z",  # DST began 02:00 local: 09:00 EST -> 13:00Z
        "2026-03-15T13:00:00Z",
        "2026-03-22T13:00:00Z",
    ]
    excluded = items["2026-03-08T13:00:00Z"]
    assert excluded["excluded_by"] == ["2026-03-08T13:00:00Z"]
    assert excluded["final_start"] is None
    moved = items["2026-03-15T13:00:00Z"]
    assert moved["override"]["action"] == "RESCHEDULED"
    assert moved["final_start"] == "2026-03-16T15:00:00Z"

    assert_consistent(ics, ["evt-1"], *window)


def test_dst_fall_back_is_consistent():
    # America/New_York: EDT (UTC-4) until 2026-11-01, then EST (UTC-5).
    ics = cal(event(
        "UID:evt-1",
        "DTSTART;TZID=America/New_York:20261025T090000",
        "DTEND;TZID=America/New_York:20261025T100000",
        "RRULE:FREQ=WEEKLY;COUNT=3",
    ))
    window = ("2026-10-20T00:00:00Z", "2026-11-15T00:00:00Z")
    exp = expand(ics, *window)
    assert starts(exp) == [
        "2026-10-25T13:00:00Z",  # EDT
        "2026-11-01T14:00:00Z",  # EST (fallback already happened 02:00 local)
        "2026-11-08T14:00:00Z",  # EST
    ]
    assert_consistent(ics, ["evt-1"], *window)


# ---------------------------------------------------------------------------
# Mixed calendars: several UIDs, overrides, cancellations, floating events
# ---------------------------------------------------------------------------


def test_kitchen_sink_calendar_is_consistent_across_uids():
    ics = cal(
        event(
            "UID:series-a",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=DAILY;COUNT=5",
            "EXDATE:20260107T090000Z",
            "SUMMARY:A",
        ),
        event(  # resurrect the EXDATEd slot of series-a, in place
            "UID:series-a",
            "RECURRENCE-ID:20260107T090000Z",
            "DTSTART:20260107T090000Z",
            "DTEND:20260107T100000Z",
            "SUMMARY:A (resurrected)",
        ),
        event(  # cancel one occurrence of series-a
            "UID:series-a",
            "RECURRENCE-ID:20260108T090000Z",
            "DTSTART:20260108T090000Z",
            "DTEND:20260108T100000Z",
            "STATUS:CANCELLED",
        ),
        event(  # non-recurring event between the series-a occurrences
            "UID:single-b",
            "DTSTART:20260106T090000Z",
            "DTEND:20260106T093000Z",
            "SUMMARY:B",
        ),
        event(  # orphan override on the non-recurring UID, moved in place
            "UID:single-b",
            "RECURRENCE-ID:20260120T090000Z",
            "DTSTART:20260120T120000Z",
            "DTEND:20260120T130000Z",
        ),
    )
    exp = expand(ics, *JAN)
    assert exp.status_code == 200
    assert [(i["uid"], i["start"]) for i in exp.json()["instances"]] == [
        ("series-a", "2026-01-05T09:00:00Z"),
        ("series-a", "2026-01-06T09:00:00Z"),
        ("single-b", "2026-01-06T09:00:00Z"),
        ("series-a", "2026-01-07T09:00:00Z"),
        ("series-a", "2026-01-09T09:00:00Z"),
        ("single-b", "2026-01-20T12:00:00Z"),
    ]
    # The non-recurring instance keeps recurrence_id null; every recurring
    # or override instance is identified by its scheduled slot.
    rids = {i["uid"]: i["recurrence_id"] for i in exp.json()["instances"]}
    assert rids["single-b"] is None or rids["single-b"] == "2026-01-20T09:00:00Z"
    assert_consistent(
        ics, ["series-a", "single-b"], *JAN, non_recurring_uids=("single-b",)
    )


def test_cancelled_master_is_consistently_empty_in_expand():
    ics = cal(
        event(
            "UID:evt-1",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=DAILY;COUNT=3",
            "STATUS:CANCELLED",
        ),
        event(  # one override resurrects a single slot of the dead series
            "UID:evt-1",
            "RECURRENCE-ID:20260106T090000Z",
            "DTSTART:20260106T150000Z",
            "DTEND:20260106T160000Z",
        ),
    )
    exp = expand(ics, *JAN)
    assert starts(exp) == ["2026-01-06T15:00:00Z"]
    prov = provenance(ics, "evt-1", *JAN)
    items = by_rid(prov)
    assert items["2026-01-05T09:00:00Z"]["final_status"] == "CANCELLED"
    assert items["2026-01-05T09:00:00Z"]["final_start"] is None
    assert items["2026-01-06T09:00:00Z"]["final_status"] == "CONFIRMED"
    assert_consistent(ics, ["evt-1"], *JAN)


# ---------------------------------------------------------------------------
# Shared 422 semantics and the per-UID 10000 occurrence cap
# ---------------------------------------------------------------------------


def assert_both_422(ics, uid, window_start, window_end, fragment):
    exp = expand(ics, window_start, window_end)
    prov = provenance(ics, uid, window_start, window_end)
    assert exp.status_code == 422
    assert prov.status_code == 422
    assert fragment in exp.json()["detail"]
    assert exp.json()["detail"] == prov.json()["detail"]


def test_occurrence_cap_is_enforced_identically():
    # Minutely over an 8-day window: 11520 candidates > 10000.
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260101T000000Z",
        "DTEND:20260101T003000Z",
        "RRULE:FREQ=MINUTELY",
    ))
    assert_both_422(
        ics, "evt-1", "2026-01-01T00:00:00Z", "2026-01-09T00:00:00Z",
        "more than 10000 occurrences",
    )


def test_invalid_rrule_is_rejected_identically():
    ics = cal(event(
        "UID:evt-1", "DTSTART:20260105T090000Z", "RRULE:FREQ=BOGUS;COUNT=2"
    ))
    assert_both_422(ics, "evt-1", *JAN, "invalid RRULE")


def test_unknown_tzid_is_rejected_identically():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART;TZID=Mars/Olympus_Mons:20260105T090000",
        "DTEND;TZID=Mars/Olympus_Mons:20260105T100000",
    ))
    assert_both_422(ics, "evt-1", *JAN, "unknown timezone")


def test_thisandfuture_override_is_rejected_identically():
    ics = cal(
        event(
            "UID:evt-1",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=DAILY;COUNT=2",
        ),
        event(
            "UID:evt-1",
            "RECURRENCE-ID;RANGE=THISANDFUTURE:20260106T090000Z",
            "DTSTART:20260106T100000Z",
        ),
    )
    assert_both_422(ics, "evt-1", *JAN, "THISANDFUTURE")
