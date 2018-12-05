"""
This module delivers the :class:`.CoreApplication` derived from
:class:`tornado.web.Application`, :class:`.CoreApiContainer` encapsulating
one or more applications and helper method :meth:`.serve` and
:meth:`.serve_all` utilising :class:`.CoreApiServerTool` for server and
endpoint management.

A blueprint for server definition and startup is::

    from core4.api.v1.application import CoreApiContainer, serve
    from core4.api.v1.request.queue.job import JobHandler

    class CoreApiServer(CoreApiContainer):
        root = "core4/api/v1"
        rules = [
            (r'/job/?(.*)', JobHandler)
        ]


    if __name__ == '__main__':
        serve(CoreApiServer)


Please note that :meth:`.serve` can handle one or multiple
:class:`.CoreApiServer` objects with multiple endpoints and resources as in
the following example::

    serve(CoreApiServer, CoreAnotherpiAServer)
"""

import hashlib

import tornado.routing
import tornado.web

import core4.const
import core4.error
import core4.service.setup
import core4.util.data
import core4.util.node
from core4.api.v1.request.default import DefaultHandler
from core4.api.v1.request.main import CoreRequestHandler
from core4.api.v1.request.static import CoreStaticFileHandler
from core4.api.v1.request.standard.file import FileHandler
from core4.api.v1.request.standard.info import InfoHandler
from core4.api.v1.request.standard.login import LoginHandler
from core4.api.v1.request.standard.logout import LogoutHandler
from core4.api.v1.request.standard.profile import ProfileHandler
from core4.base.main import CoreBase


class CoreApiContainer(CoreBase):
    """
    The :class:`CoreApiContainer` class is a container for a single or multiple
    :class:`.CoreRequestHandler` which is based on torando's class
    :class:`tornado.web.RequestHandler`. A container encapsulates endpoint
    resources under the same :attr:`.root` URL defined by the :attr:`.root`
    attribute.

    The default ``root`` is the project name.
    """

    #: if ``True`` then the application container is deployed with serve_all
    enabled = True
    #: root URL, defaults to the project name
    root = None
    #: list of tuples with route, request handler (i.e.
    #  :class:`.CoreRequestHandler` or class:`tornado.web.RequestHandler`
    #  derived class
    rules = []

    upwind = ["log_level", "enabled", "root"]

    def __init__(self, base_url=None, **kwargs):
        CoreBase.__init__(self)
        for attr in ("debug", "compress_response", "cookie_secret"):
            kwargs[attr] = kwargs.get(attr, self.config.api.setting[attr])
        kwargs["default_handler_class"] = DefaultHandler
        kwargs["default_handler_args"] = ()
        kwargs["log_function"] = self._log
        self._settings = kwargs
        self.base_url = base_url
        # upwind class properties from configuration
        for prop in ("enabled", "root"):
            if prop in self.class_config:
                if self.class_config[prop] is not None:
                    setattr(self, prop, self.class_config[prop])

    def _log(self, handler):
        # internal logging method
        if getattr(handler, "logger", None) is None:
            # regular logging
            logger = self.logger
            identifier = self.identifier
        else:
            # CoreRequestHandler logging
            logger = handler.logger
            identifier = handler.identifier
        if handler.get_status() < 400:
            meth = logger.info
        elif handler.get_status() < 500:
            meth = logger.warning
        else:
            meth = logger.error
        request_time = 1000.0 * handler.request.request_time()
        meth("[%d] [%s %s] in [%.2fms] by [%s] from [%s]",
             handler.get_status(), handler.request.method,
             handler.request.path, request_time, handler.current_user,
             self.identifier, extra={"identifier": identifier})

    @classmethod
    def url(cls, *args):
        """
        Class method to prefix the passed, optional URL with ``root`` using
        :meth:`.get_root`.

        :param args: optional URL str
        :return: absolute URI path from :attr:`root`
        """
        return cls().get_root(*args)

    def get_root(self, path=None):
        """
        Returns the container`s ``root`` URL or prefixes the passed relative
        path with the container's ``root``

        :param path: relative path (optional)
        :return: ``root`` or absolute path below ``root``
        """
        root = self.root
        if root is None:
            root = self.project
        if not root.startswith("/"):
            root = "/" + root
        if root.endswith("/"):
            root = root[:-1]
        if path:
            if not path.startswith("/"):
                path = "/" + path
            return root + path
        return root

    def iter_rule(self):
        """
        Returns the rooted request handler as defined by the container's
        :attr:`rules` attribute.

        :return: list of tuples with route, request handler and handler
                 parameters
        """
        return ((self.get_root(ret[0]), *ret[1:]) for ret in self.rules)

    def make_application(self):
        """
        Validates and pre-processes :class:`CoreApiContainer` rules and
        transfers a handler lookup dictionary to :class:`RootContainer`.

        :return: :class:`.CoreApplication` instance
        """
        unique = set()
        rules = []
        routes = {}
        for rule in self.iter_rule():
            if isinstance(rule, (tuple, list)):
                if len(rule) >= 2:
                    routing = rule[0]
                    cls = rule[1]
                    if (isinstance(routing, str)
                            and issubclass(cls, tornado.web.RequestHandler)):
                        md5_route = hashlib.md5(
                            routing.encode("utf-8")).hexdigest()
                        if md5_route not in unique:
                            # self.logger.debug(
                            #     "starting [%s] as [%s] with [%s]",
                            #     routing, md5_route, cls.__name__)
                            unique.add(md5_route)
                            rules.append(
                                tornado.routing.Rule(
                                    tornado.routing.PathMatches(routing),
                                    *rule[1:], name=md5_route))
                            # lookup applies to core request handlers only
                            if issubclass(cls, CoreRequestHandler):
                                md5_qual_name = hashlib.md5(
                                    cls.qual_name().encode(
                                        "utf-8")).hexdigest()
                                routes.setdefault(md5_qual_name, {})
                                routes[md5_qual_name][md5_route] = (
                                    self, *rule)
                        else:
                            raise core4.error.Core4SetupError(
                                "route [%s] already exists" % routing)
                        continue
            raise core4.error.Core4SetupError(
                "routing requires list of tuples (str, handler, *args)")
        app = CoreApplication(rules, self, **self._settings)
        # transfer routes lookup with handler/routing md5 and app to container
        for md5_qual_name in routes:
            RootContainer.routes.setdefault(md5_qual_name, {})
            for md5_route in routes[md5_qual_name]:
                RootContainer.routes[md5_qual_name][md5_route] = (
                    app, *routes[md5_qual_name][md5_route])
                self.logger.info(
                    "started [%s] on [%s], pattern [%s] as [%s/%s]",
                    routes[md5_qual_name][md5_route][2].__name__,
                    core4.util.data.unre_url(
                        routes[md5_qual_name][md5_route][1]),
                    routes[md5_qual_name][md5_route][1],
                    md5_qual_name,
                    md5_route)
        return app


class CoreApplication(tornado.web.Application):
    """
    Represents a wrapper class around :class:`tornado.web.Application`. This
    wrapper extends applications' properties with a ``.container`` property
    referencing the :class:`.CoreApiContainer` object and delivers special
    processing for the *card* and *help* handler requests.
    """

    def __init__(self, handlers, container, *args, **kwargs):
        super().__init__(handlers, *args, **kwargs)
        self.container = container
        self.identifier = container.identifier

    def find_handler(self, request, **kwargs):
        """
        todo: requires documentation
        :param request:
        :param kwargs:
        :return:
        """
        if request.path.startswith(core4.const.CARD_URL):
            parts = request.path.split("/")
            md5_qual_name = parts[-2]
            md5_rule_id = parts[-1]
            (app, container, pattern, cls, *args) = self.find_md5(
                md5_qual_name, md5_rule_id)
            request.method = core4.const.CARD_METHOD
            return self.get_handler_delegate(request, cls, *args)
        return super().find_handler(request, **kwargs)

    def find_md5(self, md5_qual_name, md5_route=None):
        """
        Find the passed ``qual_name`` and optional ``routing`` MD5 digest in
        the bundled lookup built during the creation of applications
        (:meth:`.make_application).

        :param md5_qual_name: find the request handler based on
                              :class:`.CoreRequestHandler` by the ``qual_name``
                              MD5 digest.
        :param md5_route: find the request handler route based on
                          :class:`.CoreRequestHandler` by the ``qual_name``
                          and routing pattern MD5 digests.
        :return: tuple of (:class:`.CoreApiContainer`, pattern, class,
                 arguments)
        """
        handler = RootContainer.routes.get(md5_qual_name)
        if md5_route:
            return handler.get(md5_route)
        return list(handler.values())[0]

    def handler_help(self, cls):
        """
        Delivers dict with help information about the passed
        :class:`.CoreRequestHandler` class

        :param cls: :class:`.CoreRequestHandler` class
        :return: dict as delivered by :meth:`.CoreApiInspectir.handler_info`
        """
        from core4.service.introspect.api import CoreApiInspector
        inspect = core4.service.introspect.api.CoreApiInspector()
        return inspect.handler_info(cls)


class RootContainer(CoreApiContainer):
    """
    This class is automatically attached to each server with :meth:`serve``
    or :meth:`serve_all` to deliver the following standard request handlers
    with core4:

    * ``/login`` with :class:`.LoginHandler`
    * ``/logout`` with :class:`.LogoutHandler`
    * ``/profile`` with :class:`.ProfileHandler`
    * ``/file`` for static file access with :class:`FileHandler`
    * ``/info`` with :class:`.InfoHandler`
    * ``/`` for ``favicon.ico`` delivery with :class:`.CoreStaticFileHandler`
    """
    root = ""
    rules = [
        (core4.const.CORE4_API + r"/login", LoginHandler),
        (core4.const.CORE4_API + r"/logout", LogoutHandler),
        (core4.const.CORE4_API + r"/profile", ProfileHandler),
        (core4.const.CORE4_API + r"/file/(default|project)/(.+?)/(.+?)/(.+)$",
         FileHandler),
        (core4.const.CORE4_API + r'/info/?(.*)', InfoHandler),
        (r'/(.*)', CoreStaticFileHandler, {"path": "./request/_static"})
    ]
    routes = {}
    apps = {}
