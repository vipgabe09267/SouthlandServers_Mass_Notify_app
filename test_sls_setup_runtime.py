"""Shutdown identity fencing and setup UI scheduling; no real process is stopped."""
import ctypes
from pathlib import Path
import threading
import unittest
from unittest import mock

import sls_installer as installer
import sls_install_windows as windows
import sls_mass_notify as client
from test_sls_client_reliability import IsolatedClientTest


class ProcessShutdownTests(unittest.TestCase):
    def setUp(self):
        self.path = Path("C:/Program Files/Test/App.exe").resolve()
        self.enterContext(mock.patch.object(windows.time, "sleep"))
        self.close = self.enterContext(mock.patch.object(windows, "request_window_close"))
        self.enterContext(mock.patch.object(windows, "process_parents", return_value={20: 10, 10: 1}))

    def test_already_stopped_does_not_send_commands(self):
        with mock.patch.object(windows, "exact_processes", return_value=[]):
            windows.close_exact_processes(self.path, force=True)
        self.close.assert_not_called()

    def test_graceful_exit_never_terminates(self):
        with mock.patch.object(windows, "exact_processes", side_effect=[[10, 20], []]), \
             mock.patch.object(windows, "terminate_exact_process") as terminate:
            windows.close_exact_processes(self.path, force=True)
        self.close.assert_called_once_with({10, 20})
        terminate.assert_not_called()

    def test_stuck_child_is_stopped_before_onefile_parent(self):
        alive = {10, 20}
        order = []
        def terminate(pid, path):
            self.assertEqual(path, self.path)
            order.append(pid)
            alive.discard(pid)
        with mock.patch.object(windows, "exact_processes", side_effect=lambda _: sorted(alive)), \
             mock.patch.object(windows, "terminate_exact_process", side_effect=terminate):
            windows.close_exact_processes(self.path, timeout=0, force=True)
        self.assertEqual(order, [20, 10])

    def test_termination_requires_explicit_force_policy(self):
        with mock.patch.object(windows, "exact_processes", return_value=[10]), \
             mock.patch.object(windows, "terminate_exact_process") as terminate:
            with self.assertRaisesRegex(RuntimeError, "process IDs.*10"):
                windows.close_exact_processes(self.path, timeout=0)
        terminate.assert_not_called()

    def test_access_denial_keeps_precise_failure(self):
        with mock.patch.object(windows, "exact_processes", return_value=[20]), \
             mock.patch.object(windows, "terminate_exact_process", side_effect=PermissionError("process 20 access denied")):
            with self.assertRaisesRegex(PermissionError, "process 20 access denied"):
                windows.close_exact_processes(self.path, timeout=0, force=True)

    def test_parent_order_is_cycle_safe(self):
        self.assertEqual(set(windows.children_first([1, 2, 3], {1: 2, 2: 1, 3: 2})), {1, 2, 3})

    def kernel(self, image):
        kernel = mock.Mock()
        kernel.OpenProcess.return_value = 42
        kernel.WaitForSingleObject.side_effect = [258, 0]
        def query(_handle, _flags, buffer, _length):
            buffer.value = image
            return True
        kernel.QueryFullProcessImageNameW.side_effect = query
        def long_path(source, buffer, _length):
            buffer.value = source
            return len(source)
        kernel.GetLongPathNameW.side_effect = long_path
        kernel.TerminateProcess.return_value = True
        return kernel

    def test_reused_pid_or_same_filename_elsewhere_is_not_terminated(self):
        kernel = self.kernel(str(self.path.parent.parent / "Other" / "App.exe"))
        with mock.patch.object(ctypes, "WinDLL", return_value=kernel, create=True):
            self.assertFalse(windows.terminate_exact_process(123, self.path))
        kernel.TerminateProcess.assert_not_called()
        kernel.CloseHandle.assert_called_once_with(42)

    def test_termination_uses_same_validated_handle_and_waits_for_exit(self):
        kernel = self.kernel(str(self.path))
        with mock.patch.object(ctypes, "WinDLL", return_value=kernel, create=True):
            self.assertTrue(windows.terminate_exact_process(123, self.path))
        kernel.TerminateProcess.assert_called_once_with(42, 0)
        kernel.WaitForSingleObject.assert_called_with(42, 5000)
        kernel.CloseHandle.assert_called_once_with(42)


class SetupOperationTests(unittest.TestCase):
    def test_blocked_work_keeps_ui_callbacks_on_caller_thread(self):
        root = mock.Mock()
        release, entered = threading.Event(), threading.Event()
        callbacks = []
        operation = installer.SetupOperation(root, lambda message: callbacks.append((threading.get_ident(), message)),
                                             lambda result, error: callbacks.append((threading.get_ident(), result, error)))
        def work(report):
            report("Waiting for shutdown")
            entered.set()
            release.wait(5)
            return "installed"
        operation.start(work)
        try:
            self.assertTrue(entered.wait(1))
            self.assertTrue(operation.active)
            self.assertEqual(callbacks, [])
            operation.poll()
            self.assertEqual(callbacks, [(threading.get_ident(), "Waiting for shutdown")])
            with self.assertRaises(RuntimeError):
                operation.start(work)
        finally:
            release.set()
            operation.thread.join(2)
        operation.poll()
        self.assertFalse(operation.active)
        self.assertEqual(callbacks[-1], (threading.get_ident(), "installed", None))

    def test_worker_failure_is_delivered_to_ui_for_retry(self):
        completed = mock.Mock()
        operation = installer.SetupOperation(mock.Mock(), mock.Mock(), completed)
        operation.start(lambda _report: (_ for _ in ()).throw(PermissionError("file locked")))
        operation.thread.join(2)
        completed.assert_not_called()
        operation.poll()
        self.assertFalse(operation.active)
        self.assertIsInstance(completed.call_args.args[1], PermissionError)

    def test_busy_window_close_does_not_interrupt_transaction(self):
        for window_class in (installer.InstallerWindow, installer.UninstallerWindow):
            window = window_class.__new__(window_class)
            window.root = mock.Mock()
            window.status = mock.Mock()
            window.operation = mock.Mock(active=True)
            window.close()
            window.root.destroy.assert_not_called()
            window.operation.active = False
            window.close()
            window.root.destroy.assert_called_once()


class ApplicationShutdownTests(IsolatedClientTest):
    def test_blocked_transport_stop_cannot_block_tk_or_exceed_exit_deadline(self):
        entered, release = threading.Event(), threading.Event()
        stop_threads = []
        def stop():
            stop_threads.append(threading.get_ident())
            entered.set()
            release.wait(5)
        worker = mock.Mock()
        worker.stop.side_effect = stop
        self.instance.workers = {0: worker}
        self.instance.root = mock.Mock()
        try:
            with mock.patch.object(client.time, "monotonic", return_value=100):
                self.instance.shutdown()
                self.assertTrue(entered.wait(1))
                self.assertNotEqual(stop_threads[0], threading.get_ident())
                self.instance.root.destroy.assert_not_called()
                poll = self.instance.root.after.call_args.args[1]
            with mock.patch.object(client.time, "monotonic", return_value=104):
                poll()
            self.instance.root.destroy.assert_called_once()
        finally:
            release.set()
            self.assertTrue(self.instance._shutdown_ready.wait(1))
