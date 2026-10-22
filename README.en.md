# xHarness

[中文](README.md) | English

[![CI](https://github.com/guessleej/xHarness/actions/workflows/ci.yml/badge.svg)](https://github.com/guessleej/xHarness/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-red.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

A plugin-based AI agent harness by xCloudinfo: **governance first, zero runtime dependencies**, any OpenAI-compatible endpoint, your data stays in your own room. About 17,500 lines of Python and 519 tests.

## Three steps

![Three steps](docs/images/xharness_quickstart_en.png)

```sh
git clone https://github.com/guessleej/xHarness.git && cd xHarness
python3 -m venv .venv && ./.venv/bin/pip install -e .
XHARNESS_BASE_URL=http://localhost:8080/v1 XHARNESS_MODEL=your-model ./.venv/bin/xharness web
```

## Architecture

![xHarness architecture](docs/images/xharness_architecture_en.png)

Five entrances share one core and one approval path; the core is a roughly 150-line `Context` and everything else is a plugin; every step can be stopped and traced.

## How people use it

![xHarness user flow](docs/images/xharness_user_flow_en.png)

## One tool call

![One tool call](docs/images/xharness_toolcall_en.png)

## Multiple agents

![Multiple agents](docs/images/xharness_agents_en.png)

## Model routing

![Model routing](docs/images/xharness_routing_en.png)

## Multi-node fleet

![Multi-node fleet](docs/images/xharness_fleet_en.png)

## Service usage report

![Service usage report](docs/images/xharness_report_en.png)

## Built-in tools

| Group | Tools |
|---|---|
| Files and commands | `read` `write` `edit` `glob` `grep` `bash` `terminal` `powershell` `job_*` |
| Agents | `subagent` `subagent_batch` `ralph` `workflow` `discussion` `list_agents` `send_message` `report` |
| Memory and history | `memory_*` `session_search` `session_read` `todo_write` `skill` `schedule_*` |
| Talking to people | `ask_user` `goal_done` |
| Code | `lsp` `security_scan` |
| Once configured | `web_search` `browser_open` `webfetch` and MCP tools |

## More

[Reference (every feature and setting)](docs/reference.md) · [Architecture notes](docs/architecture.md) · [SSDLC](docs/ssdlc.md) · [Security policy](SECURITY.md) · [Changelog](CHANGELOG.md) · MIT licence
