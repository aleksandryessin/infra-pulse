"""Notebooks are committed without outputs: outputs can carry rows of the organisers' journal."""

import json
from pathlib import Path

NOTEBOOKS = sorted((Path(__file__).resolve().parents[1] / "notebooks").glob("*.ipynb"))


def test_notebooks_are_committed_without_outputs():
    assert NOTEBOOKS
    dirty = []
    for path in NOTEBOOKS:
        cells = json.loads(path.read_text(encoding="utf-8"))["cells"]
        code = [cell for cell in cells if cell["cell_type"] == "code"]
        if any(cell.get("outputs") or cell.get("execution_count") for cell in code):
            dirty.append(path.name)
    assert dirty == [], f"clear outputs before committing: {dirty}"
