// One place that talks to the backend. In the static demo build it reads a recorded snapshot instead.
export const STATIC = import.meta.env.VITE_STATIC === "1";

export interface Row {
  id: number; source: string; external_id: string; kind: string; amount_minor: number; currency: string;
  occurred_at: string; reference: string | null; match_id: number | null; pass: string | null;
  confidence: number | null; diff_minor: number | null; exception: string | null;
  status: "matched" | "pending" | "exception";
}
export interface Reconciliation { ledger: Row[]; bank: Row[] }
export interface Analytics {
  ledger_total: number; ledger_matched: number; bank_total: number; bank_matched: number; match_rate: number | null;
  matches_by_pass: { pass: string; n: number }[];
  fees: { source: string; currency: string; fee_minor: number; gross_minor: number }[];
  dispute_exposure: { currency: string; n: number; exposure_minor: number }[];
  open_exceptions: { kind: string; n: number }[];
  trial_balance: { code: string; type: string; currency: string; balance_minor: number }[];
}
export interface Exception {
  id: number; kind: string; status: "open" | "resolved"; detail: Record<string, unknown>; created_at: string;
  resolved_by: string | null; resolution: string | null; txn_id: number | null; source: string | null;
  external_id: string | null; txn_kind: string | null; amount_minor: number | null; currency: string | null;
}
export interface AuditEntry { id: number; at: string; actor: string; action: string; entity: string; entity_id: string; detail: Record<string, unknown> }

const creds = () => ({ token: sessionStorage.getItem("token") ?? "", actor: sessionStorage.getItem("actor") ?? "" });

async function call<T>(path: string, body?: unknown): Promise<T> {
  const { token, actor } = creds();
  const res = await fetch(`/api/${path}`, {
    method: body ? "POST" : "GET",
    headers: { Authorization: `Bearer ${token}`, "X-Actor": actor, ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`${res.status}: ${JSON.stringify((await res.json().catch(() => ({}))).detail ?? res.statusText)}`);
  return res.json();
}

export const get = <T,>(name: string): Promise<T> =>
  STATIC ? fetch(`./data/${name}.json`).then((r) => r.json()) : call<T>(name);
export const resolveException = (id: number, resolution: string) => call(`exceptions/${id}/resolve`, { resolution });
export const manualMatch = (ledger_txn: number, bank_txn: number, reason: string) => call("matches", { ledger_txn, bank_txn, reason });
export const runMatcher = () => call<{ matched: number; exceptions_raised: number }>("reconcile", {});

export const money = (minor: number | null, currency: string | null) =>
  minor === null || currency === null ? "" :
  new Intl.NumberFormat("en-US", { style: "currency", currency }).format(minor / (["JPY", "KRW"].includes(currency) ? 1 : 100));
export const day = (iso: string) => iso.slice(0, 10);
