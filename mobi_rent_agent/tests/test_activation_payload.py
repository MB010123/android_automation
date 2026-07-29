from __future__ import annotations

from io import BytesIO
import sys
from pathlib import Path

import zxingcpp
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.models import ActivationJob
from infrastructure.activation_payload import QrActivationPayloadResolver


class FakeResponse:
    def __init__(self, content: bytes) -> None:
        self.headers = {"Content-Length": str(len(content))}
        self._content = content

    def raise_for_status(self) -> None:
        pass

    def iter_content(self, chunk_size: int):
        yield self._content


def make_qr_png(text: str) -> bytes:
    barcode = zxingcpp.create_barcode(text, zxingcpp.BarcodeFormat.QRCode)
    image = Image.fromarray(zxingcpp.write_barcode_to_image(barcode, 200))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_resolver_decodes_lpa_string_from_qr_without_camera(monkeypatch):
    resolver = QrActivationPayloadResolver()
    response = FakeResponse(make_qr_png("LPA:1$server.example$activation"))
    monkeypatch.setattr(resolver._session, "get", lambda *_args, **_kwargs: response)

    resolved = resolver.resolve(
        ActivationJob(job_id="job-qr", slot_id=1, qr_url="https://storage.example/qr.png")
    )

    assert resolved.activation_code == "LPA:1$server.example$activation"
    assert resolved.qr_url is None


def test_direct_activation_string_requires_no_download():
    job = ActivationJob(job_id="job-code", slot_id=1, activation_code="LPA:1$server$code")

    assert QrActivationPayloadResolver().resolve(job) is job
