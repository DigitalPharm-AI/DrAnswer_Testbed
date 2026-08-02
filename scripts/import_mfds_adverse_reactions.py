from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, text

from agent_app.persistence.schema import migrate_agent_schema
from shared.db import DatabaseEngineConfig, create_database_engine
from shared.settings import get_settings

TABLE_COLUMNS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "import_runs",
        "mfds_import_runs",
        (
            "run_id",
            "source_file",
            "source_sha256",
            "extractor_version",
            "started_at",
            "completed_at",
            "status",
            "stats_json",
        ),
    ),
    (
        "drug_products",
        "mfds_drug_products",
        (
            "item_seq",
            "item_name",
            "item_eng_name",
            "entp_name",
            "entp_eng_name",
            "item_permit_date",
            "etc_otc_code",
            "cancel_name",
            "cancel_date",
            "change_date",
            "atc_code",
            "main_item_ingr",
            "main_ingr_eng",
            "material_name",
            "search_text",
        ),
    ),
    (
        "label_documents",
        "mfds_label_documents",
        (
            "document_id",
            "document_hash",
            "document_type",
            "raw_xml_zlib",
            "raw_xml_length",
            "parse_status",
            "parse_error",
        ),
    ),
    (
        "product_label_documents",
        "mfds_product_label_documents",
        ("item_seq", "document_id", "change_date"),
    ),
    (
        "label_sections",
        "mfds_label_sections",
        ("section_id", "section_type", "section_text", "section_hash"),
    ),
    (
        "document_label_sections",
        "mfds_document_label_sections",
        (
            "document_id",
            "section_id",
            "heading",
            "section_path",
            "section_order",
        ),
    ),
    (
        "adverse_reactions",
        "mfds_adverse_reactions",
        ("reaction_id", "normalized_term"),
    ),
    (
        "section_adverse_reactions",
        "mfds_section_adverse_reactions",
        (
            "mention_id",
            "section_id",
            "reaction_normalized",
            "reaction_raw",
            "organ_system_text",
            "frequency_text",
            "population_text",
            "condition_text",
            "assertion",
            "evidence_start",
            "evidence_end",
            "reaction_start",
            "reaction_end",
            "extraction_method",
            "extractor_version",
            "confidence",
            "review_status",
        ),
    ),
)

TESTBED_ALIASES: tuple[tuple[str, str, str], ...] = (
    ("메트포르민", "200401015", "dranswer_testbed"),
    ("암로디핀", "200610660", "dranswer_testbed"),
    ("수니티닙", "200606182", "dranswer_testbed"),
    ("레트로졸", "200108765", "dranswer_testbed"),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _source_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _source_tables(connection: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _validate_source(connection: sqlite3.Connection) -> dict[str, int]:
    expected = {source for source, _target, _columns in TABLE_COLUMNS}
    missing = sorted(expected - _source_tables(connection))
    if missing:
        raise RuntimeError(f"mfds_source_schema_missing:{','.join(missing)}")
    foreign_key_errors = list(connection.execute("PRAGMA foreign_key_check"))
    if foreign_key_errors:
        raise RuntimeError(f"mfds_source_foreign_key_errors:{len(foreign_key_errors)}")
    return {source: int(connection.execute(f'SELECT COUNT(*) FROM "{source}"').fetchone()[0]) for source, _target, _columns in TABLE_COLUMNS}


def _rows(
    connection: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
    *,
    batch_size: int,
) -> Iterator[list[tuple[Any, ...]]]:
    quoted = ", ".join(f'"{column}"' for column in columns)
    cursor = connection.execute(f'SELECT {quoted} FROM "{table}"')
    while True:
        batch = cursor.fetchmany(batch_size)
        if not batch:
            return
        yield [tuple(bytes(value) if isinstance(value, memoryview) else value for value in row) for row in batch]


def _copy_rows(
    cursor: Any,
    *,
    table: str,
    columns: Sequence[str],
    batches: Iterable[list[tuple[Any, ...]]],
) -> int:
    column_sql = ", ".join(columns)
    copied = 0
    with cursor.copy(f"COPY {table} ({column_sql}) FROM STDIN") as copy:
        for batch in batches:
            for row in batch:
                copy.write_row(row)
            copied += len(batch)
    return copied


def _truncate_reference(cursor: Any) -> None:
    cursor.execute(
        """
        TRUNCATE TABLE
            mfds_drug_aliases,
            mfds_section_adverse_reactions,
            mfds_adverse_reactions,
            mfds_document_label_sections,
            mfds_label_sections,
            mfds_product_label_documents,
            mfds_label_documents,
            mfds_drug_products,
            mfds_import_runs
        """
    )


def _seed_testbed_aliases(cursor: Any) -> None:
    cursor.executemany(
        """
        INSERT INTO mfds_drug_aliases (
            alias_normalized,
            item_seq,
            source
        ) VALUES (%s, %s, %s)
        """,
        TESTBED_ALIASES,
    )


def _destination_counts(cursor: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for source, target, _columns in TABLE_COLUMNS:
        cursor.execute(f"SELECT COUNT(*) FROM {target}")
        counts[source] = int(cursor.fetchone()[0])
    return counts


def _validate_destination(cursor: Any, expected: dict[str, int]) -> None:
    actual = _destination_counts(cursor)
    if actual != expected:
        raise RuntimeError(
            "mfds_destination_count_mismatch:"
            + json.dumps(
                {"expected": expected, "actual": actual},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM mfds_section_adverse_reactions mention
        JOIN mfds_label_sections section
          ON section.section_id = mention.section_id
        WHERE mention.evidence_start < 0
           OR mention.evidence_end < mention.evidence_start
           OR mention.evidence_end > char_length(section.section_text)
           OR mention.reaction_start < mention.evidence_start
           OR mention.reaction_end > mention.evidence_end
        """
    )
    invalid_offsets = int(cursor.fetchone()[0])
    if invalid_offsets:
        raise RuntimeError(f"mfds_destination_offset_errors:{invalid_offsets}")
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM mfds_drug_aliases alias
        JOIN mfds_drug_products product
          ON product.item_seq = alias.item_seq
        WHERE alias.source = 'dranswer_testbed'
        """
    )
    alias_count = int(cursor.fetchone()[0])
    if alias_count != len(TESTBED_ALIASES):
        raise RuntimeError(f"mfds_testbed_alias_count_mismatch:{alias_count}")


def import_database(
    *,
    sqlite_path: Path,
    engine: Engine,
    batch_size: int = 2_000,
) -> dict[str, Any]:
    if not sqlite_path.is_file():
        raise FileNotFoundError(sqlite_path)
    source_sha256 = _sha256(sqlite_path)
    source = _source_connection(sqlite_path)
    try:
        source_counts = _validate_source(source)
        raw = engine.raw_connection()
        try:
            with raw.cursor() as cursor:
                _truncate_reference(cursor)
                copied: dict[str, int] = {}
                for source_table, target_table, columns in TABLE_COLUMNS:
                    copied[source_table] = _copy_rows(
                        cursor,
                        table=target_table,
                        columns=columns,
                        batches=_rows(
                            source,
                            source_table,
                            columns,
                            batch_size=batch_size,
                        ),
                    )
                if copied != source_counts:
                    raise RuntimeError(
                        "mfds_copy_count_mismatch:"
                        + json.dumps(
                            {"expected": source_counts, "copied": copied},
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                    )
                _seed_testbed_aliases(cursor)
                _validate_destination(cursor, source_counts)
            raw.commit()
        except Exception:
            raw.rollback()
            raise
        finally:
            raw.close()
    finally:
        source.close()
    return {
        "ok": True,
        "source_file": sqlite_path.name,
        "source_sha256": source_sha256,
        "counts": source_counts,
        "testbed_aliases": len(TESTBED_ALIASES),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=("Import the reviewed MFDS SQLite artifact into the Agent/Trace PostgreSQL database."))
    parser.add_argument("sqlite_path", type=Path)
    parser.add_argument("--batch-size", type=int, default=2_000)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    if arguments.batch_size < 1:
        raise ValueError("batch_size_must_be_positive")
    settings = get_settings()
    settings.require_agent_migration_postgresql()
    engine = create_database_engine(
        settings.agent_migration_database_url,
        config=DatabaseEngineConfig(
            pool_size=1,
            max_overflow=0,
            pool_timeout_seconds=settings.agent_db_pool_timeout_seconds,
            pool_recycle_seconds=settings.agent_db_pool_recycle_seconds,
            statement_timeout_ms=0,
            lock_timeout_ms=0,
        ),
    )
    try:
        migrate_agent_schema(engine)
        result = import_database(
            sqlite_path=arguments.sqlite_path.resolve(),
            engine=engine,
            batch_size=arguments.batch_size,
        )
        with engine.connect() as connection:
            result["database"] = connection.execute(text("SELECT current_database()")).scalar_one()
    finally:
        engine.dispose()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
