from celery import current_app
from django.conf import settings
from django.test import TestCase
from django.urls import reverse

from hello.tasks import add, nightly_greeting


class HelloAppTests(TestCase):
    def test_index_renders(self):
        response = self.client.get(reverse("index"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Hello, world!")


class CeleryTaskTests(TestCase):
    def test_broker_reachable(self):
        conn = current_app.connection()
        transport = conn.connect()
        self.assertIsNotNone(transport)
        conn.release()

    def test_add_task_returns_sum(self):
        result = add.delay(2, 3)
        value = result.get(timeout=20)
        self.assertEqual(value, 5)

    def test_nightly_greeting_task(self):
        result = nightly_greeting.delay()
        data = result.get(timeout=20)
        self.assertIn("greeting", data)
        self.assertIsNotNone(data.get("at"))

    def test_beat_schedule_contains_nightly_greeting(self):
        self.assertIn("nightly-greeting", settings.CELERY_BEAT_SCHEDULE)
        self.assertEqual(
            settings.CELERY_BEAT_SCHEDULE["nightly-greeting"]["task"],
            "hello.tasks.nightly_greeting",
        )
