from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infrastructure.lpa import LpaFormatError, parse_lpa, redact_lpa


def test_parse_valid_lpa():
    parsed = parse_lpa("LPA:1$t-mobile.idemia.io$MATCHING-ID-TEST")
    assert parsed.smdp_address == "t-mobile.idemia.io"
    assert parsed.matching_id == "MATCHING-ID-TEST"


def test_reject_spaces_and_embedded_url():
    with pytest.raises(LpaFormatError):
        parse_lpa("LPA:1$t-mobile.idemia.io (http://t-mobile.idemia.io/)$ABC")


def test_reject_missing_matching_id():
    with pytest.raises(LpaFormatError):
        parse_lpa("LPA:1$t-mobile.idemia.io")


def test_redact_hides_matching_id():
    redacted = redact_lpa("LPA:1$t-mobile.idemia.io$MATCHING-ID-TEST")
    assert redacted == "LPA:1$t-mobile.idemia.io$<redacted>"
    assert "MATCHING-ID-TEST" not in redacted


def test_redact_unparseable():
    assert redact_lpa("not-an-lpa") == "LPA:<unparseable>"
