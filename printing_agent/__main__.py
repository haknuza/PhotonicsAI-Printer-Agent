"""Run with python3 -m printing_agent --mode mock; JSON lines on stdin/stdout."""
import argparse
import json
import sys

from .agent import PrintingAgent
from printer_commands import parse_command

MAX_LINE_BYTES = 65536


def serve(agent, source, destination):
    while True:
        raw = source.readline(MAX_LINE_BYTES + 1)
        if not raw:
            return
        if len(raw.encode("utf-8")) > MAX_LINE_BYTES:
            # Drain the remainder as one rejected request, keeping framing intact.
            while raw and not raw.endswith("\n"):
                raw = source.readline(MAX_LINE_BYTES + 1)
            reply = agent.failure("INVALID_JSON", "Delegation exceeds 65536 bytes")
        elif not raw.strip():
            continue
        else:
            try:
                reply = agent.handle(parse_command(raw))
            except (ValueError, RecursionError) as exc:
                reply = agent.failure("INVALID_JSON", str(exc))
        destination.write(json.dumps(reply, allow_nan=False) + "\n")
        destination.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["mock"], default="mock")
    args = parser.parse_args(argv)
    agent = PrintingAgent(mode=args.mode)
    try:
        serve(agent, sys.stdin, sys.stdout)
    except KeyboardInterrupt:
        agent._commands.execute({"command": "stop"})
        return 130
    except BrokenPipeError:
        return 1
    finally:
        # EOF waits for accepted simulated work. Explicit stop is available separately.
        agent.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
