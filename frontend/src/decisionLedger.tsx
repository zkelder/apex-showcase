import { FormEvent, useCallback, useEffect, useState } from "react";

import { ApiRecord, api } from "./api";
import {
  Empty,
  ErrorNotice,
  EvidenceButton,
  InspectEvidence,
  Loading,
  PageHead,
  Pagination,
  Panel,
  ReasonList,
  list,
  records,
  text,
} from "./consoleKit";

// Read-only view over the stable CounterfactualDecision ledger. The
// decision event and its classifier interpretation are always rendered as
// visually distinct fields -- never merged -- and no "good/bad decision"
// judgment is computed or displayed anywhere here: counterfactual
// attribution is not implemented yet.

function ClassificationBadges({ item }: { item: ApiRecord }) {
  const versions = records(item.classifications);
  if (!versions.length) {
    return <span className="muted">not yet classified</span>;
  }
  return (
    <span className="codes">
      {versions.map((version) => (
        <code
          key={text(version.classifier_version)}
          title={list(version.reason_families).join(", ") || "no reason families"}
        >
          {text(version.classifier_version)}
          {version.classifier_version === item.current_classifier_version ? " (current)" : ""}
        </code>
      ))}
    </span>
  );
}

export function DecisionLedgerPage({ inspect }: { inspect: InspectEvidence }) {
  const [venue, setVenue] = useState("ALL");
  const [disposition, setDisposition] = useState("ALL");
  const [query, setQuery] = useState("");
  const [appliedQuery, setAppliedQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [value, setValue] = useState<ApiRecord | null>(null);
  const [error, setError] = useState<unknown>(null);

  const refresh = useCallback(() => {
    const params = new URLSearchParams({
      venue,
      disposition,
      query: appliedQuery,
      limit: "25",
      offset: String(offset),
    });
    api<ApiRecord>(`/api/v1/operator-console/decisions?${params}`)
      .then((loaded) => {
        setValue(loaded);
        setError(null);
      })
      .catch(setError);
  }, [venue, disposition, appliedQuery, offset]);
  useEffect(refresh, [refresh]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setOffset(0);
    setAppliedQuery(query.trim());
  };

  const items = records(value?.items);

  return (
    <>
      <PageHead
        eyebrow="STABLE COUNTERFACTUAL LEDGER"
        title="Decision Ledger"
        detail={`Every normalized eligibility and mandate decision, plus each classifier version's independent interpretation of it. Current classifier: ${text(value?.current_classifier_version)}. Historical decisions never change when a classifier version changes.`}
        refresh={refresh}
      />
      <ErrorNotice error={error} />
      <div className="notice neutral">
        STABLE DECISION EVENTS · READ ONLY · NO GOOD/BAD JUDGMENT · COUNTERFACTUAL ATTRIBUTION NOT IMPLEMENTED
      </div>
      {!value && !error && <Loading />}
      <Panel title="Bounded filters">
        <form className="filters" onSubmit={submit}>
          {/* SECURITIES Operator Console Backend Read-Model Closure v1: this
              endpoint's own venue allow-list (application.
              operator_console_evidence._VENUES) is now derived from the
              canonical SharedVenue enum, so SECURITIES-origin decisions are a
              real, accepted filter choice -- row rendering below was already
              venue-generic and needed no change. */}
          <label>
            Venue
            <select value={venue} onChange={(event) => { setVenue(event.target.value); setOffset(0); }}>
              <option>ALL</option>
              <option>CRYPTO</option>
              <option>KALSHI</option>
              <option>SECURITIES</option>
            </select>
          </label>
          <label>
            Disposition
            <select value={disposition} onChange={(event) => { setDisposition(event.target.value); setOffset(0); }}>
              <option>ALL</option>
              <option>ADVANCED</option>
              <option>CONSTRAINED</option>
              <option>NOT_ADVANCED</option>
            </select>
          </label>
          <label>
            Exact ID / subject search
            <input maxLength={160} value={query} onChange={(event) => setQuery(event.target.value)} />
          </label>
          <button type="submit">Apply filters</button>
        </form>
      </Panel>
      <Panel title="Decision ledger" badge="AUTHORITY NONE">
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th scope="col">Decision / evaluated</th>
                <th scope="col">Market / subject</th>
                <th scope="col">Disposition</th>
                <th scope="col">Raw reason codes</th>
                <th scope="col">Classifier interpretation</th>
                <th scope="col">Follow-up</th>
                <th scope="col">Evidence</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={text(item.decision_id)}>
                  <td>
                    {text(item.decision_id)}
                    <small>{text(item.evaluated_at)} · {text(item.origin)}</small>
                  </td>
                  <td>
                    {text(item.subject_source_record_type)}
                    <small>{text(item.subject_projection_id)}</small>
                  </td>
                  <td>
                    <span className={`state state-${text(item.disposition, "unknown").toLowerCase()}`}>
                      {text(item.disposition)}
                    </span>
                  </td>
                  <td>
                    <ReasonList codes={item.raw_reason_codes} />
                  </td>
                  <td>
                    <ClassificationBadges item={item} />
                    <small>
                      <ReasonList codes={item.current_reason_families} emptyText="no current families" />
                    </small>
                  </td>
                  <td>{item.has_followup ? "observed evidence exists" : "none yet"}</td>
                  <td><EvidenceButton href={item.evidence_href} label="Counterfactual decision" inspect={inspect} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!items.length && value && (
          <Empty title="No matching decisions">
            {value.empty_state === "NO_RETAINED_DECISIONS"
              ? "No retained CounterfactualDecision rows exist yet."
              : "No retained decision matches these filters."}
          </Empty>
        )}
        <Pagination offset={offset} total={Number(value?.total ?? 0)} setOffset={setOffset} />
      </Panel>
    </>
  );
}
