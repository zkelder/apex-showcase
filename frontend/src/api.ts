export type ApiEnvelope<T> = { schema_version: string; capability: string; data: T };
export type RecordValue = string | number | boolean | null | string[] | Record<string, unknown> | RecordValue[];
export type ApiRecord = Record<string, RecordValue | undefined>;

export type Campaign = ApiRecord & {
  campaign_id: string;
  name: string;
  operating_mode: "SHADOW" | "PAPER";
  state: string;
  contribution: string;
  authorized_loss: string;
  starts_at: string;
  expires_at: string;
};

export type Health = { service_state: string; database_ready: boolean };

export class ApiError extends Error {
  constructor(
    message: string,
    readonly code = "REQUEST_REJECTED",
    readonly status = 0,
  ) {
    super(message);
  }
}

function idempotencyKey(): string {
  return crypto.randomUUID();
}

export function canonicalDecimal(value: string): boolean {
  return /^(?:0|[1-9]\d*)(?:\.\d+)?$/.test(value);
}

export function formatTimestamp(value: unknown): string {
  if (typeof value !== "string") return "—";
  const time = new Date(value);
  return Number.isNaN(time.valueOf()) ? value : time.toLocaleString();
}

export function asItems(value: unknown): ApiRecord[] {
  if (!value || typeof value !== "object" || !Array.isArray((value as { items?: unknown }).items)) return [];
  return (value as { items: unknown[] }).items.filter(
    (item): item is ApiRecord => Boolean(item) && typeof item === "object",
  );
}

export const DEMO_MODE = import.meta.env.VITE_APEX_DEMO === "1";

type DemoManifest = { files: Record<string, string> };
let demoManifest: Promise<DemoManifest> | null = null;

function demoBase(): string {
  return `${import.meta.env.BASE_URL}demo-data/`;
}

/** Serves a retained, pre-generated API response instead of calling a live backend. */
async function demoApi<T>(path: string, init?: RequestInit): Promise<T> {
  if (init?.method && init.method.toUpperCase() !== "GET") {
    throw new ApiError(
      "This is a read-only synthetic demonstration. Actions are disabled.",
      "DEMO_READ_ONLY",
      405,
    );
  }
  demoManifest ??= fetch(`${demoBase()}manifest.json`).then((response) => response.json() as Promise<DemoManifest>);
  const manifest = await demoManifest;
  const bare = path.split("?")[0];
  const file = manifest.files[path] ?? manifest.files[bare];
  if (!file) {
    throw new ApiError(
      "This view was not captured in the synthetic demonstration snapshot.",
      "DEMO_NOT_CAPTURED",
      404,
    );
  }
  const response = await fetch(`${demoBase()}${file}`);
  const stored = (await response.json()) as { status: number; body: ApiEnvelope<T> | { error_code?: string; message?: string } };
  if (stored.status >= 400) {
    const error = stored.body as { error_code?: string; message?: string };
    throw new ApiError(error.message ?? "Request rejected", error.error_code, stored.status);
  }
  return (stored.body as ApiEnvelope<T>).data;
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  if (DEMO_MODE) return demoApi<T>(path, init);
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 10_000);
  try {
    const response = await fetch(path, { ...init, signal: controller.signal });
    const body = (await response.json()) as
      | ApiEnvelope<T>
      | { error_code?: string; message?: string };
    if (!response.ok) {
      const error = body as { error_code?: string; message?: string };
      throw new ApiError(error.message ?? "Request rejected", error.error_code, response.status);
    }
    return (body as ApiEnvelope<T>).data;
  } catch (reason) {
    if (reason instanceof ApiError) throw reason;
    if (reason instanceof DOMException && reason.name === "AbortError") {
      throw new ApiError("The local API did not respond within ten seconds", "REQUEST_TIMEOUT");
    }
    throw new ApiError("The local operator API is unavailable", "DATABASE_UNAVAILABLE");
  } finally {
    window.clearTimeout(timeout);
  }
}

export function mutate<T>(path: string, body: unknown, key = idempotencyKey()): Promise<T> {
  return api<T>(path, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": key,
    },
    body: JSON.stringify(body),
  });
}
