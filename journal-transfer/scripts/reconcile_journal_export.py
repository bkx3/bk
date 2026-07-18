#!/usr/bin/env python3
import argparse
import csv
import hashlib
import html
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


WORKSPACE = Path.cwd()
DEFAULT_GENERATED = WORKSPACE / "shortcut-import-feed" / "EntryFolderBatches"
DEFAULT_GENERATED_MANIFEST = WORKSPACE / "shortcut-import-feed" / "entries.json"
DEFAULT_APPLE_EXPORT = WORKSPACE / "apple-journal-export" / "Entries"
DEFAULT_OUT = WORKSPACE / "reconciliation"

WHITESPACE_RE = re.compile(r"\s+")
DATE_FROM_FILENAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
PAGE_HEADER_RE = re.compile(r"<div class=['\"]pageHeader['\"]>(.*?)</div>", re.DOTALL)
TITLE_RE = re.compile(r"<div class=['\"]title['\"]>(.*?)</div>", re.DOTALL)
BODY_PARAGRAPH_RE = re.compile(r"<p class=['\"]p2['\"]><span class=['\"]s2['\"]>(.*?)</span></p>", re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class Entry:
    source: str
    identifier: str
    path: str
    date: str
    title: str
    body: str
    signature: str
    body_signature: str


def normalize_text(text: str) -> str:
    text = html.unescape(text)
    text = text.replace("\u200b", "").replace("\ufeff", "")
    text = text.replace("\xa0", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [WHITESPACE_RE.sub(" ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def html_fragment_text(fragment: str) -> str:
    fragment = fragment.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    return normalize_text(TAG_RE.sub("", fragment))


def short_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def signature(date: str, title: str, body: str) -> str:
    return short_hash("\n---\n".join([date, normalize_text(title), normalize_text(body)]))


def body_signature(date: str, body: str) -> str:
    return short_hash("\n---\n".join([date, normalize_text(body)]))


def read_generated(root: Path) -> list[Entry]:
    entries = []
    for entry_dir in sorted(root.glob("Batch_*/*")):
        if not entry_dir.is_dir():
            continue
        iso_file = entry_dir / "ISODate.txt"
        title_file = entry_dir / "Heading.txt"
        body_file = entry_dir / "Body.txt"
        if not (iso_file.exists() and title_file.exists() and body_file.exists()):
            continue
        date = iso_file.read_text(encoding="utf-8").strip()[:10]
        title = normalize_text(title_file.read_text(encoding="utf-8"))
        body = normalize_text(body_file.read_text(encoding="utf-8"))
        entries.append(
            Entry(
                source="generated",
                identifier=entry_dir.name.split("_", 1)[0],
                path=str(entry_dir),
                date=date,
                title=title,
                body=body,
                signature=signature(date, title, body),
                body_signature=body_signature(date, body),
            )
        )
    return entries


def read_generated_manifest(path: Path) -> list[Entry]:
    data = json.loads(path.read_text(encoding="utf-8"))["entries"]
    entries = []
    for index, item in enumerate(data, start=1):
        date = item["creationDate"][:10]
        title = normalize_text(item.get("title") or "")
        body = normalize_text(item.get("finalEntry") or "")
        entries.append(
            Entry(
                source="generated",
                identifier=f"{index:04d}_{item.get('uuid', '')}",
                path=str(path),
                date=date,
                title=title,
                body=body,
                signature=signature(date, title, body),
                body_signature=body_signature(date, body),
            )
        )
    return entries


def read_apple_export(root: Path) -> list[Entry]:
    entries = []
    for html_file in sorted(root.glob("*.html")):
        match = DATE_FROM_FILENAME_RE.match(html_file.name)
        if not match:
            continue
        content = html_file.read_text(encoding="utf-8", errors="replace")
        page_header_match = PAGE_HEADER_RE.search(content)
        if page_header_match:
            page_header = html_fragment_text(page_header_match.group(1))
            try:
                date = datetime.strptime(page_header, "%A, %B %d, %Y").date().isoformat()
            except ValueError:
                date = match.group(1)
        else:
            date = match.group(1)
        title_match = TITLE_RE.search(content)
        title = html_fragment_text(title_match.group(1)) if title_match else ""
        body = normalize_text("\n".join(html_fragment_text(match.group(1)) for match in BODY_PARAGRAPH_RE.finditer(content)))
        entries.append(
            Entry(
                source="apple",
                identifier=html_file.stem,
                path=str(html_file),
                date=date,
                title=title,
                body=body,
                signature=signature(date, title, body),
                body_signature=body_signature(date, body),
            )
        )
    return entries


def write_entries(path: Path, entries: list[Entry]) -> None:
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=["date", "title", "body_preview", "signature", "body_signature", "identifier", "path"],
        )
        writer.writeheader()
        for entry in entries:
            writer.writerow(
                {
                    "date": entry.date,
                    "title": entry.title,
                    "body_preview": entry.body[:220].replace("\n", " / "),
                    "signature": entry.signature,
                    "body_signature": entry.body_signature,
                    "identifier": entry.identifier,
                    "path": entry.path,
                }
            )


def groups_by(entries: list[Entry], attr: str) -> dict[str, list[Entry]]:
    groups: dict[str, list[Entry]] = {}
    for entry in entries:
        groups.setdefault(getattr(entry, attr), []).append(entry)
    return groups


def write_duplicate_groups(path: Path, groups: dict[str, list[Entry]]) -> int:
    duplicate_groups = [items for items in groups.values() if len(items) > 1]
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=["group_size", "date", "title", "body_preview", "signature", "path"],
        )
        writer.writeheader()
        for items in sorted(duplicate_groups, key=lambda group: (group[0].date, group[0].title, len(group))):
            for entry in items:
                writer.writerow(
                    {
                        "group_size": len(items),
                        "date": entry.date,
                        "title": entry.title,
                        "body_preview": entry.body[:220].replace("\n", " / "),
                        "signature": entry.signature,
                        "path": entry.path,
                    }
                )
    return len(duplicate_groups)


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare generated Day One import batches to an Apple Journal HTML export.")
    parser.add_argument("--generated", type=Path, default=DEFAULT_GENERATED)
    parser.add_argument("--generated-manifest", type=Path, default=DEFAULT_GENERATED_MANIFEST)
    parser.add_argument("--apple-export", type=Path, default=DEFAULT_APPLE_EXPORT)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    generated = read_generated_manifest(args.generated_manifest) if args.generated_manifest.exists() else read_generated(args.generated)
    apple = read_apple_export(args.apple_export)

    generated_by_signature = groups_by(generated, "signature")
    apple_by_signature = groups_by(apple, "signature")
    generated_signatures = set(generated_by_signature)
    apple_signatures = set(apple_by_signature)

    missing_from_apple = [
        entry for entry in generated if entry.signature not in apple_signatures
    ]
    extra_in_apple = [
        entry for entry in apple if entry.signature not in generated_signatures
    ]
    duplicate_apple_groups = write_duplicate_groups(args.out / "apple_exact_duplicate_groups.csv", apple_by_signature)
    duplicate_apple_body_groups = write_duplicate_groups(args.out / "apple_same_date_body_duplicate_groups.csv", groups_by(apple, "body_signature"))
    duplicate_generated_groups = write_duplicate_groups(args.out / "generated_exact_duplicate_groups.csv", generated_by_signature)

    write_entries(args.out / "generated_missing_from_apple.csv", missing_from_apple)
    write_entries(args.out / "apple_not_in_generated.csv", extra_in_apple)
    write_entries(args.out / "generated_entries.csv", generated)
    write_entries(args.out / "apple_entries.csv", apple)

    summary = {
        "generated_entries": len(generated),
        "apple_export_entries": len(apple),
        "generated_missing_from_apple": len(missing_from_apple),
        "apple_not_in_generated": len(extra_in_apple),
        "apple_exact_duplicate_groups": duplicate_apple_groups,
        "apple_same_date_body_duplicate_groups": duplicate_apple_body_groups,
        "generated_exact_duplicate_groups": duplicate_generated_groups,
    }
    summary_path = args.out / "summary.txt"
    summary_path.write_text(
        "\n".join(f"{key}: {value}" for key, value in summary.items()) + "\n",
        encoding="utf-8",
    )
    print(summary_path.read_text(encoding="utf-8"), end="")
    print(f"Wrote reports to {args.out}")


if __name__ == "__main__":
    main()
