from __future__ import (absolute_import, print_function, division)
from Queue import Queue
import socket
import threading

from hpack.hpack import Encoder, Decoder
import backports.socketpair

from libmproxy.models import HTTPRequest, HTTPResponse
from netlib.http import Headers
from netlib.http.http1 import HTTP1Protocol
from netlib.http.http2 import Frame, HeadersFrame, ContinuationFrame, DataFrame, HTTP2Protocol, \
    SettingsFrame, WindowUpdateFrame
from netlib.tcp import Reader, ssl_read_select
from ..exceptions import Http2Exception, ProtocolException
from .base import Layer
from .http import _StreamingHttpLayer, HttpLayer

assert backports.socketpair


CLIENT_CONNECTION_PREFACE = "PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"


class Http2Connection(object):
    def __init__(self, connection):
        self.encoder = Encoder()
        self.decoder = Decoder()
        self.lock = threading.RLock()

        self._connection = connection

        self.initiated_streams = 0
        self.streams = {}

    def read_frame(self):
        with self.lock:
            return Frame.from_file(self.rfile)  # TODO: max_body_size

    def send_frame(self, *frames):
        """
        Should only be called with multiple frames that MUST be sent in sequence,
        e.g. a HEADEDRS frame and its CONTINUATION frames
        """
        with self.lock:
            for frame in frames:
                print("send frame", self.__class__.__name__, frame.human_readable())
                self._connection.wfile.write(frame.to_bytes())
                self._connection.wfile.flush()

    def send_headers(self, headers, stream_id, end_stream=False):
        with self.lock:
            if stream_id is None:
                stream_id = self._next_stream_id()
            proto = HTTP2Protocol(encoder=self.encoder)
            frames = proto._create_headers(headers, stream_id, end_stream)
            self.send_frame(*frames)
            return stream_id

    def send_data(self, data, stream_id, end_stream=False):
        proto = HTTP2Protocol()
        frames = proto._create_body(data, stream_id, end_stream)
        for frame in frames:
            self.send_frame(frame)

    def send(self, *args):  # pragma: nocover
        raise RuntimeError("Must only send frames on a HTTP 2 Connection")

    def read_headers(self, headers_frame):
        all_header_frames = self._read_all_header_frames(headers_frame)
        header_block_fragment = b"".join(frame.header_block_fragment for frame in all_header_frames)
        headers = Headers(
            [[str(k), str(v)] for k, v in self.decoder.decode(header_block_fragment)]
        )
        return all_header_frames, headers

    def _read_all_header_frames(self, headers_frame):
        frames = [headers_frame]
        while not frames[-1].flags & Frame.FLAG_END_HEADERS:
            # This blocks the whole connection if the client does not send header frames,
            # but that should not matter in practice.
            with self.lock:
                frame = self.read_frame()

            if not isinstance(frame, ContinuationFrame) or frame.stream_id != frames[-1].stream_id:
                raise Http2Exception("Unexpected frame: %s" % repr(frame))

            frames.append(frame)
        return frames

    def __nonzero__(self):
        return bool(self._connection)

    def __getattr__(self, item):
        return getattr(self._connection, item)

    def preface(self):
        raise NotImplementedError()

    def _next_stream_id(self):
        """
        Gets the next stream id. The caller must already hold the lock.
        """
        raise NotImplementedError()


class Http2ClientConnection(Http2Connection):
    def preface(self):
        # Check Client Preface
        expected_client_preface = CLIENT_CONNECTION_PREFACE
        actual_client_preface = self._connection.rfile.read(len(CLIENT_CONNECTION_PREFACE))
        if expected_client_preface != actual_client_preface:
            raise Http2Exception("Invalid Client preface: %s" % actual_client_preface)

        # Send Settings Frame
        settings_frame = SettingsFrame(settings={
            SettingsFrame.SETTINGS.SETTINGS_MAX_CONCURRENT_STREAMS: 50,
            SettingsFrame.SETTINGS.SETTINGS_INITIAL_WINDOW_SIZE: 2**31 - 1  # yolo flow control (tm)
        })
        self.send_frame(settings_frame)

        # yolo flow control (tm)
        window_update_frame = WindowUpdateFrame(stream_id=0, window_size_increment=2**31 - 2**16)
        self.send_frame(window_update_frame)

    def _next_stream_id(self):
        self.initiated_streams += 1
        # RFC 7540 5.1.1: stream 0x1 cannot be selected as a new stream identifier
        # by a client that upgrades from HTTP/1.1.
        return self.initiated_streams * 2


class Http2ServerConnection(Http2Connection):
    def connect(self):
        with self.lock:
            self._connection.connect()
            self.preface()

    def preface(self):
        self._connection.wfile.write(CLIENT_CONNECTION_PREFACE)
        self._connection.wfile.flush()

        # Send Settings Frame
        settings_frame = SettingsFrame(settings={
            SettingsFrame.SETTINGS.SETTINGS_ENABLE_PUSH: 0,
            SettingsFrame.SETTINGS.SETTINGS_MAX_CONCURRENT_STREAMS: 50,
            SettingsFrame.SETTINGS.SETTINGS_INITIAL_WINDOW_SIZE: 2**31 - 1  # yolo flow control (tm)
        })
        self.send_frame(settings_frame)

        # yolo flow control (tm)
        window_update_frame = WindowUpdateFrame(stream_id=0, window_size_increment=2**31 - 2**16)
        self.send_frame(window_update_frame)

    def _next_stream_id(self):
        self.initiated_streams += 1
        return self.initiated_streams * 2 + 1


class Http2Layer(Layer):

    def __init__(self, ctx, mode):
        super(Http2Layer, self).__init__(ctx)
        if mode != "transparent":
            raise NotImplementedError("HTTP2 supports transparent mode only")

        self.client_conn = Http2ClientConnection(self.client_conn)
        self.client_conn.preface()
        self.server_conn = Http2ServerConnection(self.server_conn)
        if self.server_conn:
            self.server_conn.preface()

        self.active_conns = [self.client_conn.connection]
        if self.server_conn:
            self.active_conns.append(self.server_conn.connection)

    def connect(self):
        self.server_conn.connect()
        self.active_conns.append(self.server_conn.connection)

    def set_server(self):
        raise NotImplementedError("Cannot change server for HTTP2 connections.")

    def disconnect(self):
        raise NotImplementedError("Cannot dis- or reconnect in HTTP2 connections.")

    def __call__(self):
        client = self.client_conn
        server = self.server_conn

        try:
            while True:
                r = ssl_read_select(self.active_conns, 10)
                for conn in r:
                    if conn == client.connection:
                        source = client
                    else:
                        source = server

                    frame = source.read_frame()
                    self.log("receive frame", "debug", (source.__class__.__name__, frame.human_readable()))

                    is_new_stream = (
                        isinstance(frame, HeadersFrame) and
                        source == client and
                        frame.stream_id not in source.streams
                    )
                    is_server_headers = (
                        isinstance(frame, HeadersFrame) and
                        source == server and
                        frame.stream_id in source.streams
                    )
                    is_data_frame = (
                        isinstance(frame, DataFrame) and
                        frame.stream_id in source.streams
                    )
                    is_settings_frame = (
                        isinstance(frame, SettingsFrame) and
                        frame.stream_id == 0
                    )
                    is_window_update_frame = (
                        isinstance(frame, WindowUpdateFrame)
                    )
                    if is_new_stream:
                        self._create_new_stream(frame, source)
                    elif is_server_headers:
                        self._process_server_headers(frame, source)
                    elif is_data_frame:
                        self._process_data_frame(frame, source)
                    elif is_settings_frame:
                        self._process_settings_frame(frame, source)
                    elif is_window_update_frame:
                        self._process_window_update_frame(frame)
                    else:
                        raise Http2Exception("Unexpected Frame: %s" % frame.human_readable())

        finally:
            self.log("Waiting for streams to finish...", "debug")
            for stream in self.client_conn.streams.values() + self.server_conn.streams.values():
                stream.join()


    def _process_window_update_frame(self, window_update_frame):
        pass  # yolo flow control (tm)

    def _process_settings_frame(self, settings_frame, source):
        if settings_frame.flags & Frame.FLAG_ACK:
            pass
        else:
            # yolo settings processing (tm) - fixme maybe
            settings_ack_frame = SettingsFrame(flags=Frame.FLAG_ACK)
            source.send_frame(settings_ack_frame)

    def _process_data_frame(self, data_frame, source):
        stream = source.streams[data_frame.stream_id]
        if source == self.client_conn:
            target = stream.into_client_conn
        else:
            target = stream.into_server_conn

        if len(data_frame.payload) > 0:
            chunk = "{:x}\r\n{}\r\n".format(len(data_frame.payload), data_frame.payload)
            target.sendall(chunk)

        if data_frame.flags & Frame.FLAG_END_STREAM:
            target.sendall("0\r\n\r\n")
            target.shutdown(socket.SHUT_WR)

    def _create_new_stream(self, headers_frame, source):
        header_frames, headers = self.client_conn.read_headers(headers_frame)
        stream = Stream(self, headers_frame.stream_id)
        source.streams[headers_frame.stream_id] = stream
        stream.start()

        stream.client_headers.put(headers)
        if header_frames[-1].flags & Frame.FLAG_END_STREAM:
            stream.into_client_conn.shutdown(socket.SHUT_WR)

    def _process_server_headers(self, headers_frame, source):
        header_frames, headers = self.server_conn.read_headers(headers_frame)
        stream = source.streams[headers_frame.stream_id]

        stream.server_headers.put(headers)
        if header_frames[-1].flags & Frame.FLAG_END_STREAM:
            stream.into_server_conn.shutdown(socket.SHUT_WR)


class StreamConnection(object):
    def __init__(self, original_connection, connection):
        self.original_connection = original_connection
        self.rfile = Reader(connection.makefile('rb', -1))

    @property
    def address(self):
        return self.original_connection.address

    @property
    def tls_established(self):
        return self.original_connection.tls_established

    def __nonzero__(self):
        return bool(self.original_connection)


class Stream(_StreamingHttpLayer, threading.Thread):

    def __init__(self, ctx, client_stream_id):
        super(Stream, self).__init__(ctx)

        self.client_stream_id = client_stream_id
        self.server_stream_id = None

        a, b = socket.socketpair()
        self.client_conn = StreamConnection(self.ctx.client_conn, a)
        self.into_client_conn = b
        self.server_conn = StreamConnection(self.ctx.server_conn, b)
        self.into_server_conn = a

        self.client_headers = Queue()
        self.server_headers = Queue()

    def read_request(self):
        headers = self.client_headers.get()

        # All HTTP/2 requests MUST include exactly one valid value for the :method, :scheme, and
        # :path pseudo-header fields, unless it is a CONNECT request
        try:
            # TODO: Possibly .pop()?
            method = headers[':method']
            if method == "CONNECT":
                raise NotImplementedError("HTTP2 CONNECT not supported")
            else:
                host = None
                port = None
            scheme = headers[':scheme']
            path = headers[':path']
        except KeyError:
            raise Http2Exception("Malformed HTTP2 request")

        body = HTTP1Protocol(rfile=self.client_conn.rfile).read_http_body(
            headers,
            self.config.body_size_limit,
            method,
            None,
            True
        )

        return HTTPRequest(
            "relative",
            method,
            scheme,
            host,
            port,
            path,
            (2, 0),
            headers,
            body,
        )

    def send_request(self, request):
        # TODO: The end_stream stuff is too simple for a CONNECT request.
        self.server_stream_id = self.ctx.server_conn.send_headers(
            request.headers,
            None,
            end_stream=not request.body
        )
        # TODO: This feels like the wrong place for registering.
        self.ctx.server_conn.streams[self.server_stream_id] = self
        if request.body:
            self.ctx.server_conn.send_data(request.body, self.server_stream_id, end_stream=True)

    def read_response_headers(self):

        # TODO: The first headers received from the server might be informational headers
        headers = self.server_headers.get()

        # All HTTP/2 responses MUST include exactly one valid value for the :status
        try:
            # TODO: Possibly .pop()?
            status = int(headers[':status'])
        except KeyError:
            raise Http2Exception("Malformed HTTP2 response")

        # TODO: Timestamps
        return HTTPResponse(
            (2, 0),
            status,
            "",
            headers,
            None
        )

    def read_response_body(self, headers, request_method, response_code, max_chunk_size=None):
        return HTTP1Protocol(rfile=self.server_conn.rfile)._read_chunked(
            self.config.body_size_limit, False
        )

    def send_response_headers(self, response):
        self.ctx.client_conn.send_headers(
            response.headers,
            self.client_stream_id,
            end_stream=False
        )

    def send_response_body(self, response, chunks):
        if chunks:
            for chunk in chunks:
                self.ctx.client_conn.send_data(chunk, self.client_stream_id, end_stream=False)
        self.ctx.client_conn.send_data("", self.client_stream_id, end_stream=True)

    def check_close_connection(self, flow):
        # RFC 7540 8.1
        # > An HTTP request/response exchange fully consumes a single stream.
        return True

    def run(self):
        layer = HttpLayer(self, "transparent")
        try:
            layer()
        except ProtocolException as e:
            self.log(e, "info")
            # TODO: Send RST_STREAM?
