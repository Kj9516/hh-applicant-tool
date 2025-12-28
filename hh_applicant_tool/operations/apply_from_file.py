from __future__ import annotations

import argparse
import csv
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from ..api import ApiClient, BadResponse
from ..api.errors import LimitExceeded
from ..applications import render_application_message, send_application
from ..main import BaseOperation
from ..main import Namespace as BaseNamespace
from ..mixins import GetResumeIdMixin

logger = logging.getLogger(__package__)


class Namespace(BaseNamespace):
    file: Path
    resume_id: str | None
    message: str | None
    dry_run: bool
    limit: int | None
    min_interval: float
    state_file: Path | None
    output_report: Path | None


@dataclass
class PreparedApplication:
    row: str
    vacancy_id: str
    resume_id: str
    message: str
    vacancy: dict


class Operation(BaseOperation, GetResumeIdMixin):
    """Apply to vacancies listed in a CSV/TSV file."""

    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--file",
            required=True,
            type=Path,
            help="Path to CSV/TSV file containing vacancy rows",
        )
        parser.add_argument("--resume-id", help="Default resume id if not present in the file")
        parser.add_argument(
            "--message",
            help=(
                "Default message if not present in the file; falls back to config reply_message"
            ),
        )
        parser.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=False)
        parser.add_argument("--limit", type=int, help="Process only the first N valid rows")
        parser.add_argument(
            "--min-interval",
            type=float,
            default=1.5,
            help="Minimum interval between applications",
        )
        parser.add_argument(
            "--state-file",
            type=Path,
            help="File to store processed vacancy ids; already listed ids will be skipped",
        )
        parser.add_argument(
            "--output-report",
            type=Path,
            help="Optional CSV file to write apply-from-file results",
        )

    def run(self, args: Namespace, api_client: ApiClient, _) -> None:
        self.api_client = api_client
        self.args = args
        self.default_resume_id = args.resume_id or self._get_resume_id()
        self.default_message = args.message or args.config.get("reply_message")
        self.state_file_path = args.state_file
        self.output_report_path = args.output_report
        self.processed_ids = self._load_processed_ids()
        self.report_entries: list[dict[str, str]] = []
        self.me = self.api_client.get("/me")

        if not args.file.exists():
            raise FileNotFoundError(f"File not found: {args.file}")

        rows = list(self._parse_file(args.file))
        rows = self._apply_limit(rows, args.limit)
        prepared_rows = self._prepare_applications(rows)

        self._print_plan(prepared_rows)
        if args.dry_run:
            for application in prepared_rows:
                self._record_report(
                    application.vacancy_id,
                    "skipped",
                    "dry-run",
                    application.row,
                )
            self._write_report()
            return

        self._apply(prepared_rows)
        self._write_report()

    def _parse_file(self, file_path: Path) -> Iterable[dict[str, str | None]]:
        delimiter = "\t" if file_path.suffix.lower() == ".tsv" else ","
        logger.debug(f"Using delimiter %r for %s", delimiter, file_path)
        with file_path.open("r", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f, delimiter=delimiter)
            seen: set[str] = set()
            for index, row in enumerate(reader, start=2):
                vacancy_id = self._get_vacancy_id(row)
                if not vacancy_id:
                    logger.warning("Row %d skipped: missing vacancy id", index)
                    self._record_report("", "skipped", "missing vacancy id", str(index))
                    continue
                if not re.fullmatch(r"\d+", vacancy_id):
                    logger.warning("Row %d skipped: invalid vacancy id %s", index, vacancy_id)
                    self._record_report(
                        vacancy_id, "skipped", "invalid vacancy id", str(index)
                    )
                    continue
                if not self._is_enabled(row):
                    logger.info("Row %d skipped: disabled", index)
                    self._record_report(vacancy_id, "skipped", "disabled", str(index))
                    continue
                if vacancy_id in seen:
                    logger.info("Row %d skipped: duplicate vacancy id %s", index, vacancy_id)
                    self._record_report(
                        vacancy_id, "skipped", "duplicate in file", str(index)
                    )
                    continue
                if vacancy_id in self.processed_ids:
                    logger.info(
                        "Row %d skipped: vacancy id %s already processed", index, vacancy_id
                    )
                    self._record_report(
                        vacancy_id, "skipped", "already processed", str(index)
                    )
                    continue
                seen.add(vacancy_id)
                yield {
                    "row": str(index),
                    "vacancy_id": vacancy_id,
                    "resume_id": (row.get("resume_id") or "").strip() or None,
                    "message": (row.get("message") or "").strip() or None,
                }

    @staticmethod
    def _is_enabled(row: dict[str, str | None]) -> bool:
        raw = (row.get("enabled") or "").strip().lower()
        return raw not in {"0", "false", "no"}

    @staticmethod
    def _get_vacancy_id(row: dict[str, str | None]) -> str | None:
        vacancy_id = (row.get("vacancy_id") or "").strip()
        if vacancy_id:
            return vacancy_id
        vacancy_url = (row.get("vacancy_url") or "").strip()
        if vacancy_url:
            patterns = [r"/vacancy/(\d+)", r"vacancyId=(\d+)"]
            for pattern in patterns:
                if match := re.search(pattern, vacancy_url):
                    return match.group(1)
        return None

    def _apply_limit(self, rows: list[dict[str, str | None]], limit: int | None):
        if limit is None:
            return rows

        if limit < 0:
            for row in rows:
                self._record_report(row["vacancy_id"], "skipped", "limit", row["row"])
            return []

        limited = rows[:limit]
        for row in rows[limit:]:
            self._record_report(row["vacancy_id"], "skipped", "limit", row["row"])
        return limited

    def _load_processed_ids(self) -> set[str]:
        if not self.state_file_path or not self.state_file_path.exists():
            return set()
        try:
            with self.state_file_path.open("r", encoding="utf-8", errors="replace") as f:
                ids = {line.strip() for line in f if line.strip()}
                logger.info("Loaded %d processed vacancy ids from state file", len(ids))
                return ids
        except OSError as exc:
            logger.warning("Failed to read state file: %s", exc)
            return set()

    def _prepare_applications(
        self, rows: list[dict[str, str | None]]
    ) -> list[PreparedApplication]:
        prepared: list[PreparedApplication] = []

        for row in rows:
            vacancy_id = row["vacancy_id"]
            resume_id = row["resume_id"] or self.default_resume_id

            try:
                vacancy = self.api_client.get(f"/vacancies/{vacancy_id}")
            except BadResponse as exc:
                logger.error(
                    "Row %s: failed to fetch vacancy %s: %s", row["row"], vacancy_id, exc
                )
                self._record_report(
                    vacancy_id, "error", f"vacancy fetch failed: {exc}", row["row"]
                )
                continue

            if vacancy.get("has_test"):
                reason = "has test"
                logger.debug("Row %s skipped: %s", row["row"], reason)
                self._record_report(vacancy_id, "skipped", reason, row["row"])
                continue

            if vacancy.get("archived"):
                reason = "archived vacancy"
                logger.info("Row %s skipped: %s", row["row"], reason)
                self._record_report(vacancy_id, "skipped", reason, row["row"])
                continue

            if relations := vacancy.get("relations"):
                reason = "already has relations"
                logger.info(
                    "Row %s skipped: vacancy %s already has relations", row["row"], vacancy_id
                )
                self._record_report(vacancy_id, "skipped", reason, row["row"])
                continue

            message_template = row["message"] or self.default_message or ""
            placeholders = self._build_placeholders(vacancy)

            try:
                message = (
                    render_application_message(message_template, placeholders)
                    if message_template
                    else ""
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Row %s: failed to render message for vacancy %s: %s",
                    row["row"],
                    vacancy_id,
                    exc,
                )
                self._record_report(
                    vacancy_id,
                    "error",
                    f"message rendering failed: {exc}",
                    row["row"],
                )
                continue

            prepared.append(
                PreparedApplication(
                    row=row["row"],
                    vacancy_id=vacancy_id,
                    resume_id=resume_id,
                    message=message,
                    vacancy=vacancy,
                )
            )

        return prepared

    def _build_placeholders(self, vacancy: dict) -> dict[str, str]:
        me = self.me or {}
        return {
            "vacancy_name": vacancy.get("name", ""),
            "employer_name": vacancy.get("employer", {}).get("name", ""),
            "first_name": me.get("first_name", ""),
            "last_name": me.get("last_name", ""),
            "email": me.get("email", ""),
            "phone": me.get("phone", ""),
        }

    def _record_report(
        self, vacancy_id: str, status: str, reason: str, row_number: str
    ) -> None:
        timestamp = datetime.now(tz=timezone.utc).isoformat()
        self.report_entries.append(
            {
                "vacancy_id": vacancy_id,
                "status": status,
                "reason": reason,
                "timestamp": timestamp,
                "row_number": row_number,
            }
        )

    def _write_report(self) -> None:
        if not self.output_report_path:
            return

        try:
            self.output_report_path.parent.mkdir(parents=True, exist_ok=True)
            with self.output_report_path.open("w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=["vacancy_id", "status", "reason", "timestamp", "row_number"],
                )
                writer.writeheader()
                writer.writerows(self.report_entries)
        except OSError as exc:
            logger.warning("Failed to write output report %s: %s", self.output_report_path, exc)

    def _print_plan(self, rows: list[PreparedApplication]) -> None:
        if not rows:
            print("No rows to process.")
            return

        if self.args.dry_run:
            print(
                "Dry-run plan (API calls are not executed yet). min_interval =",
                self.args.min_interval,
            )
        else:
            print(
                "Execution plan (applications will be sent). min_interval =",
                self.args.min_interval,
            )
        for app in rows:
            print(
                f"Row {app.row}: would apply to vacancy {app.vacancy_id} with resume {app.resume_id}"
            )
            if app.message:
                print(f"  message: {app.message}")
            else:
                print("  message: <none>")

    def _apply(self, rows: list[PreparedApplication]) -> None:
        if not rows:
            print("No rows to process.")
            return

        for index, app in enumerate(rows):
            params = {
                "resume_id": app.resume_id,
                "vacancy_id": app.vacancy_id,
                "message": app.message,
            }

            if index > 0 and self.args.min_interval > 0:
                time.sleep(self.args.min_interval)

            try:
                send_application(self.api_client, params)
            except LimitExceeded:
                logger.warning(
                    "Row %s: HH API limit reached while applying to vacancy %s",
                    app.row,
                    app.vacancy_id,
                )
                self._record_report(app.vacancy_id, "error", "limit exceeded", app.row)
                break
            except BadResponse as exc:
                logger.error(
                    "Row %s: API error applying to vacancy %s: %s",
                    app.row,
                    app.vacancy_id,
                    exc,
                )
                self._record_report(
                    app.vacancy_id, "error", f"api error: {exc}", app.row
                )
                continue
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Row %s: unexpected error applying to vacancy %s: %s",
                    app.row,
                    app.vacancy_id,
                    exc,
                )
                self._record_report(
                    app.vacancy_id, "error", f"unexpected error: {exc}", app.row
                )
                continue

            self._save_processed_id(app.vacancy_id)
            self._record_report(app.vacancy_id, "applied", "", app.row)
            logger.info("Row %s: applied to vacancy %s", app.row, app.vacancy_id)

        print("📝 Applications processed.")

    def _save_processed_id(self, vacancy_id: str) -> None:
        if not self.state_file_path:
            return
        try:
            self.state_file_path.parent.mkdir(parents=True, exist_ok=True)
            with self.state_file_path.open("a", encoding="utf-8") as f:
                f.write(f"{vacancy_id}\n")
            self.processed_ids.add(vacancy_id)
        except OSError as exc:
            logger.warning("Failed to write to state file %s: %s", self.state_file_path, exc)

