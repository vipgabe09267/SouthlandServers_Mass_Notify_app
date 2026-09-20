"""Fail CI on advisories published for any explicitly locked package version."""
import json
import re
import urllib.request
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent.parent
    failed = False
    for line in (root / "requirements-build.txt").read_text(encoding="utf-8").splitlines():
        match = re.match(r"([A-Za-z0-9_-]+)==([^ ]+)", line)
        if not match:
            continue
        name, version = match.groups()
        with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=30) as response:
            data = json.load(response)
        advisories = [entry for entry in data.get("vulnerabilities", []) if not entry.get("withdrawn")]
        for advisory in advisories:
            print(f"{name}=={version}: {advisory.get('id')} {advisory.get('link')}")
            failed = True
    if failed:
        raise SystemExit("Dependency advisories require review and an updated lock before release.")
    print("No published PyPI advisories for locked versions at check time.")


if __name__ == "__main__":
    main()
