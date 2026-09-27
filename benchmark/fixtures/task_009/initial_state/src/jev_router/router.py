"""T-09 Router Core orchestration.

The router owns the stage-1 decision chain, but delegates classification,
policy, provider invocation, budget arithmetic, and token pricing to the
existing injected boundaries.  In particular, estimated maximum costs are
used only before a provider call; they are never recorded as production cost.
"""

from decimal import Decimal
from typing import Any, Callable, List, Mapping, Optional, Sequence, Tuple, Union

from pydantic import ValidationError

from .cost import CostComponent, calculate_cost_breakdown, calculate_cost_from_response
from .errors import NoEligibleModelError, SchemaValidationError
from .fallback import (
    check_budget,
    normalize_execution_failure,
    execute_jev_with_fallback,
)
from .policy import PolicyDecision, PolicyEngine
from .providers import ModelProvider, ModelRequest, ModelResponse
from .registry import ModelDefinition, ModelRegistry
from .rules import LightweightRuleEngine, RuleResult
from .schemas import (
    CapabilityLevel,
    ClassifierResult,
    CostBreakdown,
    DifficultyBucket,
    RouteError,
    RouteRequest,
    RouteResult,
    RiskLevel,
    TaskType,
)


EstimatedMaxCostSource = Union[
    Mapping[str, Union[int, float, Decimal]],
    Callable[[str], Union[int, float, Decimal]],
]
ClassifierCallable = Union[Any, Callable[[RouteRequest], ClassifierResult]]


_ERROR_MESSAGES = {
    "manual_model_budget_exceeded": "Manual model budget exceeded",
    "budget_limit_reached": "Budget limit reached before provider call",
    "model_unavailable": "Requested model is unavailable",
    "provider_timeout": "Provider request timed out",
    "provider_error": "Provider invocation failed",
    "no_eligible_model": "No eligible model",
    "policy_error": "Policy evaluation failed",
    "safe_default_failed": "Safe default model failed",
    "budget_estimate_unavailable": "Budget estimate unavailable",
    "classifier_cost_unavailable": "Classifier cost evidence unavailable",
    "jev_timeout": "JEV classifier timed out",
    "jev_network_error": "JEV classifier network failure",
    "jev_invalid_response": "JEV classifier returned invalid JSON",
    "jev_schema_error": "JEV classifier returned an invalid schema",
    "jev_provider_error": "JEV classifier failed",
}

_CAPABILITY_RANK = {
    CapabilityLevel.LOW: 0,
    CapabilityLevel.MEDIUM: 1,
    CapabilityLevel.HIGH: 2,
}


class Router:
    """Coordinate one complete route and execution attempt."""

    def __init__(
        self,
        registry: ModelRegistry,
        providers: Mapping[str, ModelProvider],
        jev_classifier: ClassifierCallable,
        *,
        safe_default_model: str,
        estimated_max_costs: Optional[EstimatedMaxCostSource] = None,
        classifier_cost_resolver: Optional[
            Callable[[RouteRequest, ClassifierResult], CostComponent]
        ] = None,
        classifier_failure_cost_resolver: Optional[
            Callable[[RouteRequest, str], CostComponent]
        ] = None,
        rule_engine: Optional[LightweightRuleEngine] = None,
        policy_engine: Optional[PolicyEngine] = None,
        result_sink: Optional[Callable[[RouteResult], None]] = None,
        estimated_max_cost_source: Optional[EstimatedMaxCostSource] = None,
    ) -> None:
        if not isinstance(registry, ModelRegistry):
            raise TypeError("registry must be a ModelRegistry")
        if not isinstance(providers, Mapping):
            raise TypeError("providers must be a model-to-provider mapping")
        if not isinstance(safe_default_model, str) or not safe_default_model.strip():
            raise ValueError("safe_default_model must be a non-empty string")
        if estimated_max_costs is not None and estimated_max_cost_source is not None:
            raise TypeError(
                "provide only one of estimated_max_costs and "
                "estimated_max_cost_source"
            )
        if not callable(jev_classifier) and not callable(
            getattr(jev_classifier, "classify", None)
        ):
            raise TypeError("jev_classifier must be callable or expose classify")
        if classifier_cost_resolver is not None and not callable(
            classifier_cost_resolver
        ):
            raise TypeError("classifier_cost_resolver must be callable")
        if classifier_failure_cost_resolver is not None and not callable(
            classifier_failure_cost_resolver
        ):
            raise TypeError("classifier_failure_cost_resolver must be callable")
        if result_sink is not None and not callable(result_sink):
            raise TypeError("result_sink must be callable")

        self._registry = registry
        self._providers = providers
        self._jev_classifier = jev_classifier
        self._safe_default_model = safe_default_model
        self._estimated_max_costs = (
            estimated_max_cost_source
            if estimated_max_cost_source is not None
            else estimated_max_costs
        )
        self._classifier_cost_resolver = classifier_cost_resolver
        self._classifier_failure_cost_resolver = classifier_failure_cost_resolver
        self._rule_engine = rule_engine or LightweightRuleEngine()
        self._policy_engine = policy_engine or PolicyEngine(registry)
        self._result_sink = result_sink

    def route(self, request: RouteRequest) -> RouteResult:
        """Route and execute one request according to the T-09 order."""

        if not isinstance(request, RouteRequest):
            raise TypeError("request must be a RouteRequest")

        if request.manual_model is not None:
            return self._route_manual(request)

        rule = self._evaluate_rule(request)
        if rule.matched:
            synthetic = _classifier_from_rule(rule)
            return self._route_policy_and_execute(
                request,
                classifier_for_policy=synthetic,
                exposed_classifier=None,
                route_source="light_rule",
                rule_id=rule.rule_id,
                budget_exposure=Decimal("0"),
            )

        jev_result = execute_jev_with_fallback(
            lambda: self._classify(request)
        )
        if jev_result.status == "safe_default":
            classifier_cost = self._resolve_classifier_failure_cost(
                request, jev_result.error_code or "jev_provider_error"
            )
            if classifier_cost is None:
                return self._finish(
                    self._result(
                        request,
                        status="failed",
                        route_source="safe_default",
                        errors=[
                            self._error(
                                jev_result.error_code or "jev_provider_error"
                            ),
                            self._error("classifier_cost_unavailable"),
                        ],
                        cost=_unavailable_cost_breakdown(),
                    )
                )
            return self._route_safe_default(
                request,
                jev_result.error_code or "jev_provider_error",
                classifier_cost=classifier_cost,
            )

        classifier = jev_result.value
        classifier_cost = self._resolve_classifier_cost(request, classifier)
        if classifier_cost is None:
            return self._finish(
                self._result(
                    request,
                    status="failed",
                    route_source="jev",
                    classifier=classifier,
                    errors=[self._error("classifier_cost_unavailable")],
                    cost=_unavailable_cost_breakdown(),
                )
            )
        budget_exposure = _component_amount(classifier_cost)
        return self._route_policy_and_execute(
            request,
            classifier_for_policy=classifier,
            exposed_classifier=classifier,
            route_source="jev",
            rule_id=None,
            budget_exposure=budget_exposure,
            classifier_cost=classifier_cost,
        )

    # ``run`` is a convenient compatibility alias for callers that treat the
    # Router as an executable service.
    run = route

    def _route_manual(self, request: RouteRequest) -> RouteResult:
        model = request.manual_model
        assert model is not None
        budget, budget_error, _ = self._budget_decision(
            model, Decimal("0"), request.budget_limit
        )
        if budget_error is not None:
            return self._finish(
                self._result(
                    request,
                    status="failed",
                    route_source="manual_override",
                    selected_model=None,
                    errors=[self._error(budget_error)],
                )
            )
        if not budget.allowed:
            return self._finish(
                self._result(
                    request,
                    status="failed",
                    route_source="manual_override",
                    selected_model=None,
                    errors=[self._error("manual_model_budget_exceeded")],
                )
            )

        response, error_code, _ = self._invoke(model, request)
        if response is None:
            return self._finish(
                self._result(
                    request,
                    status="failed",
                    route_source="manual_override",
                    selected_model=None,
                    errors=[self._error(error_code or "provider_error")],
                )
            )

        execution_cost = self._execution_cost(model, response)
        return self._finish(
            self._result(
                request,
                status="success",
                route_source="manual_override",
                selected_model=model,
                cost=calculate_cost_breakdown(execution_cost=execution_cost),
            )
        )

    def _route_safe_default(
        self,
        request: RouteRequest,
        jev_error: str,
        *,
        classifier_cost: CostComponent,
    ) -> RouteResult:
        model = self._safe_default_model
        errors = [self._error(jev_error)]
        budget_exposure = _component_amount(classifier_cost)
        budget, budget_error, _ = self._budget_decision(
            model, budget_exposure, request.budget_limit
        )
        if budget_error is not None:
            errors.append(self._error(budget_error))
            return self._finish(
                self._result(
                    request,
                    status="failed",
                    route_source="safe_default",
                    selected_model=None,
                    errors=errors,
                    cost=calculate_cost_breakdown(classifier_cost=classifier_cost),
                )
            )
        if not budget.allowed:
            errors.append(self._error("budget_limit_reached"))
            return self._finish(
                self._result(
                    request,
                    status="failed",
                    route_source="safe_default",
                    selected_model=None,
                    errors=errors,
                    cost=calculate_cost_breakdown(classifier_cost=classifier_cost),
                )
            )

        response, error_code, _ = self._invoke(model, request)
        if response is None:
            errors.append(self._error("safe_default_failed"))
            if error_code is not None and error_code != "safe_default_failed":
                errors.append(self._error(error_code))
            return self._finish(
                self._result(
                    request,
                    status="failed",
                    route_source="safe_default",
                    selected_model=None,
                    fallback_history=[f"{model}:{error_code or 'provider_error'}"],
                    errors=errors,
                    cost=calculate_cost_breakdown(classifier_cost=classifier_cost),
                )
            )

        return self._finish(
            self._result(
                request,
                status="success",
                route_source="safe_default",
                selected_model=model,
                fallback_history=[f"{model}:success"],
                errors=errors,
                cost=calculate_cost_breakdown(
                    classifier_cost=classifier_cost,
                    execution_cost=self._execution_cost(model, response)
                ),
            )
        )

    def _route_policy_and_execute(
        self,
        request: RouteRequest,
        *,
        classifier_for_policy: ClassifierResult,
        exposed_classifier: Optional[ClassifierResult],
        route_source: str,
        rule_id: Optional[str],
        budget_exposure: Decimal,
        classifier_cost: Optional[CostComponent] = None,
    ) -> RouteResult:
        try:
            decision = self._policy_decide(classifier_for_policy)
        except NoEligibleModelError:
            return self._finish(
                self._result(
                    request,
                    status="failed",
                    route_source=route_source,
                    rule_id=rule_id,
                    classifier=exposed_classifier,
                    possible_rule_misclassification=_possible_rule_misclassification(
                        self._registry,
                        route_origin=route_source,
                        status="failed",
                        classifier=classifier_for_policy,
                        selected_model=None,
                    ),
                    errors=[self._error("no_eligible_model")],
                    cost=calculate_cost_breakdown(
                        classifier_cost=classifier_cost
                    ),
                )
            )
        except Exception:
            return self._finish(
                self._result(
                    request,
                    status="failed",
                    route_source=route_source,
                    rule_id=rule_id,
                    classifier=exposed_classifier,
                    possible_rule_misclassification=_possible_rule_misclassification(
                        self._registry,
                        route_origin=route_source,
                        status="failed",
                        classifier=classifier_for_policy,
                        selected_model=None,
                    ),
                    errors=[self._error("policy_error")],
                    cost=calculate_cost_breakdown(
                        classifier_cost=classifier_cost
                    ),
                )
            )

        candidates = _unique_models(
            [decision.primary_model, *decision.fallback_models]
        )
        history: List[str] = []
        errors: List[RouteError] = []
        for index, model in enumerate(candidates):
            budget, budget_error, estimated_cost = self._budget_decision(
                model, budget_exposure, request.budget_limit
            )
            if budget_error is not None:
                history.append(f"{model}:{budget_error}")
                errors.append(self._error(budget_error))
                break
            if not budget.allowed:
                history.append(f"{model}:budget_limit_reached")
                errors.append(self._error("budget_limit_reached"))
                break

            response, error_code, invoked = self._invoke(model, request)
            if response is None:
                normalized = error_code or "provider_error"
                history.append(f"{model}:{normalized}")
                errors.append(self._error(normalized))
                if invoked:
                    budget_exposure += estimated_cost
                continue

            if index == 0:
                execution_cost = self._execution_cost(model, response)
                return self._finish(
                    self._result(
                        request,
                        status="success",
                        route_source=route_source,
                        rule_id=rule_id,
                        classifier=exposed_classifier,
                        possible_rule_misclassification=_possible_rule_misclassification(
                            self._registry,
                            route_origin=route_source,
                            status="success",
                            classifier=classifier_for_policy,
                            selected_model=model,
                        ),
                        selected_model=model,
                        fallback_history=history,
                        errors=errors,
                        cost=calculate_cost_breakdown(
                            classifier_cost=classifier_cost,
                            execution_cost=execution_cost,
                        ),
                    )
                )

            history.append(f"{model}:success")
            return self._finish(
                self._result(
                    request,
                    status="success",
                    route_source="fallback",
                    rule_id=rule_id,
                    classifier=exposed_classifier,
                    possible_rule_misclassification=_possible_rule_misclassification(
                        self._registry,
                        route_origin=route_source,
                        status="success",
                        classifier=classifier_for_policy,
                        selected_model=model,
                    ),
                    selected_model=model,
                    fallback_history=history,
                    errors=errors,
                    cost=calculate_cost_breakdown(
                        classifier_cost=classifier_cost,
                        fallback_cost=self._execution_cost(model, response),
                    ),
                )
            )

        return self._finish(
            self._result(
                request,
                status="failed",
                route_source=route_source,
                rule_id=rule_id,
                classifier=exposed_classifier,
                possible_rule_misclassification=_possible_rule_misclassification(
                    self._registry,
                    route_origin=route_source,
                    status="failed",
                    classifier=classifier_for_policy,
                    selected_model=None,
                ),
                selected_model=None,
                fallback_history=history,
                errors=errors or [self._error("provider_error")],
                cost=calculate_cost_breakdown(classifier_cost=classifier_cost),
            )
        )

    def _evaluate_rule(self, request: RouteRequest) -> RuleResult:
        evaluator = getattr(self._rule_engine, "evaluate", None)
        if not callable(evaluator):
            evaluator = getattr(self._rule_engine, "match", None)
        if not callable(evaluator):
            raise TypeError("rule_engine must expose evaluate or match")
        return evaluator(request)

    def _policy_decide(self, classifier: ClassifierResult) -> PolicyDecision:
        decider = getattr(self._policy_engine, "decide", None)
        if not callable(decider):
            decider = getattr(self._policy_engine, "select", None)
        if not callable(decider):
            raise TypeError("policy_engine must expose decide or select")
        return decider(classifier)

    def _classify(self, request: RouteRequest) -> ClassifierResult:
        classifier = getattr(self._jev_classifier, "classify", None)
        value = (
            classifier(request)
            if callable(classifier)
            else self._jev_classifier(request)
        )
        if isinstance(value, ClassifierResult):
            return value
        try:
            return ClassifierResult.model_validate(value)
        except ValidationError:
            raise SchemaValidationError(
                "JEV classifier response failed schema validation"
            ) from None

    def _resolve_classifier_cost(
        self, request: RouteRequest, classifier: ClassifierResult
    ) -> Optional[CostComponent]:
        if self._classifier_cost_resolver is None:
            return None
        try:
            component = self._classifier_cost_resolver(request, classifier)
        except Exception:
            return None
        if not isinstance(component, CostComponent):
            return None
        return component

    def _resolve_classifier_failure_cost(
        self, request: RouteRequest, jev_error: str
    ) -> Optional[CostComponent]:
        if self._classifier_failure_cost_resolver is None:
            return None
        try:
            component = self._classifier_failure_cost_resolver(request, jev_error)
        except Exception:
            return None
        if not isinstance(component, CostComponent):
            return None
        return component

    def _invoke(
        self, model: str, request: RouteRequest
    ) -> Tuple[Optional[ModelResponse], Optional[str], bool]:
        provider = self._providers.get(model)
        definition = self._definition(model)
        if definition is None or not definition.enabled or provider is None:
            return None, "model_unavailable", False
        if not hasattr(provider, "invoke"):
            return None, "model_unavailable", False
        provider_request = ModelRequest(
            model_id=definition.model_id,
            prompt=request.prompt,
            context=list(request.context),
            metadata=dict(request.metadata),
        )
        try:
            response = provider.invoke(provider_request)
        except Exception as exc:
            return None, normalize_execution_failure(exc), True
        if not isinstance(response, ModelResponse):
            return None, "provider_error", True
        return response, None, True

    def _execution_cost(self, model: str, response: ModelResponse) -> CostComponent:
        definition = self._definition(model)
        if definition is None:
            # This is unreachable after _invoke, but keeps the cost boundary
            # explicit if a custom PolicyEngine returns an unknown model.
            raise ValueError("model definition is unavailable")
        return calculate_cost_from_response(
            response,
            definition.input_cost_per_million,
            definition.output_cost_per_million,
        )

    def _budget_decision(
        self,
        model: str,
        current_cost: Decimal,
        budget_limit: Optional[float],
    ):
        if budget_limit is None:
            return check_budget(current_cost, Decimal("0"), None), None, Decimal("0")
        try:
            estimated = self._estimated_cost(model)
            if estimated is None or isinstance(estimated, bool):
                raise ValueError("estimate unavailable")
            estimated_decimal = Decimal(str(estimated))
            if not estimated_decimal.is_finite() or estimated_decimal < 0:
                raise ValueError("estimate unavailable")
            return (
                check_budget(current_cost, estimated_decimal, budget_limit),
                None,
                estimated_decimal,
            )
        except Exception:
            return None, "budget_estimate_unavailable", Decimal("0")

    def _estimated_cost(self, model: str) -> Union[int, float, Decimal]:
        source = self._estimated_max_costs
        if source is None:
            raise KeyError(model)
        if isinstance(source, Mapping):
            return source[model]
        return source(model)

    def _definition(self, model: str) -> Optional[ModelDefinition]:
        try:
            return self._registry.get(model)
        except KeyError:
            return None

    def _result(
        self,
        request: RouteRequest,
        *,
        status: str,
        route_source: str,
        rule_id: Optional[str] = None,
        possible_rule_misclassification: bool = False,
        classifier: Optional[ClassifierResult] = None,
        selected_model: Optional[str] = None,
        fallback_history: Optional[Sequence[str]] = None,
        errors: Optional[Sequence[RouteError]] = None,
        cost: Optional[CostBreakdown] = None,
    ) -> RouteResult:
        return RouteResult(
            task_id=request.task_id,
            status=status,
            route_source=route_source,
            rule_id=rule_id,
            possible_rule_misclassification=possible_rule_misclassification,
            classifier=classifier,
            selected_model=selected_model,
            fallback_history=list(fallback_history or ()),
            cost=cost or CostBreakdown(),
            errors=list(errors or ()),
        )

    def _finish(self, result: RouteResult) -> RouteResult:
        if self._result_sink is not None:
            self._result_sink(result)
        return result

    @staticmethod
    def _error(code: str) -> RouteError:
        return RouteError(code=code, message=_ERROR_MESSAGES.get(code, "Router operation failed"))


def _classifier_from_rule(rule: RuleResult) -> ClassifierResult:
    if rule.capability is None:
        raise ValueError("a matched lightweight rule must provide capability")
    return ClassifierResult(
        task_type=TaskType.FILE_OPERATION,
        difficulty_score=1,
        difficulty_bucket=DifficultyBucket.LOW,
        required_capability=rule.capability,
        confidence=rule.confidence,
        risk_level=RiskLevel.LOW,
    )


def _possible_rule_misclassification(
    registry: ModelRegistry,
    *,
    route_origin: str,
    status: str,
    classifier: ClassifierResult,
    selected_model: Optional[str],
) -> bool:
    """Mark light-rule routes that fail or require higher capability."""

    if route_origin != "light_rule":
        return False
    if status == "failed":
        return True
    if selected_model is None:
        return False
    selected_definition = registry.get(selected_model)
    return (
        _CAPABILITY_RANK[selected_definition.capability]
        > _CAPABILITY_RANK[classifier.required_capability]
    )


def _component_amount(component: Optional[CostComponent]) -> Decimal:
    if component is None:
        return Decimal("0")
    return Decimal(str(component.cost))


def _unavailable_cost_breakdown() -> CostBreakdown:
    """Mark a failed route's missing evidence as non-exact."""

    return CostBreakdown(
        cost_estimated=True,
        cost_estimation_source="heuristic",
    )


def _unique_models(models: Sequence[str]) -> List[str]:
    unique: List[str] = []
    seen = set()
    for model in models:
        if model not in seen:
            seen.add(model)
            unique.append(model)
    return unique
