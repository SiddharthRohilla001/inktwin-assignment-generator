import asyncio
import base64
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from PIL import Image

from handwriting_agent import auth
from handwriting_agent.agent import AgentProviderError, HandwritingAgent
from handwriting_agent.api import app
from handwriting_agent.database import _TursoConnection, connect
from handwriting_agent.schemas import AgentResult


def make_png() -> bytes:
    image = Image.new("RGB", (128, 128), "white")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class HealthRouteTests(unittest.TestCase):
    def test_health_reports_provider_configuration(self) -> None:
        with TestClient(app) as client:
            response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertIn(response.json()["status"], {"ok", "not_configured"})
        self.assertIsInstance(response.json()["provider_configured"], bool)


class AuthenticatedApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.database = tempfile.TemporaryDirectory()
        self.addCleanup(self.database.cleanup)
        self.environment = patch.dict(
            os.environ,
            {
                "DATABASE_PATH": str(Path(self.database.name) / "test.sqlite3"),
                "AUTH_SECRET_KEY": "test-auth-secret-key-with-more-than-32-characters",
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.user_id = auth.create_user(
            "student@example.com", "secure-test-password", "123456"
        )
        auth.verify_email("student@example.com", "123456")
        self.token = auth.create_token(self.user_id)
        self.headers = {"Authorization": f"Bearer {self.token}"}


class AccountTests(AuthenticatedApiTestCase):
    def test_signup_requires_email_verification(self) -> None:
        sent = {}
        with patch(
            "handwriting_agent.auth.send_verification_email",
            side_effect=lambda email, code: sent.update(email=email, code=code),
        ):
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/auth/register",
                    json={
                        "email": "new-student@example.com",
                        "password": "long-enough-password",
                    },
                )
                verify = client.post(
                    "/api/v1/auth/verify",
                    json={"email": sent["email"], "code": sent["code"]},
                )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(verify.status_code, 200)
        self.assertEqual(verify.json()["user"]["assignments_generated"], 0)
        self.assertNotIn("paid_credits", verify.json()["user"])

    def test_authenticated_account_has_unlimited_assignment_reservations(self) -> None:
        reservations = []
        for _ in range(5):
            reservation = auth.reserve_assignment(self.user_id)
            self.assertIsNotNone(reservation)
            reservations.append(reservation)
            auth.finish_assignment(reservation[0], succeeded=True)

        with TestClient(app) as client:
            account = client.get("/api/v1/auth/me", headers=self.headers)
            routes = client.get("/openapi.json").json()["paths"]

        self.assertEqual(account.status_code, 200)
        self.assertEqual(account.json()["assignments_generated"], 5)
        self.assertNotIn("pending_gateway_order", account.json())
        self.assertFalse(any("/payments" in path for path in routes))

    def test_failed_generation_does_not_count_against_account(self) -> None:
        with TestClient(app) as client, patch(
            "handwriting_agent.api.agent.generate",
            new=AsyncMock(side_effect=AgentProviderError("Test provider failure.")),
        ):
            response = client.post(
                "/api/v1/pages",
                files={"sample": ("sample.png", make_png(), "image/png")},
                data={"prompt": "Write about trees"},
                headers=self.headers,
            )
            account = client.get("/api/v1/auth/me", headers=self.headers)

        self.assertEqual(response.status_code, 502)
        self.assertEqual(account.json()["assignments_generated"], 0)

    def test_generates_assignments_past_the_former_two_use_limit(self) -> None:
        generated_result = AgentResult.model_validate(
            {
                "language": "English",
                "style_profile": {
                    "script_type": "print",
                    "slant": "neutral",
                    "letter_shape": "rounded",
                    "stroke_weight": "medium",
                    "spacing": "even",
                    "line_spacing": "regular",
                    "confidence": "medium",
                    "limitations": [],
                },
                "pages": [
                    {"page_number": 1, "title": "Trees", "content": ["Trees matter."]}
                ],
                "rendering_note": "Style guidance only.",
            }
        )
        with TestClient(app) as client, patch(
            "handwriting_agent.api.agent.generate",
            new=AsyncMock(return_value=generated_result),
        ) as generate:
            responses = [
                client.post(
                    "/api/v1/pages",
                    files={"sample": ("sample.png", make_png(), "image/png")},
                    data={"prompt": "Write about trees"},
                    headers=self.headers,
                )
                for _ in range(3)
            ]
            account = client.get("/api/v1/auth/me", headers=self.headers)

        self.assertEqual([response.status_code for response in responses], [200, 200, 200])
        self.assertEqual(generate.await_count, 3)
        self.assertEqual(account.json()["assignments_generated"], 3)


class GenerateRouteValidationTests(AuthenticatedApiTestCase):
    def test_generation_requires_verified_account(self) -> None:
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/pages",
                files={"sample": ("sample.png", make_png(), "image/png")},
                data={"prompt": "Write about trees"},
            )

        self.assertEqual(response.status_code, 401)

    def test_rejects_non_image_upload(self) -> None:
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/pages",
                files={"sample": ("sample.jpg", b"not an image", "image/jpeg")},
                data={"prompt": "Write about trees"},
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("valid image", response.json()["detail"])

    def test_rejects_blank_prompt(self) -> None:
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/pages",
                files={"sample": ("sample.png", make_png(), "image/png")},
                data={"prompt": "   "},
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"], "Prompt must not be blank.")

    def test_returns_configuration_error_without_provider_key(self) -> None:
        with patch.dict("os.environ", {"LLM_API_KEY": "", "LLM_MODEL": ""}):
            from handwriting_agent import api

            original = api.agent
            api.agent = api.HandwritingAgent()
            try:
                with TestClient(app) as client:
                    response = client.post(
                        "/api/v1/pages",
                        files={"sample": ("sample.png", make_png(), "image/png")},
                        data={"prompt": "Write about trees"},
                        headers=self.headers,
                    )
            finally:
                api.agent = original

        self.assertEqual(response.status_code, 503)
        self.assertIn("LLM_API_KEY and LLM_MODEL", response.json()["detail"])
        self.assertEqual(auth.get_user(self.user_id)["assignments_generated"], 0)


class AgentGenerationTests(unittest.TestCase):
    def test_sends_sample_to_vision_model_and_returns_requested_pages(self) -> None:
        result_body = {
            "language": "English",
            "style_profile": {
                "script_type": "print",
                "slant": "slight right",
                "letter_shape": "rounded",
                "stroke_weight": "medium",
                "spacing": "even",
                "line_spacing": "regular",
                "confidence": "medium",
                "limitations": [],
            },
            "pages": [
                {"page_number": 1, "title": "Trees", "content": ["First page."]},
                {"page_number": 2, "title": "Trees", "content": ["Second page."]},
            ],
            "rendering_note": "Style guidance only.",
        }
        captured = {}

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"choices": [{"message": {"content": json.dumps(result_body)}}]}

        class FakeClient:
            def __init__(self, **_: object) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_: object) -> None:
                return None

            async def post(self, url: str, *, headers: dict, json: dict) -> FakeResponse:
                captured["url"] = url
                captured["payload"] = json
                return FakeResponse()

        with patch.dict(
            "os.environ",
            {
                "LLM_API_KEY": "test-key",
                "LLM_MODEL": "test-vision-model",
                "LLM_BASE_URL": "https://example.test/v1",
            },
        ):
            with patch("handwriting_agent.agent.httpx.AsyncClient", FakeClient):
                result = asyncio.run(
                    HandwritingAgent().generate(
                        image=b"test-image",
                        image_media_type="image/png",
                        prompt="Write about trees",
                        language="English",
                        page_count=2,
                    )
                )

        self.assertEqual(captured["url"], "https://example.test/v1/chat/completions")
        self.assertEqual(captured["payload"]["model"], "test-vision-model")
        self.assertEqual(
            captured["payload"]["messages"][1]["content"][1]["image_url"]["url"],
            "data:image/png;base64," + base64.b64encode(b"test-image").decode(),
        )
        self.assertEqual([page.page_number for page in result.pages], [1, 2])
        self.assertIn("will not exactly", result.rendering_note)


class TursoDatabaseAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = tempfile.TemporaryDirectory()
        self.addCleanup(self.database.cleanup)
        self.database_path = str(Path(self.database.name) / "turso-adapter.sqlite3")
        self.environment = patch.dict(
            os.environ,
            {"AUTH_SECRET_KEY": "test-auth-secret-key-with-more-than-32-characters"},
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.connector = patch(
            "handwriting_agent.auth.connect",
            lambda: _TursoConnection(f"file:{self.database_path}", ""),
        )
        self.connector.start()
        self.addCleanup(self.connector.stop)

    def test_account_and_unlimited_assignments_use_remote_db_api(self) -> None:
        user_id = auth.create_user("turso@example.com", "secure-test-password", "654321")
        self.assertIsNotNone(auth.verify_email("turso@example.com", "654321"))

        for _ in range(5):
            reservation = auth.reserve_assignment(user_id)
            self.assertIsNotNone(reservation)
            auth.finish_assignment(reservation[0], succeeded=True)

        self.assertEqual(auth.get_user(user_id)["assignments_generated"], 5)

    def test_requires_both_turso_credentials(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TURSO_DATABASE_URL": "libsql://example.turso.io",
                "TURSO_AUTH_TOKEN": "",
            },
        ):
            with self.assertRaisesRegex(RuntimeError, "Set both TURSO_DATABASE_URL"):
                connect()


if __name__ == "__main__":
    unittest.main()
