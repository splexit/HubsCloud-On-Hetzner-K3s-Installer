from __future__ import annotations

import json
import shutil
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from config import TEMPLATES_DIR, instance_dir
from schemas.models import InstanceSpec
from services.hetzner import estimate_cluster_monthly_cost
from services.core_images import SUPPORT_APP_IMAGES, image_pull_policy, resolve_all_core_app_images
from services.secrets import generate_rsa_material, generate_self_signed_cert


def sed_replacement(value: str) -> str:
    """Escape a value that the Reticulum image will sed into config.toml.

    The image ships config.toml.template with <PLACEHOLDER> tokens and replaces
    them from the turkeyCfg_* env vars using sed. Observed failure: an SMTP
    password "t&5M*..." reached Swoosh as "t<SMTP_PASS>5M*..." — in a sed
    replacement "&" means "the whole match" — and authentication failed with no
    useful error. Users cannot be asked to avoid characters in a password their
    provider issued, so this is handled here.

    Escaping only "&" and "\\" is not enough: the s/// delimiter is also special,
    and we cannot see the image's entrypoint to know whether it is "/", "|", "#"
    or something else. So backslash-escape EVERY non-alphanumeric character. In a
    sed replacement, "\\X" yields a literal X for any X without special meaning,
    which makes this correct whatever the delimiter turns out to be. Alphanumerics
    are deliberately left alone — "\\n", "\\t" and "\\1".."\\9" would otherwise
    become newlines, tabs and backreferences.

    Apply ONLY to values consumed through turkeyCfg_* substitution — never to
    PERMS_KEY (legitimately contains backslashes) or DB_PASS (also used verbatim
    in the PSQL/PGRST_DB_URI connection strings).
    """
    return "".join(c if c.isalnum() else "\\" + c for c in (value or ""))


def _jinja_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    env.filters["sed_replacement"] = sed_replacement
    env.filters["pull_policy"] = image_pull_policy
    return env


def _ensure_instance_secrets(spec: InstanceSpec, secrets: dict[str, str]) -> dict[str, str]:
    """Fill in any missing per-instance key material and persist it.

    Anything generated here MUST be written back to secrets.json. Every render
    calls this, so material that is generated but not persisted would differ
    between hcce.yaml and the k8s manifests of a single run, and again on the
    next re-render — silently breaking a deployment whose components no longer
    agree on the same key.
    """
    from services.storage import save_secrets

    filled = dict(secrets)
    if "perms_key" not in filled or "pgrst_jwt_secret" not in filled:
        filled["perms_key"], filled["pgrst_jwt_secret"] = generate_rsa_material()
    # Bootstrap TLS material is tied to the hub domain, so regenerate it if the
    # domain changed as well as when it is missing entirely.
    if spec.hub_domain and not filled.get("init_cert"):
        filled["init_cert"], filled["init_key"] = generate_self_signed_cert(spec.hub_domain)

    if filled != secrets:
        save_secrets(spec.id, filled)
    return filled


def _base_context(spec: InstanceSpec, secrets: dict[str, str]) -> dict:
    types = spec.resolved_server_types()
    secrets = _ensure_instance_secrets(spec, secrets)
    inst = instance_dir(spec.id)
    resolved_images = resolve_all_core_app_images(spec.core_app_images)
    return {
        "hetzner_api_token": spec.hetzner_api_token,
        "hub_domain": spec.hub_domain,
        "admin_email": spec.admin_email,
        "location": spec.location,
        "smtp_host": spec.smtp_host,
        "smtp_port": str(spec.smtp_port),
        "smtp_user": spec.smtp_user,
        "smtp_password": spec.smtp_password,
        "smtp_from": spec.smtp_from,
        "sketchfab_api_key": spec.sketchfab_api_key or "",
        "tenor_api_key": spec.tenor_api_key or "",
        "pgsql_volume_size": spec.pgsql_volume_size,
        "reticulum_volume_size": spec.reticulum_volume_size,
        "pgsql_backup_volume_size": spec.pgsql_backup_volume_size,
        "k3s_token": secrets["k3s_token"],
        "db_password": secrets["db_password"],
        "node_cookie": secrets["node_cookie"],
        "guardian_key": secrets["guardian_key"],
        "phx_key": secrets["phx_key"],
        "admin_password": secrets["admin_password"],
        "perms_key": secrets["perms_key"],
        "pgrst_jwt_secret": secrets["pgrst_jwt_secret"],
        "init_cert": secrets.get("init_cert", ""),
        "init_key": secrets.get("init_key", ""),
        "reticulum_image": resolved_images["reticulum"],
        "hubs_image": resolved_images["hubs"],
        "spoke_image": resolved_images["spoke"],
        "dialog_image": SUPPORT_APP_IMAGES["dialog"],
        "nearspark_image": SUPPORT_APP_IMAGES["nearspark"],
        "photomnemonic_image": SUPPORT_APP_IMAGES["photomnemonic"],
        "ssh_public_key_path": str(inst / "ssh" / "id_ed25519.pub"),
        "ssh_private_key_path": str(inst / "ssh" / "id_ed25519"),
        "firewall_hardened": "true" if spec.firewall_hardened else "false",
        "firewall_allow_ssh": "true" if spec.firewall_allow_ssh else "false",
        **types,
        "private_network_id": "{{ private_network_id }}",
    }


def render_terraform(
    spec: InstanceSpec,
    secrets: dict[str, str],
    *,
    firewall_hardened: bool | None = None,
    firewall_allow_ssh: bool | None = None,
) -> Path:
    inst = instance_dir(spec.id)
    tf_dir = inst / "terraform"
    tf_dir.mkdir(parents=True, exist_ok=True)
    src_tf = TEMPLATES_DIR / "terraform"
    for name in ["main.tf", "variables.tf", "outputs.tf", "cloud-init-master.yaml", "cloud-init-webrtc-worker.yaml", "cloud-init-web-worker.yaml"]:
        shutil.copy2(src_tf / name, tf_dir / name)
    env = _jinja_env()
    template = env.get_template("terraform/terraform.tfvars.j2")
    ctx = _base_context(spec, secrets)
    if firewall_hardened is not None:
        ctx["firewall_hardened"] = "true" if firewall_hardened else "false"
    if firewall_allow_ssh is not None:
        ctx["firewall_allow_ssh"] = "true" if firewall_allow_ssh else "false"
    types = spec.resolved_server_types()
    if spec.hetzner_api_token and spec.location:
        ctx["estimated_monthly_cost_eur"] = estimate_cluster_monthly_cost(
            spec.hetzner_api_token,
            spec.location,
            types["master_server_type"],
            types["web_server_type"],
            types["webrtc_server_type"],
        )
    else:
        ctx["estimated_monthly_cost_eur"] = ""
    (tf_dir / "terraform.tfvars").write_text(template.render(**ctx))
    return tf_dir / "terraform.tfvars"


def render_hcce(spec: InstanceSpec, secrets: dict[str, str]) -> Path:
    inst = instance_dir(spec.id)
    rendered_dir = inst / "rendered"
    rendered_dir.mkdir(parents=True, exist_ok=True)
    env = _jinja_env()
    template = env.get_template("hcce/hcce.yaml.j2")
    ctx = _base_context(spec, secrets)
    out = rendered_dir / "hcce.yaml"
    out.write_text(template.render(**ctx))
    return out


def render_static_pvs(spec: InstanceSpec, secrets: dict[str, str]) -> Path | None:
    from services.volume_inventory import reattach_entries

    entries = reattach_entries(spec)
    if not entries:
        inst = instance_dir(spec.id)
        out = inst / "rendered" / "hcce-static-pvs.yaml"
        if out.exists():
            out.unlink()
        return None

    inst = instance_dir(spec.id)
    rendered_dir = inst / "rendered"
    rendered_dir.mkdir(parents=True, exist_ok=True)
    env = _jinja_env()
    template = env.get_template("hcce/static-pvs.yaml.j2")
    ctx = _base_context(spec, secrets)
    ctx["reattach_volumes"] = [
        {
            "pvc_name": e.pvc_name,
            "size": e.size,
            "hetzner_volume_id": e.hetzner_volume_id,
        }
        for e in entries
    ]
    out = rendered_dir / "hcce-static-pvs.yaml"
    out.write_text(template.render(**ctx))
    return out


def render_platform(spec: InstanceSpec, secrets: dict[str, str], private_network_id: str = "0") -> Path:
    inst = instance_dir(spec.id)
    k8s_out = inst / "rendered" / "k8s"
    if k8s_out.exists():
        shutil.rmtree(k8s_out)
    shutil.copytree(TEMPLATES_DIR / "k8s", k8s_out)
    env = _jinja_env()
    ctx = _base_context(spec, secrets)
    ctx["private_network_id"] = private_network_id
    for j2_path in list((TEMPLATES_DIR / "k8s").rglob("*.j2")):
        rel = j2_path.relative_to(TEMPLATES_DIR)
        template = env.get_template(str(rel).replace("\\", "/"))
        out_rel = rel.with_suffix("")
        target = inst / "rendered" / out_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(template.render(**ctx))
        copied_j2 = k8s_out / j2_path.relative_to(TEMPLATES_DIR / "k8s")
        if copied_j2.exists():
            copied_j2.unlink()
    return k8s_out


def render_all(spec: InstanceSpec, secrets: dict[str, str], private_network_id: str = "0") -> None:
    render_terraform(spec, secrets)
    render_hcce(spec, secrets)
    render_static_pvs(spec, secrets)
    render_platform(spec, secrets, private_network_id)


def plan_labels() -> list[dict[str, str]]:
    return [
        {"node": "hcce-master-db", "label_key": "workload-type", "label_value": "database"},
        {"node": "hcce-web-worker", "label_key": "workload-type", "label_value": "web"},
        {"node": "hcce-webrtc-worker", "label_key": "workload-type", "label_value": "webrtc"},
    ]


def plan_svclb_labels() -> list[dict[str, str]]:
    """Pin k3s ServiceLB (svclb) to the master so WebRTC hostPorts 4443/5349 stay free.

    Matches k3s-setup/configure-node-labels.sh — required before applying haproxy-ingress-lb.
    """
    return [
        {"node": "hcce-master-db", "label_key": "svccontroller.k3s.cattle.io/enablelb", "label_value": "true"},
        {"node": "hcce-master-db", "label_key": "svccontroller.k3s.cattle.io/lbpool", "label_value": "master-only"},
    ]
