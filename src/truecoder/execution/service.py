from truecoder.execution.models import (
    ExecutionContext,
    ExecutionRequest,
    ExecutionResult,
)


class ExecutionService:
    async def execute(
        self,
        request: ExecutionRequest,
        context: ExecutionContext,
    ) -> ExecutionResult:
        decision = self._policy.evaluate(request, context)

        if not decision.allowed:
            return self._denied_result(
                request,
                context,
                decision.reason,
            )

        effective_request = replace(
            request,
            limits=decision.effective_limits,
        )

        backend = select_backend(
            effective_request,
            self._backends,
        )

        handle = await backend.start(
            effective_request,
            context,
        )

        # Timeout, cancellation, output collection,
        # lifecycle events, and final result assembly.
