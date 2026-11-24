"""Ontology Evaluation Observatory, public-research MVP.

Modules, in data-flow order:
    model    facts + comparability kernel (Status, Outcome, RunRecord, Cohort)
    catalog  authored TOML -> validated domain types       -- trust boundary
    adapter  adapter executable protocol + isolated invoke -- trust boundary
    runner   plan (pre-register) + execute                 -- writes runs
    store    append-only files, one writer per file        -- persistence
    scoring  executable checks, human review, resolve      -- writes decisions
    compare  cohorts, population, stats, coverage        -- pure derivation
    report   static site, publish snapshots, export        -- rendering
    cli      `obs` shell
"""
