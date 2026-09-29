"""Verify HTTP tracking and artifact roundtrip with synthetic data, no model fit."""

import argparse
import json
import tempfile
from pathlib import Path


def main():
    import mlflow
    from mlflow import MlflowClient

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tracking-uri", default="http://127.0.0.1:5000")
    args = parser.parse_args()
    mlflow.set_tracking_uri(args.tracking_uri)
    mlflow.set_experiment("infra-connectivity-smoke")
    payload = {"synthetic": True, "models_fitted": 0, "model_quality_claim": False}
    with mlflow.start_run(run_name="synthetic-artifact-roundtrip") as run:
        mlflow.set_tag("source_mode", "synthetic_smoke")
        mlflow.log_param("purpose", "connectivity")
        mlflow.log_metric("roundtrip_check", 1)
        mlflow.log_dict(payload, "smoke.json")
        run_id = run.info.run_id
    client = MlflowClient()
    saved = client.get_run(run_id)
    assert saved.info.status == "FINISHED"
    assert saved.data.params["purpose"] == "connectivity"
    assert saved.data.metrics["roundtrip_check"] == 1
    with tempfile.TemporaryDirectory(prefix="infra-mlflow-smoke-") as directory:
        path = client.download_artifacts(run_id, "smoke.json", directory)
        assert json.loads(Path(path).read_text(encoding="utf-8")) == payload
    print(json.dumps({"run_id": run_id, "http_tracking": "passed", "artifact_roundtrip": "passed"}))


if __name__ == "__main__":
    main()
