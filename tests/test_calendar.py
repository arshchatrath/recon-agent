from datetime import date, timedelta

import pytest

from src.calendar_utils import (add_working_days, get_calendar, is_working_day,
                                working_days_between)


def test_weekend_is_not_a_working_day():
    assert not is_working_day(date(2025, 1, 11))   # Saturday
    assert not is_working_day(date(2025, 1, 12))   # Sunday
    assert is_working_day(date(2025, 1, 13))       # Monday


def test_indian_public_holiday_is_skipped():
    # 15 Aug 2025 (Independence Day) is a Friday, so a Thursday T+1 lands Monday.
    assert not is_working_day(date(2025, 8, 15))
    assert add_working_days(date(2025, 8, 14), 1) == date(2025, 8, 18)


def test_republic_day_2026_is_a_holiday():
    assert not is_working_day(date(2026, 1, 26))   # a Monday


def test_friday_card_settles_the_following_tuesday():
    friday = date(2025, 1, 10)
    assert add_working_days(friday, 2) == date(2025, 1, 14)   # Tue, over a weekend


def test_upi_t_plus_one_over_a_weekend():
    assert add_working_days(date(2025, 1, 10), 1) == date(2025, 1, 13)


def test_non_working_start_rolls_forward_without_consuming_a_day():
    # Saturday rolls to Monday, then +1 working day lands Tuesday.
    assert add_working_days(date(2025, 1, 11), 0) == date(2025, 1, 13)
    assert add_working_days(date(2025, 1, 11), 1) == date(2025, 1, 14)


def test_between_is_half_open_and_inverse_of_add():
    d = date(2025, 2, 3)
    for n in range(0, 12):
        assert working_days_between(d, add_working_days(d, n)) == n


def test_between_is_zero_for_same_day_and_negative_backwards():
    assert working_days_between(date(2025, 1, 13), date(2025, 1, 13)) == 0
    assert working_days_between(date(2025, 1, 20), date(2025, 1, 13)) == -5


def test_prefix_sum_matches_brute_force():
    cal = get_calendar()
    a, b = date(2025, 1, 1), date(2025, 4, 30)
    brute = sum(cal.is_working_day(a + timedelta(days=i))
                for i in range((b - a).days))
    assert cal.working_days_between(a, b) == brute


def test_out_of_range_date_raises():
    with pytest.raises(ValueError):
        is_working_day(date(2010, 1, 1))


def test_negative_add_raises():
    with pytest.raises(ValueError):
        add_working_days(date(2025, 1, 13), -1)
