"""sec-fin@0.3.0: data subset, schema and tasks. Built by build_sec_package.py --version 0.3.0."""
from sec_fin_0_2_0 import AMENDERS, FORMS, P, RATIO, TOP_N, absent, fact, ratio, src, task, v

NOTES_MONTHS = ("2026_07", "2026_08", "2026_09")
PROVENANCE_NOTE = (f"; subset: the {TOP_N} filers with the largest reported Assets plus the {AMENDERS} largest other filers "
                   "that filed a 10-K/A or 10-Q/A in the quarter, without co-registrant copies of another filer's filing; "
                   "axis and member labels from the SEC Financial Statement and Notes data sets "
                   + ", ".join(f"https://www.sec.gov/files/dera/data/financial-statement-notes-data-sets/{m}_notes.zip"
                               for m in NOTES_MONTHS))

SCHEMA = """\
-- SEC EDGAR Financial Statement Data Sets (FSDS), one quarter, subset to the largest filers.
-- Readme-level metadata only. Tag labels and definitions, and axis and member labels, are not in this database.

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

-- One row per reported numeric fact of the consolidated entity as a whole: no dimensional qualifier (no axis/member,
-- no co-registrant) and a standard taxonomy tag. Facts belong to a filing, so the same fact can appear in several
-- filings: a filing repeats prior-period comparatives, and an amendment repeats or restates facts of the filing it amends.
CREATE TABLE num (
  adsh TEXT,              -- accession number, joins sub.adsh
  tag TEXT,               -- taxonomy element name, joins tag.tag
  version TEXT,           -- taxonomy version, for example us-gaap/2025
  ddate INTEGER,          -- end date of the fact's period, YYYYMMDD, rounded to the month end (the instant for balance sheet items)
  qtrs INTEGER,           -- duration in quarters rounded: 0 for an instant (point in time), 1, 2, 3, 4 for durations
  uom TEXT,               -- unit of measure: USD (also for per-share amounts such as earnings per share), shares,
                          -- pure (ratios), EUR; dnum also holds other currencies and units (AUD, CAD, GBP, JPY, oz, ...)
  value REAL,             -- reported value in uom (full units, not thousands or millions)
  PRIMARY KEY (adsh, tag, version, ddate, qtrs, uom)
);

-- One row per reported numeric fact qualified by one or more dimensions (axis/member pairs), for example revenue of
-- one business segment, one geographic area or one product line, or one component of equity. Same columns as num
-- plus the qualifier, whose axis/member pairs are in dim. A fact is qualified by ALL of its pairs: a fact with the
-- two pairs BusinessSegments=Cloud and Geographical=US is the Cloud segment's US amount, not the Cloud total. The facts with
-- no qualifier (the consolidated totals) are in num, not here. Standard taxonomy tags only.
CREATE TABLE dnum (
  fact_id INTEGER PRIMARY KEY, -- joins dim.fact_id
  adsh TEXT,              -- accession number, joins sub.adsh
  tag TEXT,               -- taxonomy element name, joins tag.tag
  version TEXT,           -- taxonomy version
  ddate INTEGER,          -- as in num
  qtrs INTEGER,           -- as in num
  uom TEXT,               -- as in num
  ndims INTEGER,          -- number of axis/member pairs qualifying the fact (its rows in dim)
  value REAL              -- reported value in uom (full units)
);

-- One row per axis/member pair of a dnum fact. Names are XBRL element names with the namespace prefix, a leading
-- "Statement" and a trailing "Axis", "Member" or "Domain" removed, as FSDS writes them: the standard axis
-- StatementBusinessSegmentsAxis is BusinessSegments, StatementGeographicalAxis is Geographical, the standard member
-- OperatingSegmentsMember is OperatingSegments. Members a filer defines itself (its own segments, products or
-- regions) carry the filer's element name, for example AmericasSegment. Axis LegalEntity separates the legal entities of
-- a combined filing: co-registrant subsidiaries, and sometimes the parent, variable interest entities or aggregates.
-- A segment's fact may carry a second pair ConsolidationItems=OperatingSegments: some filers report segment figures
-- only that way, some report both forms (with equal or different values), so ndims differs between filers and
-- ndims = 1 cannot be assumed for a segment.
CREATE TABLE dim (
  fact_id INTEGER,        -- joins dnum.fact_id
  axis TEXT,              -- the dimension, for example BusinessSegments, Geographical, ProductOrService,
                          -- ConsolidationItems, EquityComponents, LegalEntity
  member TEXT,            -- the member of that axis the fact belongs to
  PRIMARY KEY (fact_id, axis)
) WITHOUT ROWID;

-- One row per standard taxonomy element used in num or dnum.
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



def dfact(company: str, tag: str, pairs: str, ddate: int = P, qtrs: int = 0, form: str = "10-Q", period: int = P,
          uom: str = "USD") -> str:
    """One dimensional fact of one filing, qualified by exactly these 'Axis=Member;...' pairs and no others."""
    ps = [p.split("=") for p in pairs.split(";")]
    has = " AND ".join(f"EXISTS (SELECT 1 FROM dim d WHERE d.fact_id = n.fact_id AND d.axis = '{a}' AND d.member = '{m}')"
                       for a, m in ps)
    return (f"SELECT n.value FROM sub s JOIN dnum n ON n.adsh = s.adsh WHERE s.name = '{company}' AND s.form = '{form}' "
            f"AND s.period = {period} AND n.tag = '{tag}' AND n.ddate = {ddate} AND n.qtrs = {qtrs} AND n.uom = '{uom}' "
            f"AND n.ndims = {len(ps)} AND {has}")


def dv(*a, **k) -> str:
    return f"({dfact(*a, **k)})"


def members(company: str, tag: str, axis: str, ddate: int = P, qtrs: int = 0, form: str = "10-Q", period: int = P,
            extra: str = "") -> str:
    """FROM/WHERE over the one-pair facts on one axis of one filing (plus optional extra pairs in the WHERE)."""
    return (f"FROM sub s JOIN dnum n ON n.adsh = s.adsh JOIN dim d ON d.fact_id = n.fact_id WHERE s.name = '{company}' "
            f"AND s.form = '{form}' AND s.period = {period} AND n.tag = '{tag}' AND n.ddate = {ddate} AND n.qtrs = {qtrs} "
            f"AND n.uom = 'USD' AND d.axis = '{axis}' {extra}")


K = dict(form="10-K")
MSFT, PG, CSCO, FDX, CAH, ADP = ("MICROSOFT CORP", "PROCTER & GAMBLE CO", "CISCO SYSTEMS, INC.", "FEDEX CORP",
                                 "CARDINAL HEALTH INC", "AUTOMATIC DATA PROCESSING INC")
FY_CSCO, FY_FDX = 20260731, 20260531
APPLE, AMZN, META, LLY, NVDA, WMT, DIS, JNJ, CAT, RTX = (
    "APPLE INC", "AMAZON COM INC", "META PLATFORMS, INC.", "ELI LILLY & CO", "NVIDIA CORP", "WALMART INC.", "WALT DISNEY CO",
    "JOHNSON & JOHNSON", "CATERPILLAR INC", "RTX CORP")
REV = "RevenueFromContractWithCustomerExcludingAssessedTax"
SEG = "ConsolidationItems=OperatingSegments"
Q10 = "original 10-Q filings (form 10-Q, not 10-Q/A) with a period ending 2026-06-30"
TOP = ("FROM sub s JOIN num n ON n.adsh = s.adsh WHERE s.form = '10-Q' AND s.period = 20260630 AND n.ddate = 20260630 "
       "AND n.uom = 'USD' AND ")
NAMES = "Answer with company names exactly as stored in sub.name."

TASKS: list[dict] = [
    # 1. lookup: one consolidated fact, instant or duration, often from a 10-K's fiscal-year columns.
    task("lk_msft_rd_fy", "lookup", "simple", "config",
         f"What was the research and development expense, in US dollars, of {MSFT} for its fiscal year ended 2026-06-30, "
         f"{src('10-K')}?",
         fact(MSFT, "ResearchAndDevelopmentExpense", P, 4, **K), "One R&D tag; the 10-K's fiscal-year column is qtrs = 4."),
    task("lk_cisco_goodwill_prior", "lookup", "simple", "heldout",
         f"What was the goodwill, in US dollars, of {CSCO} on 2025-07-31, {src('10-K', FY_CSCO)}?",
         fact(CSCO, "Goodwill", 20250731, form="10-K", period=FY_CSCO), "The 10-K's comparative balance sheet date."),
    task("lk_pg_net_earnings_parent", "lookup", "moderate", "heldout",
         f"What were the net earnings attributable to {PG} (excluding noncontrolling interests), in US dollars, for its "
         f"fiscal year ended 2026-06-30, {src('10-K')}?",
         fact(PG, "NetIncomeLoss", P, 4, **K), "NetIncomeLoss (16.05B); ProfitLoss (16.14B) includes noncontrolling interests."),
    task("lk_cardinal_equity", "lookup", "moderate", "heldout",
         f"What was the total shareholders' equity (deficit) attributable to {CAH}, in US dollars, on 2026-06-30, "
         f"{src('10-K')}? Give a deficit as a negative number.",
         fact(CAH, "StockholdersEquity", **K), "One StockholdersEquity instant at 2026-06-30 on the balance sheet; it is a deficit."),
    task("lk_cardinal_nci_ni", "lookup", "challenging", "heldout",
         f"What was the net income (loss) attributable to noncontrolling interests, in US dollars, of {CAH} for its fiscal "
         f"year ended 2026-06-30, {src('10-K')}? Give a loss as a negative number.",
         fact(CAH, "NetIncomeLossAttributableToNoncontrollingInterest", P, 4, **K),
         "The fiscal-2026 figure is a 9M loss; fiscal 2025 was an 8M gain."),
    task("lk_msft_cash_fy2025", "lookup", "challenging", "heldout",
         f"What were the cash and cash equivalents, in US dollars, of {MSFT} at the end of its fiscal year 2025, "
         f"{src('10-K')}?",
         fact(MSFT, "CashAndCashEquivalentsAtCarryingValue", 20250630, **K),
         "Microsoft's fiscal year 2025 ended 2025-06-30, the comparative instant in its fiscal-2026 10-K."),

    # 2. aggregate_sector: SIC-filtered counts, sums, averages.
    task("as_6022_count", "aggregate_sector", "simple", "config",
         f"How many {Q10} were filed by filers with SIC code 6022 (state commercial banks)?",
         "SELECT COUNT(*) FROM sub WHERE form = '10-Q' AND period = 20260630 AND sic = 6022", "A count of sub rows."),
    task("as_4911_assets", "aggregate_sector", "simple", "heldout",
         f"What was the combined total assets, in US dollars, on 2026-06-30 of all filers with SIC code 4911 (electric "
         f"services) among {Q10}?",
         f"SELECT SUM(n.value) {TOP}s.sic = 4911 AND n.tag = 'Assets' AND n.qtrs = 0",
         "Consolidated Assets from num; co-registrant subsidiaries' assets are in dnum (axis LegalEntity) and not counted."),
    task("as_2834_avg_assets", "aggregate_sector", "moderate", "heldout",
         f"What was the average total assets, in US dollars, on 2026-06-30 of the filers with SIC code 2834 (pharmaceutical "
         f"preparations) among {Q10}?",
         f"SELECT AVG(n.value) {TOP}s.sic = 2834 AND n.tag = 'Assets' AND n.qtrs = 0",
         "All eight SIC 2834 filings report consolidated Assets at 2026-06-30 (OPKO's small balance pulls the mean down)."),
    task("as_6021_assets", "aggregate_sector", "moderate", "heldout",
         f"What was the combined total assets, in US dollars, on 2026-06-30 of all filers with SIC code 6021 (national "
         f"commercial banks) among {Q10}?",
         f"SELECT SUM(n.value) {TOP}s.sic = 6021 AND n.tag = 'Assets' AND n.qtrs = 0",
         "All 23 SIC 6021 filings report consolidated Assets at 2026-06-30. Total deposits were avoided: Truist reports only "
         "interest-bearing and noninterest-bearing deposits, no total."),
    task("as_7370_rd", "aggregate_sector", "moderate", "heldout",
         f"What was the combined research and development expense, in US dollars, for the three months ended 2026-06-30 of "
         f"all filers with SIC code 7370 (computer programming and data processing) among {Q10}?",
         f"SELECT SUM(n.value) {TOP}s.sic = 7370 AND n.tag = 'ResearchAndDevelopmentExpense' AND n.qtrs = 1",
         "Meta, Alphabet and SpaceX each report one consolidated three-month R&D fact."),
    task("as_4911_coregistrants", "aggregate_sector", "challenging", "heldout",
         f"How many of the {Q10} by filers with SIC code 4911 (electric services) report figures for at least one "
         "separate legal entity (axis LegalEntity), such as a co-registrant subsidiary?",
         "SELECT COUNT(*) FROM sub s WHERE s.form = '10-Q' AND s.period = 20260630 AND s.sic = 4911 AND EXISTS (SELECT 1 FROM "
         "dnum n JOIN dim d ON d.fact_id = n.fact_id WHERE n.adsh = s.adsh AND d.axis = 'LegalEntity')",
         "Counts filings with any LegalEntity fact; the axis also holds parents, VIEs and aggregates, but each of the 12 filings has a genuine subsidiary member."),

    # 3. unanswerable: the database does not contain the fact, including breakdowns a filer does not report.
    absent("un_pg_rd", "simple", "config",
           f"What was the research and development expense, in US dollars, of {PG} for its fiscal year ended 2026-06-30, "
           f"{src('10-K')}?",
           fact(PG, "ResearchAndDevelopmentExpense", P, 4, **K),
           "P&G's filing has no research and development tag, dimensional or not."),
    absent("un_fedex_inventory", "simple", "heldout",
           f"What were the inventories, in US dollars, of {FDX} on 2026-05-31, {src('10-K', FY_FDX)}?",
           fact(FDX, "InventoryNet", FY_FDX, form="10-K", period=FY_FDX), "FedEx's filing has no inventory tag of any kind."),
    absent("un_cisco_q4_revenue", "moderate", "heldout",
           f"What was the revenue, in US dollars, of {CSCO} for the three months ended 2026-07-31, {src('10-K', FY_CSCO)}?",
           fact(CSCO, REV, FY_CSCO, 1, form="10-K", period=FY_CSCO),
           "Cisco's 10-K has fiscal-year columns only: no qtrs = 1 fact of any tag, in num or dnum."),
    absent("un_meta_rl_europe", "moderate", "heldout",
           f"What was the revenue, in US dollars, of the Reality Labs segment of {META} from customers in Europe for the three "
           f"months ended 2026-06-30, {src()}?",
           dfact(META, REV, "BusinessSegments=RealityLabs;Geographical=Europe", P, 1),
           "Meta reports Reality Labs facts with the segment as the only pair; geography is reported for the whole company."),
    absent("un_apple_germany", "moderate", "heldout",
           f"What were the net sales, in US dollars, of {APPLE} in Germany for the three months ended 2026-06-30, {src()}?",
           dfact(APPLE, REV, "Geographical=DE", P, 1),
           "Apple has no Geographical axis at all; its geographic segments (Europe, Americas, ...) are the finest breakdown."),
    absent("un_nvidia_graphics_taiwan", "challenging", "heldout",
           f"What was the revenue, in US dollars, of {NVDA}'s Graphics segment from customers in Taiwan for the three months "
           f"ended 2026-07-31, {src(period=20260731)}?",
           dfact(NVDA, "Revenues", "BusinessSegments=GraphicsSegment;Geographical=TW", 20260731, 1, period=20260731),
           "NVIDIA reports revenue by segment and by country separately, never crossed."),
    absent("un_msft_azure", "challenging", "heldout",
           f"What was the revenue, in US dollars, of {MSFT} from Azure for its fiscal year ended 2026-06-30, {src('10-K')}?",
           dfact(MSFT, REV, "ProductOrService=Azure", P, 4, **K),
           "No member names Azure; Azure sits inside server products and cloud services and the Microsoft Cloud, which "
           "both include other offerings."),
    absent("un_apple_us_net_sales", "challenging", "heldout",
           f"What were the net sales, in US dollars, of {APPLE} in the United States for the three months ended 2026-06-30, "
           f"{src()}?",
           dfact(APPLE, REV, "Geographical=US", P, 1),
           "Apple's 10-Q reports an Americas segment, which also covers Canada and Latin America, and no US figure."),
    absent("un_amazon_us_net_sales", "challenging", "heldout",
           f"What were the net sales, in US dollars, of {AMZN} in the United States for the three months ended 2026-06-30, "
           f"{src()}?",
           dfact(AMZN, REV, "Geographical=US", P, 1),
           "Amazon's 10-Q has no Geographical axis; its North America segment also covers Canada and Mexico."),

    # 4. period_compare: across the fiscal-year columns of a 10-K.
    task("pc_msft_capex_growth", "period_compare", "simple", "config",
         f"What was the growth rate of {MSFT}'s payments for property and equipment in its fiscal year ended 2026-06-30 over "
         f"its fiscal year ended 2025-06-30, {src('10-K')}? Compute (current / prior) - 1 and give it as a decimal rounded "
         "to 4 decimal places.",
         f"SELECT ROUND({v(MSFT, 'PaymentsToAcquirePropertyPlantAndEquipment', P, 4, **K)} * 1.0 / "
         f"{v(MSFT, 'PaymentsToAcquirePropertyPlantAndEquipment', 20250630, 4, **K)} - 1, 4)",
         "Both fiscal-year columns of the 10-K.", tolerance=RATIO),
    task("pc_cisco_inventory_change", "period_compare", "moderate", "heldout",
         f"By how much, in US dollars, did the inventories of {CSCO} change from 2025-07-31 to 2026-07-31, "
         f"{src('10-K', FY_CSCO)}? Give an increase as a positive number.",
         f"SELECT {v(CSCO, 'InventoryNet', FY_CSCO, form='10-K', period=FY_CSCO)} - "
         f"{v(CSCO, 'InventoryNet', 20250731, form='10-K', period=FY_CSCO)}", "Both balance-sheet instants of the 10-K."),
    task("pc_fedex_cash_change", "period_compare", "moderate", "heldout",
         f"By how much, in US dollars, did the cash and cash equivalents of {FDX} change from 2025-05-31 to 2026-05-31, "
         f"{src('10-K', FY_FDX)}? Give an increase as a positive number.",
         f"SELECT {v(FDX, 'CashAndCashEquivalentsAtCarryingValue', FY_FDX, form='10-K', period=FY_FDX)} - "
         f"{v(FDX, 'CashAndCashEquivalentsAtCarryingValue', 20250531, form='10-K', period=FY_FDX)}",
         "FedEx's fiscal year ends in May."),
    task("pc_pg_opinc_change", "period_compare", "moderate", "heldout",
         f"By how much, in US dollars, did the operating income of {PG} change from its fiscal year ended 2025-06-30 to its "
         f"fiscal year ended 2026-06-30, {src('10-K')}? Give a decrease as a negative number.",
         f"SELECT {v(PG, 'OperatingIncomeLoss', P, 4, **K)} - {v(PG, 'OperatingIncomeLoss', 20250630, 4, **K)}",
         "Operating income fell, so the answer is negative."),
    task("pc_cardinal_ocf_growth", "period_compare", "challenging", "heldout",
         f"What was the growth rate of {CAH}'s net cash provided by operating activities in its fiscal year 2026 over its "
         f"fiscal year 2025, {src('10-K')}? Compute (current / prior) - 1 and give it as a decimal rounded to 4 decimal places.",
         f"SELECT ROUND({v(CAH, 'NetCashProvidedByUsedInOperatingActivities', P, 4, **K)} * 1.0 / "
         f"{v(CAH, 'NetCashProvidedByUsedInOperatingActivities', 20250630, 4, **K)} - 1, 4)",
         "Cardinal's fiscal years end in June: fiscal 2026 is the year ended 2026-06-30.", tolerance=RATIO),
    task("pc_cisco_revenue_growth", "period_compare", "challenging", "heldout",
         f"What was the growth rate of {CSCO}'s revenue in its fiscal year 2026 over its fiscal year 2025, "
         f"{src('10-K', FY_CSCO)}? Compute (current / prior) - 1 and give it as a decimal rounded to 4 decimal places.",
         f"SELECT ROUND({v(CSCO, REV, FY_CSCO, 4, form='10-K', period=FY_CSCO)} * 1.0 / "
         f"{v(CSCO, REV, 20250731, 4, form='10-K', period=FY_CSCO)} - 1, 4)",
         "Cisco's fiscal years end in late July: fiscal 2026 is the year ended 2026-07-31.", tolerance=RATIO),

    # 5. ratio: formulas stated in business terms, rounded, with an absolute tolerance.
    task("ra_msft_net_margin", "ratio", "simple", "config",
         f"What was the net margin (net income divided by revenue) of {MSFT} for its fiscal year ended 2026-06-30, "
         f"{src('10-K')}? Give it as a decimal rounded to 4 decimal places.",
         ratio(v(MSFT, "NetIncomeLoss", P, 4, **K), v(MSFT, REV, P, 4, **K)), "Fiscal-year columns (qtrs = 4).",
         tolerance=RATIO),
    task("ra_cisco_current_ratio", "ratio", "moderate", "heldout",
         f"What was the current ratio (total current assets divided by total current liabilities) of {CSCO} on 2026-07-31, "
         f"{src('10-K', FY_CSCO)}? Give it as a decimal rounded to 4 decimal places.",
         ratio(v(CSCO, "AssetsCurrent", FY_CSCO, form="10-K", period=FY_CSCO),
               v(CSCO, "LiabilitiesCurrent", FY_CSCO, form="10-K", period=FY_CSCO)),
         "Both instants at the 10-K's balance sheet date.", tolerance=RATIO),
    task("ra_fedex_capex_to_ocf", "ratio", "moderate", "heldout",
         f"What was the ratio of capital expenditures to net cash provided by operating activities of {FDX} for its fiscal "
         f"year ended 2026-05-31, {src('10-K', FY_FDX)}? Give it as a decimal rounded to 4 decimal places.",
         ratio(v(FDX, "PaymentsToAcquireProductiveAssets", FY_FDX, 4, form="10-K", period=FY_FDX),
               v(FDX, "NetCashProvidedByUsedInOperatingActivities", FY_FDX, 4, form="10-K", period=FY_FDX)),
         "FedEx tags capital expenditures as PaymentsToAcquireProductiveAssets; it has no property-and-equipment payment tag.",
         tolerance=RATIO),
    task("ra_pg_payout", "ratio", "moderate", "heldout",
         f"What was the dividend payout ratio of {PG} for its fiscal year ended 2026-06-30, defined as dividends paid divided "
         f"by net earnings attributable to Procter & Gamble, {src('10-K')}? Give it as a decimal rounded to 4 decimal places.",
         ratio(v(PG, "PaymentsOfDividends", P, 4, **K), v(PG, "NetIncomeLoss", P, 4, **K)),
         "Dividends paid is the one cash-flow dividend tag; the denominator excludes noncontrolling interests.",
         tolerance=RATIO),
    task("ra_adp_rd_intensity", "ratio", "moderate", "heldout",
         f"What was the research and development expense of {ADP} divided by its revenue for its fiscal year ended "
         f"2026-06-30, {src('10-K')}? Give it as a decimal rounded to 4 decimal places.",
         ratio(v(ADP, "ResearchAndDevelopmentExpense", P, 4, **K), v(ADP, REV, P, 4, **K)),
         "One R&D and one revenue tag, fiscal-year columns.", tolerance=RATIO),
    task("ra_cardinal_inventory_turnover", "ratio", "challenging", "heldout",
         f"What was the inventory turnover of {CAH} for its fiscal year ended 2026-06-30, defined as cost of products sold "
         f"divided by the average of inventories at 2025-06-30 and 2026-06-30, {src('10-K')}? Give it as a decimal rounded "
         "to 4 decimal places.",
         ratio(v(CAH, "CostOfGoodsAndServicesSold", P, 4, **K),
               f"(({v(CAH, 'InventoryNet', **K)} + {v(CAH, 'InventoryNet', 20250630, **K)}) / 2.0)"),
         "One cost-of-sales tag for the fiscal year over two inventory instants.", tolerance=RATIO),
    task("ra_msft_tax_rate", "ratio", "challenging", "heldout",
         f"What was the effective income tax rate (income tax expense divided by income before income taxes) of {MSFT} for "
         f"its fiscal year ended 2026-06-30, {src('10-K')}? Give it as a decimal rounded to 4 decimal places.",
         ratio(v(MSFT, "IncomeTaxExpenseBenefit", P, 4, **K),
               v(MSFT, "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest", P, 4,
                 **K)),
         "One pre-tax income tag; fiscal-year columns.", tolerance=RATIO),
    task("ra_cisco_roe", "ratio", "challenging", "heldout",
         f"What was the return on average equity of {CSCO} for its fiscal year ended 2026-07-31, defined as net income "
         f"divided by the average of total shareholders' equity at 2025-07-31 and 2026-07-31, {src('10-K', FY_CSCO)}? Give "
         "it as a decimal rounded to 4 decimal places.",
         ratio(v(CSCO, "NetIncomeLoss", FY_CSCO, 4, form="10-K", period=FY_CSCO),
               f"(({v(CSCO, 'StockholdersEquity', FY_CSCO, form='10-K', period=FY_CSCO)} + "
               f"{v(CSCO, 'StockholdersEquity', 20250731, form='10-K', period=FY_CSCO)}) / 2.0)"),
         "Cisco has no noncontrolling interests; equity at the two balance sheet dates.", tolerance=RATIO),

    # 6. aggregate_rank: ordering across filers.
    task("ar_equity_top3", "aggregate_rank", "simple", "config",
         f"Among {Q10}, which three filers reported the largest shareholders' equity attributable to the company (excluding "
         f"noncontrolling interests) on 2026-06-30? {NAMES}",
         f"SELECT s.name {TOP}n.tag = 'StockholdersEquity' AND n.qtrs = 0 ORDER BY n.value DESC LIMIT 3",
         "Third (Amazon, 551.6B) and fourth (JPMorgan, 374.6B) are far apart; filers that report only total equity "
         "including noncontrolling interests are all below 127B. Cash was avoided: Citigroup, JPMorgan and Bank of America "
         "report cash only together with restricted cash.", fmt="names"),
    task("ar_rd_top1", "aggregate_rank", "simple", "heldout",
         f"Among {Q10}, which filer reported the largest research and development expense for the three months ended "
         f"2026-06-30? {NAMES}",
         f"SELECT s.name {TOP}n.tag = 'ResearchAndDevelopmentExpense' AND n.qtrs = 1 ORDER BY n.value DESC LIMIT 1",
         "Meta 21.66B, Alphabet 18.22B.", fmt="names"),
    task("ar_goodwill_top5", "aggregate_rank", "moderate", "heldout",
         f"Among {Q10}, which five filers reported the largest goodwill on 2026-06-30? {NAMES}",
         f"SELECT s.name {TOP}n.tag = 'Goodwill' AND n.qtrs = 0 ORDER BY n.value DESC LIMIT 5",
         "Fifth (IBM, 74.60B) and sixth (Pfizer, 71.42B) are 3.2B apart; segment goodwill facts are in dnum. Inventories "
         "were avoided: Boeing reports inventory net of customer advances (88.4B) under a different tag.", fmt="names"),
    task("ar_capex_top3", "aggregate_rank", "challenging", "heldout",
         f"Among {Q10}, which three filers reported the largest capital expenditures for the six months ended 2026-06-30, "
         "where a filer reports capital expenditures either as payments to acquire property, plant and equipment or as "
         f"payments to acquire productive assets? {NAMES}",
         f"SELECT s.name {TOP}n.tag IN ('PaymentsToAcquirePropertyPlantAndEquipment', 'PaymentsToAcquireProductiveAssets') "
         "AND n.qtrs = 2 ORDER BY n.value DESC LIMIT 3",
         "No filing reports both tags. Amazon (98.4B) uses PaymentsToAcquireProductiveAssets; Alphabet 80.6B, Meta 49.1B, "
         "then SpaceX 28.5B.", fmt="names"),
    task("ar_pfizer_goodwill_rank", "aggregate_rank", "moderate", "heldout",
         f"Among {Q10} that reported goodwill on 2026-06-30, what was the rank of PFIZER INC by goodwill on that date, where "
         "rank 1 is the largest?",
         f"SELECT COUNT(*) + 1 {TOP}n.tag = 'Goodwill' AND n.qtrs = 0 AND n.value > {v('PFIZER INC', 'Goodwill')}",
         "Pfizer 71.4B is sixth behind UnitedHealth, CVS, Berkshire, Disney (74.68B) and IBM (74.60B); no ties."),
    task("ar_lowest_current_ratio", "aggregate_rank", "challenging", "heldout",
         f"Among {Q10} that reported both total current assets and total current liabilities on 2026-06-30, which filer had "
         f"the lowest current ratio (current assets divided by current liabilities)? {NAMES}",
         "SELECT s.name FROM sub s JOIN num a ON a.adsh = s.adsh AND a.tag = 'AssetsCurrent' AND a.ddate = 20260630 "
         "AND a.qtrs = 0 AND a.uom = 'USD' JOIN num l ON l.adsh = s.adsh AND l.tag = 'LiabilitiesCurrent' AND l.ddate = 20260630 "
         "AND l.qtrs = 0 AND l.uom = 'USD' WHERE s.form = '10-Q' AND s.period = 20260630 ORDER BY a.value / l.value LIMIT 1",
         "Royal Caribbean 0.209, next American Tower 0.352; banks report no current assets and drop out.", fmt="names"),
    task("ar_eps_drop", "aggregate_rank", "challenging", "heldout",
         f"Among {Q10} that reported diluted earnings per share for both the three months ended 2026-06-30 and the three "
         "months ended 2025-06-30, which filer had the largest decrease in diluted earnings per share, in US dollars per "
         f"share? {NAMES}",
         "SELECT s.name FROM sub s JOIN num c ON c.adsh = s.adsh AND c.tag = 'EarningsPerShareDiluted' AND c.ddate = 20260630 "
         "AND c.qtrs = 1 AND c.uom = 'USD' JOIN num p ON p.adsh = s.adsh AND p.tag = 'EarningsPerShareDiluted' "
         "AND p.ddate = 20250630 AND p.qtrs = 1 AND p.uom = 'USD' WHERE s.form = '10-Q' AND s.period = 20260630 "
         "ORDER BY c.value - p.value LIMIT 1",
         "Strategy's diluted EPS fell by 57.05; next SharonAI by 20.43.", fmt="names"),
    task("ar_bank_roa_top3", "aggregate_rank", "challenging", "heldout",
         f"Among {Q10} by filers with SIC code 6021 (national commercial banks), which three had the highest ratio of net "
         "income attributable to the company for the three months ended 2026-06-30 to total assets on 2026-06-30? "
         f"{NAMES}",
         "SELECT s.name FROM sub s JOIN num i ON i.adsh = s.adsh AND i.tag = 'NetIncomeLoss' AND i.ddate = 20260630 "
         "AND i.qtrs = 1 AND i.uom = 'USD' JOIN num a ON a.adsh = s.adsh AND a.tag = 'Assets' AND a.ddate = 20260630 "
         "AND a.qtrs = 0 AND a.uom = 'USD' WHERE s.form = '10-Q' AND s.period = 20260630 AND s.sic = 6021 "
         "ORDER BY i.value / a.value DESC LIMIT 3",
         "Third (JPMorgan, 42.2 bp) and fourth (UMB, 38.4 bp) are apart. Truist and PNC lack NetIncomeLoss; computed from "
         "ProfitLoss they reach 27.9 and 33.1 bp, below the top three either way.", fmt="names"),

    # 7. disambiguation: competing facts, often a segment total versus its external part; the question states the rule.
    task("di_walmart_net_sales", "disambiguation", "moderate", "config",
         f"What were the consolidated net sales (excluding membership and other income), in US dollars, of {WMT} for the "
         f"three months ended 2026-07-31, {src(period=20260731)}?",
         fact(WMT, REV, 20260731, 1, period=20260731),
         "Net sales is RevenueFromContractWithCustomerExcludingAssessedTax; total revenues (Revenues) add membership income."),
    task("di_cat_pe_external", "disambiguation", "moderate", "heldout",
         f"What were the sales and revenues, in US dollars, of {CAT}'s Power & Energy segment to external customers (excluding sales to other "
         f"Caterpillar segments) for the three months ended 2026-06-30, {src()}?",
         dfact(CAT, "Revenues", "BusinessSegments=PowerEnergy;ConsolidationItems=OperatingSegmentsExcludingIntersegmentElimination",
               P, 1),
         "External sales 6.795B, reported both with the segment alone and with the excluding-intersegment member (same value); "
         "total segment sales including intersegment are 8.238B."),
    task("di_rtx_collins_total", "disambiguation", "moderate", "heldout",
         f"What were the total sales, in US dollars, of {RTX}'s Collins Aerospace segment for the three months ended 2026-06-30, including "
         f"its sales to other RTX segments, {src()}?",
         dfact(RTX, REV, "BusinessSegments=CollinsAerospace;" + SEG, P, 1),
         "Segment total 8.210B (with ConsolidationItems=OperatingSegments); external sales alone are 7.552B."),
    task("di_cardinal_profit_incl_nci", "disambiguation", "moderate", "heldout",
         f"What were the net earnings of {CAH} for its fiscal year ended 2026-06-30, including the portion attributable to "
         f"noncontrolling interests, in US dollars, {src('10-K')}?",
         fact(CAH, "ProfitLoss", P, 4, **K), "ProfitLoss (1.705B) includes the -9M noncontrolling share; NetIncomeLoss is 1.714B."),
    task("di_disney_entertainment_ads", "disambiguation", "challenging", "heldout",
         f"What was the advertising revenue, in US dollars, of {DIS}'s Entertainment segment alone for the three months ended "
         f"2026-06-30, {src()}?",
         dfact(DIS, "Revenues", "BusinessSegments=EntertainmentSegment;ProductOrService=Advertising", P, 1),
         "Company-wide advertising is 2.829B; the Entertainment segment's is 1.625B (Sports carries the rest)."),
    task("di_lilly_jardiance", "disambiguation", "challenging", "heldout",
         f"What was the total revenue, in US dollars, of {LLY} from Jardiance worldwide for the three months ended 2026-06-30, "
         f"{src()}?",
         dfact(LLY, "Revenues", "ProductOrService=Jardiance", P, 1),
         "The product's total is the fact with only the product pair (1.232B); the 250M fact with a second pair "
         "(TypeOfArrangement=MilestonePaymentsSalesBased) is one arrangement type, and US/non-US facts carry a Geographical pair. "
         "US 616M + non-US 615M = 1.231B (filing rounding), so a rel 1e-3 tolerance accepts that build.", tolerance={"rel": 0.001}),
    task("di_msft_product_revenue", "disambiguation", "challenging", "heldout",
         f"What was the revenue {MSFT} classifies as product revenue (as opposed to service and other revenue) for its fiscal "
         f"year ended 2026-06-30, in US dollars, {src('10-K')}?",
         dfact(MSFT, REV, "ProductOrService=Product", P, 4, **K),
         "ProductOrService=Product (64.7B) is the product/service split; the product-line members (Xbox, Windows, ...) are "
         "a different breakdown on the same axis."),
    task("di_meta_foa_advertising", "disambiguation", "challenging", "heldout",
         f"What was the advertising revenue, in US dollars, of {META}'s Family of Apps segment for the three months ended "
         f"2026-06-30, excluding the segment's other revenue, {src()}?",
         dfact(META, REV, "BusinessSegments=FamilyOfApps;ProductOrService=Advertising", P, 1),
         "Family of Apps total revenue is 60.370B; advertising alone is 59.363B (two pairs)."),

    # 8. concept_composition: three or more concepts, sign conventions, subtotals.
    task("cc_msft_fcf", "concept_composition", "moderate", "config",
         f"What was the free cash flow, in US dollars, of {MSFT} for its fiscal year ended 2026-06-30, defined as net cash "
         f"from operating activities minus additions to property and equipment, {src('10-K')}?",
         f"SELECT {v(MSFT, 'NetCashProvidedByUsedInOperatingActivities', P, 4, **K)} - "
         f"{v(MSFT, 'PaymentsToAcquirePropertyPlantAndEquipment', P, 4, **K)}", "Both fiscal-year cash-flow facts."),
    task("cc_adp_working_capital", "concept_composition", "simple", "heldout",
         f"What was the working capital, in US dollars, of {ADP} on 2026-06-30, defined as total current assets minus total "
         f"current liabilities, {src('10-K')}?",
         f"SELECT {v(ADP, 'AssetsCurrent', **K)} - {v(ADP, 'LiabilitiesCurrent', **K)}",
         "Both instants include client funds, as ADP's balance sheet does."),
    task("cc_cisco_shareholder_returns", "concept_composition", "moderate", "heldout",
         f"How much cash, in US dollars, did {CSCO} return to shareholders in its fiscal year ended 2026-07-31, defined as "
         f"repurchases of common stock plus dividends paid, {src('10-K', FY_CSCO)}?",
         f"SELECT {v(CSCO, 'PaymentsForRepurchaseOfCommonStock', FY_CSCO, 4, form='10-K', period=FY_CSCO)} + "
         f"{v(CSCO, 'PaymentsOfDividends', FY_CSCO, 4, form='10-K', period=FY_CSCO)}", "Both fiscal-year cash-flow facts."),
    task("cc_pg_fcf_after_dividends", "concept_composition", "moderate", "heldout",
         f"What was the free cash flow after dividends, in US dollars, of {PG} for its fiscal year ended 2026-06-30, defined "
         f"as operating cash flow minus capital expenditures minus dividends paid, {src('10-K')}?",
         f"SELECT {v(PG, 'NetCashProvidedByUsedInOperatingActivities', P, 4, **K)} - "
         f"{v(PG, 'PaymentsToAcquirePropertyPlantAndEquipment', P, 4, **K)} - {v(PG, 'PaymentsOfDividends', P, 4, **K)}",
         "Three fiscal-year cash-flow facts."),
    task("cc_cisco_net_debt", "concept_composition", "moderate", "heldout",
         f"What was the net debt, in US dollars, of {CSCO} on 2026-07-31, defined as short-term debt plus long-term debt "
         f"minus cash and cash equivalents, {src('10-K', FY_CSCO)}?",
         f"SELECT {v(CSCO, 'DebtCurrent', FY_CSCO, form='10-K', period=FY_CSCO)} + "
         f"{v(CSCO, 'LongTermDebtNoncurrent', FY_CSCO, form='10-K', period=FY_CSCO)} - "
         f"{v(CSCO, 'CashAndCashEquivalentsAtCarryingValue', FY_CSCO, form='10-K', period=FY_CSCO)}",
         "Cisco reports one current-debt (DebtCurrent) and one noncurrent-debt total."),
    task("cc_fedex_ebitda", "concept_composition", "moderate", "heldout",
         f"What was the EBITDA, in US dollars, of {FDX} for its fiscal year ended 2026-05-31, defined as operating income "
         f"plus depreciation and amortization, {src('10-K', FY_FDX)}?",
         f"SELECT {v(FDX, 'OperatingIncomeLoss', FY_FDX, 4, form='10-K', period=FY_FDX)} + "
         f"{v(FDX, 'DepreciationDepletionAndAmortization', FY_FDX, 4, form='10-K', period=FY_FDX)}",
         "One operating income and one D&A total for the fiscal year."),
    task("cc_msft_sm_ga", "concept_composition", "challenging", "heldout",
         f"What were the operating expenses of {MSFT} other than research and development for its fiscal year ended "
         f"2026-06-30, computed as revenue minus cost of revenue minus research and development minus operating income, in "
         f"US dollars, {src('10-K')}?",
         f"SELECT {v(MSFT, REV, P, 4, **K)} - {v(MSFT, 'CostOfGoodsAndServicesSold', P, 4, **K)} - "
         f"{v(MSFT, 'ResearchAndDevelopmentExpense', P, 4, **K)} - {v(MSFT, 'OperatingIncomeLoss', P, 4, **K)}",
         "Four fiscal-year facts; the result equals sales and marketing plus general and administrative (26.710B + 7.956B)."),
    task("cc_cardinal_nonoperating", "concept_composition", "challenging", "heldout",
         f"What was the net non-operating result of {CAH} for its fiscal year ended 2026-06-30, computed as income before "
         f"income taxes minus operating income, in US dollars, {src('10-K')}? Give a net expense as a negative number.",
         f"SELECT {v(CAH, 'IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest', P, 4, **K)}"
         f" - {v(CAH, 'OperatingIncomeLoss', P, 4, **K)}",
         "Pre-tax income (2.174B) is below operating income (2.613B): a net expense, negative."),
    task("cc_cisco_operating_expenses", "concept_composition", "challenging", "heldout",
         f"What were the total operating expenses of {CSCO} for its fiscal year ended 2026-07-31, computed as gross margin "
         f"minus operating income, in US dollars, {src('10-K', FY_CSCO)}?",
         f"SELECT {v(CSCO, 'GrossProfit', FY_CSCO, 4, form='10-K', period=FY_CSCO)} - "
         f"{v(CSCO, 'OperatingIncomeLoss', FY_CSCO, 4, form='10-K', period=FY_CSCO)}",
         "GrossProfit and OperatingIncomeLoss, fiscal-year columns."),
    task("cc_cardinal_ebitda_less_capex", "concept_composition", "challenging", "heldout",
         f"What was the EBITDA minus capital expenditures, in US dollars, of {CAH} for its fiscal year ended 2026-06-30, "
         f"defined as operating income plus depreciation and amortization minus additions to property and equipment, "
         f"{src('10-K')}?",
         f"SELECT {v(CAH, 'OperatingIncomeLoss', P, 4, **K)} + {v(CAH, 'DepreciationDepletionAndAmortization', P, 4, **K)} - "
         f"{v(CAH, 'PaymentsToAcquirePropertyPlantAndEquipment', P, 4, **K)}", "Three fiscal-year facts."),
    task("cc_adp_cash_after_returns", "concept_composition", "challenging", "heldout",
         f"What was the operating cash flow of {ADP} left after shareholder returns for its fiscal year ended 2026-06-30, "
         f"defined as net cash from operating activities minus dividends paid minus repurchases of common stock, in US "
         f"dollars, {src('10-K')}?",
         f"SELECT {v(ADP, 'NetCashProvidedByUsedInOperatingActivities', P, 4, **K)} - {v(ADP, 'PaymentsOfDividends', P, 4, **K)}"
         f" - {v(ADP, 'PaymentsForRepurchaseOfCommonStock', P, 4, **K)}", "Three fiscal-year cash-flow facts."),
    task("cc_msft_net_cash", "concept_composition", "challenging", "heldout",
         f"What was the net cash position of {MSFT} on 2026-06-30, defined as cash and cash equivalents minus the current "
         f"portion of long-term debt minus noncurrent long-term debt, in US dollars, {src('10-K')}? Give a net debt position "
         "as a negative number.",
         f"SELECT {v(MSFT, 'CashAndCashEquivalentsAtCarryingValue', **K)} - {v(MSFT, 'LongTermDebtCurrent', **K)} - "
         f"{v(MSFT, 'LongTermDebtNoncurrent', **K)}",
         "Cash alone (short-term investments excluded) is below total debt, so the result is negative."),

    # 9. segment_lookup: one dimensional fact named in business language.
    task("sl_apple_greater_china", "segment_lookup", "moderate", "config",
         f"What were the net sales, in US dollars, of {APPLE}'s Greater China segment for the three months ended 2026-06-30, "
         f"{src()}?",
         dfact(APPLE, REV, "BusinessSegments=GreaterChinaSegment;" + SEG, P, 1),
         "The segment's sales carry two pairs (the segment and ConsolidationItems=OperatingSegments); no other Greater China "
         "revenue fact exists for the quarter."),
    task("sl_apple_iphone_9m", "segment_lookup", "simple", "heldout",
         f"What were the net sales, in US dollars, of iPhone for {APPLE} for the nine months ended 2026-06-30, {src()}?",
         dfact(APPLE, REV, "ProductOrService=IPhone", P, 3), "One iPhone fact at qtrs = 3; the quarter is qtrs = 1."),
    task("sl_amazon_aws_opinc", "segment_lookup", "simple", "heldout",
         f"What was the operating income, in US dollars, of {AMZN}'s AWS segment for the three months ended 2026-06-30, "
         f"{src()}?",
         dfact(AMZN, "OperatingIncomeLoss", "BusinessSegments=AmazonWebServicesSegment", P, 1),
         "One segment operating income fact per segment for the quarter."),
    task("sl_meta_reality_labs", "segment_lookup", "moderate", "heldout",
         f"What was the operating income (loss), in US dollars, of {META}'s Reality Labs segment for the three months ended "
         f"2026-06-30, {src()}? Give a loss as a negative number.",
         dfact(META, "OperatingIncomeLoss", "BusinessSegments=RealityLabs", P, 1), "The segment's quarterly result is a loss."),
    task("sl_lilly_zepbound_us", "segment_lookup", "moderate", "heldout",
         f"What was the revenue, in US dollars, of {LLY} from Zepbound in the United States for the three months ended "
         f"2026-06-30, {src()}?",
         dfact(LLY, "Revenues", "Geographical=US;ProductOrService=Zepbound", P, 1),
         "Two pairs, product and country; worldwide Zepbound is 4.928B and outside the US 55M."),
    task("sl_msft_intelligent_cloud", "segment_lookup", "moderate", "heldout",
         f"What was the revenue, in US dollars, of {MSFT}'s Intelligent Cloud segment for its fiscal year ended 2026-06-30, "
         f"{src('10-K')}?",
         dfact(MSFT, REV, "BusinessSegments=IntelligentCloud", P, 4, **K),
         "One segment revenue fact for the fiscal year; the Microsoft Cloud (a different, cross-segment measure) is not it."),
    task("sl_jnj_oncology", "segment_lookup", "moderate", "heldout",
         f"What were the sales, in US dollars, of the Oncology franchise of {JNJ}'s Innovative Medicine segment for the three "
         f"months ended 2026-06-30, {src()}?",
         dfact(JNJ, REV, "BusinessSegments=InnovativeMedicine;Subsegments=Oncology", P, 1),
         "Franchises are members of axis Subsegments within the segment; one Oncology fact for the quarter."),
    task("sl_nvidia_taiwan", "segment_lookup", "moderate", "heldout",
         f"What was the revenue, in US dollars, of {NVDA} attributed to Taiwan for the three months ended 2026-07-31, "
         f"{src(period=20260731)}?",
         dfact(NVDA, "Revenues", "Geographical=TW", 20260731, 1, period=20260731),
         "Taiwan is the country member TW on the Geographical axis; one fact for the quarter."),
    task("sl_cat_pe_total_sales", "segment_lookup", "challenging", "heldout",
         f"What were the total sales and revenues, in US dollars, of {CAT}'s Power & Energy segment for the three months "
         f"ended 2026-06-30, including its sales to other Caterpillar segments, {src()}?",
         dfact(CAT, "Revenues", "BusinessSegments=PowerEnergy;" + SEG, P, 1),
         "With ConsolidationItems=OperatingSegments the segment total is 8.238B; the segment-only fact (6.795B) is external "
         "sales, and the intersegment part is -1.443B."),
    task("sl_walmart_sams_revenues", "segment_lookup", "challenging", "heldout",
         f"What were the total revenues (net sales plus membership and other income), in US dollars, of {WMT}'s Sam's Club "
         f"U.S. segment for the three months ended 2026-07-31, {src(period=20260731)}?",
         dfact(WMT, "Revenues", "BusinessSegments=SamsClubUS;" + SEG, 20260731, 1, period=20260731),
         "Revenues (26.367B) is the total; the segment's net sales (RevenueFromContractWithCustomer..., 25.713B) excludes "
         "membership income."),
    task("sl_southern_georgia_power", "segment_lookup", "challenging", "heldout",
         "What was the net income, in US dollars, of Georgia Power Company for the three months ended 2026-06-30, as reported "
         "in SOUTHERN CO's 10-Q for the period ended 2026-06-30?",
         dfact("SOUTHERN CO", "NetIncomeLoss", "LegalEntity=GeorgiaPower", P, 1),
         "Georgia Power is a co-registrant: its figures are facts of Southern Co's filing on axis LegalEntity."),
    task("sl_aep_texas_ni_6m", "segment_lookup", "challenging", "heldout",
         "What was the net income, in US dollars, of AEP Texas Inc. for the six months ended 2026-06-30, as reported in "
         "AMERICAN ELECTRIC POWER CO INC's 10-Q for the period ended 2026-06-30?",
         dfact("AMERICAN ELECTRIC POWER CO INC", "NetIncomeLoss", "LegalEntity=AEPTexasInc.", P, 2),
         "AEP Texas's own copy of the filing is not in sub (co-registrant rule); its figures are LegalEntity facts of AEP's."),

    # 10. segment_aggregate: sums, counts and maxima over members, members against the consolidated total, eliminations.
    task("sa_amazon_segment_opinc", "segment_aggregate", "simple", "config",
         f"What was the combined operating income, in US dollars, of {AMZN}'s North America and International segments for the "
         f"three months ended 2026-06-30, {src()}?",
         f"SELECT SUM(n.value) {members(AMZN, 'OperatingIncomeLoss', 'BusinessSegments', P, 1)} AND n.ndims = 1 AND d.member IN "
         "('NorthAmericaSegment', 'InternationalSegment')",
         "Two of the three segment facts (the third is AWS); all three sum to consolidated operating income, so asking for all would not need dnum."),
    task("sa_apple_segment_count", "segment_aggregate", "simple", "heldout",
         f"How many geographic business segments does {APPLE} report net sales for in its 10-Q for the period ended "
         "2026-06-30, for the three months ended 2026-06-30?",
         f"SELECT COUNT(DISTINCT d.member) {members(APPLE, REV, 'BusinessSegments', P, 1)}",
         "Americas, Europe, Greater China, Japan and Rest of Asia Pacific."),
    task("sa_msft_segment_opinc", "segment_aggregate", "simple", "heldout",
         f"What was the combined operating income, in US dollars, of {MSFT}'s Intelligent Cloud and More Personal Computing "
         f"segments for its fiscal year ended 2026-06-30, {src('10-K')}?",
         f"SELECT SUM(n.value) {members(MSFT, 'OperatingIncomeLoss', 'BusinessSegments', P, 4, **K)} AND n.ndims = 1 AND d.member IN "
         "('IntelligentCloud', 'MorePersonalComputing')",
         "Two of the three one-pair segment facts for the fiscal year; all three sum to consolidated operating income, so asking for all would not need dnum."),
    task("sa_meta_outside_us_canada", "segment_aggregate", "moderate", "heldout",
         f"What was the revenue, in US dollars, of {META} from outside the United States and Canada for the three months "
         f"ended 2026-06-30, {src()}?",
         f"SELECT SUM(n.value) {members(META, REV, 'Geographical', P, 1)} AND n.ndims = 1 AND d.member <> 'USCanada'",
         "Meta's four regions (USCanada, Europe, AsiaPacific, RestOfWorld) partition revenue; the answer is the other three."),
    task("sa_duke_coregistrants", "segment_aggregate", "moderate", "heldout",
         "For how many co-registrant subsidiaries does DUKE ENERGY CORP's 10-Q for the period ended 2026-06-30 report net "
         "income for the three months ended 2026-06-30?",
         f"SELECT COUNT(DISTINCT d.member) {members('DUKE ENERGY CORP', 'NetIncomeLoss', 'LegalEntity', P, 1)} AND n.ndims = 1",
         "Distinct LegalEntity members with a quarterly NetIncomeLoss fact."),
    task("sa_lilly_non_us", "segment_aggregate", "moderate", "heldout",
         f"What was the total revenue, in US dollars, of {LLY} from outside the United States for the three months ended "
         f"2026-06-30, {src()}?",
         dfact(LLY, "Revenues", "Geographical=NonUs", P, 1),
         "NonUs (8.561B) is itself a member and equals Europe + Japan + China + rest of world; summing every non-US member "
         "would count it twice."),
    task("sa_disney_segments_gross", "segment_aggregate", "challenging", "heldout",
         f"What was the combined revenue, in US dollars, of {DIS}'s Entertainment, Sports and Experiences segments for the "
         f"three months ended 2026-06-30, before intersegment eliminations, {src()}?",
         f"SELECT SUM(n.value) {members(DIS, 'Revenues', 'BusinessSegments', P, 1)} AND n.ndims = 1 AND d.member IN "
         "('EntertainmentSegment', 'SportsSegment', 'ExperiencesSegment')",
         "Three one-pair segment facts (25.813B); consolidated revenue (25.248B) is after the -565M eliminations."),
    task("sa_disney_eliminations", "segment_aggregate", "moderate", "heldout",
         f"What was the amount, in US dollars, of {DIS}'s intersegment revenue eliminations for the three months ended 2026-06-30, {src()}? "
         "Give a reduction of revenue as a negative number.",
         dfact(DIS, "Revenues", "BusinessSegments=SegmentEliminations", P, 1),
         "The SegmentEliminations member alone; its geographic and product splits carry a second pair."),
    task("sa_cat_intersegment", "segment_aggregate", "challenging", "heldout",
         f"What was the total, in US dollars, of sales between all of {CAT}'s operating segments, including All Other (intersegment "
         f"sales eliminated in consolidation), for the three months ended 2026-06-30, {src()}? Give it as a positive number.",
         f"SELECT ABS({dv(CAT, 'Revenues', 'ConsolidationItems=IntersegmentElimination', P, 1)})",
         "Caterpillar reports the total as +1.677B; the per-segment elimination facts (-69M, -85M, 0, -1.443B, -80M) sum to "
         "-1.677B, so either route gives the same magnitude."),
    task("sa_rtx_external_sales", "segment_aggregate", "moderate", "heldout",
         f"What were the combined sales to external customers, in US dollars, of {RTX}'s Collins Aerospace, Pratt & Whitney "
         f"and Raytheon segments for the three months ended 2026-06-30, {src()}?",
         f"SELECT SUM(n.value) {members(RTX, REV, 'BusinessSegments', P, 1)} AND n.ndims = 1 AND d.member IN "
         "('CollinsAerospace', 'PrattAndWhitney', 'Raytheon')",
         "External sales are the one-pair segment facts; with ConsolidationItems=OperatingSegments they include intersegment sales."),
    task("sa_jnj_top_medtech_franchise", "segment_aggregate", "moderate", "heldout",
         f"What were the sales, in US dollars, of the largest franchise of {JNJ}'s MedTech segment for the three months ended "
         f"2026-06-30, {src()}?",
         f"SELECT MAX(n.value) {members(JNJ, REV, 'Subsegments', P, 1)} AND n.ndims = 2 AND EXISTS (SELECT 1 FROM dim e WHERE "
         "e.fact_id = n.fact_id AND e.axis = 'BusinessSegments' AND e.member = 'MedTech')",
         "Surgery (2.653B) leads Orthopaedics (2.418B)."),
    task("sa_lilly_cardiometabolic_other", "segment_aggregate", "challenging", "heldout",
         f"What was the worldwide revenue, in US dollars, of {LLY}'s cardiometabolic health products other than Mounjaro and "
         f"Zepbound for the three months ended 2026-06-30, {src()}?",
         f"SELECT {dv(LLY, 'Revenues', 'ProductOrService=CardiometabolicHealth', P, 1)} - "
         f"{dv(LLY, 'Revenues', 'ProductOrService=Mounjaro', P, 1)} - {dv(LLY, 'Revenues', 'ProductOrService=Zepbound', P, 1)}",
         "Cardiometabolic health (18.353B) is the parent of Mounjaro, Zepbound, Jardiance, Trulicity and other; the remainder "
         "equals Jardiance + Trulicity + other cardiometabolic (3.482B)."),
    task("sa_southern_coregistrants_ni", "segment_aggregate", "challenging", "heldout",
         "What was the combined net income attributable to each co-registrant subsidiary (after noncontrolling interests), in US "
         "dollars, of the co-registrant subsidiaries reported in SOUTHERN CO's 10-Q "
         "for the period ended 2026-06-30, for the three months ended 2026-06-30?",
         f"SELECT SUM(n.value) {members('SOUTHERN CO', 'NetIncomeLoss', 'LegalEntity', P, 1)} AND n.ndims = 1",
         "Five LegalEntity members with a quarterly NetIncomeLoss fact (attributable to the entity; Southern Power also reports ProfitLoss, before its noncontrolling share)."),

    # 11. segment_composition: segment facts combined with other concepts, totals or periods.
    task("sc_amazon_aws_margin", "segment_composition", "moderate", "config",
         f"What was the operating margin of {AMZN}'s AWS segment (segment operating income divided by segment net sales) for "
         f"the three months ended 2026-06-30, {src()}? Give it as a decimal rounded to 4 decimal places.",
         ratio(dv(AMZN, "OperatingIncomeLoss", "BusinessSegments=AmazonWebServicesSegment", P, 1),
               dv(AMZN, REV, "BusinessSegments=AmazonWebServicesSegment", P, 1)),
         "Both one-pair segment facts for the quarter.", tolerance=RATIO),
    task("sc_meta_foa_margin", "segment_composition", "simple", "heldout",
         f"What was the operating margin of {META}'s Family of Apps segment (segment operating income divided by segment "
         f"revenue) for the three months ended 2026-06-30, {src()}? Give it as a decimal rounded to 4 decimal places.",
         ratio(dv(META, "OperatingIncomeLoss", "BusinessSegments=FamilyOfApps", P, 1),
               dv(META, REV, "BusinessSegments=FamilyOfApps", P, 1)),
         "Segment revenue is the one-pair fact (60.370B), not its advertising part.", tolerance=RATIO),
    task("sc_nvidia_us_share", "segment_composition", "moderate", "heldout",
         f"What share of {NVDA}'s total revenue for the three months ended 2026-07-31 was attributed to the United States, "
         f"{src(period=20260731)}? Give it as a decimal rounded to 4 decimal places.",
         ratio(dv(NVDA, "Revenues", "Geographical=US", 20260731, 1, period=20260731),
               v(NVDA, "Revenues", 20260731, 1, period=20260731)),
         "US member over consolidated Revenues from num.", tolerance=RATIO),
    task("sc_apple_china_growth", "segment_composition", "moderate", "heldout",
         f"What was the growth rate of {APPLE}'s Greater China segment net sales for the three months ended 2026-06-30 over "
         f"the three months ended 2025-06-30, {src()}? Compute (current / prior) - 1 and give it as a decimal rounded to 4 "
         "decimal places.",
         f"SELECT ROUND({dv(APPLE, REV, 'BusinessSegments=GreaterChinaSegment;' + SEG, P, 1)} * 1.0 / "
         f"{dv(APPLE, REV, 'BusinessSegments=GreaterChinaSegment;' + SEG, 20250630, 1)} - 1, 4)",
         "Both quarters in one filing, qtrs = 1.", tolerance=RATIO),
    task("sc_apple_services_gross_margin", "segment_composition", "challenging", "heldout",
         f"What was the gross margin of {APPLE}'s services for the nine months ended 2026-06-30, defined as services net sales "
         f"minus services cost of sales, divided by services net sales, {src()}? Give it as a decimal rounded to 4 decimal "
         "places.",
         ratio(f"({dv(APPLE, REV, 'ProductOrService=Service', P, 3)} - "
               f"{dv(APPLE, 'CostOfGoodsAndServicesSold', 'ProductOrService=Service', P, 3)})",
               dv(APPLE, REV, "ProductOrService=Service", P, 3)),
         "Revenue and cost on member Service at qtrs = 3.", tolerance=RATIO),
    task("sc_msft_cloud_share", "segment_composition", "challenging", "heldout",
         f"What share of {MSFT}'s total revenue for its fiscal year ended 2026-06-30 was Microsoft Cloud revenue, "
         f"{src('10-K')}? Give it as a decimal rounded to 4 decimal places.",
         ratio(dv(MSFT, REV, "ProductsOrServicesSecondaryCategorization=MicrosoftCloud", P, 4, **K), v(MSFT, REV, P, 4, **K)),
         "The Microsoft Cloud is a member of axis ProductsOrServicesSecondaryCategorization, not a segment.", tolerance=RATIO),
    task("sc_walmart_intl_ecommerce", "segment_composition", "moderate", "heldout",
         f"What share of {WMT}'s Walmart International segment net sales for the three months ended 2026-07-31 came from "
         f"eCommerce, {src(period=20260731)}? Give it as a decimal rounded to 4 decimal places.",
         ratio(dv(WMT, REV, "BusinessSegments=WalmartInternational;MajorCustomers=ECommerce", 20260731, 1, period=20260731),
               dv(WMT, REV, "BusinessSegments=WalmartInternational", 20260731, 1, period=20260731)),
         "eCommerce sales are a member of axis MajorCustomers within the segment.", tolerance=RATIO),
    task("sc_cat_pe_intersegment_share", "segment_composition", "challenging", "heldout",
         f"What share of the total sales of {CAT}'s Power & Energy segment for the three months ended 2026-06-30 were sales to "
         f"other Caterpillar segments, {src()}? Give it as a positive decimal rounded to 4 decimal places.",
         ratio(f"ABS({dv(CAT, 'Revenues', 'BusinessSegments=PowerEnergy;ConsolidationItems=IntersegmentElimination', P, 1)})",
               dv(CAT, "Revenues", "BusinessSegments=PowerEnergy;" + SEG, P, 1)),
         "Intersegment sales (-1.443B, stored as an elimination) over the segment total including them (8.238B).",
         tolerance=RATIO),
    task("sc_lilly_mounjaro_us_share", "segment_composition", "moderate", "heldout",
         f"What share of {LLY}'s worldwide Mounjaro revenue for the three months ended 2026-06-30 came from the United States, "
         f"{src()}? Give it as a decimal rounded to 4 decimal places.",
         ratio(dv(LLY, "Revenues", "Geographical=US;ProductOrService=Mounjaro", P, 1),
               dv(LLY, "Revenues", "ProductOrService=Mounjaro", P, 1)),
         "The US fact has two pairs; the worldwide fact only the product.", tolerance=RATIO),
    task("sc_rtx_raytheon_margin", "segment_composition", "challenging", "heldout",
         f"What was the operating margin of {RTX}'s Raytheon segment for the three months ended 2026-06-30, defined as segment "
         f"operating profit divided by total segment sales including intersegment sales, {src()}? Give it as a decimal "
         "rounded to 4 decimal places.",
         ratio(dv(RTX, "OperatingIncomeLoss", "BusinessSegments=Raytheon;" + SEG, P, 1),
               dv(RTX, REV, "BusinessSegments=Raytheon;" + SEG, P, 1)),
         "Total segment sales (8.269B) carry ConsolidationItems=OperatingSegments; external sales are 8.220B.", tolerance=RATIO),
    task("sc_apple_americas_opinc_change", "segment_composition", "moderate", "heldout",
         f"By how much, in US dollars, did the operating income of {APPLE}'s Americas segment for the three months ended "
         f"2026-06-30 exceed that for the three months ended 2025-06-30, {src()}?",
         f"SELECT {dv(APPLE, 'OperatingIncomeLoss', 'BusinessSegments=AmericasSegment;' + SEG, P, 1)} - "
         f"{dv(APPLE, 'OperatingIncomeLoss', 'BusinessSegments=AmericasSegment;' + SEG, 20250630, 1)}",
         "Both quarters in one filing."),
    task("sc_jnj_medtech_us_growth", "segment_composition", "challenging", "heldout",
         f"What was the growth rate of {JNJ}'s MedTech segment sales in the United States for the six months ended 2026-06-30 "
         f"over the six months ended 2025-06-30, {src()}? Compute (current / prior) - 1 and give it as a decimal rounded to 4 "
         "decimal places.",
         f"SELECT ROUND({dv(JNJ, REV, 'BusinessSegments=MedTech;Geographical=US', P, 2)} * 1.0 / "
         f"{dv(JNJ, REV, 'BusinessSegments=MedTech;Geographical=US', 20250630, 2)} - 1, 4)",
         "Two-pair facts at qtrs = 2; the quarter pair (qtrs = 1) gives a different rate.", tolerance=RATIO),
]


def limitations(quarter: str) -> list[str]:
    return [
        f"{len(TASKS)} tasks in 11 scenario groups of 6 to 13 tasks; group-level uncertainty rests on 11 clusters.",
        f"Single quarter of data ({quarter}); one filing season, so period coverage is narrow.",
        f"Subset: the {TOP_N} filers with the largest reported Assets among 10-K, 10-Q and their amendments, plus the "
        f"{AMENDERS} largest other filers that filed a 10-K/A or 10-Q/A, without co-registrant copies (the 0.2.0 rule; "
        "the subset is unchanged).",
        "Standard taxonomy tags only: facts on company-extension tags are dropped, dimensional or not. Axes and members "
        "keep the filer's own names, since a dimensional fact is only reachable through them.",
        "Dimensional facts whose FSDS segments value is not a clean list of 'Axis=Member;' pairs (typed dimensions whose "
        "value contains '=', ';' or a line break, such as some investment identifiers) are dropped.",
        "dimensions.json labels axes and members from the SEC Financial Statement and Notes data sets (2026_07 to "
        "2026_09): a standard element gets the publisher's label by the definitions.json rule, a filer's element the "
        "filer's label. Names that resolve to no element (typed member values) have no label.",
        "Definitions and dimension labels are gated together by information_access 'definitions'; the schema-only baseline "
        "sees neither, only the table comments in schema.sql.",
        "A segment total can appear twice: alone and with ConsolidationItems=OperatingSegments, with equal or different "
        "values (external versus total sales). Every question names which one it wants.",
        "Several filers report a parent member and its children on the same axis (Product and iPhone, NonUs and Europe); "
        "summing every member of an axis double counts.",
        "Tasks are not fully independent: MICROSOFT CORP appears in 13 tasks and CISCO SYSTEMS, INC. in 9, and several "
        "segment tasks reuse one filing (Apple, Eli Lilly, Caterpillar).",
        "Four groups (lookup, period_compare, ratio, concept_composition; 32 tasks) draw from six 10-K filers only "
        "(Microsoft, Cisco, Cardinal Health, P&G, FedEx, ADP), all on fiscal-year facts, so each is close to six filings "
        "asked several ways; 87 tasks name a filer and only 20 distinct filers are named.",
        "Some segment facts exist only with a second ConsolidationItems=OperatingSegments pair and some without (Apple "
        "against Amazon, Meta, Microsoft); schema.sql says so, but a solver that assumes ndims = 1 for every filer fails "
        "the others.",
        "Ratio and growth answers are rounded to 4 decimal places and accepted within an absolute 0.0001; other numbers "
        "within a relative 1e-6.",
        "Unanswerable tasks have a null reference, reached only by an explicit abstention (the llm-sql adapter returns null "
        "only for a query result of exactly 'NOT_IN_DATABASE'); five of the nine ask for a breakdown the filer does not "
        "report.",
    ]
