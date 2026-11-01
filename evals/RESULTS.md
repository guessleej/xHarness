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

## 2026-11-01 — granite-4.2-30b Q6_K (llama.cpp, MI50 farm, :8095, thinking off)

Code: 1.18.0 with separate workspaces and the task board (branch `feat/multiagent`).

| case | result | checks | tokens | time |
|---|---|---|---|---|
| 01-write-file | PASS | 3/3 | 12,330 | 52.5s |
| 02-edit-file | PASS | 3/3 | 24,914 | 57.1s |
| 03-search | PASS | 1/1 | 12,378 | 52.1s |
| 04-bash | PASS | 2/2 | 12,271 | 51.1s |
| 05-multi-step | PASS | 3/3 | 25,222 | 63.1s |
| 06-instruction | PASS | 2/2 | 6,154 | 50.8s |

**6/6 (100%)**, 93,269 tokens, 327s total. Tokens are higher than the
2026-09-19 run because the tool list is longer now (desktop, board and the
1.10–1.17 tools are all in the prompt).

## 2026-11-01 — several agents on one task board

`bench/mabench.py` (kept outside the repo): a fresh git repository with 8 stub
modules and their tests; the 8 tasks are posted on the board, then K agents run
at once. Each agent works in its own worktree, loops board_list → board_claim →
edit → board_note with `verify` (the system runs the test) → board_release,
and when it stops its change is applied only if `python3 check_done.py`
(every non-stub module's test) passes. "passing" is counted afterwards on the
final working directory, not taken from the agents.

Same model as above, two llama.cpp instances (:8093, :8095, agents alternate),
each serving one or two requests at a time — beyond four agents, requests
queue at the model. Each agent gets 60 turns, so the total budget grows with K.

| agents | passing | wall | tokens | model calls | claims | system-verified OK | workspaces |
|---|---|---|---|---|---|---|---|
| 1 | 6/8 | 275s | 610,186 | 60 | 6 | 6 | 1 integrated |
| 2 | 8/8 | 256s | 734,813 | 81 | 8 | 8 | 2 integrated |
| 4 | 8/8 | 534s | 677,761 | 85 | 8 | 8 | 4 integrated |
| 8 | 8/8 | 536s | 913,579 | 121 | 8 | 8 | 8 integrated |

- One agent ran out of its 60 turns after 6 tasks; two agents finished all 8.
- No task was claimed twice (claims = tasks), no workspace conflicted or failed
  its check, and every success note was verified by the system.
- Four and eight agents took twice as long as two: the model, not the harness,
  is the bottleneck (at most four llama.cpp slots in total). Eight agents also spent 35%
  more tokens than four for the same 8 tasks — agents with nothing left to claim
  still pay for board_list calls. On this hardware, two agents is the best value.
