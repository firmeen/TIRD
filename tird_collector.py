
"""
tird_collector.py
TIRD NEWSLETTER COLLECTOR
Version 5.0.0

HTTP ENGINE:
    curl.exe via subprocess

DOWNLOAD ENGINE:
    ThreadPoolExecutor + Shared Rate Limiter

REQUIREMENTS:
    pip install python-dotenv rich

.env:
    TIRD_AUTHORIZATION="YOUR_AUTHORIZATION"
    TIRD_COOKIE="YOUR_COMPLETE_COOKIE"
    TIRD_REFERER="https://tirdmember.tird.insure/Content?_mId=YOUR_ID"

COMMANDS:
    python tird_collector.py --test-only
    python tird_collector.py --test-only --verbose
    python tird_collector.py --list
    python tird_collector.py --limit 1
    python tird_collector.py --limit 3
    python tird_collector.py --parallel 3
    python tird_collector.py --year 2026
    python tird_collector.py --service-id SERVICE_ID
    python tird_collector.py
    python tird_collector.py --force
    python tird_collector.py --debug-response

FEATURES:
    - Authenticated TIRD API
    - curl.exe HTTP transport
    - Parallel PDF downloads
    - Shared request rate limiting
    - Automatic retry for transient HTTP errors
    - Thread-safe result handling
    - Atomic PDF writes
    - Resume and skip completed downloads
    - PDF signature and SHA256 validation
    - CSV manifest
    - Rich terminal interface
    - Safe HTTP diagnostics
    - Graceful interruption

SECURITY:
    - Uses an existing authorized TIRD session.
    - Credentials are read from .env.
    - Credentials are passed to curl through stdin.
    - Credentials are not included in command-line arguments.
    - No TLS verification bypass.
    - No authentication bypass.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from concurrent.futures import (
    ThreadPoolExecutor,
    as_completed,
    Future,
)
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import unquote, urlparse

from dotenv import load_dotenv

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.progress import (
        BarColumn,
        Progress,
        SpinnerColumn,
        TaskProgressColumn,
        TextColumn,
        TimeElapsedColumn,
    )
    from rich.table import Table

    RICH_AVAILABLE = True

except ImportError:
    RICH_AVAILABLE = False


# ============================================================
# APPLICATION CONSTANTS
# ============================================================

APP_NAME = "TIRD NEWSLETTER COLLECTOR"
APP_VERSION = "5.1.0"

BASE_URL = "https://tirdmember.tird.insure"

API = {
    "catalog": "/Content/GetService",
    "detail": "/Content/GetServiceById",
    "content": "/Content/ShowContentFiles",
    "download": "/Content/ListFiles",
}

PROJECT_DIR = Path(__file__).resolve().parent

DEFAULT_OUTPUT = PROJECT_DIR / "TIRD_Newsletter"

DEFAULT_PARALLEL = 3
MAX_PARALLEL = 6

DEFAULT_DELAY = 0.20
DEFAULT_TIMEOUT = 45
DEFAULT_RETRIES = 2

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/157.0.0.0 Safari/537.36"
)

CHROME_CLIENT_HINTS = (
    '"Not-A.Brand";v="99", '
    '"Google Chrome";v="157", '
    '"Chromium";v="157"'
)

MANIFEST_COLUMNS = [
    "service_id",
    "title",
    "edition",
    "published_date",
    "year",
    "filename",
    "file_path",
    "file_size",
    "sha256",
    "status",
    "downloaded_at",
    "error",
]

THAI_MONTHS = {
    "มกราคม": 1,
    "กุมภาพันธ์": 2,
    "มีนาคม": 3,
    "เมษายน": 4,
    "พฤษภาคม": 5,
    "มิถุนายน": 6,
    "กรกฎาคม": 7,
    "สิงหาคม": 8,
    "กันยายน": 9,
    "ตุลาคม": 10,
    "พฤศจิกายน": 11,
    "ธันวาคม": 12,
}

LOG = logging.getLogger("TIRD")

console = Console() if RICH_AVAILABLE else None


# ============================================================
# EXCEPTIONS
# ============================================================

class TIRDError(Exception):
    pass


class ConfigurationError(TIRDError):
    pass


class CurlError(TIRDError):
    pass


class AuthenticationError(TIRDError):
    pass


class APIResponseError(TIRDError):
    pass


class PDFDownloadError(TIRDError):
    pass


class CollectionStopped(TIRDError):
    pass


# ============================================================
# CONFIGURATION
# ============================================================

@dataclass(frozen=True)
class Config:
    authorization: str
    cookie: str
    referer: str

    curl_executable: str
    output: Path

    parallel: int = DEFAULT_PARALLEL
    delay: float = DEFAULT_DELAY
    timeout: int = DEFAULT_TIMEOUT
    retries: int = DEFAULT_RETRIES

    verbose: bool = False
    debug_response: bool = False
    skip_content_page: bool = False
    verify_existing: bool = False

    @classmethod
    def load(cls, args: argparse.Namespace) -> "Config":

        env_path = PROJECT_DIR / ".env"

        if not env_path.is_file():
            raise ConfigurationError(
                f"Missing .env file: {env_path}"
            )

        load_dotenv(
            dotenv_path=env_path,
            override=True,
            encoding="utf-8-sig",
            interpolate=False,
        )

        def env(name: str) -> str:

            value = os.getenv(name, "").strip()

            if (
                len(value) >= 2
                and value[0] == value[-1]
                and value[0] in ('"', "'")
            ):
                value = value[1:-1]

            return value.strip()

        authorization = env("TIRD_AUTHORIZATION")
        cookie = env("TIRD_COOKIE")
        referer = env("TIRD_REFERER")

        if not authorization:
            raise ConfigurationError(
                "TIRD_AUTHORIZATION is missing from .env"
            )

        if not cookie:
            raise ConfigurationError(
                "TIRD_COOKIE is missing from .env"
            )

        if not referer:
            raise ConfigurationError(
                "TIRD_REFERER is missing from .env"
            )

        parsed_referer = urlparse(referer)

        if (
            parsed_referer.scheme != "https"
            or parsed_referer.netloc.lower()
            != "tirdmember.tird.insure"
            or parsed_referer.path != "/Content"
        ):
            raise ConfigurationError(
                "TIRD_REFERER must be the authorized "
                "TIRD Content page URL."
            )

        for name, value in (
            ("TIRD_AUTHORIZATION", authorization),
            ("TIRD_COOKIE", cookie),
            ("TIRD_REFERER", referer),
        ):
            if any(
                char in value
                for char in ("\r", "\n", "\x00")
            ):
                raise ConfigurationError(
                    f"Invalid control character in {name}"
                )

        curl_executable = shutil.which("curl.exe")

        if not curl_executable:
            raise ConfigurationError(
                "curl.exe was not found in PATH."
            )

        if not 1 <= args.parallel <= MAX_PARALLEL:
            raise ConfigurationError(
                f"--parallel must be between 1 and {MAX_PARALLEL}"
            )

        if args.delay < 0:
            raise ConfigurationError(
                "--delay cannot be negative."
            )

        if args.timeout <= 0:
            raise ConfigurationError(
                "--timeout must be greater than zero."
            )

        if not 0 <= args.retries <= 5:
            raise ConfigurationError(
                "--retries must be between 0 and 5."
            )

        output = args.output.resolve()

        output.mkdir(
            parents=True,
            exist_ok=True,
        )

        return cls(
            authorization=authorization,
            cookie=cookie,
            referer=referer,
            curl_executable=curl_executable,
            output=output,
            parallel=args.parallel,
            delay=args.delay,
            timeout=args.timeout,
            retries=args.retries,
            verbose=args.verbose,
            debug_response=args.debug_response,
            skip_content_page=args.skip_content_page,
            verify_existing=args.verify_existing,
        )


# ============================================================
# LOGGING AND DISPLAY
# ============================================================

def setup_logging(verbose: bool) -> None:

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=(
            "%(asctime)s | %(levelname)-7s | %(message)s"
        ),
        datefmt="%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )


def show_header() -> None:

    if RICH_AVAILABLE:

        console.print()

        console.print(
            Panel(
                "[bold cyan]TIRD[/bold cyan]  "
                "[bold white]NEWSLETTER COLLECTOR[/bold white]\n"
                "[dim]CURL ENGINE  /  PARALLEL DOWNLOAD  /  PDF ARCHIVE[/dim]",
                title=f"v{APP_VERSION}",
                border_style="cyan",
                padding=(1, 3),
            )
        )

    else:

        print()
        print("=" * 68)
        print(APP_NAME)
        print(f"VERSION {APP_VERSION}")
        print("HTTP ENGINE: CURL")
        print("DOWNLOAD ENGINE: PARALLEL")
        print("=" * 68)


def show_summary(
    stats: dict[str, int],
    output: Path,
    elapsed: float,
) -> None:

    if RICH_AVAILABLE:

        table = Table(
            title="COLLECTION SUMMARY",
            border_style="cyan",
            show_header=False,
        )

        table.add_column(
            "Metric",
            style="white",
        )

        table.add_column(
            "Value",
            style="bold cyan",
            justify="right",
        )

        for key, value in stats.items():
            table.add_row(
                key.upper(),
                str(value),
            )

        table.add_row(
            "ELAPSED",
            f"{elapsed:.2f} sec",
        )

        console.print()
        console.print(table)
        console.print()
        console.print(
            f"[dim]Output: {output}[/dim]"
        )

    else:

        print()
        print("=" * 68)

        for key, value in stats.items():
            print(f"{key:14}: {value}")

        print(f"Elapsed       : {elapsed:.2f} sec")
        print(f"Output        : {output}")

        print("=" * 68)


# ============================================================
# COMMON UTILITIES
# ============================================================

def now_iso() -> str:

    return datetime.now().astimezone().isoformat(
        timespec="seconds"
    )


def sha256_file(path: Path) -> str:

    digest = hashlib.sha256()

    with path.open("rb") as file:

        for chunk in iter(
            lambda: file.read(1024 * 1024),
            b"",
        ):
            digest.update(chunk)

    return digest.hexdigest()


def valid_pdf(path: Path) -> bool:

    try:

        if not path.is_file():
            return False

        if path.stat().st_size < 5:
            return False

        with path.open("rb") as file:
            return file.read(5) == b"%PDF-"

    except OSError:
        return False


def safe_filename(
    value: str,
    max_stem_length: int = 110,
) -> str:

    value = str(value or "").strip()

    value = re.sub(
        r'[<>:"/\\|?*\x00-\x1f]',
        "_",
        value,
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    value = value.strip(" .")

    if value.lower().endswith(".pdf"):
        value = value[:-4]

    value = value[:max_stem_length].rstrip(" .")

    if not value:
        value = "document"

    if value.upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "LPT1",
    }:
        value = "_" + value

    return value + ".pdf"


def parse_thai_date(
    text: str | None,
) -> datetime | None:

    if not text:
        return None

    match = re.search(
        r"(\d{1,2})\s+([ก-๙]+)\s+(\d{4})",
        text,
    )

    if not match:
        return None

    day = int(match.group(1))
    month = THAI_MONTHS.get(match.group(2))
    year = int(match.group(3))

    if month is None:
        return None

    if year >= 2400:
        year -= 543

    try:

        return datetime(
            year=year,
            month=month,
            day=day,
        )

    except ValueError:
        return None


def parse_edition(
    text: str | None,
) -> str:

    match = re.search(
        r"ฉบับที่\s*(\d+)",
        text or "",
    )

    return match.group(1) if match else ""


def service_id_of(service: dict) -> str:

    return str(
        service.get("Service_id")
        or service.get("Service_Id")
        or service.get("service_id")
        or ""
    ).strip()


def service_title_of(service: dict) -> str:

    return str(
        service.get("Service_name")
        or service.get("Service_Name")
        or service.get("Menu_Name")
        or "Untitled Newsletter"
    ).strip()


def service_subtitle_of(service: dict) -> str:

    return str(
        service.get("Service_Sub_Name")
        or service.get("Service_sub_name")
        or ""
    )


def decode_text(content: bytes) -> str:

    if not content:
        return ""

    try:
        return content.decode("utf-8-sig")

    except UnicodeDecodeError:
        return content.decode(
            "utf-8",
            errors="replace",
        )


def parse_json_bytes(content: bytes) -> Any:

    text = decode_text(content).strip()

    if not text:
        raise APIResponseError(
            "Response body is empty."
        )

    if text.startswith(")]}'"):
        text = text.split("\n", 1)[-1].strip()

    try:
        result = json.loads(text)

    except json.JSONDecodeError as exc:
        raise APIResponseError(
            "Invalid JSON response: "
            f"{exc.msg} at position {exc.pos}"
        ) from exc

    if isinstance(result, str):

        nested = result.strip()

        if nested.startswith(("[", "{")):

            try:
                result = json.loads(nested)

            except json.JSONDecodeError:
                pass

    return result


def parse_content_disposition(
    value: str,
) -> str | None:

    if not value:
        return None

    match = re.search(
        r"filename\*\s*=\s*(?:UTF-8'')?([^;]+)",
        value,
        flags=re.IGNORECASE,
    )

    if match:

        filename = unquote(
            match.group(1).strip().strip('"'),
            encoding="utf-8",
            errors="replace",
        )

        return safe_filename(filename)

    match = re.search(
        r'filename\s*=\s*"([^"]+)"',
        value,
        flags=re.IGNORECASE,
    )

    if not match:
        match = re.search(
            r"filename\s*=\s*([^;]+)",
            value,
            flags=re.IGNORECASE,
        )

    if match:
        return safe_filename(
            match.group(1).strip().strip('"')
        )

    return None


# ============================================================
# SHARED RATE LIMITER
# ============================================================

class RateLimiter:
    """
    Minimum time interval between request starts
    across all worker threads.

    This limits aggregate request frequency,
    not merely the number of concurrent workers.
    """

    def __init__(self, interval: float):

        self.interval = max(
            0.0,
            float(interval),
        )

        self._lock = threading.Lock()

        self._next_allowed = 0.0

    def wait(
        self,
        stop_event: threading.Event,
    ) -> None:

        if stop_event.is_set():
            raise CollectionStopped(
                "Collection stopped."
            )

        if self.interval <= 0:
            return

        with self._lock:

            if stop_event.is_set():
                raise CollectionStopped(
                    "Collection stopped."
                )

            now = time.monotonic()

            scheduled = max(
                now,
                self._next_allowed,
            )

            self._next_allowed = (
                scheduled + self.interval
            )

            sleep_time = scheduled - now

        if sleep_time > 0:

            if stop_event.wait(sleep_time):
                raise CollectionStopped(
                    "Collection stopped."
                )


# ============================================================
# CURL CONFIG HELPERS
# ============================================================

def curl_quote(value: str) -> str:

    value = str(value)

    if any(
        character in value
        for character in ("\r", "\n", "\x00")
    ):
        raise ConfigurationError(
            "Invalid control character in curl option."
        )

    value = (
        value
        .replace("\\", "\\\\")
        .replace('"', '\\"')
    )

    return f'"{value}"'


def curl_option(
    name: str,
    value: str | None = None,
) -> str:

    if value is None:
        return name

    return f"{name} = {curl_quote(value)}"


def parse_header_file(
    path: Path,
) -> tuple[int, dict[str, str]]:

    if not path.is_file():
        return 0, {}

    raw = path.read_bytes()

    blocks = re.split(
        rb"\r?\n\r?\n",
        raw,
    )

    for block in reversed(blocks):

        if not block.startswith(b"HTTP/"):
            continue

        text = block.decode(
            "iso-8859-1",
            errors="replace",
        )

        lines = text.splitlines()

        if not lines:
            continue

        match = re.match(
            r"HTTP/\S+\s+(\d{3})",
            lines[0],
        )

        if not match:
            continue

        status = int(match.group(1))

        headers: dict[str, str] = {}

        for line in lines[1:]:

            if ":" not in line:
                continue

            key, value = line.split(
                ":",
                1,
            )

            headers[key.strip().lower()] = (
                value.strip()
            )

        return status, headers

    return 0, {}


# ============================================================
# CURL RESPONSE
# ============================================================

@dataclass
class CurlResponse:

    status_code: int
    headers: dict[str, str]
    body_path: Path
    endpoint: str
    http_version: str = ""

    @property
    def body_size(self) -> int:

        if not self.body_path.is_file():
            return 0

        return self.body_path.stat().st_size

    @property
    def content_type(self) -> str:

        return self.headers.get(
            "content-type",
            "",
        )

    @property
    def content_disposition(self) -> str:

        return self.headers.get(
            "content-disposition",
            "",
        )

    def read_bytes(self) -> bytes:

        return self.body_path.read_bytes()

    def read_text(self) -> str:

        return decode_text(
            self.read_bytes()
        )

    def json(self) -> Any:

        return parse_json_bytes(
            self.read_bytes()
        )


# ============================================================
# CURL HTTP TRANSPORT
# ============================================================

class CurlTransport:

    def __init__(
        self,
        config: Config,
        rate_limiter: RateLimiter | None = None,
        stop_event: threading.Event | None = None,
    ):

        self.config = config
        self.curl = config.curl_executable

        self.rate_limiter = rate_limiter if rate_limiter is not None else RateLimiter(config.delay)
        self.stop_event = stop_event if stop_event is not None else threading.Event()

        self._debug_lock = threading.Lock()

    # --------------------------------------------------------
    # BUILD CURL CONFIG
    # --------------------------------------------------------

    def build_config(
        self,
        endpoint: str,
        data: dict[str, str] | None,
        authorization: bool,
    ) -> bytes:

        lines = [
            curl_option(
                "url",
                BASE_URL + endpoint,
            ),
            curl_option(
                "request",
                "POST",
            ),
            "silent",
            "show-error",
            "globoff",
        ]

        headers = [
            ("accept", "*/*"),
            ("accept-language", "th-TH,th;q=0.9"),
        ]

        if authorization:

            headers.append((
                "authorization",
                self.config.authorization,
            ))

        headers.extend([
            (
                "origin",
                BASE_URL,
            ),
            (
                "priority",
                "u=1, i",
            ),
            (
                "referer",
                self.config.referer,
            ),
            (
                "sec-ch-ua",
                CHROME_CLIENT_HINTS,
            ),
            (
                "sec-ch-ua-mobile",
                "?0",
            ),
            (
                "sec-ch-ua-platform",
                '"Windows"',
            ),
            (
                "sec-fetch-dest",
                "empty",
            ),
            (
                "sec-fetch-mode",
                "cors",
            ),
            (
                "sec-fetch-site",
                "same-origin",
            ),
            (
                "user-agent",
                USER_AGENT,
            ),
            (
                "x-requested-with",
                "XMLHttpRequest",
            ),
        ])

        # The successful Chrome GetService request
        # uses an empty POST body and Content-Length: 0.
        if data is None:

            headers.append((
                "content-length",
                "0",
            ))

        for name, value in headers:

            lines.append(
                curl_option(
                    "header",
                    f"{name}: {value}",
                )
            )

        # Equivalent to curl -b with the complete cookie.
        lines.append(
            curl_option(
                "cookie",
                self.config.cookie,
            )
        )

        if data is not None:

            for key, value in data.items():

                lines.append(
                    curl_option(
                        "data-urlencode",
                        f"{key}={value}",
                    )
                )

        return (
            "\n".join(lines) + "\n"
        ).encode("utf-8")

    # --------------------------------------------------------
    # REDACT SECRETS
    # --------------------------------------------------------

    def redact(self, text: str) -> str:

        result = str(text)

        for secret in (
            self.config.authorization,
            self.config.cookie,
        ):

            if secret:

                result = result.replace(
                    secret,
                    "[REDACTED]",
                )

        result = re.sub(
            r"(?im)^.*(?:authorization|set-cookie|cookie)"
            r"\s*[:=].*$",
            "[REDACTED HEADER]",
            result,
        )

        return result

    # --------------------------------------------------------
    # DEBUG METADATA
    # --------------------------------------------------------

    def save_debug(
        self,
        response: CurlResponse,
    ) -> None:

        if not self.config.debug_response:
            return

        debug_dir = (
            self.config.output / "_debug"
        )

        debug_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        body = response.read_bytes()

        metadata = {
            "timestamp": now_iso(),
            "endpoint": response.endpoint,
            "status_code": response.status_code,
            "http_version": response.http_version,
            "content_type": response.content_type,
            "body_size": len(body),
            "body_sha256": hashlib.sha256(
                body
            ).hexdigest(),
            "server": response.headers.get(
                "server",
                "",
            ),
            "cf_ray": response.headers.get(
                "cf-ray",
                "",
            ),
            "response_preview": self.redact(
                decode_text(body[:200])
            ),
        }

        name = response.endpoint.strip("/").replace(
            "/",
            "_",
        )

        with self._debug_lock:

            suffix = datetime.now().strftime(
                "%Y%m%d_%H%M%S_%f"
            )

            path = (
                debug_dir
                / f"{name}_{suffix}.json"
            )

            path.write_text(
                json.dumps(
                    metadata,
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

        LOG.info(
            "Debug metadata saved: %s",
            path,
        )

    # --------------------------------------------------------
    # EXECUTE HTTP REQUEST
    # --------------------------------------------------------

    @contextmanager
    def post(
        self,
        endpoint: str,
        data: dict[str, str] | None = None,
        *,
        authorization: bool = True,
    ) -> Iterator[CurlResponse]:

        self.rate_limiter.wait(
            self.stop_event
        )

        if self.stop_event.is_set():
            raise CollectionStopped(
                "Collection stopped."
            )

        with tempfile.TemporaryDirectory(
            prefix=".tird_http_",
            dir=self.config.output,
        ) as temporary_directory:

            temporary = Path(
                temporary_directory
            )

            body_path = (
                temporary / "response.body"
            )

            header_path = (
                temporary / "response.headers"
            )

            config_bytes = self.build_config(
                endpoint=endpoint,
                data=data,
                authorization=authorization,
            )

            command = [
                self.curl,
                "--config",
                "-",
                "--output",
                str(body_path),
                "--dump-header",
                str(header_path),
                "--write-out",
                (
                    "HTTP_CODE=%{http_code}\n"
                    "HTTP_VERSION=%{http_version}\n"
                ),
                "--connect-timeout",
                str(min(self.config.timeout, 20)),
                "--max-time",
                str(self.config.timeout),
                "--retry",
                str(self.config.retries),
                "--retry-delay",
                "1",
            ]

            if self.config.verbose:

                LOG.debug(
                    "POST %s | fields=%s",
                    endpoint,
                    (
                        list(data.keys())
                        if data is not None
                        else []
                    ),
                )

            process_timeout = (
                self.config.timeout
                * (self.config.retries + 1)
                + 15
            )

            try:

                completed = subprocess.run(
                    command,
                    input=config_bytes,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
                    timeout=process_timeout,
                    check=False,
                )

            except subprocess.TimeoutExpired as exc:

                raise CurlError(
                    f"Request timed out at {endpoint}"
                ) from exc

            except OSError as exc:

                raise CurlError(
                    f"Could not execute curl.exe: {exc}"
                ) from exc

            stdout = completed.stdout.decode(
                "utf-8",
                errors="replace",
            )

            stderr = self.redact(
                completed.stderr.decode(
                    "utf-8",
                    errors="replace",
                )
            )

            if completed.returncode != 0:

                raise CurlError(
                    f"curl.exe failed at {endpoint}. "
                    f"Exit={completed.returncode}. "
                    f"{stderr[:400]}"
                )

            status, headers = parse_header_file(
                header_path
            )

            metadata: dict[str, str] = {}

            for line in stdout.splitlines():

                if "=" not in line:
                    continue

                key, value = line.split(
                    "=",
                    1,
                )

                metadata[key.strip()] = (
                    value.strip()
                )

            if status == 0:

                try:

                    status = int(
                        metadata.get(
                            "HTTP_CODE",
                            "0",
                        )
                    )

                except ValueError:
                    status = 0

            response = CurlResponse(
                status_code=status,
                headers=headers,
                body_path=body_path,
                endpoint=endpoint,
                http_version=metadata.get(
                    "HTTP_VERSION",
                    "",
                ),
            )

            if self.config.verbose:

                LOG.debug(
                    "HTTP %s | %s | %s bytes | HTTP/%s",
                    response.status_code,
                    response.content_type or "unknown",
                    response.body_size,
                    response.http_version or "?",
                )

            if response.status_code in (
                301,
                302,
                303,
                307,
                308,
            ):

                self.save_debug(response)

                raise AuthenticationError(
                    f"HTTP {response.status_code} "
                    f"redirect at {endpoint}."
                )

            if response.status_code in (
                401,
                403,
            ):

                self.save_debug(response)

                raise AuthenticationError(
                    f"HTTP {response.status_code} "
                    f"at {endpoint}. "
                    "Authentication or access rejected."
                )

            if not 200 <= response.status_code < 300:

                self.save_debug(response)

                raise CurlError(
                    f"HTTP {response.status_code} "
                    f"at {endpoint}"
                )

            if response.body_size == 0:

                self.save_debug(response)

                raise APIResponseError(
                    f"{endpoint} returned an empty body."
                )

            yield response


# ============================================================
# TIRD API CLIENT
# ============================================================

class TIRDClient:

    def __init__(
        self,
        config: Config,
        stop_event: threading.Event | None = None,
    ):

        self.config = config

        self.stop_event = stop_event if stop_event is not None else threading.Event()

        self.rate_limiter = RateLimiter(
            config.delay
        )

        self.transport = CurlTransport(
            config=config,
            rate_limiter=self.rate_limiter,
            stop_event=self.stop_event,
        )

    # --------------------------------------------------------
    # JSON REQUEST
    # --------------------------------------------------------

    def post_json(
        self,
        endpoint: str,
        data: dict[str, str] | None = None,
    ) -> Any:

        with self.transport.post(
            endpoint,
            data=data,
        ) as response:

            try:

                return response.json()

            except APIResponseError:

                self.transport.save_debug(
                    response
                )

                if "text/html" in response.content_type.lower():

                    raise AuthenticationError(
                        f"{endpoint} returned HTML "
                        "instead of JSON."
                    )

                raise

    # --------------------------------------------------------
    # CATALOG
    # --------------------------------------------------------

    def get_services(self, *, quiet: bool = False) -> list[dict]:

        if not quiet:
            LOG.info("Fetching TIRD newsletter catalog")

        result = self.post_json(
            API["catalog"],
            data=None,
        )

        services: list | None = None

        if isinstance(result, list):

            services = result

        elif isinstance(result, dict):

            for key in (
                "data",
                "Data",
                "result",
                "Result",
                "items",
                "Items",
            ):

                value = result.get(key)

                if isinstance(value, list):

                    services = value
                    break

                if isinstance(value, str):

                    try:

                        nested = json.loads(
                            value
                        )

                        if isinstance(nested, list):

                            services = nested
                            break

                    except json.JSONDecodeError:
                        pass

        if services is None:

            raise APIResponseError(
                "Unexpected GetService response structure."
            )

        accessible = []

        for service in services:

            if not isinstance(service, dict):
                continue

            if not service_id_of(service):
                continue

            if service.get(
                "Permission_used",
                "Y",
            ) != "Y":
                continue

            if service.get(
                "IsActive",
                True,
            ) is False:
                continue

            accessible.append(service)

        if not quiet:
            LOG.info("Catalog records: %s", len(services))
            LOG.info("Accessible documents: %s", len(accessible))

        return accessible

    # --------------------------------------------------------
    # SERVICE DETAILS
    # --------------------------------------------------------

    def get_service_detail(
        self,
        service_id: str,
    ) -> dict:

        result = self.post_json(
            API["detail"],
            data={
                "Service_Id": service_id,
            },
        )

        if not isinstance(result, dict):

            raise APIResponseError(
                "GetServiceById returned "
                "an unexpected response."
            )

        return result

    def get_service_key(
        self,
        service_id: str,
    ) -> str:

        detail = self.get_service_detail(
            service_id
        )

        service_key = (
            detail.get("workspace_id")
            or detail.get("Workspace_Id")
            or detail.get("Workspace_id")
        )

        if not service_key:

            for name in (
                "data",
                "Data",
                "result",
                "Result",
            ):

                nested = detail.get(name)

                if isinstance(nested, dict):

                    service_key = (
                        nested.get("workspace_id")
                        or nested.get("Workspace_Id")
                        or nested.get("Workspace_id")
                    )

                    if service_key:
                        break

        if not service_key:

            raise APIResponseError(
                "workspace_id not found for "
                f"Service_Id={service_id}"
            )

        return str(service_key)

    # --------------------------------------------------------
    # CONTENT PAGE
    # --------------------------------------------------------

    def show_content(
        self,
        service_id: str,
        service_key: str,
    ) -> None:

        payload = {
            "Form": "Content",
            "Service_Id": service_id,
            "Service_Key": service_key,
        }

        with self.transport.post(
            API["content"],
            data=payload,
        ) as response:

            if self.config.verbose:

                LOG.debug(
                    "ShowContentFiles: HTTP %s, %s bytes",
                    response.status_code,
                    response.body_size,
                )

    # --------------------------------------------------------
    # PDF DOWNLOAD
    # --------------------------------------------------------

    def download_pdf(
        self,
        service_id: str,
        service_key: str,
        output_dir: Path,
        preferred_filename: str,
    ) -> tuple[Path, int, str]:

        if self.stop_event.is_set():
            raise CollectionStopped(
                "Collection stopped."
            )

        output_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        payload = {
            "Form": "Content",
            "Service_Id": service_id,
            "Service_Key": service_key,
        }

        if not self.config.skip_content_page:

            self.show_content(
                service_id,
                service_key,
            )

        with self.transport.post(
            API["download"],
            data=payload,
            authorization=False,
        ) as response:

            if not valid_pdf(
                response.body_path
            ):

                self.transport.save_debug(
                    response
                )

                raise PDFDownloadError(
                    "ListFiles did not return a valid PDF. "
                    f"Content-Type={response.content_type}; "
                    f"Bytes={response.body_size}"
                )

            filename = (
                parse_content_disposition(
                    response.content_disposition
                )
                or preferred_filename
            )

            unique_suffix = hashlib.sha256(
                service_id.encode("utf-8")
            ).hexdigest()[:12]

            final_filename = safe_filename(
                f"{Path(filename).stem[:105]}_"
                f"{unique_suffix}.pdf"
            )

            destination = (
                output_dir / final_filename
            )

            source_size = response.body_size

            expected_header = response.headers.get(
                "content-length",
                "",
            )

            if expected_header:

                try:

                    expected_size = int(
                        expected_header
                    )

                except ValueError:

                    expected_size = None

                if (
                    expected_size is not None
                    and expected_size != source_size
                ):

                    raise PDFDownloadError(
                        "Incomplete PDF response. "
                        f"Expected={expected_size}; "
                        f"Received={source_size}"
                    )

            checksum = sha256_file(
                response.body_path
            )

            # Unique temporary name prevents collisions
            # between concurrent download workers.
            temporary_path: Path | None = None

            try:

                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    prefix=".tird_pdf_",
                    suffix=".part",
                    dir=output_dir,
                    delete=False,
                ) as temporary_file:

                    temporary_path = Path(
                        temporary_file.name
                    )

                    with response.body_path.open(
                        "rb"
                    ) as source:

                        shutil.copyfileobj(
                            source,
                            temporary_file,
                            length=1024 * 1024,
                        )

                if not valid_pdf(
                    temporary_path
                ):

                    raise PDFDownloadError(
                        "Temporary PDF failed validation."
                    )

                if (
                    temporary_path.stat().st_size
                    != source_size
                ):

                    raise PDFDownloadError(
                        "PDF copy size mismatch."
                    )

                if sha256_file(temporary_path) != checksum:

                    raise PDFDownloadError(
                        "PDF SHA256 mismatch."
                    )

                os.replace(
                    temporary_path,
                    destination,
                )

                temporary_path = None

            finally:

                if temporary_path is not None:

                    temporary_path.unlink(
                        missing_ok=True
                    )

            return (
                destination,
                source_size,
                checksum,
            )


# ============================================================
# MANIFEST
# ============================================================

class Manifest:

    """
    Manifest is only modified by the main thread.
    Worker threads return results without writing CSV.
    """

    def __init__(
        self,
        output: Path,
        verify_existing: bool = False,
    ):

        self.output = output.resolve()

        self.path = (
            self.output / "manifest.csv"
        )

        self.verify_existing = verify_existing

        self.records: dict[str, dict] = {}

        self.load()

    def load(self) -> None:

        if not self.path.is_file():
            return

        with self.path.open(
            "r",
            encoding="utf-8-sig",
            newline="",
        ) as file:

            reader = csv.DictReader(file)

            for record in reader:

                service_id = record.get(
                    "service_id",
                    "",
                )

                if service_id:

                    self.records[service_id] = record

    def get(
        self,
        service_id: str,
    ) -> dict | None:

        return self.records.get(
            service_id
        )

    def completed(
        self,
        service_id: str,
    ) -> bool:

        record = self.get(service_id)

        if not record:
            return False

        if record.get(
            "status"
        ) != "success":
            return False

        relative = record.get(
            "file_path",
            "",
        )

        if not relative:
            return False

        path = (
            self.output / relative
        ).resolve()

        if not path.is_relative_to(self.output):
            return False

        if not valid_pdf(path):
            return False

        expected_size = (
            record.get("file_size")
            or record.get("size_bytes")
        )

        if expected_size:

            try:

                if (
                    path.stat().st_size
                    != int(expected_size)
                ):
                    return False

            except ValueError:
                return False

        if self.verify_existing:

            expected_hash = record.get(
                "sha256",
                "",
            )

            if expected_hash:

                if sha256_file(path) != expected_hash:
                    return False

        return True

    def upsert(
        self,
        record: dict,
    ) -> None:

        service_id = record["service_id"]

        self.records[service_id] = {
            column: record.get(
                column,
                "",
            )
            for column in MANIFEST_COLUMNS
        }

        self.save()

    def save(self) -> None:

        self.output.mkdir(
            parents=True,
            exist_ok=True,
        )

        temporary: Path | None = None

        try:

            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8-sig",
                newline="",
                prefix=".tird_manifest_",
                suffix=".tmp",
                dir=self.output,
                delete=False,
            ) as file:

                temporary = Path(file.name)

                writer = csv.DictWriter(
                    file,
                    fieldnames=MANIFEST_COLUMNS,
                )

                writer.writeheader()

                writer.writerows(
                    self.records.values()
                )

            os.replace(
                temporary,
                self.path,
            )

            temporary = None

        finally:

            if temporary is not None:

                temporary.unlink(
                    missing_ok=True
                )


# ============================================================
# DOCUMENT RESULT
# ============================================================

@dataclass
class DownloadResult:

    service_id: str
    title: str
    status: str

    record: dict | None = None

    error: str = ""
    size: int = 0
    elapsed: float = 0.0


# ============================================================
# PARALLEL COLLECTOR
# ============================================================

class TIRDCollector:

    def __init__(
        self,
        config: Config,
        force: bool = False,
    ):

        self.config = config
        self.force = force

        self.stop_event = threading.Event()

        self.client = TIRDClient(
            config=config,
            stop_event=self.stop_event,
        )

        self.manifest = Manifest(
            config.output,
            verify_existing=config.verify_existing,
        )

        self.stats = {
            "total": 0,
            "downloaded": 0,
            "skipped": 0,
            "failed": 0,
        }

    # --------------------------------------------------------
    # CONNECTION TEST
    # --------------------------------------------------------

    def test_connection(self) -> bool:

        LOG.info(
            "Starting TIRD API connection test"
        )

        LOG.info(
            "HTTP engine: curl.exe"
        )

        LOG.info(
            "Authorization configured: %s",
            bool(self.config.authorization),
        )

        LOG.info(
            "Cookie configured: %s",
            bool(self.config.cookie),
        )

        LOG.info(
            "Referer configured: %s",
            bool(self.config.referer),
        )

        LOG.info(
            "Testing GetService"
        )

        services = self.client.get_services()

        LOG.info(
            "GetService: PASS"
        )

        if not services:

            LOG.warning(
                "No accessible documents in catalog."
            )

            return True

        LOG.info(
            "Testing GetServiceById"
        )

        service_id = service_id_of(
            services[0]
        )

        service_key = self.client.get_service_key(
            service_id
        )

        if not service_key:

            raise APIResponseError(
                "Unable to resolve service workspace."
            )

        LOG.info(
            "GetServiceById: PASS"
        )

        if RICH_AVAILABLE:

            console.print()

            console.print(
                Panel(
                    "[bold green]"
                    "API CONNECTION SUCCESSFUL"
                    "[/bold green]\n\n"
                    f"Catalog: {len(services)} documents\n"
                    "GetService: PASS\n"
                    "GetServiceById: PASS\n"
                    "PDF download: NOT TESTED",
                    border_style="green",
                    padding=(1, 3),
                )
            )

        else:

            print()
            print("=" * 56)
            print("API CONNECTION SUCCESSFUL")
            print(f"Catalog: {len(services)} documents")
            print("GetService: PASS")
            print("GetServiceById: PASS")
            print("PDF download: NOT TESTED")
            print("=" * 56)

        return True

    # --------------------------------------------------------
    # LIST DOCUMENTS
    # --------------------------------------------------------

    def list_services(
        self,
        services: list[dict],
    ) -> None:

        if RICH_AVAILABLE:

            table = Table(
                title="TIRD NEWSLETTER CATALOG",
                border_style="cyan",
                show_lines=False,
            )

            table.add_column(
                "#",
                justify="right",
                style="dim",
            )

            table.add_column(
                "Newsletter",
                style="white",
            )

            table.add_column(
                "Edition",
                justify="center",
                style="cyan",
            )

            table.add_column(
                "Published",
                style="green",
            )

            table.add_column(
                "Local",
                justify="center",
            )

            for index, service in enumerate(
                services,
                1,
            ):

                service_id = service_id_of(
                    service
                )

                subtitle = service_subtitle_of(
                    service
                )

                published = parse_thai_date(
                    subtitle
                )

                completed = self.manifest.completed(
                    service_id
                )

                table.add_row(
                    str(index),
                    service_title_of(service),
                    parse_edition(subtitle) or "-",
                    (
                        published.strftime("%Y-%m-%d")
                        if published
                        else "-"
                    ),
                    (
                        "[green]Saved[/green]"
                        if completed
                        else "[dim]-[/dim]"
                    ),
                )

            console.print(table)

            console.print(
                f"\n[dim]Total: {len(services)} documents[/dim]"
            )

        else:

            print()

            for index, service in enumerate(
                services,
                1,
            ):

                status = (
                    "SAVED"
                    if self.manifest.completed(
                        service_id_of(service)
                    )
                    else "NEW"
                )

                print(
                    f"{index:03d} | {status:5} | "
                    f"{service_title_of(service)}"
                )

            print(
                f"\nTotal: {len(services)}"
            )

    # --------------------------------------------------------
    # CREATE MANIFEST RECORD
    # --------------------------------------------------------

    def build_record(
        self,
        service: dict,
    ) -> tuple[dict, Path, str]:

        service_id = service_id_of(service)

        title = service_title_of(service)

        subtitle = service_subtitle_of(service)

        edition = parse_edition(subtitle)

        published = parse_thai_date(subtitle)

        if published:

            output_dir = (
                self.config.output
                / str(published.year)
                / f"{published.month:02d}"
            )

            published_date = published.strftime(
                "%Y-%m-%d"
            )

            year = str(published.year)

        else:

            output_dir = (
                self.config.output / "unknown_date"
            )

            published_date = ""
            year = ""

        preferred_filename = safe_filename(
            f"{edition + '_' if edition else ''}"
            f"{title}.pdf"
        )

        record = {
            "service_id": service_id,
            "title": title,
            "edition": edition,
            "published_date": published_date,
            "year": year,
            "filename": "",
            "file_path": "",
            "file_size": "",
            "sha256": "",
            "status": "failed",
            "downloaded_at": now_iso(),
            "error": "",
        }

        return (
            record,
            output_dir,
            preferred_filename,
        )

    # --------------------------------------------------------
    # WORKER
    # --------------------------------------------------------

    def download_worker(
        self,
        service: dict,
    ) -> DownloadResult:

        started = time.monotonic()

        service_id = service_id_of(service)

        title = service_title_of(service)

        if self.stop_event.is_set():

            return DownloadResult(
                service_id=service_id,
                title=title,
                status="cancelled",
            )

        record, output_dir, preferred_filename = (
            self.build_record(service)
        )

        try:

            service_key = self.client.get_service_key(
                service_id
            )

            destination, size, checksum = (
                self.client.download_pdf(
                    service_id=service_id,
                    service_key=service_key,
                    output_dir=output_dir,
                    preferred_filename=preferred_filename,
                )
            )

            record.update({
                "filename": destination.name,
                "file_path": destination.relative_to(
                    self.config.output
                ).as_posix(),
                "file_size": size,
                "sha256": checksum,
                "status": "success",
                "downloaded_at": now_iso(),
                "error": "",
            })

            return DownloadResult(
                service_id=service_id,
                title=title,
                status="success",
                record=record,
                size=size,
                elapsed=time.monotonic() - started,
            )

        except AuthenticationError:

            self.stop_event.set()
            raise

        except CollectionStopped:

            return DownloadResult(
                service_id=service_id,
                title=title,
                status="cancelled",
                elapsed=time.monotonic() - started,
            )

        except (
            CurlError,
            APIResponseError,
            PDFDownloadError,
            OSError,
            ValueError,
        ) as exc:

            record["status"] = "failed"
            record["error"] = str(exc)
            record["downloaded_at"] = now_iso()

            return DownloadResult(
                service_id=service_id,
                title=title,
                status="failed",
                record=record,
                error=str(exc),
                elapsed=time.monotonic() - started,
            )

    # --------------------------------------------------------
    # PROCESS RESULT
    # --------------------------------------------------------

    def process_result(
        self,
        result: DownloadResult,
    ) -> None:

        if result.status == "success":

            if result.record is not None:

                self.manifest.upsert(
                    result.record
                )

            self.stats["downloaded"] += 1

            LOG.info(
                "SUCCESS | %.2f MB | %.2fs | %s",
                result.size / (1024 * 1024),
                result.elapsed,
                result.title,
            )

        elif result.status == "failed":

            # Preserve the previous successful manifest
            # if a forced redownload has failed.
            if (
                result.record is not None
                and not self.manifest.completed(
                    result.service_id
                )
            ):

                self.manifest.upsert(
                    result.record
                )

            self.stats["failed"] += 1

            LOG.error(
                "FAILED | %s | %s",
                result.title,
                result.error,
            )

        elif result.status == "cancelled":

            LOG.warning(
                "CANCELLED | %s",
                result.title,
            )

    # --------------------------------------------------------
    # COLLECT IN PARALLEL
    # --------------------------------------------------------

    def collect(
        self,
        services: list[dict],
    ) -> int:

        started = time.monotonic()

        self.stats["total"] = len(services)

        pending_services = []

        for service in services:

            service_id = service_id_of(
                service
            )

            if (
                not self.force
                and self.manifest.completed(
                    service_id
                )
            ):

                self.stats["skipped"] += 1

                LOG.info(
                    "SKIP | %s",
                    service_title_of(service),
                )

            else:

                pending_services.append(
                    service
                )

        LOG.info(
            "Total documents: %s",
            len(services),
        )

        LOG.info(
            "Already completed: %s",
            self.stats["skipped"],
        )

        LOG.info(
            "Pending downloads: %s",
            len(pending_services),
        )

        LOG.info(
            "Parallel workers: %s",
            self.config.parallel,
        )

        LOG.info(
            "Request interval: %.2fs",
            self.config.delay,
        )

        if not pending_services:

            show_summary(
                self.stats,
                self.config.output,
                time.monotonic() - started,
            )

            return 0

        if RICH_AVAILABLE:

            self._collect_with_rich(
                pending_services
            )

        else:

            self._collect_without_rich(
                pending_services
            )

        elapsed = time.monotonic() - started

        show_summary(
            self.stats,
            self.config.output,
            elapsed,
        )

        return (
            0 if self.stats["failed"] == 0
            else 1
        )

    # --------------------------------------------------------
    # PARALLEL - RICH
    # --------------------------------------------------------

    def _collect_with_rich(
        self,
        services: list[dict],
    ) -> None:

        with Progress(
            SpinnerColumn(),
            TextColumn(
                "[cyan]{task.description}"
            ),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=console,
        ) as progress:

            task = progress.add_task(
                "Downloading newsletters",
                total=len(services),
            )

            self._execute_workers(
                services=services,
                progress=progress,
                task_id=task,
            )

    # --------------------------------------------------------
    # PARALLEL - BASIC
    # --------------------------------------------------------

    def _collect_without_rich(
        self,
        services: list[dict],
    ) -> None:

        self._execute_workers(
            services=services,
            progress=None,
            task_id=None,
        )

    # --------------------------------------------------------
    # EXECUTOR
    # --------------------------------------------------------

    def _execute_workers(
        self,
        services: list[dict],
        progress: Any,
        task_id: Any,
    ) -> None:

        futures: dict[Future, dict] = {}

        first_error: AuthenticationError | None = None

        # The executor is responsible only for network and
        # filesystem work. All manifest updates occur here
        # in the main thread.
        with ThreadPoolExecutor(
            max_workers=self.config.parallel,
            thread_name_prefix="TIRD",
        ) as executor:

            for service in services:

                future = executor.submit(
                    self.download_worker,
                    service,
                )

                futures[future] = service

            completed_count = 0

            try:

                for future in as_completed(futures):

                    service = futures[future]

                    completed_count += 1

                    try:

                        result = future.result()

                    except AuthenticationError as exc:

                        first_error = exc

                        self.stop_event.set()

                        for pending_future in futures:
                            pending_future.cancel()

                        break

                    except Exception as exc:

                        # Unexpected worker failures should not
                        # crash the entire collector.
                        result = DownloadResult(
                            service_id=service_id_of(service),
                            title=service_title_of(service),
                            status="failed",
                            error=(
                                f"{type(exc).__name__}: {exc}"
                            ),
                        )

                    self.process_result(
                        result
                    )

                    if progress is not None:

                        progress.advance(
                            task_id
                        )

                        progress.update(
                            task_id,
                            description=(
                                f"Processed "
                                f"{completed_count}/{len(services)}"
                            ),
                        )

                    else:

                        LOG.info(
                            "Progress: %s/%s",
                            completed_count,
                            len(services),
                        )

            except KeyboardInterrupt:

                self.stop_event.set()

                for future in futures:
                    future.cancel()

                raise

        if first_error is not None:
            raise first_error


# ============================================================
# CLI
# ============================================================

def parse_args() -> argparse.Namespace:

    parser = argparse.ArgumentParser(
        prog="tird_collector.py",
        description=APP_NAME,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "--test-only",
        action="store_true",
        help="Test GetService and GetServiceById",
    )

    parser.add_argument(
        "--list",
        action="store_true",
        help="Display the newsletter catalog",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of documents",
    )

    parser.add_argument(
        "--year",
        type=int,
        default=None,
        help="Filter publication year (2026 or 2569)",
    )

    parser.add_argument(
        "--service-id",
        type=str,
        default=None,
        help="Download one specific service ID",
    )

    parser.add_argument(
        "--scope",
        choices=("both", "advisor", "insights"),
        default="both",
        help="Catalog section to list or download",
    )

    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run downloads in CLI mode instead of Textual",
    )

    parser.add_argument(
        "--diagnose-scopes",
        action="store_true",
        help="Test Advisor -> Insights -> Advisor catalog routing (no PDFs)",
    )

    parser.add_argument(
        "--ui",
        action="store_true",
        help="Launch Textual interface",
    )

    parser.add_argument(
        "--parallel",
        type=int,
        default=DEFAULT_PARALLEL,
        help="Maximum concurrent download workers",
    )

    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY,
        help="Minimum interval between HTTP request starts",
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help="Maximum seconds per HTTP transfer attempt",
    )

    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help="Retries for transient curl HTTP errors",
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Output directory",
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="Redownload completed documents",
    )

    parser.add_argument(
        "--verify-existing",
        action="store_true",
        help="Verify SHA256 of existing PDFs",
    )

    parser.add_argument(
        "--skip-content-page",
        action="store_true",
        help="Skip ShowContentFiles request",
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show non-sensitive HTTP diagnostics",
    )

    parser.add_argument(
        "--debug-response",
        action="store_true",
        help="Save safe response metadata on failure",
    )

    return parser.parse_args()


# ============================================================
# FILTER SERVICES
# ============================================================

def filter_services(
    services: list[dict],
    args: argparse.Namespace,
) -> list[dict]:

    result = services

    if args.service_id:

        result = [
            service
            for service in result
            if service_id_of(service)
            == args.service_id
        ]

    if args.year is not None:

        target_year = (
            args.year - 543
            if args.year >= 2400
            else args.year
        )

        filtered = []

        for service in result:

            published = parse_thai_date(
                service_subtitle_of(service)
            )

            if (
                published is not None
                and published.year == target_year
            ):

                filtered.append(
                    service
                )

        result = filtered

    if args.limit is not None:

        result = result[:args.limit]

    return result


# ============================================================
# MAIN
# ============================================================

def main() -> int:
    args = parse_args()
    setup_logging(args.verbose)
    if args.limit is not None and args.limit <= 0:
        LOG.error("--limit must be greater than zero")
        return 2
    if not (1 <= args.parallel <= MAX_PARALLEL):
        LOG.error("--parallel must be between 1 and %s", MAX_PARALLEL)
        return 2
    if args.delay < 0:
        LOG.error("--delay must be >= 0")
        return 2

    # Load only local .env; no credentials are placed in Git or the UI log.
    try:
        config = Config.load(args)
        from tird_scopes import configured_scopes, collect_catalogs, filter_documents, ParallelArchive
        scopes = configured_scopes(config, args.scope)
    except (ConfigurationError, ValueError, OSError) as exc:
        LOG.error("CONFIGURATION ERROR: %s", exc)
        return 2

    interactive = args.ui or not (
        args.test_only or args.list or args.headless or args.diagnose_scopes
    )
    if interactive:
        try:
            from tird_ui import run_app
            return run_app(args, sys.modules[__name__])
        except ImportError as exc:
            LOG.error("UI dependencies missing: %s; pip install -r requirements.txt", exc)
            return 2

    show_header()
    try:
        if args.diagnose_scopes:
            from tird_scopes import diagnose_scope_routing
            report = diagnose_scope_routing(scopes)
            print("TIRD A/B/A ROUTING DIAGNOSTIC")
            print(f"Advisor first:   {report.advisor_first} documents")
            print(f"Insights:        {report.insights} documents")
            print(f"Advisor repeat:  {report.advisor_repeat} documents")
            print(f"Overlap:         {report.overlap} Service IDs")
            print(f"Advisor stable:  {report.advisor_stable}")
            print(f"Distinct:        {report.distinct}")
            print(f"Shared session:  {report.shared_credentials}")
            print(
                "Result:          " + (
                    "PASS (referer routing consistent)"
                    if report.consistent else
                    "FAIL (verify menu URLs and server-side session context)"
                )
            )
            print("PDF download:   NOT TESTED")
            return 0 if report.consistent else 1

        documents, errors = collect_catalogs(scopes)
        for name, message in errors.items():
            LOG.error("%s: %s", name, message)
        if not documents:
            return 1 if errors else 0
        documents = filter_documents(documents, args)
        if args.test_only:
            from collections import Counter
            counts = Counter(doc.scope for doc in documents)
            for scope in scopes:
                if scope.slug in errors:
                    print(f"{scope.label}: ERROR")
                else:
                    print(f"{scope.label}: {counts[scope.slug]} accessible documents | GetService and GetServiceById PASS")
            print("PDF download: NOT TESTED")
            return 1 if errors else 0
        if args.list:
            from rich.table import Table
            table = Table(title="TIRD / Advisor + Business Insights")
            for heading in ("CATEGORY", "ISSUE", "DATE", "NEWSLETTER"):
                table.add_column(heading)
            for doc in documents:
                subtitle = service_subtitle_of(doc.service)
                issued = parse_thai_date(subtitle)
                table.add_row(doc.label, parse_edition(subtitle) or "-", issued.strftime("%Y-%m-%d") if issued else "-", service_title_of(doc.service))
            console.print(table) if console else print(f"{len(documents)} records")
            return 1 if errors else 0
        archive = ParallelArchive(scopes, config, force=args.force)
        stats = archive.collect(documents)
        print(f"COMPLETE / saved {stats['downloaded']} / skipped {stats['skipped']} / failed {stats['failed']}")
        return 1 if stats['failed'] or errors else 0
    except (TIRDError, OSError, ValueError) as exc:
        LOG.error("TIRD ERROR: %s", exc)
        return 1
    except KeyboardInterrupt:
        LOG.warning("Interrupted")
        return 130


if __name__ == "__main__":
    # Unify exception/model types when scope helpers import this script.
    sys.modules.setdefault("tird_collector", sys.modules[__name__])
    sys.exit(main())
