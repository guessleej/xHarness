# SSDLC: secure software development life cycle

English | [中文](ssdlc.zh.md)

This document is the security backbone of xHarness: the threat model the
architecture is designed against, the controls each life-cycle phase enforces,
and the residual risks we accept and disclose. Every feature change is expected
to pass through these phases; anything that alters a trust boundary must update
the threat model in the same pull request.

## 1. Threat model

### Assets

| Asset | Where it lives |
|---|---|
| Provider credentials | Environment variables only, named by `api_key_env` in config; never stored in the repo, config file, or session log |
| The user's filesystem | Reached through the `read`/`write`/`edit`/`bash` tools with the invoking user's privileges |
| Session logs | JSONL under `$XHARNESS_HOME/sessions/`; may contain file contents and command output |
| The model endpoint | The only network destination xHarness ever contacts |

### Trust boundaries

1. **Model output is untrusted input.** Tool calls arrive from an LLM whose
   context may contain adversarial content (prompt injection through files it
   read). The agent therefore treats every tool call as hostile: unknown names
   and malformed JSON become error results, and mutating tools must pass the
   approval policy before acting.
2. **Tool-read content is untrusted.** Anything `read`/`grep`/`bash` returns
   may attempt to steer the model. xHarness cannot make the model immune; it
   limits the blast radius via the approval policy and bounded tool outputs.
3. **The config file and plugin modules are trusted.** They are user-owned
   code and configuration, equivalent in power to running Python by hand.
4. **The network boundary is the configured endpoint.** `base_url` must be
   `http` or `https` (validated at adapter construction, so `file:` and custom
   schemes cannot reach the request path), and credentials are attached only to
   requests sent to that endpoint.

### Threats and mitigations

| Threat | Mitigation | Status |
|---|---|---|
| Prompt injection drives a destructive tool call | Approval policy: mutating tools (`bash`, `write`, `edit`) ask a human under `prompt` mode; interactive default is `prompt` | Mitigated; residual under `auto` (below) |
| Credential leakage | Keys resolved per request from env vars; never written to config, logs, or session files; sent only as a header to the validated endpoint | Mitigated |
| SSRF / scheme abuse via `base_url` | Scheme allow-list (`http`/`https`) enforced in `OpenAIAdapter.__init__`, with a regression test | Mitigated |
| Secrets committed to the repo | `xharness.toml` is gitignored; gitleaks scans every push and PR in CI | Mitigated |
| Supply-chain compromise | Zero runtime dependencies (standard library only); dev-only dependencies audited by pip-audit in CI weekly and on every push | Mitigated |
| Malicious or vulnerable session resume data | JSONL parsed line-by-line; corrupt lines skipped; content is data, never executed | Mitigated |
| Runaway agent (cost / DoS) | `max_turns` bound, per-command timeouts, output size caps on every tool, and the telemetry budget middleware (`max_total_tokens` / `max_llm_calls`) hard-stops the loop before the next model call | Mitigated |
| Malicious `AGENTS.md`/`CLAUDE.md` in an untrusted repo steers the agent (system-prompt injection) | Instruction files are repo content: loading them is a trust decision. Size-capped (24k chars), disabled with `--no-instructions` / `project_instructions = false`; the approval policy still gates mutating tools | Mitigated; residual in `auto` mode on untrusted repos |
| Web UI reached from another site (CSRF) or via DNS rebinding | Loopback binding by default; non-loopback `Host` rejected unless a token is configured; every POST requires the `X-XHarness-Client` header (custom headers force a CORS preflight that is never granted); no CORS headers; CSP on the page | Mitigated |
| Web UI exposed on the network | Binding a non-loopback address refuses to start without `--token`; the token travels only in `Authorization`; the SSE stream uses 60-second single-use tickets; no auth means no non-loopback binding | Mitigated |
| Subagent recursion / runaway fan-out | Children cannot see the `subagent` tools; `max_workers` bounds parallelism; per-child `max_turns`; every child side effect goes through the parent's approval policy; the telemetry budget counts children too | Mitigated |
| Poisoned memory persists a prompt injection across sessions | Memories are model-written content fed back into future system prompts, so they are a trust decision like `AGENTS.md`: writes and deletes go through the approval policy; only the size-capped index is injected, bodies load on explicit `memory_read`; every change is audited (`audit.jsonl`: agent, session, action) and `xharness memory audit` / the Web UI expose it; files are plain Markdown a human can delete; `[memory] enabled = false` turns it off | Mitigated; residual in `auto` mode |
| Multi-node hub compromised or node token leaked | Node tokens are referenced by environment variable name (`token_env`), never written to config; the hub forwards only `approvals` and `stop` with validated ids; nodes ignore `nodes` fan-out when asked by a hub (no recursive polling); a node must bind non-loopback with a token, and `XHARNESS_WEB_TOKEN_FILE` keeps it off the command line; plain http is for trusted LANs only — put TLS in front across boundaries | Mitigated; residual: LAN sniffing without TLS |
| Consolidation lets the model rewrite or delete memory wholesale | Plans are proposals: CLI dry-run by default, the agent tool returns the plan unless `apply` is set and then goes through approval; model proposals may only name memories that exist in the topic, everything else is dropped; the deterministic layer only merges near-duplicates into the newer entry; every merge/delete is audited as `consolidate` | Mitigated; residual in `auto` mode |
| Model exfiltrates file contents to the endpoint | Inherent to the design: the model must see file contents to work. Point xHarness at an endpoint you trust (on-prem friendly by construction) | Accepted, disclosed |

### Residual risks (accepted and disclosed)

- **Sandbox fallback.** `bash` runs under `sandbox-exec` (macOS) or `bwrap`
  (Linux) when available: writes confined to the working directory and temp
  dirs, network optionally cut. In `auto` mode without a backend, commands run
  with the invoking user's full privileges and the approval policy is the only
  guard — use `mode = "require"` where confinement must be a hard guarantee.
  `--approve auto` on untrusted tasks without a sandbox is explicitly outside
  the threat model.
- **MCP servers are code.** A configured MCP server runs as a child process
  with the user's privileges, outside the bash sandbox. Configuring one is a
  trust decision, same as installing a plugin; the approval policy still gates
  its non-read-only tools, and a misbehaving server is skipped at startup
  rather than trusted silently.
- **Plugins are code.** A plugin module runs with full interpreter privileges.
  Only load trusted paths.
- **Session logs are plaintext.** They inherit `$XHARNESS_HOME` directory
  permissions; protect that directory as you would shell history.

## 2. Life-cycle controls

### Phase 1 — Requirements and threat modeling

Every feature that adds a tool, a network destination, or a data store states
its trust-boundary impact. If a boundary changes, this document changes in the
same pull request.

### Phase 2 — Design

Rules the architecture enforces:

- Model output crosses into the harness only through the tool registry, never
  through `eval`/`exec` or shell interpolation of model text outside the
  `bash` tool's explicit contract.
- The approval decision belongs to the agent (policy), never to tools.
- Failures at trust boundaries fail closed: unknown provider scheme, missing
  service, or a broken plugin refuse to proceed rather than degrade silently.

### Phase 3 — Implementation

- **Standard library only** at runtime. Adding a runtime dependency requires a
  documented justification here and a supply-chain review.
- No secrets in the repository: credentials are env-var references
  (`api_key_env`), and the working config `xharness.toml` is gitignored.
- Input validation at boundaries: URL scheme allow-list, tool-argument JSON
  validation, session JSONL tolerant parsing, bounded outputs and timeouts.
- Every `# nosec` suppression carries a justification comment at the site and
  a corresponding entry in this document (current: `B404`/`B603`/`B607` in
  `tools/bash.py` — running commands is that tool's purpose; `B310` in
  `llm.py` — scheme validated at construction; `B404`/`B603` in `tools/security.py` —
  invoking the scanners is that tool's purpose, with fixed argv and timeouts; `B404`/`B603` in `evals.py` — `command` checks are case-author code run in a throwaway workspace; `B404`/`B603`/`B108` in `sandbox.py` — backend
  functional probes and the bwrap /tmp bind target, fixed probe argv).

### Phase 4 — Verification

CI gates on every push and pull request (`.github/workflows/`):

| Gate | Tool | Workflow |
|---|---|---|
| Unit tests, 3 Python versions | pytest | `ci.yml` |
| SAST | bandit (fails on any finding) | `security.yml` |
| Dependency audit | pip-audit | `security.yml` (also weekly, scheduled) |
| Secret scan, full history | gitleaks | `security.yml` |

A pull request that reduces coverage of a trust boundary (removing a
validation, widening a permission) must say so explicitly in its description.

### Phase 5 — Release

- Version bump in `pyproject.toml` and `xharness/__init__.py`, annotated tag.
- All CI gates green on the release commit; no suppressed findings without a
  documented justification.

### Phase 6 — Operations and response

- Vulnerabilities are reported privately per [SECURITY.md](../SECURITY.md):
  acknowledgement within 3 business days, fix or mitigation plan within 14
  days for confirmed issues.
- Security fixes land on `main` and are called out in the release notes with
  affected versions.

### Web UI

The UI runs conversations with the configured approval policy; `prompt` mode surfaces an approval card and blocks the tool for up to ten minutes waiting for a human, then denies. There is no user model: whoever can reach the port and pass the guards above is the operator. Do not expose it beyond a trusted network even with a token; put a reverse proxy with real authentication in front for anything shared.

The fleet view adds an operator brake: stopping a conversation sets the agent's stop flag (honoured before the next model call and before each remaining tool call) and denies every pending approval. An in-flight model request completes; nothing else runs after it.

Provider presets change no network posture by themselves: local presets stay on loopback, hosted presets are explicit opt-ins whose endpoints receive prompts and tool output. `xharness providers probe` only issues `GET /models` over http(s).

### Eval subsystem

Eval cases run the agent with automatic approval in a throwaway temp workspace, because a benchmark cannot pause for a human. Case files (prompts, setup files, `command` checks) are author-trusted, like test code; the model output they exercise is not. Evaluate untrusted models with the sandbox enabled, and never point a suite at a workspace you care about — the runner only ever creates and deletes its own temp directories.

## 3. Secure-usage guidance for deployers

- Interactive work: keep `approval = "prompt"`.
- Headless automation: run under a dedicated low-privilege account, in a
  container or VM, with the working directory scoped to the task.
- Point `base_url` at an endpoint you operate or trust; xHarness never
  contacts anything else.
- Treat `$XHARNESS_HOME` as sensitive; rotate provider keys through your
  normal secret-management process (they live only in env vars).
