import os
import urllib.error
import urllib.request

import pytest
from playwright.sync_api import sync_playwright


def pytest_addoption(parser):
    parser.addoption(
        "--frontend-url",
        action="store",
        default=os.getenv("ASK_YOUR_DATA_URL", "http://localhost:5173"),
        help="Base URL for the Vite frontend.",
    )
    parser.addoption(
        "--backend-url",
        action="store",
        default=os.getenv("ASK_YOUR_DATA_BACKEND", "http://localhost:8000"),
        help="Base URL for the FastAPI backend.",
    )

@pytest.fixture(scope="session")
def frontend_url(request):
    return request.config.getoption("--frontend-url")


@pytest.fixture(scope="session")
def backend_url(request):
    return request.config.getoption("--backend-url")


@pytest.fixture(scope="session")
def backend_health(backend_url):
    request = urllib.request.Request(
        f"{backend_url.rstrip('/')}/api/health",
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            if response.status != 200:
                pytest.fail(
                    f"Backend health check returned HTTP {response.status}; "
                    "start the backend before running e2e tests."
                )
    except (urllib.error.URLError, TimeoutError) as exc:
        pytest.fail(
            "Backend health check failed; start the backend before running "
            f"e2e tests. Details: {exc}"
        )


@pytest.fixture(scope="session")
def browser_factory():
    with sync_playwright() as pw:
        yield pw


@pytest.fixture
def page(frontend_url, browser_factory, backend_health):
    browser = browser_factory.chromium.launch(headless=True)
    context = browser.new_context(viewport={"width": 1440, "height": 1200})
    page_obj = context.new_page()
    try:
        page_obj.goto(frontend_url, wait_until="domcontentloaded", timeout=30_000)
        yield page_obj
    finally:
        context.close()
        browser.close()
