"""Deterministic scheduling boundaries, unions, and actual-instant DST semantics."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from piocloop.schedule import Schedule

UTC = timezone.utc


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


@pytest.mark.parametrize(
    "value,allowed",
    [
        ("2026-07-13T08:59:59", False),
        ("2026-07-13T09:00:00", True),
        ("2026-07-13T11:59:59", True),
        ("2026-07-13T12:00:00", False),
        ("2026-07-13T13:00:00", True),
        ("2026-07-13T17:00:00", False),
        ("2026-07-18T09:00:00", False),
    ],
)
def test_boundaries_and_multiple_ranges(value, allowed):
    schedule = Schedule(run_windows=["Mo-Fr 09:00-12:00,13:00-17:00"])
    assert schedule.evaluate(instant(value)).allowed is allowed


def test_weekday_saturday_sunday_example():
    schedule = Schedule(
        run_windows=["Mo-Fr 18:00-23:00; Sa 09:00-18:00; Su 12:00-16:00"]
    )
    for value in ("2026-07-13T18:00:00", "2026-07-18T09:00:00", "2026-07-19T12:00:00"):
        assert schedule.evaluate(instant(value)).allowed
    for value in ("2026-07-13T17:00:00", "2026-07-18T18:00:00", "2026-07-19T11:00:00"):
        assert not schedule.evaluate(instant(value)).allowed


def test_semicolon_rules_preserve_osm_override_semantics():
    schedule = Schedule(run_windows=["Mo-Fr 09:00-17:00; We 10:00-12:00"])
    assert not schedule.evaluate(instant("2026-07-15T09:00:00")).allowed
    assert schedule.evaluate(instant("2026-07-15T10:00:00")).allowed
    assert not schedule.evaluate(instant("2026-07-15T12:00:00")).allowed


def test_weekday_lists():
    schedule = Schedule(run_windows=["Mo,We,Fr 09:00-17:00"])
    assert schedule.evaluate(instant("2026-07-15T12:00:00")).allowed
    assert not schedule.evaluate(instant("2026-07-14T12:00:00")).allowed


@pytest.mark.parametrize("pause", [False, True])
def test_overnight_equals_split_without_midnight_transition(pause):
    field = "pause_windows" if pause else "run_windows"
    direct = Schedule(**{field: ["Fr 22:00-06:00"]})
    split = Schedule(**{field: ["Fr 22:00-24:00", "Sa 00:00-06:00"]})
    base = instant("2026-07-17T21:59:00")
    for offset in (0, 1, 120, 121, 122, 480, 481):
        now = base + timedelta(minutes=offset)
        assert direct.evaluate(now) == split.evaluate(now)
    before = split.evaluate(instant("2026-07-17T23:59:59"))
    after = split.evaluate(instant("2026-07-18T00:00:00"))
    assert before.allowed is after.allowed is (not pause)
    assert before.next_change == after.next_change == instant("2026-07-18T06:00:00")


@pytest.mark.parametrize("pause", [False, True])
def test_adjacent_and_overlapping_windows_are_one_permission_interval(pause):
    field = "pause_windows" if pause else "run_windows"
    schedule = Schedule(**{field: ["09:00-12:00", "11:00-13:00", "13:00-17:00"]})
    for hour in (9, 11, 12, 13, 16):
        decision = schedule.evaluate(instant(f"2026-07-13T{hour:02}:00:00"))
        assert decision.allowed is (not pause)
        assert decision.next_change == instant("2026-07-13T17:00:00")


def test_pause_precedence_and_effective_next_permission():
    schedule = Schedule(
        run_windows=["22:00-06:00"],
        pause_windows=["02:00-04:00", "03:00-07:00"],
    )
    decision = schedule.evaluate(instant("2026-07-18T02:00:00"))
    assert not decision.allowed
    assert decision.reasons == ("scheduled pause",)
    # Leaving the union of pauses at 07:00 is outside the run window.
    assert decision.next_allowed == instant("2026-07-18T22:00:00")
    assert schedule.evaluate(instant("2026-07-18T06:00:00")).reasons == (
        "outside run windows",
        "scheduled pause",
    )


def test_independent_overnight_and_saturday_daytime():
    schedule = Schedule(run_windows=["Fr 22:00-06:00", "Sa 09:00-18:00"])
    for hour, allowed in (
        (0, True),
        (5, True),
        (6, False),
        (8, False),
        (9, True),
        (18, False),
    ):
        assert (
            schedule.evaluate(instant(f"2026-07-18T{hour:02}:00:00")).allowed is allowed
        )


@pytest.mark.parametrize(
    "base,expression,end",
    [
        ("2026-07-19T23:59:59", "Su 22:00-06:00", "2026-07-20T06:00:00"),
        ("2026-07-31T23:59:59", "Fr 22:00-06:00", "2026-08-01T06:00:00"),
        ("2026-12-31T23:59:59", "Th 22:00-06:00", "2027-01-01T06:00:00"),
    ],
)
def test_week_month_year_spillovers(base, expression, end):
    schedule = Schedule(run_windows=[expression])
    decision = schedule.evaluate(instant(base))
    assert decision.allowed
    assert decision.next_change == instant(end)


@pytest.mark.parametrize(
    "schedule,allowed",
    [
        (Schedule(), True),
        (Schedule(run_windows=["24/7"]), True),
        (Schedule(pause_windows=["24/7"]), False),
        (Schedule(run_windows=["09:00-17:00"], pause_windows=["09:00-17:00"]), False),
    ],
)
def test_permanent_states(schedule, allowed):
    decision = schedule.evaluate(instant("2026-07-13T12:00:00"))
    assert decision.allowed is allowed
    assert decision.next_change is None
    assert decision.next_allowed is None


def test_selected_timezone_converts_input_instants():
    schedule = Schedule(run_windows=["09:00-17:00"], timezone_name="Europe/Paris")
    assert schedule.evaluate(instant("2026-07-13T07:00:00")).allowed
    assert not schedule.evaluate(instant("2026-07-13T06:59:59")).allowed
    assert schedule.evaluate(instant("2026-07-13T06:59:59")).next_allowed == datetime(
        2026,
        7,
        13,
        9,
        tzinfo=ZoneInfo("Europe/Paris"),
    )


def test_dst_fall_back_evaluates_both_occurrences_and_predicts_first():
    schedule = Schedule(run_windows=["Su 02:15-02:45"], timezone_name="Europe/Paris")
    for value, allowed, next_value in (
        ("2026-10-25T00:00:00", False, "2026-10-25T00:15:00"),
        ("2026-10-25T00:15:00", True, "2026-10-25T00:45:00"),
        ("2026-10-25T00:45:00", False, "2026-10-25T01:15:00"),
        ("2026-10-25T01:15:00", True, "2026-10-25T01:45:00"),
    ):
        decision = schedule.evaluate(instant(value))
        assert decision.allowed is allowed
        assert decision.next_change.astimezone(UTC) == instant(next_value)
    for fold in (0, 1):
        assert schedule.evaluate(
            datetime(2026, 10, 25, 2, 30, tzinfo=schedule.timezone, fold=fold)
        ).allowed


def test_dst_rollback_itself_can_change_permission():
    schedule = Schedule(run_windows=["Su 02:00-02:30"], timezone_name="Europe/Paris")
    decision = schedule.evaluate(instant("2026-10-25T00:50:00"))
    assert not decision.allowed
    # 02:50 CEST jumps backwards to 02:00 CET, which is permitted again.
    assert decision.next_allowed.astimezone(UTC) == instant("2026-10-25T01:00:00")


def test_dst_spring_forward_has_no_imaginary_permission():
    schedule = Schedule(run_windows=["Su 02:15-02:45"], timezone_name="Europe/Paris")
    decision = schedule.evaluate(instant("2026-03-29T00:00:00"))
    assert not decision.allowed
    assert decision.next_allowed.astimezone(UTC) == instant("2026-04-05T00:15:00")
    assert not schedule.evaluate(instant("2026-03-29T01:00:00")).allowed
    # Looking ahead from the previous Monday must skip the entirely missing
    # weekly opening too, rather than claiming there is no upcoming window.
    assert schedule.evaluate(instant("2026-03-23T00:00:00")).next_allowed.astimezone(
        UTC
    ) == instant("2026-04-05T00:15:00")


def test_dst_spring_jump_into_window():
    schedule = Schedule(run_windows=["Su 02:15-04:00"], timezone_name="Europe/Paris")
    decision = schedule.evaluate(instant("2026-03-29T00:00:00"))
    assert decision.next_allowed.astimezone(UTC) == instant("2026-03-29T01:00:00")
    assert schedule.evaluate(instant("2026-03-29T01:00:00")).allowed


def test_clock_moving_backward_or_forward_recomputes_decision():
    schedule = Schedule(run_windows=["09:00-17:00"])
    assert schedule.evaluate(instant("2026-07-13T18:00:00")).next_allowed == instant(
        "2026-07-14T09:00:00"
    )
    assert schedule.evaluate(instant("2026-07-13T08:00:00")).next_allowed == instant(
        "2026-07-13T09:00:00"
    )
    assert schedule.evaluate(instant("2026-07-14T09:00:00")).allowed


@pytest.mark.parametrize(
    "expression",
    [
        "",
        "nonsense",
        "24/24",
        "PH off",
        "sunrise-sunset",
        "09:00+",
        "24/7 unknown",
        "Mo-Fr",
        "25:00-26:00",
        "24:00-06:00",
        "09:60-17:00",
        "09:00-24:01",
        "09:00-09:00",
        "09:00-17:00 || unknown",
        "Jan 09:00-17:00",
        "x" * 4097,
    ],
)
def test_rejects_invalid_or_unsupported_expression(expression):
    with pytest.raises(ValueError):
        Schedule(run_windows=[expression])


def test_invalid_timezone_and_naive_timestamp():
    with pytest.raises(ValueError, match="IANA"):
        Schedule(timezone_name="Not/AZone")
    with pytest.raises(ValueError, match="timezone-aware"):
        Schedule().evaluate(datetime(2026, 1, 1))
