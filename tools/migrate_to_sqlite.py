from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1].resolve()
sys.path.insert(0, str(ROOT / "src"))

from patent_viewer.domain import Repository
from patent_viewer.storage import SCHEMA_VERSION


def verify(repository: Repository, environment: str) -> dict:
    store = repository.store(environment)
    statistics = store.statistics()
    with store.connect() as connection:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        missing_payloads = int(
            connection.execute(
                "SELECT COUNT(*) FROM documents WHERE payload_json IS NULL OR payload_json=''"
            ).fetchone()[0]
        )
        invalid_vectors = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM embeddings
                WHERE length(technology) != dimensions * 4
                   OR length(problem) != dimensions * 4
                """
            ).fetchone()[0]
        )
        orphan_vectors = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM embeddings e
                LEFT JOIN documents d
                  ON d.research_id=e.research_id AND d.patent_id=e.patent_id
                WHERE d.patent_id IS NULL
                """
            ).fetchone()[0]
        )
    return {
        **statistics,
        "integrity": integrity,
        "missing_payloads": missing_payloads,
        "invalid_vectors": invalid_vectors,
        "orphan_vectors": orphan_vectors,
        "ok": (
            statistics["schema_version"] == SCHEMA_VERSION
            and integrity == "ok"
            and missing_payloads == 0
            and invalid_vectors == 0
            and orphan_vectors == 0
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import PatentViewer CSV/JSON state into the local SQLite databases"
    )
    parser.add_argument(
        "--environment",
        choices=("normal", "debug", "all"),
        default="all",
    )
    parser.add_argument(
        "--artifacts",
        action="store_true",
        help="also import pipeline/checkpoint/audit JSON; embeddings always become float32 BLOBs",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="reindex research sources even when their source signature is unchanged",
    )
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    repository = Repository(ROOT)
    environments = ("normal", "debug") if args.environment == "all" else (args.environment,)
    reports = []
    for environment in environments:
        migration = None
        if not args.verify_only:
            migration = repository.sync_environment(
                environment,
                force=args.force,
                import_artifacts=args.artifacts,
            )
        reports.append(
            {
                "environment": environment,
                "migration": migration,
                "verification": verify(repository, environment),
            }
        )
    ok = all(report["verification"]["ok"] for report in reports)
    payload = {"ok": ok, "reports": reports}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for report in reports:
            verification = report["verification"]
            print(
                f"{report['environment']}: schema={verification['schema_version']} "
                f"documents={verification['documents']} embeddings={verification['embeddings']} "
                f"artifacts={verification['artifacts']} integrity={verification['integrity']}"
            )
        print("SQLite migration verified." if ok else "SQLite migration verification failed.")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except sqlite3.Error as exc:
        print(f"SQLite migration failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
