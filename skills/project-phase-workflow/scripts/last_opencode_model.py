"""Read only OpenCode model-call metadata; never use UI selections as call history."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3


def latest_model(database):
    database = Path(database).expanduser().resolve()
    if not database.is_file():
        return {"status": "MANUAL_REQUIRED", "reason": "CALL_DATABASE_MISSING"}
    candidates = []
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=3)) as db:
            db.execute("PRAGMA query_only=ON")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "session_message" in tables:
                columns = {row[1] for row in db.execute("PRAGMA table_info(session_message)")}
                if not {"type", "data", "time_created"}.issubset(columns):
                    return {"status": "MANUAL_REQUIRED", "reason": "UNSUPPORTED_CALL_SCHEMA"}
                row = db.execute("""SELECT json_extract(data,'$.model.providerID'),
                    json_extract(data,'$.model.id'),json_extract(data,'$.model.variant'),time_created
                    FROM session_message WHERE type='assistant'
                    AND json_extract(data,'$.model.providerID') IS NOT NULL
                    AND json_extract(data,'$.model.id') IS NOT NULL
                    ORDER BY time_created DESC LIMIT 1""").fetchone()
                if row:
                    candidates.append((*row, "session_message"))
            if "message" in tables:
                columns = {row[1] for row in db.execute("PRAGMA table_info(message)")}
                if not {"data", "time_created"}.issubset(columns):
                    return {"status": "MANUAL_REQUIRED", "reason": "UNSUPPORTED_CALL_SCHEMA"}
                row = db.execute("""SELECT json_extract(data,'$.providerID'),
                    json_extract(data,'$.modelID'),NULL,time_created FROM message
                    WHERE json_extract(data,'$.role')='assistant'
                    AND json_extract(data,'$.providerID') IS NOT NULL
                    AND json_extract(data,'$.modelID') IS NOT NULL
                    ORDER BY time_created DESC LIMIT 1""").fetchone()
                if row:
                    candidates.append((*row, "message"))
    except (OSError, sqlite3.Error):
        return {"status": "MANUAL_REQUIRED", "reason": "CALL_HISTORY_UNREADABLE"}
    if not candidates:
        return {"status": "MANUAL_REQUIRED", "reason": "NO_RECORDED_MODEL_CALL"}
    try:
        provider, model, variant, timestamp, table = max(candidates, key=lambda row: row[3])
        if not isinstance(provider, str) or not isinstance(model, str) or not provider or not model:
            raise ValueError("Invalid model metadata")
        if any(c.isspace() for c in provider + model) or "/" in provider or "#" in provider + model:
            raise ValueError("Ambiguous model metadata")
        full_name = provider + "/" + model
        if variant and variant != "default":
            if not isinstance(variant, str) or any(c.isspace() for c in variant) or "#" in variant:
                raise ValueError("Invalid variant")
            full_name += "#" + variant
        called_at = datetime.fromtimestamp(timestamp / 1000, timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return {"status": "MANUAL_REQUIRED", "reason": "INVALID_CALL_METADATA"}
    return {
        "status": "FOUND", "full_name": full_name, "providerID": provider, "modelID": model,
        "variant": variant or None, "called_at_utc": called_at,
        "source": str(database), "source_table": table,
        "evidence_scope": "latest recorded assistant model call; not a UI selection or configured default",
    }


def main():
    data_root = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=data_root / "opencode/opencode.db")
    args = parser.parse_args()
    result = latest_model(args.database)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "FOUND" else 2


if __name__ == "__main__":
    raise SystemExit(main())
