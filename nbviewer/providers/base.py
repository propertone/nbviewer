# -----------------------------------------------------------------------------
#  Copyright (C) Jupyter Development Team
#
#  Distributed under the terms of the BSD License.  The full license is in
#  the file COPYING, distributed as part of this software.
# -----------------------------------------------------------------------------
import asyncio
import hashlib
import pickle
import socket
import time
from contextlib import contextmanager
from datetime import datetime
from html import escape
from http.client import responses
from urllib.parse import quote
from urllib.parse import urlencode
from urllib.parse import urlparse
from urllib.parse import urlunparse

import statsd
from nbformat import current_nbformat
from nbformat import reads
from tornado import httpclient
from tornado import web
from tornado.concurrent import Future
from tornado.escape import url_escape
from tornado.escape import url_unescape
from tornado.escape import utf8
from tornado.ioloop import IOLoop

from ..render import NbFormatError
from ..render import render_notebook
from ..utils import EmptyClass
from ..utils import parse_header_links
from ..utils import time_block
from ..utils import url_path_join

try:
    import pycurl
    from tornado.curl_httpclient import CurlError
except ImportError:
    pycurl = None

    class CurlError(Exception):
        pass


format_prefix = "/format/"


class BaseHandler(web.RequestHandler):
    """Base Handler class with common utilities"""

    def initialize(self, format=None, format_prefix="", **handler_settings):
        # format: str, optional
        #     Rendering format (e.g. script, slides, html)
        self.format = format or self.default_format
        self.format_prefix = format_prefix
        self.http_client = httpclient.AsyncHTTPClient()
        self.date_fmt = "%a, %d %b %Y %H:%M:%S UTC"

        for handler_setting in handler_settings:
            setattr(self, handler_setting, handler_settings[handler_setting])

    # Overloaded methods
    def redirect(self, url, *args, **kwargs):
        purl = urlparse(url)

        eurl = urlunparse(
            (
                purl.scheme,
                purl.netloc,
                "/".join(
                    [
                        url_escape(url_unescape(p), plus=False)
                        for p in purl.path.split("/")
                    ]
                ),
                purl.params,
                purl.query,
                purl.fragment,
            )
        )

        return super().redirect(eurl, *args, **kwargs)

    def set_default_headers(self):
        self.add_header("Content-Security-Policy", self.content_security_policy)

    async def prepare(self):
        """Check if the user is authenticated with JupyterHub if the hub
        API endpoint and token are configured.

        Redirect unauthenticated requests to the JupyterHub login page.
        Do nothing if not running as a JupyterHub service.
        """
        # if any of these are set, assume we want to do auth, even if
        # we're misconfigured (better safe than sorry!)
        if self.hub_api_url or self.hub_api_token or self.hub_base_url:

            def redirect_to_login():
                self.redirect(
                    url_path_join(self.hub_base_url, "/hub/login")
                    + "?"
                    + urlencode({"next": self.request.path})
                )

            encrypted_cookie = self.get_cookie(self.hub_cookie_name)
            if not encrypted_cookie:
                # no cookie == not authenticated
                return redirect_to_login()

            try:
                # if the hub returns a success code, the user is known
                await self.http_client.fetch(
                    url_path_join(
                        self.hub_api_url,
                        "authorizations/cookie",
                        self.hub_cookie_name,
                        quote(encrypted_cookie, safe=""),
                    ),
                    headers={"Authorization": "token " + self.hub_api_token},
                )
            except httpclient.HTTPError as ex:
                if ex.response.code == 404:
                    # hub does not recognize the cookie == not authenticated
                    return redirect_to_login()
                # let all other errors surface: they're unexpected
                raise ex

    # Properties

    @property
    def base_url(self):
        return self.settings["base_url"]

    @property
    def binder_base_url(self):
        return self.settings["binder_base_url"]

    @property
    def cache(self):
        return self.settings["cache"]

    @property
    def cache_expiry_max(self):
        return self.settings.setdefault("cache_expiry_max", 120)

    @property
    def cache_expiry_min(self):
        return self.settings.setdefault("cache_expiry_min", 60)

    @property
    def client(self):
        return self.settings["client"]

    @property
    def config(self):
        return self.settings["config"]

    @property
    def content_security_policy(self):
        return self.settings["content_security_policy"]

    @property
    def default_format(self):
        return self.settings["default_format"]

    @property
    def formats(self):
        return self.settings["formats"]

    @property
    def frontpage_setup(self):
        return self.settings["frontpage_setup"]

    @property
    def hub_api_token(self):
        return self.settings.get("hub_api_token")

    @property
    def hub_api_url(self):
        return self.settings.get("hub_api_url")

    @property
    def hub_base_url(self):
        return self.settings["hub_base_url"]

    @property
    def hub_cookie_name(self):
        return "jupyterhub-services"

    @property
    def index(self):
        return self.settings["index"]

    @property
    def ipywidgets_base_url(self):
        return self.settings["ipywidgets_base_url"]

    @property
    def jupyter_js_widgets_version(self):
        return self.settings["jupyter_js_widgets_version"]

    @property
    def jupyter_widgets_html_manager_version(self):
        return self.settings["jupyter_widgets_html_manager_version"]

    @property
    def mathjax_url(self):
        return self.settings["mathjax_url"]

    @property
    def log(self):
        return self.settings["log"]

    @property
    def max_cache_uris(self):
        return self.settings.setdefault("max_cache_uris", set())

    @property
    def pending(self):
        return self.settings.setdefault("pending", {})

    @property
    def pool(self):
        return self.settings["pool"]

    @property
    def providers(self):
        return self.settings["providers"]

    @property
    def rate_limiter(self):
        return self.settings["rate_limiter"]

    @property
    def static_url_prefix(self):
        return self.settings["static_url_prefix"]

    @property
    def statsd(self):
        if hasattr(self, "_statsd"):
            return self._statsd
        if self.settings["statsd_host"]:
            self._statsd = statsd.StatsClient(
                self.settings["statsd_host"],
                self.settings["statsd_port"],
                self.settings["statsd_prefix"] + "." + type(self).__name__,
            )
            return self._statsd
        else:
            # return an empty mock object!
            self._statsd = EmptyClass()
            return self._statsd

    # ---------------------------------------------------------------
    # template rendering
    # ---------------------------------------------------------------

    def from_base(self, url, *args):
        if not url.startswith("/") or url.startswith(self.base_url):
            return url_path_join(url, *args)
        return url_path_join(self.base_url, url, *args)

    def get_template(self, name):
        """Return the jinja template object for a given name"""
        return self.settings["jinja2_env"].get_template(name)

    def render_template(self, name, **namespace):
        namespace.update(self.template_namespace)
        template = self.get_template(name)
        return template.render(**namespace)

    # Wrappers to facilitate custom rendering in subclasses without having to rewrite entire GET methods
    # This would seem to mostly involve creating different template namespaces to enable custom logic in
    # extended templates, but there might be other possibilities
    def render_status_code_template(self, status_code, **namespace):
        return self.render_template("%d.html" % status_code, **namespace)

    def render_error_template(self, **namespace):
        return self.render_template("error.html", **namespace)

    @property
    def template_namespace(self):
        return {
            "mathjax_url": self.mathjax_url,
            "static_url": self.static_url,
            "from_base": self.from_base,
            "google_analytics_id": self.settings.get("google_analytics_id"),
            "ipywidgets_base_url": self.ipywidgets_base_url,
            "jupyter_js_widgets_version": self.jupyter_js_widgets_version,
            "jupyter_widgets_html_manager_version": self.jupyter_widgets_html_manager_version,
        }

    # Overwrite the static_url method from Tornado to work better with our custom StaticFileHandler
    def static_url(self, url):
        return url_path_join(self.static_url_prefix, url)

    def breadcrumbs(self, path, base_url):
        """Generate a list of breadcrumbs"""
        breadcrumbs = []
        if not path:
            return breadcrumbs

        for name in path.split("/"):
            base_url = url_path_join(base_url, name)
            breadcrumbs.append({"url": base_url, "name": name})
        return breadcrumbs

    def get_page_links(self, response):
        """return prev_url, next_url for pagination

        Response must be an HTTPResponse from a paginated GitHub API request.

        Each will be None if there no such link.
        """
        links = parse_header_links(response.headers.get("Link", ""))
        next_url = prev_url = None
        if "next" in links:
            next_url = "?" + urlparse(links["next"]["url"]).query
        if "prev" in links:
            prev_url = "?" + urlparse(links["prev"]["url"]).query
        return prev_url, next_url

    # ---------------------------------------------------------------
    # error handling
    # ---------------------------------------------------------------

    def client_error_message(self, exc, url, body, msg=None):
        """Turn the tornado HTTP error into something useful
        
        Returns error code
        """
        str_exc = str(exc)

        # strip the unhelpful 599 prefix
        if str_exc.startswith("HTTP 599: "):
            str_exc = str_exc[10:]

        if (msg is None) and body and len(body) < 100:
            # if it's a short plain-text error message, include it
            msg = "%s (%s)" % (str_exc, escape(body))

        if not msg:
            msg = str_exc

        # Now get the error code
        if exc.code == 599:
            if isinstance(exc, CurlError):
                en = getattr(exc, "errno", -1)
                # can't connect to server should be 404
                # possibly more here
                if en in (pycurl.E_COULDNT_CONNECT, pycurl.E_COULDNT_RESOLVE_HOST):
                    code = 404
            # otherwise, raise 400 with informative message:
            code = 400
        elif exc.code >= 500:
            # 5XX, server error, but not this server
            code = 502
        else:
            # client-side error, blame our client
            if exc.code == 404:
                code = 404
                msg = "Remote %s" % msg
            else:
                code = 400

        return code, msg

    def reraise_client_error(self, exc):
        """Remote fetch raised an error"""
        try:
            url = exc.response.request.url.split("?")[0]
            body = exc.response.body.decode("utf8", "replace").strip()
        except AttributeError:
            url = "url"
            body = ""

        code, msg = self.client_error_message(exc, url, body)

        slim_body = escape(body[:300])

        self.log.warn("Fetching %s failed with %s. Body=%s", url, msg, slim_body)
        raise web.HTTPError(code, msg)

    @contextmanager
    def catch_client_error(self):
        """context manager for catching httpclient errors

        they are transformed into appropriate web.HTTPErrors
        """
        try:
            yield
        except httpclient.HTTPError as e:
            self.reraise_client_error(e)
        except socket.error as e:
            raise web.HTTPError(404, str(e))

    @property
    def fetch_kwargs(self):
        return self.settings.setdefault("fetch_kwargs", {})

    async def fetch(self, url, **overrides):
        """fetch a url with our async client

        handle default arguments and wrapping exceptions
        """
        kw = {}
        kw.update(self.fetch_kwargs)
        kw.update(overrides)
        with self.catch_client_error():
            response = await self.client.fetch(url, **kw)
        return response

    def write_error(self, status_code, **kwargs):
        """render custom error pages"""
        exc_info = kwargs.get("exc_info")
        message = ""
        status_message = responses.get(status_code, "Unknown")
        if exc_info:
            # get the custom message, if defined
            exception = exc_info[1]
            try:
                message = exception.log_message % exception.args
            except Exception:
                pass

            # construct the custom reason, if defined
            reason = getattr(exception, "reason", "")
            if reason:
                status_message = reason

        # build template namespace
        namespace = dict(
            status_code=status_code,
            status_message=status_message,
            message=message,
            exception=exception,
        )

        # render the template
        try:
            html = self.render_status_code_template(status_code, **namespace)
        except Exception as e:
            html = self.render_error_template(**namespace)
        self.set_header("Content-Type", "text/html")
        self.write(html)

    # ---------------------------------------------------------------
    # response caching
    # ---------------------------------------------------------------

    @property
    def cache_headers(self):
        # are there other headers to cache?
        h = {}
        for key in ("Content-Type",):
            if key in self._headers:
                h[key] = self._headers[key]
        return h

    _cache_key = None
    _cache_key_attr = "uri"

    @property
    def cache_key(self):
        """Use checksum for cache key because cache has size limit on keys
        """

        if self._cache_key is None:
            to_hash = utf8(getattr(self.request, self._cache_key_attr))
            self._cache_key = hashlib.sha1(to_hash).hexdigest()
        return self._cache_key

    def truncate(self, s, limit=256):
        """Truncate long strings"""
        if len(s) > limit:
            s = "%s...%s" % (s[: limit // 2], s[limit // 2 :])
        return s

    async def cache_and_finish(self, content=""):
        """finish a request and cache the result

        currently only works if:

        - result is not written in multiple chunks
        - custom headers are not used
        """
        request_time = self.request.request_time()
        # set cache expiry to 120x request time
        # bounded by cache_expiry_min,max
        # a 30 second render will be cached for an hour
        expiry = max(
            min(120 * request_time, self.cache_expiry_max), self.cache_expiry_min
        )

        if self.request.uri in self.max_cache_uris:
            # if it's a link from the front page, cache for a long time
            expiry = self.cache_expiry_max

        if expiry > 0:
            self.set_header("Cache-Control", "max-age=%i" % expiry)

        self.write(content)
        self.finish()

        short_url = self.truncate(self.request.path)
        cache_data = pickle.dumps(
            {"headers": self.cache_headers, "body": content}, pickle.HIGHEST_PROTOCOL
        )
        log = self.log.info if expiry > self.cache_expiry_min else self.log.debug
        log("Caching (expiry=%is) %s", expiry, short_url)
        try:
            with time_block("Cache set %s" % short_url, logger=self.log):
                await self.cache.set(
                    self.cache_key, cache_data, int(time.time() + expiry)
                )
        except Exception:
            self.log.error("Cache set for %s failed", short_url, exc_info=True)
        else:
            self.log.debug("Cache set finished %s", short_url)


def cached(method):
    """decorator for a cached page.

    This only handles getting from the cache, not writing to it.
    Writing to the cache must be handled in the decorated method.
    """

    async def cached_method(self, *args, **kwargs):
        uri = self.request.path
        short_url = self.truncate(uri)

        if self.get_argument("flush_cache", False):
            await self.rate_limiter.check(self)
            self.log.info("Flushing cache %s", short_url)
            # call the wrapped method
            await method(self, *args, **kwargs)
            return

        pending_future = self.pending.get(uri, None)
        loop = IOLoop.current()
        if pending_future:
            self.log.info("Waiting for concurrent request at %s", short_url)
            tic = loop.time()
            await pending_future
            toc = loop.time()
            self.log.info(
                "Waited %.3fs for concurrent request at %s", toc - tic, short_url
            )

        try:
            with time_block("Cache get %s" % short_url, logger=self.log):
                cached_pickle = await self.cache.get(self.cache_key)
            if cached_pickle is not None:
                cached = pickle.loads(cached_pickle)
            else:
                cached = None
        except Exception as e:
            self.log.error("Exception getting %s from cache", short_url, exc_info=True)
            cached = None

        if cached is not None:
            self.log.info("Cache hit %s", short_url)
            for key, value in cached["headers"].items():
                self.set_header(key, value)
            self.write(cached["body"])
        else:
            self.log.debug("Cache miss %s", short_url)
            await self.rate_limiter.check(self)
            future = self.pending[uri] = Future()
            try:
                # call the wrapped method
                await method(self, *args, **kwargs)
            finally:
                self.pending.pop(uri, None)
                # notify waiters
                future.set_result(None)

    return cached_method


class RenderingHandler(BaseHandler):
    """Base for handlers that render notebooks"""

    # notebook caches based on path (no url params)
    _cache_key_attr = "path"

    @property
    def render_timeout(self):
        """0 render_timeout means never finish early"""
        return self.settings.setdefault("render_timeout", 0)

    def initialize(self, **kwargs):
        super().initialize(**kwargs)
        loop = IOLoop.current()
        if self.render_timeout:
            self.slow_timeout = loop.add_timeout(
                loop.time() + self.render_timeout, self.finish_early
            )

    def finish_early(self):
        """When the render is slow, draw a 'waiting' page instead

        rely on the cache to deliver the page to a future request.
        """
        if self._finished:
            return
        self.log.info("Finishing early %s", self.request.uri)
        html = self.render_template("slow_notebook.html")
        self.set_status(202)  # Accepted
        self.finish(html)

        # short circuit some methods because the rest of the rendering will still happen
        self.write = self.finish = self.redirect = lambda chunk=None: None
        self.statsd.incr("rendering.waiting", 1)

    def filter_formats(self, nb, raw):
        """Generate a list of formats that can render the given nb json

        formats that do not provide a `test` method are assumed to work for
        any notebook
        """
        for name, format in self.formats.items():
            test = format.get("test", None)
            try:
                if test is None or test(nb, raw):
                    yield (name, format)
            except Exception as err:
                self.log.info("Failed to test %s: %s", self.request.uri, name)

    # empty methods to be implemented by subclasses to make GET requests more modular
    def get_notebook_data(self, **kwargs):
        """
        Pass as kwargs variables needed to define those variables which will be necessary for 
        the provider to find the notebook. (E.g. path for LocalHandler, user and repo for GitHub.) 
        Return variables the provider needs to find and load the notebook. Then run custom logic
        in GET or pass the output of get_notebook_data immediately to deliver_notebook.

        First part of any provider's GET method. 

        Custom logic, if applicable, is middle part of any provider's GET method, and usually
        is implemented or overwritten in subclasses, while get_notebook_data and deliver_notebook
        will often remain unchanged from the parent class (e.g. for a custom GitHub provider).
        """
        pass

    def deliver_notebook(self, **kwargs):
        """
        Pass as kwargs the return values of get_notebook_data to this method. Get the JSON data
        from the provider to render the notebook. Finish with a call to self.finish_notebook.

        Last part of any provider's GET method.
        """
        pass

    # Wrappers to facilitate custom rendering in subclasses without having to rewrite entire GET methods
    # This would seem to mostly involve creating different template namespaces to enable custom logic in
    # extended templates, but there might be other possibilities
    def render_notebook_template(
        self, body, nb, download_url, json_notebook, **namespace
    ):
        return self.render_template(
            "formats/%s.html" % self.format,
            body=body,
            nb=nb,
            download_url=download_url,
            format=self.format,
            default_format=self.default_format,
            format_prefix=format_prefix,
            formats=dict(self.filter_formats(nb, json_notebook)),
            format_base=self.request.uri.replace(self.format_prefix, "").replace(
                self.base_url, "/"
            ),
            date=datetime.utcnow().strftime(self.date_fmt),
            **namespace
        )

    async def finish_notebook(
        self, json_notebook, download_url, msg=None, public=False, **namespace
    ):
        """Renders a notebook from its JSON body.

        Parameters
        ----------
        json_notebook: str
            Notebook document in JSON format
        download_url: str
            URL to download the notebook document
        msg: str, optional
            Extra information to log when rendering fails
        public: bool, optional
            True if the notebook is public and its access indexed, False if not
        """

        if msg is None:
            msg = download_url

        try:
            parse_time = self.statsd.timer("rendering.parsing.time").start()
            nb = reads(json_notebook, current_nbformat)
            parse_time.stop()
        except ValueError:
            self.log.error("Failed to render %s", msg, exc_info=True)
            self.statsd.incr("rendering.parsing.fail")
            raise web.HTTPError(400, "Error reading JSON notebook")

        try:
            self.log.debug("Requesting render of %s", download_url)
            with time_block(
                "Rendered %s" % download_url, logger=self.log, debug_limit=0
            ):
                self.log.info(
                    "Rendering %d B notebook from %s", len(json_notebook), download_url
                )
                render_time = self.statsd.timer("rendering.nbrender.time").start()
                loop = asyncio.get_event_loop()
                nbhtml, config = await loop.run_in_executor(
                    self.pool,
                    render_notebook,
                    self.formats[self.format],
                    nb,
                    download_url,
                    self.config,
                )
                render_time.stop()
        except NbFormatError as e:
            self.statsd.incr("rendering.nbrender.fail", 1)
            self.log.error("Invalid notebook %s: %s", msg, e)
            raise web.HTTPError(400, str(e))
        except Exception as e:
            self.statsd.incr("rendering.nbrender.fail", 1)
            self.log.error("Failed to render %s", msg, exc_info=True)
            raise web.HTTPError(400, str(e))
        else:
            self.statsd.incr("rendering.nbrender.success", 1)
            self.log.debug("Finished render of %s", download_url)

        html_time = self.statsd.timer("rendering.html.time").start()
        html = self.render_notebook_template(
            body=nbhtml,
            nb=nb,
            download_url=download_url,
            json_notebook=json_notebook,
            **namespace
        )
        html_time.stop()

        if "content_type" in self.formats[self.format]:
            self.set_header("Content-Type", self.formats[self.format]["content_type"])
        await self.cache_and_finish(html)

        # Index notebook
        self.index.index_notebook(download_url, nb, public)


class FilesRedirectHandler(BaseHandler):
    """redirect files URLs without files prefix

    matches behavior of old app, currently unused.
    """

    def get(self, before_files, after_files):
        self.log.info("Redirecting %s to %s", before_files, after_files)
        self.redirect("%s/%s" % (before_files, after_files))


class AddSlashHandler(BaseHandler):
    """redirector for URLs that should always have trailing slash"""

    def get(self, *args, **kwargs):
        uri = self.request.path + "/"
        if self.request.query:
            uri = "%s?%s" % (uri, self.request.query)
        self.redirect(uri)


class RemoveSlashHandler(BaseHandler):
    """redirector for URLs that should never have trailing slash"""

    def get(self, *args, **kwargs):
        uri = self.request.path.rstrip("/")
        if self.request.query:
            uri = "%s?%s" % (uri, self.request.query)
        self.redirect(uri)
