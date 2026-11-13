"""Flask application assembly; business logic lives in domain modules."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flask import Flask, render_template

from .api import create_api_blueprint
from .clock import Clock, SystemClock, format_time
from .config import Policy, default_db_path, default_fixtures_dir, load_policy
from .db import Database
from .store import list_batches, load_credentials
from .verification_store import latest_verifications


def create_app(
    db_path: str | Path | None = None,
    clock: Clock | None = None,
    policy: Policy | None = None,
    fixtures_dir: str | Path | None = None,
) -> Flask:
    base = Path(__file__).resolve().parent
    app = Flask(
        __name__,
        template_folder=str(base / "web" / "templates"),
        static_folder=str(base / "web" / "static"),
        static_url_path="/static",
    )
    db = Database(db_path or default_db_path())
    db.initialize()
    resolved_clock = clock or SystemClock()
    resolved_policy = policy or _load_policy(fixtures_dir or default_fixtures_dir())
    app.config["CREDROT_DB"] = db.path
    app.register_blueprint(
        create_api_blueprint(db, resolved_clock, resolved_policy)
    )

    @app.get("/")
    def index() -> Any:
        now = resolved_clock.now()
        observations = latest_verifications(db)
        rows = []
        for credential in sorted(
            load_credentials(db).values(), key=lambda item: item.id
        ):
            row = credential.to_dict(now)
            row["verification"] = observations.get(credential.id)
            rows.append(row)
        return render_template(
            "index.html",
            generated_at=format_time(now),
            credentials=rows,
            batches=list_batches(db, limit=8),
            db_path=db.path,
        )

    return app


def _load_policy(fixtures_dir: Path) -> Policy:
    policy, issues = load_policy(Path(fixtures_dir) / "policy.yaml")
    if issues:
        detail = "; ".join(issue.render() for issue in issues)
        raise ValueError(f"invalid policy for server: {detail}")
    return policy
