import { useCallback, useEffect, useState } from "react";
import { api, downloadUrl } from "../api";

type AssetsBackupEntry = {
  filename: string;
  size_bytes: number;
  backup_date: string;
  backup_time: string;
  created_at: string;
};

type AssetsBackupsList = {
  available: boolean;
  message: string;
  latest: AssetsBackupEntry | null;
  recent: AssetsBackupEntry[];
  all_count: number;
};

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function formatBackupWhen(entry: AssetsBackupEntry) {
  return `${entry.backup_date} ${entry.backup_time} UTC`;
}

export function AssetsBackupsPanel({ instanceId, enabled }: { instanceId: string; enabled: boolean }) {
  const [data, setData] = useState<AssetsBackupsList | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [creating, setCreating] = useState(false);
  const [createMessage, setCreateMessage] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const res = await api<AssetsBackupsList>(`/instances/${instanceId}/assets-backups`);
      setData(res);
    } catch (e) {
      setData(null);
      setError(e instanceof Error ? e.message : "Could not load asset backups");
    } finally {
      setLoading(false);
    }
  }, [instanceId]);

  useEffect(() => {
    if (!enabled) return;
    void load();
  }, [enabled, load]);

  if (!enabled) return null;

  const createBackupNow = async () => {
    setCreating(true);
    setCreateMessage("");
    setError("");
    try {
      const entry = await api<AssetsBackupEntry>(`/instances/${instanceId}/assets-backups/run`, {
        method: "POST",
      });
      setCreateMessage(`Archive saved — ${formatBackupWhen(entry)} · ${formatBytes(entry.size_bytes)}`);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not create asset backup");
    } finally {
      setCreating(false);
    }
  };

  const latestUrl = data?.latest
    ? downloadUrl(`/instances/${instanceId}/assets-backups/download?latest=true`)
    : null;

  return (
    <section className="panel db-backups-panel" style={{ marginTop: 24 }}>
      <h2 style={{ marginBottom: 8 }}>Reticulum assets</h2>
      <p className="muted" style={{ marginTop: 0 }}>
        Uploads and generated files from the <code>ret-pvc</code> volume (<code>/storage</code> in Reticulum).
        Archives are streamed from the running Reticulum pod and saved under{" "}
        <code>assets-backups/</code> on this machine — no extra sidecar or deployment change.
      </p>

      {loading && !data && <p className="muted">Loading saved archives…</p>}
      {error && <p className="bad-text">{error}</p>}

      {data && (
        <>
          {data.message && !data.latest && <p className="muted">{data.message}</p>}

          <div className="db-backups-actions">
            <button
              type="button"
              className="btn btn-primary"
              disabled={!data.available || creating}
              onClick={() => void createBackupNow()}
            >
              {creating ? "Creating archive…" : "Create assets backup now"}
            </button>
            {latestUrl ? (
              <a href={latestUrl} className="btn btn-outline" style={{ textDecoration: "none" }}>
                Download latest
              </a>
            ) : (
              data.available && (
                <span className="muted">No archives yet — first backup may take a while on large hubs.</span>
              )
            )}
            {data.latest && (
              <span className="muted db-backups-latest-meta">
                Latest: {formatBackupWhen(data.latest)} · {formatBytes(data.latest.size_bytes)}
              </span>
            )}
          </div>

          {createMessage && (
            <p className="ok-text" style={{ marginTop: 12, fontSize: 13 }}>
              {createMessage}
            </p>
          )}

          <div className="db-backups-recent" style={{ marginTop: 16 }}>
            <h3 className="db-backups-subtitle">Saved archives (last 7 days)</h3>
            {data.recent.length === 0 ? (
              <p className="muted" style={{ margin: 0 }}>
                No asset archives saved in the last 7 days.
              </p>
            ) : (
              <table className="data-table db-backups-table">
                <thead>
                  <tr>
                    <th>When (UTC)</th>
                    <th>Size</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {data.recent.map((entry) => (
                    <tr key={entry.filename}>
                      <td>{formatBackupWhen(entry)}</td>
                      <td>{formatBytes(entry.size_bytes)}</td>
                      <td style={{ textAlign: "right" }}>
                        <a
                          href={downloadUrl(
                            `/instances/${instanceId}/assets-backups/download?filename=${encodeURIComponent(entry.filename)}`
                          )}
                          className="btn btn-ghost btn-sm"
                          style={{ textDecoration: "none" }}
                        >
                          Download
                        </a>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <p className="muted db-backups-footnote">
              {data.all_count} archive{data.all_count === 1 ? "" : "s"} stored locally under{" "}
              <code>./data/instances/…/assets-backups/</code>.
            </p>
          </div>
        </>
      )}

      <button
        type="button"
        className="btn btn-outline btn-sm"
        style={{ marginTop: 12 }}
        disabled={loading}
        onClick={() => void load()}
      >
        {loading ? "Refreshing…" : "Refresh list"}
      </button>
    </section>
  );
}
