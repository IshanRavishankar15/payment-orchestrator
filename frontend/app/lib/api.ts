export type PaymentState =
  | "CREATED"
  | "PROCESSING"
  | "SUCCEEDED"
  | "FAILED"
  | "UNKNOWN";

export type PaymentIntent = {
  id: string;
  merchant_id: string;
  amount_minor: number;
  currency: string;
  state: PaymentState;
  created_at: string;
  updated_at: string;
};

export type PaymentIntentList = {
  items: PaymentIntent[];
  limit: number;
  offset: number;
};

export type PaymentTimeline = {
  payment: PaymentIntent;
  attempts: {
    id: string;
    provider_name: string;
    attempt_number: number;
    provider_reference: string | null;
    status: string;
    started_at: string;
    finished_at: string | null;
    error_code: string | null;
    error_message: string | null;
    reason: string | null;
  }[];
  transitions: {
    id: string;
    from_state: PaymentState | null;
    to_state: PaymentState;
    reason: string;
    attempt_id: string | null;
    provider_event_row_id: string | null;
    occurred_at: string;
  }[];
  provider_events: {
    id: string;
    provider_name: string;
    provider_event_id: string;
    event_type: string;
    signature_valid: boolean;
    received_at: string;
    processed_at: string | null;
    processing_result: string | null;
  }[];
  reconciliation_jobs: {
    id: string;
    attempt_id: string | null;
    status: string;
    retry_count: number;
    max_retries: number;
    available_at: string;
    lease_expires_at: string | null;
    last_error: string | null;
    created_at: string;
    updated_at: string;
  }[];
};

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    path: string,
  ) {
    super(`Backend request ${path} failed with HTTP ${status}.`);
    this.name = "ApiError";
  }
}

export async function fetchApi<T>(path: string): Promise<T> {
  const apiBaseUrl = process.env.API_BASE_URL ?? "http://localhost:8000";
  const response = await fetch(`${apiBaseUrl}${path}`, { cache: "no-store" });
  if (!response.ok) {
    throw new ApiError(response.status, path);
  }
  return (await response.json()) as T;
}
