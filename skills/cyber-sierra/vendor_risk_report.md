# Vendor Risk Report

*Generated from live Cyber Sierra TPRM API data.*

## Summary
This tenant currently has **1 vendor (assessee) on file** and **0 active third-party risk assessments** returned by the assessments API at this time. Organization-level risk scoring thresholds are **not yet configured** (all risk band min/max values are 0), and the assessment dashboard endpoint is currently inaccessible due to a permission restriction.

## Vendors (Assessees) on File

| Company | Country | Point of Contact | Contact Email | Status | Assessment Sent? |
|---------|---------|-------------------|----------------|--------|-------------------|
| Test assesse | India | Mathew | mathew.v@keyvalue.systems | Added | Yes |

## Assessments

No assessments were returned by the live assessments API (`tprm assessments list`) at the time of this report, despite the vendor above being flagged as having had an assessment sent (`isAssessmentSent: true`). This could mean the assessment has since been completed/archived, or that it falls outside the current query's visibility. Recommend verifying directly in the Cyber Sierra dashboard if a specific assessment ID is expected.

## Risk Scoring Configuration

Org-level risk scoring (`tprm risk-score-config get-org`) is present but **not yet calibrated**:
- Question compliance score buckets (compliant, non-compliant, partially compliant, etc.) are all set to 0.
- Risk level bands (satisfactory / inadequate / unsatisfactory) all have min = 0, max = 0.

**No vendors currently fall into a meaningfully differentiated risk tier** because these thresholds haven't been set. This should be addressed before the board report can convey a real risk distribution.

## Data Not Available

- **Assessment dashboard summary** (`tprm dashboard get`) could not be retrieved — the API returned a **Permission Error**. This looks like an access/role restriction on this endpoint rather than an authentication problem; report to your Cyber Sierra admin if dashboard-level metrics are needed.

## Next Steps
- Confirm with the platform whether the previously-tracked assessment (vendor: Test assesse) is complete, withdrawn, or simply not visible via this query.
- Calibrate org-level risk score bands so vendors are classified into meaningful risk tiers.
- Add more vendors and send assessments to build out a broader risk picture.
- Request dashboard/report permissions if aggregate risk metrics are needed going forward.
- **Recurring delivery to the board**: the Cyber Sierra CLI has no built-in scheduler or email-send action for reports. To make this monthly, either (a) configure a recurring report/notification inside the Cyber Sierra web app if that feature exists there, or (b) have this report regenerated on a monthly cadence (e.g., a calendar reminder to re-run this request) and forward it to the board manually.

---
*This report reflects live data pulled directly from the Cyber Sierra TPRM API. It supersedes the previous version of this file.*
