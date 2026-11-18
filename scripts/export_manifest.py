"""Write the built-in manifest as JSON (the file the free.ai repo serves at
/api/v1/code/manifest/). Usage: python scripts/export_manifest.py OUT.json [--version 0.2.0]
Bump manifest_version in the free.ai copy whenever its content changes."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from free_code.builtin_manifest import BUILTIN_MANIFEST  # noqa: E402
from free_code import manifest  # noqa: E402

out = Path(sys.argv[1])
data = dict(BUILTIN_MANIFEST)
if "--version" in sys.argv:
    data["latest_version"] = sys.argv[sys.argv.index("--version") + 1]
manifest.validate_manifest(data)
out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
print(f"wrote {out}")
