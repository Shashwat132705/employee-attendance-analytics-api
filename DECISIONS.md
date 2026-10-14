# DECISIONS.md

1. **Indexes.** I created indexes to support attendance date and employee-based queries, filtering, and sorting. The date-and-employee compound index helps attendance queries avoid unnecessary scans. I would consider an index on department and joining date for headcount queries, but would add it only after checking query plans and write overhead.

2. **Punch-in race.** Two identical punch-in requests can arrive together. The code must rely on a database-enforced unique key or an atomic conditional write, not just a Python check before inserting. The first request should succeed; the second should receive a duplicate/conflict response. This guarantee depends on the actual write operation and unique index.

3. **Ties.** The leaderboard sorts by late minutes and uses a limit. If employees tie at the cutoff, a simple limit may exclude one tied employee. A secondary sort makes ordering deterministic, but including every cutoff tie requires extra logic.

4. **Headcount.** Department headcount must come from employee records, not attendance logs alone. Counting eligible employees by department and joining date includes employees who have no attendance logs.

5. **100x data.** I would benchmark the main endpoints with realistic data, inspect execution plans, and tune indexes and aggregation pipelines. I would also consider pagination and precomputed daily analytics if repeated aggregations became expensive.