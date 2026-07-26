from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from pypdf import PdfWriter


@contextmanager
def synthetic_debug_pdfs(root: Path) -> Iterator[list[Path]]:
    """Create only the missing PDFs used by the committed synthetic DEBUG fixture."""
    manifest_path = root / "debug_data" / "researches" / "debug_laser_demo" / "patents.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    names = [
        str(item["pdf"])
        for item in payload.get("patents", [])
        if str(item.get("pdf", "")).startswith("SAMPLE-")
    ]
    pool = root / "patent_pool"
    pool.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    try:
        for name in names:
            path = pool / name
            if path.exists():
                continue
            writer = PdfWriter()
            writer.add_blank_page(width=612, height=792)
            writer.add_metadata({
                "/Title": "PatentViewer synthetic DEBUG fixture",
                "/Subject": "Generated locally for UI regression; contains no patent content",
            })
            with path.open("wb") as output:
                writer.write(output)
            created.append(path)
        yield created
    finally:
        for path in created:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
