from __future__ import annotations

import httpx

from config import instance_dir
from pipeline.core import PipelineError
from schemas.models import InstanceSpec
from services.core_images import validate_core_app_images
from services.providers.registry import get_cloud_provider
from services.providers.hetzner.api import validate_server_types
from services.secrets import generate_secrets
from services.storage import append_log, load_secrets
from services.volumes import validate_volume_sizes


def write_ssh_files(spec: InstanceSpec) -> None:
    inst = instance_dir(spec.id)
    ssh_dir = inst / "ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    pub = ssh_dir / "id_ed25519.pub"
    pub.write_text(spec.ssh_public_key.strip() + "\n")
    priv = ssh_dir / "id_ed25519"
    if spec.ssh_private_key_pem:
        priv.write_text(spec.ssh_private_key_pem if spec.ssh_private_key_pem.endswith("\n") else spec.ssh_private_key_pem + "\n")
    elif not priv.exists():
        raise PipelineError("SSH private key missing — click Generate key pair in the wizard")
    priv.chmod(0o600)


def validate_cloud_token(spec: InstanceSpec) -> tuple[bool, str]:
    provider = get_cloud_provider(spec)
    token = spec.hetzner_api_token
    if not token:
        return False, f"{provider.id} API token required"
    return provider.validate_token(token)


def validate_hetzner_token(token: str) -> tuple[bool, str]:
    """Backward-compatible Hetzner token check for wizard API."""
    from services.providers.hetzner.provider import HetznerProvider

    return HetznerProvider().validate_token(token)


def validate_spec(spec: InstanceSpec) -> None:
    if not spec.hetzner_api_token:
        raise PipelineError("Hetzner API token required")
    ok, msg = validate_cloud_token(spec)
    if not ok:
        raise PipelineError(msg)
    if not spec.hub_domain:
        raise PipelineError("hub_domain required")
    if not spec.admin_email:
        raise PipelineError("admin_email required")
    if not spec.location:
        raise PipelineError("Choose a Hetzner location in the wizard")
    if not spec.ssh_public_key or not spec.ssh_key_generated:
        raise PipelineError("Generate an SSH key pair in the wizard before provisioning")
    if not all([spec.smtp_host, spec.smtp_user, spec.smtp_password, spec.smtp_from]):
        raise PipelineError("SMTP settings incomplete (host, user, password, and sender email required)")
    image_errors = validate_core_app_images(spec.core_app_images)
    if image_errors:
        raise PipelineError("; ".join(image_errors))
    volume_errors = validate_volume_sizes(
        spec.pgsql_volume_size,
        spec.reticulum_volume_size,
        spec.pgsql_backup_volume_size,
    )
    if volume_errors:
        raise PipelineError("; ".join(volume_errors))

    from services.volume_inventory import validate_reattach

    reattach_errors = validate_reattach(spec)
    if reattach_errors:
        raise PipelineError("; ".join(reattach_errors))

    if spec.restore_from_bundle.enabled:
        from services.restore_bundle_paths import has_restore_bundle

        if not has_restore_bundle(spec.id):
            raise PipelineError(
                "Restore from local bundle is enabled but restore-bundle/ is missing on disk — "
                "save a restore bundle before destroy or disable the option"
            )

    types = spec.resolved_server_types()
    ok, msg, _invalid = validate_server_types(
        spec.hetzner_api_token,
        spec.location,
        types["master_server_type"],
        types["web_server_type"],
        types["webrtc_server_type"],
    )
    if not ok:
        raise PipelineError(f"Server types invalid for {spec.location}: {msg}")


def reset_stale_terraform_after_console_wipe(instance_id: str, spec: InstanceSpec) -> None:
    """If cloud has no master but ./data still has tfstate, wipe state for a clean apply."""
    provider = get_cloud_provider(spec)
    tf_dir = instance_dir(instance_id) / "terraform"
    state_files = list(tf_dir.glob("terraform.tfstate*"))
    if not state_files:
        return
    try:
        if provider.cluster_master_present(spec.hetzner_api_token):
            return
    except httpx.HTTPError as e:
        append_log(instance_id, f"Could not verify cloud servers before Terraform: {e}")
        return
    append_log(
        instance_id,
        "No hcce-master-db in cloud — clearing saved Terraform state and kubeconfig (fresh build after console delete)",
    )
    for path in state_files:
        path.unlink()
    kc = instance_dir(instance_id) / "kubeconfig"
    if kc.exists():
        kc.unlink()
