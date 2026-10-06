"""GADS hub adapter for the Slot-1 remote-access proof of concept.

Backend-only. The admin credential never leaves this process; the browser
only ever receives a per-rental, short-lived GADS user credential that is
deleted again on revoke/release.

GADS REST surface used (hub/router/handler.go, hub/auth/auth.go):
  POST   /authenticate                         -> {result: {access_token, ...}}
  POST   /admin/user                           (admin)  create user
  DELETE /admin/user/{username}                (admin)  delete user
  GET    /admin/devices                        (admin)  registered devices
  GET    /available-devices?workspaceId=...    SSE      live connected/in-use state
  POST   /devices/control/{udid}/release       (admin leftover-lease cleanup)

Customer screen/control uses a workspace-scoped user JWT. GADS exclusive
device lock/unlock is not part of the customer rental path.

Nothing here talks to the Android device, EuiccManager or ADB.
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests

from domain.remote_access import (
    PlatformAccessGrant,
    RemoteAccessPlatformError,
    RemoteDeviceStatus,
)

logger = logging.getLogger("vps_backend.remote_access.gads")

# GADS user JWTs are valid for one hour; refresh a little early.
_ADMIN_TOKEN_TTL_SECONDS = 3300.0
_MAX_SESSION_TTL_MINUTES = 360
_SSE_MAX_BYTES = 512 * 1024


@dataclass
class GadsHttpResult:
    status: int
    body: dict[str, Any]

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and bool(self.body.get("success", True))

    @property
    def result(self) -> Any:
        return self.body.get("result")


class GadsHubClient:
    """Thin HTTP client. Injectable ``session`` keeps unit tests offline."""

    def __init__(
        self,
        base_url: str,
        *,
        admin_username: str,
        admin_password: str,
        timeout_seconds: float = 10.0,
        session: requests.Session | None = None,
        clock=time.time,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._admin_username = admin_username
        self._admin_password = admin_password
        self._timeout = timeout_seconds
        self._session = session or requests.Session()
        self._clock = clock
        self._admin_token: str | None = None
        self._admin_token_at: float = 0.0

    # -- low level -----------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> GadsHttpResult:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = self._session.request(
                method,
                f"{self._base}{path}",
                json=json_body,
                params=params,
                headers=headers,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise RemoteAccessPlatformError(f"gads_unreachable: {exc.__class__.__name__}") from exc
        try:
            parsed = response.json() if response.content else {}
        except ValueError:
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {"result": parsed}
        return GadsHttpResult(status=response.status_code, body=parsed)

    def authenticate(self, username: str, password: str) -> str:
        result = self._request(
            "POST",
            "/authenticate",
            json_body={"username": username, "password": password},
        )
        token = ""
        if isinstance(result.result, dict):
            token = str(result.result.get("access_token") or "")
        if not result.ok or not token:
            raise RemoteAccessPlatformError(f"gads_authenticate_failed status={result.status}")
        return token

    def admin_token(self) -> str:
        now = self._clock()
        if self._admin_token and (now - self._admin_token_at) < _ADMIN_TOKEN_TTL_SECONDS:
            return self._admin_token
        self._admin_token = self.authenticate(self._admin_username, self._admin_password)
        self._admin_token_at = now
        return self._admin_token

    def _admin_request(self, method: str, path: str, **kwargs: Any) -> GadsHttpResult:
        result = self._request(method, path, token=self.admin_token(), **kwargs)
        if result.status == 401:
            # Token may have been rotated/expired server-side: re-authenticate once.
            self._admin_token = None
            result = self._request(method, path, token=self.admin_token(), **kwargs)
        return result

    # -- users ---------------------------------------------------------

    def add_user(self, username: str, password: str, workspace_ids: list[str]) -> None:
        result = self._admin_request(
            "POST",
            "/admin/user",
            json_body={
                "username": username,
                "password": password,
                "role": "user",
                "workspace_ids": list(workspace_ids),
            },
        )
        if not result.ok:
            raise RemoteAccessPlatformError(f"gads_add_user_failed status={result.status}")

    def delete_user(self, username: str) -> bool:
        result = self._admin_request("DELETE", f"/admin/user/{quote(username, safe='')}")
        return result.ok or result.status == 404

    # -- devices -------------------------------------------------------

    def registered_devices(self) -> list[dict[str, Any]]:
        result = self._admin_request("GET", "/admin/devices")
        if not result.ok:
            raise RemoteAccessPlatformError(f"gads_admin_devices_failed status={result.status}")
        devices = (result.result or {}).get("devices") if isinstance(result.result, dict) else None
        return [d for d in (devices or []) if isinstance(d, dict)]

    def live_devices(self, workspace_id: str) -> list[dict[str, Any]]:
        """Read one server-sent event from /available-devices and close."""
        try:
            response = self._session.get(
                f"{self._base}/available-devices",
                params={"workspaceId": workspace_id},
                headers={"Accept": "text/event-stream"},
                timeout=self._timeout,
                stream=True,
            )
        except requests.RequestException as exc:
            raise RemoteAccessPlatformError(f"gads_unreachable: {exc.__class__.__name__}") from exc
        try:
            if response.status_code != 200:
                raise RemoteAccessPlatformError(
                    f"gads_available_devices_failed status={response.status_code}"
                )
            payload = _first_sse_data(response)
        finally:
            response.close()
        if payload is None:
            return []
        try:
            parsed = json.loads(payload)
        except ValueError as exc:
            raise RemoteAccessPlatformError("gads_available_devices_invalid_json") from exc
        return [d for d in parsed if isinstance(d, dict)] if isinstance(parsed, list) else []

    def release_device(self, udid: str) -> bool:
        result = self._admin_request(
            "POST",
            f"/devices/control/{quote(udid, safe='')}/release",
        )
        return result.ok or result.status == 404

    def device_control(
        self,
        method: str,
        udid: str,
        suffix: str,
        *,
        token: str,
        json_body: dict[str, Any] | None = None,
    ) -> GadsHttpResult:
        """Customer-lease token only. Never the admin JWT."""
        path = f"/device/{quote(udid, safe='')}/{suffix.lstrip('/')}"
        return self._request(method, path, token=token, json_body=json_body)

    def open_android_mjpeg(self, *, udid: str, token: str, timeout: float = 120.0):
        """Raw MJPEG from the GADS hub device proxy. Caller must close."""
        headers = {"Authorization": f"Bearer {token}", "Accept": "multipart/x-mixed-replace"}
        try:
            response = self._session.get(
                f"{self._base}/device/{quote(udid, safe='')}/android-stream-mjpeg",
                headers=headers,
                timeout=timeout,
                stream=True,
            )
        except requests.RequestException as exc:
            raise RemoteAccessPlatformError(f"gads_unreachable: {exc.__class__.__name__}") from exc
        if response.status_code == 404:
            response.close()
            try:
                response = self._session.get(
                    f"{self._base}/device/{quote(udid, safe='')}/android-stream",
                    headers=headers,
                    timeout=timeout,
                    stream=True,
                )
            except requests.RequestException as exc:
                raise RemoteAccessPlatformError(f"gads_unreachable: {exc.__class__.__name__}") from exc
        if response.status_code != 200:
            status = response.status_code
            response.close()
            raise RemoteAccessPlatformError(f"gads_stream_failed status={status}")
        return response


def _first_sse_data(response: requests.Response) -> str | None:
    """Return the payload of the first ``data:`` line, bounded in size."""
    seen = 0
    for raw in response.iter_lines(decode_unicode=True):
        if raw is None:
            continue
        line = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
        seen += len(line) + 1
        if seen > _SSE_MAX_BYTES:
            raise RemoteAccessPlatformError("gads_available_devices_too_large")
        if line.startswith("data:"):
            return line[len("data:"):].strip()
    return None


def platform_username_for_rental(rental_id: str) -> str:
    """Deterministic, non-secret GADS username for one rental."""
    compact = "".join(ch for ch in str(rental_id).lower() if ch.isalnum())
    return f"rental-{compact[:12] or 'unknown'}"


class GadsRemoteAccessPlatform:
    """Implements ``domain.remote_access.RemoteAccessPlatform`` on GADS."""

    def __init__(
        self,
        client: GadsHubClient,
        *,
        workspace_id: str,
        public_url: str,
        clock=time.time,
    ) -> None:
        self._client = client
        self._workspace_id = workspace_id
        self._public_url = public_url.rstrip("/") + "/"
        self._clock = clock
        self._user_tokens: dict[str, tuple[str, float]] = {}

    def _require_registered_in_workspace(self, device_id: str, workspace_id: str) -> dict[str, Any]:
        expected = str(workspace_id or "").strip()
        if not expected:
            raise RemoteAccessPlatformError("device_not_in_poc_workspace")
        for device in self._client.registered_devices():
            if str(device.get("udid") or "") == device_id:
                if str(device.get("workspace_id") or "") != expected:
                    raise RemoteAccessPlatformError("device_not_in_poc_workspace")
                return device
        raise RemoteAccessPlatformError("device_not_registered")

    def grant_access(
        self,
        *,
        device_id: str,
        rental_id: str,
        ttl_minutes: int,
        workspace_id: str | None = None,
    ) -> PlatformAccessGrant:
        if not str(self._public_url).lower().startswith("https://"):
            # Never return the private hub URL (Tailscale/HTTP) to a browser.
            raise RemoteAccessPlatformError("public_url_not_https")
        workspace = str(workspace_id or self._workspace_id or "").strip()
        self._require_registered_in_workspace(device_id, workspace)
        username = platform_username_for_rental(rental_id)
        password = secrets.token_urlsafe(24)
        # Rotate: drop any stale account from an earlier grant for this rental.
        self._client.delete_user(username)
        self._client.add_user(username, password, [workspace])
        try:
            self._client.authenticate(username, password)
        except RemoteAccessPlatformError:
            self._client.delete_user(username)
            raise
        ttl = max(1, min(int(ttl_minutes), _MAX_SESSION_TTL_MINUTES))
        return PlatformAccessGrant(
            device_id=device_id,
            platform_username=username,
            platform_password=password,
            access_url=self._public_url,
            expires_at=self._clock() + ttl * 60,
        )

    def revoke_access(self, *, device_id: str, platform_username: str) -> bool:
        self._user_tokens.pop(str(platform_username), None)
        released = self._client.release_device(device_id)
        deleted = self._client.delete_user(platform_username)
        return bool(released and deleted)

    def release_device(self, *, device_id: str) -> bool:
        return bool(self._client.release_device(device_id))

    def device_status(
        self,
        *,
        slot_id: int,
        device_id: str,
        workspace_id: str | None = None,
    ) -> RemoteDeviceStatus:
        workspace = str(workspace_id or self._workspace_id or "").strip()
        registered = False
        for device in self._client.registered_devices():
            if str(device.get("udid") or "") == device_id:
                registered = str(device.get("workspace_id") or "") == workspace
                break
        if not registered:
            return RemoteDeviceStatus(
                slot_id=slot_id,
                device_id=device_id,
                registered=False,
                online=False,
                available=False,
            )
        online = False
        available = False
        in_use_by: str | None = None
        raw: dict[str, Any] = {}
        for live in self._client.live_devices(workspace):
            info = live.get("info") if isinstance(live.get("info"), dict) else {}
            if str(info.get("udid") or live.get("udid") or "") != device_id:
                continue
            raw = live
            online = bool(live.get("connected")) and str(live.get("provider_state") or "") == "live"
            in_use = bool(live.get("in_use"))
            available = online and bool(live.get("available")) and not in_use
            in_use_by = str(live.get("in_use_by") or "") or None
            break
        return RemoteDeviceStatus(
            slot_id=slot_id,
            device_id=device_id,
            registered=True,
            online=online,
            available=available,
            in_use_by=in_use_by,
            raw=raw,
        )

    def _user_token(self, username: str, password: str) -> str:
        now = self._clock()
        cached = self._user_tokens.get(username)
        if cached and (now - cached[1]) < 3000.0:
            return cached[0]
        token = self._client.authenticate(username, password)
        self._user_tokens[username] = (token, now)
        return token

    def tap(
        self,
        *,
        device_id: str,
        platform_username: str,
        platform_password: str,
        x: int,
        y: int,
    ) -> None:
        token = self._user_token(platform_username, platform_password)
        result = self._client.device_control(
            "POST",
            device_id,
            "tap",
            token=token,
            json_body={"x": int(x), "y": int(y)},
        )
        if not (200 <= result.status < 300):
            raise RemoteAccessPlatformError(f"gads_tap_failed status={result.status}")

    def swipe(
        self,
        *,
        device_id: str,
        platform_username: str,
        platform_password: str,
        x: int,
        y: int,
        x2: int,
        y2: int,
    ) -> bool:
        token = self._user_token(platform_username, platform_password)
        result = self._client.device_control(
            "POST",
            device_id,
            "swipe",
            token=token,
            json_body={"x": int(x), "y": int(y), "endX": int(x2), "endY": int(y2)},
        )
        if result.status == 404:
            return False
        if not (200 <= result.status < 300):
            raise RemoteAccessPlatformError(f"gads_swipe_failed status={result.status}")
        return True

    def type_text(
        self,
        *,
        device_id: str,
        platform_username: str,
        platform_password: str,
        text: str,
    ) -> None:
        token = self._user_token(platform_username, platform_password)
        result = self._client.device_control(
            "POST",
            device_id,
            "typeText",
            token=token,
            json_body={"text": text},
        )
        if not (200 <= result.status < 300):
            raise RemoteAccessPlatformError(f"gads_type_failed status={result.status}")

    def press_back(
        self,
        *,
        device_id: str,
        platform_username: str,
        platform_password: str,
    ) -> bool:
        token = self._user_token(platform_username, platform_password)
        result = self._client.device_control("POST", device_id, "back", token=token, json_body={})
        if result.status == 404:
            return False
        if not (200 <= result.status < 300):
            raise RemoteAccessPlatformError(f"gads_back_failed status={result.status}")
        return True

    def open_mjpeg_stream(
        self,
        *,
        device_id: str,
        platform_username: str,
        platform_password: str,
    ):
        token = self._user_token(platform_username, platform_password)
        return self._client.open_android_mjpeg(udid=device_id, token=token)


def gads_platform_from_config(config: Any, *, session: requests.Session | None = None) -> GadsRemoteAccessPlatform | None:
    """Build the adapter from ``AgentConfig``; ``None`` when not fully configured."""
    url = getattr(config, "remote_access_platform_url", None)
    user = getattr(config, "remote_access_admin_username", None)
    password = getattr(config, "remote_access_admin_password", None)
    workspace = getattr(config, "remote_access_workspace_id", None)
    if not (url and user and password and workspace):
        return None
    public_url = getattr(config, "remote_access_public_url", None)
    if not public_url or not str(public_url).lower().startswith("https://"):
        logger.warning("remote_access_public_url_not_https")
        public_url = ""
    client = GadsHubClient(
        url,
        admin_username=user,
        admin_password=password,
        timeout_seconds=float(getattr(config, "request_timeout_seconds", 10.0)),
        session=session,
    )
    return GadsRemoteAccessPlatform(client, workspace_id=workspace, public_url=public_url)
