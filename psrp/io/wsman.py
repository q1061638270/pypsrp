# -*- coding: utf-8 -*-
# Copyright: (c) 2021, Jordan Borean (@jborean93) <jborean93@gmail.com>
# MIT License (see LICENSE or https://opensource.org/licenses/MIT)

import abc
import asyncio
import base64
import functools
from ssl import SSLContext

import httpcore
import httpx
import re
import spnego
import spnego.channel_bindings
import struct
import typing

from urllib.parse import urlparse

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.exceptions import UnsupportedAlgorithm

from httpcore._async.connection import (
    AsyncHTTPConnection,
    ConnectionState,
)


def _async_wrap(func, *args, **kwargs):
    """ Runs a sync function in the background. """
    loop = asyncio.get_running_loop()
    task = loop.run_in_executor(None, functools.partial(func, *args, **kwargs))

    return task


class _AsyncWinRMTransport(httpcore.AsyncHTTPTransport):

    def __init__(
            self,
            ssl_context: SSLContext,
            keepalive_expiry: float,
            username: typing.Optional[str] = None,
            password: typing.Optional[str] = None,
            protocol: str = 'negotiate',
            encryption_required: bool = False,
            service: str = 'HTTP',
            hostname_override: typing.Optional[str] = None,
            send_cbt: bool = True,
            delegate: bool = False,
            credssp_allow_tlsv1: bool = False,
            credssp_require_kerberos: bool = False,
    ):
        self.username = username
        self.password = password
        self.protocol = protocol.lower()
        self.service = service
        self.hostname_override = hostname_override
        self.send_cbt = send_cbt

        self._connection = None
        self._ssl_context = ssl_context
        self._keepalive_expiry = keepalive_expiry

        self._auth_header = None
        self._context = None
        self._context_req = spnego.ContextReq.default
        self._spnego_options = spnego.NegotiateOptions.none
        self._encrypt = encryption_required

        if encryption_required:
            self._spnego_options |= spnego.NegotiateOptions.wrapping_winrm

        if self.protocol == 'credssp':
            self._accepted_protocols = ['CredSSP']

            if credssp_allow_tlsv1:
                self._spnego_options |= spnego.NegotiateOptions.credssp_allow_tlsv1

            if credssp_require_kerberos:
                self._spnego_options |= spnego.NegotiateOptions.negotiate_kerberos

        elif self.protocol in ['negotiate', 'kerberos', 'ntlm']:
            self._accepted_protocols = ['Negotiate', 'Kerberos', 'NTLM']

            if delegate:
                self._context_req |= spnego.ContextReq.delegate

        else:
            raise ValueError("%s only supports credssp, negotiate, kerberos, or ntlm authentication"
                             % type(self).__name__)

        escaped_protocols = '|'.join([re.escape(p) for p in self._accepted_protocols])
        self._regex = re.compile(r'(%s)\s*([^,]*),?' % escaped_protocols, re.I)

    async def arequest(
        self,
        method: bytes,
        url: httpx.URL,
        headers: httpx.Headers = None,
        stream: httpcore.AsyncByteStream = None,
        ext: typing.Dict = None,
    ) -> typing.Tuple[int, httpx.Headers, httpcore.AsyncByteStream, typing.Dict]:
        ext = ext or {}
        connection = await self._get_connection()

        if not connection:
            self._context = None
            self._connection = connection = await self._create_connection(url, ext)

        if not self._context:
            # Set up the authentication context. If we are encrypting data we
            # cannot send anything at the moment.
            response = await self._authenticate(
                connection, method, url, headers,
                None if self._encrypt else stream, ext
            )

            if not self._encrypt:
                return response

        new_headers = httpx.Headers(headers)
        new_stream = stream
        if self._encrypt:
            dec_data = b''
            async for data in stream:
                dec_data += data
            enc_data, content_type = _encrypt_wsman(dec_data, new_headers['Content-Type'], self._encryption_type,
                                                    self._context)
            new_headers['Content-Type'] = content_type
            new_headers['Content-Length'] = str(len(enc_data))
            new_stream = httpcore.PlainByteStream(enc_data)

        status_code, headers, stream, ext = await self._connection.arequest(
            method, url, headers=headers, stream=new_stream, ext=ext
        )
        connection.expires_at = await connection.backend.time() + self._keepalive_expiry

        new_stream = stream
        content_type = httpx.Headers(headers).get('content-type', '')
        if content_type.startswith('multipart/encrypted;') or content_type.startswith('multipart/x-multi-encrypted;'):
            enc_data = b''
            async for data in stream:
                enc_data += data
            await stream.aclose()
            dec_data = _decrypt_wsman(enc_data, content_type, self._context)
            new_stream = httpcore.PlainByteStream(dec_data)

        return status_code, headers, new_stream, ext

    async def _authenticate(
            self,
            connection: AsyncHTTPConnection,
            method: bytes,
            url: httpx.URL,
            headers: httpx.Headers = None,
            stream: httpcore.AsyncByteStream = None,
            ext: typing.Dict = None,
    ):
        # Get the TLS object for CBT if required - will be None when connecting over HTTP
        cbt = None
        ssl_object = connection.socket.stream_writer.get_extra_info('ssl_object')
        if ssl_object and self.send_cbt and self.protocol != 'credssp':
            cert = ssl_object.getpeercert(True)
            cert_hash = get_tls_server_end_point_hash(cert)
            cbt = spnego.channel_bindings.GssChannelBindings(application_data=b"tls-server-end-point:" + cert_hash)

        auth_hostname = self.hostname_override or url[1].decode('utf-8')
        self._context = await _async_wrap(
            spnego.client, self.username, self.password, hostname=auth_hostname,
            service=self.service, channel_bindings=cbt, context_req=self._context_req,
            protocol=self.protocol, options=self._spnego_options
        )

        # Send a blank request for the first authentication packet
        # TODO: Send actual if not encrypting data
        new_headers = httpx.Headers(headers.copy())
        if not stream:
            new_headers['Content-Length'] = '0'
        auth_header = 'Negotiate'

        out_token = await _async_wrap(self._context.step)
        while not self._context.complete or out_token is not None:
            new_headers['Authorization'] = "%s %s" % (auth_header, base64.b64encode(out_token).decode())

            # send the request with the auth token and get the response
            response = await connection.arequest(method, url, headers=new_headers.raw, stream=stream, ext=ext)
            await response[2].aclose()
            connection.expires_at = await connection.backend.time() + self._keepalive_expiry

            auth_header = httpx.Headers(response[1]).get('www-authenticate', '')
            in_token = self._regex.search(auth_header)
            if in_token:
                in_token = base64.b64decode(in_token.group(2))

            # If there was no token received from the host then we just break the auth cycle.
            if not in_token:
                break

            out_token = await _async_wrap(self._context.step, in_token)

        return response

    async def aclose(self) -> None:
        await self._connection.aclose()
        self._connection = None

    async def _create_connection(
            self,
            url: httpx.URL,
            ext: typing.Dict,
    ):
        connection = AsyncHTTPConnection(
            origin=url[:3],
            ssl_context=self._ssl_context,
        )
        socket = await connection._open_socket(timeout=ext.get('timeout', {}))
        connection.socket = socket
        connection.expires_at = await connection.backend.time() + self._keepalive_expiry

        return connection

    async def _get_connection(self):
        connection = self._connection

        if not connection:
            return

        must_close = False

        if connection.state == ConnectionState.IDLE:
            now = await connection.backend.time()
            if connection.is_socket_readable() or now >= connection.expires_at:
                must_close = True

        else:
            must_close = True

        if must_close:
            await connection.aclose()
            self._connection = None

        return self._connection

    @property
    def _encryption_type(self) -> str:
        """ Returns the WSMan encryption Content-Type for the authentication protocol used. """
        if self.protocol == 'kerberos':
            protocol = 'Kerberos'

        elif self.protocol == 'credssp':
            protocol = 'CredSSP'

        else:
            protocol = 'SPNEGO'

        return f'application/HTTP-{protocol}-session-encrypted'


class _SyncWinRMTransport(httpcore.SyncConnectionPool):

    def _create_connection(self, *args, **kwargs):
        connection = super()._create_connection(*args, **kwargs)
        orig_close = connection.close

        def new_close():
            orig_close()
            raise _NoAuthenticationContext()
        connection.close = new_close()
        return connection


class _NoAuthenticationContext(Exception):
    # Used to notify the http client that we need to send a blank request for encryption.
    pass


def get_tls_server_end_point_hash(certificate_der: bytes) -> bytes:
    backend = default_backend()

    cert = x509.load_der_x509_certificate(certificate_der, backend)
    try:
        hash_algorithm = cert.signature_hash_algorithm
    except UnsupportedAlgorithm:
        hash_algorithm = None

    # If the cert signature algorithm is unknown, md5, or sha1 then use sha256 otherwise use the signature
    # algorithm of the cert itself.
    if not hash_algorithm or hash_algorithm.name in ['md5', 'sha1']:
        digest = hashes.Hash(hashes.SHA256(), backend)
    else:
        digest = hashes.Hash(hash_algorithm, backend)

    digest.update(certificate_der)
    certificate_hash = digest.finalize()

    return certificate_hash


def _select_protocol(
        auth_header: str,
        protocol: str
) -> str:
    auth_header_l = auth_header.lower()
    selected_protocol = auth_header_l

    if auth_header_l != protocol:
        if protocol == 'negotiate':
            # The protocol specified by the user was negotiate but the server did not response with Negotiate.
            # When creating the auth context use the protocol explicitly set by the server (Kerberos or NTLM).
            selected_protocol = auth_header_l

        elif auth_header_l == 'negotiate':
            # The server specified it supports Negotiate but the user wants either NTLM or Kerberos. Use what the
            # user prefers as it should work with Negotiate.
            selected_protocol = protocol

        else:
            raise ValueError("Server responded with the auth protocol '%s' which is incompatible with the "
                             "specified auth_provider '%s'" % (auth_header, protocol))

    return selected_protocol


def _valid_auth_headers(
        www_authenticate: str,
        accepted_protocols: typing.List[str]
) -> str:
    matched_protocols = [p for p in accepted_protocols if p.lower() in www_authenticate.lower()]
    if not matched_protocols:
        raise Exception("The server did not response with one of the following authentication methods %s - "
                        "actual: '%s'" % (", ".join(accepted_protocols), www_authenticate))

    return matched_protocols[0]


class WSManConnectionBase(metaclass=abc.ABCMeta):
    """The WSManConnection contract.

    This is the WSManConnection contract that defines what is required for a WSMan IO class to be used by this library.
    """

    async def __aenter__(self):
        """ Implements 'async with' for the WSMan connection. """
        await self.open()
        return self

    def __enter__(self):
        """ Implements 'with' for the WSMan connection. """
        self.open()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """ Implements the closing method for 'async with' for the WSMan connection. """
        await self.close()

    def __exit__(self, exc_type, exc_val, exc_tb):
        """ Implements the closing method for 'with' for the WSMan connection. """
        self.close()

    @abc.abstractmethod
    def send(
            self,
            data: bytes,
    ) -> bytes:
        """Send WSMan data to the endpoint.

        The WSMan envelope is sent as a HTTP POST request to the endpoint specified. This method should deal with the
        encryption required for a request if it is necessary.

        Args:
            data: The WSMan envelope to send to the endpoint.

        Returns:
            bytes: The WSMan response.
        """
        pass

    @abc.abstractmethod
    def open(self):
        """Opens the WSMan connection.

        Opens the WSMan connection and sets up the connection for sending any WSMan envelopes.
        """
        pass

    @abc.abstractmethod
    def close(self):
        """Closes the WSMan connection.

        Closes the WSMan connection and any sockets/connections that are in use.
        """
        pass


class AsyncWSManConnection(WSManConnectionBase):

    def __init__(
            self,
            connection_uri: str,
            encryption: str = 'auto',
            verify: typing.Union[str, bool] = True,
            connection_timeout: int = 30,
            read_timeout: int = 30,
            # TODO reconnection and proxy settings

            auth: str = 'negotiate',
            username: typing.Optional[str] = None,
            password: typing.Optional[str] = None,

            # Cert auth
            certificate_pem: typing.Optional[str] = None,
            certificate_key_pem: typing.Optional[str] = None,
            certificate_password: typing.Optional[str] = None,

            # SPNEGO
            negotiate_service: str = 'HTTP',
            negotiate_hostname: typing.Optional[str] = None,
            negotiate_delegate: bool = False,
            send_cbt: bool = True,

            # CredSSP
            credssp_allow_tlsv1: bool = False,
            credssp_require_kerberos: bool = False,
    ):
        self.connection_uri = urlparse(connection_uri)
        self.username = username or ''
        self.auth = auth

        if encryption not in ["auto", "always", "never"]:
            raise ValueError("The encryption value '%s' must be auto, always, or never" % encryption)

        self.encrypt = {
            'auto': self.connection_uri.scheme == 'http',
            'always': True,
            'never': False,
        }[encryption]

        # Default for 'Accept-Encoding' is 'gzip, default' which normally doesn't matter on vanilla WinRM but for
        # Exchange endpoints hosted on IIS they actually compress it with 1 of the 2 algorithms. By explicitly setting
        # identity we are telling the server not to transform (compress) the data using the HTTP methods which we don't
        # support. https://tools.ietf.org/html/rfc7231#section-5.3.4
        headers = {
            'Accept-Encoding': 'identity',
            'User-Agent': 'Python PSRP Client',
        }

        client_kwargs = {}
        ssl_context = httpx.create_ssl_context(verify=verify)
        keepalive_expiry = 60.0

        transport = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            max_connections=1,
            max_keepalive_connections=1,
            keepalive_expiry=keepalive_expiry,
        )

        supported_auths = ['basic', 'certificate', 'negotiate', 'kerberos', 'ntlm', 'credssp']
        if auth not in supported_auths:
            raise ValueError("The specified auth '%s' is not supported, please select one of '%s'"
                             % (auth, ", ".join(supported_auths)))

        elif auth == 'basic':
            client_kwargs['auth'] = (username, password)

        elif auth == 'certificate':
            # TODO: Test password (3-tuple).
            headers['Authorization'] = 'http://schemas.dmtf.org/wbem/wsman/1/wsman/secprofile/https/mutual'
            client_kwargs['cert'] = (certificate_pem, certificate_key_pem, certificate_password)

        else:
            wsman_auth_kwargs = {
                'service': negotiate_service,
                'hostname_override': negotiate_hostname,
                'send_cbt': send_cbt,
                'delegate': negotiate_delegate,
                'credssp_allow_tlsv1': credssp_allow_tlsv1,
                'credssp_require_kerberos': credssp_require_kerberos,
            }
            transport = _AsyncWinRMTransport(
                ssl_context=ssl_context,
                keepalive_expiry=keepalive_expiry,
                username=username,
                password=password,
                protocol=auth,
                encryption_required=self.encrypt,
                **wsman_auth_kwargs,
            )

        # TODO: Proxy/SOCKS
        # TODO: Reconnection
        timeout = httpx.Timeout(max(connection_timeout, read_timeout), connect=connection_timeout, read=read_timeout)
        self._http = httpx.AsyncClient(headers=headers, timeout=timeout, transport=transport)

    async def send(
            self,
            data: bytes,
    ) -> bytes:
        a = ''
        response = await self._http.post(self.connection_uri.geturl(), content=data, headers={
            'Content-Type': 'application/soap+xml;charset=UTF-8',
        })

        content = await response.aread()
        await response.aclose()

        if response.status_code != 200 and not content:
            response.raise_for_status()

        return content

    async def open(self):
        await self._http.__aenter__()

    async def close(self):
        await self._http.aclose()


class WSManConnection(WSManConnectionBase):

    def send(
            self,
            data: bytes,
    ):
        pass

    def open(self):
        self._http.__enter__()
        if self.encrypt:
            self.send(b'')

    def close(self):
        pass


def _decrypt_wsman(
        data: bytes,
        content_type: str,
        context,
) -> bytes:
    boundary = re.search('boundary=[''|\\"](.*)[''|\\"]', content_type).group(1)
    # Talking to Exchange endpoints gives a non-compliant boundary that has a space between the --boundary.
    # not ideal but we just need to handle it.
    parts = re.compile((r"--\s*%s\r\n" % re.escape(boundary)).encode()).split(data)
    parts = list(filter(None, parts))

    content = []
    for i in range(0, len(parts), 2):
        header = parts[i].strip()
        payload = parts[i + 1]

        expected_length = int(header.split(b"Length=")[1])

        # remove the end MIME block if it exists
        payload = re.sub((r'--\s*%s--\r\n$' % boundary).encode(), b'', payload)

        wrapped_data = payload.replace(b"\tContent-Type: application/octet-stream\r\n", b"")

        header_length = struct.unpack("<i", wrapped_data[:4])[0]
        b_header = wrapped_data[4:4 + header_length]
        b_enc_data = wrapped_data[4 + header_length:]
        unwrapped_data = context.unwrap_winrm(b_header, b_enc_data)
        actual_length = len(unwrapped_data)

        if actual_length != expected_length:
            raise Exception("The encrypted length from the server does not match the expected length, "
                            "decryption failed, actual: %d != expected: %d"
                            % (actual_length, expected_length))
        content.append(unwrapped_data)

    return b"".join(content)


def _encrypt_wsman(
        data: bytes,
        content_type: str,
        encryption_type: str,
        context,
) -> typing.Tuple[bytes, str]:
    boundary = 'Encrypted Boundary'

    # If using CredSSP we must encrypt in 16KiB chunks.
    max_size = 16384 if 'CredSSP' in encryption_type else len(data)
    chunks = [data[i:i + max_size] for i in range(0, len(data), max_size)]

    encrypted_chunks = []
    for chunk in chunks:
        enc_details = context.wrap_winrm(chunk)
        padding_length = enc_details.padding_length
        wrapped_data = struct.pack("<i", len(enc_details.header)) + enc_details.header + enc_details.data
        chunk_length = str(len(chunk) + padding_length)

        content = "\r\n".join([
            '--%s' % boundary,
            '\tContent-Type: %s' % encryption_type,
            '\tOriginalContent: type=%s;Length=%s' % (content_type, chunk_length),
            '--%s' % boundary,
            '\tContent-Type: application/octet-stream',
            '',
        ])
        encrypted_chunks.append(content.encode() + wrapped_data)

    content_sub_type = 'multipart/encrypted' if len(encrypted_chunks) == 1 else 'multipart/x-multi-encrypted'
    content_type = '%s;protocol="%s";boundary="%s"' % (content_sub_type, encryption_type, boundary)
    data = b"".join(encrypted_chunks) + ("--%s--\r\n" % boundary).encode()

    return data, content_type
