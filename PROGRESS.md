# Progress

- 2026-11-07: milestone 1 done: scanner, rules, shell generators, rewriter, undo scripts; 302 tests (table + bash/dash behavioural + real git force-push scenarios) green.
- 2026-11-07: milestone 2+3 done: receipts store, hooks, /swaps /unswap + hermes safe-swap CLI, integration test through the real plugin loader and handle_function_call; hermes plugins validate passes (security scan: safe); hook latency p50 ~1.7 ms with a DB write.
- 2026-11-07: milestone 4 done: README (transcript from a real run), demo/demo.sh, catalog-entry.yaml (parses with Hermes' catalog loader).
- 2026-11-07: review pass: hardline sweep (3.4k variants), per-kind restore scripts, private refs/safe-swap snapshots, approval-aware runner, CDPATH fix; 3429 tests green; validate passes. Latency p50 0.8 ms (no rule) / 1.5-1.8 ms (rewrite incl. DB write), max 15 ms.
