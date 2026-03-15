from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from models import (SellRequest, SellRecommendation, RecommendedFundSale,
                    TaxLot, ManualScenarioRequest, ManualScenarioResponse,
                    ScenarioTaxImpact, ManualFundSale, FundHolding)
from datetime import date
from typing import List
import anthropic
import os
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Vanguard Sell & Rebalance API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://vanguard-rebalance-ui.vercel.app"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.options("/{rest_of_path:path}")
async def preflight_handler(rest_of_path: str):
    response = Response()
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "*"
    return response

@app.get("/")
def root():
    return {"message": "Vanguard Sell & Rebalance API is running"}

# ─── Lot-level helpers ────────────────────────────────────────────────────────

def classify_lot(lot: TaxLot) -> str:
    """
    Returns one of four priority categories for lot selection.
    Lower string sorts first (most tax-efficient).
    """
    is_long_term = (date.today() - lot.purchase_date).days >= 365
    gain_loss = (lot.current_nav - lot.cost_per_share) * lot.shares

    if not is_long_term and gain_loss < 0:
        return "1_st_loss"      # Best: short-term loss
    elif is_long_term and gain_loss < 0:
        return "2_lt_loss"      # Good: long-term loss (tax-loss harvesting)
    elif is_long_term and gain_loss >= 0:
        return "3_lt_gain"      # Acceptable: long-term gain, lower rate
    else:
        return "4_st_gain"      # Last resort: short-term gain, highest tax


def best_lot_category(holding: FundHolding) -> str:
    """Returns the best (lowest) lot category available for a holding."""
    if not holding.tax_lots:
        return "4_st_gain"
    return min(classify_lot(lot) for lot in holding.tax_lots)


def tax_rate_for_lot(lot: TaxLot, federal_bracket: float, ltcg_rate: float) -> float:
    """
    Returns the applicable tax rate for a given lot.
    Loss lots return 0.0 — they generate a deductible loss, not a tax.
    """
    is_long_term = (date.today() - lot.purchase_date).days >= 365
    gain_loss = (lot.current_nav - lot.cost_per_share) * lot.shares

    if gain_loss < 0:
        return 0.0
    elif is_long_term:
        return ltcg_rate
    else:
        return federal_bracket


def mintax_sell_from_fund(
    holding: FundHolding,
    target_amount: float,
    federal_bracket: float,
    ltcg_rate: float,
) -> dict:
    """
    Phase 2: Given a target dollar amount to sell from a single fund,
    consume lots in MinTax priority order (losses first, then LT gains,
    then ST gains). Returns accumulated sale data for that fund.
    """
    sorted_lots = sorted(holding.tax_lots, key=classify_lot)

    remaining = target_amount
    shares_to_sell = 0.0
    estimated_proceeds = 0.0
    estimated_tax = 0.0
    lot_details = []

    for lot in sorted_lots:
        if remaining <= 0:
            break

        lot_max_proceeds = lot.shares * lot.current_nav
        proceeds_to_take = min(remaining, lot_max_proceeds)
        shares_sold = proceeds_to_take / lot.current_nav
        cost_basis = shares_sold * lot.cost_per_share
        gain = proceeds_to_take - cost_basis
        rate = tax_rate_for_lot(lot, federal_bracket, ltcg_rate)
        tax_on_lot = max(0.0, gain * rate)

        shares_to_sell += shares_sold
        estimated_proceeds += proceeds_to_take
        estimated_tax += tax_on_lot

        category = classify_lot(lot)
        lot_details.append(
            f"{category} | {shares_sold:.2f} shares @ ${lot.current_nav} | tax: ${tax_on_lot:.2f}"
        )
        remaining -= proceeds_to_take

    return {
        "fund_symbol": holding.fund_symbol,
        "fund_name": holding.fund_name,
        "shares_to_sell": shares_to_sell,
        "estimated_proceeds": estimated_proceeds,
        "estimated_tax_impact": estimated_tax,
        "lot_details": lot_details,
    }


# ─── FIFO baseline (for savings calculation) ─────────────────────────────────

def calculate_fifo_tax_per_fund(request: SellRequest) -> dict:
    """
    Simulates FIFO sell and returns tax broken down by fund symbol.
    Used only for computing tax_savings_vs_fifo on the response.
    """
    all_lots = []
    for holding in request.holdings:
        for lot in holding.tax_lots:
            is_lt = (date.today() - lot.purchase_date).days >= 365
            gain = (lot.current_nav - lot.cost_per_share) * lot.shares
            rate = (
                0.0 if gain < 0
                else request.ltcg_tax_rate if is_lt
                else request.federal_tax_bracket
            )
            all_lots.append({"lot": lot, "fund_symbol": holding.fund_symbol, "tax_rate": rate})

    all_lots.sort(key=lambda x: x["lot"].purchase_date)

    remaining = request.target_withdrawal_amount
    fifo_tax_by_fund: dict[str, float] = {}

    for item in all_lots:
        if remaining <= 0:
            break
        lot = item["lot"]
        proceeds = min(remaining, lot.shares * lot.current_nav)
        fifo_tax_by_fund[item["fund_symbol"]] = (
            fifo_tax_by_fund.get(item["fund_symbol"], 0.0) + proceeds * item["tax_rate"]
        )
        remaining -= proceeds

    return fifo_tax_by_fund


# ─── Phase 1: Fund-level allocation ──────────────────────────────────────────

# Maximum fraction of the total withdrawal that any single rebalancing fund
# may absorb before loss-harvesting funds are given their share.
# At 0.60: a fund over-allocated by more than 60% of the withdrawal is capped,
# and the remainder flows to loss-harvesting funds.
MAX_REBALANCE_FRACTION = 0.60

# Minimum allocation drift (as a decimal fraction of portfolio) required for a
# fund to qualify as a rebalancing candidate in Phase 1 Step A.
# Funds with drift below this threshold are treated as effectively at-target
# and fall into the fill_lt bucket instead, preventing noise-level over-allocation
# from consuming withdrawal dollars ahead of loss-harvesting funds.
# 0.005 = 0.5 percentage points (e.g., 20.3% current vs 20.0% target = 0.3pp → skip)
MIN_DRIFT_THRESHOLD = 0.005


def allocate_withdrawal(request: SellRequest) -> list[dict]:
    """
    Phase 1 of the two-phase MinTax algorithm.

    Separates holdings into three buckets and allocates the withdrawal:

    REBALANCE bucket — over-allocated funds whose best available lots are
        LT-favorable (category 3_lt_gain or better). These receive withdrawal
        dollars first, capped at MAX_REBALANCE_FRACTION of the total so that
        loss-harvesting funds always get a share.

    HARVEST bucket — funds with loss lots (category 1_st_loss or 2_lt_loss),
        regardless of allocation drift. These receive the remainder after
        rebalancing funds are filled, up to remaining need.

    FILL bucket — any at-target or under-target funds with LT gains.
        Used only if rebalancing + harvesting do not cover the full withdrawal.

    LAST RESORT — ST-gain-only funds, consumed only if no other option exists.

    Returns a list of {holding, target_amount} dicts for Phase 2.
    """
    withdrawal = request.target_withdrawal_amount

    rebalance: list[dict]  = []   # over-allocated, LT-favorable
    harvest:   list[dict]  = []   # loss lots available
    fill_lt:   list[dict]  = []   # at-target, LT gain
    last_resort: list[dict] = []  # ST-gain only

    for holding in request.holdings:
        drift = holding.current_allocation_pct - holding.target_allocation_pct
        best_cat = best_lot_category(holding)
        current_value = holding.total_shares * holding.current_nav

        entry = {"holding": holding, "drift": drift, "best_cat": best_cat,
                 "capacity": current_value}

        if best_cat in ("1_st_loss", "2_lt_loss"):
            # Has loss lots → always a harvesting candidate
            harvest.append(entry)
            # Also add to rebalance bucket only if meaningfully over-allocated
            if drift > MIN_DRIFT_THRESHOLD:
                rebalance.append(entry)
        elif best_cat == "3_lt_gain":
            if drift > MIN_DRIFT_THRESHOLD:
                rebalance.append(entry)
            else:
                fill_lt.append(entry)
        else:
            # 4_st_gain only
            last_resort.append(entry)

    # Sort rebalance bucket: highest drift first, then best tax category
    rebalance.sort(key=lambda e: (-e["drift"], e["best_cat"]))
    # Sort harvest bucket: largest loss capacity first
    harvest.sort(key=lambda e: e["best_cat"])

    allocations: dict[str, float] = {}  # holding_id → target_amount
    remaining = withdrawal

    # ── Step A: Rebalancing funds (capped) ───────────────────────────────────
    rebalance_cap = withdrawal * MAX_REBALANCE_FRACTION
    for entry in rebalance:
        if remaining <= 0:
            break
        # Skip if this fund is also in harvest — it will be handled there
        # (we don't want to double-allocate a fund that has both drift and losses)
        if entry["best_cat"] in ("1_st_loss", "2_lt_loss"):
            continue
        h = entry["holding"]
        # Use the full portfolio total value (passed in from the frontend) so that
        # allocation drift percentages are calculated against the correct denominator.
        # Summing only brokerage holdings (~$453k) would understate the portfolio
        # and inflate over-allocation dollars for every fund.
        portfolio_total = sum(h2.total_shares * h2.current_nav for h2 in request.holdings)
        over_alloc_dollars = entry["drift"] * portfolio_total
        # Cap: don't sell more than over-allocation, more than rebalance_cap,
        #      more than remaining withdrawal, or more than fund capacity
        fund_alloc = min(over_alloc_dollars, rebalance_cap, remaining, entry["capacity"])
        if fund_alloc > 0:
            hid = str(h.holding_id)
            allocations[hid] = allocations.get(hid, 0.0) + fund_alloc
            remaining -= fund_alloc

    # ── Step B: Loss-harvesting funds ────────────────────────────────────────
    for entry in harvest:
        if remaining <= 0:
            break
        h = entry["holding"]
        hid = str(h.holding_id)
        # Give this fund up to what's left (but don't exceed its capacity)
        fund_alloc = min(remaining, entry["capacity"])
        if fund_alloc > 0:
            allocations[hid] = allocations.get(hid, 0.0) + fund_alloc
            remaining -= fund_alloc

    # ── Step C: At-target LT-gain funds (if still short) ────────────────────
    if remaining > 0:
        fill_lt.sort(key=lambda e: e["best_cat"])
        for entry in fill_lt:
            if remaining <= 0:
                break
            h = entry["holding"]
            hid = str(h.holding_id)
            fund_alloc = min(remaining, entry["capacity"])
            if fund_alloc > 0:
                allocations[hid] = allocations.get(hid, 0.0) + fund_alloc
                remaining -= fund_alloc

    # ── Step D: Last resort — ST-gain funds ──────────────────────────────────
    if remaining > 0:
        for entry in last_resort:
            if remaining <= 0:
                break
            h = entry["holding"]
            hid = str(h.holding_id)
            fund_alloc = min(remaining, entry["capacity"])
            if fund_alloc > 0:
                allocations[hid] = allocations.get(hid, 0.0) + fund_alloc
                remaining -= fund_alloc

    # Build result list ordered by allocation size descending
    result = []
    for holding in request.holdings:
        hid = str(holding.holding_id)
        amt = allocations.get(hid, 0.0)
        if amt > 0:
            result.append({"holding": holding, "target_amount": amt})

    result.sort(key=lambda x: -x["target_amount"])
    return result


# ─── /recommend endpoint ─────────────────────────────────────────────────────

@app.post("/recommend", response_model=SellRecommendation)
def get_sell_recommendation(request: SellRequest):
    """
    Workflow A: automated sell recommendation using a two-phase MinTax algorithm.

    Phase 1 (fund allocation): determines how much to sell from each fund,
        prioritising over-allocated funds first (rebalancing), then funds with
        loss lots (tax-loss harvesting), then at-target LT-gain funds, and
        finally ST-gain funds as a last resort.  A MAX_REBALANCE_FRACTION cap
        prevents any single rebalancing fund from consuming the entire withdrawal,
        ensuring loss-harvesting funds always participate.

    Phase 2 (lot selection): within each fund's allocated amount, selects lots
        in MinTax priority order (ST losses → LT losses → LT gains → ST gains).
    """
    # Phase 1: determine per-fund allocation
    fund_allocations = allocate_withdrawal(request)

    # Phase 2: lot-level MinTax selection within each fund's allocation
    fifo_tax_by_fund = calculate_fifo_tax_per_fund(request)

    recommended_sales = []
    total_proceeds = 0.0
    total_tax = 0.0

    for item in fund_allocations:
        holding = item["holding"]
        target_amount = item["target_amount"]

        sale_data = mintax_sell_from_fund(
            holding, target_amount,
            request.federal_tax_bracket,
            request.ltcg_tax_rate,
        )

        fifo_tax = fifo_tax_by_fund.get(holding.fund_symbol, 0.0)
        tax_savings = round(fifo_tax - sale_data["estimated_tax_impact"], 2)
        rationale = "; ".join(sale_data["lot_details"])

        recommended_sales.append(RecommendedFundSale(
            fund_symbol=sale_data["fund_symbol"],
            fund_name=sale_data["fund_name"],
            shares_to_sell=round(sale_data["shares_to_sell"], 4),
            estimated_proceeds=round(sale_data["estimated_proceeds"], 2),
            estimated_tax_impact=round(sale_data["estimated_tax_impact"], 2),
            tax_savings_vs_fifo=tax_savings,
            rationale=rationale,
        ))

        total_proceeds += sale_data["estimated_proceeds"]
        total_tax += sale_data["estimated_tax_impact"]

    effective_rate = (total_tax / total_proceeds) if total_proceeds > 0 else 0.0

    return SellRecommendation(
        investor_id=request.investor_id,
        total_proceeds=round(total_proceeds, 2),
        total_estimated_tax=round(total_tax, 2),
        effective_tax_rate_on_sale=round(effective_rate, 4),
        recommended_sales=recommended_sales,
    )


# ─── /scenario endpoint ───────────────────────────────────────────────────────

def calculate_lot_level_tax(
    fund_sale: ManualFundSale,
    federal_bracket: float,
    ltcg_rate: float,
) -> dict:
    """
    Given a user-entered dollar amount for a fund, calculate the tax impact
    by consuming tax lots in MinTax priority order.
    """
    if fund_sale.user_entered_amount <= 0:
        return {"stcg": 0.0, "ltcg": 0.0, "stcg_tax": 0.0, "ltcg_tax": 0.0, "warning": None}

    sorted_lots = sorted(fund_sale.tax_lots, key=classify_lot)

    remaining = fund_sale.user_entered_amount
    total_stcg = total_ltcg = total_stcg_tax = total_ltcg_tax = 0.0
    warning = None

    max_available = sum(lot.shares * lot.current_nav for lot in fund_sale.tax_lots)
    if fund_sale.user_entered_amount > max_available:
        warning = f"Amount exceeds available value of ${max_available:,.2f}"
        remaining = max_available

    for lot in sorted_lots:
        if remaining <= 0:
            break

        proceeds_to_take = min(remaining, lot.shares * lot.current_nav)
        shares_sold = proceeds_to_take / lot.current_nav
        is_long_term = (date.today() - lot.purchase_date).days >= 365
        gain_loss = proceeds_to_take - shares_sold * lot.cost_per_share

        if is_long_term:
            total_ltcg += gain_loss
            total_ltcg_tax += gain_loss * ltcg_rate   # negative when loss — intentional
        else:
            total_stcg += gain_loss
            total_stcg_tax += gain_loss * federal_bracket  # negative when loss — intentional

        remaining -= proceeds_to_take

    return {
        "stcg": round(total_stcg, 2),
        "ltcg": round(total_ltcg, 2),
        "stcg_tax": round(total_stcg_tax, 2),
        "ltcg_tax": round(total_ltcg_tax, 2),
        "warning": warning,
    }


@app.post("/scenario", response_model=ManualScenarioResponse)
def calculate_scenario(request: ManualScenarioRequest):
    """
    Workflow B: real-time tax calculation on user-entered sell amounts.
    No optimisation — pure tax math on whatever amounts the user provides.
    """
    fund_impacts = []
    total_sell = total_stcg = total_ltcg = total_stcg_tax = total_ltcg_tax = 0.0

    for fund_sale in request.fund_sales:
        tax_result = calculate_lot_level_tax(
            fund_sale, request.federal_tax_bracket, request.ltcg_tax_rate
        )

        derived_shares = (
            fund_sale.user_entered_amount / fund_sale.current_nav
            if fund_sale.current_nav > 0 else 0.0
        )

        value_after_sale = (
            fund_sale.current_allocation_pct * request.portfolio_total_value
            - fund_sale.user_entered_amount
        )
        portfolio_after = request.portfolio_total_value - fund_sale.user_entered_amount
        allocation_after_sale = (
            max(0.0, value_after_sale / portfolio_after)
            if portfolio_after > 0 else 0.0
        )

        total_tax_for_fund = max(0.0, tax_result["stcg_tax"] + tax_result["ltcg_tax"])
        net_gain_loss = tax_result["stcg"] + tax_result["ltcg"]

        fund_impacts.append(ScenarioTaxImpact(
            fund_symbol=fund_sale.fund_symbol,
            fund_name=fund_sale.fund_name,
            user_entered_amount=fund_sale.user_entered_amount,
            derived_shares=round(derived_shares, 4),
            estimated_stcg=tax_result["stcg"],
            estimated_ltcg=tax_result["ltcg"],
            estimated_stcg_tax=tax_result["stcg_tax"],
            estimated_ltcg_tax=tax_result["ltcg_tax"],
            estimated_total_tax=round(total_tax_for_fund, 2),
            is_tax_loss=net_gain_loss < 0,
            allocation_after_sale=round(allocation_after_sale, 4),
            warning=tax_result["warning"],
        ))

        total_sell += fund_sale.user_entered_amount
        total_stcg += tax_result["stcg"]
        total_ltcg += tax_result["ltcg"]
        total_stcg_tax += tax_result["stcg_tax"]
        total_ltcg_tax += tax_result["ltcg_tax"]

    # ── Portfolio-level netting ───────────────────────────────────────────────
    # ST losses offset ST gains first; any excess ST loss offsets LT gains.
    # LT losses offset LT gains. Tax is floored at zero — no refund possible.
    # This is the correct IRS treatment for capital gain/loss netting.
    net_stcg = total_stcg
    net_ltcg = total_ltcg

    # If net ST is a loss, apply it against LT gains
    if net_stcg < 0:
        net_ltcg += net_stcg   # e.g. −500 ST loss reduces LT gain by $500
        net_stcg = 0.0

    # Apply rates on netted amounts, clamped at zero
    portfolio_stcg_tax = max(0.0, net_stcg * request.federal_tax_bracket)
    portfolio_ltcg_tax = max(0.0, net_ltcg * request.ltcg_tax_rate)
    total_tax = portfolio_stcg_tax + portfolio_ltcg_tax

    effective_rate = (total_tax / total_sell) if total_sell > 0 else 0.0

    return ManualScenarioResponse(
        investor_id=request.investor_id,
        source_recommendation_id=request.source_recommendation_id,
        total_sell_amount=round(total_sell, 2),
        total_estimated_stcg=round(total_stcg, 2),
        total_estimated_ltcg=round(total_ltcg, 2),
        total_estimated_stcg_tax=round(portfolio_stcg_tax, 2),
        total_estimated_ltcg_tax=round(portfolio_ltcg_tax, 2),
        total_estimated_tax=round(total_tax, 2),
        effective_tax_rate_on_sale=round(effective_rate, 4),
        fund_impacts=fund_impacts,
    )


# ─── /explain endpoint ────────────────────────────────────────────────────────

@app.post("/explain")
def explain_recommendation(recommendation: SellRecommendation):
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    sales_summary = "\n".join([
        f"- {s.fund_name} ({s.fund_symbol}): sell {s.shares_to_sell} shares, "
        f"${s.estimated_proceeds:,.2f} proceeds, ${s.estimated_tax_impact:,.2f} estimated tax, "
        f"${s.tax_savings_vs_fifo:,.2f} saved vs FIFO. Rationale: {s.rationale}"
        for s in recommendation.recommended_sales
    ])

    prompt = f"""You are a helpful financial assistant explaining a tax-optimized sell recommendation to a Vanguard investor.

Here is the recommendation:
- Total proceeds: ${recommendation.total_proceeds:,.2f}
- Total estimated tax: ${recommendation.total_estimated_tax:,.2f}
- Effective tax rate on this sale: {recommendation.effective_tax_rate_on_sale * 100:.1f}%

Fund-level breakdown:
{sales_summary}

Write a clear, friendly 3-4 sentence explanation a non-expert investor would understand.
Explain why these specific funds and lots were chosen, what the tax benefit is, and what the effective rate means in plain terms.
Do not use technical jargon like 'tax lots' or 'FIFO' — translate everything into plain English."""

    message = client.messages.create(
        model="claude-sonnet-4-5-20251101",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    return {
        "investor_id": recommendation.investor_id,
        "explanation": message.content[0].text,
    }
