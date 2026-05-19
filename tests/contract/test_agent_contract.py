import schemathesis
from schemathesis.checks import not_a_server_error

from control_station_lite.agent.main import app

schema = schemathesis.openapi.from_asgi("/openapi.json", app)


@schema.parametrize()
def test_agent_api_contract(case: schemathesis.Case) -> None:
    """Verify every agent endpoint accepts valid inputs without returning a server error."""
    response = case.call(app=app)
    case.validate_response(response, checks=(not_a_server_error,))
