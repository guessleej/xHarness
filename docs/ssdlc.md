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
| Runaway agent (cost / DoS) | `max_turns` bound, per-command timeouts, output size caps on every tool | Mitigated |
| Model exfiltrates file contents to the endpoint | Inherent to the design: the model must see file contents to work. Point xHarness at an endpoint you trust (on-prem friendly by construction) | Accepted, disclosed |

### Residual risks (accepted and disclosed)

- **No sandbox.** `bash` runs with the invoking user's full privileges. The
  approval policy is the only guard; `--approve auto` on untrusted tasks is
  explicitly outside the threat model. A filesystem sandbox is on the roadmap.
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
  `llm.py` — scheme validated at construction).

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

## 3. Secure-usage guidance for deployers

- Interactive work: keep `approval = "prompt"`.
- Headless automation: run under a dedicated low-privilege account, in a
  container or VM, with the working directory scoped to the task.
- Point `base_url` at an endpoint you operate or trust; xHarness never
  contacts anything else.
- Treat `$XHARNESS_HOME` as sensitive; rotate provider keys through your
  normal secret-management process (they live only in env vars).
