#!/usr/bin/env python
# Copyright (c) 2026 Jifeng Wu
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from __future__ import print_function

import argparse
import logging
import signal
import socket
import sys
import threading
import time

from enum import Enum
from typing import Callable, Optional, Tuple, Union

import paramiko  # type: ignore[import-untyped]

if sys.version_info[0] == 2:
    TEXT_TYPE = unicode  # type: ignore[name-defined]
    BINARY_TYPES = (str,)
else:
    TEXT_TYPE = str
    BINARY_TYPES = (bytes,)


class ConnState(Enum):
    STARTING = "STARTING"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RECONNECT_WAIT = "RECONNECT_WAIT"
    RECONNECTING = "RECONNECTING"
    SHUTTING_DOWN = "SHUTTING_DOWN"
    STOPPED = "STOPPED"


class RetryableConnectError(Exception):
    __slots__ = ()


class FatalConnectError(Exception):
    __slots__ = ()


class Runtime(object):
    __slots__ = (
        "mode",
        "host",
        "port",
        "username",
        "password",
        "publickey",
        "local_port",
        "remote_port",
        "local_only",
        "state",
        "transport",
        "transport_sock",
        "last_error",
        "stop_requested",
        "backoff",
        "max_backoff",
        "lock",
        "forward_active",
        "forward_port",
        "listener",
        "accept_thread",
    )

    def __init__(
        self,
        mode,
        host,
        port,
        username,
        password,
        publickey,
        local_port,
        remote_port,
        local_only=False,
    ):
        # type: (str, str, int, str, Optional[str], Optional[paramiko.PKey], int, int, bool) -> None
        self.mode = mode  # type: str
        self.host = host  # type: str
        self.port = port  # type: int
        self.username = username  # type: str
        self.password = password  # type: Optional[str]
        self.publickey = publickey  # type: Optional[paramiko.PKey]
        self.local_port = local_port  # type: int
        self.remote_port = remote_port  # type: int
        self.local_only = local_only  # type: bool
        self.state = ConnState.STARTING  # type: ConnState
        self.transport = None  # type: Optional[paramiko.Transport]
        self.transport_sock = None  # type: Optional[socket.socket]
        self.last_error = None  # type: Optional[Exception]
        self.stop_requested = False  # type: bool
        self.backoff = 1  # type: int
        self.max_backoff = 10  # type: int
        self.lock = threading.RLock()
        self.forward_active = False  # type: bool
        self.forward_port = None  # type: Optional[int]
        self.listener = None  # type: Optional[socket.socket]
        self.accept_thread = None  # type: Optional[threading.Thread]


class SignalStopHandler(object):
    __slots__ = ("ctx",)

    def __init__(self, ctx):
        # type: (Runtime) -> None
        self.ctx = ctx  # type: Runtime

    def __call__(self, signum, frame):
        # type: (int, object) -> None
        del frame
        # Only set the flag here — do NOT call set_state() or logging,
        # because logging acquires a lock which may be held by the
        # interrupted thread, causing a deadlock.
        self.ctx.stop_requested = True


def set_state(ctx, new_state, reason=""):
    # type: (Runtime, ConnState, str) -> None
    old_state = ctx.state
    ctx.state = new_state
    if reason:
        logging.info("[state] %s -> %s: %s" % (old_state.value, new_state.value, reason))
    else:
        logging.info("[state] %s -> %s" % (old_state.value, new_state.value))


def connect_upstream(ctx):
    # type: (Runtime) -> None
    try:
        sock = socket.create_connection((ctx.host, ctx.port))
        transport = paramiko.Transport(sock)
        transport.start_client()
        transport.set_keepalive(15)

        if ctx.publickey is not None:
            transport.auth_publickey(ctx.username, ctx.publickey)
        else:
            transport.auth_password(ctx.username, str(ctx.password))

        if not transport.is_authenticated():
            raise FatalConnectError("upstream authentication failed")

        with ctx.lock:
            ctx.transport_sock = sock
            ctx.transport = transport
    except paramiko.AuthenticationException as error:
        raise FatalConnectError(str(error))
    except ValueError as error:
        raise FatalConnectError(str(error))
    except Exception as error:
        raise RetryableConnectError(str(error))


def close_transport(ctx):
    # type: (Runtime) -> None
    with ctx.lock:
        transport = ctx.transport
        transport_sock = ctx.transport_sock
        ctx.transport = None
        ctx.transport_sock = None
    if transport is not None:
        try:
            transport.close()
        except Exception:
            logging.info("[cleanup] ignored upstream transport close failure")
    if transport_sock is not None:
        try:
            transport_sock.close()
        except Exception:
            logging.info("[cleanup] ignored upstream socket close failure")


def transport_is_alive(ctx):
    # type: (Runtime) -> bool
    with ctx.lock:
        transport = ctx.transport
    return transport is not None and transport.is_active()


def get_transport(ctx):
    # type: (Runtime) -> Optional[paramiko.Transport]
    with ctx.lock:
        return ctx.transport


def install_signal_handlers(ctx):
    # type: (Runtime) -> None
    handler = SignalStopHandler(ctx)
    signal.signal(signal.SIGINT, handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handler)


def make_thread(target, args, name):
    # type: (Callable[..., None], Tuple, str) -> threading.Thread
    thread = threading.Thread(target=target, args=args, name=name)  # type: ignore[call-arg]
    thread.daemon = True
    return thread


def safe_close(channel):
    # type: (Optional[paramiko.Channel]) -> None
    if channel is None:
        return
    try:
        channel.close()
    except Exception:
        logging.info("[cleanup] ignored channel close failure")


def safe_close_socket(sock):
    # type: (Optional[socket.socket]) -> None
    if sock is None:
        return
    try:
        sock.close()
    except Exception:
        logging.info("[cleanup] ignored socket close failure")


# ---------------------------------------------------------------------------
# Push helpers
# ---------------------------------------------------------------------------


def push_forwarded_handler(ctx, channel, origin, server):
    # type: (Runtime, paramiko.Channel, Tuple[str, int], Tuple[str, int]) -> None
    del origin
    del server

    def run_bridge():
        # type: () -> None
        local_sock = None  # type: Optional[socket.socket]
        try:
            local_sock = socket.create_connection(("localhost", ctx.local_port), timeout=10)
            bidirectional_bridge(local_sock, channel)
        except Exception as error:
            errno_val = getattr(error, "errno", None)
            if errno_val == 111:  # ECONNREFUSED
                logging.info(
                    "[push] Connection refused: nothing is listening on localhost:%s. "
                    "Check that your local service is running (e.g. 'ss -tlnp | grep %s' or 'netstat -tlnp | grep %s')."
                    % (ctx.local_port, ctx.local_port, ctx.local_port)
                )
            else:
                logging.info("[push] failed to bridge incoming forwarded connection: %s" % (error,))
        finally:
            safe_close(channel)
            safe_close_socket(local_sock)

    bridge_thread = make_thread(run_bridge, (), "push-bridge")
    bridge_thread.start()


def activate_forward(ctx):
    # type: (Runtime) -> bool
    transport = get_transport(ctx)
    if transport is None or not transport.is_active():
        return False

    bind_host = "localhost" if ctx.local_only else ""
    try:
        active_port = transport.request_port_forward(
            bind_host,
            ctx.remote_port,
            handler=lambda channel, origin, server: push_forwarded_handler(
                ctx, channel, origin, server
            ),
        )
        with ctx.lock:
            ctx.forward_active = True
            ctx.forward_port = active_port
        logging.info(
            "[push] remote port forwarding active: %s:%s -> localhost:%s"
            % (
                "localhost" if ctx.local_only else "0.0.0.0",
                active_port,
                ctx.local_port,
            )
        )
        return True
    except Exception as error:
        logging.info(
            "[push] failed to request remote forward %s:%s: %s"
            % (
                bind_host if bind_host else "0.0.0.0",
                ctx.remote_port,
                error,
            )
        )
        return False


def cancel_forward(ctx):
    # type: (Runtime) -> None
    with ctx.lock:
        forward_port = ctx.forward_port
        ctx.forward_active = False
        ctx.forward_port = None
    transport = get_transport(ctx)
    if transport is not None and transport.is_active() and forward_port is not None:
        try:
            bind_host = "localhost" if ctx.local_only else ""
            transport.cancel_port_forward(bind_host, forward_port)
            logging.info("[push] cancelled remote forward %s" % (forward_port,))
        except Exception:
            logging.info("[cleanup] ignored remote forward cancel failure")


# ---------------------------------------------------------------------------
# Pull helpers
# ---------------------------------------------------------------------------


def bind_local_listener(local_port):
    # type: (int) -> socket.socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("localhost", local_port))
    sock.listen()
    return sock


def close_listener(ctx):
    # type: (Runtime) -> None
    listener = ctx.listener
    if listener is not None:
        try:
            listener.close()
        except Exception:
            logging.info("[cleanup] ignored listener close failure")
        ctx.listener = None


def accept_loop(ctx):
    # type: (Runtime) -> None
    listener = ctx.listener
    if listener is None:
        raise RuntimeError("listener not initialized")
    while not ctx.stop_requested:
        try:
            client_sock, client_addr = listener.accept()
        except OSError as e:
            # On Python 2, EINTR is not auto-retried; on Python 3 it is
            # (PEP 475), but checking errno is harmless on both.
            if hasattr(e, "errno") and e.errno == 4:  # errno.EINTR
                continue
            break
        except IOError as e:
            if hasattr(e, "errno") and e.errno == 4:
                continue
            break
        thread = make_thread(
            handle_pull_client,
            (ctx, client_sock, client_addr),
            "pull-client-%s" % (client_addr[1],),
        )
        thread.start()


def handle_pull_client(ctx, client_sock, client_addr):
    # type: (Runtime, socket.socket, Tuple[str, int]) -> None
    channel = None  # type: Optional[paramiko.Channel]
    try:
        transport = get_transport(ctx)
        if transport is None or not transport.is_active():
            return

        channel = transport.open_channel(
            kind="direct-tcpip",
            dest_addr=("localhost", ctx.remote_port),
            src_addr=client_addr,
        )
        if channel is None:
            return

        logging.info(
            "[pull] forwarding localhost:%s -> remote localhost:%s for %s:%s"
            % (
                ctx.local_port,
                ctx.remote_port,
                client_addr[0],
                client_addr[1],
            )
        )
        bidirectional_bridge(client_sock, channel)
    except Exception as error:
        logging.info("[pull] failed to bridge connection: %s" % (error,))
    finally:
        safe_close(channel)
        safe_close_socket(client_sock)


# ---------------------------------------------------------------------------
# Shared: bidirectional byte pump
# ---------------------------------------------------------------------------


def pump_bytes(src, dst):
    # type: (Union[paramiko.Channel, socket.socket], Union[paramiko.Channel, socket.socket]) -> None
    buf_size = 32768
    try:
        while True:
            data = src.recv(buf_size)
            if not data:
                break
            dst.sendall(data)
    except Exception:
        pass
    finally:
        try:
            if isinstance(dst, paramiko.Channel):
                dst.shutdown_write()
            else:
                dst.shutdown(socket.SHUT_WR)
        except Exception:
            pass


def bidirectional_bridge(left, right):
    # type: (socket.socket, paramiko.Channel) -> None
    left_to_right = make_thread(pump_bytes, (left, right), "bridge-left-to-right")
    right_to_left = make_thread(pump_bytes, (right, left), "bridge-right-to-left")
    left_to_right.start()
    right_to_left.start()
    left_to_right.join()
    right_to_left.join()


# ---------------------------------------------------------------------------
# Main application: unified state machine for push / pull
# ---------------------------------------------------------------------------


def app(ctx):
    # type: (Runtime) -> int
    install_signal_handlers(ctx)

    while True:
        # Check for stop_requested in every iteration so the main thread
        # (not the signal handler) drives the state transition, keeping
        # logging calls signal-safe.
        if ctx.stop_requested and ctx.state not in (
            ConnState.SHUTTING_DOWN,
            ConnState.STOPPED,
        ):
            set_state(ctx, ConnState.SHUTTING_DOWN, "stop requested")

        if ctx.state == ConnState.STARTING:
            if ctx.mode == "pull":
                ctx.listener = bind_local_listener(ctx.local_port)
                ctx.accept_thread = make_thread(accept_loop, (ctx,), "accept-loop")
                ctx.accept_thread.start()
            set_state(ctx, ConnState.CONNECTING, "%s mode ready" % (ctx.mode,))

        elif ctx.state == ConnState.CONNECTING:
            try:
                connect_upstream(ctx)
                ctx.backoff = 1
                logging.info(
                    "Connected upstream to %s:%s as %s"
                    % (ctx.host, ctx.port, ctx.username)
                )

                if ctx.mode == "push":
                    if not activate_forward(ctx):
                        close_transport(ctx)
                        set_state(ctx, ConnState.RECONNECT_WAIT, "forward activation failed")
                        continue
                    access_str = "open on localhost only" if ctx.local_only else "open on all interfaces"
                    logging.info(
                        "Pushing localhost:%s on your machine to %s (%s) via ssh -p %s %s@%s (Press Ctrl+C to stop)"
                        % (
                            ctx.local_port,
                            ctx.remote_port,
                            access_str,
                            ctx.port,
                            ctx.username,
                            ctx.host,
                        )
                    )
                else:
                    logging.info(
                        "Pulling localhost:%s on ssh -p %s %s@%s to localhost:%s on your machine (Press Ctrl+C to stop)"
                        % (
                            ctx.remote_port,
                            ctx.port,
                            ctx.username,
                            ctx.host,
                            ctx.local_port,
                        )
                    )
                set_state(ctx, ConnState.CONNECTED, "upstream connect ok")
            except RetryableConnectError as error:
                ctx.last_error = error
                set_state(ctx, ConnState.RECONNECT_WAIT, "connect failed: %s" % (error,))
            except FatalConnectError as error:
                logging.error("Fatal: %s" % (error,))
                return 1

        elif ctx.state == ConnState.CONNECTED:
            if ctx.stop_requested:
                if ctx.mode == "push":
                    cancel_forward(ctx)
                set_state(ctx, ConnState.SHUTTING_DOWN, "shutdown requested")
            elif not transport_is_alive(ctx):
                if ctx.mode == "push":
                    cancel_forward(ctx)
                close_transport(ctx)
                set_state(ctx, ConnState.RECONNECT_WAIT, "upstream lost")
            else:
                time.sleep(0.2)

        elif ctx.state == ConnState.RECONNECT_WAIT:
            if ctx.stop_requested:
                set_state(ctx, ConnState.SHUTTING_DOWN, "shutdown requested during reconnect wait")
            else:
                delay = ctx.backoff
                logging.info("[reconnect] waiting %ss" % (delay,))
                time.sleep(delay)
                ctx.backoff = min(ctx.backoff * 2, ctx.max_backoff)
                set_state(ctx, ConnState.RECONNECTING, "retrying upstream connect")

        elif ctx.state == ConnState.RECONNECTING:
            try:
                connect_upstream(ctx)
                ctx.backoff = 1
                if ctx.mode == "push":
                    if not activate_forward(ctx):
                        close_transport(ctx)
                        set_state(ctx, ConnState.RECONNECT_WAIT, "reconnected but forward activation failed")
                        continue
                    access_str = "open on localhost only" if ctx.local_only else "open on all interfaces"
                    logging.info(
                        "Reconnected. Pushing localhost:%s to %s (%s) via ssh -p %s %s@%s"
                        % (
                            ctx.local_port,
                            ctx.remote_port,
                            access_str,
                            ctx.port,
                            ctx.username,
                            ctx.host,
                        )
                    )
                else:
                    logging.info(
                        "Reconnected. Pulling localhost:%s on ssh -p %s %s@%s to localhost:%s"
                        % (
                            ctx.remote_port,
                            ctx.port,
                            ctx.username,
                            ctx.host,
                            ctx.local_port,
                        )
                    )
                set_state(ctx, ConnState.CONNECTED, "upstream reconnected")
            except RetryableConnectError as error:
                ctx.last_error = error
                set_state(ctx, ConnState.RECONNECT_WAIT, "reconnect failed: %s" % (error,))
            except FatalConnectError as error:
                logging.error("Fatal: %s" % (error,))
                return 1

        elif ctx.state == ConnState.SHUTTING_DOWN:
            if ctx.mode == "push":
                cancel_forward(ctx)
            else:
                close_listener(ctx)
            close_transport(ctx)
            set_state(ctx, ConnState.STOPPED, "cleanup complete")

        elif ctx.state == ConnState.STOPPED:
            return 0

        else:
            raise RuntimeError("Unhandled state: %s" % (ctx.state,))


def main():
    # type: () -> int
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(
        description="Dead simple, jargon-free Python tool to make a local TCP port available on a remote host or make a remote TCP port available locally, with auto-reconnect."
    )
    subparsers = parser.add_subparsers(dest="mode", help="available commands")

    push_parser = subparsers.add_parser(
        "push",
        help="push localhost:<local_port> to <remote_port> on the remote SSH host (ssh -R equivalent)",
    )
    push_parser.add_argument(
        "--host",
        required=True,
        help="upstream SSH server host name or address",
    )
    push_parser.add_argument(
        "--port",
        required=False,
        type=int,
        default=22,
        help="upstream SSH server port number (default: 22)",
    )
    push_parser.add_argument(
        "--username",
        required=True,
        help="upstream SSH username",
    )
    push_auth = push_parser.add_mutually_exclusive_group(required=True)
    push_auth.add_argument(
        "--password",
        help="password used to connect to the remote SSH server",
    )
    push_auth.add_argument(
        "--rsa-key",
        help="user-facing path to the RSA private key used for the upstream SSH server",
    )
    push_auth.add_argument(
        "--ed25519-key",
        help="user-facing path to the Ed25519 private key used for the upstream SSH server",
    )
    push_parser.add_argument(
        "--local-port",
        required=True,
        type=int,
        help="local TCP port to push from",
    )
    push_parser.add_argument(
        "--remote-port",
        required=True,
        type=int,
        help="remote TCP port to push to",
    )
    push_parser.add_argument(
        "--local-only",
        action="store_true",
        default=False,
        help="open remote port on localhost only",
    )

    pull_parser = subparsers.add_parser(
        "pull",
        help="pull localhost:<remote_port> on the remote SSH host to localhost:<local_port> (ssh -L equivalent)",
    )
    pull_parser.add_argument(
        "--host",
        required=True,
        help="upstream SSH server host name or address",
    )
    pull_parser.add_argument(
        "--port",
        required=False,
        type=int,
        default=22,
        help="upstream SSH server port number (default: 22)",
    )
    pull_parser.add_argument(
        "--username",
        required=True,
        help="upstream SSH username",
    )
    pull_auth = pull_parser.add_mutually_exclusive_group(required=True)
    pull_auth.add_argument(
        "--password",
        help="password used to connect to the remote SSH server",
    )
    pull_auth.add_argument(
        "--rsa-key",
        help="user-facing path to the RSA private key used for the upstream SSH server",
    )
    pull_auth.add_argument(
        "--ed25519-key",
        help="user-facing path to the Ed25519 private key used for the upstream SSH server",
    )
    pull_parser.add_argument(
        "--local-port",
        required=True,
        type=int,
        help="local TCP port to pull to",
    )
    pull_parser.add_argument(
        "--remote-port",
        required=True,
        type=int,
        help="remote TCP port to pull from",
    )

    args = parser.parse_args()

    if args.mode not in ("push", "pull"):
        parser.print_help()
        return 1

    password = args.password  # type: Optional[str]
    publickey = None  # type: Optional[paramiko.PKey]
    if args.rsa_key is not None:
        publickey = paramiko.RSAKey.from_private_key_file(args.rsa_key)
    elif args.ed25519_key is not None:
        publickey = paramiko.Ed25519Key.from_private_key_file(args.ed25519_key)

    if password is None and publickey is None:
        parser.print_help()
        return 1

    ctx = Runtime(
        mode=args.mode,
        host=args.host,
        port=args.port,
        username=args.username,
        password=password,
        publickey=publickey,
        local_port=args.local_port,
        remote_port=args.remote_port,
        local_only=getattr(args, "local_only", False),
    )
    return app(ctx)


if __name__ == "__main__":
    sys.exit(main())
