"""Case (v): file cycle pkgs <-> pkgs.sub; LCA is an endpoint, so no package cycle."""
from pkgs.sub import sub_value


def top_value() -> int:
    return sub_value() + 1
