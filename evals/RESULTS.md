# Eval baselines

Real runs of `evals/basic` on the on-prem fleet. Numbers are per case from the
telemetry plugin (fresh harness per case). Re-run with:

```sh
xharness eval evals/basic --json results.jsonl
```

## 2026-09-19 — gemma-4-26b-taide-zhtw (llama.cpp, MI50 farm, :8091)

| case | result | checks | tokens | time |
|---|---|---|---|---|
| 01-write-file | PASS | 3/3 | 1,997 | 4.5s |
| 02-edit-file | PASS | 3/3 | 3,176 | 4.0s |
| 03-search | PASS | 1/1 | 4,178 | 6.4s |
| 04-bash | PASS | 2/2 | 1,974 | 2.8s |
| 05-multi-step | FAIL | 1/3 | 5,401 | 12.1s |
| 06-instruction | PASS | 2/2 | 987 | 2.3s |

**5/6 (83%)**, 17,713 tokens, 32s total.

05 failed for a real reason: the model wrote `sum.py` but also **overwrote the
provided `data.csv`** with its own numbers (10..50) and reported 150 instead
of 100 — it did not use the data it was given. Both the `command` check
(`python3 sum.py | grep -q 100`) and the `answer_regex` check caught it.
