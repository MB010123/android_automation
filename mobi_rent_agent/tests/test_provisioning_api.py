from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infrastructure.provisioning_api import HttpActivationJobSource, ProvisioningTransportError


class FakeResponse:
    def __init__(self, body) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        pass

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, body) -> None:
        self._body = body
        self.calls: list[tuple[str, dict]] = []

    def post(self, endpoint: str, **kwargs):
        self.calls.append((endpoint, kwargs))
        return FakeResponse(self._body)


def make_source(body) -> tuple[HttpActivationJobSource, FakeSession]:
    source = HttpActivationJobSource("https://backend.example/provisioning", "token")
    session = FakeSession(body)
    source._session = session
    return source, session


def test_fetch_atomically_claims_jobs_for_managed_slots():
    source, session = make_source(
        {
            "jobs": [
                {
                    "job_id": "job-1",
                    "slot_id": 1,
                    "activation_code": "LPA:1$server$secret",
                    "switch_after_download": False,
                }
            ]
        }
    )

    jobs = list(source.fetch_pending([2, 1]))

    assert jobs[0].switch_after_download is False
    assert session.calls == [
        (
            "https://backend.example/provisioning/claim",
            {"json": {"slot_ids": [1, 2]}, "timeout": 10.0},
        )
    ]


def test_fetch_rejects_string_boolean_instead_of_coercing_it():
    source, _session = make_source(
        [
            {
                "job_id": "job-1",
                "slot_id": 1,
                "activation_code": "LPA:1$server$secret",
                "switch_after_download": "false",
            }
        ]
    )

    with pytest.raises(ProvisioningTransportError, match="must be a boolean"):
        list(source.fetch_pending([1]))
