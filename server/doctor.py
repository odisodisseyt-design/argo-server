"""Offline diagnostics: `python3 -m server.doctor`."""
from __future__ import annotations

import asyncio

from . import db
from .config import settings
from .packs import list_packs, validate_pack


async def main() -> None:
    await db.init_db()
    issues: list[str] = []
    for pack_id in list_packs():
        issues.extend(f"[pack {pack_id}] {error}" for error in validate_pack(pack_id))
    campaigns = await db.fetchall("SELECT id,phase,state_json FROM campaigns")
    for row in campaigns:
        state = db.loads(row["state_json"], {})
        if row["phase"] not in {"orders", "resolving", "published"}:
            issues.append(f"[{row['id']}] unknown phase {row['phase']}")
        for member in state.get("members", []):
            if not member["character"].get("alive", True):
                issues.append(f"[{row['id']}] dead character still active: {member['user_id']}")
    errors = await db.fetchone("SELECT COUNT(*) AS n FROM llm_logs WHERE error != ''")
    issues.append(f"llm errors logged: {errors['n'] if errors else 0}")
    for label, sql in (
        ("llm timeouts 24h", "SELECT COUNT(*) AS n FROM llm_logs "
                             "WHERE error LIKE 'timeout%' AND created_at > datetime('now','-1 day')"),
        ("invalid item ids 24h", "SELECT COUNT(*) AS n FROM audit_logs "
                                 "WHERE action='invalid_item' AND created_at > datetime('now','-1 day')"),
        ("invalid entities 24h", "SELECT COUNT(*) AS n FROM audit_logs "
                                 "WHERE action='invalid_entities' AND created_at > datetime('now','-1 day')"),
    ):
        row = await db.fetchone(sql)
        print(f"{label}: {row['n'] if row else 0}")
    print(f"provider={settings.provider} model={settings.model} "
          f"intent={settings.intent_model} narrator={settings.narrator_model} "
          f"packs={len(list_packs())} campaigns={len(campaigns)}")
    for issue in issues:
        print(" -", issue)
    print("OK" if not any(i.startswith("[") for i in issues) else "ISSUES FOUND")


if __name__ == "__main__":
    asyncio.run(main())
