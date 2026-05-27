"""Export the FastAPI OpenAPI spec to openapi.json in the repo root.

Run after any route or Pydantic model change:
    python scripts/export_openapi.py

CI uses this to verify the committed spec is up to date (see .github/workflows/contract.yml).
"""
import json
import os
import sys

# Allow running without a live DB — engine creation at import time only needs
# a parseable URL; actual connections are not made until a request is served.
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://x:x@localhost/x")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app  # noqa: E402

output_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "openapi.json")

spec = app.openapi()
with open(output_path, "w") as f:
    json.dump(spec, f, indent=2, sort_keys=True)
    f.write("\n")

print(f"Wrote {output_path}")
