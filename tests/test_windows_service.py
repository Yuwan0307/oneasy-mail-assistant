"""Service lifecycle tests with pywin32 and all operating-system effects simulated."""
import importlib.util
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class FakeServiceFramework:
    def __init__(self, args):
        self.statuses = []

    def ReportServiceStatus(self, status):
        self.statuses.append(status)


class FakeListener:
    def __init__(self):
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def setsockopt(self, *args):
        pass

    def bind(self, address):
        self.address = address

    def listen(self, backlog):
        pass


class FakeServer:
    def __init__(self):
        self.should_exit = False
        self.runs = 0
        self.on_run = None

    def run(self, sockets):
        self.runs += 1
        if self.on_run:
            self.on_run()


class WindowsServiceStopTest(unittest.TestCase):
    def setUp(self):
        scratch = Path.cwd() / '.test-temp'
        scratch.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        specification = importlib.util.spec_from_file_location(
            'app._windows_service_stop_test', Path(__file__).resolve().parents[1] / 'app/windows_service.py')
        self.module = importlib.util.module_from_spec(specification)
        fake_modules = {
            'win32event': SimpleNamespace(),
            'win32service': SimpleNamespace(SERVICE_STOP_PENDING=3),
            'win32serviceutil': SimpleNamespace(ServiceFramework=FakeServiceFramework)}
        with patch.dict(sys.modules, fake_modules):
            specification.loader.exec_module(self.module)
        self.server = FakeServer()
        self.listener = FakeListener()
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(self.module, 'ROOT', self.root))
        stack.enter_context(patch.object(self.module.os, 'chdir'))
        stack.enter_context(patch.object(self.module.logging, 'basicConfig'))
        stack.enter_context(patch.object(self.module.socket, 'SO_EXCLUSIVEADDRUSE', 4, create=True))
        stack.enter_context(patch.object(self.module.socket, 'socket', return_value=self.listener))
        self.initialize = stack.enter_context(patch.object(self.module, 'initialize_config', return_value={}))
        stack.enter_context(patch.object(self.module, 'Store', return_value=object()))
        stack.enter_context(patch.object(self.module, 'create_app', return_value=object()))
        stack.enter_context(patch.object(self.module.uvicorn, 'Config', return_value=object()))
        self.make_server = stack.enter_context(patch.object(self.module.uvicorn, 'Server', return_value=self.server))
        self.service = self.module.MailService(['OneasyMailAssistant'])

    def test_stop_received_before_server_creation_prevents_server_start(self):
        self.service.SvcStop()
        self.service.SvcDoRun()
        self.assertEqual(self.server.runs, 0)
        self.assertTrue(self.listener.closed)
        self.assertEqual(self.service.statuses, [3])

    def test_stop_during_config_initialization_is_not_lost(self):
        def initialize(path):
            self.service.SvcStop()
            return {}
        self.initialize.side_effect = initialize
        self.service.SvcDoRun()
        self.assertEqual(self.server.runs, 0)
        self.assertTrue(self.listener.closed)

    def test_stop_during_server_construction_is_not_lost(self):
        def construct(config):
            self.service.SvcStop()
            return self.server
        self.make_server.side_effect = construct
        self.service.SvcDoRun()
        self.assertEqual(self.server.runs, 0)
        self.assertTrue(self.listener.closed)

    def test_stop_of_running_server_requests_normal_uvicorn_shutdown(self):
        self.server.on_run = self.service.SvcStop
        self.service.SvcDoRun()
        self.assertEqual(self.server.runs, 1)
        self.assertTrue(self.server.should_exit)
        self.assertEqual(self.service.statuses, [3])
        self.assertTrue(self.listener.closed)

    def test_a_new_service_instance_starts_after_another_instance_was_stopped(self):
        self.service.SvcStop()
        replacement = self.module.MailService(['OneasyMailAssistant'])
        replacement.SvcDoRun()
        self.assertEqual(self.server.runs, 1)
        self.assertFalse(self.server.should_exit)


if __name__ == '__main__':
    unittest.main()
