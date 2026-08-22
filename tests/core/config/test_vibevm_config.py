from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from vibe.core.config import VibeVMConfig
from vibe.core.config.layers.environment import EnvironmentLayer
from vibe.core.config.vibe_schema import VibeConfigSchema


def test_defaults_are_disabled_with_default_budget() -> None:
    config = VibeVMConfig()

    assert config.enabled is False
    assert config.context_budget == 100_000
    assert config.evict_target_ratio == 0.8
    assert config.min_page_tokens == 400
    assert config.protect_recent_turns == 2


def test_vibe_config_schema_wires_in_vibevm_defaults() -> None:
    schema = VibeConfigSchema()

    assert schema.vibevm.enabled is False
    assert schema.vibevm.context_budget == 100_000


@pytest.mark.asyncio
async def test_toml_vibevm_section_round_trips_through_schema(tmp_path: Path) -> None:
    toml_path = tmp_path / "config.toml"
    toml_path.write_text(
        """\
[vibevm]
enabled = true
context_budget = 20000
"""
    )

    from vibe.core.config.layers.user import UserConfigLayer
    from vibe.core.config.orchestrator import ConfigOrchestrator

    layer = UserConfigLayer(path=toml_path)
    orchestrator = await ConfigOrchestrator[VibeConfigSchema].create(
        schema=VibeConfigSchema, layers=[layer], default_layer_resolver=lambda: layer
    )

    assert orchestrator.config.vibevm.enabled is True
    assert orchestrator.config.vibevm.context_budget == 20000


@pytest.mark.asyncio
async def test_env_var_overrides_vibevm_context_budget() -> None:
    env = {"VIBE_VIBEVM__CONTEXT_BUDGET": "20000"}
    with patch.dict(os.environ, env, clear=True):
        layer = EnvironmentLayer(schema=VibeConfigSchema)
        data = await layer.load()

    assert data.model_dump() == {"vibevm": {"context_budget": 20000}}
