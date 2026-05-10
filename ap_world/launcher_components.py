"""Register the YGO LotD-LE client with the Archipelago Launcher so it shows
up alongside other game clients. Imported from __init__.py so registration
runs at apworld load time."""

from worlds.LauncherComponents import (
    Component,
    SuffixIdentifier,
    Type,
    components,
    launch_subprocess,
)


def _launch_ygo_client(*args: str) -> None:
    # Deferred import so CommonClient / pymem are only loaded when the user
    # actually launches the client (avoid slowing down generation).
    from .client.launch import launch_ygo_client

    launch_ygo_client(*args)


components.append(
    Component(
        "YGO LotD-LE Client",
        func=lambda *args: launch_subprocess(_launch_ygo_client, name="YGOLotDClient", args=args),
        component_type=Type.CLIENT,
        file_identifier=SuffixIdentifier(),
    )
)
