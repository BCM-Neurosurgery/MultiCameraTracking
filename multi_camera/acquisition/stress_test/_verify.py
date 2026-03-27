"""Output file verification for deployment validation."""

from __future__ import annotations

import glob
import json
import os
import subprocess


def verify_mp4_files(output_dir: str, num_cameras: int, expected_segments: int) -> tuple[list[str], int]:
    """Verify all MP4 files with ffprobe. Returns (issues, total_bytes)."""
    issues = []
    total_bytes = 0
    mp4s = sorted(glob.glob(os.path.join(output_dir, "*.mp4")))

    if not mp4s:
        issues.append("No MP4 files produced")
        return issues, 0

    expected = num_cameras * expected_segments
    if len(mp4s) < expected:
        issues.append(f"Expected {expected} MP4s ({num_cameras} cams x {expected_segments} seg), got {len(mp4s)}")

    corrupt = 0
    for mp4 in mp4s:
        total_bytes += os.path.getsize(mp4)
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration,nb_streams", "-of", "csv=p=0", mp4],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if r.returncode != 0 or not r.stdout.strip():
                corrupt += 1
        except Exception:
            corrupt += 1

    if corrupt > 0:
        issues.append(f"{corrupt}/{len(mp4s)} MP4 files failed ffprobe validation")

    return issues, total_bytes


def verify_metadata_files(output_dir: str, expected_segments: int) -> list[str]:
    """Verify .metadata.jsonl and aggregate .json files. Returns issues."""
    issues = []

    # JSONL journals
    jsonl_files = sorted(glob.glob(os.path.join(output_dir, "*.metadata.jsonl")))
    if not jsonl_files:
        issues.append("No .metadata.jsonl files produced")
        return issues

    if len(jsonl_files) < expected_segments:
        issues.append(f"Expected {expected_segments} metadata journals, found {len(jsonl_files)}")

    for path in jsonl_files:
        line_count, parse_errors = 0, 0
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    line_count += 1
                    try:
                        record = json.loads(line)
                        if "camera_serials" not in record or "timestamps" not in record:
                            parse_errors += 1
                    except json.JSONDecodeError:
                        parse_errors += 1
        except Exception as e:
            issues.append(f"{os.path.basename(path)}: {e}")
            continue

        if parse_errors > 0:
            issues.append(f"{os.path.basename(path)}: {parse_errors}/{line_count} lines failed JSON parse")

    # Aggregate JSON files
    for path in sorted(glob.glob(os.path.join(output_dir, "*.json"))):
        if path.endswith(".metadata.jsonl"):
            continue
        try:
            with open(path) as f:
                data = json.load(f)
            if "real_times" not in data or "timestamps" not in data:
                issues.append(f"{os.path.basename(path)}: missing expected fields")
        except json.JSONDecodeError as e:
            issues.append(f"{os.path.basename(path)}: invalid JSON — {e}")
        except Exception as e:
            issues.append(f"{os.path.basename(path)}: {e}")

    return issues
