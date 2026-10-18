"""Regenerate ``schemas/*.json`` from the dataclasses in ``kallimachos.interfaces``,
``kallimachos.config.schema`` and ``kallimachos.profile.schema``.

Usage::

    python tools/gen_schemas.py          # write files
    python tools/gen_schemas.py --check  # exit 1 when any file is out of date

``tests/test_schemas.py`` runs the same check, so a dataclass change without regeneration fails CI.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import kallimachos.config.schema  # noqa: E402  (registers config.schema.json)
import kallimachos.profile.schema  # noqa: E402,F401  (registers profile.schema.json)
from kallimachos.interfaces import SCHEMA_REGISTRY, json_schema  # noqa: E402

SCHEMAS_DIR = REPO / "schemas"


def render(name: str) -> str:
    cls, schema_id = SCHEMA_REGISTRY[name]
    return json.dumps(json_schema(cls, schema_id=schema_id), indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify instead of writing")
    args = parser.parse_args(argv)
    SCHEMAS_DIR.mkdir(exist_ok=True)
    stale: list[str] = []
    for name in sorted(SCHEMA_REGISTRY):
        text = render(name)
        target = SCHEMAS_DIR / name
        if args.check:
            if not target.exists() or target.read_text(encoding="utf-8") != text:
                stale.append(name)
        else:
            target.write_text(text, encoding="utf-8")
            print(f"wrote {target.relative_to(REPO)}")
    if stale:
        print("out of date: " + ", ".join(stale) + " (run python tools/gen_schemas.py)")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
