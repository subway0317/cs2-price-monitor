from __future__ import annotations

import argparse

from cs2_price_monitor.db import init_db


def main() -> None:
    """Command line entrypoint."""
    parser = argparse.ArgumentParser(description="CS2 price monitor tools")
    parser.add_argument("command", choices=["init-db"], help="Command to run")
    args = parser.parse_args()

    if args.command == "init-db":
        init_db()
        print("Database initialized.")


if __name__ == "__main__":
    main()
