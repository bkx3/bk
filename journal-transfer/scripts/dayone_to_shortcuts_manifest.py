#!/usr/bin/env python3
import argparse
import os
import json
import re
import shutil
import subprocess
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dayone_to_apple_journal_html import (
    DEFAULT_EXPORT,
    WORKSPACE,
    source_photo_path,
    strip_photo_markers,
    weather_line,
)


DEFAULT_OUTPUT = WORKSPACE / "shortcut-import-feed"
WHITESPACE_RE = re.compile(r"\s+")


def parse_date(value: str, timezone_name: str | None) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timezone_name:
        try:
            return dt.astimezone(ZoneInfo(timezone_name))
        except Exception:
            return dt
    return dt


def date_title(dt: datetime) -> str:
    return dt.strftime("%B ") + str(dt.day) + dt.strftime(", %Y")


def shortcuts_date_text(dt: datetime) -> str:
    return dt.strftime("%B ") + str(dt.day) + dt.strftime(", %Y at %-I:%M %p")


def shortcuts_iso_date_text(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S %z")


def word_count(text: str) -> int:
    return len(re.findall(r"\b[\w'’.-]+\b", text))


def clean_title_candidate(text: str) -> str:
    cleaned = WHITESPACE_RE.sub(" ", text).strip()
    cleaned = re.sub(r"^#{1,6}\s+", "", cleaned).strip()
    cleaned = re.sub(r"\\([\\`*_{}\[\]()#+\-.!|>])", r"\1", cleaned)
    return cleaned.strip()


def unescape_markdown_text(text: str) -> str:
    return re.sub(r"\\([\\`*_{}\[\]()#+\-.!|>])", r"\1", text)


def normalize_plain_text_markdown(text: str) -> str:
    text = unescape_markdown_text(text)
    text = re.sub(
        r'<a\s+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        lambda match: f"{match.group(2)} ({match.group(1)})",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    text = re.sub(r"(?m)^#{1,6}\s+", "", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", r"\1", text)
    return text


def title_from_body(text: str, dt: datetime, word_limit: int) -> tuple[str, bool]:
    for line in text.splitlines():
        cleaned = clean_title_candidate(line)
        if not cleaned or cleaned in {"---", "--"}:
            continue
        if word_count(cleaned) <= word_limit:
            return cleaned, False
        return date_title(dt), True
    return date_title(dt), False


def title_prompt(text: str) -> str:
    trimmed = text.strip()
    if len(trimmed) > 2500:
        trimmed = trimmed[:2500].rsplit(" ", 1)[0]
    return (
        "Create a concise journal entry title from this text. "
        "Use 2 to 8 words. Do not use quotation marks. Do not add a period.\n\n"
        + trimmed
    )


def clean_generated_title(text: str, fallback: str) -> str:
    title = text.strip().splitlines()[0].strip()
    title = title.strip("\"'“”‘’ ")
    title = re.sub(r"\s+", " ", title)
    title = re.sub(r"[.!?]+$", "", title)
    if not title:
        return fallback
    words = title.split()
    if len(words) > 10:
        title = " ".join(words[:10])
    return title


def heuristic_title(text: str, fallback: str) -> str:
    first_paragraph = next((line.strip() for line in text.splitlines() if line.strip()), "")
    quoted = re.search(r'"([^"]{8,70})"', first_paragraph)
    if quoted:
        return clean_generated_title(quoted.group(1), fallback).title()

    sentence = re.split(r"(?<=[.!?])\s+", first_paragraph, maxsplit=1)[0]
    sentence = re.sub(r"^(Today|Yesterday|Tonight|This morning|This afternoon|This evening)\b\s*(has been|was|is|,)?\s*", "", sentence, flags=re.IGNORECASE)
    return clean_generated_title(sentence, fallback)


def openai_title(prompt: str, model: str) -> str:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required for --generate-titles openai")

    payload = json.dumps(
        {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": "Generate concise, faithful personal journal titles. Return only the title.",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 24,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        data = json.loads(response.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def command_title(prompt: str, command: str) -> str:
    result = subprocess.run(
        command,
        input=prompt,
        text=True,
        shell=True,
        check=True,
        capture_output=True,
    )
    return result.stdout


def generated_title(text: str, fallback: str, mode: str, model: str, command: str | None) -> str:
    prompt = title_prompt(text)
    if mode == "none":
        return fallback
    if mode == "heuristic":
        return heuristic_title(text, fallback)
    if mode == "openai":
        return clean_generated_title(openai_title(prompt, model), fallback)
    if mode == "command":
        if not command:
            raise RuntimeError("--title-command is required for --generate-titles command")
        return clean_generated_title(command_title(prompt, command), fallback)
    raise RuntimeError(f"Unknown title generation mode: {mode}")


def load_title_overrides(path: Path | None) -> dict[str, str]:
    if not path:
        return {}
    if not path.exists():
        raise FileNotFoundError(path)

    if path.suffix.lower() == ".jsonl":
        overrides = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            uuid_value = item.get("uuid") or item.get("id")
            title = item.get("title")
            if uuid_value and title:
                overrides[str(uuid_value).strip()] = clean_generated_title(str(title), "")
        return overrides

    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            items = data.items()
        else:
            items = (
                (item.get("uuid") or item.get("id"), item.get("title"))
                for item in data
                if isinstance(item, dict)
            )
        return {
            str(uuid_value).strip(): clean_generated_title(str(title), "")
            for uuid_value, title in items
            if uuid_value and title
        }

    overrides = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        uuid_value, separator, title = line.partition("\t")
        if not separator:
            uuid_value, separator, title = line.partition(",")
        if not separator:
            continue
        uuid_value = uuid_value.strip()
        if uuid_value.lower() in {"uuid", "id"}:
            continue
        title = clean_generated_title(title, "")
        if uuid_value and title:
            overrides[uuid_value] = title
    return overrides


def title_request(entry: dict, text: str, dt: datetime, fallback: str, excerpt_chars: int) -> dict:
    body = text.strip()
    if len(body) > excerpt_chars:
        body = body[:excerpt_chars].rsplit(" ", 1)[0].strip()
    return {
        "uuid": entry["uuid"],
        "date": dt.date().isoformat(),
        "fallbackTitle": fallback,
        "body": body,
    }


def write_title_requests(path: Path, requests: list[dict], batch_size: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        for request in requests:
            file.write(json.dumps(request, ensure_ascii=False) + "\n")
    if batch_size:
        batches_dir = path.parent / f"{path.stem}_batches"
        if batches_dir.exists():
            shutil.rmtree(batches_dir)
        batches_dir.mkdir(parents=True, exist_ok=True)
        for start in range(0, len(requests), batch_size):
            batch = requests[start : start + batch_size]
            batch_path = batches_dir / f"{path.stem}_{start // batch_size + 1:03d}.jsonl"
            with batch_path.open("w", encoding="utf-8") as file:
                for request in batch:
                    file.write(json.dumps(request, ensure_ascii=False) + "\n")


def remove_promoted_title_line(text: str, title: str, needs_generated_title: bool) -> str:
    if needs_generated_title:
        return text

    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        if clean_title_candidate(line) != title:
            return text

        remaining = lines[:index] + lines[index + 1 :]
        while remaining and not remaining[0].strip():
            remaining.pop(0)
        return "\n".join(remaining).strip()
    return text


def compact_body_spacing(text: str) -> str:
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{2,}", "\n", text).strip()


def resource_name(identifier: str, extension: str) -> str:
    return f"{uuid.uuid5(uuid.NAMESPACE_URL, f'dayone-photo:{identifier}').hex.upper()}.{extension.lower()}"


def location_block(entry: dict) -> str:
    location = entry.get("location") or {}
    if not location:
        return ""

    lines = ["", "Written at:", ""]
    place = location.get("placeName")
    locality = location.get("localityName")
    admin = location.get("administrativeArea")
    country = location.get("country")
    if place:
        lines.append(place)
    if locality or admin:
        lines.append(", ".join(part for part in [locality, admin] if part))
    if country:
        lines.append(country)
    lat = location.get("latitude")
    lon = location.get("longitude")
    if lat is not None and lon is not None:
        url = f"https://maps.apple.com/?ll={lat},{lon}"
        if place or locality:
            url += "&q=" + (place or locality)
        lines.extend(["", url])
    return "\n".join(lines)


def final_entry_text(entry: dict) -> str:
    text = normalize_plain_text_markdown(strip_photo_markers(entry.get("text") or ""))
    extras = []
    loc = location_block(entry)
    if loc:
        extras.append(loc)
    weather = weather_line(entry, parse_date(entry["creationDate"], entry.get("timeZone")))
    if weather:
        extras.append("\n" + weather)
    if extras:
        return text.rstrip() + "\n\n" + "\n".join(extras).strip()
    return text


def should_include(entry: dict, only_with_photos: bool) -> bool:
    if only_with_photos and not entry.get("photos"):
        return False
    return bool((entry.get("text") or "").strip() or entry.get("photos"))


def entry_on_or_after(entry: dict, start_date: str | None) -> bool:
    if not start_date:
        return True
    return entry["creationDate"][:10] >= start_date


def spread_sample(entries: list[dict], count: int | None) -> list[dict]:
    if count is None or count >= len(entries):
        return entries
    if count <= 0:
        return []
    if count == 1:
        return [entries[0]]
    last = len(entries) - 1
    indexes = sorted({round(i * last / (count - 1)) for i in range(count)})
    return [entries[index] for index in indexes]


def media_files(entry: dict, export_dir: Path, resources_dir: Path, max_photos: int | None) -> list[str]:
    files = []
    photos = sorted(entry.get("photos") or [], key=lambda photo: photo.get("orderInEntry", 0))
    if max_photos is not None:
        photos = photos[:max_photos]

    for photo in photos:
        source = source_photo_path(export_dir, photo)
        if not source:
            continue
        name = resource_name(photo["identifier"], source.suffix.lstrip(".") or photo.get("type") or "jpeg")
        target = resources_dir / name
        if not target.exists():
            shutil.copy2(source, target)
        files.append(str(target))
    return files


def folder_batch_dir(output_dir: Path, index: int, batch_size: int | None) -> Path:
    if not batch_size:
        return output_dir / "EntryFolders"
    batch_index = (index - 1) // batch_size + 1
    start = (batch_index - 1) * batch_size + 1
    end = batch_index * batch_size
    return output_dir / "EntryFolderBatches" / f"Batch_{batch_index:03d}_{start:04d}-{end:04d}"


def write_control_text(folder: Path, name: str, text: str) -> None:
    (folder / f"{name}.txt").write_text(text, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a JSON feed for Shortcuts' Create Journal Entry action.")
    parser.add_argument("--export-dir", type=Path, default=DEFAULT_EXPORT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--sample-spread", type=int, default=None)
    parser.add_argument("--start-date", default=None, help="Only include entries on or after YYYY-MM-DD.")
    parser.add_argument("--only-with-photos", action="store_true")
    parser.add_argument("--max-photos", type=int, default=None)
    parser.add_argument("--title-word-limit", type=int, default=12)
    parser.add_argument("--generate-titles", choices=["none", "heuristic", "openai", "command"], default="none")
    parser.add_argument("--openai-model", default=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"))
    parser.add_argument("--title-command", default=None)
    parser.add_argument("--title-overrides", type=Path, default=None, help="JSON, TSV, or CSV mapping entry UUIDs to generated titles.")
    parser.add_argument("--write-title-requests", type=Path, default=None, help="Write JSONL requests for entries that still need generated titles.")
    parser.add_argument("--title-request-excerpt-chars", type=int, default=1800)
    parser.add_argument("--title-request-batch-size", type=int, default=100)
    parser.add_argument("--titles-only", action="store_true", help="Only write title requests; do not write import entries or copy media.")
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--folders", action="store_true", help="Also write one Shortcut-friendly folder per entry.")
    parser.add_argument("--folder-batch-size", type=int, default=None, help="When writing folders, group entry folders into batch directories.")
    args = parser.parse_args()

    if args.titles_only and not args.write_title_requests:
        parser.error("--titles-only requires --write-title-requests")
    if args.folder_batch_size is not None and args.folder_batch_size <= 0:
        parser.error("--folder-batch-size must be greater than zero")

    if args.clean and args.output_dir.exists():
        shutil.rmtree(args.output_dir)
    resources_dir = args.output_dir / "Resources"
    entry_folders_dir = args.output_dir / "EntryFolders"
    entry_folder_batches_dir = args.output_dir / "EntryFolderBatches"
    if not args.titles_only:
        resources_dir.mkdir(parents=True, exist_ok=True)
        if args.folders and not args.folder_batch_size:
            entry_folders_dir.mkdir(parents=True, exist_ok=True)
        if args.folders and args.folder_batch_size:
            entry_folder_batches_dir.mkdir(parents=True, exist_ok=True)

    title_overrides = load_title_overrides(args.title_overrides)
    unresolved_title_requests = []
    entries = json.loads((args.export_dir / "Journal.json").read_text(encoding="utf-8"))["entries"]
    entries = [
        entry
        for entry in entries
        if should_include(entry, args.only_with_photos) and entry_on_or_after(entry, args.start_date)
    ]
    entries.sort(key=lambda entry: entry["creationDate"])
    entries = spread_sample(entries, args.sample_spread)
    if args.limit is not None:
        entries = entries[: args.limit]

    if args.titles_only:
        for entry in entries:
            dt = parse_date(entry["creationDate"], entry.get("timeZone"))
            text = final_entry_text(entry)
            title, needs_generated_title = title_from_body(text, dt, args.title_word_limit)
            if not needs_generated_title:
                continue
            if title_overrides.get(entry["uuid"]):
                continue
            if args.generate_titles != "none":
                continue
            unresolved_title_requests.append(
                title_request(entry, compact_body_spacing(text), dt, title, args.title_request_excerpt_chars)
            )
        write_title_requests(args.write_title_requests, unresolved_title_requests, args.title_request_batch_size)
        print(f"Wrote {len(unresolved_title_requests)} title requests to {args.write_title_requests}")
        return

    manifest = []
    for index, entry in enumerate(entries, start=1):
        dt = parse_date(entry["creationDate"], entry.get("timeZone"))
        text = final_entry_text(entry)
        title, needs_generated_title = title_from_body(text, dt, args.title_word_limit)
        if needs_generated_title:
            override = title_overrides.get(entry["uuid"])
            if override:
                title = override
                needs_generated_title = False
            elif args.generate_titles != "none":
                title = generated_title(text, title, args.generate_titles, args.openai_model, args.title_command)
                needs_generated_title = False
            else:
                title = ""
                needs_generated_title = False
        text = remove_promoted_title_line(text, title, needs_generated_title)
        text = compact_body_spacing(text)
        if needs_generated_title:
            unresolved_title_requests.append(title_request(entry, text, dt, title, args.title_request_excerpt_chars))
        files = media_files(entry, args.export_dir, resources_dir, args.max_photos)
        if not text.strip() and not files and title:
            text = title
        location = entry.get("location") or {}
        manifest.append(
            {
                "uuid": entry["uuid"],
                "title": title,
                "needsGeneratedTitle": needs_generated_title,
                "titlePrompt": title_prompt(text) if needs_generated_title else "",
                "finalEntry": text,
                "creationDate": dt.isoformat(),
                "shortcutDate": shortcuts_date_text(dt),
                "mediaFiles": files,
                "location": {
                    "name": location.get("placeName") or location.get("localityName"),
                    "latitude": location.get("latitude"),
                    "longitude": location.get("longitude"),
                }
                if location
                else None,
            }
        )

        if args.folders:
            safe_title = re.sub(r"[^A-Za-z0-9._ -]+", "", title).strip().replace(" ", "_")[:80]
            parent_folder = folder_batch_dir(args.output_dir, index, args.folder_batch_size)
            folder = parent_folder / f"{index:04d}_{dt.date().isoformat()}_{safe_title or entry['uuid'][:8]}"
            media_dir = folder / "Media"
            media_dir.mkdir(parents=True, exist_ok=True)
            write_control_text(folder, "Body", text)
            write_control_text(folder, "Heading", title)
            write_control_text(folder, "NeedsAI", "true" if needs_generated_title else "false")
            write_control_text(folder, "AIPrompt", title_prompt(text) if needs_generated_title else "")
            write_control_text(folder, "Date", shortcuts_date_text(dt))
            write_control_text(folder, "ISODate", shortcuts_iso_date_text(dt))
            write_control_text(folder, "UnixTimestamp", str(int(dt.timestamp())))
            for file_path in files:
                source = Path(file_path)
                target = media_dir / source.name
                if not target.exists():
                    shutil.copy2(source, target)

    out = args.output_dir / "entries.json"
    out.write_text(json.dumps({"entries": manifest}, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.write_title_requests:
        write_title_requests(args.write_title_requests, unresolved_title_requests, args.title_request_batch_size)
        print(f"Wrote {len(unresolved_title_requests)} title requests to {args.write_title_requests}")
    print(f"Wrote {len(manifest)} Shortcut entries to {out}")
    print(f"Copied/referenced media in {resources_dir}")
    if args.folders:
        if args.folder_batch_size:
            print(f"Wrote batched entry folders to {entry_folder_batches_dir}")
        else:
            print(f"Wrote entry folders to {entry_folders_dir}")


if __name__ == "__main__":
    main()
