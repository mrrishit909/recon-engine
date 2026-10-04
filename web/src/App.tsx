import type { ColumnDef } from "@tanstack/react-table";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  type Analytics, type AuditEntry, type Exception, type Reconciliation, type Row, STATIC,
  day, get, manualMatch, money, resolveException, runMatcher,
} from "./api";
import { Grid } from "./Grid";

type Tab = "control" | "analytics" | "exceptions" | "audit";
type Filter = "all" | "matched" | "pending" | "exception";
interface Data { recon: Reconciliation; analytics: Analytics; exceptions: Exception[]; audit: AuditEntry[] }

const label = (s: string) => s.replace(/_/g, " ");
const hue = (matchId: number) => (matchId * 47) % 360;

function Link({ row }: { row: Row }) {
  if (row.match_id !== null)
    return (
      <span className="link" style={{ ["--h" as string]: hue(row.match_id) }} title={`pass: ${row.pass}, confidence ${row.confidence}`}>
        M-{row.match_id} · {row.pass}
        {row.diff_minor ? <b className="diff"> Δ {money(row.diff_minor, row.currency)}</b> : null}
      </span>
    );
  return <span className={`chip ${row.status}`}>{row.status === "exception" ? label(row.exception ?? "exception") : "pending"}</span>;
}

function columns(side: "ledger" | "bank"): ColumnDef<Row, any>[] {
  const date: ColumnDef<Row, any> = { header: "Date", accessorFn: (r) => day(r.occurred_at) };
  const id: ColumnDef<Row, any> = { header: "ID", accessorKey: "external_id", cell: (c) => <code>{c.getValue()}</code> };
  const amount: ColumnDef<Row, any> = { header: "Amount", accessorKey: "amount_minor", meta: { num: true }, cell: (c) => money(c.getValue(), c.row.original.currency) };
  const link: ColumnDef<Row, any> = { header: "Link", accessorFn: (r) => r.match_id ?? -1, cell: (c) => <Link row={c.row.original} /> };
  // the link sits on the inner edge of each pane, next to the split
  return side === "ledger"
    ? [date, { header: "Source", accessorFn: (r) => `${r.source} ${r.kind}` }, id, amount, link]
    : [link, date, amount, { header: "Bank memo", accessorFn: (r) => r.reference ?? "" }, id];
}

function ControlCenter({ data, act }: { data: Data; act: Actions }) {
  const [filter, setFilter] = useState<Filter>("all");
  const [query, setQuery] = useState("");
  const [active, setActive] = useState<number | null>(null);          // highlighted match id
  const [pick, setPick] = useState<{ ledger?: Row; bank?: Row }>({});  // unmatched rows chosen for a manual match
  const [reason, setReason] = useState("");
  const [path, setPath] = useState("");
  const panes = useRef<HTMLDivElement>(null);

  const visible = useCallback((rows: Row[]) => rows.filter((r) =>
    (filter === "all" || r.status === filter) &&
    (!query || `${r.external_id} ${r.reference ?? ""} ${r.source} ${(r.amount_minor / 100).toFixed(2)}`.toLowerCase().includes(query.toLowerCase()))),
  [filter, query]);
  const ledger = useMemo(() => visible(data.recon.ledger), [visible, data]);
  const bank = useMemo(() => visible(data.recon.bank), [visible, data]);
  const cols = useMemo(() => ({ ledger: columns("ledger"), bank: columns("bank") }), []);

  // The curve that joins the two halves of the highlighted match across the split.
  const draw = useCallback(() => {
    const box = panes.current;
    const a = active === null ? null : box?.querySelector<HTMLElement>(`[data-side=ledger] tr.active`);
    const b = active === null ? null : box?.querySelector<HTMLElement>(`[data-side=bank] tr.active`);
    if (!box || !a || !b) return setPath("");
    const o = box.getBoundingClientRect(), ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
    if (rb.left < ra.right - 4) return setPath("");                   // panes are stacked (phone): no connector
    const x1 = ra.right - o.left, y1 = ra.top + ra.height / 2 - o.top, x2 = rb.left - o.left, y2 = rb.top + rb.height / 2 - o.top;
    setPath(`M${x1},${y1} C${x1 + 28},${y1} ${x2 - 28},${y2} ${x2},${y2}`);
  }, [active]);
  useLayoutEffect(() => {
    panes.current?.querySelectorAll<HTMLElement>("tr.active").forEach((el) => el.scrollIntoView({ block: "nearest" }));
    draw();
    window.addEventListener("resize", draw);
    return () => window.removeEventListener("resize", draw);
  }, [draw, ledger, bank]);

  const click = (side: "ledger" | "bank") => (row: Row) => {
    if (row.match_id !== null) { setActive(row.match_id === active ? null : row.match_id); return; }
    setActive(null);
    setPick((p) => ({ ...p, [side]: p[side]?.id === row.id ? undefined : row }));
  };
  const rowClass = (side: "ledger" | "bank") => (r: Row) =>
    [r.status, r.match_id !== null && r.match_id === active ? "active" : "", pick[side]?.id === r.id ? "picked" : "",
     r.diff_minor ? "has-diff" : ""].join(" ");
  const counts = (rows: Row[]) => `${rows.filter((r) => r.status === "matched").length} matched · ${rows.filter((r) => r.status === "pending").length} pending · ${rows.filter((r) => r.status === "exception").length} in review`;

  return (
    <>
      <div className="toolbar">
        <div className="seg" role="group" aria-label="Filter by status">
          {(["all", "matched", "pending", "exception"] as Filter[]).map((f) => (
            <button key={f} aria-pressed={filter === f} onClick={() => setFilter(f)}>{f === "exception" ? "in review" : f}</button>
          ))}
        </div>
        <input type="search" placeholder="Search id, memo, amount" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Search" />
        <button onClick={act.run}>Run matcher</button>
      </div>
      {(pick.ledger || pick.bank) && (
        <form className="manual" onSubmit={(e) => { e.preventDefault(); if (pick.ledger && pick.bank) act.match(pick.ledger, pick.bank, reason).then(() => { setPick({}); setReason(""); }); }}>
          <span>Manual match: <b>{pick.ledger ? `${pick.ledger.external_id} ${money(pick.ledger.amount_minor, pick.ledger.currency)}` : "pick an unmatched record on the left"}</b>
            {" ↔ "}<b>{pick.bank ? `${pick.bank.external_id} ${money(pick.bank.amount_minor, pick.bank.currency)}` : "pick an unmatched deposit on the right"}</b></span>
          <input required minLength={3} placeholder="Reason (goes in the audit log)" value={reason} onChange={(e) => setReason(e.target.value)} aria-label="Reason" />
          <button disabled={!pick.ledger || !pick.bank}>Match</button>
          <button type="button" onClick={() => setPick({})}>Cancel</button>
        </form>
      )}
      <div className="panes" ref={panes}>
        <section data-side="ledger" onScrollCapture={draw}>
          <h2>Processor &amp; accounting records <small>{counts(data.recon.ledger)}</small></h2>
          <Grid rows={ledger} columns={cols.ledger} rowClass={rowClass("ledger")} onRowClick={click("ledger")} />
        </section>
        <section data-side="bank" onScrollCapture={draw}>
          <h2>Bank deposits <small>{counts(data.recon.bank)}</small></h2>
          <Grid rows={bank} columns={cols.bank} rowClass={rowClass("bank")} onRowClick={click("bank")} />
        </section>
        <svg className="connector" aria-hidden="true">{path && <path d={path} style={{ ["--h" as string]: hue(active ?? 0) }} />}</svg>
      </div>
      <p className="hint">Click a matched row to see its partner. Click one unmatched row on each side to match them by hand.</p>
    </>
  );
}

function AnalyticsView({ a }: { a: Analytics }) {
  const maxPass = Math.max(1, ...a.matches_by_pass.map((p) => p.n));
  return (
    <div className="analytics">
      <div className="tiles">
        <div className="tile"><span>Match rate</span><b>{a.match_rate === null ? "n/a" : `${(a.match_rate * 100).toFixed(1)}%`}</b>
          <small>{a.ledger_matched} of {a.ledger_total} payouts and payments found at the bank</small></div>
        {a.fees.map((f) => (
          <div className="tile" key={f.source + f.currency}><span>Fees paid · {f.source} · {f.currency}</span><b>{money(f.fee_minor, f.currency)}</b>
            <small>{((f.fee_minor / f.gross_minor) * 100).toFixed(2)}% of {money(f.gross_minor, f.currency)} gross</small></div>
        ))}
        {a.dispute_exposure.map((d) => (
          <div className="tile warn" key={d.currency}><span>Unresolved dispute exposure · {d.currency}</span><b>{money(d.exposure_minor, d.currency)}</b>
            <small>{d.n} open chargeback{d.n === 1 ? "" : "s"}, fees included</small></div>
        ))}
        <div className="tile warn"><span>Open exceptions</span><b>{a.open_exceptions.reduce((s, e) => s + e.n, 0)}</b>
          <small>{a.open_exceptions.map((e) => `${e.n} ${label(e.kind)}`).join(" · ") || "none"}</small></div>
      </div>
      <div className="two">
        <section>
          <h2>Matches by pass</h2>
          {a.matches_by_pass.map((p) => (
            <div className="bar" key={p.pass}><span>{p.pass}</span><i style={{ width: `${(p.n / maxPass) * 100}%` }} /><b>{p.n}</b></div>
          ))}
        </section>
        <section>
          <h2>Trial balance <small>debits positive; each currency sums to zero</small></h2>
          <Grid rows={a.trial_balance} columns={[
            { header: "Account", accessorKey: "code", cell: (c) => label(c.getValue()) },
            { header: "Type", accessorKey: "type" },
            { header: "Balance", accessorKey: "balance_minor", meta: { num: true }, cell: (c) => money(c.getValue(), c.row.original.currency) },
          ]} />
        </section>
      </div>
    </div>
  );
}

function ExceptionsView({ data, act }: { data: Data; act: Actions }) {
  const [sel, setSel] = useState<Exception | null>(null);
  const [reason, setReason] = useState("");
  const done = () => { setSel(null); setReason(""); };
  const pair = sel && typeof sel.detail.ledger_txn === "number" && sel.txn_id !== null
    ? { ledger: data.recon.ledger.find((r) => r.id === sel.detail.ledger_txn), bank: data.recon.bank.find((r) => r.id === sel.txn_id) } : null;
  return (
    <>
      {sel && sel.status === "open" && (
        <form className="manual" onSubmit={(e) => { e.preventDefault(); act.resolve(sel, reason).then(done); }}>
          <span>Exception #{sel.id} · <b>{label(sel.kind)}</b> · {sel.external_id} {money(sel.amount_minor, sel.currency)}</span>
          <input required minLength={3} placeholder="Reason (goes in the audit log)" value={reason} onChange={(e) => setReason(e.target.value)} aria-label="Reason" />
          <button>Resolve</button>
          {pair?.ledger && pair.bank && pair.ledger.match_id === null && pair.bank.match_id === null && (
            <button type="button" disabled={reason.length < 3} onClick={() => act.match(pair.ledger!, pair.bank!, reason).then(done)}>Match the two records</button>
          )}
          <button type="button" onClick={done}>Cancel</button>
        </form>
      )}
      <Grid rows={data.exceptions} onRowClick={setSel} rowClass={(e) => `${e.status === "open" ? "exception" : "resolved"} ${sel?.id === e.id ? "picked" : ""}`} columns={[
        { header: "#", accessorKey: "id", meta: { num: true } },
        { header: "Kind", accessorKey: "kind", cell: (c) => <span className={`chip ${c.row.original.status === "open" ? "exception" : "matched"}`}>{label(c.getValue())}</span> },
        { header: "Record", accessorFn: (e) => `${e.source ?? ""} ${e.txn_kind ?? ""} ${e.external_id ?? ""}` },
        { header: "Amount", accessorKey: "amount_minor", meta: { num: true }, cell: (c) => money(c.getValue(), c.row.original.currency) },
        { header: "Detail", accessorFn: (e) => Object.entries(e.detail).map(([k, v]) => `${label(k)}: ${JSON.stringify(v)}`).join(" · ") },
        { header: "Status", accessorFn: (e) => (e.status === "open" ? "open" : `resolved by ${e.resolved_by}: ${e.resolution}`) },
      ]} />
      <p className="hint">Click an open exception to resolve it. Every resolution needs a reason and is written to the audit log.</p>
    </>
  );
}

interface Actions {
  run: () => Promise<void>;
  resolve: (e: Exception, reason: string) => Promise<void>;
  match: (ledger: Row, bank: Row, reason: string) => Promise<void>;
}

export function App() {
  const [tab, setTab] = useState<Tab>("control");
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState("");
  const [signedIn, setSignedIn] = useState(STATIC || !!sessionStorage.getItem("token"));

  const load = useCallback(async () => {
    try {
      const [recon, analytics, exceptions, audit] = await Promise.all([
        get<Reconciliation>("reconciliation"), get<Analytics>("analytics"), get<Exception[]>("exceptions"), get<AuditEntry[]>("audit")]);
      setData({ recon, analytics, exceptions, audit });
      setError("");
    } catch (e) { setError(String(e)); if (String(e).includes("401")) { sessionStorage.removeItem("token"); setSignedIn(false); } }
  }, []);
  useEffect(() => { if (signedIn) load(); }, [signedIn, load]);

  const guard = (fn: () => Promise<unknown>) => async () => { try { await fn(); await load(); } catch (e) { setError(String(e)); } };
  const now = () => new Date().toISOString();
  const act: Actions = STATIC ? {
    // Demo snapshot: changes live in this browser tab only.
    run: async () => setError("This is a recorded snapshot: the matcher already ran. Run the project locally to re-run it."),
    resolve: async (e, reason) => setData((d) => d && {
      ...d,
      exceptions: d.exceptions.map((x) => x.id === e.id ? { ...x, status: "resolved", resolved_by: "user:demo", resolution: reason } : x),
      audit: [{ id: d.audit.length + 1, at: now(), actor: "user:demo", action: "exception_resolved", entity: "exceptions", entity_id: String(e.id), detail: { resolution: reason } }, ...d.audit],
    }),
    match: async (l, b, reason) => setData((d) => {
      if (!d) return d;
      const id = Math.max(0, ...d.recon.ledger.map((r) => r.match_id ?? 0)) + 1;
      const set = (r: Row): Row => r.id === l.id || r.id === b.id
        ? { ...r, match_id: id, pass: "manual", confidence: 1, status: "matched", exception: null, diff_minor: l.currency === b.currency ? b.amount_minor - l.amount_minor : 0 } : r;
      return {
        ...d,
        recon: { ledger: d.recon.ledger.map(set), bank: d.recon.bank.map(set) },
        exceptions: d.exceptions.map((x) => x.status === "open" && (x.txn_id === l.id || x.txn_id === b.id || x.detail.ledger_txn === l.id)
          ? { ...x, status: "resolved", resolved_by: "user:demo", resolution: `manual match ${id}: ${reason}` } : x),
        audit: [{ id: d.audit.length + 1, at: now(), actor: "user:demo", action: "manual_match", entity: "matches", entity_id: String(id), detail: { ledger_txn: l.id, bank_txn: b.id, reason } }, ...d.audit],
      };
    }),
  } : {
    run: guard(runMatcher),
    resolve: (e, reason) => guard(() => resolveException(e.id, reason))(),
    match: (l, b, reason) => guard(() => manualMatch(l.id, b.id, reason))(),
  };

  if (!signedIn)
    return (
      <form className="signin" onSubmit={(e) => { e.preventDefault(); const f = new FormData(e.currentTarget); sessionStorage.setItem("token", String(f.get("token"))); sessionStorage.setItem("actor", String(f.get("actor"))); setSignedIn(true); }}>
        <h1>Reconciliation Control Center</h1>
        <label>Your name (recorded in the audit log)<input name="actor" required autoComplete="name" /></label>
        <label>API token<input name="token" type="password" required autoComplete="off" /></label>
        <button>Sign in</button>
        {error && <p className="error" role="alert">{error}</p>}
      </form>
    );

  const open = data?.exceptions.filter((e) => e.status === "open").length ?? 0;
  return (
    <>
      <header>
        <h1>Reconciliation Control Center</h1>
        <nav>
          {([["control", "Control center"], ["analytics", "Analytics"], ["exceptions", `Exceptions (${open})`], ["audit", "Audit log"]] as [Tab, string][]).map(([t, name]) => (
            <button key={t} aria-current={tab === t ? "page" : undefined} onClick={() => setTab(t)}>{name}</button>
          ))}
        </nav>
      </header>
      {STATIC && <p className="banner">Demo: a recorded run on synthetic data (no real money or customers). Actions you take here are not saved.</p>}
      {error && <p className="error" role="alert">{error} <button onClick={() => setError("")}>Dismiss</button></p>}
      <main>
        {!data ? <p className="empty">Loading…</p> :
          tab === "control" ? <ControlCenter data={data} act={act} /> :
          tab === "analytics" ? <AnalyticsView a={data.analytics} /> :
          tab === "exceptions" ? <ExceptionsView data={data} act={act} /> :
          <Grid rows={data.audit} columns={[
            { header: "#", accessorKey: "id", meta: { num: true } },
            { header: "When (UTC)", accessorFn: (r) => r.at.slice(0, 19).replace("T", " ") },
            { header: "Who", accessorKey: "actor" },
            { header: "Action", accessorKey: "action", cell: (c) => label(c.getValue()) },
            { header: "What", accessorFn: (r) => `${r.entity} ${r.entity_id}` },
            { header: "Detail", accessorFn: (r) => Object.entries(r.detail).map(([k, v]) => `${label(k)}: ${JSON.stringify(v)}`).join(" · ") },
          ]} />}
      </main>
    </>
  );
}
