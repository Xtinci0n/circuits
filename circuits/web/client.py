
try:
    from urllib.parse import urlparse
except ImportError:
    from urlparse import urlparse  # NOQA

from circuits.web.headers import Headers
from circuits.core import handler, BaseComponent, Event
from circuits.net.sockets import Close, Connect, TCPClient, Write

from circuits.net.protocols.http import HTTP


def parse_url(url):
    p = urlparse(url)

    if p.hostname:
        host = p.hostname
    else:
        raise ValueError("URL must be absolute")

    if p.scheme == "http":
        secure = False
        port = p.port or 80
    elif p.scheme == "https":
        secure = True
        port = p.port or 443
    else:
        raise ValueError("Invalid URL scheme")

    path = p.path or "/"

    if p.query:
        path += "?" + p.query

    return (host, port, path, secure)


class HTTPException(Exception):
    pass


class NotConnected(HTTPException):
    pass


class Request(Event):
    """Request Event

    This Event is used to initiate a new request.

    :param method: HTTP Method (PUT, GET, POST, DELETE)
    :type  method: str

    :param url: Request URL
    :type  url: str
    """

    def __init__(self, method, path, body=None, headers={}):
        "x.__init__(...) initializes x; see x.__class__.__doc__ for signature"

        super(Request, self).__init__(method, path, body, headers)


class Client(BaseComponent):

    channel = "client"

    def __init__(self, channel=channel):
        super(Client, self).__init__(channel=channel)
        self._response = None

        self._transport = TCPClient(channel=channel).register(self)

        HTTP(channel=channel).register(self._transport)

    @handler("write")
    def write(self, data):
        if self._transport.connected:
            self.fire(Write(data), self._transport)

    @handler("close")
    def close(self):
        if self._transport.connected:
            self.fire(Close(), self._transport)

    @handler("connect", filter=True)
    def connect(self, host=None, port=None, secure=None):
        if not self._transport.connected:
            self.fire(Connect(host, port, secure), self._transport)

        return True

    @handler("request")
    def request(self, method, url, body=None, headers={}):
        host, port, path, secure = parse_url(url)

        if not self._transport.connected:
            self.fire(Connect(host, port, secure))
            yield self.wait("connected", self._transport.channel)

        headers = Headers([(k, v) for k, v in headers.items()])
        # Clients MUST include Host header in HTTP/1.1 requests (RFC 2616)
        if "Host" not in headers:
            headers["Host"] = "{0:s}:{1:s}".format(host, "" if port in (80, 443) else str(port))
        if body is not None:
            headers["Content-Length"] = len(body)
        command = "%s %s HTTP/1.1" % (method, path)
        message = "%s\r\n%s" % (command, headers)
        self.fire(Write(message.encode('utf-8')), self._transport)
        if body is not None:
            self.fire(Write(body), self._transport)

    @handler("response")
    def _on_response(self, response):
        self._response = response
        if response.headers.get("Connection") == "Close":
            self.fire(Close(), self._transport)

    @property
    def connected(self):
        if hasattr(self, "_transport"):
            return self._transport.connected

    @property
    def response(self):
        return getattr(self, "_response", None)
