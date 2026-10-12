from __future__ import annotations

from decimal import Decimal, InvalidOperation


def clean(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def positive_decimal(value: object) -> Decimal | None:
    parsed = decimal_or_none(value)
    return parsed if parsed is not None and parsed > 0 else None


def from_minor_units(value: object, minor_unit: object) -> Decimal | None:
    parsed = decimal_or_none(value)
    if parsed is None:
        return None
    try:
        places = int(str(minor_unit))
    except (TypeError, ValueError):
        places = 2
    if places < 0:
        places = 0
    return parsed / (Decimal(10) ** places)
