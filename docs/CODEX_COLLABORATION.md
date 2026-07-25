# Codex collaboration

## Architecture

PatentViewer keeps the existing browser UI as the single visible interaction surface. Codex uses a repository-local stdio MCP server, which calls the localhost API. Routine work is expressed as semantic blocks and compiled into the existing `data-agent-id` actions.

```text
Codex -> tools/collaboration_mcp.py -> localhost API
                                      -> rule-based reads
                                      -> semantic block planner -> visible UI Bridge
```

The MCP layer contains no patent-analysis logic. Domain rules remain in Python, and existing UI event handlers remain responsible for UI mutations.

## Portable setup

After cloning the repository on another device:

```powershell
python tools/patent_viewer.py start
python tools/patent_viewer.py mcp-config --write
python tools/patent_viewer.py doctor
```

`mcp-config --write` creates `.codex/mcp.local.json` with absolute paths for that device. The file is ignored by Git. If Codex uses a different MCP configuration location, run `mcp-config` without `--write` and copy the printed `patent-viewer` entry there.

The MCP server reads `runtime/server-control.json`, so it automatically follows the port and per-process token selected by the local PatentViewer instance. No device-specific path or token is committed.

For a production device that starts with new PDFs and an empty data store, follow
[`PRODUCTION_BOOTSTRAP.md`](PRODUCTION_BOOTSTRAP.md). That procedure explicitly
forbids copying development PDFs, databases, analysis results, and runtime
artifacts to production.

For UI-only work that must not start the managed Ollama process:

```powershell
python tools/patent_viewer.py start --no-managed-ollama
```

## MCP tools

- `viewer_status`: server, environment, browsers, target counts, active commands
- `list_researches`, `get_dashboard`, `search_documents`: rule-based reads without an LLM
- `list_blocks`, `plan_block`: capability discovery and dry-run
- `execute_ui_block`: idempotent visible UI execution
- `get_command_status`, `control_command`: progress and pause/run/cancel
- `get_activity_log`: persistent JSONL audit
- `get_pipeline_overview`: pipeline state without opening the UI

Use `search_documents` before asking an LLM to inspect records. Use `plan_block` before a high-risk or heavy block.

## Semantic blocks

The catalog is in `schemas/collaboration-blocks.json`. Its current blocks cover research selection, compound filters, view and cell selection, patent/PDF opening, interpretation save, preflight, pipeline opening, preparation, execution, and control.

`execute_pipeline` requires `confirmation: RUN_LOCAL_LLM`. Every block reports a risk class, whether it writes, whether it is heavy, an estimated route, and the expanded UI actions.

## Safety and consistency

- The server binds to localhost by default.
- MCP mutations require the random token in `runtime/server-control.json`.
- Codex-originated application mutations require a running command claimed by the same visible browser and environment.
- Human UI requests retain their existing behavior.
- `idempotency_key` prevents duplicate command creation.
- Commands older than five minutes in the queue and commands whose browser disconnects are marked failed with structured codes.
- UI command and event history is appended to `runtime/collaboration/audit.jsonl`.
- The browser stores the current step in session storage and can continue a claimed command after reload.
- NORMAL/DEBUG selection is scoped to each browser session, so a Codex smoke run does not switch another device or tab.
- Target snapshots are hashed; unchanged heartbeats do not resend the full DOM target catalog.
- Command waiting uses bounded long polling rather than continuous sub-second empty requests.

## Diagnostics and tests

```powershell
python tools/patent_viewer.py doctor
python -m unittest discover -v
python tools/ui_visual_check.py
python tools/browser_smoke.py
```

`ui_visual_check.py` starts an isolated local server, uses the installed Edge or Chrome, captures wide and narrow DEBUG screenshots, compares them with `tests/visual_baselines/`, and restores the browser environment. Use `--update-baselines` only after an intentional UI change.

`browser_smoke.py` requires a running server and visible browser. It exercises filters, threat-cell selection, patent detail, PDF preview when available, and the LLM preflight dialog through semantic blocks. It restores filters and the selected browser's original environment in a `finally` path. When multiple clients are connected, pass `--client-id`; an ambiguous target or missing browser is a failure unless `--allow-skip` is explicitly supplied. `scripts/debug.ps1` runs screenshot regression and the strict visible-browser smoke after the unit and data-contract checks.

The collaboration protocol is versioned independently from the HTTP application version. Its action Schema is `schemas/collaboration-command.schema.json`.
