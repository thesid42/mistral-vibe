from __future__ import annotations

from pathlib import Path
import sqlite3

from vibe.core.paths import VM_DIR
from vibe.core.vibevm.models import VMSnapshot
from vibe.core.vibevm.store import page_from_row, stats_from_rows


def find_session_db(session_id: str | None) -> Path | None:
    vm_dir = VM_DIR.path
    if session_id is not None:
        candidate = vm_dir / f"{session_id}.db"
        return candidate if candidate.exists() else None
    if not vm_dir.exists():
        return None
    candidates = sorted(
        vm_dir.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    return candidates[0] if candidates else None


def load_snapshot(db_path: Path) -> VMSnapshot:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        pages = [
            page_from_row(row).model_copy(update={"content": ""})
            for row in conn.execute(
                "SELECT * FROM pages ORDER BY created_seq"
            ).fetchall()
        ]
        stats = stats_from_rows(conn.execute("SELECT key, value FROM stats").fetchall())
    finally:
        conn.close()
    return VMSnapshot(pages=pages, stats=stats, db_path=str(db_path))
