type ApiHealth = {
  status: string;
  service: string;
};

async function getApiHealth(): Promise<ApiHealth | null> {
  const apiBaseUrl = process.env.API_BASE_URL ?? "http://localhost:8000";
  try {
    const response = await fetch(`${apiBaseUrl}/health`, { cache: "no-store" });
    if (!response.ok) {
      return null;
    }
    return (await response.json()) as ApiHealth;
  } catch {
    return null;
  }
}

export default async function Home() {
  const apiHealth = await getApiHealth();

  return (
    <main className="page-shell">
      <header className="topbar">
        <p className="eyebrow">EDUCATIONAL PAYMENT SIMULATION</p>
        <h1>Payment Orchestrator</h1>
        <p className="subtitle">
          A local transaction state machine for studying reliable payment
          workflows.
        </p>
      </header>
      <section className="status-card" aria-labelledby="foundation-title">
        <div>
          <p className="eyebrow">PHASE 1 FOUNDATION</p>
          <h2 id="foundation-title">Service connectivity</h2>
        </div>
        <p className={apiHealth ? "status status-ready" : "status status-error"}>
          <span aria-hidden="true" />
          {apiHealth
            ? `API ${apiHealth.status}`
            : "API unavailable; check that the backend is running"}
        </p>
      </section>
      <p className="notice">
        Synthetic data only. This project does not process real payments or
        collect payment credentials.
      </p>
    </main>
  );
}
