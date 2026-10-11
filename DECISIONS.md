# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

# DECISIONS.md

**1. Indexes.** I created a unique index on `employees.emp_code` to prevent duplicate employee codes and a unique compound index on `(emp_code, date)` to allow one attendance record per employee per day. Additional indexes on `(date, emp_code)`, `(emp_code, punch_in)`, and `(department, emp_code)` support attendance listing, employee punch history, and department queries. I also added `(department, joined_on)` and `(joined_on)` indexes to support department headcount and employee date-range queries. I considered a status index but rejected it to avoid extra maintenance without proven benefit.

**2. Punch-in race.** If two identical punch-in requests arrive together, both may pass initial checks, but the unique attendance index permits only one insert. The other raises `DuplicateKeyError`, which the API converts to HTTP 409.

**3. Ties.** The leaderboard sorts by late minutes descending and employee code ascending, then applies the limit. A tie at the cutoff may be split, so all tied employees are not guaranteed to appear.

**4. Headcount.** The department summary starts from employees and looks up attendance logs. Employees without logs remain included in headcount.

**5. At 100x scale.** I would profile slow queries, review indexes using execution statistics, and consider pre-aggregating frequently requested analytics.