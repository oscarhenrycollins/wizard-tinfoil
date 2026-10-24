"""Downstream WebSocket framing; upstream sockets belong exclusively to the SDK."""
import base64
import json
from pathlib import Path
import socket
import subprocess
import threading

from wsproto import WSConnection, ConnectionType
from wsproto.events import AcceptConnection, BytesMessage, TextMessage, CloseConnection, Ping
from wsproto.utilities import LocalProtocolError, RemoteProtocolError


def serve(handler, key):
    ws = WSConnection(ConnectionType.SERVER)
    lock = threading.RLock()
    ended = threading.Event()
    process = None

    def send(event):
        with lock:
            handler.connection.sendall(ws.send(event))

    def terminal(code):
        if ended.is_set(): return
        try:
            send(TextMessage(data=json.dumps({'type': 'error', 'code': code, 'message': 'Private transcription unavailable.'})))
            send(CloseConnection(code=1000))
        except (OSError, LocalProtocolError): pass
        ended.set()
        try: handler.connection.shutdown(socket.SHUT_RD)
        except OSError: pass

    try:
        headers = [(k.encode('ascii'), v.encode('latin-1')) for k, v in handler.headers.items()]
        ws.initiate_upgrade_connection(headers, handler.path)
        list(ws.events())
        send(AcceptConnection())
        handler.close_connection = True
        if key is None:
            terminal('signed_out'); return
        process = subprocess.Popen(['node', str(Path(__file__).with_name('realtime.mjs'))],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        process.stdin.write(json.dumps({'type': 'init', 'key': key}) + '\n'); process.stdin.flush()

        def forward():
            try:
                for line in process.stdout:
                    event = json.loads(line)
                    if event.get('type') not in ('ready', 'delta', 'final', 'error', 'closed'): raise ValueError()
                    send(TextMessage(data=json.dumps(event)))
                    if event['type'] in ('error', 'closed'):
                        send(CloseConnection(code=1000)); ended.set()
                        handler.connection.shutdown(socket.SHUT_RD)
                        return
                terminal('provider_error')
            except (OSError, ValueError, LocalProtocolError): terminal('provider_error')

        threading.Thread(target=forward, daemon=True).start()
        # Idle timeout is not an utterance duration cap; finalization has its own SDK watchdog.
        handler.connection.settimeout(180)
        fragment, kind = b'', None
        while not ended.is_set():
            data = handler.rfile.read1(65536)
            if not data: break
            with lock:
                ws.receive_data(data)
                events = list(ws.events())
            for event in events:
                if ended.is_set(): break
                if isinstance(event, CloseConnection):
                    send(event.response()); ended.set(); break
                if isinstance(event, Ping): send(event.response()); continue
                if not isinstance(event, (TextMessage, BytesMessage)): continue
                current = 'text' if isinstance(event, TextMessage) else 'audio'
                if kind and kind != current: terminal('provider_error'); break
                kind = current
                fragment += event.data.encode() if current == 'text' else event.data
                if len(fragment) > (4096 if current == 'text' else 65536):
                    terminal('unsupported_format' if current == 'audio' else 'limit'); break
                if not event.message_finished: continue
                if current == 'text':
                    frame = json.loads(fragment)
                    if not isinstance(frame, dict) or frame.get('type') not in ('start', 'commit', 'stop'):
                        terminal('provider_error'); break
                else: frame = {'type': 'audio', 'audio': base64.b64encode(fragment).decode()}
                process.stdin.write(json.dumps(frame) + '\n'); process.stdin.flush()
                fragment, kind = b'', None
    except (OSError, ValueError, LocalProtocolError, RemoteProtocolError): terminal('provider_error')
    finally:
        ended.set()
        if process is not None:
            if process.poll() is None: process.kill()
            process.wait()
            process.stdin.close(); process.stdout.close()
