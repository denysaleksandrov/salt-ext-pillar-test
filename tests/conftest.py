import importlib.util
import pathlib
import sys

import pytest

PILLAR_DIR = pathlib.Path(__file__).resolve().parent.parent / "_pillar"

DEFAULT_OPTS = {
    "pillar_source_merging_strategy": "smart",
    "renderer": "yaml",
    "pillar_merge_lists": False,
}


def load_module(name, opts):
    """Import a _pillar module outside the Salt loader and inject dunders."""
    spec = importlib.util.spec_from_file_location(
        f"test_{name}", PILLAR_DIR / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.__opts__ = {**DEFAULT_OPTS, **opts}
    return module


@pytest.fixture(autouse=True)
def _reset_pg_registry():
    sys.modules.pop("_salt_pg_pillar_connections", None)
    yield
    sys.modules.pop("_salt_pg_pillar_connections", None)
