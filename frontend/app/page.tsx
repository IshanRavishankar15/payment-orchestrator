import Link from "next/link";
import {
  ApiError,
  fetchApi,
  type PaymentIntentList,
  type PaymentState,
  type PaymentTimeline,
} from "./lib/api";

const states: PaymentState[] = [
  "CREATED",
  "PROCESSING",
  "SUCCEEDED",
  "FAILED",
  "UNKNOWN",
];
const pageSize = 25;

type SearchParams = {
  state?: string;
  merchant_id?: string;
  offset?: string;
  payment_id?: string;
};

function isPaymentState(value: string | undefined): value is PaymentState {
  return value !== undefined && states.includes(value as PaymentState);
}

function formatAmount(amountMinor: number, currency: string): string {
  try {
    return new Intl.NumberFormat("en-IN", {
      style: "currency",
      currency,
    }).format(amountMinor / 100);
  } catch (error) {
    if (error instanceof RangeError) {
      return `${currency} ${(amountMinor / 100).toFixed(2)}`;
    }
    throw error;
  }
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat("en-IN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function statusClass(value: string): string {
  return `state-pill state-${value.toLowerCase()}`;
}

function dashboardQuery(
  state: PaymentState | undefined,
  merchantId: string,
  offset: number,
  paymentId?: string,
): string {
  const query = new URLSearchParams();
  if (state) query.set("state", state);
  if (merchantId) query.set("merchant_id", merchantId);
  if (offset > 0) query.set("offset", String(offset));
  if (paymentId) query.set("payment_id", paymentId);
  const serialized = query.toString();
  return serialized ? `/?${serialized}` : "/";
}

async function loadList(path: string): Promise<{
  data: PaymentIntentList | null;
  error: string | null;
}> {
  try {
    return { data: await fetchApi<PaymentIntentList>(path), error: null };
  } catch (error) {
    if (error instanceof ApiError || error instanceof TypeError) {
      return {
        data: null,
        error:
          error instanceof ApiError
            ? error.message
            : "The backend API is unavailable. Check that the services are running.",
      };
    }
    throw error;
  }
}

async function loadTimeline(paymentId: string): Promise<{
  data: PaymentTimeline | null;
  error: string | null;
}> {
  try {
    return {
      data: await fetchApi<PaymentTimeline>(
        `/payments/${encodeURIComponent(paymentId)}/timeline`,
      ),
      error: null,
    };
  } catch (error) {
    if (error instanceof ApiError || error instanceof TypeError) {
      return {
        data: null,
        error:
          error instanceof ApiError && error.status === 404
            ? "This payment could not be found."
            : error instanceof ApiError
              ? error.message
              : "The payment timeline could not be loaded from the backend.",
      };
    }
    throw error;
  }
}

function StatePill({ state }: { state: string }) {
  return <span className={statusClass(state)}>{state}</span>;
}

export default async function Home({
  searchParams,
}: {
  searchParams: Promise<SearchParams>;
}) {
  const params = await searchParams;
  const state = isPaymentState(params.state) ? params.state : undefined;
  const merchantId = params.merchant_id?.trim().slice(0, 100) ?? "";
  const requestedOffset = Number(params.offset ?? 0);
  const offset =
    Number.isSafeInteger(requestedOffset) && requestedOffset >= 0
      ? requestedOffset
      : 0;
  const listQuery = new URLSearchParams({
    limit: String(pageSize),
    offset: String(offset),
  });
  if (state) listQuery.set("state", state);
  if (merchantId) listQuery.set("merchant_id", merchantId);

  const listResult = await loadList(`/payments?${listQuery.toString()}`);
  const selectedPaymentId =
    params.payment_id ?? listResult.data?.items[0]?.id ?? "";
  const timelineResult = selectedPaymentId
    ? await loadTimeline(selectedPaymentId)
    : { data: null, error: null };
  const items = listResult.data?.items ?? [];
  const stateCounts = states.reduce<Record<PaymentState, number>>(
    (counts, paymentState) => {
      counts[paymentState] = items.filter(
        (payment) => payment.state === paymentState,
      ).length;
      return counts;
    },
    {
      CREATED: 0,
      PROCESSING: 0,
      SUCCEEDED: 0,
      FAILED: 0,
      UNKNOWN: 0,
    },
  );
  const timeline = timelineResult.data;

  return (
    <main className="app-shell">
      <aside className="sidebar">
        <Link className="brand" href="/">
          <span className="brand-mark" aria-hidden="true">
            P
          </span>
          <span>
            <strong>Flowstate</strong>
            <small>PAYMENT OPS</small>
          </span>
        </Link>
        <p className="nav-label">WORKSPACE</p>
        <Link className="nav-link nav-link-active" href="/">
          <span aria-hidden="true">▦</span> Transactions
        </Link>
        <div className="sidebar-note">
          <span className="live-indicator" />
          <div>
            <strong>Local simulation</strong>
            <small>Synthetic data only</small>
          </div>
        </div>
        <p className="sidebar-footer">No real payments or credentials</p>
      </aside>

      <section className="main-content">
        <header className="topbar">
          <div className="breadcrumb">Workspace <span>/</span> Transactions</div>
          <div className="topbar-status">
            <span
              className={
                listResult.data
                  ? "live-indicator"
                  : "live-indicator live-indicator-offline"
              }
            />
            {listResult.data ? "Live backend data" : "Backend unavailable"}
          </div>
        </header>

        <div className="page-heading">
          <div>
            <p className="eyebrow">PAYMENT OPERATIONS</p>
            <h1>Transactions</h1>
            <p className="subtitle">
              Monitor payment state, provider attempts, and recovery activity.
            </p>
          </div>
          <div className="data-source">
            <span className="source-icon" aria-hidden="true">↗</span>
            <span><strong>PostgreSQL</strong><small>Authoritative data</small></span>
          </div>
        </div>

        <section className="metrics-grid" aria-label="Payment state counts">
          <article className="metric-card">
            <span className="metric-label">Visible payments</span>
            <strong>{items.length}</strong>
            <small>Current filtered page</small>
          </article>
          <article className="metric-card metric-created">
            <span className="metric-label">Queued</span>
            <strong>{stateCounts.CREATED}</strong>
            <small>Awaiting execution</small>
          </article>
          <article className="metric-card metric-processing">
            <span className="metric-label">Processing</span>
            <strong>{stateCounts.PROCESSING}</strong>
            <small>Provider decision pending</small>
          </article>
          <article className="metric-card metric-success">
            <span className="metric-label">Succeeded</span>
            <strong>{stateCounts.SUCCEEDED}</strong>
            <small>Confirmed outcomes</small>
          </article>
          <article className="metric-card metric-attention">
            <span className="metric-label">Needs attention</span>
            <strong>{stateCounts.UNKNOWN + stateCounts.FAILED}</strong>
            <small>Unknown or failed</small>
          </article>
        </section>

        <section className="panel filter-panel" aria-label="Payment filters">
          <form action="/" method="get" className="filter-form">
            <label>
              <span>Merchant ID</span>
              <input
                type="search"
                name="merchant_id"
                placeholder="Filter by merchant"
                defaultValue={merchantId}
                maxLength={100}
              />
            </label>
            <label>
              <span>Payment state</span>
              <select name="state" defaultValue={state ?? ""}>
                <option value="">All states</option>
                {states.map((paymentState) => (
                  <option value={paymentState} key={paymentState}>
                    {paymentState}
                  </option>
                ))}
              </select>
            </label>
            <input type="hidden" name="offset" value="0" />
            <button type="submit" className="button button-primary">
              Apply filters
            </button>
            <Link className="clear-link" href="/">
              Clear
            </Link>
          </form>
        </section>

        {listResult.error ? (
          <div className="error-banner" role="alert">
            <strong>Could not load transactions</strong>
            <span>{listResult.error}</span>
          </div>
        ) : null}

        <div className="workspace-grid">
          <section className="panel transactions-panel">
            <div className="panel-heading">
              <div>
                <h2>Recent payments</h2>
                <p>
                  {items.length
                    ? `${items.length} result${items.length === 1 ? "" : "s"} on this page`
                    : "No payments match these filters"}
                </p>
              </div>
              <span className="table-count">
                {String(offset + (items.length ? 1 : 0)).padStart(2, "0")}–
                {String(offset + items.length).padStart(2, "0")}
              </span>
            </div>
            {items.length ? (
              <>
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>Payment</th>
                        <th>Merchant</th>
                        <th>Amount</th>
                        <th>State</th>
                        <th>Updated</th>
                      </tr>
                    </thead>
                    <tbody>
                      {items.map((payment) => (
                        <tr
                          key={payment.id}
                          className={
                            selectedPaymentId === payment.id ? "row-selected" : ""
                          }
                        >
                          <td>
                            <Link
                              className="payment-link"
                              href={dashboardQuery(
                                state,
                                merchantId,
                                offset,
                                payment.id,
                              )}
                              title={payment.id}
                            >
                              <span className="payment-dot" />
                              {payment.id.slice(0, 8)}
                            </Link>
                          </td>
                          <td className="merchant-cell">{payment.merchant_id}</td>
                          <td className="amount-cell">
                            {formatAmount(payment.amount_minor, payment.currency)}
                          </td>
                          <td><StatePill state={payment.state} /></td>
                          <td className="date-cell">{formatDate(payment.updated_at)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <nav className="pagination" aria-label="Payment pages">
                  <Link
                    className={offset === 0 ? "page-link disabled" : "page-link"}
                    aria-disabled={offset === 0}
                    href={dashboardQuery(
                      state,
                      merchantId,
                      Math.max(0, offset - pageSize),
                    )}
                  >
                    ← Previous
                  </Link>
                  <span>Page {Math.floor(offset / pageSize) + 1}</span>
                  <Link
                    className={
                      items.length < pageSize ? "page-link disabled" : "page-link"
                    }
                    aria-disabled={items.length < pageSize}
                    href={dashboardQuery(state, merchantId, offset + pageSize)}
                  >
                    Next →
                  </Link>
                </nav>
              </>
            ) : (
              <div className="empty-state">
                <span className="empty-icon" aria-hidden="true">⌕</span>
                <strong>No payments found</strong>
                <p>Try clearing a filter or create a synthetic payment through the API.</p>
              </div>
            )}
          </section>

          <section className="panel detail-panel" aria-labelledby="detail-heading">
            {timelineResult.error ? (
              <div className="detail-error" role="alert">
                <h2 id="detail-heading">Payment details unavailable</h2>
                <p>{timelineResult.error}</p>
              </div>
            ) : timeline ? (
              <>
                <div className="detail-header">
                  <div>
                    <p className="eyebrow">PAYMENT DETAIL</p>
                    <h2 id="detail-heading">Transaction timeline</h2>
                  </div>
                  <StatePill state={timeline.payment.state} />
                </div>
                <div className="detail-amount">
                  <strong>
                    {formatAmount(
                      timeline.payment.amount_minor,
                      timeline.payment.currency,
                    )}
                  </strong>
                  <span title={timeline.payment.id}>{timeline.payment.id}</span>
                </div>
                <dl className="detail-meta">
                  <div><dt>Merchant</dt><dd>{timeline.payment.merchant_id}</dd></div>
                  <div><dt>Created</dt><dd>{formatDate(timeline.payment.created_at)}</dd></div>
                </dl>

                <div className="detail-section">
                  <div className="section-title">
                    <h3>State history</h3>
                    <span>{timeline.transitions.length}</span>
                  </div>
                  {timeline.transitions.length ? (
                    <ol className="timeline">
                      {timeline.transitions.map((transition) => (
                        <li key={transition.id}>
                          <span className={`timeline-marker marker-${transition.to_state.toLowerCase()}`} />
                          <div className="timeline-content">
                            <div className="timeline-state">
                              <strong>{transition.to_state}</strong>
                              <time>{formatDate(transition.occurred_at)}</time>
                            </div>
                            <p>{transition.reason}</p>
                            {transition.from_state ? (
                              <small>{transition.from_state} → {transition.to_state}</small>
                            ) : <small>Initial state</small>}
                          </div>
                        </li>
                      ))}
                    </ol>
                  ) : <p className="muted-copy">No state transitions recorded.</p>}
                </div>

                <div className="detail-section">
                  <div className="section-title">
                    <h3>Provider attempts</h3>
                    <span>{timeline.attempts.length}</span>
                  </div>
                  {timeline.attempts.length ? (
                    <div className="activity-list">
                      {timeline.attempts.map((attempt) => (
                        <article className="activity-card" key={attempt.id}>
                          <div className="activity-heading">
                            <strong>{attempt.provider_name}</strong>
                            <StatePill state={attempt.status} />
                          </div>
                          <p>Attempt {attempt.attempt_number} · {attempt.provider_reference ?? "No reference"}</p>
                          <small>{attempt.reason ?? "No routing reason recorded"}</small>
                          {attempt.error_message ? (
                            <p className="error-copy">{attempt.error_message}</p>
                          ) : null}
                        </article>
                      ))}
                    </div>
                  ) : <p className="muted-copy">No provider attempt has started.</p>}
                </div>

                <div className="detail-section">
                  <div className="section-title">
                    <h3>Provider events</h3>
                    <span>{timeline.provider_events.length}</span>
                  </div>
                  {timeline.provider_events.length ? (
                    <div className="activity-list">
                      {timeline.provider_events.map((event) => (
                        <article className="activity-card" key={event.id}>
                          <div className="activity-heading">
                            <strong>{event.event_type.replaceAll("_", " ")}</strong>
                            <span className="event-result">{event.processing_result ?? "Received"}</span>
                          </div>
                          <p>{event.provider_name} · {event.provider_event_id}</p>
                          <small>
                            {event.signature_valid ? "HMAC verified" : "Signature invalid"} · {formatDate(event.received_at)}
                          </small>
                        </article>
                      ))}
                    </div>
                  ) : <p className="muted-copy">No provider events received.</p>}
                </div>

                <div className="detail-section">
                  <div className="section-title">
                    <h3>Reconciliation</h3>
                    <span>{timeline.reconciliation_jobs.length}</span>
                  </div>
                  {timeline.reconciliation_jobs.length ? (
                    <div className="activity-list">
                      {timeline.reconciliation_jobs.map((job) => (
                        <article className="activity-card" key={job.id}>
                          <div className="activity-heading">
                            <strong>{job.status}</strong>
                            <span className="retry-count">
                              {job.retry_count}/{job.max_retries} retries
                            </span>
                          </div>
                          <p>Next check {formatDate(job.available_at)}</p>
                          {job.last_error ? <small className="error-copy">{job.last_error}</small> : null}
                        </article>
                      ))}
                    </div>
                  ) : <p className="muted-copy">No reconciliation work scheduled.</p>}
                </div>
              </>
            ) : (
              <div className="empty-state detail-empty">
                <span className="empty-icon" aria-hidden="true">↖</span>
                <strong>Select a payment</strong>
                <p>Choose a transaction to inspect its attempts and timeline.</p>
              </div>
            )}
          </section>
        </div>
        <footer className="footer-note">
          Educational payment simulation · Synthetic data only · No payment credentials collected
        </footer>
      </section>
    </main>
  );
}
