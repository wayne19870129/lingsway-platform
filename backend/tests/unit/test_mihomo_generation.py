import pytest

from backend.app.core.config import Settings
from backend.app.providers.registry import ProviderConfigurationError, build_registry


def test_mihomo_forwarder_is_still_rejected() -> None:
    with pytest.raises(ProviderConfigurationError, match="FORWARDER_PROVIDER"):
        build_registry(Settings(forwarder_provider="mihomo"))
