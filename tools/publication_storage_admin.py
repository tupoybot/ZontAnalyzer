"""Private, bounded primitives for publication storage maintenance.

These tools use GET followed by If-None-Match PUT. The provider's CopyObject
source condition does not establish an atomic destination-create condition;
without that guarantee a server-side copy could overwrite an existing report.
No ZONT, model, report rendering, or canonical-report APIs are opened here.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from uuid import uuid4

from zont_analyzer.cloud.object_storage import StorageError, valid_key
from zont_analyzer.cloud.publication import CloudPublication

LEGACY_KEY = re.compile(r"publication/(?:reports/[a-f0-9]{64}\.(?:html|json)|"
                        r"manifests/[a-f0-9]{64}\.json|latest/[a-f0-9]{64}\.html)\Z")
REPORT_KEY = re.compile(r"(daily|weekly|monthly|seasonal)/(\d{4}-\d{2}-\d{2})\.(html|json)\Z")
STABLE_META_KEYS = {"site-index.json", "reports.json", "latest.html"}
LEASE_SECONDS = 120


class MaintenanceError(StorageError):
    """Messages must not contain keys, bucket names, response bodies or paths."""


def digest(body):
    return hashlib.sha256(body).hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def fingerprint(value):
    return digest(encoded(value))


def stable_report(key, suffix=None):
    if not isinstance(key, str):
        return False
    match = REPORT_KEY.fullmatch(key)
    if match is None or (suffix and match[3] != suffix):
        return False
    try:
        date.fromisoformat(match[2])
    except ValueError:
        return False
    return True


def allowed_key(key):
    return isinstance(key, str) and (key in STABLE_META_KEYS or stable_report(key) or bool(LEGACY_KEY.fullmatch(key)))


def parse_json(body):
    try:
        return json.loads(body)
    except (ValueError, UnicodeError):
        raise MaintenanceError("invalid publication JSON") from None


def validate_manifest(body, *, stable=False):
    value = parse_json(body)
    if (not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1
            or not isinstance(value.get("updated_at"), str) or not isinstance(value.get("reports"), list)):
        raise MaintenanceError("invalid publication manifest")
    seen = set()
    for entry in value["reports"]:
        if not isinstance(entry, dict):
            raise MaintenanceError("invalid publication entry")
        href = entry.get("href")
        if not stable_report(href, "html") or href in seen:
            raise MaintenanceError("invalid or duplicate report path")
        seen.add(href)
        if entry.get("kind") != href.split("/")[0] or entry.get("start") != href.split("/")[1][:-5]:
            raise MaintenanceError("report path does not match period")
        for field, suffix in (("html_key", "html"), ("json_key", "json")):
            key = entry.get(field)
            expected = href if suffix == "html" else href[:-5] + ".json"
            if not isinstance(key, str) or not valid_key(key):
                raise MaintenanceError("invalid report object key")
            if stable and key != expected:
                raise MaintenanceError("report does not use stable storage")
            if not stable and not LEGACY_KEY.fullmatch(key):
                raise MaintenanceError("report does not use legacy storage")
            if not key.endswith("." + suffix) or (not stable and not key.startswith("publication/reports/")):
                raise MaintenanceError("report object type mismatch")
    return value


class PrivateArtifacts:
    """Owner-only artifacts outside the checkout; never follow artifact symlinks."""

    def __init__(self, directory):
        self.path = Path(directory).absolute()
        repository = Path(__file__).resolve().parents[1]
        if self.path == repository or repository in self.path.parents:
            raise MaintenanceError("private artifacts must be outside the repository")
        if self.path.exists() and self.path.is_symlink():
            raise MaintenanceError("private artifact directory cannot be a symlink")
        self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.resolve() != self.path:
            raise MaintenanceError("private artifact path cannot contain symlinks")
        info = self.path.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise MaintenanceError("private artifact directory must be owner-only")

    def read(self, name):
        if not re.fullmatch(r"[a-zA-Z0-9._-]+", name):
            raise MaintenanceError("invalid private artifact name")
        path = self.path / name
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise MaintenanceError("private artifact must be an owner-only regular file")
            return stream.read()

    def write(self, name, body):
        if not re.fullmatch(r"[a-zA-Z0-9._-]+", name):
            raise MaintenanceError("invalid private artifact name")
        temporary = self.path / (".pending-" + str(uuid4()))
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path / name)
            directory = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def backup(self, body):
        name = "sha256-" + digest(body)
        existing = self.read(name)
        if existing is None:
            self.write(name, body)
        elif existing != body:
            raise MaintenanceError("private backup checksum mismatch")
        return name


@contextmanager
def publication_lease(jobs):
    owner = "storage-maintenance-" + str(uuid4())
    lease = jobs.acquire("publication", owner, LEASE_SECONDS)
    if lease is None:
        raise MaintenanceError("publication lease is busy")

    def renew():
        if jobs.renew("publication", owner, lease.attempt, LEASE_SECONDS) is None:
            raise MaintenanceError("publication lease was lost")

    try:
        yield owner, lease.attempt, renew
    finally:
        jobs.release("publication", owner, lease.attempt)


def required_object(storage, key, *, expected_etag="", checksum=True):
    if not allowed_key(key):
        raise MaintenanceError("object outside publication namespace")
    value = storage.get(key)
    if value is None or not value.etag:
        raise MaintenanceError("publication object missing or has no ETag")
    if expected_etag and value.etag != expected_etag:
        raise MaintenanceError("publication object ETag mismatch")
    if checksum and LEGACY_KEY.fullmatch(key) and key.rsplit("/", 1)[1].split(".")[0] != digest(value.body):
        raise MaintenanceError("legacy publication checksum mismatch")
    return value


def committed_objects(storage, items, manifest, *, fence):
    """Verify every visible pair against its committed entry and exact ETags."""
    entries = {entry["href"]: entry for entry in manifest["reports"]}
    published = {href: item for href, item in items.items() if item.get("entry") is not None}
    if entries.keys() != published.keys():
        raise MaintenanceError("manifest differs from committed publication entries")
    objects = {}
    for href, entry in entries.items():
        item = published[href]
        if parse_json(item["entry"]) != entry:
            raise MaintenanceError("manifest entry differs from committed publication")
        for field, stamp, destination in (("html_key", "html_stamp", href),
                                           ("json_key", "json_stamp", href[:-5] + ".json")):
            fence()
            if not item.get(stamp):
                raise MaintenanceError("committed report has no ETag")
            objects[destination] = (entry[field], required_object(storage, entry[field], expected_etag=item[stamp]))
    return objects


def read_iam_token(path):
    path = Path(path).absolute()
    if path.resolve() != path:
        raise MaintenanceError("IAM token file cannot contain symlinks")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise MaintenanceError("IAM token file must be an owner-only regular file")
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise MaintenanceError("IAM token file exceeds byte limit")
    try:
        token = raw.decode("ascii").strip()
    except UnicodeError:
        raise MaintenanceError("invalid IAM token file") from None
    if not token or not re.fullmatch(r"[!-~]+", token):
        raise MaintenanceError("invalid IAM token file")
    return token


def open_dependencies(namespace, token_file=None):
    from zont_analyzer.adapters.ydb.database import YdbConfig, YdbDatabase
    from zont_analyzer.adapters.ydb.jobs import JobLeaseRepository
    from zont_analyzer.adapters.ydb.publication import PublicationRepository
    from zont_analyzer.cloud.object_storage import ObjectStorage

    token = None
    if token_file is not None:
        token = read_iam_token(token_file)
        # The SDK checks these credential selectors before the explicit token.
        # This short-lived maintenance process must use the requested identity
        # consistently for YDB and S3, rather than silently falling back.
        for name in ("YDB_SERVICE_ACCOUNT_KEY_FILE_CREDENTIALS", "YDB_USER", "YDB_PASSWORD",
                     "YDB_ANONYMOUS_CREDENTIALS", "YDB_METADATA_CREDENTIALS", "YDB_OAUTH2_KEY_FILE"):
            os.environ.pop(name, None)
        os.environ["YDB_ACCESS_TOKEN_CREDENTIALS"] = token
    config = YdbConfig.from_environment(namespace=namespace)
    if config.namespace != namespace:
        raise MaintenanceError("YDB namespace disagrees with explicit target")
    db = YdbDatabase(config)
    try:
        storage = ObjectStorage(os.environ["CLOUD_PUBLICATION_BUCKET"],
                                os.environ.get("CLOUD_PUBLICATION_PREFIX", "reports"), token=lambda: token) \
            if token is not None else ObjectStorage.from_environment()
        return db, PublicationRepository(db), JobLeaseRepository(db), storage
    except BaseException:
        db.close()
        raise


def storage_target(storage):
    return CloudPublication(storage).storage_target
