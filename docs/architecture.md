# xHarness architecture

English | [中文](architecture.zh.md)

Notes for contributors. The README covers usage; this file covers design.

## The kernel: Context, Plugin, Harness

`xharness/context.py` is the whole kernel, about 150 lines:

- **`Context`** holds three registries: named **services** (`provide`/`get`/`optional`), **event listeners** (`on`/`emit`, failures contained and logged), and **middleware chains** (`intercept`/`invoke`) for interceptable calls.
- **`Plugin`** is a name plus `apply(ctx, config)`. Applying a plugin is the only way anything enters the context.
- **`Harness`** applies plugins in order. While a plugin applies, every registration's disposer is collected into that plugin's scope; `unload(id)` runs the scope in reverse, so a plugin's contribution unwinds exactly. A plugin that raises during `apply` is unwound immediately and mounts nothing.

There is no privileged core: the model adapter, every tool, the session log, and the agent's tool wiring are mounted the same way user plugins are. `presets.build_harness()` is just a default composition order, not special machinery.

## Seams

| Seam | Kind | Contract |
|---|---|---|
| `llm` | service | `stream(messages, tools, on_delta) -> AssistantTurn`. Any object with this shape serves; tests mount a fake. |
| `tools` | service | `ToolRegistry`: `register` (returns disposer), `list`, `get`. |
| `session` | service (optional) | `SessionLog.append(event)`. Absent means no persistence; the agent checks with `ctx.optional`. |
| `llm/stream` | middleware | Wraps every model call. Payload is `{"messages", "tools"}`; the terminal performs the real request. Caching, logging, routing, and request rewriting all live here. |
| `agent/task-start`, `agent/task-end`, `plugin/loaded`, `plugin/unloaded` | events | Observational; listener failures never break the host. |

## The agent loop

`Agent.run(task)` is a plain loop: append the user message, call the model through `llm/stream`, execute any tool calls through the registry, append tool results, repeat until the model answers without tool calls or `max_turns` hits. Malformed tool calls (unknown name, invalid JSON arguments) become error results fed back to the model rather than crashes, so the model can self-correct.

Approval is policy, not tool logic: mutating tools call `tool_ctx.approve(summary)` before acting, and the agent decides what that means (`auto` approves, `prompt` asks the human, `prompt` without a prompter denies). Tools never read the policy directly.

## Sessions

`SessionLog` is an append-only JSONL file per session under `$XHARNESS_HOME/sessions/`. Events are `message` and `tool-result` records with ISO timestamps. `--resume` rebuilds the message list with `messages_from_events`; corrupt lines are skipped rather than failing the whole session. Append-only means a crash can lose at most the event being written, never rewrite history.

## The adapter

`OpenAIAdapter` speaks the OpenAI chat-completions wire format with `stream: true`, parsing SSE with the standard library (`urllib` + line iteration). Tool-call deltas are accumulated by index; usage comes from the final chunk when the server sends `stream_options.include_usage`. Credentials resolve per request from an environment variable named in config (`api_key_env`), so no secret lives in the config file.

## Comparison with a large harness

For scale, here is xHarness next to a large plugin-based harness (DeepSeek's dsh, 2026-08 snapshot):

| | dsh | xHarness |
|---|---|---|
| Language | TypeScript | Python (3.11+, stdlib only) |
| Size | ~565k lines, 227 packages | ~1.9k lines, 1 package |
| Plugin runtime | Cordis (services, typed events, HMR, fibers) | `Context` (services, events, middleware, scoped disposal) |
| Composition | profiles, bundles, layered YAML patches | one preset function + a TOML plugin list |
| Providers | adapter registry, catalogs, model discovery | one OpenAI-compatible adapter |
| Sandbox | bwrap / Landlock / Seatbelt / ACL, fail-closed | Seatbelt (macOS) / bwrap (Linux); `require` mode fail-closed, `auto` falls back to approval policy |
| UI | web app | CLI |

The claim is not parity. The claim is that the architectural core — plugins over a shared context with reversible effects, an interceptable model-call seam, an append-only session log — fits in a package a single developer can audit, which matters for on-prem deployments where every line that touches the network must be reviewable.

## Known limitations

- The `bash` sandbox needs a backend (`sandbox-exec` on macOS, `bwrap` on Linux); in `auto` mode without one, approval policy is the only guard. Use `mode = "require"` for a hard guarantee, and do not run untrusted tasks with `--approve auto` on an unsandboxed host.
- One provider per run; no mid-session model switching.
- Sync, single-threaded: one model call and one tool at a time.
- `resume` replays what the log holds; nothing more granular than whole messages.
