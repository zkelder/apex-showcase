import { useCallback, useEffect, useState } from "react";

import { ApiRecord, api } from "./api";
import {
  Empty,
  ErrorNotice,
  EvidenceButton,
  InspectEvidence,
  Loading,
  Metrics,
  PageHead,
  Panel,
  ReasonList,
  boolText,
  fraction,
  list,
  pairs,
  record,
  records,
  text,
} from "./consoleKit";

// Existing normalized PortfolioState and InvestmentMandate truth only.
// No valuation logic runs here, and there is no editing/authoring control
// for the mandate in v1 -- the versioned backend mandate is the only
// mandate this page ever shows.

const EXPOSURE_SLOTS = ["slot-1", "slot-2", "slot-3", "slot-4"] as const;

function ExposureBars({ title, pairsValue }: { title: string; pairsValue: Array<[string, string]> }) {
  const numeric = pairsValue.map(([key, value]) => [key, Number(value) || 0] as const);
  const max = Math.max(1, ...numeric.map(([, value]) => value));
  return (
    <Panel title={title}>
      {numeric.length ? (
        <>
          <div className="exposure-bars">
            {numeric.map(([key, value], index) => (
              <div className="exposure-row" key={key}>
                <span>{key}</span>
                <div className="exposure-track">
                  <div
                    className={`exposure-fill ${EXPOSURE_SLOTS[index % EXPOSURE_SLOTS.length]}`}
                    style={{ width: `${Math.max(2, (value / max) * 100)}%` }}
                  />
                </div>
                <span className="numeric">{pairsValue[index][1]}</span>
              </div>
            ))}
          </div>
          <div className="legend">
            {numeric.map(([key], index) => (
              <span className="legend-item" key={key}>
                <span className={`legend-swatch exposure-fill ${EXPOSURE_SLOTS[index % EXPOSURE_SLOTS.length]}`} />
                {key}
              </span>
            ))}
          </div>
        </>
      ) : (
        <p className="muted">No known exposure value retained for this grouping.</p>
      )}
    </Panel>
  );
}

export function PortfolioMandatePage({ inspect }: { inspect: InspectEvidence }) {
  const [value, setValue] = useState<ApiRecord | null>(null);
  const [error, setError] = useState<unknown>(null);

  const refresh = useCallback(() => {
    api<ApiRecord>("/api/v1/operator-console/portfolio-mandate")
      .then((loaded) => {
        setValue(loaded);
        setError(null);
      })
      .catch(setError);
  }, []);
  useEffect(refresh, [refresh]);

  const snapshot = record(value?.portfolio_snapshot);
  const balances = records(value?.account_balances);
  const positions = records(value?.positions);
  const mandate = record(value?.mandate);
  const mandateVersions = records(value?.mandate_versions);
  const evaluations = records(value?.latest_evaluations);

  return (
    <>
      <PageHead
        eyebrow="NORMALIZED PORTFOLIO AND INVESTMENT MANDATE"
        title="Portfolio / Mandate"
        detail="Existing retained portfolio snapshot and versioned mandate truth only. No mandate editing exists in this console."
        refresh={refresh}
      />
      <ErrorNotice error={error} />
      <div className="notice neutral">AUTHORITY NONE · CURRENT MANDATE PERMITS ZERO CAPITAL UNTIL EXPLICITLY AUTHORED OTHERWISE</div>
      {!value && !error && <Loading />}

      <Panel title="Portfolio snapshot" badge={text(snapshot?.completeness, "UNKNOWN")}>
        {value?.portfolio_empty_state ? (
          <Empty title="No retained portfolio snapshot">Run the bounded portfolio-snapshot task to retain one; this console never triggers it.</Empty>
        ) : (
          <>
            <Metrics
              values={[
                ["As of", text(snapshot?.as_of)],
                ["Known cash", snapshot?.total_known_cash, "may be unknown"],
                ["Known position value", snapshot?.total_known_position_value],
                ["Known portfolio value", snapshot?.total_known_portfolio_value],
                ["Accounts / positions", `${balances.length} / ${positions.length}`],
                ["Unavailable sources", records(snapshot?.unavailable_sources).length],
              ]}
            />
            {list(snapshot?.warnings).length > 0 && (
              <p className="muted">Warnings: {list(snapshot?.warnings).join(" · ")}</p>
            )}
            {list(snapshot?.limitations).length > 0 && (
              <p className="muted">Limitations: {list(snapshot?.limitations).join(" · ")}</p>
            )}
          </>
        )}
      </Panel>

      {snapshot && (
        <div className="research-summary-grid" style={{ gridTemplateColumns: "repeat(2, minmax(0, 1fr))" }}>
          <ExposureBars title="Exposure by venue" pairsValue={pairs(snapshot.exposure_by_venue)} />
          <ExposureBars title="Exposure by market family" pairsValue={pairs(snapshot.exposure_by_market_family)} />
        </div>
      )}

      <Panel title="Account balances">
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th scope="col">Account / venue</th>
                <th scope="col">Cash / reserved / available</th>
                <th scope="col">Buying power / equity</th>
                <th scope="col">Evidence class</th>
                <th scope="col">Limitations</th>
              </tr>
            </thead>
            <tbody>
              {balances.map((item) => (
                <tr key={text(item.balance_id)}>
                  <td>{text(item.account_id)}<small>{text(item.venue)} · {text(item.account_kind)}</small></td>
                  <td>{text(item.cash, "unknown")} / {text(item.reserved_cash, "unknown")} / {text(item.available_cash, "unknown")}</td>
                  <td>{text(item.buying_power, "unknown")} / {text(item.equity, "unknown")}</td>
                  <td>{text(item.evidence_class)}</td>
                  <td>{list(item.limitations).join(" · ") || "none"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!balances.length && <Empty title="No retained account balances">No balance evidence is retained in this snapshot.</Empty>}
      </Panel>

      <Panel title="Positions">
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th scope="col">Instrument / venue</th>
                <th scope="col">Exposure / quantity</th>
                <th scope="col">Mark / exposure value</th>
                <th scope="col">Unrealized / realized P/L</th>
                <th scope="col">Limitations</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((item) => (
                <tr key={text(item.position_id)}>
                  <td>{text(item.instrument_id)}<small>{text(item.venue)} · {text(item.market_family)}</small></td>
                  <td>{text(item.exposure)} / {text(item.quantity, "unknown")}</td>
                  <td>{text(item.mark_value, "unknown")} / {text(item.exposure_value, "unknown")}<small>{text(item.exposure_value_source, "no source")}</small></td>
                  <td>{text(item.unrealized_pnl, "unknown")} / {text(item.realized_pnl, "unknown")}</td>
                  <td>{list(item.limitations).join(" · ") || "none"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!positions.length && <Empty title="No retained positions">No position evidence is retained in this snapshot.</Empty>}
      </Panel>

      <Panel title="Active investment mandate" badge={mandate ? `v${text(mandate.version)}` : "NONE AUTHORED"}>
        {value?.mandate_empty_state ? (
          <Empty title="No mandate authored">Every mandate evaluation stays REJECTED until an operator explicitly authors a real mandate.</Empty>
        ) : (
          <>
            <Metrics
              values={[
                ["Version", mandate?.version],
                ["Effective at", text(mandate?.effective_at)],
                ["Total capital allocated", mandate?.total_capital_allocated],
                ["Autonomy requirement", mandate?.autonomy_requirement],
                ["Leverage / short / options", `${boolText(mandate?.leverage_permitted)} / ${boolText(mandate?.short_exposure_permitted)} / ${boolText(mandate?.options_permitted)}`],
                ["Max lockup days", mandate?.maximum_capital_lockup_days],
              ]}
            />
            <dl className="key-values">
              <div><dt>Permitted venues</dt><dd>{list(mandate?.permitted_venues).join(" · ") || "none"}</dd></div>
              <div><dt>Permitted market families</dt><dd>{list(mandate?.permitted_market_families).join(" · ") || "none"}</dd></div>
              <div><dt>Disallowed market families</dt><dd>{list(mandate?.disallowed_market_families).join(" · ") || "none"}</dd></div>
              <div><dt>Aggregate / instrument cap</dt><dd>{fraction(mandate?.maximum_aggregate_exposure_fraction)} / {fraction(mandate?.maximum_per_instrument_fraction)}</dd></div>
              <div><dt>Venue / family concentration cap</dt><dd>{fraction(mandate?.maximum_venue_concentration_fraction)} / {fraction(mandate?.maximum_market_family_concentration_fraction)}</dd></div>
              <div><dt>Notes</dt><dd>{text(mandate?.notes, "none")}</dd></div>
            </dl>
            {mandateVersions.length > 1 && (
              <p className="muted">
                {mandateVersions.length} version(s) retained for this lineage, newest first:{" "}
                {mandateVersions.map((item) => `v${text(item.version)}`).reverse().join(" ← ")}
              </p>
            )}
          </>
        )}
      </Panel>

      <Panel title="Latest mandate evaluations" badge="AUTHORITY NONE">
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th scope="col">Evaluated / opportunity</th>
                <th scope="col">Venue / family</th>
                <th scope="col">State</th>
                <th scope="col">Reason codes</th>
                <th scope="col">Binding constraints</th>
                <th scope="col">Permitted allocation</th>
              </tr>
            </thead>
            <tbody>
              {evaluations.map((item) => {
                const constraints = records(item.constraints_evaluated).filter((check) => check.binding);
                return (
                  <tr key={text(item.evaluation_id)}>
                    <td>{text(item.evaluated_at)}<small>{text(item.opportunity_id)}</small></td>
                    <td>{text(item.venue)} / {text(item.market_family)}</td>
                    <td><span className={`state state-${text(item.state, "unknown").toLowerCase()}`}>{text(item.state)}</span></td>
                    <td><ReasonList codes={item.reason_codes} /></td>
                    <td>{constraints.map((check) => text(check.name)).join(" · ") || "none binding"}</td>
                    <td>{text(item.permitted_allocation_amount, "0")}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {!evaluations.length && <Empty title="No retained mandate evaluations">No Opportunity has been evaluated against a mandate yet.</Empty>}
      </Panel>
    </>
  );
}
