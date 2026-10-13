# DECISIONS.md

1. **Indexes.** I use a unique index on `employees.emp_code` so duplicate employee codes cannot race, and a unique `(emp_code, date)` index so only one attendance record exists per employee per attendance day. I also index department/join date and common attendance date, status, and employee/date access patterns. For a much larger workload, I would inspect real explain plans and tune compound index order to match the most common filters and sorts.

2. **Punch-in race.** Both requests can pass the employee lookup, but the unique attendance index is the final authority. MongoDB accepts one insert; the other raises `DuplicateKeyError`, which the API maps to 409. A preliminary read is not enough to guarantee correctness.

3. **Ties.** The leaderboard uses MongoDB's rank window function, so equal totals share a rank and the next rank is skipped. Filtering by `rank <= limit` after ranking means a tie at the cutoff returns every employee tied at that rank. Rows are then ordered by total minutes and employee code.

4. **Headcount.** The department summary starts from employees and looks up logs. This preserves employees with no attendance records and filters out employees whose `joined_on` is after the month's end.

5. **100x data.** I would add automated integration/load tests, profile actual plans, and consider pre-aggregated daily summaries for analytics while retaining the raw logs as the source of truth.
