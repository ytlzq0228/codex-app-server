"""Shared presentation formatting for amounts and token counts."""
from decimal import Decimal


def money(value):
    return format(Decimal(str(value)), ".4f") if value is not None else None


def tokens(value):
    if value is None:
        return None
    number = Decimal(str(value))
    return f"{number / Decimal(1000000):.4f} million" if number > 1000000 else str(int(number))
