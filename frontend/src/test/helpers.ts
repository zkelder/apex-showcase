import { vi } from "vitest";

/** Wraps a payload in the same {schema_version, capability, data} envelope
 * every real operator-console route returns, and stubs global fetch to
 * resolve it -- so component tests exercise the identical parsing path
 * api() uses in production, never a shortcut around it. */
export function mockApiOnce(data: unknown, init: { ok?: boolean; status?: number } = {}): void {
  const ok = init.ok ?? true;
  const status = init.status ?? (ok ? 200 : 500);
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValueOnce({
      ok,
      status,
      json: async () => (ok ? { schema_version: "test", capability: "TEST", data } : { error_code: "TEST_ERROR", message: "boom" }),
    }),
  );
}

export function mockApiSequence(payloads: unknown[]): void {
  const fetchMock = vi.fn();
  for (const data of payloads) {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ schema_version: "test", capability: "TEST", data }),
    });
  }
  vi.stubGlobal("fetch", fetchMock);
}

/** Never resolves -- for asserting a page's initial (pre-fetch) render. */
export function mockApiPending(): void {
  vi.stubGlobal("fetch", vi.fn().mockReturnValue(new Promise(() => {})));
}
