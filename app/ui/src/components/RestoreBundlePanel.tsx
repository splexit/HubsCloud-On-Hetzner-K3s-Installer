import { useCallback, useEffect, useState } from "react";
import { api, downloadUrl } from "../api";

type RestoreBundleManifest = {
  instance_id: string;
  hub_domain: string;
  created_at: string;
  db_backup_filename: string;
  db_backup_date: string;
  db_backup_time: string;
  db_backup_size_bytes: number;
  includes_reticulum_assets: boolean;
};

type RestoreBundleStatus = {
  present: boolean;
  manifest: RestoreBundleManifest | null;
  message: string;
};

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function RestoreBundlePanel({ instanceId }: { instanceId: string }) {
  const [status, setStatus] = useState<RestoreBundleStatus | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await api<RestoreBundleStatus>(`/instances/${instanceId}/restore-bundle`);
      setStatus(res);
    } catch {
      setStatus(null);
    } finally {
      setLoading(false);
    }
  }, [instanceId]);

  useEffect(() => {
    void load();
  }, [load]);

  if (loading && !status) {
    return (
      <section className="panel" style={{ marginTop: 24 }}>
        <p className="muted">Loading restore bundle…</p>
      </section>
    );
  }

  if (!status?.present || !status.manifest) return null;

  const m = status.manifest;
  const zipUrl = downloadUrl(`/instances/${instanceId}/restore-bundle.zip`);

  return (
    <section className="panel restore-bundle-panel" style={{ marginTop: 24 }}>
      <h2 style={{ marginBottom: 8 }}>Local restore bundle</h2>
      <p className="muted" style={{ marginTop: 0 }}>
        Secrets, installer spec, SSH keys, and a PostgreSQL dump are saved under{" "}
        <code>restore-bundle/</code>. Use this when Hetzner block volumes are gone — recreate with new volumes
        and enable database restore on the wizard review step.
      </p>
      <ul className="muted" style={{ fontSize: 13, paddingLeft: 18 }}>
        <li>
          Dump: {m.db_backup_filename} ({m.db_backup_date} {m.db_backup_time} UTC ·{" "}
          {formatBytes(m.db_backup_size_bytes)})
        </li>
        <li>Saved: {new Date(m.created_at).toLocaleString()}</li>
        <li>Reticulum assets: not included yet — reattach <code>ret-pvc</code> when possible</li>
      </ul>
      <div style={{ display: "flex", gap: 8, marginTop: 12, flexWrap: "wrap" }}>
        <a href={zipUrl} className="btn btn-outline btn-sm" style={{ textDecoration: "none" }}>
          Download ZIP
        </a>
        <button type="button" className="btn btn-ghost btn-sm" disabled={loading} onClick={() => void load()}>
          Refresh
        </button>
      </div>
    </section>
  );
}
