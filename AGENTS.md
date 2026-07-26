# PatentViewer Codex instructions

## First-time environment bootstrap

When the user asks to set up, launch, bootstrap, or make this system runnable in a newly cloned workspace—including a short request such as `このシステムを実行できる環境を立ち上げて`—treat it as a request for the complete bootstrap described below. Do not stop after merely starting the HTTP server.

Before taking setup actions:

1. Read `docs/ENVIRONMENT_MIGRATION_RUNBOOK.md` completely.
2. Read the startup and data-boundary sections of `README.md` and `docs/PRODUCTION_BOOTSTRAP.md`.
3. Inspect the current scripts and command help instead of assuming the documents are current.
4. Confirm the repository revision and that the worktree does not contain unexpected changes.

Complete the applicable phases of the migration runbook in order. The target state is:

- a supported Python 3.10+ interpreter and all `requirements.txt` dependencies are available;
- PatentViewer starts on localhost and `/api/health` reports success;
- the initial NORMAL SQLite database is created and passes verification;
- a browser can open the PatentViewer UI;
- Ollama and the configured generation, rescue, and embedding models are available when local analysis is required;
- `.codex/mcp.local.json` is generated for this clone;
- `doctor`, unit/API tests, and the practical browser checks that the environment supports have been run;
- the initial NORMAL environment contains zero researches and zero documents until the user supplies approved production inputs.

Do not copy or commit development-machine PDFs, CSVs, research definitions, SQLite databases, model responses, embeddings, logs, runtime tokens, or generated artifacts. Do not create a production research or start bulk LLM analysis unless the user separately supplies or identifies approved inputs and authorizes that action.

If a system prerequisite, network download, permission, Codex restart, or user decision is required, perform all safe work that remains and then report the exact blocker and command needed. Never report bootstrap completion while a required check is failing or skipped without explanation.

At handoff, report:

- the Git revision used;
- Python, browser, Ollama, and model readiness;
- application URL and health result;
- SQLite verification result and initial research/document counts;
- MCP configuration and whether Codex must be restarted or reconnected;
- tests and browser checks run;
- any remaining blocker before production data can be registered.
