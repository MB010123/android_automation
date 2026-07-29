"""Shared ADB command and local-socket companion transport."""
from __future__ import annotations

import json
import logging
import socket
import subprocess
from dataclasses import dataclass

logger = logging.getLogger("mobi_rent_agent.adb_companion")


class AdbCommandError(RuntimeError):
    """An ADB command failed, timed out, or could not be started."""


@dataclass(frozen=True)
class AdbCommandResult:
    stdout: str
    stderr: str


class AdbCommandRunner:
    def __init__(self, adb_path: str = "adb", timeout_seconds: float = 10.0) -> None:
        self._adb_path = adb_path
        self._timeout = timeout_seconds

    def run(self, serial: str, arguments: list[str]) -> AdbCommandResult:
        command = [self._adb_path, "-s", serial, *arguments]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AdbCommandError(f"ADB command timed out for serial {serial}") from exc
        except FileNotFoundError as exc:
            raise AdbCommandError(f"ADB executable not found: {self._adb_path}") from exc
        except OSError as exc:
            raise AdbCommandError(f"ADB command could not start for serial {serial}: {exc}") from exc

        if completed.returncode != 0:
            detail = completed.stderr.strip() or f"exit code {completed.returncode}"
            raise AdbCommandError(f"ADB command failed for serial {serial}: {detail}")
        return AdbCommandResult(stdout=completed.stdout.strip(), stderr=completed.stderr.strip())


class AdbForwardedJsonClient:
    """Sends one JSON-line request to a device-local abstract socket."""

    def __init__(
        self,
        command_runner: AdbCommandRunner,
        companion_socket: str,
        timeout_seconds: float,
        max_response_bytes: int = 64 * 1024,
    ) -> None:
        self._command_runner = command_runner
        self._companion_socket = companion_socket
        self._timeout = timeout_seconds
        self._max_response_bytes = max_response_bytes

    def request(self, serial: str, payload: dict) -> dict:
        local_port: int | None = None
        try:
            forward = self._command_runner.run(
                serial,
                ["forward", "tcp:0", f"localabstract:{self._companion_socket}"],
            )
            local_port = self._parse_port(forward.stdout)
            return self._send(local_port, payload)
        finally:
            if local_port is not None:
                try:
                    self._command_runner.run(serial, ["forward", "--remove", f"tcp:{local_port}"])
                except AdbCommandError as exc:
                    logger.warning("Failed to remove ADB forward for %s: %s", serial, exc)

    @staticmethod
    def _parse_port(output: str) -> int:
        try:
            port = int(output)
        except ValueError as exc:
            raise ValueError("ADB did not return an allocated forwarding port") from exc
        if not (1 <= port <= 65535):
            raise ValueError(f"ADB returned invalid forwarding port {port}")
        return port

    def _send(self, local_port: int, payload: dict) -> dict:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
        with socket.create_connection(("127.0.0.1", local_port), timeout=self._timeout) as client:
            client.settimeout(self._timeout)
            client.sendall(encoded)
            raw_response = self._read_line(client)

        try:
            response = json.loads(raw_response)
        except json.JSONDecodeError as exc:
            raise ValueError("device companion returned invalid JSON") from exc
        if not isinstance(response, dict):
            raise ValueError("device companion response must be an object")
        return response

    def _read_line(self, client: socket.socket) -> str:
        chunks = bytearray()
        while len(chunks) < self._max_response_bytes:
            chunk = client.recv(min(4096, self._max_response_bytes - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
            if b"\n" in chunk:
                break
        if b"\n" not in chunks:
            raise ValueError("device companion response was incomplete or too large")
        return bytes(chunks).split(b"\n", 1)[0].decode("utf-8")
