# kintone App Record Management Agent

AI agent for creating and updating kintone app records, built with Agentic Star.

> **Category**: Cat 2 (a fixed multi-step pipeline for one job-to-be-done)
> **Industry**: Common
> **Template ID**: CMN-C2-276

## Overview

Turns a plain-language instruction into a single, confirmed action against a Cybozu kintone
app record.

Give it a sentence like *"look up record 101 in kintone app 17 and summarize what is on file"*
and it works out which operation you meant, resolves the target app id and record number from the
wording or from caller-supplied data, assembles the matching kintone REST API request, and hands
back a confirmation carrying the affected record id and a `kintone://` reference.

Three intents are supported — look up, create and update. Create and update write to the app, so
the agent never invents a target: a lookup or update with no explicit record number fails with a
clear error rather than touching whatever a guess would have landed on, and an unclassifiable
request falls back to the read-only lookup, never to a write.

Intent classification is deterministic (keyword based). Field extraction is deterministic
(regex/pattern based) with an optional Azure OpenAI enhancement for record titles and free-form
record attributes phrased in natural language; any LLM failure — missing key, API error,
malformed response — degrades silently back to the deterministic result, so the pipeline still
runs and is fully testable without a language model configured. Out of the box it ships with a
network-free stub transport that returns the documented kintone response shapes, which makes the
template runnable end to end before you connect a real tenant; injecting a live HTTP transport
and providing an API token is all that is needed to go live.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. The agent imports its base classes from the framework package at start-up, so without that
package installed and configured, import and graph compile fail outright rather than leaving the
agent running in a partially working state. This is intentional — a half-running agent is worse
than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit and boundary tests
config/       agent.yaml (registry manifest) and config.yaml (runtime parameters)
docs/         design document and test specification
```

See `docs/` for the design document and the test specification.

## Customising

1. Set your tenant's kintone base URL under `kintone:` in `config/config.yaml`.
2. Inject live `get`/`post`/`put` transports when constructing `KintoneClient`
   (`src/services/kintone_client.py`) and provision `KINTONE_API_TOKEN` through the secrets
   provider — the shipped default is a deterministic, network-free stub.
3. Adjust the intent keywords and field-extraction patterns under `src/nodes/` for your own
   wording and record schema.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.

