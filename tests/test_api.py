"""Tests for our own API exposition."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import griffe
import pytest
from mkdocstrings import Inventory

import cheaphelp

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(name="loader", scope="module")
def _fixture_loader() -> griffe.GriffeLoader:
    loader = griffe.GriffeLoader()
    loader.load("cheaphelp")
    loader.resolve_aliases()
    return loader


@pytest.fixture(name="internal_api", scope="module")
def _fixture_internal_api(loader: griffe.GriffeLoader) -> griffe.Module:
    return loader.modules_collection["cheaphelp._internal"]


@pytest.fixture(name="public_api", scope="module")
def _fixture_public_api(loader: griffe.GriffeLoader) -> griffe.Module:
    return loader.modules_collection["cheaphelp"]


def _yield_public_objects(
    obj: griffe.Module | griffe.Class,
    *,
    modules: bool = False,
    modulelevel: bool = True,
    inherited: bool = False,
    special: bool = False,
) -> Iterator[griffe.Object | griffe.Alias]:
    for member in obj.all_members.values() if inherited else obj.members.values():
        try:
            if member.is_module:
                if member.is_alias or not member.is_public:
                    continue
                if modules:
                    yield member
                yield from _yield_public_objects(
                    member,  # ty: ignore[invalid-argument-type]
                    modules=modules,
                    modulelevel=modulelevel,
                    inherited=inherited,
                    special=special,
                )
            elif member.is_public and (special or not member.is_special):
                yield member
            else:
                continue
            if member.is_class and not modulelevel:
                yield from _yield_public_objects(
                    member,  # ty: ignore[invalid-argument-type]
                    modules=modules,
                    modulelevel=False,
                    inherited=inherited,
                    special=special,
                )
        except (griffe.AliasResolutionError, griffe.CyclicAliasError):
            continue


@pytest.fixture(name="modulelevel_internal_objects", scope="module")
def _fixture_modulelevel_internal_objects(internal_api: griffe.Module) -> list[griffe.Object | griffe.Alias]:
    return list(_yield_public_objects(internal_api, modulelevel=True))


@pytest.fixture(name="internal_objects", scope="module")
def _fixture_internal_objects(internal_api: griffe.Module) -> list[griffe.Object | griffe.Alias]:
    return list(_yield_public_objects(internal_api, modulelevel=False, special=True))


@pytest.fixture(name="public_objects", scope="module")
def _fixture_public_objects(public_api: griffe.Module) -> list[griffe.Object | griffe.Alias]:
    return list(_yield_public_objects(public_api, modulelevel=False, inherited=True, special=True))


@pytest.fixture(name="inventory", scope="module")
def _fixture_inventory() -> Inventory:
    inventory_file = Path(__file__).parent.parent / "site" / "objects.inv"
    if not inventory_file.exists():
        pytest.skip("The objects inventory is not available.")
    with inventory_file.open("rb") as file:
        return Inventory.parse_sphinx(file)


# NOTE: The upstream copier-uv template ships three more API-surface checks here:
#   - test_exposed_objects: every public object in `_internal` must be re-exported
#     in `cheaphelp.__all__`.
#   - test_no_module_docstrings_in_internal_api: internal modules must have no
#     module docstrings.
#   - test_unique_names: every public object name must be unique across the whole
#     internal API.
# Those encode a *library* convention (a thin public package re-exporting a flat
# internal API). cheaphelp is an *application* with many genuinely-internal
# modules that we do not want to surface as a flat public API; per-module names
# like `build_prompt` / `apply_decision` recurring across role modules
# (responder, planner, worker, reviewer) is intentional and clearer. The module
# docstrings are also useful documentation. These checks were intentionally removed.


def test_single_locations(public_api: griffe.Module) -> None:
    """All objects have a single public location."""

    def _public_path(obj: griffe.Object | griffe.Alias) -> bool:
        return obj.is_public and (obj.parent is None or _public_path(obj.parent))

    multiple_locations = {}
    for obj_name in cheaphelp.__all__:
        obj = public_api[obj_name]
        if obj.aliases and (
            public_aliases := [path for path, alias in obj.aliases.items() if path != obj.path and _public_path(alias)]
        ):
            multiple_locations[obj.path] = public_aliases
    assert not multiple_locations, "Multiple public locations:\n" + "\n".join(
        f"{path}: {aliases}" for path, aliases in multiple_locations.items()
    )


def test_api_matches_inventory(inventory: Inventory, public_objects: list[griffe.Object | griffe.Alias]) -> None:
    """All public objects are added to the inventory."""
    ignore_names = {"__getattr__", "__init__", "__repr__", "__str__", "__post_init__"}
    not_in_inventory = [
        f"{obj.relative_filepath}:{obj.lineno}: {obj.path}"
        for obj in public_objects
        if obj.name not in ignore_names and obj.path not in inventory
    ]
    msg = "Objects not in the inventory (try running `make run zensical build --clean`):\n{paths}"
    assert not not_in_inventory, msg.format(paths="\n".join(sorted(not_in_inventory)))


def test_inventory_matches_api(
    inventory: Inventory,
    public_objects: list[griffe.Object | griffe.Alias],
    loader: griffe.GriffeLoader,
) -> None:
    """The inventory doesn't contain any additional Python object."""
    not_in_api = []
    public_api_paths = {obj.path for obj in public_objects}
    public_api_paths.add("cheaphelp")
    for item in inventory.values():
        if (
            item.domain == "py"
            and "(" not in item.name
            and (item.name == "cheaphelp" or item.name.startswith("cheaphelp."))
            and item.name != "cheaphelp.__all__"
        ):
            obj = loader.modules_collection[item.name]
            if obj.path not in public_api_paths and not any(path in public_api_paths for path in obj.aliases):
                not_in_api.append(item.name)
    msg = "Inventory objects not in public API (try running `make run zensical build --clean`):\n{paths}"
    assert not not_in_api, msg.format(paths="\n".join(sorted(not_in_api)))
