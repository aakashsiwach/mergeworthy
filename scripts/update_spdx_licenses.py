"""Regenerate src/mergeworthy/data/spdx_licenses.json from a tagged SPDX license list release.

Usage (needs network; never run by tests):

    python scripts/update_spdx_licenses.py v3.29.0
    python scripts/update_spdx_licenses.py v3.29.0 --from-file path/to/licenses.json

Only the licence identifier and its OSI-approved flag are kept. Review the diff before committing.
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.request
from pathlib import Path

URL = "https://raw.githubusercontent.com/spdx/license-list-data/{tag}/json/licenses.json"
OUT = Path(__file__).resolve().parents[1] / "src" / "mergeworthy" / "data" / "spdx_licenses.json"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("tag", help="release tag, e.g. v3.29.0")
    parser.add_argument("--from-file", type=Path, help="use an already downloaded licenses.json")
    args = parser.parse_args()
    if not re.fullmatch(r"v\d+\.\d+(?:\.\d+)?", args.tag):
        raise SystemExit("tag must look like v3.29.0")
    source = URL.format(tag=args.tag)
    if args.from_file:
        raw = args.from_file.read_text(encoding="utf-8")
    else:
        with urllib.request.urlopen(source, timeout=30) as response:
            raw = response.read().decode("utf-8")
    data = json.loads(raw)
    licenses = {
        entry["licenseId"]: bool(entry.get("isOsiApproved", False))
        for entry in sorted(data["licenses"], key=lambda e: e["licenseId"].lower())
    }
    out = {
        "_comment": (
            "Vendored from the SPDX License List. Only licenseId and isOsiApproved are kept. "
            "Regenerate with scripts/update_spdx_licenses.py; do not edit by hand."
        ),
        "source": source,
        "licenseListVersion": data["licenseListVersion"],
        "releaseDate": data.get("releaseDate"),
        "licenses": licenses,
    }
    OUT.write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8", newline="\n")
    approved = sum(licenses.values())
    version = data["licenseListVersion"]
    print(f"wrote {OUT}: {len(licenses)} licenses, {approved} OSI-approved, version {version}")


if __name__ == "__main__":
    main()
