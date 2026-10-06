# CMN-C2-276 - Unit tests: KintoneClient service (kintone REST API record shape)
# Adapted from a sibling tool-calling golden (kaonavi_client -> kintone_client).
# Pure service layer (stdlib-only, no framework imports) - plain function tests.

import pytest

from src.services.kintone_client import KintoneApiError, KintoneClient


def test_get_record_success_with_injected_get():
    captured = {}

    def get(url, headers, body):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = body
        return 200, {
            "record": {
                "$id": {"type": "__ID__", "value": "101"},
                "title": {"type": "SINGLE_LINE_TEXT", "value": "Record 101"},
            }
        }

    client = KintoneClient("https://kintone.example.test/k/v1/", get=get)
    resp = client.get_record("17", "101", "tok123")
    assert resp["record"]["$id"]["value"] == "101"
    assert captured["url"] == "https://kintone.example.test/k/v1/record.json"
    # kintone REST API auth: the per-call token travels in X-Cybozu-API-Token.
    assert captured["headers"]["X-Cybozu-API-Token"] == "tok123"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["body"]["app"] == "17"
    assert captured["body"]["id"] == "101"


def test_add_record_success_with_injected_post():
    captured = {}

    def post(url, headers, body):
        captured["url"] = url
        captured["body"] = body
        return 200, {"id": "202", "revision": "1"}

    client = KintoneClient("https://kintone.example.test/k/v1", post=post)
    payload = {"app": "17", "record": {"title": {"value": "Onboarding"}}}
    resp = client.add_record(payload, "tok")
    assert resp["id"] == "202"
    assert captured["url"] == "https://kintone.example.test/k/v1/record.json"
    assert captured["body"] == payload


def test_update_record_success_with_injected_put():
    captured = {}

    def put(url, headers, body):
        captured["url"] = url
        captured["body"] = body
        return 200, {"revision": "2"}

    client = KintoneClient("https://kintone.example.test/k/v1", put=put)
    payload = {"app": "17", "id": "101", "record": {"Status": {"value": "closed"}}}
    resp = client.update_record(payload, "tok")
    assert resp["revision"] == "2"
    assert captured["body"] == payload


def test_non_2xx_raises_kintone_api_error():
    def post(url, headers, body):
        return 400, {"message": "record is malformed"}

    client = KintoneClient("https://kintone.example.test/k/v1", post=post)
    with pytest.raises(KintoneApiError) as exc:
        client.add_record({"app": "17", "record": {}}, "tok")
    assert exc.value.status_code == 400
    assert "record is malformed" in str(exc.value)


def test_non_2xx_error_message_falls_back_to_errors_field():
    def get(url, headers, body):
        return 520, {"errors": {"app": ["required field"]}}

    client = KintoneClient("https://kintone.example.test/k/v1", get=get)
    with pytest.raises(KintoneApiError) as exc:
        client.get_record("17", "101", "tok")
    assert "required field" in str(exc.value)


def test_default_stub_transport_lookup_shape():
    # No transport injected -> deterministic, network-free v1 stub.
    client = KintoneClient()
    assert client.uses_stub_transport is True
    resp = client.get_record("17", "101", "tok")
    assert resp.get("_stub") is True
    record = resp["record"]
    assert record["$id"]["value"] == "101"
    assert record["title"]["value"] == "Record 101"


def test_default_stub_transport_update_echoes_request_id():
    client = KintoneClient()
    resp = client.update_record({"app": "17", "id": "101", "record": {}}, "tok")
    assert resp.get("_stub") is True
    assert resp["id"] == "101"
    assert resp["revision"] == "1"


def test_default_stub_transport_create_synthesizes_id():
    client = KintoneClient()
    resp = client.add_record({"app": "17", "record": {"title": {"value": "Onboarding"}}}, "tok")
    assert resp.get("_stub") is True
    # No request id on create -> a deterministic synthetic receipt id.
    assert resp["id"].startswith("r-")


def test_injected_transport_disables_stub_flag():
    client = KintoneClient(get=lambda url, headers, body: (200, {"record": {}}))
    assert client.uses_stub_transport is False
