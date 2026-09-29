"""Executable dependency boundaries and parity of published API artifacts."""

import ast
import json
import subprocess
import sys
from pathlib import Path

from infra_pulse_backend.api.app import attention_fixture, create_app, fixture
from infra_pulse_backend.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def test_api_handles_fixture_without_importing_research_or_ml():
    # A fresh interpreter avoids already-loaded modules hiding forbidden imports.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib.abc
import sys
class BlockResearch(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {
            'infra_pulse_research', 'mlflow', 'numpy', 'pandas',
            'catboost', 'sklearn', 'duckdb', 'pyarrow', 'torch'
        }:
            raise AssertionError('HTTP imported ' + fullname)
sys.meta_path.insert(0, BlockResearch())
from fastapi.testclient import TestClient
from infra_pulse_backend.api.app import create_app
from infra_pulse_backend.config import Settings
with TestClient(create_app(Settings(mode='fixture', _env_file=None))) as client:
    assert client.get('/health/live').status_code == 200
    assert client.get('/health/ready').status_code == 503
    result = client.get('/api/v1/risks')
    assert result.status_code == 200
    assert result.json()['items'][0]['source'] == 'synthetic_fixture'
""",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_dependency_direction_in_source():
    boundaries = {
        ROOT / "backend/src": {"infra_pulse_research", "mlflow", "torch"},
        ROOT / "packages/core/src": {
            "infra_pulse_backend",
            "infra_pulse_research",
            "mlflow",
            "sklearn",
            "catboost",
            "torch",
        },
    }
    for directory, forbidden in boundaries.items():
        for source in directory.rglob("*.py"):
            for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    modules = [node.module]
                for module in modules:
                    assert module.split(".")[0] not in forbidden, (source, module)


def test_generated_api_artifacts_match_canonical_contract():
    app = create_app(Settings(mode="scaffold", _env_file=None))
    assert (
        json.loads((ROOT / "contracts/openapi.json").read_text(encoding="utf-8")) == app.openapi()
    )
    assert json.loads(
        (ROOT / "contracts/risk.fixture.json").read_text(encoding="utf-8")
    ) == json.loads(fixture().model_dump_json())
    assert json.loads(
        (ROOT / "contracts/attention.fixture.json").read_text(encoding="utf-8")
    ) == json.loads(attention_fixture().model_dump_json())
