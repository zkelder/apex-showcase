"""PAPER Trial Closure Readiness v1: the pure horizon-close attempt state
machine (``domain.strategy_paper_trial_lifecycle.classify_close_attempts``).

Only the fields the classifier reads are modeled; every id is matched
exactly, so lookalike ids must never be recognized as an attempt."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from apex.domain.crypto_paper import PaperOrderState
from apex.domain.strategy_paper_trial_lifecycle import (
    CloseAttemptState,
    StrategyPaperTrialLifecycleError,
    classify_close_attempts,
    close_attempt_seed,
)

TRIAL = "strategy_paper_trial_generation_test"
T0 = datetime(2026, 9, 30, 0, 0, 31, tzinfo=UTC)
QTY = Decimal("0.00012687")


def _authorization(seed: str, created_at: datetime = T0) -> Any:
    return SimpleNamespace(
        authorization_id=f"{seed}-authorization", expires_at=created_at + timedelta(hours=1)
    )


def _preview(seed: str, *, created_at: datetime = T0, rejected: bool = False) -> Any:
    return SimpleNamespace(
        preview_id=f"{seed}-preview",
        expires_at=created_at + timedelta(seconds=300),
        rejection_reasons=("CRYPTO_PAPER_NOTIONAL_CAP",) if rejected else (),
    )


def _order(seed: str, state: PaperOrderState) -> Any:
    return SimpleNamespace(order_id=f"{seed}-order", state=state)


def _classify(
    *,
    authorizations: tuple[Any, ...] = (),
    previews: tuple[Any, ...] = (),
    orders: tuple[Any, ...] = (),
    position: Decimal = QTY,
    at: datetime = T0,
):
    return classify_close_attempts(
        trial_id=TRIAL,
        authorizations=authorizations,
        previews=previews,
        orders=orders,
        position_base_quantity=position,
        evaluated_at=at,
    )


SEED_1 = close_attempt_seed(trial_id=TRIAL, attempt_number=1)
SEED_2 = close_attempt_seed(trial_id=TRIAL, attempt_number=2)


def test_attempt_one_keeps_the_original_seed_and_later_attempts_are_numbered() -> None:
    assert f"{TRIAL}-close" == SEED_1
    assert f"{TRIAL}-close-2" == SEED_2
    with pytest.raises(StrategyPaperTrialLifecycleError):
        close_attempt_seed(trial_id=TRIAL, attempt_number=0)


def test_no_attempt_acts_with_attempt_one() -> None:
    result = _classify()
    assert result.state is CloseAttemptState.NO_ATTEMPT
    assert result.action_seed == SEED_1


def test_lookalike_ids_are_never_recognized_as_an_attempt() -> None:
    lookalikes = (
        SimpleNamespace(authorization_id=f"{TRIAL}-closed-authorization", expires_at=T0),
        SimpleNamespace(authorization_id=f"{TRIAL}X-close-authorization", expires_at=T0),
        SimpleNamespace(authorization_id=f"{TRIAL}-close-2-authorization", expires_at=T0),
    )
    # attempt 2 without attempt 1 is not contiguous -> no attempt.
    assert _classify(authorizations=lookalikes).state is CloseAttemptState.NO_ATTEMPT


def test_authorized_without_order_resumes_the_same_attempt() -> None:
    result = _classify(authorizations=(_authorization(SEED_1),), at=T0 + timedelta(minutes=5))
    assert result.state is CloseAttemptState.RESUMABLE
    assert result.action_seed == SEED_1


def test_valid_preview_without_order_resumes_submission() -> None:
    result = _classify(
        authorizations=(_authorization(SEED_1),),
        previews=(_preview(SEED_1),),
        at=T0 + timedelta(seconds=299),
    )
    assert result.state is CloseAttemptState.RESUMABLE
    assert result.action_seed == SEED_1


@pytest.mark.parametrize(
    ("preview", "detail"),
    [
        (_preview(SEED_1, rejected=True), "PREVIEW_REJECTED"),
        (_preview(SEED_1), "PREVIEW_EXPIRED_WITHOUT_ORDER"),
    ],
)
def test_dead_preview_waits_for_the_authorization_to_expire(preview: Any, detail: str) -> None:
    waiting = _classify(
        authorizations=(_authorization(SEED_1),),
        previews=(preview,),
        at=T0 + timedelta(minutes=30),
    )
    assert waiting.state is CloseAttemptState.DEAD_AWAITING_AUTHORIZATION_EXPIRY
    assert waiting.detail == detail
    assert waiting.action_seed is None
    retry = _classify(
        authorizations=(_authorization(SEED_1),),
        previews=(preview,),
        at=T0 + timedelta(hours=1),
    )
    assert retry.state is CloseAttemptState.DEAD_RETRYABLE
    assert retry.action_seed == SEED_2


def test_expired_authorization_without_order_is_retryable() -> None:
    result = _classify(authorizations=(_authorization(SEED_1),), at=T0 + timedelta(hours=2))
    assert result.state is CloseAttemptState.DEAD_RETRYABLE
    assert result.detail == "AUTHORIZATION_EXPIRED_WITHOUT_ORDER"
    assert result.action_seed == SEED_2


@pytest.mark.parametrize(
    "state",
    [
        PaperOrderState.SUBMITTED,
        PaperOrderState.OPEN,
        PaperOrderState.PARTIALLY_FILLED,
        PaperOrderState.CANCEL_REQUESTED,
        PaperOrderState.RECONCILIATION_REQUIRED,
    ],
)
def test_an_existing_live_order_is_never_duplicated(state: PaperOrderState) -> None:
    result = _classify(
        authorizations=(_authorization(SEED_1),),
        orders=(_order(SEED_1, state),),
        at=T0 + timedelta(hours=5),  # even long after the authorization expired
    )
    assert result.state is CloseAttemptState.IN_FLIGHT
    assert result.action_seed is None


@pytest.mark.parametrize(
    "state", [PaperOrderState.EXPIRED, PaperOrderState.CANCELED, PaperOrderState.REJECTED]
)
def test_terminal_unfilled_order_is_retryable_after_authorization_expiry(
    state: PaperOrderState,
) -> None:
    result = _classify(
        authorizations=(_authorization(SEED_1),),
        orders=(_order(SEED_1, state),),
        at=T0 + timedelta(hours=4),
    )
    assert result.state is CloseAttemptState.DEAD_RETRYABLE
    assert result.action_seed == SEED_2


def test_filled_with_flat_position_is_filled_and_names_that_attempt() -> None:
    result = _classify(
        authorizations=(_authorization(SEED_1), _authorization(SEED_2, T0 + timedelta(hours=4))),
        orders=(
            _order(SEED_1, PaperOrderState.EXPIRED),
            _order(SEED_2, PaperOrderState.FILLED),
        ),
        position=Decimal(0),
        at=T0 + timedelta(hours=4, minutes=5),
    )
    assert result.state is CloseAttemptState.FILLED
    assert result.attempt_number == 2
    assert result.order_id == f"{SEED_2}-order"
    assert result.authorization_id == f"{SEED_2}-authorization"
    assert result.action_seed is None


def test_filled_with_residual_position_is_never_a_close() -> None:
    result = _classify(
        authorizations=(_authorization(SEED_1),),
        orders=(_order(SEED_1, PaperOrderState.FILLED),),
        position=Decimal("0.00000001"),
        at=T0 + timedelta(hours=2),
    )
    assert result.state is CloseAttemptState.DEAD_RETRYABLE
    assert result.detail == "ORDER_FILLED_RESIDUAL_POSITION"


def test_classification_is_deterministic_and_order_independent() -> None:
    authorizations = (_authorization(SEED_2, T0 + timedelta(hours=4)), _authorization(SEED_1))
    orders = (_order(SEED_2, PaperOrderState.OPEN), _order(SEED_1, PaperOrderState.EXPIRED))
    first = _classify(authorizations=authorizations, orders=orders)
    second = _classify(authorizations=authorizations[::-1], orders=orders[::-1])
    assert first == second
    assert first.state is CloseAttemptState.IN_FLIGHT
    assert first.attempt_number == 2
