from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from services.core_images import normalize_core_app_images
from services.volumes import (
    DEFAULT_PGSQL_BACKUP_VOLUME,
    DEFAULT_PGSQL_VOLUME,
    DEFAULT_RETICULUM_VOLUME,
    normalize_backup_volume,
    normalize_data_volume,
)


class ClusterSize(str, Enum):
    small = "small"
    medium = "medium"
    large = "large"


# Resource guidance only — does not map to Hetzner type names (those vary by location).
CLUSTER_SIZE_RECOMMENDATIONS: dict[str, dict[str, dict[str, int]]] = {
    "small": {
        "master": {"cores": 2, "memory_gb": 4},
        "web": {"cores": 1, "memory_gb": 2},
        "webrtc": {"cores": 1, "memory_gb": 2},
    },
    "medium": {
        "master": {"cores": 2, "memory_gb": 4},
        "web": {"cores": 1, "memory_gb": 2},
        "webrtc": {"cores": 2, "memory_gb": 4},
    },
    "large": {
        "master": {"cores": 4, "memory_gb": 8},
        "web": {"cores": 2, "memory_gb": 4},
        "webrtc": {"cores": 2, "memory_gb": 4},
    },
}

ALLOWED_LOCATIONS = {"nbg1", "fsn1", "hel1", "ash", "hil"}  # fallback when API unavailable

HCCE_NAMESPACE = "hcce"

# PVC names are fixed in hcce.yaml.j2 — used for inventory and reattach.
KNOWN_VOLUME_PVCS: dict[str, dict[str, str]] = {
    "pgsql-pvc": {"role": "postgresql", "size_field": "pgsql_volume_size"},
    "ret-pvc": {"role": "reticulum", "size_field": "reticulum_volume_size"},
    "pgsql-backups-pvc": {"role": "postgresql-backup", "size_field": "pgsql_backup_volume_size"},
}


class VolumeInventoryEntry(BaseModel):
    pvc_name: str
    namespace: str = HCCE_NAMESPACE
    role: str = ""
    size: str = "10Gi"
    pvc_uid: str | None = None
    pv_name: str | None = None
    hetzner_volume_id: int | None = None
    hetzner_volume_name: str | None = None
    status: str = "unknown"  # bound | pending | orphaned | missing | available
    bound_at: str | None = None


class VolumeInventory(BaseModel):
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    location: str = ""
    volumes: list[VolumeInventoryEntry] = Field(default_factory=list)


class VolumeReattachPolicy(BaseModel):
    mode: Literal["provision_new", "reattach_saved"] = "provision_new"
    # Per-PVC override; omitted keys default to True when mode=reattach_saved and inventory has an ID.
    volumes: dict[str, bool] = Field(default_factory=dict)

    def wants_reattach(self, pvc_name: str, *, has_saved_id: bool) -> bool:
        if self.mode != "reattach_saved" or not has_saved_id:
            return False
        if pvc_name in self.volumes:
            return self.volumes[pvc_name]
        return True


class VolumeContextEntry(BaseModel):
    pvc_name: str
    role: str
    size: str
    hetzner_volume_id: int | None = None
    hetzner_volume_name: str | None = None
    status: str
    in_hetzner: bool = False
    in_inventory: bool = False
    selected_for_reattach: bool = False
    reattach_eligible: bool = False


class VolumesContext(BaseModel):
    inventory: VolumeInventory
    reattach_eligible: bool = False
    secrets_available: bool = False
    entries: list[VolumeContextEntry] = Field(default_factory=list)


class InstanceSpec(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    hetzner_api_token: str = ""
    hub_domain: str = ""
    admin_email: str = ""
    location: str = ""
    cluster_size: ClusterSize = ClusterSize.medium
    master_server_type: str | None = None
    worker_server_type: str | None = None
    webrtc_server_type: str | None = None
    ssh_public_key: str = ""
    ssh_private_key_pem: str | None = None
    ssh_key_generated: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    sketchfab_api_key: str = ""
    tenor_api_key: str = ""
    pgsql_volume_size: str = DEFAULT_PGSQL_VOLUME
    reticulum_volume_size: str = DEFAULT_RETICULUM_VOLUME
    pgsql_backup_volume_size: str = DEFAULT_PGSQL_BACKUP_VOLUME
    volume_reattach: VolumeReattachPolicy = Field(default_factory=VolumeReattachPolicy)
    firewall_hardened: bool = False
    firewall_allow_ssh: bool = True
    auto_repair_cluster_join: bool = True
    persistent_volume_size: str = DEFAULT_PGSQL_VOLUME  # legacy alias for older saved specs
    core_app_images: dict[str, dict[str, str]] = Field(default_factory=dict)
    image_overrides: dict[str, str] = Field(default_factory=dict)

    @field_validator("core_app_images", mode="before")
    @classmethod
    def normalize_core_app_images_field(cls, v: object) -> dict[str, dict[str, str]]:
        if not v:
            return normalize_core_app_images(None)
        if isinstance(v, dict):
            return normalize_core_app_images(v)
        return normalize_core_app_images(None)

    @field_validator("location")
    @classmethod
    def validate_location(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if not v:
            return ""
        if len(v) > 32:
            raise ValueError("location must be a valid Hetzner location code")
        return v

    @field_validator("smtp_port")
    @classmethod
    def validate_port(cls, v: int) -> int:
        if not 1 <= v <= 65535:
            raise ValueError("smtp_port must be between 1 and 65535")
        return v

    @field_validator("pgsql_volume_size")
    @classmethod
    def validate_pgsql_volume(cls, v: str) -> str:
        return normalize_data_volume(v)

    @field_validator("reticulum_volume_size")
    @classmethod
    def validate_reticulum_volume(cls, v: str) -> str:
        return normalize_data_volume(v, default=DEFAULT_RETICULUM_VOLUME)

    @field_validator("pgsql_backup_volume_size")
    @classmethod
    def validate_pgsql_backup_volume(cls, v: str) -> str:
        return normalize_backup_volume(v)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_volume_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        legacy = data.get("persistent_volume_size")
        if legacy:
            if not data.get("pgsql_volume_size"):
                data["pgsql_volume_size"] = legacy
            if not data.get("pgsql_backup_volume_size"):
                data["pgsql_backup_volume_size"] = legacy
        if not data.get("reticulum_volume_size"):
            data["reticulum_volume_size"] = DEFAULT_RETICULUM_VOLUME
        return data

    def resolved_server_types(self) -> dict[str, str]:
        return {
            "master_server_type": (self.master_server_type or "").strip(),
            "webrtc_server_type": (self.webrtc_server_type or "").strip(),
            "web_server_type": (self.worker_server_type or "").strip(),
        }


class InstanceStatus(BaseModel):
    phase: str = "idle"
    step: int = 0
    state: str = "pending"
    message: str = ""
    error: str | None = None
    intervention: str | None = None  # e.g. server_types, credentials
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class InstanceSummary(BaseModel):
    id: str
    hub_domain: str
    location: str = ""
    state: str
    phase: str
    message: str = ""
    created_at: str
    secrets_available: bool = False
    saved_volumes_count: int = 0
    reattach_eligible: bool = False
    can_recreate: bool = False
    infrastructure_active: bool = False
    display_status: str = "unknown"


class ResourcesSnapshot(BaseModel):
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    pods: list[dict[str, Any]] = Field(default_factory=list)
    deployments: list[dict[str, Any]] = Field(default_factory=list)
    ingresses: list[dict[str, Any]] = Field(default_factory=list)
    certificates: list[dict[str, Any]] = Field(default_factory=list)
    loadbalancers: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None


class HealthCheck(BaseModel):
    id: str
    name: str
    group: str
    status: str  # ok | warn | fail | skip | unknown
    detail: str
    hint: str = ""


class HealthSummary(BaseModel):
    overall: str
    checks: list[HealthCheck] = Field(default_factory=list)


class ClusterJoinServer(BaseModel):
    name: str
    role: str
    public_ip: str = ""
    private_ip: str = ""
    hetzner_status: str = ""
    k8s_ready: bool = False
    k8s_present: bool = False
    issue: str | None = None
    bootstrap_stage: str | None = None
    bootstrap_log: str | None = None


class ClusterJoinStatus(BaseModel):
    expected_nodes: int = 3
    joined_ready: int = 0
    joined_not_ready: int = 0
    missing: list[str] = Field(default_factory=list)
    stuck_seconds: int = 0
    servers: list[ClusterJoinServer] = Field(default_factory=list)
    suggested_action: str = "wait"  # wait | repair | none
    error: str | None = None


class HetznerAudit(BaseModel):
    clean: bool
    issues: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    has_billable_leftovers: bool = False
    servers: list[dict] = Field(default_factory=list)
    networks: list[dict] = Field(default_factory=list)
    firewalls: list[dict] = Field(default_factory=list)
    load_balancers: list[dict] = Field(default_factory=list)
    volumes: list[int] = Field(default_factory=list)
    placement_groups: list[dict] = Field(default_factory=list)
    ssh_keys: list[dict] = Field(default_factory=list)
