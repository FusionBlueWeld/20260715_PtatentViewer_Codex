from __future__ import annotations

import json
import sqlite3
import sys
from array import array
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator


SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def encode_vector(values: Iterable[float]) -> bytes:
    packed = array("f", (float(value) for value in values))
    if sys.byteorder != "little":
        packed.byteswap()
    return packed.tobytes()


def decode_vector(value: bytes) -> list[float]:
    packed = array("f")
    packed.frombytes(value)
    if sys.byteorder != "little":
        packed.byteswap()
    return packed.tolist()


class ClosingConnection(sqlite3.Connection):
    """Make ``with store.connect()`` close handles on Windows as well as commit."""

    def __exit__(self, exc_type, exc_value, traceback):
        result = super().__exit__(exc_type, exc_value, traceback)
        self.close()
        return result


class SQLiteStore:
    """Queryable application state.

    PDFs and large extracted text remain filesystem objects. SQLite owns the
    searchable document index, current analysis state, compact binary vectors,
    and imported JSON artifacts.
    """

    def __init__(self, root: Path, environment: str):
        if environment not in {"normal", "debug"}:
            raise ValueError("environment must be normal or debug")
        self.root = root.resolve()
        self.environment = environment
        self.path = self.root / "runtime" / environment / "patent_viewer.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path, timeout=30, factory=ClosingConnection
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS researches (
                    research_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    company_technology TEXT NOT NULL DEFAULT '',
                    lifecycle_status TEXT NOT NULL DEFAULT 'active',
                    analysis_stale INTEGER NOT NULL DEFAULT 0,
                    source_signature TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    input_audit_json TEXT NOT NULL,
                    technology_map_json TEXT NOT NULL DEFAULT '{}',
                    csv_history_json TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS documents (
                    research_id TEXT NOT NULL,
                    patent_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    pdf TEXT NOT NULL,
                    publication_number TEXT NOT NULL,
                    title TEXT NOT NULL,
                    applicant TEXT NOT NULL,
                    category TEXT NOT NULL,
                    application_number TEXT,
                    application_date TEXT,
                    registration_number TEXT,
                    registration_date TEXT,
                    year INTEGER,
                    year_source TEXT,
                    status TEXT NOT NULL,
                    source_status TEXT,
                    legal_status_category TEXT NOT NULL,
                    tags_text TEXT NOT NULL DEFAULT '',
                    pdf_available INTEGER NOT NULL DEFAULT 0,
                    analysis_state TEXT NOT NULL DEFAULT 'pending',
                    skip_reason TEXT,
                    skip_detail TEXT,
                    similarity INTEGER,
                    concept_level INTEGER,
                    tech_summary TEXT,
                    problem_summary TEXT,
                    reasoning TEXT,
                    tech_cluster TEXT,
                    problem_cluster TEXT,
                    tech_cluster_id INTEGER,
                    problem_cluster_id INTEGER,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (research_id, patent_id),
                    FOREIGN KEY (research_id) REFERENCES researches(research_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS documents_research_position
                    ON documents(research_id, position);
                CREATE INDEX IF NOT EXISTS documents_research_year
                    ON documents(research_id, year);
                CREATE INDEX IF NOT EXISTS documents_research_legal
                    ON documents(research_id, legal_status_category);
                CREATE INDEX IF NOT EXISTS documents_research_analysis
                    ON documents(research_id, analysis_state);
                CREATE INDEX IF NOT EXISTS documents_research_threat
                    ON documents(research_id, similarity, concept_level);
                CREATE INDEX IF NOT EXISTS documents_research_clusters
                    ON documents(research_id, tech_cluster_id, problem_cluster_id);

                CREATE VIRTUAL TABLE IF NOT EXISTS document_fts USING fts5(
                    research_id UNINDEXED,
                    patent_id UNINDEXED,
                    publication_number,
                    title,
                    applicant,
                    category,
                    tags,
                    tokenize = 'unicode61'
                );

                CREATE TABLE IF NOT EXISTS organizations (
                    research_id TEXT NOT NULL,
                    organization_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    document_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (research_id, organization_id),
                    FOREIGN KEY (research_id) REFERENCES researches(research_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS document_organizations (
                    research_id TEXT NOT NULL,
                    patent_id TEXT NOT NULL,
                    organization_id TEXT NOT NULL,
                    PRIMARY KEY (research_id, patent_id, organization_id),
                    FOREIGN KEY (research_id, patent_id)
                        REFERENCES documents(research_id, patent_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS document_organizations_lookup
                    ON document_organizations(research_id, organization_id, patent_id);

                CREATE TABLE IF NOT EXISTS embeddings (
                    research_id TEXT NOT NULL,
                    patent_id TEXT NOT NULL,
                    dimensions INTEGER NOT NULL,
                    technology BLOB NOT NULL,
                    problem BLOB NOT NULL,
                    input_sha256_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (research_id, patent_id),
                    FOREIGN KEY (research_id, patent_id)
                        REFERENCES documents(research_id, patent_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                    research_id TEXT NOT NULL,
                    patent_id TEXT NOT NULL,
                    artifact_type TEXT NOT NULL,
                    run_id TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL,
                    source_path TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (research_id, patent_id, artifact_type, run_id)
                );
                CREATE INDEX IF NOT EXISTS artifacts_lookup
                    ON artifacts(research_id, patent_id, artifact_type);

                CREATE TABLE IF NOT EXISTS interpretations (
                    research_id TEXT NOT NULL,
                    patent_id TEXT NOT NULL,
                    note TEXT NOT NULL,
                    source TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (research_id, patent_id)
                );
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, utc_now()),
            )

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _loads(value: str | None, default: Any) -> Any:
        if not value:
            return default
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default

    def source_signature(self, research_id: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT source_signature FROM researches WHERE research_id = ?",
                (research_id,),
            ).fetchone()
        return str(row["source_signature"]) if row else None

    def replace_research(
        self,
        research_id: str,
        *,
        config: dict[str, Any],
        company_technology: str,
        input_audit: dict[str, Any],
        csv_history: list[dict[str, Any]],
        technology_map: dict[str, Any],
        source_signature: str,
        documents: list[dict[str, Any]],
        organizations: dict[str, str],
    ) -> None:
        lifecycle = config.get("lifecycle", {})
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO researches(
                    research_id, name, description, company_technology,
                    lifecycle_status, analysis_stale, source_signature,
                    config_json, input_audit_json, technology_map_json,
                    csv_history_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(research_id) DO UPDATE SET
                    name=excluded.name,
                    description=excluded.description,
                    company_technology=excluded.company_technology,
                    lifecycle_status=excluded.lifecycle_status,
                    analysis_stale=excluded.analysis_stale,
                    source_signature=excluded.source_signature,
                    config_json=excluded.config_json,
                    input_audit_json=excluded.input_audit_json,
                    technology_map_json=excluded.technology_map_json,
                    csv_history_json=excluded.csv_history_json,
                    updated_at=excluded.updated_at
                """,
                (
                    research_id,
                    str(config.get("name", research_id)),
                    str(config.get("description", "")),
                    company_technology,
                    str(lifecycle.get("status", "active")),
                    int(bool(lifecycle.get("analysis_stale", False))),
                    source_signature,
                    self._json(config),
                    self._json(input_audit),
                    self._json(technology_map),
                    self._json(csv_history),
                    utc_now(),
                ),
            )
            existing = {
                str(row["patent_id"]): row
                for row in connection.execute(
                    "SELECT patent_id, payload_json FROM documents WHERE research_id = ?",
                    (research_id,),
                )
            }
            incoming = {str(item["id"]) for item in documents}
            removed = set(existing) - incoming
            if removed:
                placeholders = ",".join("?" for _ in removed)
                connection.execute(
                    f"DELETE FROM documents WHERE research_id = ? AND patent_id IN ({placeholders})",
                    (research_id, *sorted(removed)),
                )
            connection.execute("DELETE FROM document_fts WHERE research_id = ?", (research_id,))
            connection.execute("DELETE FROM document_organizations WHERE research_id = ?", (research_id,))
            connection.execute("DELETE FROM organizations WHERE research_id = ?", (research_id,))

            for position, item in enumerate(documents):
                tags = item.get("tags", [])
                tags_text = " ".join(str(tag) for tag in tags)
                connection.execute(
                    """
                    INSERT INTO documents(
                        research_id, patent_id, position, pdf, publication_number,
                        title, applicant, category, application_number,
                        application_date, registration_number, registration_date,
                        year, year_source, status, source_status,
                        legal_status_category, tags_text, pdf_available,
                        analysis_state, skip_reason, skip_detail, similarity,
                        concept_level, tech_summary, problem_summary, reasoning,
                        tech_cluster, problem_cluster, tech_cluster_id,
                        problem_cluster_id, payload_json
                    ) VALUES (
                        ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                    )
                    ON CONFLICT(research_id, patent_id) DO UPDATE SET
                        position=excluded.position,
                        pdf=excluded.pdf,
                        publication_number=excluded.publication_number,
                        title=excluded.title,
                        applicant=excluded.applicant,
                        category=excluded.category,
                        application_number=excluded.application_number,
                        application_date=excluded.application_date,
                        registration_number=excluded.registration_number,
                        registration_date=excluded.registration_date,
                        year=excluded.year,
                        year_source=excluded.year_source,
                        status=excluded.status,
                        source_status=excluded.source_status,
                        legal_status_category=excluded.legal_status_category,
                        tags_text=excluded.tags_text,
                        pdf_available=excluded.pdf_available,
                        analysis_state=excluded.analysis_state,
                        skip_reason=excluded.skip_reason,
                        skip_detail=excluded.skip_detail,
                        similarity=excluded.similarity,
                        concept_level=excluded.concept_level,
                        tech_summary=excluded.tech_summary,
                        problem_summary=excluded.problem_summary,
                        reasoning=excluded.reasoning,
                        tech_cluster=excluded.tech_cluster,
                        problem_cluster=excluded.problem_cluster,
                        tech_cluster_id=excluded.tech_cluster_id,
                        problem_cluster_id=excluded.problem_cluster_id,
                        payload_json=excluded.payload_json
                    """,
                    (
                        research_id,
                        item["id"],
                        position,
                        item.get("pdf", ""),
                        item.get("publication_number", ""),
                        item.get("title", ""),
                        item.get("applicant", ""),
                        item.get("category", ""),
                        item.get("application_number"),
                        item.get("application_date"),
                        item.get("registration_number"),
                        item.get("registration_date"),
                        item.get("year"),
                        item.get("year_source"),
                        item.get("status", "unknown"),
                        item.get("source_status"),
                        item.get("legal_status_category", "published"),
                        tags_text,
                        int(bool(item.get("pdf_available"))),
                        item.get("analysis_state", "pending"),
                        item.get("skip_reason"),
                        item.get("skip_detail"),
                        item.get("similarity"),
                        item.get("concept_level"),
                        item.get("tech_summary"),
                        item.get("problem_summary"),
                        item.get("reasoning"),
                        item.get("tech_cluster"),
                        item.get("problem_cluster"),
                        item.get("tech_cluster_id"),
                        item.get("problem_cluster_id"),
                        self._json(item),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO document_fts(
                        research_id, patent_id, publication_number, title,
                        applicant, category, tags
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        research_id,
                        item["id"],
                        item.get("publication_number", ""),
                        item.get("title", ""),
                        item.get("applicant", ""),
                        item.get("category", ""),
                        tags_text,
                    ),
                )
                for organization_id in item.get("applicant_organization_ids", []):
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO document_organizations(
                            research_id, patent_id, organization_id
                        ) VALUES (?, ?, ?)
                        """,
                        (research_id, item["id"], organization_id),
                    )
            for organization_id, name in organizations.items():
                count = connection.execute(
                    """
                    SELECT COUNT(*) AS value FROM document_organizations
                    WHERE research_id = ? AND organization_id = ?
                    """,
                    (research_id, organization_id),
                ).fetchone()["value"]
                connection.execute(
                    """
                    INSERT INTO organizations(
                        research_id, organization_id, name, document_count
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (research_id, organization_id, name, count),
                )

    def list_researches(self, status: str = "active") -> list[dict[str, Any]]:
        sql = "SELECT * FROM researches"
        params: tuple[Any, ...] = ()
        if status != "all":
            sql += " WHERE lifecycle_status = ?"
            params = (status,)
        sql += " ORDER BY research_id"
        with self.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
            counts = {
                str(row["research_id"]): int(row["count"])
                for row in connection.execute(
                    "SELECT research_id, COUNT(*) AS count FROM documents GROUP BY research_id"
                )
            }
        output = []
        for row in rows:
            config = self._loads(row["config_json"], {})
            output.append(
                {
                    "id": row["research_id"],
                    "name": row["name"],
                    "description": row["description"],
                    "company_technology": row["company_technology"],
                    "document_count": counts.get(str(row["research_id"]), 0),
                    "input": self._loads(row["input_audit_json"], {}),
                    "lifecycle": {
                        **config.get("lifecycle", {}),
                        "status": row["lifecycle_status"],
                        "analysis_stale": bool(row["analysis_stale"]),
                    },
                    "csv_history": self._loads(row["csv_history_json"], []),
                }
            )
        return output

    def research(self, research_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM researches WHERE research_id = ?", (research_id,)
            ).fetchone()
        if not row:
            return None
        return {
            "config": self._loads(row["config_json"], {}),
            "company_technology": row["company_technology"],
            "input": self._loads(row["input_audit_json"], {}),
            "technology_map": self._loads(row["technology_map_json"], {}),
            "csv_history": self._loads(row["csv_history_json"], []),
        }

    def organizations(self, research_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT organization_id, name, document_count
                FROM organizations
                WHERE research_id=?
                ORDER BY name COLLATE NOCASE
                """,
                (research_id,),
            ).fetchall()
        return [
            {
                "id": row["organization_id"],
                "name": row["name"],
                "document_count": int(row["document_count"]),
            }
            for row in rows
        ]

    @staticmethod
    def _filters(
        research_id: str,
        *,
        query: str = "",
        year_from: int | None = None,
        year_to: int | None = None,
        statuses: Iterable[str] = (),
        analysis_states: Iterable[str] = (),
        similarity: int | None = None,
        concept_level: int | None = None,
        tech_cluster_id: str | None = None,
        problem_cluster_id: str | None = None,
        organization_ids: Iterable[str] = (),
    ) -> tuple[str, list[Any], str]:
        clauses = ["d.research_id = ?"]
        params: list[Any] = [research_id]
        joins = ""
        if query.strip():
            clauses.append(
                """
                (
                    d.publication_number LIKE ? OR d.title LIKE ?
                    OR d.applicant LIKE ? OR d.category LIKE ?
                    OR d.tags_text LIKE ?
                )
                """
            )
            needle = f"%{query.strip()}%"
            params.extend([needle] * 5)
        if year_from is not None:
            clauses.append("d.year >= ?")
            params.append(year_from)
        if year_to is not None:
            clauses.append("d.year <= ?")
            params.append(year_to)
        for column, values in (
            ("d.legal_status_category", list(statuses)),
            ("d.analysis_state", list(analysis_states)),
        ):
            if values:
                clauses.append(f"{column} IN ({','.join('?' for _ in values)})")
                params.extend(values)
        if similarity is not None:
            clauses.append("d.similarity = ?")
            params.append(similarity)
        if concept_level is not None:
            clauses.append("d.concept_level = ?")
            params.append(concept_level)
        if tech_cluster_id is not None:
            clauses.append("CAST(d.tech_cluster_id AS TEXT) = ?")
            params.append(str(tech_cluster_id))
        if problem_cluster_id is not None:
            clauses.append("CAST(d.problem_cluster_id AS TEXT) = ?")
            params.append(str(problem_cluster_id))
        organization_ids = list(organization_ids)
        if organization_ids:
            joins += (
                " JOIN document_organizations dor ON dor.research_id = d.research_id"
                " AND dor.patent_id = d.patent_id"
            )
            clauses.append(
                f"dor.organization_id IN ({','.join('?' for _ in organization_ids)})"
            )
            params.extend(organization_ids)
        return " AND ".join(clauses), params, joins

    def document_page(
        self,
        research_id: str,
        *,
        limit: int = 200,
        offset: int = 0,
        **filters: Any,
    ) -> dict[str, Any]:
        where, params, joins = self._filters(research_id, **filters)
        aggregate_filters = dict(filters)
        for key in (
            "similarity",
            "concept_level",
            "tech_cluster_id",
            "problem_cluster_id",
            "organization_ids",
        ):
            aggregate_filters.pop(key, None)
        aggregate_where, aggregate_params, aggregate_joins = self._filters(
            research_id, **aggregate_filters
        )
        overlay_filters = dict(filters)
        for key in (
            "similarity",
            "concept_level",
            "tech_cluster_id",
            "problem_cluster_id",
        ):
            overlay_filters.pop(key, None)
        overlay_where, overlay_params, overlay_joins = self._filters(
            research_id, **overlay_filters
        )
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        with self.connect() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(DISTINCT d.patent_id) AS value FROM documents d{joins} WHERE {where}",
                    params,
                ).fetchone()["value"]
            )
            rows = connection.execute(
                f"""
                SELECT DISTINCT d.* FROM documents d{joins}
                WHERE {where}
                ORDER BY d.position
                LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            ).fetchall()
            aggregates = self._aggregates(
                connection,
                research_id,
                aggregate_where,
                aggregate_params,
                aggregate_joins,
            )
            selection_aggregates = (
                aggregates
                if (aggregate_where, aggregate_params, aggregate_joins)
                == (where, params, joins)
                else self._aggregates(connection, research_id, where, params, joins)
            )
            overlay_aggregates = (
                aggregates
                if (aggregate_where, aggregate_params, aggregate_joins)
                == (overlay_where, overlay_params, overlay_joins)
                else self._aggregates(
                    connection,
                    research_id,
                    overlay_where,
                    overlay_params,
                    overlay_joins,
                )
            )
        return {
            "research_id": research_id,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(rows) < total,
            "items": [self._loads(row["payload_json"], {}) for row in rows],
            "aggregates": aggregates,
            "selection_aggregates": selection_aggregates,
            "overlay_aggregates": overlay_aggregates,
        }

    def _aggregates(
        self,
        connection: sqlite3.Connection,
        research_id: str,
        where: str,
        params: list[Any],
        joins: str,
    ) -> dict[str, Any]:
        metric = connection.execute(
            f"""
            SELECT
                COUNT(DISTINCT d.patent_id) AS total,
                COUNT(DISTINCT CASE WHEN d.analysis_state='ready' THEN d.patent_id END) AS ready,
                COUNT(DISTINCT CASE WHEN d.legal_status_category='rights_acquired' THEN d.patent_id END) AS registered,
                COUNT(DISTINCT CASE WHEN d.analysis_state='ready' AND d.similarity>=4
                    AND d.concept_level>=4 THEN d.patent_id END) AS threat
            FROM documents d{joins} WHERE {where}
            """,
            params,
        ).fetchone()
        threat_cells = [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT d.similarity, d.concept_level,
                       COUNT(DISTINCT d.patent_id) AS count
                FROM documents d{joins}
                WHERE {where} AND d.analysis_state='ready'
                      AND d.similarity BETWEEN 1 AND 5
                      AND d.concept_level BETWEEN 1 AND 5
                GROUP BY d.similarity, d.concept_level
                """,
                params,
            )
        ]
        technology_cells = [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT CAST(d.tech_cluster_id AS TEXT) AS tech_cluster_id,
                       CAST(d.problem_cluster_id AS TEXT) AS problem_cluster_id,
                       MAX(d.tech_cluster) AS tech_cluster,
                       MAX(d.problem_cluster) AS problem_cluster,
                       COUNT(DISTINCT d.patent_id) AS count
                FROM documents d{joins}
                WHERE {where} AND d.analysis_state='ready'
                      AND d.tech_cluster_id IS NOT NULL
                      AND d.problem_cluster_id IS NOT NULL
                GROUP BY d.tech_cluster_id, d.problem_cluster_id
                """,
                params,
            )
        ]
        organization_counts = [
            dict(row)
            for row in connection.execute(
                f"""
                WITH filtered AS (
                    SELECT DISTINCT d.research_id, d.patent_id
                    FROM documents d{joins}
                    WHERE {where}
                )
                SELECT dor.organization_id, COUNT(DISTINCT dor.patent_id) AS count
                FROM filtered x
                JOIN document_organizations dor
                  ON dor.research_id=x.research_id AND dor.patent_id=x.patent_id
                GROUP BY dor.organization_id
                """,
                params,
            )
        ]
        source_status_counts = [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT COALESCE(d.source_status, '') AS source_status,
                       COUNT(DISTINCT d.patent_id) AS count
                FROM documents d{joins}
                WHERE {where}
                GROUP BY COALESCE(d.source_status, '')
                """,
                params,
            )
        ]
        years = [
            int(row["year"])
            for row in connection.execute(
                "SELECT DISTINCT year FROM documents WHERE research_id=? AND year IS NOT NULL ORDER BY year",
                (research_id,),
            )
        ]
        return {
            "metrics": {key: int(metric[key]) for key in ("total", "ready", "registered", "threat")},
            "threat_cells": threat_cells,
            "technology_cells": technology_cells,
            "organization_counts": organization_counts,
            "source_status_counts": source_status_counts,
            "years": years,
        }

    def all_documents(self, research_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM documents WHERE research_id=? ORDER BY position",
                (research_id,),
            ).fetchall()
        return [self._loads(row["payload_json"], {}) for row in rows]

    def iter_documents(self, research_id: str) -> Iterator[dict[str, Any]]:
        connection = self.connect()
        try:
            cursor = connection.execute(
                "SELECT payload_json FROM documents WHERE research_id=? ORDER BY position",
                (research_id,),
            )
            for row in cursor:
                yield self._loads(row["payload_json"], {})
        finally:
            connection.close()

    def pool_count(self) -> int:
        value = self.get_metadata("pool_count")
        return int(value or 0)

    def set_metadata(self, key: str, value: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO metadata(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, value),
            )

    def get_metadata(self, key: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key=?", (key,)
            ).fetchone()
        return str(row["value"]) if row else None

    def upsert_analysis_result(
        self, research_id: str, patent_id: str, result: dict[str, Any]
    ) -> None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT payload_json FROM documents WHERE research_id=? AND patent_id=?",
                (research_id, patent_id),
            ).fetchone()
            if not row:
                return
            payload = self._loads(row["payload_json"], {})
            payload.update(result)
            payload["analysis_state"] = "ready"
            connection.execute(
                """
                UPDATE documents SET analysis_state='ready', skip_reason=NULL,
                    skip_detail=NULL, similarity=?, concept_level=?,
                    tech_summary=?, problem_summary=?, reasoning=?,
                    tech_cluster=?, problem_cluster=?, tech_cluster_id=?,
                    problem_cluster_id=?, payload_json=?
                WHERE research_id=? AND patent_id=?
                """,
                (
                    result.get("similarity"),
                    result.get("concept_level"),
                    result.get("tech_summary"),
                    result.get("problem_summary"),
                    result.get("reasoning"),
                    result.get("tech_cluster"),
                    result.get("problem_cluster"),
                    result.get("tech_cluster_id"),
                    result.get("problem_cluster_id"),
                    self._json(payload),
                    research_id,
                    patent_id,
                ),
            )

    def analysis_result_exists(self, research_id: str, patent_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM documents
                WHERE research_id=? AND patent_id=? AND analysis_state='ready'
                """,
                (research_id, patent_id),
            ).fetchone()
        return bool(row)

    def update_technology_map(self, research_id: str, value: dict[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE researches SET technology_map_json=?, updated_at=? WHERE research_id=?",
                (self._json(value), utc_now(), research_id),
            )

    def set_document_state(
        self,
        research_id: str,
        patent_id: str,
        state: str,
        *,
        skip_reason: str | None = None,
        skip_detail: str | None = None,
    ) -> None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT payload_json FROM documents WHERE research_id=? AND patent_id=?",
                (research_id, patent_id),
            ).fetchone()
            if not row:
                return
            payload = self._loads(row["payload_json"], {})
            payload.update(
                {
                    "analysis_state": state,
                    "skip_reason": skip_reason,
                    "skip_detail": skip_detail,
                }
            )
            connection.execute(
                """
                UPDATE documents SET analysis_state=?, skip_reason=?,
                    skip_detail=?, payload_json=?
                WHERE research_id=? AND patent_id=?
                """,
                (
                    state,
                    skip_reason,
                    skip_detail,
                    self._json(payload),
                    research_id,
                    patent_id,
                ),
            )

    def put_embedding(
        self,
        research_id: str,
        patent_id: str,
        embedding: dict[str, Any],
    ) -> bool:
        vectors = embedding["vectors"]
        technology = encode_vector(vectors["technology"])
        problem = encode_vector(vectors["problem"])
        dimensions = int(embedding.get("dimensions") or len(vectors["technology"]))
        with self.transaction() as connection:
            exists = connection.execute(
                "SELECT 1 FROM documents WHERE research_id=? AND patent_id=?",
                (research_id, patent_id),
            ).fetchone()
            if not exists:
                return False
            connection.execute(
                """
                INSERT INTO embeddings(
                    research_id, patent_id, dimensions, technology, problem,
                    input_sha256_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(research_id, patent_id) DO UPDATE SET
                    dimensions=excluded.dimensions,
                    technology=excluded.technology,
                    problem=excluded.problem,
                    input_sha256_json=excluded.input_sha256_json,
                    created_at=excluded.created_at
                """,
                (
                    research_id,
                    patent_id,
                    dimensions,
                    technology,
                    problem,
                    self._json(embedding.get("input_sha256", [])),
                    str(embedding.get("created_at") or utc_now()),
                ),
            )
        return True

    def get_embedding(self, research_id: str, patent_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM embeddings WHERE research_id=? AND patent_id=?",
                (research_id, patent_id),
            ).fetchone()
        if not row:
            return None
        return {
            "input_order": ["technology", "problem"],
            "input_sha256": self._loads(row["input_sha256_json"], []),
            "dimensions": int(row["dimensions"]),
            "vectors": {
                "technology": decode_vector(row["technology"]),
                "problem": decode_vector(row["problem"]),
            },
            "created_at": row["created_at"],
        }

    def embedding_exists(self, research_id: str, patent_id: str) -> bool:
        with self.connect() as connection:
            return connection.execute(
                "SELECT 1 FROM embeddings WHERE research_id=? AND patent_id=?",
                (research_id, patent_id),
            ).fetchone() is not None

    def embedding_summary(self, research_id: str) -> dict[str, int]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count, COALESCE(MAX(dimensions), 0) AS dimensions,
                       COALESCE(MIN(dimensions), 0) AS minimum_dimensions
                FROM embeddings e
                JOIN documents d
                  ON d.research_id=e.research_id AND d.patent_id=e.patent_id
                WHERE e.research_id=? AND d.analysis_state <> 'skipped'
                """,
                (research_id,),
            ).fetchone()
        return {
            "count": int(row["count"]),
            "dimensions": int(row["dimensions"]),
            "minimum_dimensions": int(row["minimum_dimensions"]),
        }

    def iter_embedding_blobs(
        self, research_id: str, kind: str
    ) -> Iterator[tuple[str, int, bytes]]:
        if kind not in {"technology", "problem"}:
            raise ValueError("embedding kind must be technology or problem")
        connection = self.connect()
        try:
            cursor = connection.execute(
                f"""
                SELECT e.patent_id, e.dimensions, e.{kind} AS vector
                FROM embeddings e
                JOIN documents d
                  ON d.research_id=e.research_id AND d.patent_id=e.patent_id
                WHERE e.research_id=? AND d.analysis_state <> 'skipped'
                ORDER BY d.position
                """,
                (research_id,),
            )
            for row in cursor:
                yield str(row["patent_id"]), int(row["dimensions"]), bytes(row["vector"])
        finally:
            connection.close()

    def analysis_materials(self, research_id: str) -> dict[str, dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT d.patent_id,
                       score.payload_json AS score_json,
                       summaries.payload_json AS summaries_json
                FROM documents d
                JOIN embeddings e
                  ON e.research_id=d.research_id AND e.patent_id=d.patent_id
                JOIN artifacts score
                  ON score.research_id=d.research_id
                 AND score.patent_id=d.patent_id
                 AND score.artifact_type='threat_score'
                 AND score.run_id=''
                JOIN artifacts summaries
                  ON summaries.research_id=d.research_id
                 AND summaries.patent_id=d.patent_id
                 AND summaries.artifact_type='summaries'
                 AND summaries.run_id=''
                WHERE d.research_id=? AND d.analysis_state <> 'skipped'
                ORDER BY d.position
                """,
                (research_id,),
            ).fetchall()
        return {
            str(row["patent_id"]): {
                "score": self._loads(row["score_json"], {}),
                "summaries": self._loads(row["summaries_json"], {}),
            }
            for row in rows
        }

    def pipeline_counts(
        self,
        research_id: str,
        retryable_patent_ids: Iterable[str] = (),
    ) -> dict[str, int]:
        retryable_ids = sorted(set(retryable_patent_ids))
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(pdf_available) AS available,
                       SUM(CASE WHEN analysis_state='ready' THEN 1 ELSE 0 END) AS finalized,
                       SUM(CASE WHEN analysis_state='pending' THEN 1 ELSE 0 END) AS pending,
                       SUM(CASE WHEN analysis_state='skipped' THEN 1 ELSE 0 END) AS skipped,
                       SUM(CASE WHEN analysis_state='invalid' THEN 1 ELSE 0 END) AS failed
                FROM documents WHERE research_id=?
                """,
                (research_id,),
            ).fetchone()
            prepared = int(
                connection.execute(
                    """
                    SELECT COUNT(DISTINCT patent_id) FROM artifacts
                    WHERE research_id=? AND artifact_type='document_structure'
                    """,
                    (research_id,),
                ).fetchone()[0]
            )
            analyzed = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM (
                        SELECT patent_id FROM artifacts
                        WHERE research_id=?
                          AND artifact_type IN ('threat_score','summaries')
                        GROUP BY patent_id
                        HAVING COUNT(DISTINCT artifact_type)=2
                    )
                    """,
                    (research_id,),
                ).fetchone()[0]
            )
            checkpoints = int(
                connection.execute(
                    """
                    SELECT COUNT(DISTINCT a.patent_id)
                    FROM artifacts a
                    JOIN documents d
                      ON d.research_id=a.research_id
                     AND d.patent_id=a.patent_id
                    WHERE a.research_id=?
                      AND a.artifact_type='analysis_complete'
                      AND d.analysis_state='pending'
                    """,
                    (research_id,),
                ).fetchone()[0]
            )
            retryable_states: dict[str, str] = {}
            retryable_analyzed = 0
            if retryable_ids:
                placeholders = ",".join("?" for _ in retryable_ids)
                retryable_states = {
                    str(item["patent_id"]): str(item["analysis_state"])
                    for item in connection.execute(
                        f"""
                        SELECT patent_id, analysis_state FROM documents
                        WHERE research_id=? AND patent_id IN ({placeholders})
                        """,
                        (research_id, *retryable_ids),
                    ).fetchall()
                }
                retryable_analyzed = int(
                    connection.execute(
                        f"""
                        SELECT COUNT(*) FROM (
                            SELECT patent_id FROM artifacts
                            WHERE research_id=?
                              AND artifact_type IN ('threat_score','summaries')
                              AND patent_id IN ({placeholders})
                            GROUP BY patent_id
                            HAVING COUNT(DISTINCT artifact_type)=2
                        )
                        """,
                        (research_id, *retryable_ids),
                    ).fetchone()[0]
                )
        result = {
            key: int(row[key] or 0)
            for key in ("total", "available", "finalized", "pending", "skipped", "failed")
        }
        for state in retryable_states.values():
            if state == "ready":
                result["finalized"] = max(0, result["finalized"] - 1)
                result["pending"] += 1
                result["failed"] += 1
            elif state == "invalid":
                result["pending"] += 1
            elif state == "pending":
                result["failed"] += 1
            elif state == "skipped":
                result["skipped"] = max(0, result["skipped"] - 1)
                result["pending"] += 1
                result["failed"] += 1
        analyzed = max(0, analyzed - retryable_analyzed)
        result.update(
            {
                "prepared": prepared,
                "analyzed": analyzed,
                "llm_pending": max(0, result["pending"] - checkpoints),
            }
        )
        return result

    def bulk_upsert_analysis_results(
        self, research_id: str, results: Iterable[tuple[str, dict[str, Any]]]
    ) -> int:
        count = 0
        with self.transaction() as connection:
            for patent_id, result in results:
                row = connection.execute(
                    "SELECT payload_json FROM documents WHERE research_id=? AND patent_id=?",
                    (research_id, patent_id),
                ).fetchone()
                if not row:
                    continue
                payload = self._loads(row["payload_json"], {})
                payload.update(result)
                payload["analysis_state"] = "ready"
                connection.execute(
                    """
                    UPDATE documents SET analysis_state='ready', skip_reason=NULL,
                        skip_detail=NULL, similarity=?, concept_level=?,
                        tech_summary=?, problem_summary=?, reasoning=?,
                        tech_cluster=?, problem_cluster=?, tech_cluster_id=?,
                        problem_cluster_id=?, payload_json=?
                    WHERE research_id=? AND patent_id=?
                    """,
                    (
                        result.get("similarity"),
                        result.get("concept_level"),
                        result.get("tech_summary"),
                        result.get("problem_summary"),
                        result.get("reasoning"),
                        result.get("tech_cluster"),
                        result.get("problem_cluster"),
                        result.get("tech_cluster_id"),
                        result.get("problem_cluster_id"),
                        self._json(payload),
                        research_id,
                        patent_id,
                    ),
                )
                count += 1
        return count

    def put_artifact(
        self,
        research_id: str,
        patent_id: str,
        artifact_type: str,
        payload: Any,
        *,
        run_id: str = "",
        source_path: str | None = None,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO artifacts(
                    research_id, patent_id, artifact_type, run_id,
                    payload_json, source_path, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(research_id, patent_id, artifact_type, run_id)
                DO UPDATE SET payload_json=excluded.payload_json,
                    source_path=excluded.source_path,
                    updated_at=excluded.updated_at
                """,
                (
                    research_id,
                    patent_id,
                    artifact_type,
                    run_id,
                    self._json(payload),
                    source_path,
                    utc_now(),
                ),
            )

    def get_artifact(
        self, research_id: str, patent_id: str, artifact_type: str, run_id: str = ""
    ) -> Any | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM artifacts
                WHERE research_id=? AND patent_id=? AND artifact_type=? AND run_id=?
                """,
                (research_id, patent_id, artifact_type, run_id),
            ).fetchone()
        return self._loads(row["payload_json"], None) if row else None

    def latest_artifact(
        self, research_id: str, patent_id: str, artifact_type: str
    ) -> Any | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM artifacts
                WHERE research_id=? AND patent_id=? AND artifact_type=?
                ORDER BY updated_at DESC, run_id DESC
                LIMIT 1
                """,
                (research_id, patent_id, artifact_type),
            ).fetchone()
        return self._loads(row["payload_json"], None) if row else None

    def save_interpretation(
        self, research_id: str, patent_id: str, note: str, source: str
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO interpretations(
                    research_id, patent_id, note, source, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(research_id, patent_id) DO UPDATE SET
                    note=excluded.note, source=excluded.source,
                    updated_at=excluded.updated_at
                """,
                (research_id, patent_id, note, source, utc_now()),
            )

    def statistics(self) -> dict[str, Any]:
        with self.connect() as connection:
            counts = {
                table: int(
                    connection.execute(f"SELECT COUNT(*) AS value FROM {table}").fetchone()[
                        "value"
                    ]
                )
                for table in (
                    "researches",
                    "documents",
                    "embeddings",
                    "artifacts",
                    "interpretations",
                )
            }
            version = int(
                connection.execute(
                    "SELECT COALESCE(MAX(version), 0) AS value FROM schema_migrations"
                ).fetchone()["value"]
            )
        return {
            "environment": self.environment,
            "database": str(self.path.relative_to(self.root)),
            "schema_version": version,
            "bytes": self.path.stat().st_size if self.path.exists() else 0,
            **counts,
        }
