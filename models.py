from pydantic import BaseModel
from typing import Optional
from uuid import UUID
from enum import Enum
from datetime import date

class AccountType(str, Enum):
    taxable_brokerage = "taxable_brokerage"
    traditional_ira = "traditional_ira"
    roth_ira = "roth_ira"

class AssetClass(str, Enum):
    us_equity = "us_equity"
    intl_equity = "intl_equity"
    us_bond = "us_bond"
    intl_bond = "intl_bond"
    cash_equivalent = "cash_equivalent"

class TaxLot(BaseModel):
    lot_id: UUID
    holding_id: UUID
    purchase_date: date
    shares: float
    cost_per_share: float
    current_nav: float

    @property
    def is_long_term(self) -> bool:
        from datetime import date as d
        return (d.today() - self.purchase_date).days >= 365

    @property
    def unrealized_gain_loss(self) -> float:
        return (self.current_nav - self.cost_per_share) * self.shares

class FundHolding(BaseModel):
    holding_id: UUID
    account_id: UUID
    fund_symbol: str
    fund_name: str
    total_shares: float
    current_nav: float
    asset_class: AssetClass
    target_allocation_pct: float
    current_allocation_pct: float
    tax_lots: list[TaxLot] = []

    @property
    def current_value(self) -> float:
        return self.total_shares * self.current_nav

    @property
    def allocation_drift(self) -> float:
        return self.current_allocation_pct - self.target_allocation_pct

class SellRequest(BaseModel):
    investor_id: UUID
    account_type: AccountType
    target_withdrawal_amount: float
    federal_tax_bracket: float
    ltcg_tax_rate: float
    holdings: list[FundHolding]

class RecommendedFundSale(BaseModel):
    fund_symbol: str
    fund_name: str
    shares_to_sell: float
    estimated_proceeds: float
    estimated_tax_impact: float
    tax_savings_vs_fifo: float
    rationale: str

class SellRecommendation(BaseModel):
    investor_id: UUID
    total_proceeds: float
    total_estimated_tax: float
    effective_tax_rate_on_sale: float
    recommended_sales: list[RecommendedFundSale]

class ManualFundSale(BaseModel):
    holding_id: UUID
    fund_symbol: str
    fund_name: str
    user_entered_amount: float  # Dollar amount the user wants to sell
    current_nav: float
    asset_class: AssetClass
    current_allocation_pct: float
    target_allocation_pct: float
    tax_lots: list[TaxLot] = []

class ScenarioTaxImpact(BaseModel):
    fund_symbol: str
    fund_name: str
    user_entered_amount: float
    derived_shares: float
    estimated_stcg: float        # Short-term gain/loss amount
    estimated_ltcg: float        # Long-term gain/loss amount
    estimated_stcg_tax: float    # Tax on short-term portion
    estimated_ltcg_tax: float    # Tax on long-term portion
    estimated_total_tax: float
    is_tax_loss: bool
    allocation_after_sale: float
    warning: Optional[str] = None  # e.g. wash sale risk, exceeds available shares

class ManualScenarioRequest(BaseModel):
    investor_id: UUID
    account_type: AccountType
    federal_tax_bracket: float
    ltcg_tax_rate: float
    portfolio_total_value: float  # Needed to calculate allocation_after_sale
    source_recommendation_id: Optional[UUID] = None  # Populated if pre-filled from Wf-A
    fund_sales: list[ManualFundSale]

class ManualScenarioResponse(BaseModel):
    investor_id: UUID
    source_recommendation_id: Optional[UUID]
    total_sell_amount: float
    total_estimated_stcg: float
    total_estimated_ltcg: float
    total_estimated_stcg_tax: float
    total_estimated_ltcg_tax: float
    total_estimated_tax: float
    effective_tax_rate_on_sale: float
    fund_impacts: list[ScenarioTaxImpact]