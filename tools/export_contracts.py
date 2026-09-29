"""Regenerate checked-in schemas/fixtures from the canonical Python contract."""

import json
from pathlib import Path

from infra_pulse_backend.api.app import attention_fixture, create_app, fixture
from infra_pulse_backend.api.forecast_fixture import forecast_fixture_document
from infra_pulse_backend.api.product_fixture import product_fixture_document
from infra_pulse_backend.config import Settings


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = root / "contracts"
    contracts.mkdir(exist_ok=True)
    app = create_app(Settings(mode="scaffold", _env_file=None))
    (contracts / "openapi.json").write_text(
        json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (contracts / "risk.fixture.json").write_text(
        fixture().model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    (contracts / "attention.fixture.json").write_text(
        attention_fixture().model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    (contracts / "forecast.fixture.json").write_text(
        json.dumps(forecast_fixture_document(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (contracts / "product.fixture.json").write_text(
        json.dumps(product_fixture_document(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
