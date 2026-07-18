#!/usr/bin/env python3
import argparse
import html
import json
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


WORKSPACE = Path.cwd()
DEFAULT_EXPORT = WORKSPACE / "day-one-export"
DEFAULT_OUTPUT = WORKSPACE / "apple-journal-html"
STYLE_SOURCE = (
    WORKSPACE
    / "apple-journal-style-source.html"
)

PHOTO_MARKER_RE = re.compile(r"!\[[^\]]*\]\(dayone-moment://[A-Fa-f0-9-]+\)\s*")
WHITESPACE_RE = re.compile(r"\s+")
UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")


def load_style_shell() -> str:
    text = STYLE_SOURCE.read_text(encoding="utf-8")
    return text.split("<body>", 1)[0] + "<body>\n"


def parse_date(value: str, timezone_name: str | None) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timezone_name:
        try:
            return dt.astimezone(ZoneInfo(timezone_name))
        except Exception:
            return dt
    return dt


def display_date(dt: datetime) -> str:
    return dt.strftime("%A, %B ") + str(dt.day) + dt.strftime(", %Y")


def short_date(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def strip_photo_markers(text: str) -> str:
    return PHOTO_MARKER_RE.sub("", text or "").strip()


def title_from_text(text: str, fallback_date: datetime) -> str:
    for line in text.splitlines():
        cleaned = WHITESPACE_RE.sub(" ", line).strip()
        if cleaned and cleaned not in {"---", "--"}:
            return cleaned[:140]
    return fallback_date.strftime("%B ") + str(fallback_date.day) + fallback_date.strftime(", %Y")


def filename_for_entry(dt: datetime, title: str, entry_uuid: str) -> str:
    slug = UNSAFE_FILENAME_RE.sub("", title).strip().replace(" ", "_")
    slug = slug[:110].strip("._-")
    if not slug:
        slug = entry_uuid[:12]
    return f"{short_date(dt)}_{slug}.html"


def paragraph_class_map(text: str) -> tuple[str, str]:
    if "\t" in text:
        return "p1", "s1"
    return "p2", "s2"


def paragraph_html(text: str) -> str:
    if not text:
        return '<p class="p3"><span class="s2"></span><br></p>'
    p_class, s_class = paragraph_class_map(text)
    escaped = html.escape(text, quote=False).replace("\t", '<span class="Apple-tab-span">\t</span>')
    return f'<p class="{p_class}"><span class="{s_class}">{escaped}</span></p>'


def text_to_paragraphs(text: str) -> list[str]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return [paragraph_html(line.strip()) for line in lines]


def media_resource_name(identifier: str, extension: str) -> str:
    resource_uuid = uuid.uuid5(uuid.NAMESPACE_URL, f"dayone-photo:{identifier}")
    return f"{str(resource_uuid).upper()}.{extension.lower()}"


def source_photo_path(export_dir: Path, photo: dict) -> Path | None:
    md5 = photo.get("md5")
    if not md5:
        return None
    declared = (photo.get("type") or "jpeg").lower().replace("jpg", "jpeg")
    candidates = [
        export_dir / "photos" / f"{md5}.{declared}",
        export_dir / "photos" / f"{md5}.jpeg",
        export_dir / "photos" / f"{md5}.jpg",
        export_dir / "photos" / f"{md5}.png",
        export_dir / "photos" / f"{md5}.gif",
        export_dir / "photos" / f"{md5}.heic",
    ]
    return next((path for path in candidates if path.exists()), None)


def build_asset_grid(entry: dict, export_dir: Path, resources_dir: Path, max_photos: int | None) -> tuple[str, list[str]]:
    items = []
    copied = []
    photos = sorted(entry.get("photos") or [], key=lambda photo: photo.get("orderInEntry", 0))
    if max_photos is not None:
        photos = photos[:max_photos]

    for photo in photos:
        source = source_photo_path(export_dir, photo)
        if not source:
            continue
        extension = source.suffix.lstrip(".") or (photo.get("type") or "jpeg")
        resource_name = media_resource_name(photo["identifier"], extension)
        target = resources_dir / resource_name
        if not target.exists():
            shutil.copy2(source, target)
        copied.append(resource_name)
        item_id = Path(resource_name).stem
        items.append(
            f'    <div id="{item_id}" class="gridItem assetType_photo " >\n'
            f'        <img src="../Resources/{html.escape(resource_name)}" class="asset_image"/>\n'
            f"    </div>"
        )

    return '<div class="assetGrid">' + "".join(items) + "</div>", copied


def location_lines(entry: dict) -> list[str]:
    location = entry.get("location") or {}
    lines = []
    if location:
        lines.append("")
        lines.append("Written at:")
        lines.append("")
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
        query = place or locality
        if lat is not None and lon is not None:
            url = f"https://maps.apple.com/?ll={lat},{lon}"
            if query:
                url += "&q=" + query
            lines.append("")
            lines.append(url)
    return lines


def weather_line(entry: dict, dt: datetime) -> str | None:
    weather = entry.get("weather") or {}
    location = entry.get("location") or {}
    if not weather:
        return None
    parts = []
    conditions = weather.get("conditionsDescription")
    temp = weather.get("temperatureCelsius")
    humidity = weather.get("relativeHumidity")
    wind = weather.get("windSpeedKPH")
    place = location.get("placeName") or location.get("localityName") or "this location"
    prefix = f"The weather at {place} on {dt.isoformat()} was"
    if conditions:
        parts.append(str(conditions).lower())
    if temp is not None:
        parts.append(f"with a temperature of {temp}°C")
    if humidity is not None:
        parts.append(f"relative humidity of {humidity}%")
    if wind is not None:
        parts.append(f"wind speed of {round(float(wind), 2)} kph")
    if not parts:
        return None
    return prefix + " " + ", ".join(parts) + "."


def build_entry_html(shell: str, entry: dict, export_dir: Path, resources_dir: Path, max_photos: int | None) -> tuple[str, str, list[str]]:
    dt = parse_date(entry["creationDate"], entry.get("timeZone"))
    body_text = strip_photo_markers(entry.get("text") or "")
    title = title_from_text(body_text, dt)
    asset_grid, copied = build_asset_grid(entry, export_dir, resources_dir, max_photos)

    extra_lines = location_lines(entry)
    weather = weather_line(entry, dt)
    if weather:
        extra_lines.extend(["", weather])

    paragraphs = text_to_paragraphs(body_text)
    if extra_lines:
        paragraphs.extend(paragraph_html(line) for line in extra_lines)

    title_html = html.escape(title, quote=False)
    content = [
        shell,
        f'<p class="p1"><span class="s1"><div class=\'pageContainer\'>    <div class="pageHeader">{display_date(dt)}</div>    {asset_grid}<div class=\'title\'>{title_html}</span></p>',
        '<p class="p1"><span class="s1"></div><div class=\'bodyText\'></span></p>',
        *paragraphs,
        '<p class="p1"><span class="s1"></div></div></span></p>',
        "</body>",
        "</html>",
        "",
    ]
    return "\n".join(content), title, copied


def build_index(shell: str, entries: list[dict], output_entries: list[tuple[dict, str, str]]) -> str:
    rows = []
    for entry, filename, title in output_entries:
        dt = parse_date(entry["creationDate"], entry.get("timeZone"))
        label = f"{dt.strftime('%b')} {dt.day}, {dt.year} — {title}"
        rows.append(f'<p class="p1"><span class="s1"><a href="Entries/{html.escape(filename)}">{html.escape(label, quote=False)}</a></span></p>')
    return "\n".join([shell, *rows, "</body>", "</html>", ""])


def should_include(entry: dict, only_with_photos: bool) -> bool:
    if only_with_photos and not entry.get("photos"):
        return False
    return bool((entry.get("text") or "").strip() or entry.get("photos"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert Day One JSON export entries into Apple Journal-style HTML.")
    parser.add_argument("--export-dir", type=Path, default=DEFAULT_EXPORT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--only-with-photos", action="store_true")
    parser.add_argument("--max-photos", type=int, default=None)
    parser.add_argument("--clean", action="store_true")
    args = parser.parse_args()

    export_dir = args.export_dir
    output_dir = args.output_dir
    entries_dir = output_dir / "Entries"
    resources_dir = output_dir / "Resources"

    if args.clean and output_dir.exists():
        shutil.rmtree(output_dir)
    entries_dir.mkdir(parents=True, exist_ok=True)
    resources_dir.mkdir(parents=True, exist_ok=True)

    data = json.loads((export_dir / "Journal.json").read_text(encoding="utf-8"))
    entries = data["entries"]
    entries = [entry for entry in entries if should_include(entry, args.only_with_photos)]
    entries.sort(key=lambda entry: entry["creationDate"])
    if args.limit is not None:
        entries = entries[: args.limit]

    shell = load_style_shell()
    output_entries = []
    copied_count = 0

    for entry in entries:
        entry_html, title, copied = build_entry_html(shell, entry, export_dir, resources_dir, args.max_photos)
        dt = parse_date(entry["creationDate"], entry.get("timeZone"))
        filename = filename_for_entry(dt, title, entry["uuid"])
        path = entries_dir / filename
        if path.exists():
            stem = path.stem[:100]
            path = entries_dir / f"{stem}_{entry['uuid'][:8]}.html"
            filename = path.name
        path.write_text(entry_html, encoding="utf-8")
        output_entries.append((entry, filename, title))
        copied_count += len(copied)

    (output_dir / "index.html").write_text(build_index(shell, entries, output_entries), encoding="utf-8")
    print(f"Wrote {len(output_entries)} entries to {entries_dir}")
    print(f"Copied/referenced {copied_count} photos in {resources_dir}")
    print(f"Index: {output_dir / 'index.html'}")


if __name__ == "__main__":
    main()
