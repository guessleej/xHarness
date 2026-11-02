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

## 2026-11-02 — the resident agent (superagent)

Modelled on DeepSeek's "Exploring Super-agent Design": a resident main Session
decides what comes in (accept, attach, stop, ignore), one worker per ticket does
the work, and follow-ups on a ticket's threads go straight to its worker.
granite-4.2-30b Q6_K on the MI50 farm (two llama.cpp instances, :8093 and
:8095, thinking off). Scripts are kept outside the repo, like `bench/`.
Each configuration ran once or twice; read the numbers as one sample, not as
a distribution. The article's experiments are far larger.

### Live run: a GitHub issue to a pull request

A fake GitHub API and a local git origin; everything else is real. An issue
mentions the agent and asks for a README typo fix.

| | |
|---|---|
| wall | 174 s |
| tokens | 105,946 |
| main Session accept | 44 s after the issue arrived |
| writes approved | 3 (acknowledgement, push + pull request, result) |
| result | commit `Fix README typo`, pull request #101 opened by the system, result posted on the issue |

The worker never saw the token: the system pushed and opened the pull request.
Two earlier live runs failed and each led to a fix in code (see below).

### Ablation: one group chat stream

53 messages over about 2.5 minutes: 6 work requests (each a `do_work` tool that
only sleeps 30–60 s), 2 follow-ups ("also add the date"), 1 stop for request 5,
36 chatter lines and 8 CI bot lines. Completion is the time from the request to
the result message in the same thread. Limit 25 minutes. Code at `c19a7e3`.

| mode | seed | delivered | P50 | slowest | tokens | stop honoured |
|---|---|---|---|---|---|---|
| superagent | 1 | 5/5 | 10.8 min | 11.6 min | 282,928 | no |
| superagent | 2 | 5/5 | 12.1 min | 13.1 min | 327,585 | no |
| no direct delivery | 1 | 5/5 | 10.6 min | 11.6 min | 349,747 | yes |
| no direct delivery | 2 | 5/5 | 11.5 min | 12.2 min | 390,592 | yes |
| one conversation, neutral prompt | 1 | 0/5 | – | – | 65,598 | yes |
| one conversation, told to delegate | 1 | 0/5 | – | – | 72,302 | no |
| superagent, code `9299140` | 1 | 5/5 | 8.9 min | 9.0 min | 213,839 | yes |
| superagent, code `9299140` | 2 | 5/5 | 9.0 min | 10.3 min | 233,777 | no |

- The single conversation finished the work fastest (each request done 1.4–3.9
  minutes after it was posted) but never used its `reply` tool: it wrote the
  result as plain text, which reaches nobody in a group chat. The two-layer
  agent delivers because the system reminds the main Session when a ready
  result or a decision is still owed.
- At this load, direct delivery did not make results arrive sooner; it saved
  about 17% of tokens (611k against 740k over two seeds). The article reports a
  speed gain under much heavier load; we have not reproduced that.
- Results waited behind the main Session, not behind the work: a ticket done
  at 144 s was announced at 647 s. The main Session wrote long explanations at
  the end of turns (60–116 s each) that nobody reads, and once polled a working
  ticket's status 17 times in one turn.
- The stop failed in two ways. With seed 1 the request and the stop reached the
  main Session in one batch and it accepted both as one ticket, showing the
  worker only the stop. With seed 2 the stop queued behind the request's
  decision and reached the main Session after the work was done.
- After the fixes (last two rows, code `9299140`) results arrived 2–3 minutes
  sooner and used about 30% fewer tokens; the main Session stopped polling
  after its third ask. The stop that queued behind the request now went to the
  new ticket and the worker dropped the work. The stop accepted in one batch
  with its request still failed: the worker was shown both messages, oldest
  first, and did the work anyway. That is the model's judgement, not a
  delivery problem.
- In one earlier run of the no-direct mode the main Session ignored every
  request ("do_work is not defined"), with the same prompt that accepted them
  in the other runs. One run per mode is not enough to compare modes.

### Batching in the main Session

Candidates (requests, thanks, greetings) arrive at a fixed rate for 4 minutes,
then 5 minutes to drain; workers are stubs. Seconds per decision = total
main-Session turn time / decisions recorded.

| rate / min | candidates | decided, no cap | s / decision, no cap | decided, cap 8 | s / decision, cap 8 |
|---|---|---|---|---|---|
| 1 | 2 | 2 | 24.9 | 2 | 37.8 |
| 5 | 18 | 18 | 13.3 | 19 | 10.5 |
| 10 | 45–46 | 45 | 6.2 | 46 | 7.7 |
| 20 | 90 | 15 | 20.8 | 57 | 8.5 |
| 40 | 158 | 40 | 12.1 | 33 | 15.0 |

- Up to 10 a minute, batching works as the article describes: the cost per
  decision falls as batches grow.
- Without a cap, a batch of about 20 left most candidates undecided even after
  the reminder. With at most 8 items per turn, 63% were decided at 20 a minute
  instead of 17%. At 40 a minute one granite main Session cannot keep up with
  or without the cap.
- Every decision in this test was "ignore", requests included: the requests
  ("compile report 3") name no source, and the main Session said so. That is
  the test's wording, not a bug, but it means the test measures deciding, not
  accepting.

### Defects these runs found, all fixed in code with a test

1. A worker reported ready with commits but no pull request: ready is refused
   until the change has its own pull request.
2. The main Session answered in plain text: an owed result or decision is named
   again in the same turn.
3. A follow-up attached just before a ready report came back after every later
   end (looped 5 times): once any copy of a message was read, it was read.
4. On a resumed ticket the follow-up arrived one turn after the opening text,
   after the worker had already reported ready: the opening text now carries
   the waiting messages.
5. Batches of 20 or more were mostly left undecided: at most 8 items per turn.
6. Status polling: the answer says the ticket's stage and that reports arrive
   by themselves; a third ask in one turn is refused.
7. The stop's two failures above: a ticket opens with every message its
   decision covered, oldest first, and a person's message queued on the
   ticket's thread while the main Session decided goes to the new ticket.
8. The ablation script itself compared thread names in two formats and
   reported 0 delivered for the two-layer modes; the first ablation numbers
   were discarded.
