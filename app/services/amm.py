from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, getcontext
from typing import Tuple, Optional

from flask import current_app
from sqlalchemy.orm import Session

from ..extensions import db
from ..models import SwapPool, Token, TokenBalance, SwapTrade, BurnEvent

# Increase precision for AMM math
getcontext().prec = 40


@dataclass
class Quote:
    amount_out: Decimal
    fee_bps: int
    fee_amount: Decimal
    effective_in: Decimal


def _dec(v) -> Decimal:
    if isinstance(v, Decimal):
        return v
    return Decimal(str(v))


def current_fee_bps(pool: SwapPool) -> int:
    return pool.current_fee_bps()


def quote_swap(pool: SwapPool, side: str, amount_in: Decimal) -> Quote:
    side = side.strip()
    if amount_in <= 0:
        raise ValueError("amount_in must be > 0")

    fee_bps = current_fee_bps(pool)
    fee_amount = (amount_in * Decimal(fee_bps) / Decimal(10000)).quantize(Decimal("1.000000000000000000"))
    effective_in = amount_in - fee_amount
    if effective_in <= 0:
        raise ValueError("effective amount after fee must be > 0")

    ra = _dec(pool.reserve_a)
    rb = _dec(pool.reserve_b)

    if side == "AtoB":
        # Constant product x*y=K with virtual reserves
        # ΔB = (rb * ΔA_eff) / (ra + ΔA_eff)
        amount_out = (rb * effective_in) / (ra + effective_in)
    elif side == "BtoA":
        amount_out = (ra * effective_in) / (rb + effective_in)
    else:
        raise ValueError("side must be 'AtoB' or 'BtoA'")

    amount_out = amount_out.quantize(Decimal("1.000000000000000000"))
    return Quote(amount_out=amount_out, fee_bps=fee_bps, fee_amount=fee_amount, effective_in=effective_in)


def _get_or_create_balance(session: Session, user_id: int, token_id: int) -> TokenBalance:
    row = (
        session.query(TokenBalance)
        .filter_by(user_id=user_id, token_id=token_id)
        .with_for_update()
        .first()
    )
    if not row:
        row = TokenBalance(user_id=user_id, token_id=token_id, amount=Decimal("0"))
        session.add(row)
        session.flush()
    return row


def _maybe_progress_stage_and_burn(session: Session, pool: SwapPool) -> None:
    # Progress from 1->2, 2->3, 3->4 based on cumulative_volume_a thresholds
    # When crossing a stage, record a burn event for the configured token and amount
    updated = False
    next_stage = int(pool.stage or 1)
    vol_a = _dec(pool.cumulative_volume_a or 0)

    def _check(threshold, target_stage, burn_amount):
        nonlocal next_stage, updated
        thr = threshold
        if thr is not None:
            thr = _dec(threshold)
        if thr is not None and vol_a >= thr and int(pool.stage) < target_stage:
            next_stage = target_stage
            if pool.burn_token_id and burn_amount:
                session.add(
                    BurnEvent(
                        pool_id=pool.id,
                        stage=target_stage,
                        token_id=pool.burn_token_id,
                        amount=_dec(burn_amount),
                    )
                )
            updated = True

    _check(pool.stage1_threshold, 2, pool.burn_stage1_amount)
    _check(pool.stage2_threshold, 3, pool.burn_stage2_amount)
    _check(pool.stage3_threshold, 4, pool.burn_stage4_amount)

    if updated and next_stage != pool.stage:
        pool.stage = next_stage
        session.add(pool)


def execute_swap(session: Session, pool_id: int, user_id: int, side: str, amount_in: Decimal) -> Tuple[SwapTrade, Quote, SwapPool]:
    # Lock pool row (best-effort; ignored on SQLite)
    pool = session.query(SwapPool).filter_by(id=pool_id).with_for_update().first()
    if not pool:
        raise ValueError("pool_not_found")

    # Determine token ids for in/out
    token_a_id = pool.token_a_id
    token_b_id = pool.token_b_id

    # Quote
    q = quote_swap(pool, side, amount_in)

    # Balances
    if side == "AtoB":
        bal_in = _get_or_create_balance(session, user_id, token_a_id)
        bal_out = _get_or_create_balance(session, user_id, token_b_id)
        if _dec(bal_in.amount) < amount_in:
            raise ValueError("insufficient_balance")
        # Update user balances
        bal_in.amount = _dec(bal_in.amount) - amount_in
        bal_out.amount = _dec(bal_out.amount) + q.amount_out
        session.add(bal_in)
        session.add(bal_out)
        # Update pool reserves (virtual)
        pool.reserve_a = _dec(pool.reserve_a) + q.effective_in
        pool.reserve_b = _dec(pool.reserve_b) - q.amount_out
        # Accumulate fees taken from token A
        pool.fee_accum_a = _dec(pool.fee_accum_a or 0) + q.fee_amount
        # Update cumulative volumes
        pool.cumulative_volume_a = _dec(pool.cumulative_volume_a or 0) + amount_in
        pool.cumulative_volume_b = _dec(pool.cumulative_volume_b or 0) + q.amount_out
    elif side == "BtoA":
        bal_in = _get_or_create_balance(session, user_id, token_b_id)
        bal_out = _get_or_create_balance(session, user_id, token_a_id)
        if _dec(bal_in.amount) < amount_in:
            raise ValueError("insufficient_balance")
        bal_in.amount = _dec(bal_in.amount) - amount_in
        bal_out.amount = _dec(bal_out.amount) + q.amount_out
        session.add(bal_in)
        session.add(bal_out)
        pool.reserve_b = _dec(pool.reserve_b) + q.effective_in
        pool.reserve_a = _dec(pool.reserve_a) - q.amount_out
        # Accumulate fees taken from token B
        pool.fee_accum_b = _dec(pool.fee_accum_b or 0) + q.fee_amount
        pool.cumulative_volume_b = _dec(pool.cumulative_volume_b or 0) + amount_in
        pool.cumulative_volume_a = _dec(pool.cumulative_volume_a or 0) + q.amount_out
    else:
        raise ValueError("invalid_side")

    # Safety checks
    if _dec(pool.reserve_a) <= 0 or _dec(pool.reserve_b) <= 0:
        raise ValueError("pool_exhausted")

    # Persist pool update and maybe stage progression & burn
    session.add(pool)
    _maybe_progress_stage_and_burn(session, pool)

    # Record trade
    trade = SwapTrade(
        pool_id=pool.id,
        user_id=user_id,
        side=side,
        amount_in=amount_in,
        amount_out=q.amount_out,
        fee_paid=q.fee_amount,
        stage=int(pool.stage or 1),
        burn_amount=None,  # burn, if any, is recorded as BurnEvent separately
    )
    session.add(trade)
    session.flush()

    return trade, q, pool
