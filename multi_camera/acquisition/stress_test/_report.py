"""Report formatting and saving for deployment validation."""

from __future__ import annotations

import json
import re
from datetime import datetime

W = 60
PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
WARN = "\033[93m!\033[0m"
_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


class Report:
    """Accumulates report lines for terminal display and file export."""

    def __init__(self):
        self.lines: list[str] = []
        self.issues: list[str] = []
        self.json_data: dict = {"checks": {}}

    def log(self, line: str = ""):
        print(line)
        self.lines.append(line)

    def header(self, title: str):
        self.log()
        self.log("═" * W)
        self.log(f"  {title}")
        self.log("═" * W)

    def row(self, label: str, value: str, status: str = ""):
        self.log(f"  {label:<18s} {value:<28s} {status}")

    def check(self, label: str, value: str, good: bool, bad: bool = False):
        """Log a check result: good=PASS, bad=FAIL, otherwise WARN."""
        if good:
            self.row(label, value, PASS)
        elif bad:
            self.row(label, value, FAIL)
        else:
            self.row(label, value, WARN)

    def issue(self, msg: str):
        self.issues.append(msg)

    def save(self, output_dir: str):
        """Save report.txt (plain text) and report.json (machine-readable)."""
        # Plain text
        txt_path = f"{output_dir}/report.txt"
        with open(txt_path, "w") as f:
            f.write(f"Deployment Validation — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            for line in self.lines:
                f.write(_ANSI_RE.sub("", line) + "\n")

        # JSON
        self.json_data["issues"] = self.issues
        json_path = f"{output_dir}/report.json"
        with open(json_path, "w") as f:
            json.dump(self.json_data, f, indent=2, default=str)
