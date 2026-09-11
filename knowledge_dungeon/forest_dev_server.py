"""Run the real private authority with an isolated development save directory."""

import argparse
import signal
from pathlib import Path
from threading import Event

from .private_bridge import KnowledgeDungeonPrivateBridge


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    args = parser.parse_args()
    stopped = Event()
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    bridge = KnowledgeDungeonPrivateBridge(args.database, runtime_dir=args.runtime_dir)
    bridge.start()
    print(f"Forest authority ready. Rendezvous: {bridge.rendezvous_path}", flush=True)
    try:
        stopped.wait()
    finally:
        if not bridge.stop():
            raise RuntimeError("bridge shutdown did not complete")


if __name__ == "__main__":
    main()
