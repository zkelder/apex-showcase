import { useCallback, useEffect, useState } from "react";

import { ApiRecord, api } from "./api";
import {
  Empty,
  ErrorNotice,
  EvidenceButton,
  InspectEvidence,
  Loading,
  PageHead,
  Panel,
  list,
  pairs,
  record,
  records,
  text,
} from "./consoleKit";

// Options-forward research campaign view. The "N / target" progress figure,
// per-session history, and per-underlying detail are all read from
// retained state -- never hardcoded UI constants -- and this page never
// triggers a capture: it fetches one bounded GET on mount/refresh only.

function CampaignMeter({ value }: { value: ApiRecord }) {
  const captured = Number(value.campaign_sessions_captured ?? 0);
  const target = Number(value.campaign_sessions_target ?? 0) || 1;
  const pct = Math.max(0, Math.min(100, (captured / target) * 100));
  return (
    <div className="meter" role="img" aria-label={`Forward campaign progress: ${text(value.campaign_progress_label)}`}>
      <div className="meter-track">
        <div className="meter-fill" style={{ width: `${pct}%` }} />
      </div>
      <span className="meter-label">{text(value.campaign_progress_label, "0/0")}</span>
    </div>
  );
}

function SessionBarChart({ sessions }: { sessions: ApiRecord[] }) {
  if (!sessions.length) return null;
  const counts = sessions.map((session) => Number(session.underlyings_captured_count ?? 0));
  const max = Math.max(1, ...counts);
  return (
    <>
      <div className="bar-chart">
        {sessions.map((session, index) => (
          <div
            key={text(session.session_id)}
            className="bar"
            style={{ height: `${Math.max(4, (counts[index] / max) * 100)}%` }}
            title={`${text(session.session_id)}: ${counts[index]} underlyings captured`}
          />
        ))}
      </div>
      <div className="bar-axis">
        {sessions.map((session) => (
          <span key={text(session.session_id)}>{text(session.session_id)}</span>
        ))}
      </div>
      <p className="muted">Underlyings captured per banked session (retained checkpoint history).</p>
    </>
  );
}

export function OptionsResearchPage({ inspect }: { inspect: InspectEvidence }) {
  const [value, setValue] = useState<ApiRecord | null>(null);
  const [error, setError] = useState<unknown>(null);

  const refresh = useCallback(() => {
    api<ApiRecord>("/api/v1/operator-console/options-research")
      .then((loaded) => {
        setValue(loaded);
        setError(null);
      })
      .catch(setError);
  }, []);
  useEffect(refresh, [refresh]);

  const sessions = records(value?.sessions);
  const latestSession = record(value?.latest_session);
  const underlyings = records(value?.latest_underlyings);
  const captureTask = record(value?.capture_task);
  const missedSessions = list(value?.missed_session_ids);

  return (
    <>
      <PageHead
        eyebrow="OPTIONS-FORWARD RESEARCH CAMPAIGN"
        title="Options Research"
        detail="Longitudinal defined-risk options evidence, captured once per trading session after market close. Read-only: opening this page never starts a capture."
        refresh={refresh}
      />
      <ErrorNotice error={error} />
      <div className="notice neutral">
        {text(latestSession?.opra_entitlement_state, "OPRA state unknown")} · OPEN INTEREST{" "}
        {text(latestSession?.open_interest_limitation, "UNKNOWN")} · HISTORICAL PRICING{" "}
        {text(latestSession?.historical_pricing_limitation, "UNKNOWN")}
      </div>
      {!value && !error && <Loading />}

      {value?.empty_state ? (
        <Empty title="No retained forward session">
          No genuine options-forward checkpoint has been retained yet for this campaign.
        </Empty>
      ) : (
        value && (
          <>
            <Panel title="Forward campaign progress" badge={text(value.readiness_state)}>
              <CampaignMeter value={value} />
              <p className="muted">
                {list(value.readiness_reasons).join(" · ") || "distinct trading sessions with a retained checkpoint"}
              </p>
              <SessionBarChart sessions={sessions} />
            </Panel>

            <div className="research-summary-grid">
              <Panel title="Latest session">
                <dl className="key-values">
                  <div><dt>Session</dt><dd>{text(latestSession?.session_id)}</dd></div>
                  <div><dt>Window</dt><dd>{text(latestSession?.window_open_at)} → {text(latestSession?.window_close_at)}</dd></div>
                  <div><dt>Captured / requested</dt><dd>{text(latestSession?.underlyings_captured_count)} / {text(latestSession?.underlyings_requested_count)}</dd></div>
                  <div><dt>Structures retained</dt><dd>{text(latestSession?.structures_retained)} / {text(latestSession?.structures_evaluated)} evaluated</dd></div>
                </dl>
                <div className="control-row">
                  <EvidenceButton href={latestSession?.evidence_href} label="Latest checkpoint" inspect={inspect} />
                </div>
              </Panel>
              <Panel title="Next capture window" badge={text(captureTask?.status, "UNKNOWN")}>
                <dl className="key-values">
                  <div><dt>Session</dt><dd>{text(captureTask?.session_id, "none identified")}</dd></div>
                  <div><dt>Window</dt><dd>{text(captureTask?.window_open_at, "—")} → {text(captureTask?.window_close_at, "—")}</dd></div>
                  <div><dt>Next eligible window opens</dt><dd>{text(captureTask?.next_capture_window_open_at, "not retained")}</dd></div>
                </dl>
                <p className="muted">{list(captureTask?.reasons).join(" · ") || "no reasons retained"}</p>
              </Panel>
              <Panel title="Coverage quality (latest session)">
                <dl className="key-values">
                  {pairs(latestSession?.chain_coverage_quality_counts).map(([key, count]) => (
                    <div key={key}><dt>{key}</dt><dd>{count}</dd></div>
                  ))}
                  {!pairs(latestSession?.chain_coverage_quality_counts).length && <div><dt>none retained</dt><dd>0</dd></div>}
                </dl>
              </Panel>
              <Panel title="Standing limitations">
                <ul>
                  {list(latestSession?.limitations).map((item) => <li key={item}><code>{item}</code></li>)}
                </ul>
                {!list(latestSession?.limitations).length && <p className="muted">None retained for the latest session.</p>}
              </Panel>
            </div>

            {missedSessions.length > 0 && (
              <Panel title="Genuine missed capture windows" badge="HISTORICAL · NOT AN ACTIVE INCIDENT">
                <p className="muted">
                  Calendar sessions whose capture window already closed with no retained checkpoint. Shown
                  honestly, separate from banked sessions below -- these are closed history, not a current alarm.
                </p>
                <div className="codes">
                  {missedSessions.map((session) => <code key={session}>{session}</code>)}
                </div>
              </Panel>
            )}

            <Panel title="Session history" badge="BANKED SESSIONS">
              <div className="scroll">
                <table>
                  <thead>
                    <tr>
                      <th scope="col">Session</th>
                      <th scope="col">Window</th>
                      <th scope="col">Captured / requested</th>
                      <th scope="col">Quote quality</th>
                      <th scope="col">Structures</th>
                      <th scope="col">Limitations</th>
                      <th scope="col">Evidence</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sessions.map((session) => (
                      <tr key={text(session.session_id)}>
                        <td>{text(session.session_id)}<small>{text(session.provider)} · {text(session.feed)}</small></td>
                        <td>{text(session.window_open_at)}<small>→ {text(session.window_close_at)}</small></td>
                        <td>{text(session.underlyings_captured_count)} / {text(session.underlyings_requested_count)}<small>{text(session.checkpoint_count_this_session)} poll(s) this session</small></td>
                        <td>{pairs(session.quote_quality_counts).map(([key, count]) => `${key} ${count}`).join(" · ") || "none retained"}</td>
                        <td>{text(session.structures_retained)} / {text(session.structures_evaluated)}</td>
                        <td>{list(session.limitations).join(" · ") || "none"}</td>
                        <td><EvidenceButton href={session.evidence_href} label="Session checkpoint" inspect={inspect} /></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {!sessions.length && <Empty title="No banked sessions">No genuine session has a retained checkpoint yet.</Empty>}
            </Panel>

            <Panel title="Latest session — per-underlying detail" badge="AUTHORITY NONE">
              <div className="scroll">
                <table>
                  <thead>
                    <tr>
                      <th scope="col">Symbol</th>
                      <th scope="col">Snapshot quality</th>
                      <th scope="col">Quote count</th>
                      <th scope="col">Contracts (considered / quoted / excluded)</th>
                      <th scope="col">Cap constrained</th>
                      <th scope="col">Evidence limitations</th>
                    </tr>
                  </thead>
                  <tbody>
                    {underlyings.map((item) => (
                      <tr key={text(item.symbol)}>
                        <td>{text(item.symbol)}<small>{text(item.underlying_canonical_security_id)}</small></td>
                        <td><span className={`state state-${text(item.snapshot_quality, "unknown").toLowerCase()}`}>{text(item.snapshot_quality)}</span></td>
                        <td className="numeric">{text(item.quote_count)}</td>
                        <td>{text(item.contracts_considered)} / {text(item.contracts_quoted)} / {text(item.contracts_excluded)}</td>
                        <td>{item.cap_constrained ? "yes" : "no"}</td>
                        <td>{list(item.evidence_limitations).join(" · ") || "none retained"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {!underlyings.length && (
                <Empty title="No per-underlying snapshot detail">
                  The latest checkpoint retains no individually resolvable snapshot for this session yet.
                </Empty>
              )}
            </Panel>
          </>
        )
      )}
    </>
  );
}
