from django.test import TestCase


class BrandingTests(TestCase):
    def test_queue_uses_vibearr_brand(self):
        response = self.client.get("/projects/")
        self.assertContains(response, "vibearr home")
