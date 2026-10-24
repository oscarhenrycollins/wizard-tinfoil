"""Small real RFC6455 test client; only the test's downstream socket uses this."""
import json
import socket
from wsproto import WSConnection, ConnectionType
from wsproto.events import Request, AcceptConnection, TextMessage, BytesMessage, CloseConnection, Ping

class Client:
    def __init__(self, port, grant, extra_headers=()):
        self.socket = socket.create_connection(('127.0.0.1', port), timeout=45)
        self.ws = WSConnection(ConnectionType.CLIENT)
        self.pending = []
        self.socket.sendall(self.ws.send(Request(host=f'127.0.0.1:{port}',target='/v1/live',
            extra_headers=[(b'x-wizard-grant', grant.encode()), *extra_headers])))
        self.ws.receive_data(self.socket.recv(65536))
        events = list(self.ws.events())
        if not any(isinstance(e,AcceptConnection) for e in events): raise ValueError('Upgrade refused')
        self.pending.extend(json.loads(e.data) for e in events if isinstance(e,TextMessage))
    def send(self, frame):
        event = BytesMessage(data=frame) if isinstance(frame,bytes) else TextMessage(data=json.dumps(frame))
        self.socket.sendall(self.ws.send(event))
    def receive(self):
        while True:
            if self.pending: return self.pending.pop(0)
            data = self.socket.recv(65536)
            if not data: raise EOFError()
            self.ws.receive_data(data)
            for event in self.ws.events():
                if isinstance(event,TextMessage): self.pending.append(json.loads(event.data))
                elif isinstance(event,Ping): self.socket.sendall(self.ws.send(event.response()))
                elif isinstance(event,CloseConnection):
                    try: self.socket.sendall(self.ws.send(event.response()))
                    except OSError: pass
                    self.pending.append({'socket_closed':event.code})
    def close(self): self.socket.close()
