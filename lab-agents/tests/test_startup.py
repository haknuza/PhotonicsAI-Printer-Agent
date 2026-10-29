"""Hidden key entry and startup diagnostics, without cloud or printer calls."""
import argparse
import contextlib
import errno
import getpass
import io
import os
import socket
import unittest
from unittest.mock import patch

from app import prepare_startup


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.parser = argparse.ArgumentParser()
        self.args = argparse.Namespace(host='127.0.0.1', port=8770, prompt_groq_key=True)
        address = [(socket.AF_INET, socket.SOCK_STREAM, 0, '', ('127.0.0.1', 8770))]
        self.patches = [patch('app.socket.getaddrinfo', return_value=address),
                        patch('app.socket.socket'),
                        patch('app.sys.stdin.isatty', return_value=True),
                        patch.dict(os.environ, {'GROQ_API_KEY':'', 'MODEL_PROVIDER':'groq', 'GROQ_MODEL':'openai/gpt-oss-120b'})]
        self.mocks = [item.start() for item in self.patches]
        for item in self.patches:
            self.addCleanup(item.stop)

    def rejected(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output), self.assertRaises(SystemExit) as stopped:
            prepare_startup(self.args, self.parser)
        self.assertEqual(stopped.exception.code, 2)
        return output.getvalue()

    def test_prompt_sets_key_in_backend_without_printing_or_api_request(self):
        output = io.StringIO()
        fixture = 'fixture-private-input-for-startup'
        with patch('app.getpass.getpass', return_value='  '+fixture+'  '), \
                patch('urllib.request.OpenerDirector.open') as network, \
                contextlib.redirect_stdout(output):
            prepare_startup(self.args, self.parser)
        self.assertEqual(os.environ['GROQ_API_KEY'], fixture)
        self.assertNotIn(fixture, output.getvalue())
        self.assertIn('Groq key received', output.getvalue())
        network.assert_not_called()

    def test_empty_or_invalid_key_stops_instead_of_starting_unconfigured(self):
        for value in ('', '  ', 'fixture key with spaces'):
            with self.subTest(value=value), patch('app.getpass.getpass', return_value=value):
                message = self.rejected()
            self.assertIn('No valid nonempty key', message)
            self.assertEqual(os.environ['GROQ_API_KEY'], '')

    def test_busy_port_is_reported_before_requesting_key(self):
        self.mocks[1].return_value.__enter__.return_value.bind.side_effect = OSError(errno.EADDRINUSE, 'fixture')
        with patch('app.getpass.getpass') as prompt:
            message = self.rejected()
        self.assertIn('Port 8770 is already in use', message)
        prompt.assert_not_called()

    def test_noninteractive_input_cannot_fall_back_to_echoed_key_entry(self):
        self.mocks[2].return_value = False
        with patch('app.getpass.getpass') as prompt:
            self.assertIn('interactive terminal', self.rejected())
        prompt.assert_not_called()

    def test_cancelled_or_unhidden_input_does_not_start_server(self):
        for error in (EOFError(), KeyboardInterrupt(), getpass.GetPassWarning('fixture')):
            with self.subTest(error=type(error)), patch('app.getpass.getpass', side_effect=error):
                self.assertIn('cancelled or unavailable', self.rejected())

    def test_no_prompt_mode_preserves_existing_environment(self):
        self.args.prompt_groq_key = False
        with patch.dict(os.environ, {'GROQ_API_KEY':'fixture-existing'}), patch('app.getpass.getpass') as prompt:
            prepare_startup(self.args, self.parser)
            self.assertEqual(os.environ['GROQ_API_KEY'], 'fixture-existing')
        prompt.assert_not_called()

    def test_groq_key_is_not_requested_for_other_provider(self):
        with patch.dict(os.environ, {'MODEL_PROVIDER':'photonics'}), patch('app.getpass.getpass') as prompt:
            self.assertIn('requires MODEL_PROVIDER=groq', self.rejected())
        prompt.assert_not_called()

    def test_invalid_port_rejected(self):
        for port in (0, -1, 65536):
            self.args.port = port
            self.assertIn('between 1 and 65535', self.rejected())
