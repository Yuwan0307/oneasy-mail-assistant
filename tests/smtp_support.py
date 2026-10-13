import smtplib


SMTP_CONFIG = {'host': 'smtp.example.com', 'port': 465, 'security': 'ssl',
               'address': 'notify@example.com', 'password': 'local-test-secret'}


class SMTPServer:
    """Controlled network boundary; the app builds and submits real EmailMessages."""
    def __init__(self):
        self.messages = []
        self.failure = None
        self.calls = []

    def connect(self, host, port, **kwargs):
        self.calls.append(('connect', host, port, kwargs))
        return self

    def __enter__(self): return self
    def __exit__(self, *args): pass
    def ehlo(self): self.calls.append(('ehlo',))
    def starttls(self, **kwargs): self.calls.append(('starttls', kwargs))
    def login(self, username, password): self.calls.append(('login', username, password))

    def send_message(self, message, from_addr=None, to_addrs=None):
        self.calls.append(('envelope', from_addr, to_addrs))
        if self.failure: raise self.failure
        self.messages.append(message)
        return {}
