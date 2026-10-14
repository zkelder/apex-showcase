# Limitations and Future Work

## What this project is not

- Not evidence of trading edge, profitability, calibration or historical
  performance. Fixture outcomes demonstrate correct mechanics, not returns.
- Not a live trading system. There is no real-money execution path.
- Not a hosted service. The public demo is static, read-only and synthetic.

## Known limitations

- **Stale legacy demo commands.** `operator-demo-seed`, `operator-console-demo` and
  `paper-portfolio-demo` (in `scripts/dev.py` / `apex.cli`) predate the PAPER trial authorization
  requirement and fail with a missing-row error on a fresh database. They are unsupported; use
  `scripts/demo/regenerate_demo_data.sh` for console data.
- **Source-available dependency.** `vectorbt` is Apache-2.0 with the Commons Clause (see README).
- **Single machine, single operator.** Availability depends on one host and
  its container runtime. There is no high availability, no automatic restart
  policy, and no offsite backup strategy; backups are local logical dumps
  verified by disposable restore.
- **Exact schema-revision equality.** The compatibility guard is strict and
  offers no rolling-deploy window.
- **Fixture-scale demo data.** The demo and offline outcomes use small
  synthetic inputs. Scale behaviour on large genuine datasets is not part of
  the public demonstration.
- **Retrospective history is not vintage data.** Provider history captured
  after the fact cannot prove what was knowable at the time. Universe
  membership is today's cohort applied backwards where noted, and such
  datasets carry explicit survivorship and point-in-time caveats.
- **Simulation fidelity.** Paper fills use declared models (next-bar or
  later-retained-evidence rules, modeled costs). Visible order-book depth is
  evidence, not a fillable quote. Real-venue latency, queue position, partial
  fills and market impact are not modeled.
- **Some evidence families are structurally scarce.** Where a precondition
  rarely holds in public data, the system correctly produces few or no
  candidates; that is reported, not forced.
- **Large single modules.** The operator API and development runner are large
  single files. Splitting them by route family and command family would
  improve navigability without changing behaviour.
- **Domain breadth.** The repository spans prediction markets, crypto,
  securities and options research. Depth varies by area; the financial core
  (decisions, risk kernel, ledger, persistence, operating cycle) is the most
  rigorously tested.
- **Statistical caution.** Research-governance machinery (trial ledgers,
  multiple-testing controls, negative controls) reduces but cannot eliminate
  false discoveries; nothing here validates a strategy.

## Future work

- Split large modules and generate API documentation from route metadata.
- Multi-host or managed deployment with restart policy and offsite backups.
- Rolling-compatible migrations behind a compatibility window, with
  expand/contract migration discipline.
- Richer fill and market-impact models for paper simulation.
- Vintage-accurate point-in-time data acquisition going forward.
- A containerised end-to-end demo (database, API and console seeded from
  fixtures) as an alternative to the static demo.
- Continuous integration running the offline and persistence suites on every
  change.
