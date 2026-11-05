import logging
import os
import secrets
import sys
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from itertools import groupby
from textwrap import dedent
from urllib.parse import urlparse

from jupyterhub.services.auth import HubOAuthCallbackHandler, HubOAuthenticated
from jupyterhub.utils import url_path_join
from tornado import web
from tornado.ioloop import IOLoop
from tornado.log import LogFormatter
from traitlets import Int, Unicode, default
from traitlets.config import Application

from .storage import ArchiveFinder, storage_client


__version__ = "0.0.1.dev"


def human_size(size):
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def group_by_hub(archives):
    """[(hub, [archive, ...]), ...] sorted by hub, keeping the newest-first order within each hub"""
    by_hub = groupby(sorted(archives, key=lambda a: a.hub), key=lambda a: a.hub)
    return [(hub, list(hub_archives)) for hub, hub_archives in by_hub]


class BaseHandler(HubOAuthenticated, web.RequestHandler):
    @property
    def retrieval(self):
        """The DataRetrieval application, which holds the configuration"""
        return self.settings["data_retrieval"]

    @property
    def finder(self):
        return self.settings["archive_finder"]

    async def find_archives(self):
        """The logged-in user's archives"""
        return await IOLoop.current().run_in_executor(
            self.settings["lookup_executor"], self.finder.find, self.user_email()
        )

    def user_email(self):
        name = self.current_user["name"]
        return name if "@" in name else f"{name}@{self.retrieval.email_domain}"

    def template_args(self):
        """Values used by html templates"""
        return dict(
            email=self.user_email(),
            human_size=human_size,
            download_url=url_path_join(self.settings["service_prefix"], "download"),
            support_email=self.retrieval.support_email,
            expiration_minutes=self.retrieval.url_expiration // 60,
        )


class ArchiveListHandler(BaseHandler):
    """Shows the user's archives right away if they're cached,
    otherwise a loading message while the page fetches them from GCS"""

    @web.authenticated
    def get(self):
        prefix = self.settings["service_prefix"]
        archives = self.finder.cached(self.user_email())
        xsrf_token = self.xsrf_token
        self.render(
            "index.html",
            hubs=None if archives is None else group_by_hub(archives),
            archives_url=url_path_join(prefix, "archives"),
            xsrf_token=xsrf_token.decode() if isinstance(xsrf_token, bytes) else xsrf_token,
            **self.template_args(),
        )


class ArchivesHandler(BaseHandler):
    """The archive list on its own, looked up in GCS if needed"""

    @web.authenticated
    async def get(self):
        archives = await self.find_archives()
        self.render("archives.html", hubs=group_by_hub(archives), **self.template_args())


class DownloadHandler(BaseHandler):
    """Redirect to a short-lived signed URL for one of the user's archives"""

    downloads = defaultdict(deque)

    def _rate_limited(self, username):
        recent = self.downloads[username]
        now = time.time()
        while recent and recent[0] < now - self.retrieval.rate_window:
            recent.popleft()
        if len(recent) >= self.retrieval.rate_limit:
            return True
        recent.append(now)
        return False

    @web.authenticated
    async def get(self):
        username = self.current_user["name"]
        name = self.get_argument("object", "")
        archive = next((a for a in await self.find_archives() if a.name == name), None)
        if archive is None:
            raise web.HTTPError(404, "Archive not found")
        if self._rate_limited(username):
            self.retrieval.log.info(f"Rate limited {username}")
            raise web.HTTPError(429, "Too many downloads. Please try again later.")

        url = self.finder.signed_url(archive, self.retrieval.url_expiration)
        self.retrieval.log.info(f"Signed a download URL for {username}")
        self.redirect(url)


class DataRetrieval(Application):

    bucket = Unicode(
        "ucb-datahub-archived-homedirs",
        help=dedent(
            """
            GCS bucket with the archived home directories.
            """
        ).strip(),
    ).tag(
        config=True,
    )

    email_domain = Unicode(
        "berkeley.edu",
        help=dedent(
            """
            Domain appended to hub usernames to get the user's email address,
            for usernames without one. Archive names are derived from it.
            """
        ).strip(),
    ).tag(
        config=True,
    )

    support_email = Unicode(
        "datahub-support@berkeley.edu",
        help=dedent(
            """
            Address users are told to contact for help.
            """
        ).strip(),
    ).tag(
        config=True,
    )

    url_expiration = Int(
        3600,
        help=dedent(
            """
            How long a signed download URL is valid, in seconds.
            """
        ).strip(),
    ).tag(
        config=True,
    )

    cache_ttl = Int(
        24 * 3600,
        help=dedent(
            """
            How long (in seconds) to cache a user's list of archives. Archives are
            only added every few months, so this can be long.
            """
        ).strip(),
    ).tag(
        config=True,
    )

    max_concurrent_lookups = Int(
        4,
        help=dedent(
            """
            Limit the number of GCS lookups running at the same time.
            """
        ).strip(),
    ).tag(
        config=True,
    )

    rate_limit = Int(
        5,
        help=dedent(
            """
            Number of downloads allowed per user per rate_window.
            """
        ).strip(),
    ).tag(
        config=True,
    )

    rate_window = Int(
        3600,
        help=dedent(
            """
            The rate limit window, in seconds.
            """
        ).strip(),
    ).tag(
        config=True,
    )

    service_account_key = Unicode(
        os.environ.get("DATA_RETRIEVAL_SA"),
        allow_none=True,
        help=dedent(
            """
            JSON key of the GCP service account used to list the bucket and sign download URLs.
            """
        ).strip(),
    ).tag(
        config=True,
    )

    _log_formatter_cls = LogFormatter

    @default("log_level")
    def _log_level_default(self):
        return logging.INFO

    @default("log_datefmt")
    def _log_datefmt_default(self):
        """Exclude date from default date format"""
        return "%Y-%m-%d %H:%M:%S"

    @default("log_format")
    def _log_format_default(self):
        """override default log format to include time"""
        return "%(color)s[%(levelname)1.1s %(asctime)s.%(msecs).03d %(name)s %(module)s:%(lineno)d]%(end_color)s %(message)s"

    aliases = {
        "bucket": "DataRetrieval.bucket",
        "email_domain": "DataRetrieval.email_domain",
        "support_email": "DataRetrieval.support_email",
        "url_expiration": "DataRetrieval.url_expiration",
        "cache_ttl": "DataRetrieval.cache_ttl",
        "max_concurrent_lookups": "DataRetrieval.max_concurrent_lookups",
        "rate_limit": "DataRetrieval.rate_limit",
        "rate_window": "DataRetrieval.rate_window",
    }

    def make_app(self, finder):
        prefix = os.environ.get("JUPYTERHUB_SERVICE_PREFIX", "/services/data-retrieval/")
        return web.Application(
            [
                (prefix, ArchiveListHandler),
                (url_path_join(prefix, "archives"), ArchivesHandler),
                (url_path_join(prefix, "download"), DownloadHandler),
                (url_path_join(prefix, "oauth_callback"), HubOAuthCallbackHandler),
            ],
            template_path=os.path.join(os.path.dirname(__file__), "templates"),
            cookie_secret=secrets.token_bytes(32),
            service_prefix=prefix,
            data_retrieval=self,
            archive_finder=finder,
            lookup_executor=ThreadPoolExecutor(max_workers=self.max_concurrent_lookups),
        )

    def start(self):
        if not self.service_account_key:
            self.log.error("DATA_RETRIEVAL_SA must be set.")
            sys.exit(1)

        try:
            port = urlparse(os.environ["JUPYTERHUB_SERVICE_URL"]).port
        except Exception as e:
            self.log.error(f"Error getting JUPYTERHUB_SERVICE_URL. {e}")
            sys.exit(1)

        finder = ArchiveFinder(storage_client(self.service_account_key), self.bucket, self.cache_ttl)
        self.make_app(finder).listen(port, address="0.0.0.0")
        self.log.info(f"Data retrieval service listening on port {port}")
        try:
            IOLoop.current().start()
        except KeyboardInterrupt:
            pass


def main():
    DataRetrieval.launch_instance()


if __name__ == "__main__":
    main()
