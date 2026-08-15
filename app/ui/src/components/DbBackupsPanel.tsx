import { useCallback, useEffect, useState } from "react";
import { api, downloadUrl } from "../api";

type DbBackupEntry = {
  filename: string;
  size_bytes: number;
  backup_date: string;
  backup_time: string;
  created_at: string;
};

type DbBackupsList = {
  available: boolean;
  message: string;
  keep_days: number;
  latest: DbBackupEntry | null;
  recent: DbBackupEntry[];
  all_count: number;
};

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatBackupWhen(entry: DbBackupEntry) {
  return `${entry.backup_date} ${entry.backup_time} UTC`;
}

export function DbBackupsPanel({ instanceId, enabled }: { instanceId: string; enabled: boolean }) {
  const [data, setData] = useState<DbBackupsList | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [dateQuery, setDateQuery] = useState("");
  const [dateError, setDateError] = useState("");
  const [savingBundle, setSavingBundle] = useState(false);
  const [bundleMessage, setBundleMessage] = useState("");
  const [creatingBackup, setCreatingBackup] = useState(false);
  const [createMessage, setCreateMessage] = useState("");

  const load = useCallback(async () => {
    if (!enabled) return;
    setLoading(true);
    setError("");
    try {
      const res = await api<DbBackupsList>(`/instances/${instanceId}/db-backups`);
      setData(res);
    } catch (e) {
      setData(null);
      setError(e instanceof Error ? e.message : "Could not load backups");
    } finally {
      setLoading(false);
    }
  }, [enabled, instanceId]);

  useEffect(() => {
    void load();
    if (!enabled) return;
    const t = setInterval(() => void load(), 60000);
    return () => clearInterval(t);
  }, [load, enabled]);

  if (!enabled) return null;

  const latestUrl = downloadUrl(`/instances/${instanceId}/db-backups/download?latest=true`);
  const dateDownloadUrl = dateQuery.match(/^\d{4}-\d{2}-\d{2}$/)
    ? downloadUrl(`/instances/${instanceId}/db-backups/download?date=${encodeURIComponent(dateQuery)}`)
    : null;

  const saveRestoreBundle = async (body: Record<string, unknown>) => {
    setSavingBundle(true);
    setBundleMessage("");
    try {
      await api(`/instances/${instanceId}/restore-bundle`, {
        method: "POST",
        body: JSON.stringify(body),
      });
      setBundleMessage("Restore bundle saved locally — secrets and dump are kept together for recreate.");
    } catch (e) {
      setBundleMessage(e instanceof Error ? e.message : "Could not save restore bundle");
    } finally {
      setSavingBundle(false);
    }
  };

  const createBackupNow = async () => {
    setCreatingBackup(true);
    setCreateMessage("");
    setError("");
    try {
      const entry = await api<DbBackupEntry>(`/instances/${instanceId}/db-backups/run`, {
        method: "POST",
      });
      setCreateMessage(`Backup created — ${formatBackupWhen(entry)} · ${formatBytes(entry.size_bytes)}`);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not create backup");
    } finally {
      setCreatingBackup(false);
    }
  };

  return (
    <section className="panel db-backups-panel">
      <h2 style={{ marginBottom: 8 }}>Database backups</h2>
      <p className="muted" style={{ marginTop: 0 }}>
        Nightly PostgreSQL dumps from the <code>pgsql-backup</code> CronJob, stored on the backup block volume.
        Download copies for offline storage, or save a full restore bundle (secrets + dump + spec) for recreate
        without Hetzner volumes.
      </p>

      {loading && !data && <p className="muted">Loading backups from cluster…</p>}
      {error && <p className="bad-text">{error}</p>}

      {data && !data.available && (
        <p className="muted">{data.message || "Backups unavailable until the cluster is running."}</p>
      )}

      {data?.available && data.message && !data.latest && (
        <p className="muted">{data.message}</p>
      )}

      {data?.available && (
        <>
          <div className="db-backups-actions">
            <button
              type="button"
              className="btn btn-primary"
              disabled={creatingBackup || savingBundle}
              onClick={() => void createBackupNow()}
            >
              {creatingBackup ? "Creating backup…" : "Create backup now"}
            </button>
            {data.latest ? (
              <>
                <a href={latestUrl} className="btn btn-outline" style={{ textDecoration: "none" }}>
                  Download latest
                </a>
                <button
                  type="button"
                  className="btn btn-outline"
                  disabled={savingBundle || creatingBackup}
                  onClick={() => void saveRestoreBundle({ latest: true })}
                >
                  {savingBundle ? "Saving bundle…" : "Save restore bundle (latest)"}
                </button>
              </>
            ) : (
              <span className="muted">No backups on volume yet — create one or wait for 04:00 UTC CronJob.</span>
            )}
            {data.latest && (
              <span className="muted db-backups-latest-meta">
                Latest: {formatBackupWhen(data.latest)} · {formatBytes(data.latest.size_bytes)}
              </span>
            )}
          </div>

          <div className="db-backups-by-date">
            <label htmlFor="db-backup-date" className="db-backups-date-label">
              Download by date
            </label>
            <div className="db-backups-date-row">
              <input
                id="db-backup-date"
                type="date"
                value={dateQuery}
                onChange={(e) => {
                  setDateQuery(e.target.value);
                  setDateError("");
                }}
              />
              {dateDownloadUrl ? (
                <a href={dateDownloadUrl} className="btn btn-outline btn-sm" style={{ textDecoration: "none" }}>
                  Download
                </a>
              ) : (
                <button
                  type="button"
                  className="btn btn-outline btn-sm"
                  disabled={!dateQuery}
                  onClick={() => setDateError("Enter a date as YYYY-MM-DD")}
                >
                  Download
                </button>
              )}
            </div>
            {dateError && <p className="bad-text" style={{ margin: "8px 0 0", fontSize: 13 }}>{dateError}</p>}
          </div>

          <div className="db-backups-recent">
            <h3 className="db-backups-subtitle">Last 7 days</h3>
            {data.recent.length === 0 ? (
              <p className="muted" style={{ margin: 0 }}>
                No backups in the last 7 days yet.
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
                            `/instances/${instanceId}/db-backups/download?filename=${encodeURIComponent(entry.filename)}`
                          )}
                          className="btn btn-ghost btn-sm"
                          style={{ textDecoration: "none" }}
                        >
                          Download
                        </a>
                        <button
                          type="button"
                          className="btn btn-ghost btn-sm"
                          disabled={savingBundle}
                          onClick={() => void saveRestoreBundle({ latest: false, backup_filename: entry.filename })}
                        >
                          Save bundle
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <p className="muted db-backups-footnote">
              CronJob retains {data.keep_days} days on the cluster ({data.all_count} file
              {data.all_count === 1 ? "" : "s"} total on volume).
            </p>
          </div>
        </>
      )}

      {createMessage && (
        <p className="ok-text" style={{ marginTop: 12, fontSize: 13 }}>
          {createMessage}
        </p>
      )}

      {bundleMessage && (
        <p className={bundleMessage.startsWith("Restore bundle saved") ? "ok-text" : "bad-text"} style={{ marginTop: 12, fontSize: 13 }}>
          {bundleMessage}
        </p>
      )}

      <button type="button" className="btn btn-outline btn-sm" style={{ marginTop: 12 }} disabled={loading} onClick={() => void load()}>
        {loading ? "Refreshing…" : "Refresh list"}
      </button>
    </section>
  );
}
