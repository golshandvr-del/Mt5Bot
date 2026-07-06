# Execution layer package.
#
# Turns a Decision from the decision engine into (optionally real) MT5 orders,
# with position sizing and risk controls. In "paper" mode it computes and logs
# everything but never sends an order. In "live" mode it sends orders through
# the MT5Connector.
from core.execution.risk_manager import RiskManager  # noqa: F401
from core.execution.order_manager import OrderManager  # noqa: F401
