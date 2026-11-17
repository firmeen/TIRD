
"""
TIRD NEWSLETTER COLLECTOR
Version 4.0.0
=========================

HTTP Engine:
    Windows curl.exe via subprocess
    Credentials passed through stdin, not command-line arguments.

Requirements:
    pip install python-dotenv rich textual

Required files:
    tird_collector.py
    .env

.env:
    TIRD_AUTHORIZATION="YOUR_AUTHORIZATION"
    TIRD_COOKIE="YOUR_COMPLETE_COOKIE"
    TIRD_REFERER="https://tirdmember.tird.insure/Content?_mId=YOUR_ID"

Commands:
    python tird_collector.py --ui
    python tird_collector.py --headless
    python tird_collector.py --test-only
    python tird_collector.py --test-only --verbose
    python tird_collector.py --list
    python tird_collector.py --limit 1
    python tird_collector.py --year 2026
    python tird_collector.py --service-id YOUR_SERVICE_ID
    python tird_collector.py
    python tird_collector.py --force
    python tird_collector.py --debug-response

Features:
    - Authenticated HTTP requests using curl.exe
    - TIRD newsletter catalog discovery
    - Service detail and workspace ID retrieval
    - PDF download using ListFiles
    - Resume and duplicate prevention
    - Year/month directory organization
    - Atomic PDF writes
    - SHA256 verification
    - CSV manifest
    - Rich terminal output
    - Safe HTTP diagnostics

The collector uses an existing authorized session.
It does not bypass authentication or access restrictions.
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
import time

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import unquote

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
# CONSTANTS
# ============================================================

APP_NAME = "TIRD NEWSLETTER COLLECTOR"
APP_VERSION = "4.1.0"

BASE_URL = "https://tirdmember.tird.insure"

API = {
    "catalog": "/Content/GetService",
    "detail": "/Content/GetServiceById",
    "content": "/Content/ShowContentFiles",
    "download": "/Content/ListFiles",
}

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = PROJECT_DIR / "TIRD_Newsletter"

DEFAULT_TIMEOUT = 45
DEFAULT_DELAY = 1.0

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


# ============================================================
# CONFIGURATION
# ============================================================

@dataclass
class Config:

    authorization: str
    cookie: str
    referer: str

    curl_executable: str
    output: Path

    timeout: int = DEFAULT_TIMEOUT
    delay: float = DEFAULT_DELAY

    verbose: bool = False
    debug_response: bool = False
    skip_content_page: bool = False

    @classmethod
    def load(cls, args: argparse.Namespace) -> "Config":

        env_path = PROJECT_DIR / ".env"

        if not env_path.exists():
            raise ConfigurationError(
                f".env file not found: {env_path}"
            )

        load_dotenv(
            dotenv_path=env_path,
            override=True,
            encoding="utf-8-sig",
            interpolate=False,
        )

        def read_env(
            name: str,
            default: str = "",
        ) -> str:

            value = os.getenv(name, default).strip()

            if (
                len(value) >= 2
                and value[0] == value[-1]
                and value[0] in ('"', "'")
            ):
                value = value[1:-1]

            return value.strip()

        authorization = read_env(
            "TIRD_AUTHORIZATION"
        )

        cookie = read_env(
            "TIRD_COOKIE"
        )

        referer = read_env(
            "TIRD_REFERER",
            f"{BASE_URL}/Content",
        )

        if not authorization:
            raise ConfigurationError(
                "TIRD_AUTHORIZATION is missing from .env"
            )

        if not cookie:
            raise ConfigurationError(
                "TIRD_COOKIE is missing from .env"
            )

        if not referer.startswith(
            f"{BASE_URL}/Content"
        ):
            raise ConfigurationError(
                "TIRD_REFERER must point to the "
                "authorized TIRD Content page."
            )

        if "\n" in authorization or "\r" in authorization:
            raise ConfigurationError(
                "Invalid characters in TIRD_AUTHORIZATION."
            )

        if "\n" in cookie or "\r" in cookie:
            raise ConfigurationError(
                "Invalid characters in TIRD_COOKIE."
            )

        if "\n" in referer or "\r" in referer:
            raise ConfigurationError(
                "Invalid characters in TIRD_REFERER."
            )

        curl_executable = shutil.which(
            "curl.exe"
        )

        if not curl_executable:
            raise ConfigurationError(
                "curl.exe was not found. "
                "Install curl or ensure curl.exe is available "
                "in the Windows PATH."
            )

        if args.timeout <= 0:
            raise ConfigurationError(
                "--timeout must be greater than zero."
            )

        if args.delay < 0:
            raise ConfigurationError(
                "--delay cannot be negative."
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
            timeout=args.timeout,
            delay=args.delay,
            verbose=args.verbose,
            debug_response=args.debug_response,
            skip_content_page=args.skip_content_page,
        )


# ============================================================
# LOGGING
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
                "[dim]CURL ENGINE  /  PDF ARCHIVE  /  MANIFEST[/dim]",
                title=f"v{APP_VERSION}",
                border_style="cyan",
                padding=(1, 3),
            )
        )

        console.print()

    else:

        print()
        print("=" * 68)
        print(APP_NAME)
        print(f"VERSION {APP_VERSION}")
        print("HTTP ENGINE: CURL")
        print("=" * 68)


def show_summary(
    stats: dict[str, int],
    output: Path,
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
            print(f"{key:12}: {value}")

        print(f"Output      : {output}")
        print("=" * 68)


# ============================================================
# UTILITIES
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


def safe_filename(
    value: str,
    max_stem_length: int = 110,
) -> str:

    filename = str(value or "").strip()

    filename = re.sub(
        r'[<>:"/\\|?*\x00-\x1f]',
        "_",
        filename,
    )

    filename = re.sub(
        r"\s+",
        " ",
        filename,
    )

    filename = filename.strip(" .")

    if filename.lower().endswith(".pdf"):
        filename = filename[:-4]

    filename = filename[:max_stem_length].rstrip(" .")

    if not filename:
        filename = "document"

    if filename.upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
    }:
        filename = "_" + filename

    return filename + ".pdf"


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
    month_name = match.group(2)
    year = int(match.group(3))

    month = THAI_MONTHS.get(
        month_name
    )

    if month is None:
        return None

    if year >= 2400:
        year -= 543

    try:

        return datetime(
            year,
            month,
            day,
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


def service_id_of(
    service: dict,
) -> str:

    return str(
        service.get("Service_id")
        or service.get("Service_Id")
        or service.get("service_id")
        or ""
    ).strip()


def service_title_of(
    service: dict,
) -> str:

    return str(
        service.get("Service_name")
        or service.get("Service_Name")
        or service.get("Menu_Name")
        or "Untitled Newsletter"
    ).strip()


def service_subtitle_of(
    service: dict,
) -> str:

    return str(
        service.get("Service_Sub_Name")
        or service.get("Service_sub_name")
        or ""
    )


def valid_pdf(
    path: Path,
) -> bool:

    try:

        if not path.is_file():
            return False

        if path.stat().st_size < 5:
            return False

        with path.open("rb") as file:
            return file.read(5) == b"%PDF-"

    except OSError:
        return False


def decode_text(
    content: bytes,
) -> str:

    if not content:
        return ""

    try:
        return content.decode("utf-8-sig")

    except UnicodeDecodeError:
        return content.decode(
            "utf-8",
            errors="replace",
        )


def parse_json_bytes(
    content: bytes,
) -> Any:

    text = decode_text(
        content
    ).strip()

    if not text:
        raise APIResponseError(
            "Response body is empty."
        )

    if text.startswith(")]}'"):
        text = text.split(
            "\n",
            1,
        )[-1].strip()

    try:

        result = json.loads(
            text
        )

    except json.JSONDecodeError as exc:

        raise APIResponseError(
            "Invalid JSON response: "
            f"{exc.msg} at character {exc.pos}"
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

        return safe_filename(
            filename
        )

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
# CURL CONFIGURATION
# ============================================================

def curl_quote(
    value: str,
) -> str:

    """
    Quote a value for curl's configuration-file syntax.

    Credentials are sent via curl --config - over stdin.
    They do not appear in subprocess command-line arguments.
    """

    value = str(value)

    if "\x00" in value:
        raise ConfigurationError(
            "NUL characters are not allowed in curl options."
        )

    if "\r" in value or "\n" in value:
        raise ConfigurationError(
            "Newlines are not allowed in curl options."
        )

    escaped = (
        value
        .replace("\\", "\\\\")
        .replace('"', '\\"')
    )

    return f'"{escaped}"'


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

    if not path.exists():
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

        status = int(
            match.group(1)
        )

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

        if not self.body_path.exists():
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
    ):

        self.config = config
        self.curl = config.curl_executable

    # --------------------------------------------------------
    # BUILD REQUEST CONFIG
    # --------------------------------------------------------

    def build_config(
        self,
        endpoint: str,
        data: dict[str, str] | None,
        authorization: bool,
    ) -> bytes:

        url = BASE_URL + endpoint

        lines = [
            curl_option("url", url),
            curl_option("request", "POST"),
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

        # Match the successful Chrome cURL request:
        # POST GetService with Content-Length: 0.
        if data is None:

            headers.append((
                "content-length",
                "0",
            ))

        for key, value in headers:

            lines.append(
                curl_option(
                    "header",
                    f"{key}: {value}",
                )
            )

        # Equivalent to curl -b "Cookie: values".
        # The literal cookie string is preserved.
        lines.append(
            curl_option(
                "cookie",
                self.config.cookie,
            )
        )

        if data is not None:

            # curl --data-urlencode:
            # Content-Type: application/x-www-form-urlencoded
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
    # REDACT DIAGNOSTICS
    # --------------------------------------------------------

    def redact(
        self,
        text: str,
    ) -> str:

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
    # SAVE SAFE DEBUG METADATA
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

        name = response.endpoint.strip("/").replace(
            "/",
            "_",
        )

        date_suffix = datetime.now().strftime(
            "%Y%m%d_%H%M%S_%f"
        )

        path = (
            debug_dir
            / f"{name}_{date_suffix}.json"
        )

        body = response.read_bytes()

        metadata = {
            "timestamp": now_iso(),
            "endpoint": response.endpoint,
            "status_code": response.status_code,
            "http_version": response.http_version,
            "content_type": response.content_type,
            "content_encoding": response.headers.get(
                "content-encoding",
            ),
            "body_size": response.body_size,
            "body_sha256": hashlib.sha256(
                body
            ).hexdigest(),
            "server": response.headers.get(
                "server",
            ),
            "cf_ray": response.headers.get(
                "cf-ray",
            ),
            "response_preview": self.redact(
                decode_text(
                    body[:200]
                )
            ),
        }

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
    # HTTP REQUEST
    # --------------------------------------------------------

    @contextmanager
    def post(
        self,
        endpoint: str,
        data: dict[str, str] | None = None,
        *,
        authorization: bool = True,
    ) -> Iterator[CurlResponse]:

        with tempfile.TemporaryDirectory(
            prefix=".tird_http_",
            dir=self.config.output,
        ) as temp_directory:

            temporary = Path(temp_directory)

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
                "2",
                "--retry-delay",
                "1",
            ]

            if self.config.verbose:

                LOG.debug(
                    "CURL POST %s | form_fields=%s",
                    endpoint,
                    (
                        list(data.keys())
                        if data is not None
                        else []
                    ),
                )

            try:

                completed = subprocess.run(
                    command,
                    input=config_bytes,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
                    timeout=self.config.timeout + 15,
                    check=False,
                )

            except subprocess.TimeoutExpired as exc:

                raise CurlError(
                    f"CURL request timed out at {endpoint}"
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
                    f"CURL failed at {endpoint}. "
                    f"Exit code: {completed.returncode}. "
                    f"{stderr[:500]}"
                )

            status, headers = parse_header_file(
                header_path
            )

            metadata = {}

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

                LOG.debug(
                    "Server=%s | CF-Ray=%s",
                    response.headers.get(
                        "server",
                        "-",
                    ),
                    response.headers.get(
                        "cf-ray",
                        "-",
                    ),
                )

            if response.status_code in (
                301,
                302,
                303,
                307,
                308,
            ):

                self.save_debug(
                    response
                )

                raise AuthenticationError(
                    f"HTTP {response.status_code} at "
                    f"{endpoint}: unexpected redirect."
                )

            if response.status_code in (
                401,
                403,
            ):

                self.save_debug(
                    response
                )

                raise AuthenticationError(
                    f"HTTP {response.status_code} at "
                    f"{endpoint}. "
                    "The server rejected this request. "
                    "Verify the authorized browser session "
                    "and its current credentials."
                )

            if response.status_code < 200 or response.status_code >= 300:

                self.save_debug(
                    response
                )

                raise CurlError(
                    f"HTTP {response.status_code} at "
                    f"{endpoint}"
                )

            if response.body_size == 0:

                self.save_debug(
                    response
                )

                raise APIResponseError(
                    f"{endpoint} returned HTTP "
                    f"{response.status_code} "
                    "with an empty response body."
                )

            yield response


# ============================================================
# TIRD API CLIENT
# ============================================================

class TIRDClient:

    def __init__(
        self,
        config: Config,
    ):

        self.config = config

        self.transport = CurlTransport(
            config
        )

    # --------------------------------------------------------
    # JSON
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

                result = response.json()

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

            return result

    # --------------------------------------------------------
    # GET SERVICE CATALOG
    # --------------------------------------------------------

    def get_services(self) -> list[dict]:

        LOG.info(
            "Fetching TIRD newsletter catalog"
        )

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

                value = result.get(
                    key
                )

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
                "Unexpected JSON structure "
                "from /Content/GetService."
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

            accessible.append(
                service
            )

        LOG.info(
            "Catalog records: %s",
            len(services),
        )

        LOG.info(
            "Accessible documents: %s",
            len(accessible),
        )

        return accessible

    # --------------------------------------------------------
    # GET SERVICE DETAILS
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

            for key in (
                "data",
                "Data",
                "result",
                "Result",
            ):

                nested = detail.get(
                    key
                )

                if isinstance(nested, dict):

                    service_key = (
                        nested.get("workspace_id")
                        or nested.get("Workspace_Id")
                    )

                    if service_key:
                        break

        if not service_key:

            raise APIResponseError(
                "workspace_id is missing "
                f"for Service_Id={service_id}"
            )

        return str(
            service_key
        )

    # --------------------------------------------------------
    # SHOW CONTENT
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
    # DOWNLOAD PDF
    # --------------------------------------------------------

    def download_pdf(
        self,
        service_id: str,
        service_key: str,
        output_dir: Path,
        preferred_filename: str,
    ) -> tuple[Path, int, str]:

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
                    "ListFiles returned a response "
                    "that is not a valid PDF. "
                    f"HTTP={response.status_code}; "
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

            stem = Path(
                filename
            ).stem

            final_filename = safe_filename(
                f"{stem[:105]}_{unique_suffix}.pdf"
            )

            destination = (
                output_dir / final_filename
            )

            source_size = (
                response.body_size
            )

            expected_size = response.headers.get(
                "content-length",
                "",
            )

            if expected_size:

                try:

                    expected = int(
                        expected_size
                    )

                except ValueError:

                    expected = None

                if (
                    expected is not None
                    and expected != source_size
                ):

                    raise PDFDownloadError(
                        "Incomplete PDF response. "
                        f"Expected {expected} bytes, "
                        f"received {source_size} bytes."
                    )

            checksum = sha256_file(
                response.body_path
            )

            temporary_destination = (
                output_dir
                / f".{unique_suffix}.part"
            )

            try:

                # Copy to the destination filesystem first.
                # Then atomically replace the final file.
                shutil.copyfile(
                    response.body_path,
                    temporary_destination,
                )

                if not valid_pdf(
                    temporary_destination
                ):

                    raise PDFDownloadError(
                        "Temporary PDF failed validation."
                    )

                if (
                    temporary_destination.stat().st_size
                    != source_size
                ):

                    raise PDFDownloadError(
                        "PDF copy size mismatch."
                    )

                os.replace(
                    temporary_destination,
                    destination,
                )

            finally:

                temporary_destination.unlink(
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

    def __init__(
        self,
        output: Path,
    ):

        self.output = output.resolve()

        self.path = (
            self.output / "manifest.csv"
        )

        self.records: dict[str, dict] = {}

        self.load()

    def load(self) -> None:

        if not self.path.exists():
            return

        with self.path.open(
            "r",
            encoding="utf-8-sig",
            newline="",
        ) as file:

            reader = csv.DictReader(
                file
            )

            for row in reader:

                service_id = row.get(
                    "service_id",
                    "",
                )

                if service_id:

                    self.records[service_id] = row

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

        record = self.get(
            service_id
        )

        if not record:
            return False

        if record.get(
            "status"
        ) != "success":
            return False

        relative_path = record.get(
            "file_path",
            "",
        )

        if not relative_path:
            return False

        path = (
            self.output / relative_path
        ).resolve()

        if not path.is_relative_to(
            self.output
        ):
            return False

        if not valid_pdf(
            path
        ):
            return False

        expected_size = (
            record.get("file_size")
            or record.get("size_bytes")
        )

        if expected_size:

            try:

                if path.stat().st_size != int(
                    expected_size
                ):
                    return False

            except ValueError:
                return False

        return True

    def upsert(
        self,
        record: dict,
    ) -> None:

        service_id = record[
            "service_id"
        ]

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

        temporary = self.path.with_suffix(
            ".csv.tmp"
        )

        with temporary.open(
            "w",
            encoding="utf-8-sig",
            newline="",
        ) as file:

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


# ============================================================
# COLLECTOR
# ============================================================

class TIRDCollector:

    def __init__(
        self,
        client: TIRDClient,
        config: Config,
        force: bool = False,
    ):

        self.client = client
        self.config = config
        self.force = force

        self.manifest = Manifest(
            config.output
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
                "Authentication succeeded, "
                "but no accessible documents were returned."
            )

            return True

        sample = services[0]

        service_id = service_id_of(
            sample
        )

        LOG.info(
            "Testing GetServiceById"
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
            print("=" * 55)
            print("API CONNECTION SUCCESSFUL")
            print(f"Catalog: {len(services)} documents")
            print("GetService: PASS")
            print("GetServiceById: PASS")
            print("PDF download: NOT TESTED")
            print("=" * 55)

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

            for index, service in enumerate(
                services,
                1,
            ):

                subtitle = service_subtitle_of(
                    service
                )

                published = parse_thai_date(
                    subtitle
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
                )

            console.print(table)

            console.print(
                f"\n[dim]Total documents: "
                f"{len(services)}[/dim]"
            )

        else:

            print()
            print("=" * 68)

            for index, service in enumerate(
                services,
                1,
            ):

                print(
                    f"{index:03d} | "
                    f"{service_title_of(service)}"
                )

            print("=" * 68)

            print(
                f"Total: {len(services)}"
            )

    # --------------------------------------------------------
    # DOWNLOAD ONE
    # --------------------------------------------------------

    def process_service(
        self,
        service: dict,
    ) -> None:

        service_id = service_id_of(
            service
        )

        title = service_title_of(
            service
        )

        subtitle = service_subtitle_of(
            service
        )

        edition = parse_edition(
            subtitle
        )

        published = parse_thai_date(
            subtitle
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
                title,
            )

            return

        if published:

            output_dir = (
                self.config.output
                / str(published.year)
                / f"{published.month:02d}"
            )

            published_date = (
                published.strftime(
                    "%Y-%m-%d"
                )
            )

            year = str(
                published.year
            )

        else:

            output_dir = (
                self.config.output
                / "unknown_date"
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

            self.manifest.upsert(
                record
            )

            self.stats["downloaded"] += 1

            LOG.info(
                "SUCCESS | %.2f MB | %s",
                size / (1024 * 1024),
                destination.name,
            )

        except AuthenticationError:
            raise

        except (
            CurlError,
            APIResponseError,
            PDFDownloadError,
            OSError,
            ValueError,
        ) as exc:

            record["error"] = str(
                exc
            )

            record["downloaded_at"] = (
                now_iso()
            )

            # Preserve the last successful record if
            # a forced redownload was unsuccessful.
            if not self.manifest.completed(
                service_id
            ):

                self.manifest.upsert(
                    record
                )

            self.stats["failed"] += 1

            LOG.error(
                "FAILED | %s | %s",
                title,
                exc,
            )

    # --------------------------------------------------------
    # DOWNLOAD SELECTED
    # --------------------------------------------------------

    def collect(
        self,
        services: list[dict],
    ) -> int:

        self.stats["total"] = len(
            services
        )

        LOG.info(
            "Starting collection: %s documents",
            len(services),
        )

        if RICH_AVAILABLE:

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

                for index, service in enumerate(
                    services
                ):

                    progress.update(
                        task,
                        description=(
                            f"Processing "
                            f"{index + 1}/{len(services)}"
                        ),
                    )

                    try:

                        self.process_service(
                            service
                        )

                    finally:

                        progress.advance(
                            task
                        )

                    if (
                        index < len(services) - 1
                        and self.config.delay > 0
                    ):

                        time.sleep(
                            self.config.delay
                        )

        else:

            for index, service in enumerate(
                services,
                1,
            ):

                LOG.info(
                    "[%s/%s] %s",
                    index,
                    len(services),
                    service_title_of(service),
                )

                self.process_service(
                    service
                )

                if (
                    index < len(services)
                    and self.config.delay > 0
                ):

                    time.sleep(
                        self.config.delay
                    )

        show_summary(
            self.stats,
            self.config.output,
        )

        return (
            0 if self.stats["failed"] == 0
            else 1
        )


# ============================================================
# CLI ARGUMENTS
# ============================================================

def parse_args() -> argparse.Namespace:

    parser = argparse.ArgumentParser(
        prog="tird_collector.py",
        description=APP_NAME,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument("--ui", action="store_true", help="Launch Textual terminal UI")

    parser.add_argument("--headless", action="store_true", help="Download all without terminal UI")

    parser.add_argument(
        "--test-only",
        action="store_true",
        help="Test catalog and service-detail API access",
    )

    parser.add_argument(
        "--list",
        action="store_true",
        help="List available newsletter documents",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of documents to process",
    )

    parser.add_argument(
        "--year",
        type=int,
        default=None,
        help="Filter by publication year (2026 or 2569)",
    )

    parser.add_argument(
        "--service-id",
        type=str,
        default=None,
        help="Process a specific service ID",
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Destination directory",
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="Redownload previously completed documents",
    )

    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY,
        help="Delay between document downloads",
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help="HTTP timeout in seconds",
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show safe HTTP diagnostics",
    )

    parser.add_argument(
        "--debug-response",
        action="store_true",
        help="Save non-sensitive response metadata on failures",
    )

    parser.add_argument(
        "--skip-content-page",
        action="store_true",
        help="Skip ShowContentFiles before downloading",
    )

    return parser.parse_args()


# ============================================================
# MAIN
# ============================================================

def main() -> int:

    args = parse_args()

    # Keep --test-only and the original CLI flags fully compatible.
    # The no-flag command opens the interactive Textual dashboard.
    cli_selected = (
        args.headless or args.test_only or args.list
        or args.limit is not None or args.year is not None
        or args.service_id is not None
    )
    if args.ui or not cli_selected:
        try:
            from tird_ui import run_app
        except ImportError as exc:
            print(
                "Terminal UI requires: python -m pip install -r requirements.txt",
                file=sys.stderr,
            )
            return 2
        return run_app(args, sys.modules[__name__])

    setup_logging(
        args.verbose
    )

    show_header()

    if args.limit is not None and args.limit <= 0:

        LOG.error(
            "--limit must be greater than zero."
        )

        return 2

    try:

        config = Config.load(
            args
        )

    except (
        ConfigurationError,
        OSError,
    ) as exc:

        LOG.error(
            "Configuration error: %s",
            exc,
        )

        return 2

    client = TIRDClient(
        config
    )

    collector = TIRDCollector(
        client=client,
        config=config,
        force=args.force,
    )

    try:

        # ----------------------------------------------------
        # TEST
        # ----------------------------------------------------

        if args.test_only:

            return (
                0 if collector.test_connection()
                else 1
            )

        # ----------------------------------------------------
        # CATALOG
        # ----------------------------------------------------

        services = client.get_services()

        # ----------------------------------------------------
        # SERVICE ID FILTER
        # ----------------------------------------------------

        if args.service_id:

            services = [
                service
                for service in services
                if service_id_of(service)
                == args.service_id
            ]

        # ----------------------------------------------------
        # YEAR FILTER
        # ----------------------------------------------------

        if args.year is not None:

            target_year = (
                args.year - 543
                if args.year >= 2400
                else args.year
            )

            filtered = []

            for service in services:

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

            services = filtered

        # ----------------------------------------------------
        # LIMIT
        # ----------------------------------------------------

        if args.limit is not None:

            services = services[
                :args.limit
            ]

        # ----------------------------------------------------
        # LIST
        # ----------------------------------------------------

        if args.list:

            collector.list_services(
                services
            )

            return 0

        # ----------------------------------------------------
        # EMPTY
        # ----------------------------------------------------

        if not services:

            LOG.warning(
                "No documents matched the selected filters."
            )

            return 0

        # ----------------------------------------------------
        # COLLECT
        # ----------------------------------------------------

        return collector.collect(
            services
        )

    except AuthenticationError as exc:

        LOG.error(
            "AUTHENTICATION ERROR: %s",
            exc,
        )

        LOG.error(
            "Refresh the authorized browser session "
            "and verify credentials stored in .env."
        )

        return 1

    except APIResponseError as exc:

        LOG.error(
            "API RESPONSE ERROR: %s",
            exc,
        )

        LOG.error(
            "Run with --test-only --verbose --debug-response "
            "to inspect safe response diagnostics."
        )

        return 1

    except CurlError as exc:

        LOG.error(
            "CURL ERROR: %s",
            exc,
        )

        return 1

    except PDFDownloadError as exc:

        LOG.error(
            "PDF ERROR: %s",
            exc,
        )

        return 1

    except KeyboardInterrupt:

        LOG.warning(
            "Interrupted by user."
        )

        show_summary(
            collector.stats,
            config.output,
        )

        return 130

    except (
        TIRDError,
        OSError,
        ValueError,
    ) as exc:

        LOG.error(
            "Collector stopped: %s",
            exc,
        )

        return 1


if __name__ == "__main__":
    sys.exit(main())
