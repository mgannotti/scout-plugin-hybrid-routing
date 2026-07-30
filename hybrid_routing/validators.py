"""Value validators for sensitivity patterns.

A regex alone cannot tell a payment card from an order reference — both are
just digit runs. Patterns that match a *structured* identifier can declare a
validator in the config, and the classifier only treats the match as real if
the validator accepts it.

Validators are referenced by name from `data/routing_config.yaml`, so the
config stays declarative: reading the YAML tells you the whole rule, with no
behaviour hidden in Python that the config does not mention.
"""

from __future__ import annotations

import re
from typing import Callable

_NON_DIGIT = re.compile(r"\D")


def luhn(value: str) -> bool:
    """True if `value`'s digits satisfy the Luhn checksum (ISO/IEC 7812).

    Separators are ignored, so "4111 1111 1111 1111" and "4111111111111111"
    are treated alike. Runs shorter than 12 digits are rejected outright: at
    that length the checksum carries too little signal and every short numeric
    token in a document would pass one time in ten.
    """
    digits = _NON_DIGIT.sub("", value or "")
    if len(digits) < 12:
        return False

    total = 0
    parity = len(digits) % 2
    for index, char in enumerate(digits):
        digit = int(char)
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


# Name -> predicate. Referenced by `validator:` in the config; anything not
# listed here is a config error, surfaced by validate() rather than ignored.
VALIDATORS: dict[str, Callable[[str], bool]] = {
    "luhn": luhn,
}

__all__ = ["luhn", "VALIDATORS"]
