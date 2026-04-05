"""Public package surface for `kiso.brain`."""

from __future__ import annotations

import sys
from types import ModuleType

from . import common as _common
from . import consolidator as _consolidator
from . import curator as _curator
from . import planner as _planner
from . import prompts as _prompts
from . import reviewer as _reviewer
from . import text_roles as _text_roles

_MODULES = (
    _common,
    _planner,
    _reviewer,
    _curator,
    _text_roles,
    _consolidator,
    _prompts,
)

for _module in _MODULES:
    for _name in getattr(_module, "__brain_exports__", ()):
        globals()[_name] = getattr(_module, _name)

call_llm = _common.call_llm
KISO_DIR = _common.KISO_DIR
discover_tools = _planner.discover_tools
discover_connectors = _planner.discover_connectors
get_registry_tools = _planner.get_registry_tools
get_system_env = _planner.get_system_env
build_install_context = _planner.build_install_context
search_facts = _planner.search_facts
get_session = _text_roles.get_session
get_facts = _text_roles.get_facts
run_briefer = _common.run_briefer
_load_system_prompt = _common._load_system_prompt
_load_modular_prompt = _common._load_modular_prompt
build_planner_messages = _planner.build_planner_messages

__all__ = sorted(
    {
        name
        for module in _MODULES
        for name in getattr(module, "__brain_exports__", ())
        if not name.startswith("_")
    }
)

_PATCH_TARGETS: dict[str, tuple[ModuleType, ...]] = {
    "call_llm": (_common, _text_roles),
    "KISO_DIR": (_common,),
    "discover_tools": (_planner,),
    "discover_connectors": (_planner,),
    "get_registry_tools": (_planner,),
    "get_system_env": (_planner,),
    "build_install_context": (_planner,),
    "search_facts": (_planner,),
    "get_session": (_planner, _text_roles),
    "get_facts": (_text_roles,),
    "run_briefer": (_common, _planner),
    "_load_system_prompt": (
        _common,
        _planner,
        _reviewer,
        _curator,
        _text_roles,
        _consolidator,
    ),
    "_load_modular_prompt": (_common,),
    "build_planner_messages": (_planner,),
}


class _BrainModule(ModuleType):
    def __setattr__(self, name: str, value) -> None:
        super().__setattr__(name, value)
        for module in _PATCH_TARGETS.get(name, ()):
            setattr(module, name, value)


sys.modules[__name__].__class__ = _BrainModule

del _MODULES
del _common
del _consolidator
del _curator
del _module
del _name
del _planner
del _prompts
del _reviewer
del _text_roles
