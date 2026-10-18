import { useEffect, useState } from "react";

type Health = {
  status: string;
  database: { ok: boolean; postgis?: string; error?: string };
};

export default function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/health")
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json() as Promise<Health>;
      })
      .then(setHealth)
      .catch((err: Error) => setError(err.message));
  }, []);

  return (
    <main style={{ fontFamily: "system-ui, sans-serif", padding: "2rem" }}>
      <h1>River Rat</h1>
      <p>Whitewater runnability outlook. Phase 0: plumbing check.</p>
      {error && <p>API unreachable: {error}</p>}
      {!health && !error && <p>Checking API…</p>}
      {health && (
        <p>
          API: {health.status} · Database:{" "}
          {health.database.ok
            ? `connected (PostGIS ${health.database.postgis})`
            : `unavailable (${health.database.error})`}
        </p>
      )}
    </main>
  );
}
