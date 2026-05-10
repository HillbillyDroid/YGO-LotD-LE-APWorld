import asyncio
from collections.abc import Sequence

from CommonClient import get_base_parser, handle_url_arg


def launch_ygo_client(*args: Sequence[str]) -> None:
    from .ygo_client import main

    parser = get_base_parser()
    parser.add_argument("--name", default=None, help="Slot Name to connect as.")
    parser.add_argument(
        "--ygo-cloud-disabled-confirmed",
        action="store_true",
        help=(
            "Confirm that you have manually disabled Steam Cloud sync for "
            "Yu-Gi-Oh! Legacy of the Duelist: Link Evolution. Bypasses the "
            "automatic cloud-off detection (use only if detection misfires)."
        ),
    )
    parser.add_argument("url", nargs="?", help="Archipelago connection url")

    launch_args = handle_url_arg(parser.parse_args(args))

    asyncio.run(main(launch_args))
