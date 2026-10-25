import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import create_app
from detector import analyze_text, create_transparency_label, stylometric_signal


class DetectorTests(unittest.TestCase):
    def test_short_text_has_neutral_stylometric_estimate(self):
        result = stylometric_signal("A short poem, softly spoken.")
        self.assertEqual(result["ai_likelihood"], 0.5)
        self.assertIn("Insufficient text", result["evidence"])

    def test_repeated_uniform_text_scores_more_ai_like(self):
        repeated = "The quiet morning arrives. The quiet morning arrives. " * 8
        varied = (
            "At dawn, the harbor wakes. Somewhere a gull interrupts the fog! "
            "I count the boats, then forget why. By noon, rain stitches the "
            "windows; evening comes much later than expected."
        )
        self.assertGreater(
            stylometric_signal(repeated)["ai_likelihood"],
            stylometric_signal(varied)["ai_likelihood"],
        )

    def test_ai_threshold_applies_even_when_signals_disagree(self):
        text = "The quiet morning arrives. The quiet morning arrives. " * 8
        ai = analyze_text(
            text,
            classifier=lambda _text: {
                "ai_likelihood": 0.99,
                "rationale": "Consistent generic phrasing.",
            },
        )
        conflict = analyze_text(
            "A short text.",
            classifier=lambda _text: {
                "ai_likelihood": 1.0,
                "rationale": "Consistent generic phrasing.",
            },
        )
        self.assertEqual(ai["result"], "likely_ai")
        self.assertEqual(ai["combined_ai_likelihood"], 0.9922)
        self.assertEqual(conflict["signals"]["stylometric"]["ai_likelihood"], 0.5)
        self.assertEqual(conflict["combined_ai_likelihood"], 0.89)
        self.assertGreater(
            abs(
                conflict["signals"]["llm"]["ai_likelihood"]
                - conflict["signals"]["stylometric"]["ai_likelihood"]
            ),
            0.35,
        )
        self.assertEqual(conflict["result"], "likely_ai")

    def test_score_bands_classify_ai_human_and_uncertain(self):
        text = "A short text."
        below_human_threshold = analyze_text(
            text,
            classifier=lambda _text: {
                "ai_likelihood": 0.3,
                "rationale": "Test fixture below the human threshold.",
            },
        )
        low_score_with_conflicting_signals = analyze_text(
            text,
            classifier=lambda _text: {
                "ai_likelihood": 0.0,
                "rationale": "Test fixture with disagreement below the human cutoff.",
            },
        )
        at_human_threshold = analyze_text(
            text,
            classifier=lambda _text: {
                "ai_likelihood": (0.35 - 0.11) / 0.78,
                "rationale": "Test fixture at the human threshold.",
            },
        )
        uncertain = analyze_text(
            text,
            classifier=lambda _text: {
                "ai_likelihood": 0.6,
                "rationale": "Test fixture in the uncertain range.",
            },
        )
        at_ai_threshold = analyze_text(
            text,
            classifier=lambda _text: {
                "ai_likelihood": (0.75 - 0.11) / 0.78,
                "rationale": "Test fixture at the AI threshold.",
            },
        )
        above_ai_threshold = analyze_text(
            text,
            classifier=lambda _text: {
                "ai_likelihood": 0.83,
                "rationale": "Test fixture above the AI threshold.",
            },
        )
        self.assertLess(below_human_threshold["combined_ai_likelihood"], 0.35)
        self.assertEqual(below_human_threshold["result"], "likely_human")
        self.assertEqual(
            low_score_with_conflicting_signals["combined_ai_likelihood"], 0.11
        )
        self.assertEqual(
            low_score_with_conflicting_signals["result"], "likely_human"
        )
        self.assertEqual(at_human_threshold["combined_ai_likelihood"], 0.35)
        self.assertEqual(at_human_threshold["result"], "uncertain")
        self.assertEqual(uncertain["result"], "uncertain")
        self.assertEqual(at_ai_threshold["combined_ai_likelihood"], 0.75)
        self.assertEqual(at_ai_threshold["result"], "uncertain")
        self.assertGreater(above_ai_threshold["combined_ai_likelihood"], 0.75)
        self.assertEqual(above_ai_threshold["result"], "likely_ai")

    def test_four_milestone_samples_produce_distinct_combined_scores(self):
        samples = {
            "clearly_ai": (
                "Artificial intelligence represents a transformative paradigm shift in modern society. "
                "It is important to note that while the benefits of AI are numerous, it is equally "
                "essential to consider the ethical implications. Furthermore, stakeholders across "
                "various sectors must collaborate to ensure responsible deployment.",
                0.99,
            ),
            "clearly_human": (
                "ok so i finally tried that new ramen place downtown and honestly? underwhelming. "
                "the broth was fine but they put WAY too much sodium in it and i was thirsty for "
                "like three hours after. my friend got the spicy version and said it was better. "
                "probably won't go back unless someone drags me there",
                0.01,
            ),
            "formal_borderline": (
                "The relationship between monetary policy and asset price inflation has been "
                "extensively studied in the literature. Central banks face a fundamental tension "
                "between their mandate for price stability and the unintended consequences of "
                "prolonged low interest rates on equity and real estate valuations.",
                0.65,
            ),
            "edited_ai_borderline": (
                "I've been thinking a lot about remote work lately. There are genuine tradeoffs — "
                "flexibility and no commute on one side, isolation and blurred work-life boundaries "
                "on the other. Studies show productivity varies widely by individual and role type.",
                0.50,
            ),
        }
        results = {}
        for name, (text, llm_score) in samples.items():
            results[name] = analyze_text(
                text,
                classifier=lambda _text, score=llm_score: {
                    "ai_likelihood": score,
                    "rationale": "Deterministic test fixture; not a live Groq assessment.",
                },
            )

        self.assertEqual(
            set(results),
            {"clearly_ai", "clearly_human", "formal_borderline", "edited_ai_borderline"},
        )
        expected_results = {
            "clearly_ai": "likely_ai",
            "clearly_human": "likely_human",
            "formal_borderline": "uncertain",
            "edited_ai_borderline": "uncertain",
        }
        for name, result in results.items():
            self.assertIn("ai_likelihood", result["signals"]["llm"])
            self.assertIn("ai_likelihood", result["signals"]["stylometric"])
            self.assertGreaterEqual(result["combined_ai_likelihood"], 0)
            self.assertLessEqual(result["combined_ai_likelihood"], 1)
            self.assertEqual(result["result"], expected_results[name])

        scores = {
            name: result["combined_ai_likelihood"]
            for name, result in results.items()
        }
        self.assertGreater(scores["clearly_ai"], scores["formal_borderline"])
        self.assertGreater(scores["formal_borderline"], scores["edited_ai_borderline"])
        self.assertGreater(scores["edited_ai_borderline"], scores["clearly_human"])
        self.assertNotEqual(
            results["clearly_ai"]["confidence"],
            results["clearly_human"]["confidence"],
        )

    def test_highly_irregular_text_can_receive_likely_human_result(self):
        text = (
            "I left at dawn. Why? The station was empty, except for one dog, who "
            "seemed to recognize me; or maybe he just wanted breakfast. Anyway, I "
            "missed the bus. Twice, actually, because I forgot the date, lost my "
            "keys, and circled back through the rain before the bakery opened. "
            "By then, the whole plan had changed."
        )
        result = analyze_text(
            text,
            classifier=lambda _text: {
                "ai_likelihood": 0.0,
                "rationale": "Test fixture indicates human-like phrasing.",
            },
        )
        self.assertEqual(result["result"], "likely_human")

    def test_all_three_transparency_labels_are_reachable(self):
        samples = (
            (
                "The quiet morning arrives. The quiet morning arrives. " * 8,
                0.99,
                "likely_ai",
                "likely_ai",
                "Likely AI-generated",
            ),
            (
                "I left at dawn. Why? The station was empty, except for one dog, "
                "who seemed to recognize me; or maybe he just wanted breakfast. "
                "Anyway, I missed the bus. Twice, actually, because I forgot the "
                "date, lost my keys, and circled back through the rain before "
                "the bakery opened. By then, the whole plan had changed.",
                0.0,
                "likely_human",
                "likely_human",
                "Likely human-written",
            ),
            (
                "A sufficiently long sample for both independent signals.",
                0.51,
                "uncertain",
                "uncertain",
                "Uncertain",
            ),
        )
        for text, llm_score, expected_result, expected_type, expected_prefix in samples:
            result = analyze_text(
                text,
                classifier=lambda _text, score=llm_score: {
                    "ai_likelihood": score,
                    "rationale": "Deterministic label test fixture.",
                },
            )
            self.assertEqual(result["result"], expected_result)
            self.assertEqual(result["label"]["type"], expected_type)
            self.assertTrue(result["label"]["text"].startswith(expected_prefix))
            self.assertEqual(
                result["label"],
                create_transparency_label(expected_result, result["confidence"]),
            )

    def test_confidence_changes_transparency_label(self):
        text = "A sufficiently long sample for both independent signals."
        low = analyze_text(
            text,
            classifier=lambda _text: {
                "ai_likelihood": 0.51,
                "rationale": "Evidence is mixed.",
            },
        )
        high = analyze_text(
            text,
            classifier=lambda _text: {
                "ai_likelihood": 0.99,
                "rationale": "Evidence suggests AI-like prose.",
            },
        )
        self.assertLess(low["confidence"], high["confidence"])
        self.assertGreaterEqual(low["confidence"], 0)
        self.assertLessEqual(high["confidence"], 1)
        self.assertNotEqual(low["label"]["text"], high["label"]["text"])


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database = Path(self.temp_dir.name) / "test.sqlite3"
        self.app = create_app(
            {
                "TESTING": True,
                "RATELIMIT_ENABLED": False,
                "DATABASE": str(database),
                "CLASSIFIER": lambda _text: {
                    "ai_likelihood": 0.99,
                    "rationale": "Test classifier indicates AI-like style.",
                },
            }
        )
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_home_route_reports_running_status(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_data(as_text=True), "Provenance Guard is running."
        )

    def test_existing_database_is_migrated_for_creator_ids(self):
        database = Path(self.temp_dir.name) / "legacy.sqlite3"
        connection = sqlite3.connect(database)
        try:
            connection.executescript(
                """
                CREATE TABLE submissions (
                    content_id TEXT PRIMARY KEY,
                    content_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    label_type TEXT NOT NULL,
                    label_text TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    content_id TEXT NOT NULL,
                    decision_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    result TEXT,
                    confidence REAL,
                    signals_json TEXT,
                    label_type TEXT,
                    label_text TEXT,
                    details_json TEXT NOT NULL
                );
                """
            )
        finally:
            connection.close()
        create_app(
            {
                "TESTING": True,
                "RATELIMIT_ENABLED": False,
                "DATABASE": str(database),
            }
        )
        connection = sqlite3.connect(database)
        try:
            for table in ("submissions", "audit_events"):
                columns = {
                    row[1]
                    for row in connection.execute(f"PRAGMA table_info({table})")
                }
                self.assertIn("creator_id", columns)
                self.assertIn("combined_ai_likelihood", columns)
        finally:
            connection.close()

    def test_submit_records_decision_with_two_signals(self):
        response = self.client.post(
            "/submit",
            json={
                "text": "A passage to analyze for authorship.",
                "creator_id": "test-user-1",
            },
        )
        self.assertEqual(response.status_code, 201)
        payload = response.get_json()
        self.assertIn("confidence", payload)
        self.assertEqual(payload["attribution"], payload["result"])
        self.assertEqual(payload["status"], "classified")
        self.assertEqual(
            payload["llm_score"], payload["signals"]["llm"]["ai_likelihood"]
        )
        self.assertIn("combined_ai_likelihood", payload)
        self.assertEqual(set(payload["signals"]), {"llm", "stylometric"})
        self.assertIn("type", payload["label"])
        log = self.client.get("/log").get_json()
        self.assertEqual(log["count"], 1)
        self.assertEqual(log["entries"][0]["event_type"], "decision")
        self.assertEqual(log["entries"][0]["creator_id"], "test-user-1")
        self.assertEqual(log["entries"][0]["attribution"], payload["attribution"])
        self.assertEqual(log["entries"][0]["llm_score"], payload["llm_score"])
        self.assertEqual(
            log["entries"][0]["combined_ai_likelihood"],
            payload["combined_ai_likelihood"],
        )
        self.assertEqual(log["entries"][0]["status"], "classified")

    def test_singular_appeal_route_accepts_creator_reasoning(self):
        submitted = self.client.post(
            "/submit",
            json={
                "text": "A passage to analyze for authorship.",
                "creator_id": "test-user-1",
            },
        ).get_json()
        self.client.post(
            "/submit",
            json={
                "text": "A second passage to include in the audit log.",
                "creator_id": "test-user-2",
            },
        )
        response = self.client.post(
            "/appeal",
            json={
                "content_id": submitted["content_id"],
                "creator_reasoning": "I wrote this and can provide drafts.",
            },
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["status"], "under_review")
        self.assertEqual(
            response.get_json()["message"],
            "Appeal received and is under review.",
        )
        log = self.client.get("/log").get_json()
        self.assertEqual(log["count"], 3)
        appeal_entry = log["entries"][0]
        self.assertEqual(log["entries"][1]["event_type"], "decision")
        self.assertEqual(log["entries"][2]["event_type"], "decision")
        self.assertEqual(appeal_entry["status"], "under_review")
        self.assertEqual(appeal_entry["creator_id"], "test-user-1")
        self.assertEqual(
            appeal_entry["appeal_reasoning"],
            "I wrote this and can provide drafts.",
        )
        self.assertEqual(
            appeal_entry["signals"],
            log["entries"][2]["signals"],
        )
        self.assertEqual(
            appeal_entry["details"]["creator_reasoning"],
            "I wrote this and can provide drafts.",
        )

    def test_appeal_updates_status_and_is_audited(self):
        submitted = self.client.post(
            "/submit",
            json={
                "text": "A passage to analyze for authorship.",
                "creator_id": "test-user-1",
            },
        ).get_json()
        response = self.client.post(
            "/appeals",
            json={
                "content_id": submitted["content_id"],
                "reasoning": "I wrote this myself and can provide drafts.",
            },
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["status"], "under_review")
        log = self.client.get("/log").get_json()
        appeal_entry = log["entries"][0]
        self.assertEqual(appeal_entry["event_type"], "appeal")
        self.assertEqual(log["entries"][1]["event_type"], "decision")
        self.assertEqual(
            appeal_entry["details"]["creator_reasoning"],
            "I wrote this myself and can provide drafts.",
        )

    def test_invalid_submission_and_unknown_appeal_are_rejected(self):
        self.assertEqual(
            self.client.post("/submit", json={"text": "  "}).status_code, 400
        )
        self.assertEqual(
            self.client.post(
                "/submit", json={"text": "A valid sample without its creator ID."}
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                "/appeals", json={"content_id": "missing", "reasoning": "Please review."}
            ).status_code,
            404,
        )

    def test_missing_classifier_key_surfaces_as_service_error(self):
        app = create_app(
            {
                "TESTING": True,
                "RATELIMIT_ENABLED": False,
                "DATABASE": str(Path(self.temp_dir.name) / "missing-key.sqlite3"),
            }
        )
        with patch.dict("os.environ", {"GROQ_API_KEY": ""}):
            response = app.test_client().post(
                "/submit",
                json={
                    "text": "A passage that needs attribution.",
                    "creator_id": "test-user-1",
                },
            )
        self.assertEqual(response.status_code, 503)
        self.assertIn("GROQ_API_KEY", response.get_json()["error"])

    def test_submission_rate_limit_returns_json_after_five_requests(self):
        limited_app = create_app(
            {
                "TESTING": True,
                "DATABASE": str(Path(self.temp_dir.name) / "limited.sqlite3"),
                "CLASSIFIER": lambda _text: {
                    "ai_likelihood": 0.5,
                    "rationale": "Mixed evidence.",
                },
            }
        )
        client = limited_app.test_client()
        client.environ_base["REMOTE_ADDR"] = "198.51.100.17"
        for _ in range(5):
            response = client.post(
                "/submit",
                json={"text": "A test passage.", "creator_id": "test-user-1"},
            )
            self.assertEqual(response.status_code, 201)
        limited = client.post(
            "/submit",
            json={"text": "A test passage.", "creator_id": "test-user-1"},
        )
        self.assertEqual(limited.status_code, 429)
        self.assertIn("error", limited.get_json())


if __name__ == "__main__":
    unittest.main()
