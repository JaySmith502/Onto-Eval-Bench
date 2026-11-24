"""sec-fin@0.2.0: data subset, schema and tasks. Built by build_sec_package.py --version 0.2.0."""

FORMS = ("10-K", "10-Q", "10-K/A", "10-Q/A")
TOP_N = 300
AMENDERS = 10
PROVENANCE_NOTE = (f"; subset: the {TOP_N} filers with the largest reported Assets plus the {AMENDERS} largest other filers "
                   "that filed a 10-K/A or 10-Q/A in the quarter, without co-registrant copies of another filer's filing")

SCHEMA = """\
-- SEC EDGAR Financial Statement Data Sets (FSDS), one quarter, subset to the largest filers.
-- Readme-level metadata only. Tag labels and definitions are not in this database.

-- One row per filing (submission). Only 10-K and 10-Q forms and their amendments are kept.
CREATE TABLE sub (
  adsh TEXT PRIMARY KEY,  -- accession number, unique filing id
  cik INTEGER,            -- SEC central index key of the registrant
  name TEXT,              -- registrant name as filed (upper case)
  sic INTEGER,            -- standard industrial classification code
  countryba TEXT,         -- country of business address
  form TEXT,              -- submission type: 10-K, 10-Q, or an amendment 10-K/A, 10-Q/A
  period INTEGER,         -- balance sheet date, YYYYMMDD
  fy INTEGER,             -- fiscal year focus as reported by the filer (may differ from the calendar year)
  fp TEXT,                -- fiscal period focus: FY, Q1, Q2, Q3
  filed INTEGER,          -- filing date, YYYYMMDD
  prevrpt INTEGER         -- always 0 in this database (the source flags no filing as amended); find amendments by
                          -- form (10-K/A, 10-Q/A) and the most recent filing by filed
);

-- One row per reported numeric fact. Only facts with no dimensional qualifier (no segments,
-- no co-registrant) and a standard taxonomy tag are kept. Facts belong to a filing, so the same
-- fact can appear in several filings: a filing repeats prior-period comparatives, and an
-- amendment repeats or restates facts of the filing it amends.
CREATE TABLE num (
  adsh TEXT,              -- accession number, joins sub.adsh
  tag TEXT,               -- taxonomy element name, joins tag.tag
  version TEXT,           -- taxonomy version, for example us-gaap/2025
  ddate INTEGER,          -- end date of the fact's period, YYYYMMDD (the instant for balance sheet items)
  qtrs INTEGER,           -- duration in quarters rounded: 0 for an instant (point in time), 1, 2, 3, 4 for durations
  uom TEXT,               -- unit of measure: USD (also for per-share amounts such as earnings per share), shares,
                          -- pure (ratios), EUR
  value REAL,             -- reported value in uom (full units, not thousands or millions)
  PRIMARY KEY (adsh, tag, version, ddate, qtrs, uom)
);

-- One row per standard taxonomy element used in num.
CREATE TABLE tag (
  tag TEXT,               -- taxonomy element name
  version TEXT,           -- taxonomy version
  datatype TEXT,          -- monetary, shares, pure, perShare, string, ...
  iord TEXT,              -- I = instant (point in time), D = duration (period of time)
  crdr TEXT,              -- natural balance: C credit, D debit, empty if not applicable
  abstract INTEGER,       -- 1 if abstract (a heading, never reported with a value)
  PRIMARY KEY (tag, version)
);
"""

P = 20260630
RATIO = {"abs": 0.0001}   # one unit in the 4th decimal: absorbs round-half and unrounded answers, never a wrong concept


def d(x: int) -> str:
    x = str(x)
    return f"{x[:4]}-{x[4:6]}-{x[6:]}"


def src(form: str = "10-Q", period: int = P) -> str:
    return f"as reported in its {form} for the period ended {d(period)}"


def fact(company: str, tag: str, ddate: int = P, qtrs: int = 0, form: str = "10-Q", period: int = P, uom: str = "USD") -> str:
    """One fact from one filing: (name, form, period) names one filing and (adsh, tag, ddate, qtrs, uom) one row."""
    return (f"SELECT n.value FROM sub s JOIN num n ON n.adsh = s.adsh WHERE s.name = '{company}' AND s.form = '{form}' "
            f"AND s.period = {period} AND n.tag = '{tag}' AND n.ddate = {ddate} AND n.qtrs = {qtrs} AND n.uom = '{uom}'")


def v(*a, **k) -> str:
    return f"({fact(*a, **k)})"


def ratio(num: str, den: str, places: int = 4) -> str:
    return f"SELECT ROUND({num} * 1.0 / {den}, {places})"


def task(id, group, difficulty, split, question, sql, note, fmt="number", **extra) -> dict:
    """Every question is scoped to the database, so the unanswerable ones carry no wording of their own."""
    question = f"According to the filings in this database, {question[0].lower()}{question[1:]}"
    return dict(id=id, group=group, task=group, difficulty=difficulty, split=split, question=question, format=fmt,
                sql=sql, note=note, **extra)


ABSTAIN = "NOT_IN_DATABASE"   # adapters/llm_sql.py: the one result that means "the database does not contain this fact"


def absent(id, difficulty, split, question, sql, note) -> dict:
    return task(id, "unanswerable", difficulty, split, question, f"SELECT '{ABSTAIN}' WHERE NOT EXISTS ({sql})", note,
                nullable=True)


NI = ("WITH ni AS (SELECT s.name, s.sic, COALESCE((SELECT value FROM num WHERE adsh = s.adsh AND tag = 'NetIncomeLoss' "
      "AND ddate = 20260630 AND qtrs = 1 AND uom = 'USD'), (SELECT value FROM num WHERE adsh = s.adsh AND tag = 'ProfitLoss' "
      "AND ddate = 20260630 AND qtrs = 1 AND uom = 'USD') - COALESCE((SELECT value FROM num WHERE adsh = s.adsh "
      "AND tag = 'NetIncomeLossAttributableToNoncontrollingInterest' AND ddate = 20260630 AND qtrs = 1 AND uom = 'USD'), 0)) "
      "AS v FROM sub s WHERE s.form = '10-Q' AND s.period = 20260630) ")
NI_NOTE = ("Parent net income is NetIncomeLoss, or ProfitLoss minus NetIncomeLossAttributableToNoncontrollingInterest for "
           "the 36 filers without a NetIncomeLoss fact (SPDR GOLD TRUST reports only ProfitLoss and has no noncontrolling "
           "interests). ")


Q10 = "original 10-Q filings (form 10-Q, not 10-Q/A) with a period ending 2026-06-30"
TOP = ("FROM sub s JOIN num n ON n.adsh = s.adsh WHERE s.form = '10-Q' AND s.period = 20260630 AND n.ddate = 20260630 "
       "AND n.uom = 'USD' AND ")

TASKS: list[dict] = [
    # 1. lookup_instant: balance-sheet facts (qtrs = 0).
    task("li_amd_inventory", "lookup_instant", "simple", "config",
         f"What were the inventories, in US dollars, of ADVANCED MICRO DEVICES INC on 2026-06-30, {src()}?",
         fact("ADVANCED MICRO DEVICES INC", "InventoryNet"), "AMD reports one inventory line, InventoryNet, at 2026-06-30."),
    task("li_pfizer_goodwill", "lookup_instant", "simple", "heldout",
         f"What was the goodwill, in US dollars, of PFIZER INC on 2026-06-30, {src()}?",
         fact("PFIZER INC", "Goodwill"), "Single Goodwill fact at the 2026-06-30 instant in Pfizer's only filing."),
    task("li_apple_liabilities", "lookup_instant", "simple", "heldout",
         f"What were the total liabilities, in US dollars, of APPLE INC on 2026-06-30, {src()}?",
         fact("APPLE INC", "Liabilities"), "Apple reports Liabilities directly; current and noncurrent are separate tags."),
    task("li_jpm_deposits", "lookup_instant", "simple", "heldout",
         f"What were the total deposits, in US dollars, of JPMORGAN CHASE & CO on 2026-06-30, {src()}?",
         fact("JPMORGAN CHASE & CO", "Deposits"), "One Deposits fact at 2026-06-30; the other date in the filing is 2025-12-31."),
    task("li_apple_current_assets", "lookup_instant", "simple", "heldout",
         f"What were the total current assets, in US dollars, of APPLE INC on 2026-06-30, {src()}?",
         fact("APPLE INC", "AssetsCurrent"), "AssetsCurrent at the 2026-06-30 instant; the comparative is 2025-09-30."),
    task("li_apple_cl_fye", "lookup_instant", "moderate", "heldout",
         f"What were the total current liabilities, in US dollars, of APPLE INC at the end of its previous fiscal year, "
         f"2025-09-30, {src()}?",
         fact("APPLE INC", "LiabilitiesCurrent", 20250930),
         "Apple's fiscal year ends in September; its 10-Q carries the 2025-09-30 comparative."),
    task("li_nvidia_inventory_fye", "lookup_instant", "moderate", "heldout",
         f"What were the inventories, in US dollars, of NVIDIA CORP on 2026-01-31 (the end of its previous fiscal year), "
         f"{src(period=20260731)}?",
         fact("NVIDIA CORP", "InventoryNet", 20260131, period=20260731),
         "NVIDIA's 10-Q has two inventory instants; the question names 2026-01-31."),
    task("li_coke_aoci", "lookup_instant", "moderate", "heldout",
         f"What was the accumulated other comprehensive income (loss), in US dollars, of COCA COLA CO on 2026-06-30, {src()}? "
         "Give a loss as a negative number.",
         fact("COCA COLA CO", "AccumulatedOtherComprehensiveIncomeLossNetOfTax"),
         "One AOCI fact at 2026-06-30; it is a loss, stored negative."),
    task("li_apple_shares_out", "lookup_instant", "moderate", "heldout",
         f"How many shares of common stock did APPLE INC have outstanding on 2026-06-30, {src()}?",
         fact("APPLE INC", "CommonStockSharesOutstanding", uom="shares"),
         "Balance-sheet shares outstanding at the 2026-06-30 instant; weighted-average counts are durations."),
    task("li_walmart_inventory", "lookup_instant", "challenging", "heldout",
         "What were the inventories, in US dollars, of WALMART INC. at the balance sheet date of its 10-Q in the database, "
         "as reported in that 10-Q?",
         fact("WALMART INC.", "InventoryNet", 20260731, period=20260731),
         "Walmart has one filing; its balance sheet date is sub.period (2026-07-31), the other two inventory instants are comparatives."),

    # 2. lookup_duration: three-month versus year-to-date.
    task("ld_nvidia_revenue_3m", "lookup_duration", "simple", "config",
         f"What were the revenues, in US dollars, of NVIDIA CORP for the three months ended 2026-07-31, {src(period=20260731)}?",
         fact("NVIDIA CORP", "Revenues", 20260731, 1, period=20260731), "qtrs = 1 at 2026-07-31; the six-month figure is qtrs = 2."),
    task("ld_apple_ni_3m", "lookup_duration", "simple", "heldout",
         f"What was the net income, in US dollars, of APPLE INC for the three months ended 2026-06-30, {src()}?",
         fact("APPLE INC", "NetIncomeLoss", P, 1), "Apple has no noncontrolling interests (no ProfitLoss fact); qtrs = 1 is the quarter."),
    task("ld_meta_rd_3m", "lookup_duration", "simple", "heldout",
         f"What was the research and development expense, in US dollars, of META PLATFORMS, INC. for the three months ended "
         f"2026-06-30, {src()}?",
         fact("META PLATFORMS, INC.", "ResearchAndDevelopmentExpense", P, 1), "One R&D tag; qtrs = 1 for the three months."),
    task("ld_exxon_tax_3m", "lookup_duration", "simple", "heldout",
         f"What was the income tax expense, in US dollars, of EXXON MOBIL CORP for the three months ended 2026-06-30, {src()}?",
         fact("EXXON MOBIL CORP", "IncomeTaxExpenseBenefit", P, 1), "qtrs = 1; income taxes paid would be a different, cash tag."),
    task("ld_apple_ni_9m", "lookup_duration", "moderate", "heldout",
         f"What was the net income, in US dollars, of APPLE INC for the nine months ended 2026-06-30, {src()}?",
         fact("APPLE INC", "NetIncomeLoss", P, 3), "Apple is in fiscal Q3, so the year-to-date column is nine months (qtrs = 3)."),
    task("ld_nvidia_ni_6m", "lookup_duration", "moderate", "heldout",
         f"What was the net income, in US dollars, of NVIDIA CORP for the six months ended 2026-07-31, {src(period=20260731)}?",
         fact("NVIDIA CORP", "NetIncomeLoss", 20260731, 2, period=20260731), "qtrs = 2 at 2026-07-31; NVIDIA has no ProfitLoss fact."),
    task("ld_merck_eps_6m", "lookup_duration", "moderate", "heldout",
         f"What was the diluted earnings per share, in US dollars per share, of MERCK & CO., INC. for the six months ended "
         f"2026-06-30, {src()}?",
         fact("MERCK & CO., INC.", "EarningsPerShareDiluted", P, 2), "qtrs = 2; the value is negative (a loss per share)."),
    task("ld_meta_tax_6m", "lookup_duration", "moderate", "heldout",
         f"What was the income tax expense (benefit), in US dollars, of META PLATFORMS, INC. for the six months ended 2026-06-30, "
         f"{src()}? Give a benefit as a negative number.",
         fact("META PLATFORMS, INC.", "IncomeTaxExpenseBenefit", P, 2),
         "qtrs = 2 is a net benefit although the three-month figure is an expense."),
    task("ld_disney_revenue_ytd", "lookup_duration", "challenging", "heldout",
         f"What were the revenues, in US dollars, of WALT DISNEY CO for its fiscal year to date through 2026-06-30, {src()}?",
         fact("WALT DISNEY CO", "Revenues", P, 3),
         "Disney's fiscal year starts in October, so fiscal year to date at 2026-06-30 is nine months (qtrs = 3)."),
    task("ld_mckesson_eps_ytd", "lookup_duration", "challenging", "heldout",
         f"What was the diluted earnings per share, in US dollars per share, of MCKESSON CORP for its fiscal year to date "
         f"through 2026-06-30, {src()}?",
         fact("MCKESSON CORP", "EarningsPerShareDiluted", P, 1),
         "McKesson's fiscal year starts in April, so year to date at 2026-06-30 is one quarter (qtrs = 1); no qtrs = 2 fact exists."),

    # 3. period_compare: against the comparative period in the same filing.
    task("pc_amd_inventory_change", "period_compare", "simple", "config",
         f"By how much, in US dollars, did the inventories of ADVANCED MICRO DEVICES INC change from 2025-12-31 to 2026-06-30, "
         f"{src()}? Give an increase as a positive number and a decrease as a negative number.",
         f"SELECT {v('ADVANCED MICRO DEVICES INC', 'InventoryNet')} - {v('ADVANCED MICRO DEVICES INC', 'InventoryNet', 20251231)}",
         "Both instants are in AMD's one 10-Q."),
    task("pc_pfizer_liabilities_change", "period_compare", "simple", "heldout",
         f"By how much, in US dollars, did the total liabilities of PFIZER INC change from 2025-12-31 to 2026-06-30, {src()}? "
         "Give an increase as a positive number and a decrease as a negative number.",
         f"SELECT {v('PFIZER INC', 'Liabilities')} - {v('PFIZER INC', 'Liabilities', 20251231)}", "Both instants are in Pfizer's one 10-Q."),
    task("pc_nvidia_revenue_growth", "period_compare", "moderate", "heldout",
         f"What was the growth rate of NVIDIA CORP's revenues for the three months ended 2026-07-31 over the three months ended "
         f"2025-07-31, {src(period=20260731)}? Compute (current / prior) - 1 and give it as a decimal rounded to 4 decimal places.",
         f"SELECT ROUND({v('NVIDIA CORP', 'Revenues', 20260731, 1, period=20260731)} * 1.0 / "
         f"{v('NVIDIA CORP', 'Revenues', 20250731, 1, period=20260731)} - 1, 4)",
         "Two qtrs = 1 Revenues facts in one filing; the six-month pair gives a different rate.", tolerance=RATIO),
    task("pc_merck_rd_change", "period_compare", "moderate", "heldout",
         f"By how much, in US dollars, did the research and development expense of MERCK & CO., INC. for the six months ended "
         f"2026-06-30 exceed that for the six months ended 2025-06-30, {src()}?",
         f"SELECT {v('MERCK & CO., INC.', 'ResearchAndDevelopmentExpense', P, 2)} - "
         f"{v('MERCK & CO., INC.', 'ResearchAndDevelopmentExpense', 20250630, 2)}",
         "qtrs = 2 at both ddates; the three-month pair differs."),
    task("pc_meta_revenue_growth", "period_compare", "moderate", "heldout",
         f"What was the growth rate of META PLATFORMS, INC.'s revenue for the three months ended 2026-06-30 over the three months "
         f"ended 2025-06-30, {src()}? Compute (current / prior) - 1 and give it as a decimal rounded to 4 decimal places.",
         f"SELECT ROUND({v('META PLATFORMS, INC.', 'RevenueFromContractWithCustomerExcludingAssessedTax', P, 1)} * 1.0 / "
         f"{v('META PLATFORMS, INC.', 'RevenueFromContractWithCustomerExcludingAssessedTax', 20250630, 1)} - 1, 4)",
         "Meta reports revenue under one tag only; qtrs = 1 at both ddates.", tolerance=RATIO),
    task("pc_apple_ni_growth_9m", "period_compare", "moderate", "heldout",
         f"What was the growth rate of APPLE INC's net income for the nine months ended 2026-06-30 over the nine months ended "
         f"2025-06-30, {src()}? Compute (current / prior) - 1 and give it as a decimal rounded to 4 decimal places.",
         f"SELECT ROUND({v('APPLE INC', 'NetIncomeLoss', P, 3)} * 1.0 / {v('APPLE INC', 'NetIncomeLoss', 20250630, 3)} - 1, 4)",
         "qtrs = 3 at both ddates; Apple has no noncontrolling interests.", tolerance=RATIO),
    task("pc_exxon_ocf_change", "period_compare", "moderate", "heldout",
         f"By how much, in US dollars, did EXXON MOBIL CORP's net cash provided by operating activities for the six months ended "
         f"2026-06-30 differ from that for the six months ended 2025-06-30, {src()}? Give current minus prior.",
         f"SELECT {v('EXXON MOBIL CORP', 'NetCashProvidedByUsedInOperatingActivities', P, 2)} - "
         f"{v('EXXON MOBIL CORP', 'NetCashProvidedByUsedInOperatingActivities', 20250630, 2)}",
         "Cash flow is reported year to date only (qtrs = 2), at both ddates."),
    task("pc_disney_revenue_growth_ytd", "period_compare", "challenging", "heldout",
         f"What was the growth rate of WALT DISNEY CO's revenues for its fiscal year to date through 2026-06-30 over the same "
         f"fiscal year-to-date period one year earlier, {src()}? Compute (current / prior) - 1 and give it as a decimal rounded "
         "to 4 decimal places.",
         f"SELECT ROUND({v('WALT DISNEY CO', 'Revenues', P, 3)} * 1.0 / {v('WALT DISNEY CO', 'Revenues', 20250630, 3)} - 1, 4)",
         "Disney is in fiscal Q3, so fiscal year to date is qtrs = 3 at 2026-06-30 and at 2025-06-30.", tolerance=RATIO),
    task("pc_apple_cl_change_fye", "period_compare", "challenging", "heldout",
         f"By how much, in US dollars, did APPLE INC's total current liabilities change from the end of its previous fiscal year "
         f"to 2026-06-30, {src()}? Give an increase as a positive number and a decrease as a negative number.",
         f"SELECT {v('APPLE INC', 'LiabilitiesCurrent')} - {v('APPLE INC', 'LiabilitiesCurrent', 20250930)}",
         "Apple's previous fiscal year ended 2025-09-30, the only other LiabilitiesCurrent instant in the filing."),
    task("pc_walmart_inventory_yoy", "period_compare", "challenging", "heldout",
         "By how much, in US dollars, did WALMART INC.'s inventories at the balance sheet date of its 10-Q in the database "
         "exceed its inventories at the same date one year earlier, as reported in that 10-Q?",
         f"SELECT {v('WALMART INC.', 'InventoryNet', 20260731, period=20260731)} - "
         f"{v('WALMART INC.', 'InventoryNet', 20250731, period=20260731)}",
         "Balance sheet date 2026-07-31; one year earlier is 2025-07-31, not the 2026-01-31 fiscal year end also in the filing."),

    # 4. ratio: formulas stated in business terms, rounded, with an absolute tolerance.
    task("ra_apple_current_ratio", "ratio", "simple", "config",
         f"What was the current ratio (total current assets divided by total current liabilities) of APPLE INC on 2026-06-30, "
         f"{src()}? Give it as a decimal rounded to 4 decimal places.",
         ratio(v("APPLE INC", "AssetsCurrent"), v("APPLE INC", "LiabilitiesCurrent")),
         "Both instants at 2026-06-30 in one filing.", tolerance=RATIO),
    task("ra_pfizer_liabilities_to_assets", "ratio", "simple", "heldout",
         f"What was the ratio of total liabilities to total assets of PFIZER INC on 2026-06-30, {src()}? Give it as a decimal "
         "rounded to 4 decimal places.",
         ratio(v("PFIZER INC", "Liabilities"), v("PFIZER INC", "Assets")), "Both instants at 2026-06-30.", tolerance=RATIO),
    task("ra_nvidia_gross_margin", "ratio", "moderate", "heldout",
         f"What was the gross margin (gross profit divided by revenues) of NVIDIA CORP for the three months ended 2026-07-31, "
         f"{src(period=20260731)}? Give it as a decimal rounded to 4 decimal places.",
         ratio(v("NVIDIA CORP", "GrossProfit", 20260731, 1, period=20260731),
               v("NVIDIA CORP", "Revenues", 20260731, 1, period=20260731)),
         "GrossProfit and Revenues both at qtrs = 1.", tolerance=RATIO),
    task("ra_nvidia_operating_margin", "ratio", "moderate", "heldout",
         f"What was the operating margin (operating income divided by revenues) of NVIDIA CORP for the three months ended "
         f"2026-07-31, {src(period=20260731)}? Give it as a decimal rounded to 4 decimal places.",
         ratio(v("NVIDIA CORP", "OperatingIncomeLoss", 20260731, 1, period=20260731),
               v("NVIDIA CORP", "Revenues", 20260731, 1, period=20260731)),
         "OperatingIncomeLoss and Revenues at qtrs = 1.", tolerance=RATIO),
    task("ra_nvidia_tax_rate", "ratio", "moderate", "heldout",
         f"What was the effective income tax rate (income tax expense divided by income before income taxes) of NVIDIA CORP for "
         f"the three months ended 2026-07-31, {src(period=20260731)}? Give it as a decimal rounded to 4 decimal places.",
         ratio(v("NVIDIA CORP", "IncomeTaxExpenseBenefit", 20260731, 1, period=20260731),
               v("NVIDIA CORP", "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                 20260731, 1, period=20260731)),
         "NVIDIA has one pre-tax income tag; qtrs = 1.", tolerance=RATIO),
    task("ra_apple_debt_to_equity", "ratio", "moderate", "heldout",
         f"What was the ratio of total liabilities to total shareholders' equity of APPLE INC on 2026-06-30, {src()}? Give it as "
         "a decimal rounded to 4 decimal places.",
         ratio(v("APPLE INC", "Liabilities"), v("APPLE INC", "StockholdersEquity")),
         "Apple's equity statement has many StockholdersEquity instants; the question pins 2026-06-30.", tolerance=RATIO),
    task("ra_apple_gross_margin_9m", "ratio", "moderate", "heldout",
         f"What was the gross margin (gross profit divided by net sales) of APPLE INC for the nine months ended 2026-06-30, "
         f"{src()}? Give it as a decimal rounded to 4 decimal places.",
         ratio(v("APPLE INC", "GrossProfit", P, 3), v("APPLE INC", "RevenueFromContractWithCustomerExcludingAssessedTax", P, 3)),
         "Apple reports net sales under one revenue tag; both at qtrs = 3.", tolerance=RATIO),
    task("ra_amd_inventory_turnover", "ratio", "challenging", "heldout",
         f"What was the inventory turnover of ADVANCED MICRO DEVICES INC for the six months ended 2026-06-30, defined as total "
         f"cost of goods and services sold for the six months divided by the average of inventories at 2025-12-31 and "
         f"2026-06-30, {src()}? Give it as a decimal rounded to 4 decimal places.",
         ratio(v("ADVANCED MICRO DEVICES INC", "CostOfGoodsAndServicesSold", P, 2),
               f"(({v('ADVANCED MICRO DEVICES INC', 'InventoryNet')} + "
               f"{v('ADVANCED MICRO DEVICES INC', 'InventoryNet', 20251231)}) / 2.0)"),
         "Total cost of goods and services sold (not the subtotal excluding depreciation), qtrs = 2, over two inventory instants.",
         tolerance=RATIO),
    task("ra_disney_net_margin_ytd", "ratio", "challenging", "heldout",
         f"What was the net margin of WALT DISNEY CO for its fiscal year to date through 2026-06-30, defined as net income "
         f"attributable to Disney (excluding noncontrolling interests) divided by revenues, {src()}? Give it as a decimal "
         "rounded to 4 decimal places.",
         ratio(v("WALT DISNEY CO", "NetIncomeLoss", P, 3), v("WALT DISNEY CO", "Revenues", P, 3)),
         "Fiscal year to date is qtrs = 3; the parent's share is NetIncomeLoss, not ProfitLoss.", tolerance=RATIO),
    task("ra_goldman_book_value_per_share", "ratio", "challenging", "heldout",
         "What was the book value per common share of GOLDMAN SACHS GROUP INC on 2026-06-30, defined as total shareholders' "
         "equity minus preferred stock, divided by the number of common shares outstanding (not issued), "
         f"{src()}? Give it in US dollars per share rounded to 2 decimal places.",
         ratio(f"({v('GOLDMAN SACHS GROUP INC', 'StockholdersEquity')} - {v('GOLDMAN SACHS GROUP INC', 'PreferredStockValue')})",
               v("GOLDMAN SACHS GROUP INC", "CommonStockSharesOutstanding", uom="shares"), 2),
         "Outstanding (291.4M) and issued (936.3M) shares are both reported; the question names outstanding.",
         tolerance={"abs": 0.01}),

    # 5. aggregate_rank: ordering across filers.
    task("ar_goodwill_top3", "aggregate_rank", "simple", "config",
         f"Among {Q10}, which three filers reported the largest goodwill on 2026-06-30? Answer with their company names exactly as stored in sub.name.",
         f"SELECT s.name {TOP}n.tag = 'Goodwill' AND n.qtrs = 0 ORDER BY n.value DESC LIMIT 3",
         "Third (83.2B) and fourth (74.7B) are far apart.", fmt="names"),
    task("ar_net_income_top1", "aggregate_rank", "simple", "heldout",
         f"Among {Q10}, which filer reported the largest net income attributable to the company (excluding noncontrolling "
         "interests) for the three months ended 2026-06-30? Answer with its company name exactly as stored in sub.name.",
         f"{NI}SELECT name FROM ni WHERE v IS NOT NULL ORDER BY v DESC LIMIT 1",
         NI_NOTE + "First (112.2B) leads second (62.6B) by a wide margin.", fmt="names"),
    task("ar_liabilities_top5", "aggregate_rank", "simple", "heldout",
         f"Among {Q10}, which five filers reported the largest total liabilities on 2026-06-30? Answer with their company names exactly as stored in sub.name.",
         f"SELECT s.name {TOP}n.tag = 'Liabilities' AND n.qtrs = 0 ORDER BY n.value DESC LIMIT 5",
         "Fifth (2.68T) and sixth (2.10T) are far apart.", fmt="names"),
    task("ar_coke_assets_rank", "aggregate_rank", "moderate", "heldout",
         f"Among {Q10} that reported total assets on 2026-06-30, what was the rank of COCA COLA CO by total assets on that date, "
         "where rank 1 is the largest?",
         f"SELECT 1 + COUNT(*) {TOP}n.tag = 'Assets' AND n.qtrs = 0 AND n.value > {v('COCA COLA CO', 'Assets')}",
         "Rank = 1 + number of filers with strictly larger assets; no filer ties Coca-Cola. Review fix: AEP TEXAS INC.'s "
         "copy of American Electric Power's filing is no longer in the subset, so AEP counts once (118 became 117)."),
    task("ar_net_loss_bottom3", "aggregate_rank", "moderate", "heldout",
         f"Among {Q10}, which three filers reported the largest net losses attributable to the company (the most negative net "
         "income, excluding noncontrolling interests) for the three months ended 2026-06-30? Answer with their company names exactly as stored in sub.name.",
         f"{NI}SELECT name FROM ni WHERE v IS NOT NULL ORDER BY v ASC LIMIT 3",
         NI_NOTE + "Review fix: NetIncomeLoss alone missed SPDR GOLD TRUST (-19.4B), the largest loss. Third (-10.5B) "
         "and fourth (-8.2B) are far apart.", fmt="names"),
    task("ar_10k_largest_assets", "aggregate_rank", "moderate", "heldout",
         "Among the annual reports in the database (form 10-K, not 10-K/A), which filer reported the largest total assets at "
         "the balance sheet date of its report? Answer with its company name exactly as stored in sub.name.",
         "SELECT s.name FROM sub s JOIN num n ON n.adsh = s.adsh WHERE s.form = '10-K' AND n.tag = 'Assets' AND n.qtrs = 0 "
         "AND n.ddate = s.period AND n.uom = 'USD' ORDER BY n.value DESC LIMIT 1",
         "Seven 10-K filers; the first (758B) is far ahead of the second (130B).", fmt="names"),
    task("ar_trillion_assets_count", "aggregate_rank", "moderate", "heldout",
         f"How many {Q10} reported total assets above 1 trillion US dollars on 2026-06-30?",
         f"SELECT COUNT(*) {TOP}n.tag = 'Assets' AND n.qtrs = 0 AND n.value > 1e12",
         "No filer sits near the 1T threshold."),
    task("ar_eps_top5", "aggregate_rank", "challenging", "heldout",
         f"Among {Q10} that reported a diluted earnings per share figure for the three months ended 2026-06-30, which five "
         "reported the highest diluted earnings per share? Answer with their company names exactly as stored in sub.name.",
         f"SELECT s.name {TOP}n.tag = 'EarningsPerShareDiluted' AND n.qtrs = 1 ORDER BY n.value DESC LIMIT 5",
         "Fifth 17.83 versus sixth 17.73: distinct values, no tie.", fmt="names"),
    task("ar_offcycle_assets_top3", "aggregate_rank", "challenging", "heldout",
         "Among original 10-Q filings (form 10-Q) whose balance sheet date is not 2026-06-30, which three filers reported the "
         "largest total assets at their own balance sheet date? Answer with their company names exactly as stored in sub.name.",
         "SELECT s.name FROM sub s JOIN num n ON n.adsh = s.adsh WHERE s.form = '10-Q' AND s.period <> 20260630 "
         "AND n.tag = 'Assets' AND n.qtrs = 0 AND n.ddate = s.period AND n.uom = 'USD' ORDER BY n.value DESC LIMIT 3",
         "Balance sheet date = sub.period; third (293.9B) and fourth (188.1B) are far apart.", fmt="names"),
    task("ar_assets_increase_top3", "aggregate_rank", "challenging", "heldout",
         f"Among {Q10} that reported total assets at both 2025-12-31 and 2026-06-30, which three had the largest increase in "
         "total assets in US dollars (the absolute change, not the percentage change) between those dates? Answer with their company names exactly as stored in sub.name.",
         "SELECT s.name FROM sub s JOIN num a ON a.adsh = s.adsh AND a.tag = 'Assets' AND a.ddate = 20260630 AND a.qtrs = 0 "
         "JOIN num b ON b.adsh = s.adsh AND b.tag = 'Assets' AND b.ddate = 20251231 AND b.qtrs = 0 "
         "WHERE s.form = '10-Q' AND s.period = 20260630 ORDER BY a.value - b.value DESC LIMIT 3",
         "Third (318.4B) and fourth (277.6B) increases are far apart. Review fix: the question now says absolute dollars; "
         "the percentage change ranks other filers.", fmt="names"),

    # 6. aggregate_sector: SIC-filtered counts, sums, averages.
    task("as_electric_count", "aggregate_sector", "simple", "config",
         f"How many {Q10} were made by filers with SIC code 4911 (electric services)?",
         "SELECT COUNT(*) FROM sub WHERE sic = 4911 AND form = '10-Q' AND period = 20260630",
         "A plain count over sub; AEP TEXAS INC.'s copy of American Electric Power's filing is not in the subset (15 became 14)."),
    task("as_reit_filers", "aggregate_sector", "simple", "heldout",
         "How many distinct filers with SIC code 6798 (real estate investment trusts) have at least one filing of any form in "
         "the database?",
         "SELECT COUNT(DISTINCT cik) FROM sub WHERE sic = 6798", "Distinct cik; one REIT appears only through a 10-Q/A."),
    task("as_state_bank_deposits", "aggregate_sector", "simple", "heldout",
         f"What were the combined total deposits, in US dollars, on 2026-06-30 of all filers with SIC code 6022 (state commercial "
         f"banks) among {Q10}?",
         f"SELECT SUM(n.value) {TOP}s.sic = 6022 AND n.tag = 'Deposits' AND n.qtrs = 0",
         "All 16 such filers report Deposits at 2026-06-30."),
    task("as_pharma_avg_assets", "aggregate_sector", "moderate", "heldout",
         f"What was the average total assets, in US dollars, on 2026-06-30 of filers with SIC code 2834 (pharmaceutical "
         f"preparations) among {Q10}?",
         f"SELECT AVG(n.value) {TOP}s.sic = 2834 AND n.tag = 'Assets' AND n.qtrs = 0",
         "Eight filings; OPKO's 10-Q/A for the same period is excluded by the form rule, so it is not double counted."),
    task("as_rail_net_income", "aggregate_sector", "moderate", "heldout",
         f"What was the combined net income attributable to the companies (excluding noncontrolling interests), in US dollars, "
         f"for the three months ended 2026-06-30 of all filers with SIC code 4011 (railroads, line-haul operating) among {Q10}?",
         f"{NI}SELECT SUM(v) FROM ni WHERE sic = 4011",
         NI_NOTE + "All four railroads report NetIncomeLoss at qtrs = 1."),
    task("as_oil_gas_names", "aggregate_sector", "moderate", "heldout",
         f"Which filers with SIC code 1311 (crude petroleum and natural gas) made {Q10}? Answer with their company names exactly as stored in sub.name.",
         "SELECT name FROM sub WHERE sic = 1311 AND form = '10-Q' AND period = 20260630",
         "BKV's 10-K/A is excluded by the form rule; its 10-Q is included.", fmt="names"),
    task("as_big_national_banks", "aggregate_sector", "moderate", "heldout",
         f"How many filers with SIC code 6021 (national commercial banks) among {Q10} reported total deposits above 500 billion "
         "US dollars on 2026-06-30?",
         f"SELECT COUNT(*) {TOP}s.sic = 6021 AND n.tag = 'Deposits' AND n.qtrs = 0 AND n.value > 5e11",
         "Fifth 532B, sixth 484B; the one 6021 filer without a Deposits fact is far smaller."),
    task("as_pharma_goodwill", "aggregate_sector", "moderate", "heldout",
         f"What was the combined goodwill, in US dollars, on 2026-06-30 of filers with SIC code 2834 (pharmaceutical "
         f"preparations) among {Q10}?",
         f"SELECT SUM(n.value) {TOP}s.sic = 2834 AND n.tag = 'Goodwill' AND n.qtrs = 0",
         "All eight filings report Goodwill; the form rule excludes OPKO's duplicate 10-Q/A."),
    task("as_most_common_sic", "aggregate_sector", "challenging", "heldout",
         f"Which SIC code has the most filers among {Q10}? Answer with the SIC code as a number.",
         "SELECT sic FROM sub WHERE form = '10-Q' AND period = 20260630 GROUP BY sic ORDER BY COUNT(*) DESC LIMIT 1",
         "6021 has 23 filings; the runners-up have 16."),
    task("as_refiner_avg_current_ratio", "aggregate_sector", "challenging", "heldout",
         f"What was the average current ratio (total current assets divided by total current liabilities, computed per filer "
         f"and then averaged) on 2026-06-30 of filers with SIC code 2911 (petroleum refining) among {Q10}? Give it as a decimal "
         "rounded to 4 decimal places.",
         "SELECT ROUND(AVG(a.value * 1.0 / l.value), 4) FROM sub s JOIN num a ON a.adsh = s.adsh AND a.tag = 'AssetsCurrent' "
         "AND a.ddate = 20260630 AND a.qtrs = 0 JOIN num l ON l.adsh = s.adsh AND l.tag = 'LiabilitiesCurrent' "
         "AND l.ddate = 20260630 AND l.qtrs = 0 WHERE s.sic = 2911 AND s.form = '10-Q' AND s.period = 20260630",
         "All six refiners report both current totals; the question fixes the mean of ratios, not the ratio of sums.",
         tolerance=RATIO),

    # 7. concept_mapping: near-synonym tags both present for the filer; only the definition picks one.
    task("cm_ice_cash", "concept_mapping", "moderate", "config",
         f"What were the cash and cash equivalents, in US dollars, of INTERCONTINENTAL EXCHANGE, INC. on 2026-06-30, {src()}?",
         fact("INTERCONTINENTAL EXCHANGE, INC.", "CashAndCashEquivalentsAtCarryingValue"),
         "ICE also reports cash including restricted cash and margin deposits (116.6B); cash and cash equivalents alone is 1.07B."),
    task("cm_gm_contract_revenue", "concept_mapping", "moderate", "heldout",
         f"What was the revenue from contracts with customers, in US dollars, of GENERAL MOTORS CO for the three months ended "
         f"2026-06-30, {src()}?",
         fact("GENERAL MOTORS CO", "RevenueFromContractWithCustomerExcludingAssessedTax", P, 1),
         "GM also reports total Revenues and revenue not from contracts with customers; one tag is customer-contract revenue."),
    task("cm_centene_total_revenue", "concept_mapping", "moderate", "heldout",
         f"What were the total revenues (all revenue lines, including premium tax revenue), in US dollars, of CENTENE CORP for "
         f"the three months ended 2026-06-30, {src()}?",
         fact("CENTENE CORP", "Revenues", P, 1),
         "Centene reports two revenue facts: RevenueFromContractWithCustomerExcludingAssessedTax (44.4B) and total Revenues "
         "(53.6B). Review fix: the earlier note said premiums sit outside the 44.4B; per Centene's published statement that "
         "line covers premium and service revenue (not verifiable in the database), so the question now asks for the total "
         "of all revenue lines, which only Revenues is."),
    task("cm_pfizer_taxes_paid", "concept_mapping", "moderate", "heldout",
         f"How much cash did PFIZER INC pay for income taxes, net of refunds, in US dollars, during the six months ended "
         f"2026-06-30, {src()}?",
         fact("PFIZER INC", "IncomeTaxesPaidNet", P, 2), "Income tax expense (54M) is the accrual concept; cash paid is 3.54B."),
    task("cm_comcast_depreciation", "concept_mapping", "moderate", "heldout",
         f"What was the depreciation expense, in US dollars, of COMCAST CORP for the six months ended 2026-06-30, excluding "
         f"amortization, {src()}?",
         fact("COMCAST CORP", "Depreciation", P, 2),
         "Comcast reports Depreciation, AmortizationOfIntangibleAssets and their total DepreciationDepletionAndAmortization."),
    task("cm_goldman_shares_outstanding", "concept_mapping", "moderate", "heldout",
         f"How many shares of common stock did GOLDMAN SACHS GROUP INC have outstanding on 2026-06-30, {src()}?",
         fact("GOLDMAN SACHS GROUP INC", "CommonStockSharesOutstanding", uom="shares"),
         "Goldman reports issued (936.3M, incl. treasury) and outstanding (291.4M) shares."),
    task("cm_fannie_ni_common", "concept_mapping", "challenging", "heldout",
         f"What was the net income available to common stockholders, in US dollars, of FEDERAL NATIONAL MORTGAGE ASSOCIATION "
         f"FANNIE MAE for the three months ended 2026-06-30, {src()}?",
         fact("FEDERAL NATIONAL MORTGAGE ASSOCIATION FANNIE MAE", "NetIncomeLossAvailableToCommonStockholdersBasic", P, 1),
         "Net income is 3.98B; after senior preferred dividends 152M is available to common (basic and diluted tags agree)."),
    task("cm_exxon_interest_paid_net", "concept_mapping", "challenging", "heldout",
         f"How much cash did EXXON MOBIL CORP pay for interest during the six months ended 2026-06-30, in US dollars, net of the "
         f"interest it capitalized, {src()}?",
         fact("EXXON MOBIL CORP", "InterestPaidNet", P, 2),
         "Exxon reports interest expense (522M), gross interest paid (910M), capitalized (508M) and net paid (402M)."),
    task("cm_jpm_net_revenue", "concept_mapping", "challenging", "heldout",
         f"What was the total net revenue (total revenue net of interest expense), in US dollars, of JPMORGAN CHASE & CO for the "
         f"three months ended 2026-06-30, {src()}?",
         fact("JPMORGAN CHASE & CO", "RevenuesNetOfInterestExpense", P, 1),
         "JPMorgan reports several revenue components; RevenuesNetOfInterestExpense is the one total net revenue fact."),
    task("cm_boeing_loss_common", "concept_mapping", "challenging", "heldout",
         f"What was the net loss available to common shareholders, in US dollars, of BOEING CO for the three months ended "
         f"2026-06-30, after preferred stock dividends, {src()}? Give a loss as a negative number.",
         fact("BOEING CO", "NetIncomeLossAvailableToCommonStockholdersBasic", P, 1),
         "Boeing reports ProfitLoss (-428M), NetIncomeLoss (-444M) and the common shareholders' figure (-530M)."),

    # 8. concept_composition: two or more definitions combined.
    task("cc_exxon_fcf", "concept_composition", "moderate", "config",
         f"What was the free cash flow, in US dollars, of EXXON MOBIL CORP for the six months ended 2026-06-30, defined as net "
         f"cash provided by operating activities minus capital expenditures (payments to acquire property, plant and "
         f"equipment), {src()}?",
         f"SELECT {v('EXXON MOBIL CORP', 'NetCashProvidedByUsedInOperatingActivities', P, 2)} - "
         f"{v('EXXON MOBIL CORP', 'PaymentsToAcquirePropertyPlantAndEquipment', P, 2)}",
         "Both cash-flow facts are year to date (qtrs = 2)."),
    task("cc_apple_working_capital", "concept_composition", "moderate", "heldout",
         f"What was the working capital, in US dollars, of APPLE INC on 2026-06-30, defined as total current assets minus total "
         f"current liabilities, {src()}?",
         f"SELECT {v('APPLE INC', 'AssetsCurrent')} - {v('APPLE INC', 'LiabilitiesCurrent')}", "Both instants at 2026-06-30."),
    task("cc_pfizer_tangible_equity", "concept_composition", "moderate", "heldout",
         f"What was the tangible equity, in US dollars, of PFIZER INC on 2026-06-30, defined as shareholders' equity attributable "
         f"to Pfizer (excluding noncontrolling interests) minus goodwill minus intangible assets other than goodwill, {src()}? "
         "Give a negative result as a negative number.",
         f"SELECT {v('PFIZER INC', 'StockholdersEquity')} - {v('PFIZER INC', 'Goodwill')} - "
         f"{v('PFIZER INC', 'IntangibleAssetsNetExcludingGoodwill')}",
         "Parent equity, not equity including the 302M noncontrolling interest."),
    task("cc_apple_shareholder_returns", "concept_composition", "moderate", "heldout",
         f"How much cash, in US dollars, did APPLE INC return to shareholders during the nine months ended 2026-06-30, defined as "
         f"payments for repurchases of common stock plus dividends paid, {src()}?",
         f"SELECT {v('APPLE INC', 'PaymentsForRepurchaseOfCommonStock', P, 3)} + {v('APPLE INC', 'PaymentsOfDividends', P, 3)}",
         "Apple reports dividends paid under one tag; both at qtrs = 3."),
    task("cc_comcast_ebitda", "concept_composition", "moderate", "heldout",
         f"What was the EBITDA, in US dollars, of COMCAST CORP for the six months ended 2026-06-30, defined as operating income "
         f"plus total depreciation and amortization, {src()}?",
         f"SELECT {v('COMCAST CORP', 'OperatingIncomeLoss', P, 2)} + "
         f"{v('COMCAST CORP', 'DepreciationDepletionAndAmortization', P, 2)}",
         "Total D&A (7.553B) equals depreciation plus intangible amortization, so either route agrees."),
    task("cc_exxon_total_debt", "concept_composition", "challenging", "heldout",
         f"What was the total debt, in US dollars, of EXXON MOBIL CORP on 2026-06-30, defined as debt due within one year plus "
         f"noncurrent long-term debt including finance lease obligations, {src()}?",
         f"SELECT {v('EXXON MOBIL CORP', 'DebtCurrent')} + {v('EXXON MOBIL CORP', 'LongTermDebtAndCapitalLeaseObligations')}",
         "Exxon reports exactly one current-debt and one noncurrent-debt total at 2026-06-30."),
    task("cc_verizon_net_debt", "concept_composition", "challenging", "heldout",
         f"What was the net debt, in US dollars, of VERIZON COMMUNICATIONS INC on 2026-06-30, defined as debt maturing within one "
         f"year plus noncurrent long-term debt including finance lease obligations, minus cash and cash equivalents, {src()}?",
         f"SELECT {v('VERIZON COMMUNICATIONS INC', 'LongTermDebtCurrent')} + "
         f"{v('VERIZON COMMUNICATIONS INC', 'LongTermDebtAndCapitalLeaseObligations')} - "
         f"{v('VERIZON COMMUNICATIONS INC', 'CashAndCashEquivalentsAtCarryingValue')}",
         "Verizon's only current-debt fact is LongTermDebtCurrent; no short-term borrowings tag is reported."),
    task("cc_apple_fcf_9m", "concept_composition", "challenging", "heldout",
         f"What was the free cash flow, in US dollars, of APPLE INC for its fiscal year to date through 2026-06-30, defined as "
         f"net cash provided by operating activities minus payments to acquire property, plant and equipment, {src()}?",
         f"SELECT {v('APPLE INC', 'NetCashProvidedByUsedInOperatingActivities', P, 3)} - "
         f"{v('APPLE INC', 'PaymentsToAcquirePropertyPlantAndEquipment', P, 3)}",
         "Apple is in fiscal Q3: year to date is qtrs = 3, the only cash-flow duration in the filing."),
    task("cc_exxon_fcf_after_dividends", "concept_composition", "challenging", "heldout",
         f"What was the free cash flow after dividends, in US dollars, of EXXON MOBIL CORP for the six months ended 2026-06-30, "
         f"defined as net cash provided by operating activities minus payments to acquire property, plant and equipment minus "
         f"all cash dividends paid (to common shareholders and to noncontrolling interests), {src()}?",
         f"SELECT {v('EXXON MOBIL CORP', 'NetCashProvidedByUsedInOperatingActivities', P, 2)} - "
         f"{v('EXXON MOBIL CORP', 'PaymentsToAcquirePropertyPlantAndEquipment', P, 2)} - "
         f"{v('EXXON MOBIL CORP', 'PaymentsOfDividendsCommonStock', P, 2)} - "
         f"{v('EXXON MOBIL CORP', 'PaymentsOfDividendsMinorityInterest', P, 2)}",
         "Exxon reports common and noncontrolling dividend payments separately; the question includes both."),
    task("cc_kkr_nci_comprehensive", "concept_composition", "challenging", "heldout",
         f"What was the comprehensive income attributable to noncontrolling interests, in US dollars, of KKR & CO. INC. for the "
         f"three months ended 2026-06-30, computed as total comprehensive income including noncontrolling interests minus "
         f"comprehensive income attributable to KKR, {src()}?",
         f"SELECT {v('KKR & CO. INC.', 'ComprehensiveIncomeNetOfTaxIncludingPortionAttributableToNoncontrollingInterest', P, 1)} - "
         f"{v('KKR & CO. INC.', 'ComprehensiveIncomeNetOfTax', P, 1)}",
         "KKR reports no direct noncontrolling comprehensive income fact for the quarter, so the difference is the only route."),

    # 9. disambiguation: competing facts; the question states the rule that picks one.
    task("di_kkr_ni_parent", "disambiguation", "moderate", "config",
         f"What was the net income attributable to KKR & CO. INC. (excluding noncontrolling interests), before preferred stock "
         f"dividends, in US dollars, for the three months ended 2026-06-30, {src()}?",
         fact("KKR & CO. INC.", "NetIncomeLoss", P, 1),
         "ProfitLoss (1.13B) includes redeemable and nonredeemable noncontrolling interests; the parent's share is 700M. "
         "Review fix: 'before preferred stock dividends' rules out the 660M available to common stockholders."),
    task("di_blackstone_total_equity", "disambiguation", "moderate", "heldout",
         f"What was the total equity of BLACKSTONE INC., in US dollars, on 2026-06-30 as shown on the balance sheet's total "
         f"equity line, which includes non-redeemable noncontrolling interests and excludes redeemable noncontrolling "
         f"interests, {src()}?",
         fact("BLACKSTONE INC.", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
         "Parent equity is 9.0B; the total equity line is 21.1B. Review fix: the question now excludes the 1.37B of "
         "redeemable noncontrolling interests reported outside equity (21.1B + 1.37B = 22.4B is not the answer)."),
    task("di_kkr_ci_parent", "disambiguation", "moderate", "heldout",
         f"What was the comprehensive income attributable to KKR & CO. INC. (excluding noncontrolling interests), in US dollars, "
         f"for the three months ended 2026-06-30, {src()}?",
         fact("KKR & CO. INC.", "ComprehensiveIncomeNetOfTax", P, 1),
         "Including noncontrolling interests the figure is 1.28B; the parent's is 855M."),
    task("di_opko_assets_q1_amended", "disambiguation", "moderate", "heldout",
         "What were the total assets, in US dollars, of OPKO HEALTH, INC. on 2026-03-31, as reported in its amended 10-Q "
         "(form 10-Q/A) for the period ended 2026-03-31?",
         fact("OPKO HEALTH, INC.", "Assets", 20260331, form="10-Q/A", period=20260331),
         "OPKO has three filings; only the Q1 amendment carries a 2026-03-31 balance sheet."),
    task("di_bkv_assets_2024", "disambiguation", "moderate", "heldout",
         "What were the total assets, in US dollars, of BKV CORP on 2024-12-31, as reported in its amended annual report "
         "(form 10-K/A) for the period ended 2025-12-31?",
         fact("BKV CORP", "Assets", 20241231, form="10-K/A", period=20251231),
         "BKV's 10-Q and 10-K/A both report 2025-12-31; only the 10-K/A has the 2024-12-31 comparative."),
    task("di_sharonai_eps_latest", "disambiguation", "challenging", "heldout",
         "What was the diluted earnings per share, in US dollars per share, of SHARONAI HOLDINGS INC. for the three months ended "
         "2025-06-30? Two filings in the database report it; use the most recently filed one.",
         fact("SHARONAI HOLDINGS INC.", "EarningsPerShareDiluted", 20250630, 1, form="10-Q/A"),
         "The 10-Q (filed 2026-08-06) says -2.41; the 10-Q/A (filed 2026-08-11) restates it to -0.27."),
    task("di_sharonai_eps_original", "disambiguation", "challenging", "heldout",
         "What was the diluted earnings per share, in US dollars per share, of SHARONAI HOLDINGS INC. for the three months ended "
         "2025-06-30, as reported in its original 10-Q (form 10-Q, not 10-Q/A) for the period ended 2026-06-30?",
         fact("SHARONAI HOLDINGS INC.", "EarningsPerShareDiluted", 20250630, 1),
         "The original 10-Q value -2.41, superseded by the 10-Q/A's -0.27. Review fix: 'as originally reported' could mean "
         "the 2025 10-Q, which is not in the database; the question now names the filing."),
    task("di_salesforce_ni_fq2", "disambiguation", "challenging", "heldout",
         "What was the net income, in US dollars, of SALESFORCE, INC. for the second quarter of its fiscal year 2027, as "
         "reported in its 10-Q for that quarter?",
         fact("SALESFORCE, INC.", "NetIncomeLoss", 20260731, 1, period=20260731),
         "Replaces di_bloom_revenue_latest, whose two filings agreed. The filing has fy = 2027, fp = Q2, period 2026-07-31; "
         "the fiscal quarter is qtrs = 1 (3.53B). Competing facts: six months (5.63B), fiscal Q1 2027 ended 2026-04-30 "
         "(2.11B), fiscal Q2 2026 ended 2025-07-31 (1.89B)."),
    task("di_broadcom_revenue_fytd", "disambiguation", "challenging", "heldout",
         "What was the revenue, in US dollars, of BROADCOM INC. for the first three quarters of its fiscal year 2026 combined, "
         "as reported in its 10-Q for its fiscal third quarter of 2026?",
         fact("BROADCOM INC.", "RevenueFromContractWithCustomerExcludingAssessedTax", 20260731, 3, period=20260731),
         "Replaces di_opko_net_loss_6m_latest, whose two filings agreed. The filing has fy = 2026, fp = Q3, period "
         "2026-07-31; three fiscal quarters is qtrs = 3 (71.1B). Competing facts: the third quarter alone (29.6B) and the "
         "prior fiscal year's nine months (45.9B). Broadcom reports one revenue tag."),
    task("di_disney_ni_total_ytd", "disambiguation", "challenging", "heldout",
         f"What was the net income of WALT DISNEY CO, in US dollars, for its fiscal year to date through 2026-06-30, including "
         f"the share attributable to noncontrolling interests, {src()}?",
         fact("WALT DISNEY CO", "ProfitLoss", P, 3),
         "Fiscal year to date is qtrs = 3; including noncontrolling interests is ProfitLoss (7.79B), not NetIncomeLoss (7.29B)."),

    # 10. unanswerable: the database does not contain the fact; the reference is null.
    absent("un_amazon_dividends", "simple", "config",
           f"How much cash, in US dollars, did AMAZON COM INC pay in dividends during the six months ended 2026-06-30, {src()}?",
           fact("AMAZON COM INC", "PaymentsOfDividends", P, 2),
           "Amazon pays no dividends: its filing has no dividend tag of any kind, dimensional or custom included. "
           "Replaces un_starbucks_assets, which asked about a filer outside the subset."),
    absent("un_netflix_goodwill", "simple", "heldout",
           f"What was the goodwill, in US dollars, of NETFLIX INC on 2026-06-30, {src()}?",
           fact("NETFLIX INC", "Goodwill"),
           "Netflix's filing has no goodwill or intangible-asset tag of any kind. Replaces un_chipotle_revenue (filer outside "
           "the subset)."),
    absent("un_walmart_rd", "simple", "heldout",
           f"What was the research and development expense, in US dollars, of WALMART INC. for the three months ended "
           f"2026-07-31, {src(period=20260731)}?",
           fact("WALMART INC.", "ResearchAndDevelopmentExpense", 20260731, 1, period=20260731),
           "Walmart's filing has no research or development tag of any kind. Replaces un_nike_assets (filer outside the "
           "subset)."),
    absent("un_progressive_inventory", "simple", "heldout",
           f"What were the inventories, in US dollars, of PROGRESSIVE CORP/OH/ on 2026-06-30, {src()}?",
           fact("PROGRESSIVE CORP/OH/", "InventoryNet"),
           "Progressive, an insurer, has no inventory tag of any kind. Replaces un_costco_net_income (filer outside the "
           "subset)."),
    absent("un_jpm_gross_profit", "moderate", "heldout",
           f"What was the gross profit, in US dollars, of JPMORGAN CHASE & CO for the three months ended 2026-06-30, {src()}?",
           fact("JPMORGAN CHASE & CO", "GrossProfit", P, 1), "Banks do not report gross profit; JPMorgan has no GrossProfit fact."),
    absent("un_wells_inventory", "moderate", "heldout",
           f"What were the inventories, in US dollars, of WELLS FARGO & COMPANY/MN on 2026-06-30, {src()}?",
           fact("WELLS FARGO & COMPANY/MN", "InventoryNet"), "Wells Fargo reports no inventory tag of any kind."),
    absent("un_duke_rd", "moderate", "heldout",
           f"What was the research and development expense, in US dollars, of DUKE ENERGY CORP for the three months ended "
           f"2026-06-30, {src()}?",
           fact("DUKE ENERGY CORP", "ResearchAndDevelopmentExpense", P, 1), "Duke reports no research and development tag of any kind."),
    absent("un_exxon_ocf_3m", "challenging", "heldout",
           f"What was the net cash provided by operating activities, in US dollars, of EXXON MOBIL CORP for the three months "
           f"ended 2026-06-30, {src()}?",
           fact("EXXON MOBIL CORP", "NetCashProvidedByUsedInOperatingActivities", P, 1),
           "Exxon's cash flow statement is year to date only (qtrs = 2) and its Q1 10-Q is not in the database."),
    absent("un_apple_ni_6m", "challenging", "heldout",
           f"What was the net income, in US dollars, of APPLE INC for the six months ended 2026-06-30, {src()}?",
           fact("APPLE INC", "NetIncomeLoss", P, 2), "Apple is in fiscal Q3 and reports three- and nine-month figures only."),
    absent("un_amd_assets_2024", "challenging", "heldout",
           f"What were the total assets, in US dollars, of ADVANCED MICRO DEVICES INC on 2024-12-31, {src()}?",
           fact("ADVANCED MICRO DEVICES INC", "Assets", 20241231),
           "AMD's 10-Q has balance sheets at 2026-06-30 and 2025-12-31 only."),
]


def limitations(quarter: str) -> list[str]:
    return [
        f"{len(TASKS)} tasks in 10 scenario groups of about 10 tasks; group-level uncertainty rests on 10 clusters.",
        f"Single quarter of data ({quarter}); one filing season, so period coverage is narrow.",
        f"Subset: the {TOP_N} filers with the largest reported Assets among 10-K, 10-Q and their amendments, plus the "
        f"{AMENDERS} largest other filers that filed a 10-K/A or 10-Q/A; small filers are absent.",
        "Only unsegmented facts with standard (non-custom) taxonomy tags are included; dimensional detail and company "
        "extensions are dropped.",
        "Amendments in the quarter mostly repeat the original values; only SHARONAI HOLDINGS INC. restates figures, so "
        "the other disambiguation tasks turn on period, fiscal calendar or parent-versus-total rules.",
        "A filing whose kept facts are identical to an earlier-accepted filing by another filer (a co-registrant copy) is "
        "left out; in this quarter that drops AEP TEXAS INC.'s copy of American Electric Power's 10-Q.",
        "Definitions carry the publisher's US-GAAP label and documentation from the newest release older than the "
        "quarter's newest (us-gaap/2025), because FSDS fills the newest release's labels from filers; tags that only "
        "us-gaap/2026 has keep its documentation and an empty label.",
        "Some filers put the same concept under different tags (Revenues or RevenueFromContractWithCustomerExcludingAssessed"
        "Tax, DebtCurrent or LongTermDebtCurrent); the question does not say which, so a method that cannot inspect the "
        "data must guess or combine tags.",
        "Tasks are not fully independent: APPLE INC appears in 15 tasks, and a few tasks share a sub-fact (for example "
        "cc_exxon_fcf is the first part of cc_exxon_fcf_after_dividends).",
        "sub.prevrpt is 0 for every filing in this quarter (the source flags no filing as superseded), so the most recent "
        "filing must be found from sub.filed.",
        "Ratio and growth answers are rounded to 4 decimal places and accepted within an absolute 0.0001 (book value per share "
        "within 0.01); other numbers within a relative 1e-6.",
        "Unanswerable tasks have a null reference, reached only by an explicit abstention (the llm-sql adapter returns null "
        "only for a query result of exactly 'NOT_IN_DATABASE'); a method that abstains everywhere scores only on that group.",
    ]
