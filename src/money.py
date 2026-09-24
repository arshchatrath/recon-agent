"""All money is an int number of paise. Every rounding decision lives here.

Rationale: floats cannot represent 0.1 and silently drift; a reconciliation
system that drifts by a paise per row is worse than useless. Rounding is
banker's (ROUND_HALF_EVEN) everywhere, applied via Decimal. Python's
built-in round() on floats inherits binary-representation errors
(round(2.675, 2) == 2.67) and is not safe for money.
"""
from decimal import Decimal, ROUND_HALF_EVEN

__all__ = ["format_paise", "apply_rate", "within_tolerance"]


def format_paise(p: int) -> str:
    """123456 -> '₹1,234.56'. Indian lakh/crore digit grouping."""
    if not isinstance(p, int):
        raise TypeError(f"paise must be int, got {type(p).__name__}")
    sign = "-" if p < 0 else ""
    whole, frac = divmod(abs(p), 100)
    s = str(whole)
    if len(s) > 3:                      # 12,34,567 not 1,234,567
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            head, chunk = head[:-2], head[-2:]
            parts.insert(0, chunk)
        s = ",".join(([head] if head else []) + parts + [tail])
    return f"{sign}\u20b9{s}.{frac:02d}"


def apply_rate(amount: int, rate) -> int:
    """Percentage-of-amount in paise, half-even. rate=0.05 -> 5%."""
    if not isinstance(amount, int):
        raise TypeError(f"amount must be int paise, got {type(amount).__name__}")
    d = Decimal(amount) * Decimal(str(rate))
    return int(d.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def within_tolerance(a: int, b: int, tol: int) -> bool:
    """|a - b| <= tol, all ints. tol must be non-negative."""
    if tol < 0:
        raise ValueError("tolerance must be >= 0")
    return abs(a - b) <= tol
