
"""
TIRD Newsletter Collector
=========================

Install:
    python -m pip install requests python-dotenv

Create a .env file in the same directory:

    TIRD_COOKIE="PASTE_YOUR_BROWSER_COOKIE_HERE"
    TIRD_AUTHORIZATION="PASTE_YOUR_AUTHORIZATION_HERE"

TIRD_AUTHORIZATION is optional if the session cookie is sufficient.
Use credentials from your own authorized TIRD session.

Usage:
    python tird_collector.py
    python tird_collector.py --test-only
    python tird_collector.py --list
    python tird_collector.py --limit 5
    python tird_collector.py --year 2026
    python tird_collector.py --service-id 865220ac-454f-4ca1-ac20-cd059a75a7ed
    python tird_collector.py --force

Output:
    TIRD_Newsletter/
        2026/
            10/
                *.pdf
        manifest.csv
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import time

from datetime import datetime
from pathlib import Path
from urllib.parse import unquote_plus

import requests

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv


# ============================================================
# CONFIGURATION
# ============================================================

BASE_URL = "https://tirdmember.tird.insure"

ENDPOINTS = {
    "services": "/Content/GetService",
    "service_detail": "/Content/GetServiceById",
    "content": "/Content/ShowContentFiles",
    "download": "/Content/ListFiles",
}

DEFAULT_OUTPUT = Path("TIRD_Newsletter")
DEFAULT_TIMEOUT = 45
DEFAULT_DELAY = 1.0

MANIFEST_FIELDS = [
    "service_id",
    "service_key",
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


# ============================================================
# UTILITIES
# ============================================================

def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )

    logging.getLogger("urllib3").setLevel(logging.WARNING)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def safe_filename(value: str, max_length: int = 140) -> str:
    value = unquote_plus(str(value or "").strip())

    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", " ", value)
    value = value.strip(" .")

    if not value:
        value = "document"

    stem = Path(value).stem
    suffix = Path(value).suffix

    if suffix.lower() != ".pdf":
        stem = value
        suffix = ".pdf"

    reserved = {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }

    if stem.upper() in reserved:
        stem = f"_{stem}"

    stem = stem[: max_length - len(suffix)].rstrip(" ._")

    return f"{stem or 'document'}{suffix}"


def parse_published_date(text: str) -> datetime | None:
    if not text:
        return None

    pattern = (
        r"(\d{1,2})\s+"
        r"([ก-๙]+)\s+"
        r"(\d{4})"
    )

    match = re.search(pattern, text)

    if not match:
        return None

    day = int(match.group(1))
    month_name = match.group(2)
    year = int(match.group(3))

    month = THAI_MONTHS.get(month_name)

    if not month:
        return None

    if year >= 2400:
        year -= 543

    try:
        return datetime(year, month, day)
    except ValueError:
        return None


def parse_edition(text: str) -> str:
    match = re.search(r"ฉบับที่\s*(\d+)", text or "")

    return match.group(1) if match else ""


def extract_filename(content_disposition: str) -> str | None:
    if not content_disposition:
        return None

    match = re.search(
        r"filename\*\s*=\s*(?:UTF-8'')?([^;]+)",
        content_disposition,
        flags=re.IGNORECASE,
    )

    if match:
        return safe_filename(match.group(1).strip().strip('"'))

    match = re.search(
        r"filename\s*=\s*\"?([^\";]+)",
        content_disposition,
        flags=re.IGNORECASE,
    )

    if match:
        return safe_filename(match.group(1))

    return None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def is_valid_pdf(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size < 5:
            return False

        with path.open("rb") as file:
            return file.read(5) == b"%PDF-"

    except OSError:
        return False


# ============================================================
# MANIFEST
# ============================================================

class Manifest:
    def __init__(self, path: Path):
        self.path = path
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

            reader = csv.DictReader(file)

            for row in reader:
                service_id = row.get("service_id")

                if service_id:
                    self.records[service_id] = row

    def get(self, service_id: str) -> dict | None:
        return self.records.get(service_id)

    def upsert(self, record: dict) -> None:
        service_id = record["service_id"]

        self.records[service_id] = {
            field: record.get(field, "")
            for field in MANIFEST_FIELDS
        }

        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        temporary = self.path.with_suffix(".csv.tmp")

        with temporary.open(
            "w",
            encoding="utf-8-sig",
            newline="",
        ) as file:

            writer = csv.DictWriter(
                file,
                fieldnames=MANIFEST_FIELDS,
            )

            writer.writeheader()

            writer.writerows(self.records.values())

        os.replace(temporary, self.path)

    def completed(
        self,
        service_id: str,
        output_root: Path,
    ) -> bool:

        record = self.get(service_id)

        if not record:
            return False

        if record.get("status") != "success":
            return False

        relative_path = record.get("file_path")

        if not relative_path:
            return False

        root = output_root.resolve()
        path = (root / relative_path).resolve()

        if not path.is_relative_to(root):
            return False

        if not is_valid_pdf(path):
            return False

        expected_size = record.get("file_size")

        if expected_size:
            try:
                if path.stat().st_size != int(expected_size):
                    return False
            except ValueError:
                return False

        return True


# ============================================================
# HTTP CLIENT
# ============================================================

class TIRDClient:
    def __init__(
        self,
        cookie: str,
        authorization: str = "",
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self.timeout = timeout

        self.session = requests.Session()

        retry = Retry(
            total=3,
            connect=3,
            read=2,
            status=3,
            backoff_factor=1.0,
            status_forcelist=[
                429,
                500,
                502,
                503,
                504,
            ],
            allowed_methods=frozenset(["POST"]),
            respect_retry_after_header=True,
        )

        adapter = HTTPAdapter(
            max_retries=retry,
            pool_connections=5,
            pool_maxsize=5,
        )

        self.session.mount(
            "https://",
            adapter,
        )

        self.session.headers.update({
            "Accept": "*/*",
            "Accept-Language": "th-TH,th;q=0.9",
            "User-Agent": (
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/157.0.0.0 Safari/537.36"
            ),
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/Content",
            "X-Requested-With": "XMLHttpRequest",
        })

        if cookie:
            self.session.headers["Cookie"] = cookie

        if authorization:
            self.session.headers["Authorization"] = authorization

    def close(self) -> None:
        self.session.close()

    def post(
        self,
        endpoint: str,
        data: dict | None = None,
        stream: bool = False,
    ) -> requests.Response:

        url = BASE_URL + endpoint

        response = self.session.post(
            url,
            data=data,
            timeout=self.timeout,
            stream=stream,
            allow_redirects=False,
        )

        if response.status_code in (301, 302, 303, 307, 308):
            response.close()

            raise RuntimeError(
                "TIRD redirected the request. "
                "The login session may have expired."
            )

        if response.status_code in (401, 403):
            response.close()

            raise PermissionError(
                f"HTTP {response.status_code}: "
                "Authentication failed or access is not permitted. "
                "Check TIRD_COOKIE and TIRD_AUTHORIZATION."
            )

        response.raise_for_status()

        return response

    def post_json(
        self,
        endpoint: str,
        data: dict | None = None,
    ):
        with self.post(endpoint, data=data) as response:
            try:
                result = response.json()
            except ValueError as exc:
                raise RuntimeError(
                    f"Expected JSON from {endpoint}, "
                    "but received another response format. "
                    "Check the login session."
                ) from exc

        if isinstance(result, str):
            try:
                result = json.loads(result)
            except ValueError:
                pass

        return result

    # --------------------------------------------------------
    # GET ALL NEWSLETTERS
    # --------------------------------------------------------

    def get_services(self) -> list[dict]:
        LOG.info("Fetching TIRD newsletter catalog")

        result = self.post_json(
            ENDPOINTS["services"],
        )

        if isinstance(result, list):
            services = result

        elif isinstance(result, dict):
            services = None

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

            if services is None:
                raise RuntimeError(
                    "Unexpected GetService response structure."
                )
        else:
            raise RuntimeError(
                "GetService did not return a service list."
            )

        services = [
            item
            for item in services
            if isinstance(item, dict)
            and item.get("Service_id")
            and item.get("Permission_used", "Y") == "Y"
            and item.get("IsActive", True)
        ]

        LOG.info(
            "Found %s accessible newsletter records",
            len(services),
        )

        return services

    # --------------------------------------------------------
    # RESOLVE SERVICE KEY
    # --------------------------------------------------------

    def get_service_key(self, service_id: str) -> str:
        result = self.post_json(
            ENDPOINTS["service_detail"],
            data={
                "Service_Id": service_id,
            },
        )

        if not isinstance(result, dict):
            raise RuntimeError(
                f"Unexpected detail response for {service_id}"
            )

        service_key = (
            result.get("workspace_id")
            or result.get("Workspace_Id")
            or result.get("Workspace_id")
        )

        if service_key:
            return str(service_key)

        raise RuntimeError(
            f"workspace_id not found for service {service_id}"
        )

    # --------------------------------------------------------
    # DOWNLOAD PDF
    # --------------------------------------------------------

    def download_pdf(
        self,
        service_id: str,
        service_key: str,
        output_dir: Path,
        preferred_name: str,
    ) -> tuple[Path, int, str]:

        payload = {
            "Form": "Content",
            "Service_Id": service_id,
            "Service_Key": service_key,
        }

        output_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        temp_path: Path | None = None

        try:
            with self.post(
                ENDPOINTS["download"],
                data=payload,
                stream=True,
            ) as response:

                content_type = response.headers.get(
                    "Content-Type",
                    "",
                ).lower()

                if (
                    "application/pdf" not in content_type
                    and "application/octet-stream" not in content_type
                ):
                    raise RuntimeError(
                        "Expected PDF response, "
                        f"received Content-Type: {content_type}"
                    )

                filename = extract_filename(
                    response.headers.get(
                        "Content-Disposition",
                        "",
                    )
                )

                if not filename:
                    filename = preferred_name

                filename = safe_filename(filename)

                unique_suffix = service_id.replace("-", "")[:12]

                stem = Path(filename).stem

                filename = safe_filename(
                    f"{stem}_{unique_suffix}.pdf"
                )

                destination = output_dir / filename

                digest = hashlib.sha256()
                total_bytes = 0
                first_chunk = True

                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    prefix=".tird_",
                    suffix=".part",
                    dir=output_dir,
                    delete=False,
                ) as file:

                    temp_path = Path(file.name)

                    for chunk in response.iter_content(
                        chunk_size=128 * 1024
                    ):
                        if not chunk:
                            continue

                        if first_chunk:
                            first_chunk = False

                            if not chunk.startswith(b"%PDF-"):
                                raise RuntimeError(
                                    "Response does not contain "
                                    "a valid PDF signature."
                                )

                        file.write(chunk)
                        digest.update(chunk)

                        total_bytes += len(chunk)

                if total_bytes == 0:
                    raise RuntimeError(
                        "Downloaded PDF is empty."
                    )

                expected_length = response.headers.get(
                    "Content-Length"
                )

                if expected_length:
                    if total_bytes != int(expected_length):
                        raise RuntimeError(
                            "Incomplete PDF download: "
                            f"expected {expected_length} bytes, "
                            f"received {total_bytes} bytes."
                        )

                if not is_valid_pdf(temp_path):
                    raise RuntimeError(
                        "Downloaded content failed PDF validation."
                    )

                os.replace(
                    temp_path,
                    destination,
                )

                temp_path = None

                return (
                    destination,
                    total_bytes,
                    digest.hexdigest(),
                )

        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink(missing_ok=True)


# ============================================================
# COLLECTOR
# ============================================================

class TIRDCollector:
    def __init__(
        self,
        client: TIRDClient,
        output: Path,
        delay: float = DEFAULT_DELAY,
        force: bool = False,
    ):
        self.client = client
        self.output = output.resolve()
        self.delay = max(delay, 0)
        self.force = force

        self.output.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.manifest = Manifest(
            self.output / "manifest.csv"
        )

        self.stats = {
            "total": 0,
            "success": 0,
            "skipped": 0,
            "failed": 0,
        }

    def service_metadata(self, service: dict) -> dict:
        service_id = str(
            service.get("Service_id") or ""
        )

        title = (
            service.get("Service_name")
            or service.get("Menu_Name")
            or "Untitled Newsletter"
        )

        subtitle = (
            service.get("Service_Sub_Name")
            or ""
        )

        published = parse_published_date(subtitle)

        return {
            "service_id": service_id,
            "title": title.strip(),
            "edition": parse_edition(subtitle),
            "published": published,
            "subtitle": subtitle,
        }

    def get_output_directory(
        self,
        published: datetime | None,
    ) -> Path:

        if published:
            return (
                self.output
                / str(published.year)
                / f"{published.month:02d}"
            )

        return self.output / "unknown_date"

    def process_service(
        self,
        service: dict,
        index: int,
        total: int,
    ) -> None:

        metadata = self.service_metadata(service)

        service_id = metadata["service_id"]
        title = metadata["title"]
        published = metadata["published"]

        LOG.info(
            "[%s/%s] %s",
            index,
            total,
            title,
        )

        if (
            not self.force
            and self.manifest.completed(
                service_id,
                self.output,
            )
        ):
            self.stats["skipped"] += 1

            LOG.info("SKIP | Already downloaded")
            return

        record = {
            "service_id": service_id,
            "service_key": "",
            "title": title,
            "edition": metadata["edition"],
            "published_date": (
                published.strftime("%Y-%m-%d")
                if published else ""
            ),
            "year": (
                str(published.year)
                if published else ""
            ),
            "filename": "",
            "file_path": "",
            "file_size": "",
            "sha256": "",
            "status": "",
            "downloaded_at": now_iso(),
            "error": "",
        }

        try:
            service_key = self.client.get_service_key(
                service_id
            )

            record["service_key"] = service_key

            output_dir = self.get_output_directory(
                published
            )

            preferred_name = safe_filename(
                f"{metadata['edition'] + '_' if metadata['edition'] else ''}"
                f"{title}.pdf"
            )

            path, size, checksum = self.client.download_pdf(
                service_id=service_id,
                service_key=service_key,
                output_dir=output_dir,
                preferred_name=preferred_name,
            )

            record.update({
                "filename": path.name,
                "file_path": path.relative_to(
                    self.output
                ).as_posix(),
                "file_size": size,
                "sha256": checksum,
                "status": "success",
                "downloaded_at": now_iso(),
            })

            self.stats["success"] += 1

            LOG.info(
                "SUCCESS | %.2f MB | %s",
                size / (1024 * 1024),
                path.name,
            )

        except (
            requests.RequestException,
            OSError,
            RuntimeError,
            PermissionError,
            ValueError,
        ) as exc:

            record["status"] = "failed"
            record["error"] = str(exc)
            record["downloaded_at"] = now_iso()

            self.stats["failed"] += 1

            LOG.error(
                "FAILED | %s | %s",
                service_id,
                exc,
            )

        finally:
            self.manifest.upsert(record)

    def list_services(
        self,
        services: list[dict],
    ) -> None:

        print()
        print("=" * 100)
        print("TIRD NEWSLETTER CATALOG")
        print("=" * 100)

        for index, service in enumerate(
            services,
            start=1,
        ):
            metadata = self.service_metadata(service)

            published = metadata["published"]

            date_text = (
                published.strftime("%Y-%m-%d")
                if published else "Unknown"
            )

            print(
                f"\n[{index:03d}] {metadata['title']}"
            )

            print(
                f"      Date       : {date_text}"
            )

            print(
                f"      Edition    : {metadata['edition'] or '-'}"
            )

            print(
                f"      Service ID : {metadata['service_id']}"
            )

        print()
        print(f"TOTAL: {len(services)}")
        print("=" * 100)

    def test_connection(
        self,
        services: list[dict],
    ) -> bool:

        print()
        print("=" * 70)
        print("TIRD CONNECTION TEST")
        print("=" * 70)

        print(
            f"GetService       : OK ({len(services)} records)"
        )

        if not services:
            print("GetServiceById   : SKIPPED")
            print("Result           : No accessible newsletters")
            return False

        first = services[0]

        service_id = str(
            first["Service_id"]
        )

        try:
            service_key = self.client.get_service_key(
                service_id
            )

            print("GetServiceById   : OK")
            print("Service ID       :", service_id)
            print("Workspace ID     : Resolved")
            print("Download         : Not executed")
            print("Result           : PASS")
            print("=" * 70)

            return bool(service_key)

        except Exception as exc:
            print("GetServiceById   : FAILED")
            print("Error            :", exc)
            print("Result           : FAIL")
            print("=" * 70)

            return False

    def collect(
        self,
        services: list[dict],
    ) -> int:

        self.stats["total"] = len(services)

        LOG.info(
            "Starting collection: %s newsletters",
            len(services),
        )

        for index, service in enumerate(
            services,
            start=1,
        ):
            self.process_service(
                service,
                index,
                len(services),
            )

            if index < len(services) and self.delay:
                time.sleep(self.delay)

        self.print_summary()

        return (
            0 if self.stats["failed"] == 0 else 1
        )

    def print_summary(self) -> None:
        print()
        print("=" * 70)
        print("TIRD NEWSLETTER COLLECTION SUMMARY")
        print("=" * 70)
        print(f"Total      : {self.stats['total']}")
        print(f"Downloaded : {self.stats['success']}")
        print(f"Skipped    : {self.stats['skipped']}")
        print(f"Failed     : {self.stats['failed']}")
        print("-" * 70)
        print(f"Output     : {self.output}")
        print(f"Manifest   : {self.manifest.path}")
        print("=" * 70)


# ============================================================
# COMMAND LINE INTERFACE
# ============================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tird_collector.py",
        description=(
            "Download accessible TIRD Newsletter PDFs "
            "using authenticated HTTP requests."
        ),
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Output directory",
    )

    parser.add_argument(
        "--test-only",
        action="store_true",
        help="Test API access without downloading PDFs",
    )

    parser.add_argument(
        "--list",
        action="store_true",
        help="List all accessible newsletters",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of newsletters to process",
    )

    parser.add_argument(
        "--year",
        type=int,
        default=None,
        help="Filter by Gregorian publication year",
    )

    parser.add_argument(
        "--service-id",
        type=str,
        default=None,
        help="Download a specific newsletter",
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="Redownload files even if already present",
    )

    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY,
        help="Delay between requests in seconds",
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help="HTTP request timeout in seconds",
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable detailed logging",
    )

    return parser.parse_args()


# ============================================================
# MAIN
# ============================================================

def main() -> int:
    load_dotenv(
        dotenv_path=Path(__file__).resolve().parent / ".env",
        override=False,
    )

    args = parse_args()

    setup_logging(args.verbose)

    cookie = os.getenv(
        "TIRD_COOKIE",
        "",
    ).strip().strip('"')

    authorization = os.getenv(
        "TIRD_AUTHORIZATION",
        "",
    ).strip().strip('"')

    if not cookie and not authorization:
        LOG.error(
            "Missing authentication configuration."
        )

        LOG.error(
            "Set TIRD_COOKIE and/or "
            "TIRD_AUTHORIZATION in your .env file."
        )

        return 2

    if args.limit is not None and args.limit < 1:
        LOG.error("--limit must be at least 1")
        return 2

    if args.timeout < 1:
        LOG.error("--timeout must be positive")
        return 2

    client = TIRDClient(
        cookie=cookie,
        authorization=authorization,
        timeout=args.timeout,
    )

    collector = TIRDCollector(
        client=client,
        output=args.output,
        delay=args.delay,
        force=args.force,
    )

    try:
        services = client.get_services()

        if args.service_id:
            services = [
                service
                for service in services
                if str(service.get("Service_id"))
                == args.service_id
            ]

        if args.year:
            filtered = []

            for service in services:
                published = parse_published_date(
                    service.get("Service_Sub_Name") or ""
                )

                if published and published.year == args.year:
                    filtered.append(service)

            services = filtered

        if args.list:
            collector.list_services(services)
            return 0

        if args.test_only:
            return (
                0 if collector.test_connection(services)
                else 1
            )

        if args.limit is not None:
            services = services[:args.limit]

        if not services:
            LOG.warning(
                "No newsletters matched the selected filters."
            )
            return 0

        return collector.collect(services)

    except KeyboardInterrupt:
        LOG.warning(
            "Collection interrupted by user."
        )

        collector.print_summary()
        return 130

    except (
        requests.RequestException,
        RuntimeError,
        PermissionError,
        OSError,
        ValueError,
    ) as exc:
        LOG.error("Collector stopped: %s", exc)
        return 1

    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
