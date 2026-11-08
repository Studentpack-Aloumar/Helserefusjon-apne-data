#!/usr/bin/env python3
"""Snapshot and contract check for the Helserefusjon open data API.

Fetches the full code lists from https://opne-data-api.helserefusjon.no/v1 and

  * --out DIR   writes them as sorted JSON Lines (one record per line), so that
                changes between snapshots show up as small, readable git diffs;
  * --validate  checks every response against the schemas in openapi.yaml, and
                also sends one small request to each JSON takstbruk endpoint.

Exits with status 1 if a request fails or validation finds problems.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = "https://opne-data-api.helserefusjon.no/v1"
SPEC_PATH = Path(__file__).resolve().parent.parent / "openapi.yaml"

# name -> (path, key in the JSON response, schema name, id field used for sorting)
DATASETS = {
    "takstkoder": ("/takstkoder", "takstkoder", "Takstkode", "takst_id"),
    "nlkkoder": ("/nlkkoder", "nlkkoder", "NlkKode", "nlkKodeid"),
    "refusjonskategorisatser": (
        "/refusjonskategorisatser",
        "refusjonskategorisatser",
        "Refusjonskategorisats",
        "refusjonskategoriid",
    ),
    "apatkombinasjoner": ("/apatkombinasjoner", "apatKombinasjoner", "ApatKombinasjon", None),
    "fagomraader": ("/fagomraader", "fagomraader", "Fagomraade", "fagomraadekode"),
    "bandasjister": ("/bandasjister", "bandasjister", "Bandasjist", "sar_id"),
}

# One small request per JSON takstbruk endpoint, used only by --validate.
TAKSTBRUK_SAMPLES = [
    "/takstbruk/agtakst/kommune/maned?fagomraade=LE&fommaned=202401&tommaned=202401&takstkoder=2ad&kommuner=0301",
    "/takstbruk/agtakst/kommune/ar?fagomraade=LE&fomar=2024&tomar=2024&takstkoder=2ad&kommuner=0301",
    "/takstbruk/agtakst/fylke/maned?fagomraade=LE&fommaned=202401&tommaned=202401&takstkoder=2ad&fylker=03",
    "/takstbruk/agtakst/fylke/ar?fagomraade=LE&fomar=2024&tomar=2024&takstkoder=2ad&fylker=03",
    "/takstbruk/agtakst/landet/maned?fagomraade=LE&fommaned=202401&tommaned=202401&takstkoder=2ad",
    "/takstbruk/agtakst/landet/ar?fagomraade=LE&fomar=2024&tomar=2024&takstkoder=2ad",
]


def fetch_json(path: str, attempts: int = 4) -> dict:
    url = BASE_URL + path
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "helserefusjon-apne-data-snapshot"}
    )
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            if attempt == attempts:
                raise RuntimeError(f"GET {url} failed: {error}") from error
            time.sleep(2**attempt)
    raise AssertionError("unreachable")


def normalise(name: str, records: list[dict]) -> list[str]:
    """Return one canonical JSON line per record, in a stable order."""
    if name == "nlkkoder":
        # Each NLK code embeds every (historical) rate for its category. Those
        # rates are already in refusjonskategorisatser, so drop the copy: it
        # makes up most of the response size.
        records = [{k: v for k, v in r.items() if k != "refusjonskategorisats"} for r in records]
    _, _, _, id_field = DATASETS[name]
    if id_field:
        records = sorted(records, key=lambda r: str(r.get(id_field)))
    lines = [json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for r in records]
    return lines if id_field else sorted(lines)


def load_validators():
    try:
        import yaml
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT202012
    except ImportError as error:
        sys.exit(f"--validate needs pyyaml and jsonschema: {error}")

    spec = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    uri = "urn:openapi"
    registry = Registry().with_resource(uri, Resource.from_contents(spec, default_specification=DRAFT202012))

    def validator(schema_name: str) -> Draft202012Validator:
        return Draft202012Validator({"$ref": f"{uri}#/components/schemas/{schema_name}"}, registry=registry)

    return validator


def check(validator, label: str, records: list[dict], limit: int = 5) -> list[str]:
    problems: list[str] = []
    for index, record in enumerate(records):
        for error in validator.iter_errors(record):
            location = "/".join(str(p) for p in error.absolute_path) or "(record)"
            problems.append(f"{label}[{index}] {location}: {error.message}")
            if len(problems) >= limit:
                return problems + [f"{label}: stopped after {limit} problems"]
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, help="directory to write <dataset>.jsonl files to")
    parser.add_argument("--validate", action="store_true", help="check responses against openapi.yaml")
    args = parser.parse_args()
    if not args.out and not args.validate:
        parser.error("give --out, --validate or both")

    validator = load_validators() if args.validate else None
    problems: list[str] = []

    for name, (path, key, schema_name, _) in DATASETS.items():
        body = fetch_json(path)
        if key not in body or not isinstance(body[key], list) or not body[key]:
            problems.append(f"{name}: expected a non-empty list under '{key}', got keys {sorted(body)}")
            continue
        records = body[key]
        print(f"{name}: {len(records)} records", file=sys.stderr)
        if validator:
            problems += check(validator(schema_name), name, records)
        if args.out:
            args.out.mkdir(parents=True, exist_ok=True)
            lines = normalise(name, records)
            (args.out / f"{name}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    if validator:
        takstbruk = validator("TakstbrukSvar")
        for path in TAKSTBRUK_SAMPLES:
            body = fetch_json(path)
            label = path.split("?")[0]
            if not body.get("takstbruk"):
                problems.append(f"{label}: no rows returned for the sample request")
            problems += check(takstbruk, label, [body])

    for problem in problems:
        print(f"PROBLEM: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
