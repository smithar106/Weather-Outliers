"""Provider registry.

Selecting a source is a configuration decision, not a code change. Adding one
means implementing :class:`~app.providers.base.WeatherProvider` and adding a line
to ``_FACTORIES``.
"""

from __future__ import annotations

from collections.abc import Callable

from app.config import Settings, get_settings
from app.providers.base import (
    DailyRecord,
    ProviderBadRequest,
    ProviderBudgetExhausted,
    ProviderError,
    ProviderRateLimited,
    ProviderUnavailable,
    WeatherProvider,
)
from app.providers.fixture import FixtureProvider
from app.providers.open_meteo import OpenMeteoProvider

_FACTORIES: dict[str, Callable[[Settings], WeatherProvider]] = {
    "open_meteo": lambda settings: OpenMeteoProvider(settings=settings),
    "fixture": lambda _settings: FixtureProvider(),
}


def get_provider(settings: Settings | None = None) -> WeatherProvider:
    settings = settings or get_settings()
    try:
        factory = _FACTORIES[settings.weather_provider]
    except KeyError as exc:
        raise ValueError(
            f"unknown WEATHER_PROVIDER={settings.weather_provider!r}; "
            f"expected one of {sorted(_FACTORIES)}"
        ) from exc
    return factory(settings)


__all__ = [
    "DailyRecord",
    "FixtureProvider",
    "OpenMeteoProvider",
    "ProviderBadRequest",
    "ProviderBudgetExhausted",
    "ProviderError",
    "ProviderRateLimited",
    "ProviderUnavailable",
    "WeatherProvider",
    "get_provider",
]
