"""Parse and redact GSMA LPA activation strings without logging secrets.

Expected format: ``LPA:1$<SM-DP+ address>$<matching-id>``

The matching ID is a secret. Never log the raw activation code, QR contents,
or matching ID. Use :func:`redact_lpa` in logs.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

_LPA_RE = re.compile(r"^LPA:1\$([^$\s/]+)\$(.+)$")


class LpaFormatError(ValueError):
    """The activation string is not a usable GSMA LPA code."""


@dataclass(frozen=True)
class LpaActivation:
    smdp_address: str
    matching_id: str

    @property
    def activation_code(self) -> str:
        return f"LPA:1${self.smdp_address}${self.matching_id}"


def parse_lpa(activation_code: str) -> LpaActivation:
    raw = (activation_code or "").strip()
    if not raw:
        raise LpaFormatError("activation code is empty")
    if " " in raw or "(" in raw or ")" in raw:
        raise LpaFormatError(
            "activation code must be LPA:1$<sm-dp-plus>$<matching-id> with no spaces or URLs"
        )
    match = _LPA_RE.match(raw)
    if match is None:
        raise LpaFormatError(
            "activation code must be LPA:1$<sm-dp-plus>$<matching-id>"
        )
    smdp, matching_id = match.group(1), match.group(2)
    if not smdp or not matching_id:
        raise LpaFormatError("SM-DP+ address and matching ID are required")
    if smdp.lower().startswith("http"):
        raise LpaFormatError("SM-DP+ address must be a hostname, not a URL")
    return LpaActivation(smdp_address=smdp, matching_id=matching_id)


def redact_lpa(activation_code: str) -> str:
    try:
        parsed = parse_lpa(activation_code)
    except LpaFormatError:
        return "LPA:<unparseable>"
    return f"LPA:1${parsed.smdp_address}$<redacted>"
