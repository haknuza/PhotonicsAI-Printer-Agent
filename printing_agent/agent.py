"""Structured specialist delegations using the existing guarded mock controller.

No camera, language provider, SSH connection or physical printer is started.
"""
import copy
import json
from pathlib import Path
import sys
import threading

from . import CONTRACT_VERSION, SPECIALIST_ID

# The preserved printer component uses sibling imports and is not a Python package.
PRINTER_ROOT = Path(__file__).resolve().parents[1] / "photonics-printer-mcp-main"
if str(PRINTER_ROOT) not in sys.path:
    sys.path.insert(0, str(PRINTER_ROOT))
from printer_client import MockPrinter  # noqa: E402
from printer_commands import PrinterCommands  # noqa: E402


class DelegationError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def manifest():
    return json.loads((Path(__file__).with_name("manifest.json")).read_text())


class PrintingAgent:
    """One persistent process per mock printer; task IDs scope tokens and jobs.

    operator_approval is supplied by the trusted router's operator interface,
    never inferred from a model response or a user's prose.
    """

    def __init__(self, mode="mock"):
        if mode != "mock":
            raise ValueError("The PhotonicsAI Printer Agent supports mock mode only")
        self._printer = MockPrinter()
        self._commands = PrinterCommands(self._printer, homing_mode="manual")
        self._token_owners = {}
        self._job_owners = {}
        self._lock = threading.RLock()

    def close(self):
        self._commands.close()

    def response(self, task_id, status, result=None, error=None):
        return {"version": CONTRACT_VERSION, "specialist": SPECIALIST_ID,
                "task_id": task_id, "ok": error is None, "simulated": True,
                "status": status, "result": result, "error": error}

    def failure(self, code, message, task_id=None):
        return self.response(task_id, "error", error={"code": code, "message": message,
                             "retryable": False, "details": {}})

    def handle(self, delegation):
        task_id = None
        try:
            if isinstance(delegation, dict):
                candidate = delegation.get("task_id")
                if isinstance(candidate, str) and 0 < len(candidate) <= 128 and candidate.strip():
                    task_id = candidate
            self._validate(delegation)
            request = copy.deepcopy(delegation["request"])
            if request.get("command") == "describe":
                if set(request) != {"command"}:
                    raise DelegationError("INVALID_REQUEST", "describe accepts no other fields")
                return self.response(task_id, "completed", manifest())
            # Validate before ownership lookups so unhashable tokens/commands fail cleanly.
            self._commands.validate(request)
            with self._lock:
                return self._dispatch(task_id, request, delegation.get("operator_approval"))
        except ValueError as exc:
            return self.failure(getattr(exc, "code", "INVALID_REQUEST"), str(exc), task_id)
        except (TypeError, KeyError, RecursionError):
            return self.failure("INVALID_REQUEST", "Invalid delegation fields", task_id)

    def _validate(self, delegation):
        if not isinstance(delegation, dict):
            raise DelegationError("INVALID_REQUEST", "Delegation must be a JSON object")
        if set(delegation) - {"version", "specialist", "task_id", "request", "operator_approval"}:
            raise DelegationError("INVALID_REQUEST", "Unsupported delegation fields")
        if type(delegation.get("version")) is not int or delegation["version"] != CONTRACT_VERSION:
            raise DelegationError("VERSION_UNSUPPORTED", "Expected version=1")
        if delegation.get("specialist") != SPECIALIST_ID:
            raise DelegationError("WRONG_SPECIALIST", "Expected specialist=3d-printing")
        task_id = delegation.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip() or len(task_id) > 128:
            raise DelegationError("INVALID_REQUEST", "task_id must be a nonblank string of at most 128 characters")
        request = delegation.get("request")
        if not isinstance(request, dict):
            raise DelegationError("INVALID_REQUEST", "request must be a command object")
        if "operator_confirmed" in request:
            raise DelegationError("INVALID_REQUEST", "Operator confirmation belongs in operator_approval")
        if "operator_approval" in delegation:
            approval = delegation["operator_approval"]
            if (not isinstance(approval, dict)
                    or set(approval) != {"confirmed", "clear_path"}
                    or any(type(v) is not bool for v in approval.values())):
                raise DelegationError("INVALID_REQUEST", "operator_approval requires boolean confirmed and clear_path")
            if (request.get("command") not in ("move", "home")
                    or request.get("preview") is not False):
                raise DelegationError("INVALID_REQUEST", "Approval is accepted only on a move/home execution")

    def _dispatch(self, task_id, request, approval):
        # Retain ownership only while the controller retains each resource.
        self._token_owners = {k: v for k, v in self._token_owners.items() if k in self._commands.tokens}
        self._job_owners = {k: v for k, v in self._job_owners.items() if k in self._commands.jobs}
        command = request["command"]
        executing = command in ("move", "home") and request.get("preview", True) is False
        if executing:
            if not approval or approval["confirmed"] is not True or approval["clear_path"] is not True:
                raise DelegationError("CONFIRMATION_REQUIRED", "Explicit approval of the displayed path and clearance is required")
            owner = self._token_owners.get(request.get("token"))
            if owner is not None and owner != task_id:
                raise DelegationError("TASK_MISMATCH", "Preview belongs to another task")
            request["operator_confirmed"] = True
        if command == "job":
            owner = self._job_owners.get(request["job_id"])
            if owner is not None and owner != task_id:
                raise DelegationError("TASK_MISMATCH", "Job belongs to another task")
        reply = self._commands.execute(request)
        data = reply.get("data") or {}
        if reply["ok"] and reply["status"] == "previewed":
            self._token_owners[data["token"]] = task_id
        if reply["ok"] and reply["status"] == "accepted":
            self._job_owners[data["job_id"]] = task_id
        status = {"previewed": "awaiting_approval", "accepted": "running", "idle": "completed"}.get(
            reply["status"], reply["status"])
        return self.response(task_id, status, reply, reply["error"])
