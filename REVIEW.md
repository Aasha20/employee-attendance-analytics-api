# REVIEW.md

## Review of the original starter implementation

The original `app/main.py` was reviewed against the assignment requirements, API contract, and data model.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | The helper assumes the punch-in and shift start belong to the same calendar day. It does not correctly model overnight shifts. | Test a punch-in belonging to an overnight shift. | Updated attendance calculations to account for shift dates and the applicable shift start. |
| 2 | `compute_late_minutes` | The helper uses a naive datetime and derives lateness by replacing the punch-in time's hour and minute. This is not a robust timezone-aware shift calculation. | Test timestamps around midnight and shifts in IST. | Use the assignment's IST shift-date rules and calculate elapsed minutes from the correct shift start. |
| 3 | `compute_work_hours` | Python's built-in `round` does not guarantee the required ROUND_HALF_UP rounding rule. | Test work durations near a rounding boundary. | Use the specified rounding rule and return work hours to two decimal places. |
| 4 | `compute_overtime` | The helper does not handle overnight shifts correctly because it builds the shift end on the supplied calendar date. | Test a shift that ends after midnight. | Calculate the correct shift end date before determining overtime. |
| 5 | `compute_overtime` | The helper returns overtime as soon as the punch-out exceeds shift end; it does not enforce the required 30-minute eligibility threshold. | Test punch-outs 10, 29, and 30 minutes after shift end. | Apply the 30-minute eligibility rule before recording overtime. |
| 6 | `create_employee` / `EmployeeIn` | The input model has no explicit contract validation for employee-code format, date format, shift-time format, or email. | Submit malformed employee codes, dates, or shift times. | Add input validation and return validation errors for invalid data. |
| 7 | `create_employee` | The duplicate check is performed before insertion, so concurrent requests can still attempt to create the same employee. | Send concurrent requests with the same `emp_code`. | Enforce uniqueness with a MongoDB unique index and handle duplicate-key errors. |
| 8 | `create_employee` | `created_at` uses `datetime.now()`, which creates a naive local datetime rather than an explicit UTC timestamp. | Inspect stored timestamps and compare environments with different timezones. | Store timestamps consistently in UTC. |
| 9 | `list_employees` | `skip = page * page_size` skips the first page of records when `page=1`. | Request page 1 and compare its records with the collection's first records. | Use `(page - 1) * page_size` and validate pagination inputs. |
| 10 | `list_employees` | `total` counts all employees rather than employees matching the department filter. | Filter by a department and compare `total` with the number of matching employees. | Calculate the count using the same query filter as the item retrieval. |
| 11 | `list_employees` | Pagination parameters have no explicit upper bound for `page_size`. | Request a page size greater than the contract maximum. | Validate pagination and enforce the maximum page size. |
| 12 | `health` | The endpoint always returns `ok` without checking MongoDB connectivity. | Stop MongoDB and call `/health`. | Check database connectivity and report an unhealthy dependency when the check fails. |
| 13 | `punch_in` | The code does not reject an unknown employee before accessing the employee's shift settings. | Punch in using an unknown `emp_code`. | Validate employee existence and return an appropriate HTTP error. |
| 14 | `punch_in` / `PunchInIn` | The endpoint accepts arbitrary status strings and does not enforce the allowed punch-in statuses. | Submit `ABSENT`, `LEAVE`, or an unknown status. | Validate the status enum and permit punch-in only for eligible presence statuses. |
| 15 | `punch_in` | `datetime.fromtimestamp()` uses the machine's local timezone, and a falsy timestamp such as zero is treated as if no timestamp was supplied. | Submit epoch milliseconds around midnight or submit zero. | Validate epoch-millisecond input and convert it explicitly using the contract's timezone rules. |
| 16 | `punch_in` | The attendance duplicate check and insert are separate operations and can race under concurrent requests. | Send concurrent punch-in requests for the same employee and date. | Enforce the `(emp_code, date)` natural key with a unique index and handle duplicate-key errors. |
| 17 | `list_attendance` | The endpoint loads all matching attendance records into Python and sorts them in memory before pagination. | Test with a large attendance collection and inspect query performance. | Apply filtering, sorting, counting, and pagination in MongoDB, supported by suitable indexes. |
| 18 | `list_attendance` | The endpoint does not explicitly validate date filters, status values, or pagination bounds. | Submit invalid dates, an unknown status, or an excessive page size. | Add contract validation and return appropriate validation errors. |
| 19 | `list_attendance` | The response exposes BSON datetimes through default serialization rather than explicitly enforcing the required epoch-millisecond representation. | Inspect the JSON returned for punch timestamps. | Serialize API timestamps according to the specified epoch-millisecond format. |
| 20 | Starter endpoint set | The original file does not implement punch-out, attendance correction with audit history, analytics, department trend, or the explain endpoint. | Compare the registered routes with `openapi.yaml`. | Implement the missing routes and test their responses against the API contract. |

## Items reviewed that require context

- A duplicate check in application code is useful for a friendly error message, but it is not a substitute for a database unique index.
- A collection scan is not automatically a defect for an unfiltered explain query. The appropriate query plan depends on the actual filter, sort, and data volume.
- Some defects only appear for boundary cases, invalid inputs, concurrency, or larger datasets; a successful happy-path request does not establish correctness.
