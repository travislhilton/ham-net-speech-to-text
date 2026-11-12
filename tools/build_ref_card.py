"""Build the printable reference card: docs/HamNet_Station_Quick_Reference_Card.pdf (and its .html).

    python -m tools.build_ref_card

The card is station/static/card.html (the dashboard's Help shows the same card). This fills in the download QR code
(the public project page) and prints it with Chrome (headless), US Letter, one card side per page, so the PDF prints
front and back on one sheet ("flip on long edge") — the same layout as the Church of Jesus Christ EMCOMM quick
reference cards. It fails when a side does not fit one page (the PDF would not have exactly 2 pages).
Needs Google Chrome.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from station import phone

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "station" / "static" / "card.html"
OUT = ROOT / "docs"
NAME = "HamNet_Station_Quick_Reference_Card"


def chrome() -> str:
    for p in (os.environ.get("CHROME", ""), r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
        if p and os.path.isfile(p):
            return p
    return shutil.which("google-chrome") or shutil.which("chromium") or ""


def main() -> int:
    html = phone.card_with_download(SRC.read_text(encoding="utf-8"), phone.REPO_URL)
    OUT.mkdir(exist_ok=True)
    out_html = OUT / f"{NAME}.html"
    out_html.write_text(html, encoding="utf-8")
    exe = chrome()
    if not exe:
        print("Google Chrome not found: wrote the HTML only (open it and Print)")
        return 1
    pdf = OUT / f"{NAME}.pdf"
    with tempfile.TemporaryDirectory() as prof:
        subprocess.run([exe, "--headless=new", "--disable-gpu", f"--user-data-dir={prof}", "--no-pdf-header-footer",
                        f"--print-to-pdf={pdf}", out_html.as_uri()], check=True, capture_output=True, timeout=120)
    pages = len(re.findall(rb"/Type\s*/Page(?!s)", pdf.read_bytes()))
    print(f"{pdf.relative_to(ROOT)}: {pages} pages")
    if pages != 2:
        print("a card side does not fit one Letter page: shorten it")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
