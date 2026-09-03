from decimal import Decimal

import pytest

from src.money import (apply_rate, format_paise, split_proportional, to_paise,
                       within_tolerance)


# ---------------------------------------------------------------- to_paise
def test_to_paise_basic():
    assert to_paise("1234.56") == 123456


def test_to_paise_accepts_int_and_decimal():
    assert to_paise(100) == 10000
    assert to_paise(Decimal("0.01")) == 1


def test_to_paise_strips_symbol_and_commas():
    assert to_paise("₹1,23,456.78") == 12345678


@pytest.mark.parametrize("s,expected", [
    ("1234.565", 123456),   # half-even rounds DOWN to the even paise
    ("1234.575", 123458),   # half-even rounds UP to the even paise
    ("0.005", 0),
    ("0.015", 2),
])
def test_to_paise_banker_rounding_at_half(s, expected):
    assert to_paise(s) == expected


def test_to_paise_negative():
    assert to_paise("-12.34") == -1234


def test_to_paise_rejects_garbage():
    with pytest.raises(ValueError):
        to_paise("twelve rupees")


# ------------------------------------------------------------- format_paise
@pytest.mark.parametrize("p,expected", [
    (0, "₹0.00"),
    (5, "₹0.05"),
    (123456, "₹1,234.56"),
    (12345678, "₹1,23,456.78"),          # Indian lakh grouping
    (1234567890, "₹1,23,45,678.90"),     # crore grouping
    (-100, "-₹1.00"),
])
def test_format_paise(p, expected):
    assert format_paise(p) == expected


def test_format_paise_rejects_float():
    with pytest.raises(TypeError):
        format_paise(100.0)


# ---------------------------------------------------------------- apply_rate
def test_apply_rate_simple_percentage():
    assert apply_rate(100000, 0.02) == 2000


def test_apply_rate_zero():
    assert apply_rate(999999, 0) == 0


def test_apply_rate_half_even_down_and_up():
    # 50 * 0.01 = 0.5 -> 0 (even); 150 * 0.01 = 1.5 -> 2 (even)
    assert apply_rate(50, 0.01) == 0
    assert apply_rate(150, 0.01) == 2


def test_apply_rate_no_float_drift():
    # 0.1 is not representable in binary; Decimal keeps this exact.
    assert apply_rate(70000, 0.1) == 7000


def test_apply_rate_negative_amount_for_chargebacks():
    assert apply_rate(-100000, 0.02) == -2000


def test_apply_rate_rejects_float_amount():
    with pytest.raises(TypeError):
        apply_rate(1000.0, 0.02)


# ----------------------------------------------------------- within_tolerance
@pytest.mark.parametrize("a,b,tol,expected", [
    (100, 100, 0, True),
    (100, 102, 2, True),
    (100, 103, 2, False),
    (103, 100, 2, False),
    (-5, -3, 2, True),
])
def test_within_tolerance(a, b, tol, expected):
    assert within_tolerance(a, b, tol) is expected


def test_within_tolerance_rejects_negative_tol():
    with pytest.raises(ValueError):
        within_tolerance(1, 1, -1)


# --------------------------------------------------------- split_proportional
def test_split_proportional_loses_no_paise():
    parts = split_proportional(1001, [1, 1, 1])
    assert sum(parts) == 1001
    assert parts == [334, 334, 333]


def test_split_proportional_respects_weights():
    assert split_proportional(1000, [30, 70]) == [300, 700]


def test_split_proportional_negative_amount_sums_back():
    parts = split_proportional(-1001, [1, 1, 1])
    assert sum(parts) == -1001


def test_split_proportional_rejects_zero_weights():
    with pytest.raises(ValueError):
        split_proportional(100, [0, 0])
