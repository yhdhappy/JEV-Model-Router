import json
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

from jev_router.panel import PanelRuntime, make_handler


class FakeRuntime:
    def config(self):
        return {
            "models": [
                {"name": "low_model", "model_id": "provider/low", "capability": "low"}
            ],
            "default_budget": 1.5,
            "default_workspace": "/tmp",
            "jev_api_key_file_configured": False,
        }

    def execute(self, payload):
        assert payload["prompt"] == "hello"
        return {
            "ok": True,
            "answer": "done",
            "route": {
                "status": "success",
                "route_source": "manual_override",
                "selected_model": "low_model",
            },
            "selected_model_id": "provider/low",
            "provider_metrics": None,
            "jev_metrics": None,
        }


def request(server, method, path, body=None):
    connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=2)
    headers = {}
    encoded = None
    if body is not None:
        encoded = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    connection.request(method, path, body=encoded, headers=headers)
    response = connection.getresponse()
    payload = response.read()
    connection.close()
    return response.status, payload


def test_panel_serves_html_config_and_run_api():
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(FakeRuntime()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, html = request(server, "GET", "/")
        assert status == 200
        assert "JEV Model Router" in html.decode()

        status, raw = request(server, "GET", "/api/config")
        assert status == 200
        config = json.loads(raw)
        assert config["models"][0]["name"] == "low_model"

        status, raw = request(server, "POST", "/api/run", {"prompt": "hello"})
        assert status == 200
        result = json.loads(raw)
        assert result["ok"] is True
        assert result["answer"] == "done"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_panel_runtime_config_exposes_real_registry_without_credentials():
    config = PanelRuntime().config()
    assert [item["name"] for item in config["models"]] == [
        "low_model",
        "medium_model",
        "high_model",
    ]
    assert config["default_budget"] == 1.5
