"""Consistency regressions: the same ICS seen through /expand and /provenance.

The two endpoints are projections of one shared recurrence kernel
(:mod:`app.recurrence`). These tests pin the cross-endpoint invariants that
the refactor was made to guarantee:

* every surviving occurrence /provenance reports (final slot overlapping
  the window) appears in /expand with identical identity and effective
  times/status, in the same effective-time order;
* every EXDATE-excluded or CANCELLED occurrence is absent from /expand yet
  still explained by /provenance;
* the rule holds when an override resurrects an EXDATEd slot, when an
  instance is rescheduled across the window boundary, and across DST
  transitions;
* both endpoints answer the same 422 for invalid input and enforce the
  per-UID 10000 candidate cap identically.

Run from the repository root:  python -m pytest tests/ -v
"""

import datetime as dt

from fastapi.testclient import TestClient

from app.main import app
from app.recurrence import MAX_OCCURRENCES_PER_UID

client = TestClient(app)

CAL_HEAD = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//consistency//EN\r\n"
CAL_TAIL = "END:VCALENDAR\r\n"


def cal(*events: str) -> str:
    return CAL_HEAD + "".join(events) + CAL_TAIL


def event(*lines: str) -> str:
    return "BEGIN:VEVENT\r\n" + "".join(line + "\r\n" for line in lines) + "END:VEVENT\r\n"


def expand(ics: str, window_start, window_end):
    return client.post(
        "/expand",
        json={"ics": ics, "window_start": window_start, "window_end": window_end},
    )


def provenance(ics: str, uid: str, window_start, window_end):
    return client.post(
        "/provenance",
        json={
            "ics": ics,
            "uid": uid,
            "window_start": window_start,
            "window_end": window_end,
        },
    )


def _overlaps(start: str | None, end: str | None, window_start, window_end) -> bool:
    if start is None:
        return False
    s = dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
    e = dt.datetime.fromisoformat(end.replace("Z", "+00:00"))
    ws = dt.datetime.fromisoformat(window_start.replace("Z", "+00:00"))
    we = dt.datetime.fromisoformat(window_end.replace("Z", "+00:00"))
    if e > s:
        return s < we and e > ws
    return ws <= s < we


def assert_endpoints_agree(
    ics: str,
    uids,
    window_start="2026-01-01T00:00:00Z",
    window_end="2026-02-01T00:00:00Z",
):
    """Cross-projection invariants for every UID in ``uids``.

    /expand is a strict subset-projection of /provenance: its instances for
    a UID are exactly the provenance occurrences whose *final* slot
    overlaps the window, with identical identity/times/status/order.
    """
    exp = expand(ics, window_start, window_end)
    assert exp.status_code == 200, exp.text
    by_uid: dict[str, list[dict]] = {}
    for inst in exp.json()["instances"]:
        by_uid.setdefault(inst["uid"], []).append(inst)

    for uid in uids:
        prov = provenance(ics, uid, window_start, window_end)
        assert prov.status_code == 200, prov.text
        occurrences = prov.json()["occurrences"]

        # 1. Survivors with a final slot in the window equal /expand rows.
        expected = []
        for occ in occurrences:
            if occ["final_start"] is None:
                continue
            if not _overlaps(
                occ["final_start"], occ["final_end"], window_start, window_end
            ):
                continue
            generated = {g["kind"] for g in occ["generated_by"]}
            in_place = (
                occ["override"] is not None
                and occ["override"]["action"] == "MODIFIED"
            )
            # A plain non-recurring master (generated only by DTSTART with
            # no in-place override) has a null RECURRENCE-ID in /expand;
            # every other identity keeps the scheduled slot.
            rid = occ["recurrence_id"]
            if generated == {"DTSTART"} and not in_place:
                rid = None
            expected.append(
                {
                    "recurrence_id": rid,
                    "start": occ["final_start"],
                    "end": occ["final_end"],
                    "status": occ["final_status"],
                }
            )
        got = [
            {
                "recurrence_id": inst["recurrence_id"],
                "start": inst["start"],
                "end": inst["end"],
                "status": inst["status"],
            }
            for inst in by_uid.get(uid, [])
        ]
        # Provenance orders by (effective start, rid); /expand by
        # (start, uid, rid) -- identical within one UID.
        expected.sort(key=lambda o: (o["start"], o["recurrence_id"]))
        assert got == expected, (uid, got, expected)

        # 2. Nothing excluded or cancelled leaked into /expand.
        for occ in occurrences:
            if occ["final_start"] is None:
                assert not any(
                    i["recurrence_id"] == occ["recurrence_id"]
                    for i in by_uid.get(uid, [])
                ), (uid, occ)

        # 3. Every /expand row is explained as surviving in provenance.
        survivor_rids = {
            occ["recurrence_id"]
            for occ in occurrences
            if occ["final_start"] is not None
        }
        survivor_starts = {
            occ["final_start"]
            for occ in occurrences
            if occ["final_start"] is not None
        }
        for inst in by_uid.get(uid, []):
            if inst["recurrence_id"] is not None:
                assert inst["recurrence_id"] in survivor_rids, (uid, inst)
            else:
                # A non-recurring event has no rid in /expand; its start is
                # its provenance identity.
                assert inst["start"] in survivor_starts, (uid, inst)

    # Global /expand ordering by (start, uid, rid) is still honored.
    rows = exp.json()["instances"]
    keys = [(r["start"], r["uid"], r["recurrence_id"] or "") for r in rows]
    assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# EXDATE exclusion followed by an overriding RECURRENCE-ID
# ---------------------------------------------------------------------------


def test_exdate_then_override_restores_instance_in_both_views():
    # The Jan 6 occurrence is removed by EXDATE, then resurrected (and
    # rescheduled to 15:00) by a RECURRENCE-ID override at the same slot.
    ics = cal(
        event(
            "UID:evt-1",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=DAILY;COUNT=3",
            "EXDATE:20260106T090000Z",
        ),
        event(
            "UID:evt-1",
            "RECURRENCE-ID:20260106T090000Z",
            "DTSTART:20260106T150000Z",
            "DTEND:20260106T160000Z",
            "SUMMARY:made up",
        ),
    )
    resp = expand(ics, "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z")
    assert [i["start"] for i in resp.json()["instances"]] == [
        "2026-01-05T09:00:00Z",
        "2026-01-06T15:00:00Z",  # resurrected at the override time
        "2026-01-07T09:00:00Z",
    ]

    prov = provenance(ics, "evt-1", "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z")
    items = {o["recurrence_id"]: o for o in prov.json()["occurrences"]}
    restored = items["2026-01-06T09:00:00Z"]
    # The chain remembers the exclusion even though the override wins.
    assert restored["excluded_by"] == ["2026-01-06T09:00:00Z"]
    assert restored["override"]["action"] == "RESCHEDULED"
    assert restored["final_start"] == "2026-01-06T15:00:00Z"
    assert restored["final_status"] == "CONFIRMED"

    assert_endpoints_agree(ics, ["evt-1"])


def test_exdate_without_override_is_absent_from_expand_explained_in_provenance():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "RRULE:FREQ=DAILY;COUNT=3",
        "EXDATE:20260106T090000Z",
    ))
    resp = expand(ics, "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z")
    assert [i["start"] for i in resp.json()["instances"]] == [
        "2026-01-05T09:00:00Z",
        "2026-01-07T09:00:00Z",
    ]
    prov = provenance(ics, "evt-1", "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z")
    items = {o["recurrence_id"]: o for o in prov.json()["occurrences"]}
    excluded = items["2026-01-06T09:00:00Z"]
    assert excluded["final_start"] is None
    assert excluded["final_status"] is None
    assert excluded["excluded_by"] == ["2026-01-06T09:00:00Z"]
    assert_endpoints_agree(ics, ["evt-1"])


def test_cancelled_override_after_exdate_stays_dead_in_both_views():
    # EXDATE removes Jan 6; a later CANCELLED override names the same slot:
    # the override wins, so the chain reports CANCELLED (status only), and
    # /expand still must not show it.
    ics = cal(
        event(
            "UID:evt-1",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=DAILY;COUNT=3",
            "EXDATE:20260106T090000Z",
        ),
        event(
            "UID:evt-1",
            "RECURRENCE-ID:20260106T090000Z",
            "DTSTART:20260106T090000Z",
            "DTEND:20260106T100000Z",
            "STATUS:CANCELLED",
        ),
    )
    resp = expand(ics, "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z")
    assert [i["start"] for i in resp.json()["instances"]] == [
        "2026-01-05T09:00:00Z",
        "2026-01-07T09:00:00Z",
    ]
    prov = provenance(ics, "evt-1", "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z")
    dead = {o["recurrence_id"]: o for o in prov.json()["occurrences"]}[
        "2026-01-06T09:00:00Z"
    ]
    assert dead["excluded_by"] == ["2026-01-06T09:00:00Z"]
    assert dead["override"]["action"] == "CANCELLED"
    assert dead["final_start"] is None
    assert dead["final_status"] == "CANCELLED"
    assert_endpoints_agree(ics, ["evt-1"])


# ---------------------------------------------------------------------------
# Reschedules across the query window boundary
# ---------------------------------------------------------------------------


def test_override_moves_instance_into_window_agrees():
    # March occurrence moved into January: /expand sees it at the new slot,
    # provenance explains it via the March RID with an RRULE source.
    ics = cal(
        event(
            "UID:evt-1",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=WEEKLY;COUNT=10",
        ),
        event(
            "UID:evt-1",
            "RECURRENCE-ID:20260302T090000Z",
            "DTSTART:20260115T120000Z",
            "DTEND:20260115T130000Z",
        ),
    )
    ws, we = "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"
    resp = expand(ics, ws, we)
    assert [i["start"] for i in resp.json()["instances"]] == [
        "2026-01-05T09:00:00Z",
        "2026-01-12T09:00:00Z",
        "2026-01-15T12:00:00Z",
        "2026-01-19T09:00:00Z",
        "2026-01-26T09:00:00Z",
    ]
    moved_in = [
        i for i in resp.json()["instances"] if i["recurrence_id"] == "2026-03-02T09:00:00Z"
    ]
    assert len(moved_in) == 1
    assert_endpoints_agree(ics, ["evt-1"], ws, we)


def test_override_moves_instance_out_of_window_agrees():
    # January occurrence moved to March: /expand drops it, but provenance
    # keeps the chain because the *scheduled* slot overlaps the window.
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
    ws, we = "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"
    assert [i["start"] for i in expand(ics, ws, we).json()["instances"]] == [
        "2026-01-05T09:00:00Z",
        "2026-01-19T09:00:00Z",
    ]
    prov = provenance(ics, "evt-1", ws, we)
    moved_out = {o["recurrence_id"]: o for o in prov.json()["occurrences"]}[
        "2026-01-12T09:00:00Z"
    ]
    assert moved_out["scheduled_start"] == "2026-01-12T09:00:00Z"
    assert moved_out["final_start"] == "2026-03-10T09:00:00Z"
    assert_endpoints_agree(ics, ["evt-1"], ws, we)


def test_reschedule_crossing_window_with_tzid_recurrence_id_agrees():
    # The override names the slot in Europe/Berlin local time; both views
    # must resolve it to the same UTC identity.
    ics = cal(
        event(
            "UID:evt-1",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=DAILY;COUNT=2",
        ),
        event(
            "UID:evt-1",
            "RECURRENCE-ID;TZID=Europe/Berlin:20260106T100000",  # = 09:00Z
            "DTSTART:20260107T150000Z",
            "DTEND:20260107T160000Z",
        ),
    )
    assert [i["start"] for i in expand(ics, "2026-01-01T00:00:00Z",
                                      "2026-02-01T00:00:00Z").json()["instances"]] == [
        "2026-01-05T09:00:00Z",
        "2026-01-07T15:00:00Z",
    ]
    assert_endpoints_agree(ics, ["evt-1"])


# ---------------------------------------------------------------------------
# DST
# ---------------------------------------------------------------------------


def test_dst_spring_forward_series_agrees_across_endpoints():
    # America/New_York: EST (UTC-5) until 2026-03-08, then EDT (UTC-4).
    ics = cal(event(
        "UID:evt-1",
        "DTSTART;TZID=America/New_York:20260301T090000",
        "DTEND;TZID=America/New_York:20260301T100000",
        "RRULE:FREQ=WEEKLY;COUNT=3",
    ))
    ws, we = "2026-02-25T00:00:00Z", "2026-04-01T00:00:00Z"
    assert [i["start"] for i in expand(ics, ws, we).json()["instances"]] == [
        "2026-03-01T14:00:00Z",
        "2026-03-08T13:00:00Z",
        "2026-03-15T13:00:00Z",
    ]
    prov = provenance(ics, "evt-1", ws, we)
    assert [o["recurrence_id"] for o in prov.json()["occurrences"]] == [
        "2026-03-01T14:00:00Z",
        "2026-03-08T13:00:00Z",
        "2026-03-15T13:00:00Z",
    ]
    assert_endpoints_agree(ics, ["evt-1"], ws, we)


def test_dst_exdate_and_resurrecting_override_agrees():
    # EXDATE in local Berlin time on the DST transition day; an override at
    # the same local slot moves the instance. Both endpoints must resolve
    # the local instants through the same DST-aware mapping.
    ics = cal(
        event(
            "UID:evt-1",
            "DTSTART;TZID=Europe/Berlin:20260322T100000",  # CET/CEST Sundays
            "DTEND;TZID=Europe/Berlin:20260322T110000",
            "RRULE:FREQ=WEEKLY;COUNT=3",  # Mar 22 (CET), Mar 29 (CEST), Apr 5
            # Mar 29 10:00 CEST == 08:00Z (post spring-forward on that date)
            "EXDATE;TZID=Europe/Berlin:20260329T100000",
        ),
        event(
            "UID:evt-1",
            "RECURRENCE-ID;TZID=Europe/Berlin:20260329T100000",
            "DTSTART;TZID=Europe/Berlin:20260329T160000",  # 14:00Z
            "DTEND;TZID=Europe/Berlin:20260329T170000",
        ),
    )
    ws, we = "2026-03-01T00:00:00Z", "2026-04-01T00:00:00Z"
    assert [i["start"] for i in expand(ics, ws, we).json()["instances"]] == [
        "2026-03-22T09:00:00Z",  # CET UTC+1
        "2026-03-29T14:00:00Z",  # resurrected/relocated on the CEST day
    ]
    prov = provenance(ics, "evt-1", ws, we)
    items = {o["recurrence_id"]: o for o in prov.json()["occurrences"]}
    restored = items["2026-03-29T08:00:00Z"]
    assert restored["excluded_by"] == ["2026-03-29T08:00:00Z"]
    assert restored["override"]["recurrence_id"] == "2026-03-29T08:00:00Z"
    assert restored["final_start"] == "2026-03-29T14:00:00Z"
    assert_endpoints_agree(ics, ["evt-1"], ws, we)


def test_dst_fall_back_series_agrees_across_endpoints():
    # America/New_York falls back on 2026-11-01; the 09:00 local meeting
    # shifts 14:00Z -> 14:00Z... use 08:00 local: 12:00Z (EDT) -> 13:00Z (EST).
    ics = cal(event(
        "UID:evt-1",
        "DTSTART;TZID=America/New_York:20261025T080000",
        "DTEND;TZID=America/New_York:20261025T090000",
        "RRULE:FREQ=WEEKLY;COUNT=3",  # Oct 25, Nov 1 (fallback), Nov 8
    ))
    ws, we = "2026-10-01T00:00:00Z", "2026-12-01T00:00:00Z"
    assert [i["start"] for i in expand(ics, ws, we).json()["instances"]] == [
        "2026-10-25T12:00:00Z",
        "2026-11-01T13:00:00Z",
        "2026-11-08T13:00:00Z",
    ]
    assert_endpoints_agree(ics, ["evt-1"], ws, we)


# ---------------------------------------------------------------------------
# Mixed calendars, ordering and non-recurring events
# ---------------------------------------------------------------------------


def test_multi_uid_calendar_with_exdates_overrides_and_rdate_agrees():
    ics = cal(
        event(
            "UID:series-a",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=DAILY;COUNT=4",
            "EXDATE:20260106T090000Z",
            "RDATE:20260120T090000Z",
            "SUMMARY:A",
        ),
        event(
            "UID:series-a",
            "RECURRENCE-ID:20260107T090000Z",
            "DTSTART:20260107T120000Z",
            "DTEND:20260107T130000Z",
        ),
        event(
            "UID:series-b",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=WEEKLY;COUNT=3",
        ),
        event(
            "UID:series-b",
            "RECURRENCE-ID:20260112T090000Z",
            "DTSTART:20260112T090000Z",
            "DTEND:20260112T100000Z",
            "STATUS:CANCELLED",
        ),
        event("UID:one-off", "DTSTART:20260115T080000Z",
              "DTEND:20260115T090000Z", "SUMMARY:solo"),
    )
    assert_endpoints_agree(ics, ["series-a", "series-b", "one-off"])

    # Spot-check the concrete merged picture.
    rows = [
        (i["uid"], i["recurrence_id"], i["start"])
        for i in expand(
            ics, "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"
        ).json()["instances"]
    ]
    assert rows == [
        ("series-a", "2026-01-05T09:00:00Z", "2026-01-05T09:00:00Z"),
        ("series-b", "2026-01-05T09:00:00Z", "2026-01-05T09:00:00Z"),
        ("series-a", "2026-01-07T09:00:00Z", "2026-01-07T12:00:00Z"),
        ("series-a", "2026-01-08T09:00:00Z", "2026-01-08T09:00:00Z"),
        ("one-off", None, "2026-01-15T08:00:00Z"),
        ("series-b", "2026-01-19T09:00:00Z", "2026-01-19T09:00:00Z"),
        ("series-a", "2026-01-20T09:00:00Z", "2026-01-20T09:00:00Z"),
    ]


def test_orphan_override_consistency():
    # The RID is never generated (COUNT=2 stops Jan 12), yet the override
    # moves it into the window: /expand and provenance must both include it.
    ics = cal(
        event(
            "UID:evt-1",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=WEEKLY;COUNT=2",
        ),
        event(
            "UID:evt-1",
            "RECURRENCE-ID:20260302T090000Z",
            "DTSTART:20260115T120000Z",
            "DTEND:20260115T130000Z",
        ),
    )
    assert_endpoints_agree(ics, ["evt-1"])


# ---------------------------------------------------------------------------
# Shared error semantics and the per-UID cap
# ---------------------------------------------------------------------------


def test_invalid_rrule_returns_identical_422_from_both_endpoints():
    ics = cal(event(
        "UID:evt-1", "DTSTART:20260105T090000Z", "RRULE:FREQ=BOGUS;COUNT=2"
    ))
    ws, we = "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"
    r1, r2 = expand(ics, ws, we), provenance(ics, "evt-1", ws, we)
    assert r1.status_code == r2.status_code == 422
    assert r1.json()["detail"] == r2.json()["detail"]
    assert "invalid RRULE" in r1.json()["detail"]


def test_unknown_tzid_returns_identical_422_from_both_endpoints():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART;TZID=Mars/Olympus_Mons:20260105T090000",
        "DTEND;TZID=Mars/Olympus_Mons:20260105T100000",
    ))
    ws, we = "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"
    r1, r2 = expand(ics, ws, we), provenance(ics, "evt-1", ws, we)
    assert r1.status_code == r2.status_code == 422
    assert r1.json()["detail"] == r2.json()["detail"]


def test_unparseable_ics_returns_identical_422_from_both_endpoints():
    ws, we = "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"
    r1 = expand("this is not an iCalendar document", ws, we)
    r2 = provenance("this is not an iCalendar document", "evt-1", ws, we)
    assert r1.status_code == r2.status_code == 422
    assert r1.json()["detail"] == r2.json()["detail"]


def _hourly_ics(count: int) -> str:
    return cal(event(
        "UID:big",
        "DTSTART:20250101T000000Z",
        "DTEND:20250101T010000Z",
        f"RRULE:FREQ=HOURLY;COUNT={count}",
    ))


def test_occurrence_cap_is_identical_at_the_boundary():
    ws, we = "2025-01-01T00:00:00Z", "2028-01-01T00:00:00Z"

    ok = _hourly_ics(MAX_OCCURRENCES_PER_UID)
    r1, r2 = expand(ok, ws, we), provenance(ok, "big", ws, we)
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["count"] == r2.json()["count"] == MAX_OCCURRENCES_PER_UID

    too_many = _hourly_ics(MAX_OCCURRENCES_PER_UID + 1)
    r1, r2 = expand(too_many, ws, we), provenance(too_many, "big", ws, we)
    assert r1.status_code == r2.status_code == 422
    assert r1.json()["detail"] == r2.json()["detail"]
    assert str(MAX_OCCURRENCES_PER_UID) in r1.json()["detail"]
