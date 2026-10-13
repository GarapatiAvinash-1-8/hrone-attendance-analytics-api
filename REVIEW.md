# REVIEW.md

The starter contains these defects; the replacement implementation addresses them as noted.

| # | Where | What is wrong | How to notice | Fix |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Uses the punch-in's current date/offset without explicitly converting to IST and does not correctly handle overnight attendance dates. | Test an overnight employee punching in after midnight. | Convert instants to IST and calculate against the shift start on the attendance date. |
| 2 | `compute_late_minutes` | Grace-period logic is easy to get wrong at the exact 10-minute boundary and fractional seconds. | Test 09:40:00, 09:40:01 and 10:15:59 for a 09:30 shift. | Truncate instants to seconds and apply the strict `> 600 seconds` rule. |
| 3 | `compute_work_hours` | Python `round` uses ties-to-even, not required decimal half-up rounding. | Use a duration exactly halfway between two hundredths. | Use `Decimal` with `ROUND_HALF_UP`. |
| 4 | `compute_overtime` | Always places shift end on the attendance date, which is wrong for overnight shifts. | Punch out at 06:45 for a 22:00–06:00 shift. | Move overnight shift end to the next local calendar day and apply the 30-minute threshold. |
| 5 | `/health` | Always returns success without checking MongoDB connectivity. | Stop MongoDB and call `/health`. | Run a MongoDB ping and return 503 on failure. |
| 6 | `create_employee` | Duplicate check followed by insert is race-prone; created time is naive and the response may expose inconsistent timestamp representation. | Send concurrent creates with the same `emp_code`. | Enforce a unique index and handle `DuplicateKeyError`; serialize instants as epoch milliseconds. |
| 7 | `list_employees` | `skip = page * page_size` skips page 1, and total ignores the department filter. | Request page 1 and filter by department. | Use `(page - 1) * page_size` and count the filtered query. |
| 8 | `punch_in` | Does not reject an unknown employee before dereferencing it; uses local machine time and seconds instead of epoch milliseconds; doesn't validate status; and check-then-insert is race-prone. | Unknown code, invalid status, or concurrent identical requests. | Validate employee/status/time, use UTC and unique `(emp_code,date)` index. |
| 9 | `punch_in` | Uses the local calendar date instead of the IST attendance date, including the overnight-shift exception. | Punch around midnight in IST on an overnight shift. | Calculate the attendance date from the employee's shift and IST. |
| 10 | `list_attendance` | Loads the entire matching collection into Python and sorts in memory. | Run against 100,000+ records. | Count separately and let MongoDB filter, sort and paginate using indexes. |
| 11 | `list_attendance` | Omits validation for date ranges and page bounds, and returns raw BSON datetimes/ObjectIds inconsistently. | Invalid dates, reversed date range, or page 0. | Validate query inputs and serialize timestamps to epoch milliseconds. |
| 12 | Missing punch-out endpoint | Punch-out business rules and atomic conflict handling are absent. | Call the contract endpoint or send concurrent punch-outs. | Added atomic conditional update, interval checks and derived-field computation. |
| 13 | Missing regularization endpoint | No audit trail, field-diff logic, derived-field recomputation or concurrent-edit protection. | Patch a record twice concurrently. | Added compare-and-swap update and append-only history entries. |
| 14 | Missing analytics endpoints | Required monthly, department, leaderboard and trend analytics are absent. | Call any analytics route. | Added aggregation-based endpoints and documented their pipelines. |
| 15 | Missing explain endpoint/index setup | No index initialization or query-plan endpoint exists. | Inspect query plans or call `/admin/explain/...`. | Added idempotent indexes and an explain route. |

**Reviewed and not treated as defects:** `emp_code` is intentionally the public identifier, not MongoDB `_id`; date strings are intentional calendar values; and `history` is intended for manual corrections only, not normal punch events.
