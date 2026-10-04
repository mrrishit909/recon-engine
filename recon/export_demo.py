"""Write the dashboard's API responses to static JSON, so the demo can run on a static host with no server."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from . import db, service

if __name__ == "__main__":  # pragma: no cover
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "web/public/data")
    out.mkdir(parents=True, exist_ok=True)
    with db.connect() as conn:
        views = {"reconciliation": service.reconciliation_view(conn), "analytics": service.analytics(conn),
                 "exceptions": service.exceptions_view(conn), "audit": service.audit_view(conn)}
    for name, data in views.items():
        (out / f"{name}.json").write_text(json.dumps(data, default=str))
        print(name, (out / f"{name}.json").stat().st_size, "bytes")
