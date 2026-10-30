# Verification procedure

Python 3.13.13 on Windows 11; no third-party runtime packages. Run from this repository root:

```sh
python -B -m unittest discover -s tests -v
python -B scripts/research_tools.py study samples/study.json --holding-bars 1 --cost-bps 10
python -B scripts/research_tools.py journal samples/journal.json --kind paper --group-by setup
```

Fifteen original synthetic tests cover next-open entry and costs, short-side calculations, horizon/overlap/tail handling, empty-sample semantics, malformed/reordered/interpolated bars, input immutability, separation of episode kinds, missing risk, simultaneous-close ordering, duplicate records, strict JSON and reproducibility hashes.

Both example commands use invented data. The journal example deliberately loses 1.50 units overall. This is a fixture check, not an investment result. Tests establish the exercised software behavior; they do not establish market-data provenance, execution feasibility, production readiness or repeatable profitable decisions.

The wider private system's qualification evidence is not bundled or counted as tests of this limited release. No network, account or paid model call is necessary to reproduce these checks.
