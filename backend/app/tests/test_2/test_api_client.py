import asyncio
import json
import urllib.error
from argparse import Namespace
from pathlib import Path

import pytest

from app.services.errors import InfrastructureError
from app.tests.test_2 import api_client
from app.tests.test_2 import runner
from app.tests.test_2.runner import load_cases


def test_http_429_is_classified_as_infrastructure_failure(monkeypatch):
    def rate_limited(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            429,
            "Too Many Requests",
            {},
            None,
        )

    monkeypatch.setattr(api_client.urllib.request, "urlopen", rate_limited)
    monkeypatch.setattr(api_client, "CHAT_STREAM_MAX_RETRIES", 1)

    with pytest.raises(InfrastructureError) as error:
        api_client.post_chat_stream("http://localhost", "question")

    assert error.value.code == "CHAT_HTTP_429"
    assert error.value.stage == "chat_stream"


def test_http_429_retries_with_exponential_backoff(monkeypatch):
    calls = []
    sleeps = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"type":"result","request_id":"request-1","status":"ok"}\n'

    def first_two_rate_limited(request, timeout):
        calls.append(request)
        if len(calls) < 3:
            raise urllib.error.HTTPError(
                request.full_url,
                429,
                "Too Many Requests",
                {},
                None,
            )
        return Response()

    monkeypatch.setattr(api_client.urllib.request, "urlopen", first_two_rate_limited)
    monkeypatch.setattr(api_client.time, "sleep", sleeps.append)

    result = api_client.post_chat_stream("http://localhost", "question")

    assert result["request_id"] == "request-1"
    assert len(calls) == 3
    assert sleeps == [1.0, 2.0]


def test_chat_stream_timeout_is_classified_as_infrastructure_failure(monkeypatch):
    def timed_out(request, timeout):
        raise urllib.error.URLError(TimeoutError("request expired"))

    monkeypatch.setattr(api_client.urllib.request, "urlopen", timed_out)

    with pytest.raises(InfrastructureError) as error:
        api_client.post_chat_stream("http://localhost", "question")

    assert error.value.code == "CHAT_TIMEOUT"


def test_runner_reports_infrastructure_error_without_scoring_accuracy(
    tmp_path, monkeypatch
):
    case = load_cases()[0]

    async def infrastructure_failure(*args, **kwargs):
        raise InfrastructureError(
            "LLM_RATE_LIMIT_429",
            "Provider rate limit persisted.",
            stage="generate_query",
        )

    monkeypatch.setattr(runner, "load_cases", lambda _path: [case])
    monkeypatch.setattr(runner, "_run_case", infrastructure_failure)
    args = Namespace(
        mode=case["split"],
        case_file=str(Path("catalog.json")),
        case_ids=None,
        backend_url="http://localhost",
        timeout=1.0,
        delay=0.0,
        trace_dir=str(tmp_path / "traces"),
        output_dir=str(tmp_path / "reports"),
        max_consecutive_errors=3,
    )

    assert asyncio.run(runner.run(args)) == 1
    report_path = next((tmp_path / "reports").glob("*.json"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["grading_buckets"] == {"Infrastructure Error": 1}
    assert report["infrastructure_error_count"] == 1
    assert report["accuracy_scored_case_count"] == 0
