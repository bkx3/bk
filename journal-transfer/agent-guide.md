# Day One to Apple Journal Transfer: Agent Guide

This guide is for an AI agent or technical user adapting the workflow. The website page explains the story; these notes explain the expected files, commands, and checks.

## What This Does

Apple Journal does not provide a normal bulk import path. This workflow turns a Day One JSON export into small per-entry folders that Apple Shortcuts can read. The Shortcut then loops through those folders and uses Apple's Create Journal Entry action.

The important script is:

- `scripts/dayone_to_shortcuts_manifest.py`: builds the Shortcut import feed.

The optional verification script is:

- `scripts/reconcile_journal_export.py`: compares the generated feed with a later Apple Journal HTML export.

## Expected Input

Start with a Day One JSON export folder. The script expects the folder to contain:

- `Journal.json`
- `photos/` with image files named by Day One media hashes

The public script defaults to this local layout:

```text
working-folder/
  day-one-export/
    Journal.json
    photos/
```

You can also pass a different export location with `--export-dir`.

## Generate the Shortcut Import Feed

Run this from a working folder that contains `day-one-export`, or pass absolute paths:

```bash
python3 scripts/dayone_to_shortcuts_manifest.py \
  --export-dir day-one-export \
  --output-dir shortcut-import-feed \
  --folders \
  --folder-batch-size 500 \
  --clean
```

This writes:

```text
shortcut-import-feed/
  entries.json
  Resources/
  EntryFolderBatches/
    Batch_001_0001-0500/
      0001_YYYY-MM-DD_Title/
        Body.txt
        Heading.txt
        Date.txt
        ISODate.txt
        UnixTimestamp.txt
        NeedsAI.txt
        AIPrompt.txt
        Media/
```

The Shortcut is designed to choose a batch folder, repeat through each entry folder, read the text files, collect any media from `Media/`, and create Apple Journal entries.

## Title Handling

The script promotes a short first line into `Heading.txt`. If the first line is too long, it can fall back to a date title or generate a title.

Useful options:

```bash
--title-word-limit 12
--generate-titles heuristic
```

For an OpenAI-generated title pass:

```bash
OPENAI_API_KEY=... python3 scripts/dayone_to_shortcuts_manifest.py \
  --export-dir day-one-export \
  --output-dir shortcut-import-feed \
  --folders \
  --folder-batch-size 500 \
  --generate-titles openai
```

To prepare title requests for a separate AI pass without calling an API directly:

```bash
python3 scripts/dayone_to_shortcuts_manifest.py \
  --export-dir day-one-export \
  --write-title-requests title-requests.jsonl \
  --title-request-batch-size 100 \
  --titles-only
```

Then pass title overrides back with `--title-overrides`.

## Date And Media Notes

The script writes multiple date formats because Shortcuts can be picky about date parsing. The `UnixTimestamp.txt` file is usually the safest value for recreating the original date.

Day One may include Markdown image placeholders in the text. The script strips those placeholders from the body and copies the actual media into the matching entry's `Media/` folder.

## Running The Shortcut

Install the Shortcut from the public page, then point it at one batch folder at a time. Importing in batches is intentional: it keeps failures smaller, makes retrying easier, and avoids asking one Shortcut run to process thousands of entries.

If a batch fails, rerun that batch or split it into smaller chunks.

## Reconciliation

After importing, export Apple Journal as HTML if available, then compare the generated source feed to the Apple export:

```bash
python3 scripts/reconcile_journal_export.py \
  --generated-manifest shortcut-import-feed/entries.json \
  --apple-export apple-journal-export/Entries \
  --out reconciliation
```

This creates CSV reports for:

- generated entries missing from Apple Journal
- Apple Journal entries not found in the generated feed
- exact duplicates
- same-date/body duplicate groups

The reports are a starting point for cleanup, not an automatic deletion plan.

## Agent Checklist

1. Confirm the Day One export has `Journal.json` and media folders.
2. Run the Shortcut feed script with `--folders` and a batch size.
3. Verify a few generated entry folders manually.
4. Confirm the Shortcut reads `Body.txt`, `Heading.txt`, date files, and `Media/`.
5. Import a small test batch first.
6. Import the remaining batches.
7. Export Apple Journal and run reconciliation.
8. Investigate missing entries and duplicates before deleting or retrying anything.

## Limitations

This is not a polished product. It is a practical transfer workflow. It assumes Day One's JSON export shape, Apple's current Shortcuts actions, and access to Apple Journal export data for reconciliation.
