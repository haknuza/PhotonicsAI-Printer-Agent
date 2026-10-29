"""Example router client. Approval here is a synthetic mock-only test fixture."""
import json
from pathlib import Path
import subprocess
import sys
import time


def main():
    root = Path(__file__).resolve().parents[1]
    with subprocess.Popen([sys.executable, "-m", "printing_agent", "--mode", "mock"],
                          cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          text=True) as process:
        def call(request, approval=None):
            payload = {"version": 1, "specialist": "3d-printing", "task_id": "mock-demo", "request": request}
            if approval is not None:
                payload["operator_approval"] = approval
            process.stdin.write(json.dumps(payload) + "\n")
            process.stdin.flush()
            reply = json.loads(process.stdout.readline())
            if not reply["ok"] or reply["simulated"] is not True:
                raise RuntimeError(reply)
            return reply

        try:
            print("PhotonicsAI Printer Agent: simulated positioning; synthetic approval fixture")
            call({"command": "describe"})
            command = {"command": "move", "target": {"x": 110, "y": 120, "z": 30}, "speed_mm_s": 5}
            preview = call(command)
            print("Preview:", json.dumps(preview["result"]["data"]["plan"]))
            accepted = call(dict(command, preview=False, token=preview["result"]["data"]["token"]),
                            {"confirmed": True, "clear_path": True})
            job_id = accepted["result"]["data"]["job_id"]
            deadline = time.monotonic() + 5
            while True:
                reply = call({"command": "job", "job_id": job_id})
                if reply["status"] != "running":
                    print("Simulated result:", json.dumps(reply["result"]["data"]))
                    break
                if time.monotonic() > deadline:
                    call({"command": "stop"})
                    raise RuntimeError("Mock job did not finish within 5 seconds")
                time.sleep(0.01)
        finally:
            process.stdin.close()
        if process.wait(timeout=5):
            raise RuntimeError("Specialist subprocess failed")


if __name__ == "__main__":
    main()
