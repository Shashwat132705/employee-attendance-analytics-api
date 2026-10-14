# REVIEW.md

## Defects Found in the Starter Implementation

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `/admin/explain/{endpoint}` — query parameter handling | Date filters can be read incorrectly if the parameter names or aliases do not match the API contract. | Call the endpoint with the documented date query parameters and verify that the intended date range is applied. | Aligned the function parameters and aliases with the API contract and tested the endpoint in Swagger UI. |
| 2 | `department_trend` — aggregation pipeline | A trend query must calculate department-level attendance metrics over the requested date range, rather than return incomplete or incorrectly grouped results. | Query a known department over a date range containing attendance records and compare the results with the sample data. | Implemented an aggregation pipeline that joins employee records, groups attendance by date, calculates the metrics, and returns ordered daily results. |
| 3 | `/admin/explain/{endpoint}` — department trend branch | An explain endpoint is not useful if it explains a simplified or different query from the one used by the corresponding analytics endpoint. | Call the explain endpoint for `department_trend` and inspect the reported aggregation pipeline and execution statistics. | Added an explain command for the corresponding aggregation pipeline with execution statistics. |

## Reviewed but Not Classified as Defects

- **Weekend attendance rates:** A `null` attendance rate on a non-working day is intentional when the metric is defined for working days only.
- **Half-day attendance:** A half-day record contributing `0.5` to present days is intentional and should not be counted as a full day.
- **Date-range validation:** Rejecting an end date before the start date, or a range longer than the permitted 92 days, is intentional validation.
- **Pagination limits:** Restricting page size to a defined maximum is intentional protection against unnecessarily large responses.

## Verification Notes

The API was started locally and the Swagger UI was accessible. The monthly attendance endpoint, department summary, late-arrival leaderboard, department trend, and explain endpoints were exercised with sample data. These checks confirm that the tested requests returned responses; they do not by themselves prove that every business rule or edge case is correct.

The review should be updated if further testing identifies additional defects or if any listed issue does not match the original starter implementation.
