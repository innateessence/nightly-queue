import json

from django.core.exceptions import ValidationError
from django.test import RequestFactory, SimpleTestCase

from .api import read_body


class JSONBodyTests(SimpleTestCase):
    def read(self, payload):
        request = RequestFactory().post(
            "/", json.dumps(payload), content_type="application/json"
        )
        return read_body(request, ["data"])

    def test_nested_object_keys_reject_null_characters(self):
        with self.assertRaisesMessage(ValidationError, "null characters"):
            self.read({"data": [{"private\x00key": "text"}]})

    def test_nested_object_keys_reject_unpaired_surrogates(self):
        with self.assertRaisesMessage(ValidationError, "valid Unicode"):
            self.read({"data": [{"private\udfffkey": "text"}]})

    def test_json_numbers_must_be_finite(self):
        for number in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(number=number):
                with self.assertRaisesMessage(ValidationError, "finite JSON"):
                    self.read({"data": [number]})

    def test_overflowing_json_float_is_rejected(self):
        request = RequestFactory().post(
            "/", '{"data": 1e999}', content_type="application/json"
        )
        with self.assertRaisesMessage(ValidationError, "finite JSON"):
            read_body(request, ["data"])

    def test_deeply_nested_json_is_rejected(self):
        value = "text"
        for _ in range(21):
            value = [value]
        with self.assertRaisesMessage(ValidationError, "at most 20 levels"):
            self.read({"data": value})

    def test_json_decoder_recursion_error_is_rejected(self):
        payload = '{"data":' + "[" * 10000 + "0" + "]" * 10000 + "}"
        request = RequestFactory().post("/", payload, content_type="application/json")
        with self.assertRaisesMessage(ValidationError, "Send valid JSON"):
            read_body(request, ["data"])
