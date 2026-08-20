"""Did the table get the rows, and is the arithmetic right?

Programmatic all the way down: the deliverable is a database, so the check
opens it. Nothing here reads the agent's prose except the last assertion, and
that one is scored separately -- a correct database with a wrong summary is a
different failure from an empty database with a confident summary, and a
single boolean would hide which one happened.
"""

import sqlite3

EXPECTED = {
    1001: ("north", 3, 4.50, 13.50),
    1002: ("south", 10, 2.25, 22.50),
    1003: ("north", 1, 19.99, 19.99),
    1004: ("east", 7, 3.00, 21.00),
    1005: ("west", 2, 12.50, 25.00),
    1006: ("south", 5, 8.20, 41.00),
}
REVENUE = round(sum(row[3] for row in EXPECTED.values()), 2)


def check(bundle):
    from harness.bundle import fail, ok

    if not bundle.exists("out.db"):
        return fail("no out.db was produced",
                    drove_cleanly=bundle.drove_cleanly,
                    reason=bundle.outcome.get("reason"))

    try:
        connection = sqlite3.connect(bundle.path("out.db"))
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "select order_id, region, quantity, unit_price, total "
            "from sales").fetchall()
    except sqlite3.Error as e:
        return fail("out.db is not readable as expected: " + str(e))
    finally:
        try:
            connection.close()
        except Exception:                                       # noqa: BLE001
            pass

    if len(rows) != len(EXPECTED):
        return fail("expected %d rows, found %d" % (len(EXPECTED), len(rows)))

    wrong = []
    for row in rows:
        want = EXPECTED.get(int(row["order_id"]))
        if want is None:
            wrong.append("unknown order_id %s" % row["order_id"])
            continue
        if str(row["region"]) != want[0] or int(row["quantity"]) != want[1]:
            wrong.append("row %s does not match the csv" % row["order_id"])
        elif abs(float(row["total"]) - want[3]) > 0.005:
            wrong.append("row %s total is %s, expected %s"
                         % (row["order_id"], row["total"], want[3]))
    if wrong:
        return fail("; ".join(wrong[:4]), score=0.5)

    # Scored beside the real result rather than as part of it: the number in
    # the reply is a claim about the work, and the work is the database.
    said_revenue = any(form in bundle.final_text
                       for form in ("142.99", "142.990", "$142.99"))
    return ok("table correct" + ("" if said_revenue
                                 else "; reply did not state the revenue"),
              score=1.0,
              reported_revenue=said_revenue,
              expected_revenue=REVENUE,
              script_runs=bundle.tool_calls("run_script"),
              shell_runs=bundle.tool_calls("run_command"))
