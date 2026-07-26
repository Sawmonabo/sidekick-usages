"""One bounded concurrent wave across every selected account."""

from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from queue import Queue

from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.types import ProviderId
from sidekick_usages.usage.activity import TokenActivityCollector
from sidekick_usages.usage.lookup.models import (
    AccountMutationExchange,
    AccountMutationIntent,
    AccountMutationResult,
    LookupTaskFuture,
    LookupWaveEvent,
    LookupWaveReading,
    OwnerMutationRequest,
)
from sidekick_usages.usage.lookup.service import AccountLookupService

USAGE_LOOKUP_MAX_WORKERS = 8


class UsageLookupWave:
    """Submit every account before publishing completion-order results."""

    def __init__(
        self,
        lookup: AccountLookupService,
        activity: TokenActivityCollector,
    ) -> None:
        """Bind the account and local-activity lookup owners."""
        self._lookup = lookup
        self._activity = activity

    def run(
        self,
        accounts: tuple[SavedAccount, ...],
        local_providers: tuple[ProviderId, ...],
        reference_time: datetime,
        mutate: AccountMutationExchange,
    ) -> Iterator[LookupWaveReading]:
        """Yield immutable results while serializing owner mutations."""
        task_count = len(accounts) + len(local_providers)
        if task_count == 0:
            return
        if task_count == 1:
            yield self._run_single(
                accounts,
                local_providers,
                reference_time,
                mutate,
            )
            return
        yield from self._run_parallel(
            accounts,
            local_providers,
            reference_time,
            mutate,
            task_count,
        )

    def _run_single(
        self,
        accounts: tuple[SavedAccount, ...],
        local_providers: tuple[ProviderId, ...],
        reference_time: datetime,
        mutate: AccountMutationExchange,
    ) -> LookupWaveReading:
        """Run the only selected task without allocating a thread pool."""
        if accounts:
            return self._lookup.lookup(
                accounts[0],
                0,
                reference_time,
                mutate,
            )
        return self._activity.read_local(
            local_providers[0],
            reference_time,
        )

    def _run_parallel(
        self,
        accounts: tuple[SavedAccount, ...],
        local_providers: tuple[ProviderId, ...],
        reference_time: datetime,
        mutate: AccountMutationExchange,
        task_count: int,
    ) -> Iterator[LookupWaveReading]:
        """Submit every task before consuming any completion event."""
        completion_events: Queue[LookupWaveEvent] = Queue()

        def exchange(
            intent: AccountMutationIntent,
        ) -> AccountMutationResult:
            response: Future[AccountMutationResult] = Future()
            completion_events.put(
                OwnerMutationRequest(
                    intent=intent,
                    response=response,
                )
            )
            return response.result()

        worker_count = min(USAGE_LOOKUP_MAX_WORKERS, task_count)
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="sidekick-usage",
        ) as executor:
            futures = self._submit_all(
                executor,
                accounts,
                local_providers,
                reference_time,
                exchange,
                completion_events,
            )
            yield from self._consume(
                completion_events,
                len(futures),
                mutate,
            )

    def _submit_all(
        self,
        executor: ThreadPoolExecutor,
        accounts: tuple[SavedAccount, ...],
        local_providers: tuple[ProviderId, ...],
        reference_time: datetime,
        exchange: AccountMutationExchange,
        completion_events: Queue[LookupWaveEvent],
    ) -> list[LookupTaskFuture]:
        """Attach completion routing as each global-wave task is submitted."""
        futures: list[LookupTaskFuture] = []
        for ordinal, account in enumerate(accounts):
            future = executor.submit(
                self._lookup.lookup,
                account,
                ordinal,
                reference_time,
                exchange,
            )
            future.add_done_callback(completion_events.put)
            futures.append(future)
        for provider_id in local_providers:
            future = executor.submit(
                self._activity.read_local,
                provider_id,
                reference_time,
            )
            future.add_done_callback(completion_events.put)
            futures.append(future)
        return futures

    @staticmethod
    def _consume(
        completion_events: Queue[LookupWaveEvent],
        remaining: int,
        mutate: AccountMutationExchange,
    ) -> Iterator[LookupWaveReading]:
        """Serve owner mutations and yield completion-order readings."""
        failure: Exception | None = None
        while remaining:
            event = completion_events.get()
            if isinstance(event, OwnerMutationRequest):
                failure = UsageLookupWave._complete_mutation(
                    event,
                    mutate,
                    failure,
                )
                continue
            remaining -= 1
            try:
                reading = event.result()
            except Exception as error:
                if failure is None:
                    failure = error
            else:
                yield reading
        if failure is not None:
            raise failure

    @staticmethod
    def _complete_mutation(
        request: OwnerMutationRequest,
        mutate: AccountMutationExchange,
        failure: Exception | None,
    ) -> Exception | None:
        """Complete one worker request without abandoning other tasks."""
        try:
            result = mutate(request.intent)
        except Exception as error:
            request.response.set_exception(error)
            return error if failure is None else failure
        request.response.set_result(result)
        return failure
