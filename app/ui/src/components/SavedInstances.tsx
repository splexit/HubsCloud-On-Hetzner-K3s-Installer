import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";

export type InstanceSummary = {
  id: string;
  hub_domain: string;
  location: string;
  state: string;
  phase: string;
  message: string;
  created_at: string;
  secrets_available: boolean;
  saved_volumes_count: number;
  reattach_eligible: boolean;
  can_recreate: boolean;
  infrastructure_active: boolean;
  display_status: string;
};

const STATUS_LABELS: Record<string, string> = {
  running: "Running in Hetzner",
  provisioning: "Provisioning…",
  destroying: "Destroying…",
  ready_to_recreate: "Stopped · volumes saved",
  stopped: "Stopped",
  failed: "Failed",
  setup_incomplete: "Setup incomplete",
  unknown: "Unknown",
};

function statusLabel(row: InstanceSummary) {
  return STATUS_LABELS[row.display_status] || row.message || "Unknown";
}

function statusClass(row: InstanceSummary) {
  switch (row.display_status) {
    case "running":
    case "ready_to_recreate":
      return "ok-text";
    case "failed":
      return "bad-text";
    case "provisioning":
    case "destroying":
      return "warn-text";
    default:
      return "muted";
  }
}

function primaryHref(row: InstanceSummary) {
  if (row.display_status === "setup_incomplete") {
    return `/instances/${row.id}/setup`;
  }
  return `/instances/${row.id}`;
}

function primaryLabel(row: InstanceSummary) {
  switch (row.display_status) {
    case "ready_to_recreate":
      return "Recreate";
    case "running":
      return "Open";
    case "provisioning":
    case "destroying":
      return "View progress";
    case "failed":
      return "View progress";
    case "setup_incomplete":
      return "Continue setup";
    default:
      return row.can_recreate ? "Open" : "View";
  }
}

function canDeleteConfig(row: InstanceSummary) {
  return !row.infrastructure_active && row.display_status !== "provisioning" && row.display_status !== "destroying";
}

function InstanceCard({
  row,
  onDeleted,
}: {
  row: InstanceSummary;
  onDeleted: (id: string) => void;
}) {
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState("");

  const deleteConfig = async () => {
    const volumeNote =
      row.saved_volumes_count > 0
        ? `\n\nThis removes the saved mapping for ${row.saved_volumes_count} Hetzner volume(s) from ./data. ` +
          "The volumes themselves keep billing in Hetzner until you delete them in Console. " +
          "Keep your secrets ZIP if you might need those volume IDs later."
        : "";
    if (
      !window.confirm(
        `Delete saved configuration for ${row.hub_domain}?\n\n` +
          "Removes spec, secrets, and logs from this machine only — not Hetzner resources." +
          volumeNote
      )
    ) {
      return;
    }
    setDeleting(true);
    setDeleteError("");
    try {
      const res = await api<{ deleted: boolean; warnings?: string[] }>(
        `/instances/${row.id}/delete-config`,
        { method: "POST", body: JSON.stringify({ confirm: true }) }
      );
      if (res.warnings?.length) {
        window.alert(res.warnings.join("\n\n"));
      }
      onDeleted(row.id);
    } catch (e) {
      setDeleteError(e instanceof Error ? e.message : "Could not delete configuration");
    } finally {
      setDeleting(false);
    }
  };

  return (
    <li className="saved-instance-card">
      <div className="saved-instance-main">
        <Link to={primaryHref(row)} className="saved-instance-domain">
          {row.hub_domain}
        </Link>
        <span className={`saved-instance-status ${statusClass(row)}`}>{statusLabel(row)}</span>
        {row.location && <span className="muted saved-instance-meta">{row.location}</span>}
        {row.saved_volumes_count > 0 && (
          <span className="muted saved-instance-meta">
            {row.saved_volumes_count} saved volume{row.saved_volumes_count === 1 ? "" : "s"}
            {!row.secrets_available && " · secrets missing"}
          </span>
        )}
        {deleteError && <span className="bad-text saved-instance-meta">{deleteError}</span>}
      </div>
      <div className="saved-instance-actions">
        <Link
          to={primaryHref(row)}
          className={`btn ${row.display_status === "ready_to_recreate" ? "btn-primary" : "btn-outline"} btn-sm`}
        >
          {primaryLabel(row)}
        </Link>
        {canDeleteConfig(row) && (
          <button
            type="button"
            className="btn btn-ghost btn-sm bad-text"
            disabled={deleting}
            onClick={() => void deleteConfig()}
          >
            {deleting ? "Deleting…" : "Delete config"}
          </button>
        )}
      </div>
    </li>
  );
}

export function SavedInstances() {
  const [instances, setInstances] = useState<InstanceSummary[]>([]);
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    setLoading(true);
    api<InstanceSummary[]>("/instances")
      .then(setInstances)
      .catch(() => setInstances([]))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (loading) {
    return <p className="muted saved-instances-loading">Loading saved configurations…</p>;
  }

  if (instances.length === 0) return null;

  const recreateCount = instances.filter((i) => i.display_status === "ready_to_recreate").length;

  return (
    <section className="saved-instances" aria-label="Saved configurations">
      <h2 className="saved-instances-heading">
        {recreateCount > 0 ? "Recreate a cluster" : "Saved configurations"}
      </h2>
      <p className="muted saved-instances-intro">
        {recreateCount > 0
          ? "Infrastructure is down but configs and volumes are still on this machine. Open a hub to recreate, or delete configs you no longer need."
          : "Status reflects live Hetzner servers, not stale local state. Delete configs that are no longer needed — Hetzner volumes are not removed automatically."}
      </p>
      <ul className="saved-instances-cards">
        {instances.map((row) => (
          <InstanceCard
            key={row.id}
            row={row}
            onDeleted={(id) => setInstances((prev) => prev.filter((i) => i.id !== id))}
          />
        ))}
      </ul>
      <p className="muted saved-instances-footnote">
        Delete config removes files under <code>./data/instances/&lt;id&gt;</code> only. Destroy cluster infrastructure
        first if Hetzner servers still exist.
      </p>
    </section>
  );
}
