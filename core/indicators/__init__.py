# Indicator layer package (Phase 2). Pluggable technical indicators.
#
# Importing this package triggers registration of every built-in indicator via
# the @register_indicator decorators in the submodules below. New indicator
# files only need to be imported here to become available everywhere.
from core.indicators.base import Indicator, IndicatorResult  # noqa: F401
from core.indicators.registry import (  # noqa: F401
    register_indicator,
    get_indicator_class,
    list_indicators,
    build_enabled_indicators,
    build_all_indicators,
)

# Import submodules for their registration side effects.
from core.indicators import trend  # noqa: F401
from core.indicators import momentum  # noqa: F401
from core.indicators import volatility  # noqa: F401
from core.indicators import volume  # noqa: F401
from core.indicators import patterns  # noqa: F401
from core.indicators import extra  # noqa: F401  (Phase 5 extra indicators)
