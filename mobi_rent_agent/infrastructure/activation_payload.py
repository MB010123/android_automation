"""Resolve direct activation strings or decode QR images without a camera."""
from __future__ import annotations

from io import BytesIO
from urllib.parse import urlparse

import requests
import zxingcpp
from PIL import Image, UnidentifiedImageError

from domain.models import ActivationJob
from domain.ports import ActivationPayloadResolver


class ActivationPayloadError(RuntimeError):
    """The supplied activation payload could not be resolved safely."""


class QrActivationPayloadResolver(ActivationPayloadResolver):
    def __init__(self, timeout_seconds: float = 10.0, max_image_bytes: int = 5 * 1024 * 1024) -> None:
        self._timeout = timeout_seconds
        self._max_image_bytes = max_image_bytes
        self._session = requests.Session()

    def resolve(self, job: ActivationJob) -> ActivationJob:
        if job.activation_code:
            return job
        if not job.qr_url:
            raise ActivationPayloadError("activation job has neither activation_code nor qr_url")

        parsed = urlparse(job.qr_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ActivationPayloadError("QR image URL must use HTTPS")

        try:
            response = self._session.get(job.qr_url, timeout=self._timeout, stream=True)
            response.raise_for_status()
            image_bytes = self._read_limited(response)
        except requests.exceptions.Timeout as exc:
            raise ActivationPayloadError(f"QR image download timed out: {exc}") from exc
        except requests.exceptions.RequestException as exc:
            raise ActivationPayloadError(f"QR image download failed: {exc}") from exc

        try:
            with Image.open(BytesIO(image_bytes)) as image:
                result = zxingcpp.read_barcode(image)
        except (UnidentifiedImageError, OSError) as exc:
            raise ActivationPayloadError("QR payload is not a valid image") from exc

        activation_code = result.text.strip() if result is not None else ""
        if not activation_code:
            raise ActivationPayloadError("QR image does not contain a readable activation code")
        if not activation_code.startswith("LPA:"):
            raise ActivationPayloadError("QR image does not contain an LPA activation code")
        return job.with_activation_code(activation_code)

    def _read_limited(self, response: requests.Response) -> bytes:
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > self._max_image_bytes:
                    raise ActivationPayloadError("QR image exceeds the configured size limit")
            except ValueError as exc:
                raise ActivationPayloadError("QR image has an invalid Content-Length") from exc

        data = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            data.extend(chunk)
            if len(data) > self._max_image_bytes:
                raise ActivationPayloadError("QR image exceeds the configured size limit")
        return bytes(data)
