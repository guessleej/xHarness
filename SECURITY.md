# Security Policy

English | [中文](SECURITY.zh.md)

## Supported versions

Only the latest release on `main` receives security fixes.

## Reporting a vulnerability

Do not open a public issue for security problems. Report privately to
**jefflee@cloudinfo.com.tw** with:

- a description of the issue and its impact,
- steps or a proof of concept to reproduce,
- the commit or version affected.

You will receive an acknowledgement within 3 business days and a fix or
mitigation plan within 14 days for confirmed issues.

## Scope notes

- The `bash`, `write`, and `edit` tools execute model-chosen actions **by
  design**, guarded by the approval policy. Running untrusted tasks with
  `--approve auto` is outside the threat model; see
  [docs/ssdlc.md](docs/ssdlc.md) for the full threat model and residual risks.
- User plugins execute arbitrary code by design; only load plugin modules you
  trust.
