"""Build the subscription provisioner used by the daemon and Farm assign tasks."""
from __future__ import annotations

from collections.abc import Callable

from domain.esim_capabilities import AndroidAuthorizationSnapshot
from domain.models import ActivationJob
from domain.ports import ActivationPayloadResolver, SubscriptionProvisioner
from domain.slot_isolation import SlotIsolationPolicy
from domain.verification import FourLayerVerification
from infrastructure.activation_payload import QrActivationPayloadResolver
from infrastructure.adb_companion import AdbCommandRunner, AdbForwardedJsonClient
from infrastructure.adb_four_layer import collect_four_layer_verification
from infrastructure.android_authorization_probe import AdbAuthorizationProbe
from infrastructure.authorized_esim_provider import (
    AuthorizedEsimProvider,
    companion_provision_transport,
    live_download_may_arm,
    select_esim_provider,
)
from infrastructure.config import AgentConfig

VerificationSource = Callable[[str, ActivationJob], FourLayerVerification | None]


def build_subscription_provisioner(
    config: AgentConfig,
    *,
    isolation: SlotIsolationPolicy | None = None,
) -> tuple[SubscriptionProvisioner, ActivationPayloadResolver, SlotIsolationPolicy]:
    """Return provisioner, QR resolver, and isolation policy for direct slot jobs."""
    policy = isolation or SlotIsolationPolicy(config.provisioning_allowed_slot_ids)
    payload_resolver = QrActivationPayloadResolver(
        timeout_seconds=config.request_timeout_seconds,
    )
    armed = live_download_may_arm(
        real_esim_enabled=config.real_esim_enabled,
        esim_live_download_armed=config.esim_live_download_armed,
        allowed_slot_ids=policy.allowed_slot_ids,
    )
    if armed:
        runner = AdbCommandRunner(config.adb_path, config.provisioning_timeout_seconds)
        client = AdbForwardedJsonClient(
            runner,
            config.provisioning_companion_socket,
            config.provisioning_timeout_seconds,
        )
        probe = AdbAuthorizationProbe(runner, client, real_esim_flag=config.real_esim_enabled)

        def _verify(serial: str, job: ActivationJob) -> FourLayerVerification | None:
            _ = job
            status: dict = {}
            try:
                status = client.request(serial, {"command": "get_esim_status"})
            except Exception:
                status = {}
            return collect_four_layer_verification(runner, serial, status)

        provisioner: SubscriptionProvisioner = AuthorizedEsimProvider(
            probe,
            policy,
            live_download_armed=True,
            download_transport=companion_provision_transport(client),
            verification_source=_verify,
        )
    else:
        provisioner = select_esim_provider(
            AndroidAuthorizationSnapshot(real_esim_flag=config.real_esim_enabled),
            policy,
            live_download_armed=False,
        )
    return provisioner, payload_resolver, policy
