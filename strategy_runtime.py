from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from quant_platform_kit.strategy_contracts import (
    StrategyDecision,
    StrategyEntrypoint,
    StrategyRuntimeAdapter,
    build_strategy_context_from_available_inputs,
)

from strategy_loader import (
    load_strategy_entrypoint_for_profile,
    load_strategy_runtime_adapter_for_profile,
)


@dataclass(frozen=True)
class StrategyEvaluationResult:
    decision: StrategyDecision
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LoadedStrategyRuntime:
    entrypoint: StrategyEntrypoint
    runtime_adapter: StrategyRuntimeAdapter
    runtime_overrides: Mapping[str, Any] = field(default_factory=dict)
    merged_runtime_config: Mapping[str, Any] = field(default_factory=dict)

    @property
    def profile(self) -> str:
        return self.entrypoint.manifest.profile

    @property
    def managed_symbols(self) -> tuple[str, ...]:
        configured = self.merged_runtime_config.get("managed_symbols", ())
        return tuple(str(symbol) for symbol in configured)

    @property
    def benchmark_symbol(self) -> str:
        return str(self.merged_runtime_config.get("benchmark_symbol", "QQQ"))

    def evaluate(
        self,
        *,
        signal_text_fn: Callable[[str], str],
        translator: Callable[[str], str],
        **available_inputs,
    ) -> StrategyEvaluationResult:
        runtime_config = dict(self.runtime_overrides)
        runtime_config.setdefault("signal_text_fn", signal_text_fn)
        runtime_config.setdefault("translator", translator)
        ctx = build_strategy_context_from_available_inputs(
            entrypoint=self.entrypoint,
            runtime_adapter=self.runtime_adapter,
            as_of=datetime.now(timezone.utc),
            available_inputs=available_inputs,
            runtime_config=runtime_config,
        )
        decision = self.entrypoint.evaluate(ctx)
        return StrategyEvaluationResult(
            decision=decision,
            metadata={"strategy_profile": self.profile},
        )


def load_strategy_runtime(
    raw_profile: str | None,
    *,
    runtime_overrides: Mapping[str, Any] | None = None,
) -> LoadedStrategyRuntime:
    entrypoint = load_strategy_entrypoint_for_profile(raw_profile)
    runtime_adapter = load_strategy_runtime_adapter_for_profile(raw_profile)
    merged_runtime_config = dict(entrypoint.manifest.default_config)
    overrides = dict(runtime_overrides or {})
    merged_runtime_config.update(overrides)
    return LoadedStrategyRuntime(
        entrypoint=entrypoint,
        runtime_adapter=runtime_adapter,
        runtime_overrides=overrides,
        merged_runtime_config=merged_runtime_config,
    )
