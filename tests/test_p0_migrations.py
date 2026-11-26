from server import db, migrations


async def test_tables_and_columns_present():
    names = {row["name"] for row in await db.fetchall(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"turn_idempotency", "rate_limits", "schema_migrations"} <= names
    llm_cols = {row["name"] for row in await db.fetchall("PRAGMA table_info(llm_logs)")}
    assert "model_version" in llm_cols
    camp_cols = {row["name"] for row in await db.fetchall("PRAGMA table_info(campaigns)")}
    assert "model_id" in camp_cols


async def test_migrations_are_idempotent():
    await migrations.apply_migrations()
    await migrations.apply_migrations()
    rows = await db.fetchall("SELECT version FROM schema_migrations")
    assert len(rows) == len(migrations.MIGRATIONS)
