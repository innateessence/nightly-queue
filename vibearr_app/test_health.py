from unittest.mock import patch

from django.db import DatabaseError
from django.test import TestCase


class HealthTests(TestCase):
    def test_readiness_checks_the_database(self):
        response = self.client.get("/health/")
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertEqual(
            response["Cache-Control"],
            "max-age=0, no-cache, no-store, must-revalidate, private",
        )

    def test_database_failure_returns_unavailable_without_details(self):
        with patch(
            "vibearr_app.health.connection.cursor",
            side_effect=DatabaseError("private database detail"),
        ):
            response = self.client.get("/health/")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unavailable"})

    def test_readiness_does_not_accept_mutation(self):
        self.assertEqual(self.client.post("/health/").status_code, 405)
