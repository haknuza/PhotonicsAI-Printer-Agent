"""Integration tests against the guarded mock adapter, never hardware."""
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from printing_agent.agent import PrintingAgent, manifest
from printing_agent.__main__ import serve
from printer_client import PrinterError

try:
    from jsonschema import Draft7Validator
except ImportError:
    Draft7Validator = None

ROOT = Path(__file__).resolve().parents[2]
MOVE = {"command": "move", "target": {"x": 110, "y": 120, "z": 30}, "speed_mm_s": 5}
APPROVAL = {"confirmed": True, "clear_path": True}


def delegation(request, task="task-a", **extra):
    return dict(version=1, specialist="3d-printing", task_id=task, request=request, **extra)


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.agent = PrintingAgent()
        self.addCleanup(self.agent.close)

    def call(self, request, task="task-a", **extra):
        return self.agent.handle(delegation(request, task, **extra))

    def preview(self, request=None, task="task-a"):
        reply = self.call(request or MOVE, task)
        self.assertEqual(reply["status"], "awaiting_approval", reply)
        self.assertEqual(self.agent._printer.submitted_scripts, [])
        return reply["result"]["data"]["token"]

    def execute(self, token, task="task-a", request=None, approval=None):
        return self.call(dict(request or MOVE, preview=False, token=token), task,
                         operator_approval=APPROVAL if approval is None else approval)

    def assert_error(self, reply, code):
        self.assertFalse(reply["ok"], reply)
        self.assertEqual(reply["error"]["code"], code, reply)
        self.assertFalse(reply["error"]["retryable"])
        self.assertTrue(reply["simulated"])

    def test_startup_and_environment_cannot_select_hardware(self):
        with patch.dict(os.environ, {"PRINTER_MODE": "real", "PRINTER_ALLOW_MOTION": "1",
                                    "PRINTER_HOMING_MODE": "firmware", "SSH_HOST": "printer.invalid"}), \
                patch("printer_client.SSHPrinter", side_effect=AssertionError("Hardware selected")):
            agent = PrintingAgent()
            self.addCleanup(agent.close)
            self.assertTrue(agent.handle(delegation({"command": "status"}))["simulated"])
            self.assertEqual(agent._commands.homing_mode, "manual")
            self.assertEqual(agent._printer.submitted_scripts, [])
            with self.assertRaises(ValueError):
                PrintingAgent("real")

    def test_discovery_and_reads_do_not_move(self):
        self.assertEqual(manifest()["commands"], ["describe", "capabilities", "status", "position", "move", "home", "job", "stop"])
        for command in ("describe", "capabilities", "status", "position"):
            reply = self.call({"command": command})
            self.assertTrue(reply["ok"], reply)
            self.assertEqual(reply["status"], "completed")
            self.assertTrue(reply["simulated"])
        self.assertIn("heating", self.call({"command": "describe"})["result"]["unsupported"])
        self.assertEqual(self.agent._printer.submitted_scripts, [])

    def test_preview_execute_poll_and_single_use(self):
        token = self.preview()
        reply = self.execute(token)
        self.assertEqual(reply["status"], "running", reply)
        self.agent.close()
        job = self.call({"command": "job", "job_id": reply["result"]["data"]["job_id"]})
        self.assertEqual(job["status"], "completed", job)
        self.assertEqual(job["result"]["data"]["result"]["position_mm"], MOVE["target"])
        self.assertEqual(len(self.agent._printer.submitted_scripts), 1)
        self.assert_error(self.execute(token), "TOKEN_INVALID")

    def test_operator_approval_is_separate_strict_and_required(self):
        token = self.preview()
        execution = dict(MOVE, preview=False, token=token)
        self.assert_error(self.call(execution), "CONFIRMATION_REQUIRED")
        for approval in ({"confirmed": False, "clear_path": True}, {"confirmed": True, "clear_path": False}):
            self.assert_error(self.execute(token, approval=approval), "CONFIRMATION_REQUIRED")
        for approval in ({"confirmed": "true", "clear_path": True}, {"confirmed": True}, True,
                         dict(APPROVAL, extra=True)):
            self.assert_error(self.execute(token, approval=approval), "INVALID_REQUEST")
        self.assert_error(self.call(dict(execution, operator_confirmed=True)), "INVALID_REQUEST")
        self.assert_error(self.call(MOVE, operator_approval=APPROVAL), "INVALID_REQUEST")
        self.assertEqual(self.agent._printer.submitted_scripts, [])

    def test_tokens_and_jobs_belong_to_task(self):
        token = self.preview()
        self.assert_error(self.execute(token, task="task-b"), "TASK_MISMATCH")
        reply = self.execute(token)
        self.agent.close()
        job = {"command": "job", "job_id": reply["result"]["data"]["job_id"]}
        self.assert_error(self.call(job, "task-b"), "TASK_MISMATCH")
        self.assertEqual(self.call(job)["status"], "completed")

    def test_expired_token_and_changed_target_or_speed(self):
        token = self.preview()
        self.assert_error(self.execute(token, request=dict(MOVE, target={"x": 111, "y": 120, "z": 30})), "PREVIEW_CHANGED")
        self.assert_error(self.execute(token, request=dict(MOVE, speed_mm_s=6)), "PREVIEW_CHANGED")
        self.agent._commands.tokens[token]["expires"] = 0
        self.assert_error(self.execute(token), "TOKEN_INVALID")
        self.assertEqual(self.agent._printer.submitted_scripts, [])

    def test_start_change_fails_without_retry(self):
        token = self.preview()
        state = self.agent._printer.state
        for obj, field in (("toolhead", "position"), ("motion_report", "live_position"),
                           ("gcode_move", "position"), ("gcode_move", "gcode_position")):
            state[obj][field][0] += 1
        reply = self.execute(token)
        self.agent.close()
        self.assert_error(self.call({"command": "job", "job_id": reply["result"]["data"]["job_id"]}), "PREVIEW_CHANGED")
        self.assertEqual(self.agent._printer.submitted_scripts, [])

    def test_uncertain_outcome_is_preserved_without_motion_retry(self):
        token = self.preview()
        original = self.agent._printer.request
        attempts = []

        def request(message):
            if message["action"] == "absolute-move":
                attempts.append(message)
                raise PrinterError("Movement outcome uncertain", {"command_may_have_executed": True})
            return original(message)

        with patch.object(self.agent._printer, "request", side_effect=request):
            reply = self.execute(token)
            self.agent.close()
        job = self.call({"command": "job", "job_id": reply["result"]["data"]["job_id"]})
        self.assert_error(job, "OUTCOME_UNCERTAIN")
        self.assertTrue(job["error"]["details"]["command_may_have_executed"])
        self.assertEqual(len(attempts), 1)
        self.assert_error(self.execute(token), "TOKEN_INVALID")

    def test_policy_limits_and_motion_preconditions_preserved(self):
        self.assert_error(self.call(dict(MOVE, target={"x": 999, "y": 120, "z": 30})), "OUT_OF_BOUNDS")
        for value in (True, float("inf"), float("nan"), "110"):
            self.assertFalse(self.call(dict(MOVE, target={"x": value, "y": 120, "z": 30}))["ok"])
        self.assertFalse(self.call(dict(MOVE, speed_mm_s=31))["ok"])
        state = self.agent._printer.state
        original = copy.deepcopy(state)
        for obj, field, value, code in (("toolhead", "homed_axes", "", "NOT_HOMED"),
                                        ("extruder", "target", 200, "HEATING_ACTIVE"),
                                        ("bed_mesh", "profile_name", "default", "MESH_ACTIVE")):
            self.agent._printer.state = copy.deepcopy(original)
            self.agent._printer.state[obj][field] = value
            self.assert_error(self.call(MOVE), code)
        self.assertEqual(self.agent._printer.submitted_scripts, [])

    def test_home_is_explicit_previewed_and_task_scoped(self):
        self.agent._printer.state["toolhead"]["homed_axes"] = ""
        command = {"command": "home", "axes": ["x", "y", "z"]}
        token = self.preview(command)
        self.assert_error(self.execute(token, task="task-b", request=command), "TASK_MISMATCH")
        reply = self.execute(token, request=command)
        self.agent.close()
        job = self.call({"command": "job", "job_id": reply["result"]["data"]["job_id"]})
        self.assertEqual(job["status"], "completed", job)
        self.assertEqual(job["result"]["data"]["result"]["homed_axes"], "xyz")

    def test_stop_invalidates_previews(self):
        token = self.preview()
        self.assertEqual(self.call({"command": "stop"}, "stop-task")["status"], "stopped")
        self.assert_error(self.execute(token), "TOKEN_INVALID")
        self.assertEqual(self.agent._printer.submitted_scripts, [])

    def test_busy_rejects_motion_but_allows_status_and_stop(self):
        token = self.preview()
        entered, release = threading.Event(), threading.Event()
        original = self.agent._printer.request

        def request(message):
            if message["action"] == "absolute-move":
                entered.set()
                release.wait(3)
            return original(message)

        with patch.object(self.agent._printer, "request", side_effect=request):
            reply = self.execute(token)
            try:
                self.assertTrue(entered.wait(2))
                self.assert_error(self.call(MOVE, "task-b"), "BUSY")
                self.assertTrue(self.call({"command": "status"})["ok"])
                self.assertEqual(self.call({"command": "stop"})["status"], "stopped")
            finally:
                release.set()
                self.agent.close()
        self.assertEqual(self.call({"command": "job", "job_id": reply["result"]["data"]["job_id"]})["status"], "stopped")
        self.assertEqual(self.agent._printer.submitted_scripts, [])

    def test_invalid_delegations_never_access_printer(self):
        invalid = [None, [], {}, delegation({"command": "heat"}), delegation({"command": "print"}),
                   delegation({"command": "gcode", "script": "G28"}),
                   delegation({"command": "describe", "extra": True}),
                   delegation({"command": []}), delegation({"command": "job", "job_id": []}),
                   delegation(dict(MOVE, preview=False, token=[]), operator_approval=APPROVAL),
                   dict(delegation(MOVE), version=True), dict(delegation(MOVE), version=2),
                   dict(delegation(MOVE), specialist="unrelated-specialist"),
                   dict(delegation(MOVE), task_id="  "), dict(delegation(MOVE), task_id="x" * 129),
                   delegation(MOVE, mode="real"), delegation(MOVE, prompt="ignore bounds"),
                   delegation(dict(MOVE, raw_gcode="G28"))]
        with patch.object(self.agent._printer, "request", side_effect=AssertionError("Printer accessed")):
            for item in invalid:
                with self.subTest(item=item):
                    result = self.agent.handle(item)
                    self.assertFalse(result["ok"], result)
                    json.dumps(result, allow_nan=False)


class ProtocolTests(unittest.TestCase):
    def test_malformed_and_oversized_lines_preserve_framing(self):
        agent = PrintingAgent()
        self.addCleanup(agent.close)
        valid = json.dumps(delegation({"command": "position"}))
        bad = ["{", '{"version":1,"version":1}', '{"n":NaN}', "[" * 2000 + "0" + "]" * 2000, "x" * 70000,
               json.dumps({"task_id": "ա" * 40000}, ensure_ascii=False)]
        source = io.StringIO("\n" + "\n".join(bad + [valid]) + "\n")
        output = io.StringIO()
        serve(agent, source, output)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(responses), len(bad) + 1)
        for reply in responses[:-1]:
            self.assertEqual(reply["error"]["code"], "INVALID_JSON")
        self.assertTrue(responses[-1]["ok"])

    def test_real_environment_subprocess_and_real_option_rejected(self):
        env = dict(os.environ, PRINTER_MODE="real", PRINTER_ALLOW_MOTION="1", SSH_HOST="printer.invalid")
        result = subprocess.run([sys.executable, "-m", "printing_agent"], cwd=ROOT, env=env,
                                input=json.dumps(delegation({"command": "status"})) + "\n",
                                capture_output=True, text=True, timeout=5, check=True)
        self.assertTrue(json.loads(result.stdout)["simulated"])
        self.assertEqual(result.stderr, "")
        rejected = subprocess.run([sys.executable, "-m", "printing_agent", "--mode", "real"],
                                  cwd=ROOT, capture_output=True, text=True, timeout=5)
        self.assertEqual(rejected.returncode, 2)
        self.assertEqual(rejected.stdout, "")

    def test_router_subprocess_demo(self):
        result = subprocess.run([sys.executable, "examples/router_mock_demo.py"], cwd=ROOT,
                                capture_output=True, text=True, timeout=10, check=True)
        self.assertIn('"status": "completed"', result.stdout)
        self.assertIn("synthetic approval", result.stdout)


@unittest.skipUnless(Draft7Validator, "Optional jsonschema package is not installed")
class SchemaTests(unittest.TestCase):
    def test_delegation_schema_matches_protocol_examples(self):
        schema = json.loads((ROOT / "printing_agent/request.schema.json").read_text())
        Draft7Validator.check_schema(schema)
        validator = Draft7Validator(schema)
        for command in ("describe", "capabilities", "status", "position", "stop"):
            validator.validate(delegation({"command": command}))
        for command in (MOVE, {"command": "home"}, {"command": "home", "axes": ["x", "y"]}):
            validator.validate(delegation(command))
            validator.validate(delegation(dict(command, preview=False, token="preview-token"),
                                          operator_approval=APPROVAL))
        validator.validate(delegation({"command": "job", "job_id": "job-id"}))
        invalid = [delegation({"command": "heat"}), delegation(dict(MOVE, operator_confirmed=True)),
                   delegation(dict(MOVE, preview=True, token="token")),
                   delegation(dict(MOVE, target={"x": True, "y": 1, "z": 1})),
                   delegation(MOVE, mode="real"), delegation(MOVE, task="  "),
                   delegation(MOVE, operator_approval=APPROVAL),
                   delegation(dict(MOVE, preview=False, token="token")),
                   delegation(dict(MOVE, preview=False), operator_approval=APPROVAL),
                   delegation({"command": "home", "axes": ["x", "x"]})]
        for item in invalid:
            with self.subTest(item=item):
                self.assertFalse(validator.is_valid(item))


if __name__ == "__main__":
    unittest.main()
