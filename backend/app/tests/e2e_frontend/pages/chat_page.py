from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, expect


class ChatPage:
    def __init__(self, page: Page, base_url: str):
        self.page = page
        self.base_url = base_url.rstrip("/")

    def open(self) -> None:
        self.page.goto(f"{self.base_url}", wait_until="domcontentloaded", timeout=30_000)

    @property
    def question_input(self):
        return self.page.locator('textarea[aria-label="Your question"]')

    @property
    def send_button(self):
        return self.page.get_by_role("button", name="Send")

    def ask(self, question: str) -> None:
        self.question_input.fill(question)
        self.send_button.click()

    def fill_question(self, question: str) -> None:
        self.question_input.fill(question)

    def send_twice(self) -> None:
        self.send_button.dblclick()

    def start_new_chat(self) -> None:
        self.page.get_by_role(
            "button",
            name=re.compile(r"^New chat\b", flags=re.IGNORECASE),
        ).click()

    def wait_for_visible_text(self, expected_text: str, timeout: float = 60.0) -> str:
        self.page.get_by_text(expected_text, exact=False).first.wait_for(
            state="visible",
            timeout=timeout * 1000,
        )
        return self.visible_text()

    def wait_for_table(self, timeout: float = 90.0) -> None:
        self.page.locator("table").first.wait_for(
            state="visible",
            timeout=timeout * 1000,
        )

    def visible_text(self) -> str:
        return self.page.locator("body").inner_text()

    def has_table(self) -> bool:
        return self.page.locator("table").count() > 0

    def table_rows(self) -> list[dict[str, Any]]:
        table = self.page.locator("table").last
        if table.count() == 0:
            return []

        headers = [
            cell.inner_text().strip()
            for cell in table.locator("thead th").all()
        ]
        rows: list[dict[str, Any]] = []
        for tr in table.locator("tbody tr").all():
            cells = [cell.inner_text().strip() for cell in tr.locator("td").all()]
            if not cells:
                continue
            if headers and len(headers) == len(cells):
                rows.append(dict(zip(headers, cells)))
            else:
                rows.append({str(index): value for index, value in enumerate(cells)})
        return rows

    def expand_all_rows(self) -> tuple[int, int] | None:
        summary = re.search(
            r"Showing\s+(\d+)\s+of\s+(\d+)",
            self.visible_text(),
            flags=re.IGNORECASE,
        )
        button = self.page.get_by_role(
            "button",
            name=re.compile(r"Show all \d+ rows", flags=re.IGNORECASE),
        )
        if button.count() == 0:
            return None
        if summary is None:
            raise AssertionError("A truncated table has no 'Showing N of M' summary.")

        visible_count, total_count = (int(value) for value in summary.groups())
        if len(self.table_rows()) != visible_count:
            raise AssertionError(
                f"Table shows {visible_count} visible rows but renders "
                f"{len(self.table_rows())} rows."
            )

        button.click()
        self.page.get_by_text(
            re.compile(rf"Show all {total_count} rows", flags=re.IGNORECASE)
        ).wait_for(state="hidden", timeout=10_000)
        if len(self.table_rows()) != total_count:
            raise AssertionError(
                f"Expanded table should show {total_count} rows; "
                f"it shows {len(self.table_rows())}."
            )
        return visible_count, total_count

    def click_search_option(self, term: str) -> None:
        count = self.search_option_count(term)
        if count != 1:
            raise AssertionError(
                f"Expected exactly one search option for {term!r}; found {count}."
            )
        label = re.compile(
            rf"Search shop names for\s+['\u2018\u2019]?{re.escape(term)}['\u2018\u2019]?",
            flags=re.IGNORECASE,
        )
        self.page.get_by_role("button", name=label).click()

    def search_option_count(self, term: str) -> int:
        label = re.compile(
            rf"Search shop names for\s+['\u2018\u2019]?{re.escape(term)}['\u2018\u2019]?",
            flags=re.IGNORECASE,
        )
        return self.page.get_by_role("button", name=label).count()

    def click_option(self, label: str) -> None:
        self.page.get_by_role("button", name=label, exact=True).click()

    def has_option(self, label: str) -> bool:
        return self.page.get_by_role("button", name=label, exact=True).count() > 0

    def option_is_disabled(self, label: str) -> bool:
        option = self.page.get_by_role("button", name=label, exact=True)
        count = option.count()
        if count != 1:
            raise AssertionError(
                f"Expected exactly one clarification option {label!r}; found {count}."
            )
        return option.is_disabled()

    def send_is_enabled(self) -> bool:
        return self.send_button.is_enabled()

    def has_loading_state(self) -> bool:
        pending_text = self.page.get_by_text("Working on your answer…", exact=True)
        working_button = self.page.get_by_role("button", name="Working…", exact=True)
        stop_button = self.page.get_by_role("button", name="Stop", exact=True)
        return (
            pending_text.is_visible()
            or working_button.count() > 0
            or stop_button.count() > 0
        )

    def last_answer_text(self) -> str:
        answers = self.page.locator(
            "div.mx-auto.flex.w-full.max-w-4xl.flex-col.items-start.gap-2"
        )
        if answers.count() == 0:
            raise AssertionError("No visible assistant answer is available for evidence capture.")
        return answers.last.inner_text()

    def wait_for_answer_change(self, previous_text: str, timeout: float = 60.0) -> None:
        answers = self.page.locator(
            "div.mx-auto.flex.w-full.max-w-4xl.flex-col.items-start.gap-2"
        )
        if answers.count() == 0:
            raise AssertionError("No visible assistant answer is available after the option click.")
        expect(answers.last).not_to_have_text(previous_text, timeout=timeout * 1000)

    def save_evidence(self, case_id: str, option_label: str | None = None) -> tuple[Path, Path]:
        evidence_dir = Path(__file__).resolve().parents[1] / "reports" / "evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
        stem = f"{case_id.lower()}_{timestamp}"
        screenshot_path = evidence_dir / f"{stem}.png"
        text_path = evidence_dir / f"{stem}.txt"

        self.page.screenshot(path=str(screenshot_path), full_page=True)
        evidence_text = f"Last visible assistant answer:\n{self.last_answer_text()}\n"
        if option_label is not None:
            options = self.page.get_by_role("button", name=option_label, exact=True)
            count = options.count()
            if count == 0:
                option_state = "not present"
            elif count == 1:
                option_state = "disabled" if options.is_disabled() else "clickable"
            else:
                option_state = f"multiple buttons present ({count})"
            evidence_text += (
                f"\nClarification option {option_label!r}: {option_state}\n"
            )
        text_path.write_text(evidence_text, encoding="utf-8")
        return screenshot_path, text_path

    def has_reference_id(self) -> bool:
        text = self.visible_text().lower()
        return "reference id" in text or ("reference" in text and "id" in text)

    def download_csv(self):
        with self.page.expect_download(timeout=30_000) as download_info:
            self.page.get_by_role("button", name="Download CSV").click()
        return download_info.value

    def expect_message_contains(self, needle: str, timeout: float = 60.0) -> str:
        return self.wait_for_visible_text(needle, timeout=timeout)
