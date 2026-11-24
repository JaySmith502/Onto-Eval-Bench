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
