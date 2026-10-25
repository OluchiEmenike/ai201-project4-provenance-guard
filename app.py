import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, current_app, g, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from detector import DetectionError, analyze_text

load_dotenv()


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(str(current_app.config["DATABASE"]))
        g.db.row_factory = sqlite3.Row
    return g.db


def init_db() -> None:
    database = Path(str(current_app.config["DATABASE"]))
    database.parent.mkdir(parents=True, exist_ok=True)
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS submissions (
            content_id TEXT PRIMARY KEY,
            creator_id TEXT,
            content_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            result TEXT NOT NULL,
            combined_ai_likelihood REAL,
            confidence REAL NOT NULL,
            label_type TEXT NOT NULL,
            label_text TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            content_id TEXT NOT NULL,
            decision_id TEXT NOT NULL,
            creator_id TEXT,
            timestamp TEXT NOT NULL,
            result TEXT,
            combined_ai_likelihood REAL,
            confidence REAL,
            signals_json TEXT,
            label_type TEXT,
            label_text TEXT,
            details_json TEXT NOT NULL
        );
        """
    )
    for table in ("submissions", "audit_events"):
        columns = {
            row["name"] for row in db.execute(f"PRAGMA table_info({table})")
        }
        if "creator_id" not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN creator_id TEXT")
        if "combined_ai_likelihood" not in columns:
            db.execute(
                f"ALTER TABLE {table} ADD COLUMN combined_ai_likelihood REAL"
            )
    db.commit()


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_app(test_config: dict[str, object] | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_mapping(
        DATABASE=str(Path(app.instance_path) / "provenance.sqlite3"),
        MAX_CONTENT_LENGTH=100_000,
    )
    if test_config:
        app.config.update(test_config)

    limiter = Limiter(
        key_func=get_remote_address,
        default_limits=[],
        storage_uri="memory://",
    )
    limiter.init_app(app)
    app.extensions["project_limiter"] = limiter
    app.extensions["classifier"] = (
        test_config.get("CLASSIFIER") if test_config else None
    )

    @app.teardown_appcontext
    def close_db(_error: BaseException | None = None) -> None:
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.errorhandler(429)
    def handle_rate_limit(_error):
        return jsonify(error="Rate limit exceeded. Please try again later."), 429

    with app.app_context():
        init_db()

    @app.get("/")
    def home():
        return "Provenance Guard is running."

    @app.post("/submit")
    @limiter.limit("5 per hour")
    def submit():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            return jsonify(error="Request body must be JSON with a text string."), 400

        text = payload["text"].strip()
        if not text:
            return jsonify(error="Text must not be empty."), 400
        if len(text) > 50_000:
            return jsonify(error="Text must be 50,000 characters or fewer."), 400

        creator_id = payload.get("creator_id")
        if not isinstance(creator_id, str):
            return jsonify(error="Request body must include a creator_id string."), 400
        creator_id = creator_id.strip()
        if not creator_id:
            return jsonify(error="creator_id must not be empty."), 400
        if len(creator_id) > 200:
            return jsonify(error="creator_id must be 200 characters or fewer."), 400

        try:
            classifier = app.extensions["classifier"]
            if classifier:
                decision = analyze_text(text, classifier=classifier)
            else:
                decision = analyze_text(text)
        except DetectionError as exc:
            return jsonify(error=str(exc)), 503

        content_id = str(uuid.uuid4())
        timestamp = utc_timestamp()
        label = decision["label"]
        signals_json = json.dumps(decision["signals"])
        label_type = label["type"]
        label_text = label["text"]
        db = get_db()
        db.execute(
            """
            INSERT INTO submissions
                (content_id, creator_id, content_hash, status, result,
                 combined_ai_likelihood, confidence, label_type, label_text,
                 created_at)
            VALUES (?, ?, ?, 'classified', ?, ?, ?, ?, ?, ?)
            """,
            (
                content_id,
                creator_id,
                hashlib.sha256(text.encode("utf-8")).hexdigest(),
                decision["result"],
                decision["combined_ai_likelihood"],
                decision["confidence"],
                label_type,
                label_text,
                timestamp,
            ),
        )
        db.execute(
            """
            INSERT INTO audit_events
                (event_type, content_id, decision_id, creator_id, timestamp,
                 result, combined_ai_likelihood, confidence, signals_json,
                 label_type, label_text, details_json)
            VALUES ('decision', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                content_id,
                content_id,
                creator_id,
                timestamp,
                decision["result"],
                decision["combined_ai_likelihood"],
                decision["confidence"],
                signals_json,
                label_type,
                label_text,
                json.dumps({"status": "classified"}),
            ),
        )
        db.commit()

        return jsonify(
            content_id=content_id,
            creator_id=creator_id,
            status="classified",
            attribution=decision["result"],
            result=decision["result"],
            combined_ai_likelihood=decision["combined_ai_likelihood"],
            confidence=decision["confidence"],
            llm_score=decision["signals"]["llm"]["ai_likelihood"],
            label=label,
            signals=decision["signals"],
        ), 201

    @app.post("/appeal")
    @app.post("/appeals")
    @limiter.limit("5 per hour")
    def appeal():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error="Request body must be a JSON object."), 400
        content_id = payload.get("content_id")
        reasoning = payload.get("creator_reasoning", payload.get("reasoning"))
        if not isinstance(content_id, str) or not isinstance(reasoning, str):
            return jsonify(error="content_id and reasoning must be strings."), 400
        reasoning = reasoning.strip()
        if not reasoning:
            return jsonify(error="Appeal reasoning must not be empty."), 400
        if len(reasoning) > 5_000:
            return (
                jsonify(error="Appeal reasoning must be 5,000 characters or fewer."),
                400,
            )

        db = get_db()
        submission = db.execute(
            "SELECT * FROM submissions WHERE content_id = ?", (content_id,)
        ).fetchone()
        if submission is None:
            return jsonify(error="No analyzed content found for that content_id."), 404
        if submission["status"] == "under_review":
            return jsonify(error="This content already has an appeal under review."), 409

        timestamp = utc_timestamp()
        db.execute(
            "UPDATE submissions SET status = 'under_review' WHERE content_id = ?",
            (content_id,),
        )
        db.execute(
            """
            INSERT INTO audit_events
                (event_type, content_id, decision_id, creator_id, timestamp,
                 result, combined_ai_likelihood, confidence, signals_json,
                 label_type, label_text, details_json)
            VALUES (
                'appeal', ?, ?, ?, ?, ?, ?, ?,
                (
                    SELECT signals_json FROM audit_events
                    WHERE event_type = 'decision' AND content_id = ?
                    ORDER BY id LIMIT 1
                ),
                ?, ?, ?
            )
            """,
            (
                content_id,
                content_id,
                submission["creator_id"],
                timestamp,
                submission["result"],
                submission["combined_ai_likelihood"],
                submission["confidence"],
                content_id,
                submission["label_type"],
                submission["label_text"],
                json.dumps({"creator_reasoning": reasoning, "status": "under_review"}),
            ),
        )
        db.commit()
        return jsonify(
            content_id=content_id,
            status="under_review",
            message="Appeal received and is under review.",
        ), 201

    @app.get("/log")
    def audit_log():
        rows = get_db().execute(
            """
            SELECT id, event_type, content_id, decision_id, creator_id,
                   timestamp, result, combined_ai_likelihood, confidence,
                   signals_json, label_type, label_text, details_json
            FROM audit_events ORDER BY id DESC
            """
        ).fetchall()
        entries = []
        for row in rows:
            entry = dict(row)
            entry["signals"] = (
                json.loads(entry.pop("signals_json"))
                if entry["signals_json"]
                else None
            )
            entry["details"] = json.loads(entry.pop("details_json"))
            entry["attribution"] = entry["result"]
            entry["llm_score"] = (
                entry["signals"]["llm"]["ai_likelihood"]
                if entry["signals"] and "llm" in entry["signals"]
                else None
            )
            entry["status"] = entry["details"].get("status")
            if entry["event_type"] == "appeal":
                entry["appeal_reasoning"] = entry["details"].get(
                    "creator_reasoning"
                )
            entries.append(entry)
        return jsonify(count=len(entries), entries=entries)

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
