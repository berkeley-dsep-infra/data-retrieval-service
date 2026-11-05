"""Find a user's archived home directories in GCS and sign download URLs."""
import datetime
import json
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from google.cloud import storage
from google.oauth2 import service_account

from .ownership import archive_key, candidate_keys


TERM_MONTHS = {"spring": 5, "summer": 8, "fall": 12}
ARCHIVE_SUFFIXES = (".tar.gz", ".zip")
EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
GLOB_SAFE_KEY = re.compile(r"[a-z0-9._+-]+")


@dataclass(frozen=True)
class Archive:
    name: str
    semester: str
    hub: str
    size: int
    updated: datetime.datetime

    @property
    def download_filename(self):
        return f"{self.semester}-{self.hub}-{self.name.rsplit('/', 1)[-1]}"


def storage_client(service_account_key):
    credentials_info = json.loads(service_account_key)
    credentials = service_account.Credentials.from_service_account_info(credentials_info)
    return storage.Client(credentials=credentials, project=credentials.project_id)


def parse_object_name(name):
    """Get (semester, hub) from an object name.

    The bucket has three layouts:
    - <semester>/<hub>/<user>.tar.gz, e.g. 2026-01/data8/yijun.tar.gz or
      fall-2021/datahub.berkeley.edu/yijun.tar.gz
    - <semester>-<hub>/<user>.tar.gz, e.g. 2018-1-spring-data100/yijun.tar.gz
    - <semester>/<user>.zip from before there were multiple hubs, e.g. 2015-3-fall/yijun.zip
    """
    parts = name.split("/")
    semester = parts[0]
    if len(parts) >= 3:
        hub = parts[1]
    else:
        match = re.match(r"^(.*-(?:spring|summer|fall))-(.+)$", semester)
        if match:
            semester, hub = match.groups()
        else:
            hub = "datahub"
    # e.g. ischool.datahub.berkeley.edu -> ischool
    hub = hub.removesuffix(".berkeley.edu").removesuffix(".datahub")
    return semester, hub


def semester_sort_key(semester):
    """Approximate date order for names like 2024-1-spring, spring-2021 and 2026-01.

    Folder names aren't consistent (2025-3 is March 2025, 2023-1-fall is fall 2023),
    so archives are sorted by when they were created, and this only breaks ties.
    """
    year_match = re.search(r"(19|20)\d\d", semester)
    year = int(year_match.group()) if year_match else 0
    month = 0
    term = re.search(r"spring|summer|fall", semester)
    if term:
        month = TERM_MONTHS[term.group()]
    else:
        number = re.search(r"(?:19|20)\d\d-(\d{1,2})\b", semester)
        if number:
            month = int(number.group(1))
    return (year, month, semester)


class ArchiveFinder:
    """Look up a user's archives in GCS on demand"""

    def __init__(self, client, bucket_name, cache_ttl, max_cached_users=1000):
        self.client = client
        self.bucket_name = bucket_name
        self.cache_ttl = cache_ttl
        self.max_cached_users = max_cached_users
        self._cache = OrderedDict()
        self._lock = threading.Lock()

    def _lookup(self, email):
        keys = sorted(k for k in candidate_keys(email) if GLOB_SAFE_KEY.fullmatch(k))
        match_glob = "**/{" + ",".join(keys) + "}{" + ",".join(ARCHIVE_SUFFIXES) + "}"
        blobs = self.client.list_blobs(
            self.bucket_name, match_glob=match_glob, fields="items(name,size,updated),nextPageToken"
        )
        archives = []
        for blob in blobs:
            if archive_key(blob.name) not in keys:
                continue
            semester, hub = parse_object_name(blob.name)
            archives.append(Archive(blob.name, semester, hub, blob.size or 0, blob.updated))
        return sorted(archives, key=lambda a: (a.updated or EPOCH, semester_sort_key(a.semester), a.hub), reverse=True)

    def cached(self, email):
        """The cached archives for email"""
        with self._lock:
            cached = self._cache.get(email)
            if cached and cached[0] > time.monotonic():
                return cached[1]
        return None

    def find(self, email):
        """All archives belonging to email, newest first"""
        archives = self.cached(email)
        if archives is not None:
            return archives
        now = time.monotonic()
        archives = self._lookup(email)
        with self._lock:
            self._cache[email] = (now + self.cache_ttl, archives)
            self._cache.move_to_end(email)
            while len(self._cache) > self.max_cached_users:
                self._cache.popitem(last=False)
        return archives

    def signed_url(self, archive, expiration_seconds):
        """Short-lived URL that downloads the archive directly from GCS"""
        blob = self.client.bucket(self.bucket_name).blob(archive.name)
        return blob.generate_signed_url(
            version="v4",
            expiration=datetime.timedelta(seconds=expiration_seconds),
            method="GET",
            response_disposition=f'attachment; filename="{archive.download_filename}"',
        )
