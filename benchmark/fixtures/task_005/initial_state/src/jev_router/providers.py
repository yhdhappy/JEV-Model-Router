"""Provider contracts and deterministic failure-injection provider for T-05."""

from enum import Enum
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    Union,
    runtime_checkable,
)

from pydantic import Field, StrictInt

from .errors import RouterError
from .schemas import StrictModel


class ModelRequest(StrictModel):
    """Minimum provider input shared by model implementations."""

    model_id: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    context: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ModelResponse(StrictModel):
    """Normalized successful provider output and usage metadata."""

    text: str
    input_tokens: StrictInt = Field(ge=0)
    output_tokens: StrictInt = Field(ge=0)
    provider_request_id: Optional[str] = None
    latency_ms: Optional[StrictInt] = Field(default=None, ge=0)
    raw_finish_reason: Optional[str] = None


class ProviderError(RouterError):
    """Base class for stable, machine-readable provider failures."""

    code = "provider_error"


class ProviderTimeoutError(ProviderError):
    """The provider did not complete within its allowed time."""

    code = "provider_timeout"

    def __init__(self) -> None:
        super().__init__("Provider request timed out")


class ProviderUnavailableError(ProviderError):
    """The requested model or provider is unavailable."""

    code = "model_unavailable"

    def __init__(self) -> None:
        super().__init__("Provider model unavailable")


class ProviderInvocationError(ProviderError):
    """The provider failed for an unspecified invocation error."""

    code = "provider_error"

    def __init__(self) -> None:
        super().__init__("Provider invocation failed")


@runtime_checkable
class ModelProvider(Protocol):
    """Unified synchronous provider boundary used by the router."""

    def invoke(self, request: ModelRequest) -> ModelResponse:
        """Execute one model request or raise a ``ProviderError``."""


class MockScenario(str, Enum):
    """Deterministic outcomes supported by ``MockProvider``."""

    SUCCESS = "success"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


MockOutcome = Union[MockScenario, ModelResponse, str]


class MockProvider:
    """A no-network provider for repeatable success and failure scenarios.

    ``outcomes`` are consumed in call order.  A ``ModelResponse`` represents a
    successful call with that exact response; a ``MockScenario`` represents a
    fixed success or failure.  Once the configured sequence is exhausted, the
    final outcome is repeated so an accidental extra fallback attempt remains
    deterministic rather than depending on external state.
    """

    def __init__(self, outcomes: Optional[Sequence[MockOutcome]] = None):
        configured = (
            (MockScenario.SUCCESS,) if outcomes is None else tuple(outcomes)
        )
        if not configured:
            raise ValueError("outcomes must contain at least one item")

        self._outcomes = tuple(_coerce_outcome(outcome) for outcome in configured)
        self._call_count = 0
        self._requests: List[ModelRequest] = []

    @property
    def call_count(self) -> int:
        """Number of invocations, including invocations that raised."""

        return self._call_count

    @property
    def requests(self) -> Tuple[ModelRequest, ...]:
        """The immutable invocation history for test assertions."""

        return tuple(self._requests)

    def invoke(self, request: ModelRequest) -> ModelResponse:
        """Return the next deterministic outcome without network or credentials."""

        if not isinstance(request, ModelRequest):
            raise TypeError("request must be a ModelRequest")

        self._requests.append(request)
        position = min(self._call_count, len(self._outcomes) - 1)
        outcome = self._outcomes[position]
        self._call_count += 1

        if isinstance(outcome, ModelResponse):
            return outcome.model_copy(deep=True)
        if outcome is MockScenario.SUCCESS:
            return ModelResponse(
                text="mock response",
                input_tokens=1,
                output_tokens=2,
                provider_request_id="mock-provider-request",
                latency_ms=0,
                raw_finish_reason="stop",
            )
        if outcome is MockScenario.TIMEOUT:
            raise ProviderTimeoutError()
        if outcome is MockScenario.UNAVAILABLE:
            raise ProviderUnavailableError()
        raise ProviderInvocationError()


def _coerce_outcome(outcome: MockOutcome) -> Union[MockScenario, ModelResponse]:
    if isinstance(outcome, ModelResponse):
        return outcome
    if isinstance(outcome, MockScenario):
        return outcome
    if isinstance(outcome, str):
        try:
            return MockScenario(outcome)
        except ValueError:
            raise ValueError("outcome must be a supported mock scenario") from None
    raise TypeError("outcome must be a MockScenario, scenario string, or ModelResponse")
