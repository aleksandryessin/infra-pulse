"""Aggregated research summary for the «Исследование» page (read-only).

Fixture mode serves a synthetic shape; other modes read ``research_summary_path``
(an aggregated JSON produced by data-science, mounted read-only) and validate it.
"""

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError

from infra_pulse_backend.api import product_fixture
from infra_pulse_backend.api.auth_deps import require_permission
from infra_pulse_backend.config import Settings
from infra_pulse_core.contracts.research import ResearchSummary


def build_router(config: Settings) -> APIRouter:
    # «Исследование»: analysts and administrators only; dispatchers get 403.
    router = APIRouter(dependencies=[Depends(require_permission("research"))])

    @router.get("/api/v1/research-summary", response_model=ResearchSummary)
    def research_summary() -> ResearchSummary:
        if config.mode == "fixture":
            return product_fixture.research_summary()
        path = config.research_summary_path
        if path is None or not path.is_file():
            raise HTTPException(status_code=503, detail="research_summary_missing")
        try:
            return ResearchSummary.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, ValidationError) as error:
            raise HTTPException(status_code=503, detail="research_summary_invalid") from error

    return router
