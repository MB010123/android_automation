"""HTTP adapter for claiming activation jobs and reporting their results."""
from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import quote

import requests

from domain.models import ActivationJob, ProvisioningResult
from domain.ports import ActivationJobSource


class ProvisioningTransportError(RuntimeError):
    """Raised when the provisioning backend cannot complete an operation."""


class HttpActivationJobSource(ActivationJobSource):
    def __init__(self, endpoint: str, hardware_agent_token: str, timeout_seconds: float = 10.0) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._timeout = timeout_seconds
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {hardware_agent_token}",
                "X-Hardware-Agent-Token": hardware_agent_token,
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    def fetch_pending(self, slot_ids: Iterable[int]) -> Iterable[ActivationJob]:
        try:
            response = self._session.post(
                f"{self._endpoint}/claim",
                json={"slot_ids": sorted(slot_ids)},
                timeout=self._timeout,
            )
            response.raise_for_status()
            body = response.json()
        except requests.exceptions.Timeout as exc:
            raise ProvisioningTransportError(f"activation fetch timed out: {exc}") from exc
        except requests.exceptions.ConnectionError as exc:
            raise ProvisioningTransportError(f"activation fetch connection failed: {exc}") from exc
        except (requests.exceptions.RequestException, ValueError) as exc:
            raise ProvisioningTransportError(f"activation fetch failed: {exc}") from exc

        raw_jobs = body.get("jobs") if isinstance(body, dict) else body
        if not isinstance(raw_jobs, list):
            raise ProvisioningTransportError("activation response must be a list or an object containing 'jobs'")

        jobs: list[ActivationJob] = []
        for index, raw_job in enumerate(raw_jobs):
            if not isinstance(raw_job, dict):
                raise ProvisioningTransportError(f"activation job at index {index} must be an object")
            try:
                switch_after_download = raw_job.get("switch_after_download", True)
                if not isinstance(switch_after_download, bool):
                    raise TypeError("switch_after_download must be a boolean")
                jobs.append(
                    ActivationJob(
                        job_id=str(raw_job["job_id"]),
                        slot_id=int(raw_job["slot_id"]),
                        activation_code=(
                            str(raw_job["activation_code"])
                            if raw_job.get("activation_code") is not None
                            else None
                        ),
                        qr_url=str(raw_job["qr_url"]) if raw_job.get("qr_url") is not None else None,
                        switch_after_download=switch_after_download,
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ProvisioningTransportError(f"invalid activation job at index {index}: {exc}") from exc
        return jobs

    def report_result(self, result: ProvisioningResult) -> None:
        result_endpoint = f"{self._endpoint}/{quote(result.job_id, safe='')}/result"
        try:
            response = self._session.post(
                result_endpoint,
                json=result.to_dict(),
                timeout=self._timeout,
            )
            response.raise_for_status()
        except requests.exceptions.Timeout as exc:
            raise ProvisioningTransportError(f"result report timed out: {exc}") from exc
        except requests.exceptions.ConnectionError as exc:
            raise ProvisioningTransportError(f"result report connection failed: {exc}") from exc
        except requests.exceptions.RequestException as exc:
            raise ProvisioningTransportError(f"result report failed: {exc}") from exc

    def close(self) -> None:
        self._session.close()
