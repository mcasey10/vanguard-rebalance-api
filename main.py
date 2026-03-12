from fastapi import FastAPI
from models import (SellRequest, SellRecommendation, RecommendedFundSale,
                    TaxLot, ManualScenarioRequest, ManualScenarioResponse,
                    ScenarioTaxImpact, ManualFundSale)
from datetime import date
from typing import List

app = FastAPI(title="Vanguard Sell & Rebalance API", version="0.1.0")

@app.get("/")
def root():
    return {"message": "Vanguard Sell & Rebalance API is running"}

def classify_lot(lot: TaxLot) -> str:
    """
    Returns one of four priority categories for lot selection.
    Lower number = sell first (most tax-efficient).
    """
    is_long_term = (date.today() - lot.purchase_date).days >= 365
    gain_loss = (lot.current_nav - lot.cost_per_share) * lot.shares

    if not is_long_term and gain_loss < 0:
        return "1_st_loss"      # Best: short-term loss, harvest first
    elif is_long_term and gain_loss < 0:
        return "2_lt_loss"      # Good: long-term loss
    elif is_long_term and gain_loss >= 0:
        return "3_lt_gain"      # Acceptable: long-term gain, lower rate
    else:
        return "4_st_gain"      # Last resort: short-term gain, highest tax

def tax_rate_for_lot(lot: TaxLot, request: SellRequest) -> float:
    """
    Returns the applicable tax rate for a given lot.
    Loss lots return 0.0 — they generate a deductible loss, not a tax.
    """
    is_long_term = (date.today() - lot.purchase_date).days >= 365
    gain_loss = (lot.current_nav - lot.cost_per_share) * lot.shares

    if gain_loss < 0:
        return 0.0
    elif is_long_term:
        return request.ltcg_tax_rate
    else:
        return request.federal_tax_bracket

def calculate_fifo_tax_per_fund(request: SellRequest) -> dict:
    """
    Simulates FIFO sell and returns tax broken down by fund symbol.
    FIFO sells oldest lots first across the entire portfolio.
    Returns: dict of fund_symbol -> fifo_tax_amount
    """
    # Flatten all lots with their parent fund, sort by purchase date
    all_lots = []
    for holding in request.holdings:
        for lot in holding.tax_lots:
            all_lots.append({
                "lot": lot,
                "fund_symbol": holding.fund_symbol,
                "tax_rate": tax_rate_for_lot(lot, request)
            })

    all_lots.sort(key=lambda x: x["lot"].purchase_date)

    remaining = request.target_withdrawal_amount
    fifo_tax_by_fund = {}

    for item in all_lots:
        if remaining <= 0:
            break
        lot = item["lot"]
        symbol = item["fund_symbol"]
        max_proceeds = lot.shares * lot.current_nav
        proceeds_to_take = min(remaining, max_proceeds)
        tax = proceeds_to_take * item["tax_rate"]

        fifo_tax_by_fund[symbol] = fifo_tax_by_fund.get(symbol, 0.0) + tax
        remaining -= proceeds_to_take

    return fifo_tax_by_fund

@app.post("/recommend", response_model=SellRecommendation)
def get_sell_recommendation(request: SellRequest):
    """
    Workflow A: automated sell recommendation using lot-level 
    MinTax prioritization.
    
    Algorithm:
    1. Flatten all lots across all holdings into a single list
    2. Sort by tax efficiency (losses first, then LT gains, then ST gains)
    3. Fill the target withdrawal amount lot by lot
    4. Group results back by fund for the response
    """
    # Step 1: Flatten all lots, tagging each with its parent holding
    all_lots = []
    holding_map = {str(h.holding_id): h for h in request.holdings}

    for holding in request.holdings:
        for lot in holding.tax_lots:
            all_lots.append({
                "lot": lot,
                "holding": holding,
                "priority": classify_lot(lot),
                "tax_rate": tax_rate_for_lot(lot, request),
                "gain_loss": (lot.current_nav - lot.cost_per_share) * lot.shares
            })

    # Step 2: Sort by priority category (string sort works: 1_ < 2_ < 3_ < 4_)
    all_lots.sort(key=lambda x: x["priority"])

    # Step 3: Fill target amount lot by lot
    remaining = request.target_withdrawal_amount
    sales_by_fund = {}  # fund_symbol -> accumulated sale data
    total_tax = 0.0
    total_proceeds = 0.0

    for item in all_lots:
        if remaining <= 0:
            break

        lot = item["lot"]
        holding = item["holding"]
        max_proceeds_from_lot = lot.shares * lot.current_nav

        # Take only what we need from this lot
        proceeds_to_take = min(remaining, max_proceeds_from_lot)
        shares_to_sell = proceeds_to_take / lot.current_nav
        cost_basis = shares_to_sell * lot.cost_per_share
        gain = proceeds_to_take - cost_basis
        tax_on_this_lot = max(0, gain * item["tax_rate"])

        # Accumulate by fund symbol for grouping
        symbol = holding.fund_symbol
        if symbol not in sales_by_fund:
            sales_by_fund[symbol] = {
                "fund_symbol": symbol,
                "fund_name": holding.fund_name,
                "shares_to_sell": 0.0,
                "estimated_proceeds": 0.0,
                "estimated_tax_impact": 0.0,
                "lot_details": []
            }

        sales_by_fund[symbol]["shares_to_sell"] += shares_to_sell
        sales_by_fund[symbol]["estimated_proceeds"] += proceeds_to_take
        sales_by_fund[symbol]["estimated_tax_impact"] += tax_on_this_lot
        sales_by_fund[symbol]["lot_details"].append(
            f"{item['priority']} | {shares_to_sell:.2f} shares @ "
            f"${lot.current_nav} | tax: ${tax_on_this_lot:.2f}"
        )

        total_proceeds += proceeds_to_take
        total_tax += tax_on_this_lot
        remaining -= proceeds_to_take

    # Step 4: Calculate per-fund FIFO baseline for accurate savings attribution
    fifo_tax_by_fund = calculate_fifo_tax_per_fund(request)

    recommended_sales = []
    for symbol, data in sales_by_fund.items():
        # Savings = what FIFO would have charged this fund minus what MinTax charged
        # If FIFO wouldn't have sold this fund at all, fifo tax = 0
        fifo_tax_for_fund = fifo_tax_by_fund.get(symbol, 0.0)
        mintax_tax_for_fund = data["estimated_tax_impact"]
        fund_tax_savings = round(fifo_tax_for_fund - mintax_tax_for_fund, 2)

        rationale = "; ".join(data["lot_details"])

        recommended_sales.append(RecommendedFundSale(
            fund_symbol=data["fund_symbol"],
            fund_name=data["fund_name"],
            shares_to_sell=round(data["shares_to_sell"], 4),
            estimated_proceeds=round(data["estimated_proceeds"], 2),
            estimated_tax_impact=round(data["estimated_tax_impact"], 2),
            tax_savings_vs_fifo=fund_tax_savings,
            rationale=rationale
        ))

    effective_rate = (total_tax / total_proceeds) if total_proceeds > 0 else 0.0

    return SellRecommendation(
        investor_id=request.investor_id,
        total_proceeds=round(total_proceeds, 2),
        total_estimated_tax=round(total_tax, 2),
        effective_tax_rate_on_sale=round(effective_rate, 4),
        recommended_sales=recommended_sales
    )

def calculate_lot_level_tax(
    fund_sale: ManualFundSale,
    request: ManualScenarioRequest
) -> dict:
    """
    Given a user-entered dollar amount for a fund, calculate the tax
    impact by consuming tax lots in MinTax priority order.
    
    Returns a dict with stcg, ltcg, stcg_tax, ltcg_tax, and warnings.
    """
    if fund_sale.user_entered_amount <= 0:
        return {
            "stcg": 0.0, "ltcg": 0.0,
            "stcg_tax": 0.0, "ltcg_tax": 0.0,
            "warning": None
        }

    # Sort lots by MinTax priority — same logic as Workflow A
    sorted_lots = sorted(
        fund_sale.tax_lots,
        key=lambda lot: classify_lot(lot)
    )

    remaining = fund_sale.user_entered_amount
    total_stcg = 0.0
    total_ltcg = 0.0
    total_stcg_tax = 0.0
    total_ltcg_tax = 0.0
    warning = None

    # Check if user is trying to sell more than available
    max_available = sum(lot.shares * lot.current_nav for lot in fund_sale.tax_lots)
    if fund_sale.user_entered_amount > max_available:
        warning = f"Amount exceeds available value of ${max_available:,.2f}"
        remaining = max_available  # Cap at available

    for lot in sorted_lots:
        if remaining <= 0:
            break

        lot_max_proceeds = lot.shares * lot.current_nav
        proceeds_to_take = min(remaining, lot_max_proceeds)
        shares_sold = proceeds_to_take / lot.current_nav
        is_long_term = (date.today() - lot.purchase_date).days >= 365

        # Calculate gain/loss on the portion being sold
        cost_basis_sold = shares_sold * lot.cost_per_share
        gain_loss = proceeds_to_take - cost_basis_sold

        if is_long_term:
            total_ltcg += gain_loss
            tax = max(0, gain_loss * request.ltcg_tax_rate)
            total_ltcg_tax += tax
        else:
            total_stcg += gain_loss
            tax = max(0, gain_loss * request.federal_tax_bracket)
            total_stcg_tax += tax

        remaining -= proceeds_to_take

    return {
        "stcg": round(total_stcg, 2),
        "ltcg": round(total_ltcg, 2),
        "stcg_tax": round(total_stcg_tax, 2),
        "ltcg_tax": round(total_ltcg_tax, 2),
        "warning": warning
    }


@app.post("/scenario", response_model=ManualScenarioResponse)
def calculate_scenario(request: ManualScenarioRequest):
    """
    Workflow B: real-time tax calculation on user-entered sell amounts.
    
    Called every time the user changes any amount in the manual fund list.
    No optimization — pure tax math on whatever amounts are provided.
    Returns per-fund tax breakdown plus rolled-up scenario totals.
    """
    fund_impacts = []
    total_sell = 0.0
    total_stcg = 0.0
    total_ltcg = 0.0
    total_stcg_tax = 0.0
    total_ltcg_tax = 0.0

    for fund_sale in request.fund_sales:
        # Calculate lot-level tax for this fund's entered amount
        tax_result = calculate_lot_level_tax(fund_sale, request)

        derived_shares = (
            fund_sale.user_entered_amount / fund_sale.current_nav
            if fund_sale.current_nav > 0 else 0
        )

        # Calculate what allocation will be after this sale
        value_after_sale = (
            (fund_sale.current_allocation_pct * request.portfolio_total_value)
            - fund_sale.user_entered_amount
        )
        allocation_after_sale = max(0, value_after_sale / (
            request.portfolio_total_value - fund_sale.user_entered_amount
        )) if request.portfolio_total_value > fund_sale.user_entered_amount else 0.0

        total_tax_for_fund = tax_result["stcg_tax"] + tax_result["ltcg_tax"]
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
            warning=tax_result["warning"]
        ))

        total_sell += fund_sale.user_entered_amount
        total_stcg += tax_result["stcg"]
        total_ltcg += tax_result["ltcg"]
        total_stcg_tax += tax_result["stcg_tax"]
        total_ltcg_tax += tax_result["ltcg_tax"]

    total_tax = total_stcg_tax + total_ltcg_tax
    effective_rate = (total_tax / total_sell) if total_sell > 0 else 0.0

    return ManualScenarioResponse(
        investor_id=request.investor_id,
        source_recommendation_id=request.source_recommendation_id,
        total_sell_amount=round(total_sell, 2),
        total_estimated_stcg=round(total_stcg, 2),
        total_estimated_ltcg=round(total_ltcg, 2),
        total_estimated_stcg_tax=round(total_stcg_tax, 2),
        total_estimated_ltcg_tax=round(total_ltcg_tax, 2),
        total_estimated_tax=round(total_tax, 2),
        effective_tax_rate_on_sale=round(effective_rate, 4),
        fund_impacts=fund_impacts
    )