#!/usr/bin/env python3
"""
Merge Hackmageddon timeline CSV files into a single normalized CSV.

What it does
------------
1. Scans a folder for CSV files.
2. Reads each Hackmageddon timeline CSV, even if older/newer files use slightly
   different column sets.
3. Normalizes the columns into a canonical schema.
4. Adds provenance fields such as the source filename and source timeline label.
5. Writes a merged CSV and, optionally, a merge report CSV.

Examples
--------
python3 hackmageddon_merge_csvs.py
python3 hackmageddon_merge_csvs.py --input-dir hackmageddon_csv
python3 hackmageddon_merge_csvs.py --input-dir hackmageddon_csv --output merged.csv
python3 hackmageddon_merge_csvs.py --recursive
python3 hackmageddon_merge_csvs.py --report merge_report.csv
"""

from __future__ import annotations

import argparse
import logging
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd

DEFAULT_INPUT_DIR = Path(".")
DEFAULT_OUTPUT_FILE = Path("hackmageddon_merged.csv")
DEFAULT_REPORT_FILE = Path("hackmageddon_merge_report.csv")

CANONICAL_COLUMNS = [
    "Record ID",
    "Legacy Row ID",
    "Primary Date",
    "Date",
    "Date Reported",
    "Date Occurred",
    "Date Discovered",
    "Author",
    "Target",
    "Description",
    "Attack",
    "Target Class",
    "Attack Class",
    "Country",
    "Link",
    "Tags",
    "Initial Access",
    "Source File",
    "Source Timeline",
]

COLUMN_ALIASES = {
    "id": "Record ID",
    "wdt_id": "Legacy Row ID",
    "date": "Date",
    "date reported": "Date Reported",
    "date occurred": "Date Occurred",
    "date discovered": "Date Discovered",
    "author": "Author",
    "target": "Target",
    "description": "Description",
    "attack": "Attack",
    "target class": "Target Class",
    "attack class": "Attack Class",
    "country": "Country",
    "link": "Link",
    "tags": "Tags",
    "initial access": "Initial Access",
}


class MergeError(Exception):
    """Raised when merge processing fails."""


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def normalize_whitespace(text: object) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def normalize_column_name(name: object) -> str:
    cleaned = normalize_whitespace(name)
    cleaned = cleaned.replace("\ufeff", "")
    key = cleaned.lower()
    return COLUMN_ALIASES.get(key, cleaned)


def timeline_label_from_filename(path: Path) -> str:
    stem = path.stem
    stem = stem.replace("-", " ")
    stem = re.sub(r"\s+", " ", stem).strip()
    return stem


def first_non_empty(values: Iterable[object]) -> str:
    for value in values:
        text = normalize_whitespace(value)
        if text and text.lower() not in {"nan", "none"}:
            return text
    return ""


def read_csv_flexibly(path: Path) -> pd.DataFrame:
    last_error = None
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return pd.read_csv(path, encoding=encoding)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    raise MergeError("Could not read {0}: {1}".format(path, last_error))


def normalize_dataframe(df: pd.DataFrame, source_file: Path) -> pd.DataFrame:
    renamed = {}
    for col in df.columns:
        renamed[col] = normalize_column_name(col)
    df = df.rename(columns=renamed).copy()

    for canonical_col in CANONICAL_COLUMNS:
        if canonical_col not in df.columns:
            df[canonical_col] = ""

    df["Source File"] = source_file.name
    df["Source Timeline"] = timeline_label_from_filename(source_file)

    if "Primary Date" not in df.columns:
        df["Primary Date"] = ""

    if "Date" in df.columns:
        date_series = df["Date"]
    else:
        date_series = ""

    df["Primary Date"] = [
        first_non_empty(
            [
                date_value,
                reported_value,
                occurred_value,
                discovered_value,
            ]
        )
        for date_value, reported_value, occurred_value, discovered_value in zip(
            df.get("Date", pd.Series([""] * len(df))),
            df.get("Date Reported", pd.Series([""] * len(df))),
            df.get("Date Occurred", pd.Series([""] * len(df))),
            df.get("Date Discovered", pd.Series([""] * len(df))),
        )
    ]

    # Preserve only the canonical columns plus any unexpected extra columns after them.
    extra_columns = [col for col in df.columns if col not in CANONICAL_COLUMNS]
    ordered_columns = CANONICAL_COLUMNS + extra_columns
    df = df[ordered_columns]

    # Convert missing values to empty strings for cleaner CSV output.
    df = df.fillna("")

    return df


DATE_FORMATS_TO_TRY = [
    "%d/%m/%Y",
    "%Y-%m-%d",
    "%m/%d/%Y",
]


def parse_primary_date(value: object) -> pd.Timestamp:
    text = normalize_whitespace(value)
    if not text:
        return pd.NaT

    for fmt in DATE_FORMATS_TO_TRY:
        try:
            return pd.to_datetime(text, format=fmt, errors="raise")
        except Exception:  # noqa: BLE001
            pass

    # Fallback for values like 'Since March 2023' or quoted relative dates.
    return pd.to_datetime(text, errors="coerce", dayfirst=True)


def find_csv_files(input_dir: Path, recursive: bool) -> List[Path]:
    pattern = "**/*.csv" if recursive else "*.csv"
    files = sorted(p for p in input_dir.glob(pattern) if p.is_file())
    return files


def is_probably_timeline_csv(path: Path, df: pd.DataFrame) -> bool:
    normalized_columns = {normalize_column_name(col) for col in df.columns}
    strong_markers = {"Record ID", "Author", "Target", "Description", "Attack"}
    overlap = normalized_columns & strong_markers
    filename_marker = "timeline" in path.name.lower()
    return filename_marker or len(overlap) >= 4


def merge_csvs(files: List[Path]) -> Tuple[pd.DataFrame, pd.DataFrame]:
    merged_frames = []
    report_rows = []

    for path in files:
        try:
            df = read_csv_flexibly(path)
            if not is_probably_timeline_csv(path, df):
                logging.info("Skipping non-timeline CSV: %s", path.name)
                report_rows.append(
                    {
                        "Source File": path.name,
                        "Status": "skipped",
                        "Rows": 0,
                        "Columns": ", ".join(map(str, df.columns.tolist())),
                        "Message": "Not recognized as a Hackmageddon timeline CSV",
                    }
                )
                continue

            normalized = normalize_dataframe(df, path)
            merged_frames.append(normalized)
            report_rows.append(
                {
                    "Source File": path.name,
                    "Status": "merged",
                    "Rows": len(normalized),
                    "Columns": ", ".join(normalized.columns.tolist()),
                    "Message": "OK",
                }
            )
            logging.info("Merged %s (%s rows)", path.name, len(normalized))
        except Exception as exc:  # noqa: BLE001
            logging.exception("Failed to process %s", path.name)
            report_rows.append(
                {
                    "Source File": path.name,
                    "Status": "failed",
                    "Rows": 0,
                    "Columns": "",
                    "Message": str(exc),
                }
            )

    if not merged_frames:
        raise MergeError("No CSV files were successfully merged.")

    merged = pd.concat(merged_frames, ignore_index=True, sort=False)
    merged["_sort_date"] = merged["Primary Date"].apply(parse_primary_date)
    merged = merged.sort_values(by=["_sort_date", "Source File", "Record ID"], ascending=[True, True, True], na_position="last")
    merged = merged.drop(columns=["_sort_date"]).reset_index(drop=True)

    report_df = pd.DataFrame(report_rows)
    return merged, report_df


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Merge Hackmageddon timeline CSV files")
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help="Folder containing timeline CSV files (default: current directory)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_FILE,
        help="Output merged CSV file (default: hackmageddon_merged.csv)",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT_FILE,
        help="Output merge report CSV file (default: hackmageddon_merge_report.csv)",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Search for CSV files recursively under --input-dir",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)

    if not args.input_dir.exists() or not args.input_dir.is_dir():
        logging.error("Input directory does not exist or is not a directory: %s", args.input_dir)
        return 2

    files = find_csv_files(args.input_dir, args.recursive)
    if not files:
        logging.error("No CSV files found in: %s", args.input_dir)
        return 1

    logging.info("Found %s CSV file(s)", len(files))
    for idx, path in enumerate(files, start=1):
        logging.info("[%s] %s", idx, path.name)

    try:
        merged, report_df = merge_csvs(files)
    except MergeError as exc:
        logging.error(str(exc))
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)

    merged.to_csv(args.output, index=False, encoding="utf-8-sig")
    report_df.to_csv(args.report, index=False, encoding="utf-8-sig")

    merged_count = int((report_df["Status"] == "merged").sum()) if not report_df.empty else 0
    skipped_count = int((report_df["Status"] == "skipped").sum()) if not report_df.empty else 0
    failed_count = int((report_df["Status"] == "failed").sum()) if not report_df.empty else 0

    print("Summary")
    print("-------")
    print("files_found: {0}".format(len(files)))
    print("files_merged: {0}".format(merged_count))
    print("files_skipped: {0}".format(skipped_count))
    print("files_failed: {0}".format(failed_count))
    print("rows_output: {0}".format(len(merged)))
    print("output: {0}".format(args.output.resolve()))
    print("report: {0}".format(args.report.resolve()))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
