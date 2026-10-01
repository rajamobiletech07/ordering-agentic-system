"""Export the API gateway's OpenAPI 3.1 document (CI publishes it as an artifact)."""

import json
import sys
from pathlib import Path

from ordering.api_gateway.main import app

out = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/openapi.json")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(app.openapi(), indent=2))
print(f"wrote {out} ({len(app.openapi()['paths'])} paths)")
