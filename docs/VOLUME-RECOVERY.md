# Volume recovery and reattach

This installer records which Hetzner Cloud block volumes belong to each instance’s PostgreSQL, Reticulum, and backup PVCs. That mapping survives cluster destroy (when CSI volumes are kept on purpose).

## Files

| File | Purpose |
|------|---------|
| `volumes-inventory.json` | Observed mapping: PVC name → Hetzner volume ID, status, size |
| `spec.json` → `volume_reattach` | Wizard choice: provision new disks or reattach saved ones |
| `rendered/hcce-static-pvs.yaml` | Generated at provision time when reattach is enabled |

`deployment_info.json` is **not** used for volumes — it is deleted on destroy and only holds LB/DNS runtime data.

## PVC roles (fixed in `hcce.yaml.j2`)

| PVC | Role |
|-----|------|
| `pgsql-pvc` | Live PostgreSQL data |
| `ret-pvc` | Reticulum uploads / assets |
| `pgsql-backups-pvc` | CronJob backup dumps (`/backups/`) |

## Normal lifecycle

1. **First provision** — CSI creates Hetzner volumes; pipeline writes `volumes-inventory.json` after HCCE deploy.
2. **Destroy cluster infrastructure** — Terraform servers/network/firewall removed; CSI volumes kept; inventory marked `orphaned` with Hetzner IDs preserved.
3. **Reprovision same instance** — Progress page shows **Recreate cluster** (or open wizard **Review & recreate**). Reattach is enabled by default when saved volumes and secrets exist. Installer applies static PVs with saved `volumeHandle` values before HCCE PVCs.

## Manual recovery (no installer)

If you only have Hetzner Console and `volumes-inventory.json`:

```bash
kubectl apply -f - <<EOF
apiVersion: v1
kind: PersistentVolume
metadata:
  name: reuse-pgsql-pvc
spec:
  capacity:
    storage: 10Gi
  accessModes: [ReadWriteOnce]
  persistentVolumeReclaimPolicy: Retain
  storageClassName: hcloud-volumes-retain
  claimRef:
    namespace: hcce
    name: pgsql-pvc
  csi:
    driver: csi.hetzner.cloud
    volumeHandle: "YOUR_HETZNER_VOLUME_ID"
    fsType: ext4
EOF
```

Then apply HCCE PVCs. Repeat per volume.

## API

- `GET /api/instances/{id}/volumes` — inventory + reattach eligibility
- `POST /api/instances/{id}/sync-volumes` — refresh from live cluster (requires kubeconfig)

## Secrets ZIP

`volumes-inventory.json` is included in the secrets bundle for offline disaster recovery.
