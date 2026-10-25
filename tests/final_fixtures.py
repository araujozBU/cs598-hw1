"""Synthetic final-checkpoint inputs and known answers, not hidden grader data."""

import csv
from pathlib import Path


def programming_fixture(root):
    prompt = (Path(__file__).parent / 'fixtures/programming_task.md').read_text()
    with (root / 'transactions.csv').open('w', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['date', 'description', 'category', 'amount'])
        for _ in range(10000):
            writer.writerows([
                ['2026-09-01', 'Lunch, with tea', 'Food', '10.25'],
                ['2026-09-02', 'Refund', 'FOOD', '-0.25'],
                ['2026-09-03', 'Train', 'Travel', '10.00'],
                ['2026-09-04', 'Book', 'Books', '5.00'],
            ])
    return prompt


def debugging_fixture(root, alternate=False):
    # Six public fields, plus SIX LOCALLY DEFINED fields to test exact schema and
    # integers. These extra fields are not claimed to be the hidden grading keys.
    order = 'ORD-88120' if alternate else 'ORD-77341'
    expected = {
        'gateway_request_id': 'gw-2904' if alternate else 'gw-8417',
        'upstream_trace_id': 'tr-cc44dd55' if alternate else 'tr-aa11bb22',
        'hold_id': 'hold-1729' if alternate else 'hold-6543',
        'worker': 'inv-w3' if alternate else 'inv-w7',
        'root_cause_component': 'ReservationIndex' if alternate else 'LeaseRegistry',
        'payment_intent_id': 'pi_test_alt314' if alternate else 'pi_test_main271',
        'timeout_ms': 3000, 'retry_count': 3,
        'lock_path': '/var/locks/inventory-reservations.lock',
        'error_code': 'LOCK_OWNER_GONE',
        'root_cause_timestamp': '2026-09-17T14:32:01.250Z',
        'queue_depth': 17,
    }
    e = expected
    logs = root / 'logs'
    logs.mkdir()
    # Thousands of unrelated lines and prominent false leads in every service.
    for service in ('gateway', 'orders', 'inventory', 'payments'):
        lines = [f'2026-09-17T14:30:00Z INFO service={service} request=background-{i} status=ok unrelated=true\n'
                 for i in range(3000)]
        lines.insert(900, f'2026-09-17T14:32:00Z ERROR service={service} order=ORD-DECOY component=PaymentTransport message=unrelated_failure\n')
        (logs / (service + '.log')).write_text(''.join(lines))
    evidence = {
        'gateway': [
            f'2026-09-17T14:31:59Z INFO req=gw-success method=POST path=/api/checkout order={order} status=200 upstream_trace=tr-success',
            f'2026-09-17T14:32:04Z ERROR req={e["gateway_request_id"]} method=POST path=/api/checkout order={order} status=502',
            f'2026-09-17T14:32:04Z WARN req={e["gateway_request_id"]} upstream_trace={e["upstream_trace_id"]} upstream_status=504',
        ],
        'orders': [
            f'2026-09-17T14:32:00Z INFO trace={e["upstream_trace_id"]} payment_intent={e["payment_intent_id"]} payment_authorized=true',
            f'2026-09-17T14:32:04Z ERROR trace={e["upstream_trace_id"]} waiting_for_hold={e["hold_id"]} timeout_ms=3000 retry_count=3',
        ],
        'inventory': [
            f'2026-09-17T14:32:00Z INFO hold={e["hold_id"]} queued_on={e["worker"]} queue_depth=17',
            f'{e["root_cause_timestamp"]} ERROR worker={e["worker"]} hold={e["hold_id"]} component={e["root_cause_component"]} code=LOCK_OWNER_GONE lock_path={e["lock_path"]} message=hold_failed_because_lock_owner_exited',
            '2026-09-17T14:32:01Z ERROR worker=inv-w9 hold=hold-unrelated component=StockCache code=OUT_OF_STOCK',
        ],
        'payments': [
            f'2026-09-17T14:32:00Z INFO intent={e["payment_intent_id"]} status=authorized amount=89.00',
            '2026-09-17T14:32:01Z ERROR intent=pi_unrelated status=failed component=PaymentTransport',
        ],
    }
    for service, rows in evidence.items():
        path = logs / (service + '.log')
        lines = path.read_text().splitlines(True)
        for index, row in enumerate(rows):
            lines.insert(1400 + index * 250, row + '\n')
        path.write_text(''.join(lines))
    prompt = f'''CONSTRAINT: Write REPORT.json in the current directory, containing one JSON object with exactly the 12 keys below.
Incident: checkout failure for order {order} at 2026-09-17 14:32 UTC. Browser received HTTP 502 Bad Gateway (POST /api/checkout).
Logs are logs/gateway.log, logs/orders.log, logs/inventory.log, logs/payments.log.
Follow this specific failed request across services using the linked identifiers. There are unrelated errors and a successful request for the same order.
Copy values exactly from log evidence. Use strings except timeout_ms, retry_count, and queue_depth, which must be JSON integers.
Required keys:
1. gateway_request_id: req= of the failed checkout for this order.
2. upstream_trace_id: upstream trace recorded by that gateway request.
3. hold_id: hold the orders service was waiting on for that trace.
4. worker: inventory worker that hold was queued on.
5. root_cause_component: software class named as the cause, not the worker or service name.
6. payment_intent_id: payment intent associated with the failed checkout trace.
The remaining six keys are local test extensions, not the unreleased grading requirements:
7. timeout_ms: timeout in the orders error.
8. retry_count: retries in the orders error.
9. lock_path: lock path from the causal inventory error.
10. error_code: code from that same causal error.
11. root_cause_timestamp: exact timestamp of that causal error.
12. queue_depth: queue depth when that hold was queued.
Preserve all inputs. Write REPORT.json before finishing.
'''
    return prompt, expected


ALTERNATIVE_PROGRAM = '''Implement windows.py in the current directory using Python 3's standard library.
Invocation: python3 windows.py K
Read one JSON array of integers from stdin. Print a compact JSON array of the sums of every consecutive window of K items.
K must be a positive integer. If K exceeds the input length, print [].
On invalid K or invalid JSON/non-integer array, exit nonzero and print a useful error to stderr.
Use an O(n) algorithm. Preserve input files. Test the program before finishing.
'''


MERGE_INTERVALS_PROGRAM = '''Implement merge_intervals.py in the current directory using Python 3's standard library.
This is a standalone programming practice task; no data file is supplied.

Invocation: python3 merge_intervals.py
Read one JSON array of closed intervals from stdin. Each interval must be a two-element
array [start, end] of JSON integers with start <= end. Booleans, floats, strings,
missing endpoints, and extra endpoints are invalid.

Merge intervals that overlap, including intervals sharing an endpoint. For example,
[[1,3],[3,5],[8,9]] becomes [[1,5],[8,9]], while [[1,2],[3,4]] stays separate.
Print the merged intervals sorted by start as one compact JSON array followed by a
newline, with no other stdout. An empty input array prints [].

On invalid JSON or invalid intervals, exit nonzero, print a useful error to stderr,
and print nothing to stdout. Use an O(n log n) algorithm or better. Preserve all
unrelated files. Test the saved program before finishing.
'''


PRODUCT_EXCEPT_SELF_PROGRAM = '''Implement product_except_self.py in the current directory using Python 3's standard library.
This is a standalone programming practice task; no data file is supplied.

Invocation: python3 product_except_self.py
Read one JSON array of integers from stdin. For each index, output the product of all
other elements. Do not use division. An empty array prints []; a one-element array
prints [1]. Zeros and negative integers are valid. Use O(n) time apart from
integer multiplication costs. Print one compact JSON array followed by a newline,
with no other stdout.

On invalid JSON or a non-integer array, including booleans, floats, and strings,
exit nonzero, print a useful error to stderr, and print nothing to stdout.
Preserve unrelated files. Test the saved program before finishing.
'''


LONGEST_UNIQUE_SUBSTRING_PROGRAM = '''Implement longest_unique_substring.py in the current directory using Python 3's standard library.
This is a standalone programming practice task; no data file is supplied.

Invocation: python3 longest_unique_substring.py
Read one JSON string from stdin. Print the length of its longest contiguous
substring containing no repeated Unicode code points, followed by a newline.
Treat characters exactly as decoded by json.loads: do not normalize Unicode or
change case. An empty string prints 0. Use an O(n) sliding-window algorithm.

On invalid JSON or a JSON value that is not a string, exit nonzero, print a
useful error to stderr, and print nothing to stdout. Preserve unrelated files.
Test the saved program before finishing.
'''


def import_pipeline_debugging_fixture(root):
    """A failed batch import with a successful storage branch and decoy attempts."""
    expected = {
        'api_request_id': 'api-7142',
        'trace_id': 'trace-68af',
        'task_id': 'task-2057',
        'attempt_id': 'att-9f21',
        'worker': 'etl-w4',
        'root_cause_component': 'SchemaRegistry',
        'error_code': 'SCHEMA_VERSION_MISSING',
        'schema_version': 'v7',
        'root_cause_timestamp': '2026-10-02T10:15:02.125Z',
        'retry_delay_ms': 1500,
        'storage_batch_id': 'batch-882',
        'storage_status': 'committed',
    }
    logs = root / 'logs'
    logs.mkdir()
    evidence = {
        'api': [
            '2026-10-02T10:14:00Z INFO req=api-success method=POST path=/api/import job=job-4921 status=200 trace=trace-old',
            '2026-10-02T10:15:04Z ERROR req=api-7142 method=POST path=/api/import job=job-4921 status=500',
            '2026-10-02T10:15:04Z WARN req=api-7142 trace=trace-68af upstream_status=500',
            '2026-10-02T10:15:04Z ERROR req=api-decoy method=POST path=/api/import job=job-other status=500 trace=trace-decoy',
        ],
        'scheduler': [
            '2026-10-02T10:14:00Z INFO trace=trace-old task=task-old worker=etl-w2 result=complete',
            '2026-10-02T10:15:00Z INFO trace=trace-68af storage_batch=batch-882',
            '2026-10-02T10:15:01Z INFO trace=trace-68af task=task-2057 attempt=att-9f21 worker=etl-w4',
            '2026-10-02T10:15:04Z ERROR trace=trace-68af task=task-2057 attempt=att-9f21 result=failed retry_delay_ms=1500',
            '2026-10-02T10:15:04Z ERROR trace=trace-decoy task=task-decoy attempt=att-decoy result=failed retry_delay_ms=9000',
        ],
        'transform': [
            '2026-10-02T10:14:00Z ERROR worker=etl-w4 task=task-old attempt=att-old component=DiskWriter code=DISK_FULL',
            '2026-10-02T10:15:01Z INFO worker=etl-w4 task=task-2057 attempt=att-9f21 schema_version=v7',
            '2026-10-02T10:15:02.125Z ERROR worker=etl-w4 task=task-2057 attempt=att-9f21 component=SchemaRegistry code=SCHEMA_VERSION_MISSING schema_version=v7',
            '2026-10-02T10:15:03Z ERROR worker=etl-w8 task=task-decoy attempt=att-decoy component=Parser code=BAD_RECORD',
        ],
        'storage': [
            '2026-10-02T10:14:01Z INFO batch=batch-old status=committed',
            '2026-10-02T10:15:02Z INFO batch=batch-882 status=committed rows=1200',
            '2026-10-02T10:15:03Z ERROR batch=batch-decoy status=failed code=WRITE_TIMEOUT',
        ],
    }
    for service, rows in evidence.items():
        lines = [f'2026-10-02T10:13:00Z INFO service={service} background={i} status=ok\n'
                 for i in range(1200)]
        for index, row in enumerate(rows):
            lines.insert(200 + index * 180, row + '\n')
        (logs / (service + '.log')).write_text(''.join(lines))
    prompt = '''Incident: the batch import for job job-4921 failed at 2026-10-02 10:15 UTC.
The client received HTTP 500 for POST /api/import. Investigate logs/api.log,
logs/scheduler.log, logs/transform.log, and logs/storage.log. An earlier import
for the same job succeeded; other errors and attempts are unrelated. Follow
the failed request's linked identifiers and distinguish the failed transform
from the storage branch. Write REPORT.json in the current directory as one
JSON object with exactly these keys and value types:
1. api_request_id: req= for the failed import request (string).
2. trace_id: trace linked to that request (string).
3. task_id: task linked to that trace's failed attempt (string).
4. attempt_id: attempt linked to that task and trace (string).
5. worker: worker for that failed attempt (string).
6. root_cause_component: class named by the causal transform error (string).
7. error_code: code from that same transform error (string).
8. schema_version: version from that same transform error (string).
9. root_cause_timestamp: exact timestamp of that error (string).
10. retry_delay_ms: retry delay from the scheduler error (JSON integer).
11. storage_batch_id: batch linked to the failed trace (string).
12. storage_status: status for that batch in the storage log (string).
Copy values exactly from the evidence, preserve all inputs, and write the file
before finishing.
'''
    return prompt, expected


def chained_debugging_fixture(root):
    """Require service links, paginated evidence, and time/dispatch disambiguation."""
    prompt, expected = debugging_fixture(root, alternate=True)
    expected.update(timeout_ms=4200, retry_count=2,
                    lock_path='/srv/inventory/locks/reservations.lock',
                    error_code='STALE_LEASE', root_cause_timestamp='2026-09-17T14:32:02.125Z')
    del expected['queue_depth']
    expected['payment_status'] = 'authorized'
    e = expected
    evidence = {
        'gateway': [
            '2026-09-17T14:31:00Z INFO req=gw-ok method=POST path=/api/checkout order=ORD-88120 status=200 upstream_trace=tr-ok',
            '2026-09-17T14:32:04Z ERROR req=gw-other method=POST path=/api/checkout order=ORD-OTHER status=502 upstream_trace=tr-other',
            f'2026-09-17T14:32:04Z ERROR req={e["gateway_request_id"]} method=POST path=/api/checkout order=ORD-88120 status=502',
            *[f'2026-09-17T14:32:04Z INFO req={e["gateway_request_id"]} diagnostic_step={i}' for i in range(35)],
            f'2026-09-17T14:32:04Z WARN req={e["gateway_request_id"]} upstream_trace={e["upstream_trace_id"]} upstream_status=504',
        ],
        'orders': [
            '2026-09-17T14:31:00Z INFO trace=tr-ok payment_intent=pi_ok waiting_for_hold=hold-ok',
            '2026-09-17T14:32:04Z ERROR trace=tr-other waiting_for_hold=hold-other payment_intent=pi_other timeout_ms=9000 retry_count=8',
            f'2026-09-17T14:32:00Z INFO trace={e["upstream_trace_id"]} payment_intent={e["payment_intent_id"]}',
            f'2026-09-17T14:32:04Z ERROR trace={e["upstream_trace_id"]} waiting_for_hold={e["hold_id"]} timeout_ms=4200 retry_count=2',
        ],
        'inventory': [
            '2026-09-17T14:31:00Z INFO hold=hold-ok queued_on=inv-w1 dispatch=job-ok result=reserved',
            '2026-09-17T14:32:00Z INFO hold=hold-other queued_on=inv-w8 dispatch=job-other',
            '2026-09-17T14:32:01Z ERROR worker=inv-w8 dispatch=job-other component=StockCache code=OUT_OF_STOCK',
            f'2026-09-17T14:30:00Z ERROR worker={e["worker"]} dispatch=job-old component=OldCache code=EXPIRED',
            f'2026-09-17T14:32:00Z INFO hold={e["hold_id"]} queued_on={e["worker"]} dispatch=job-target',
            f'{e["root_cause_timestamp"]} ERROR worker={e["worker"]} dispatch=job-target component={e["root_cause_component"]} code={e["error_code"]} lock_path={e["lock_path"]} message=reservation_failed_because_lease_owner_exited',
            f'2026-09-17T14:35:00Z ERROR worker={e["worker"]} dispatch=job-later component=LaterCache code=DISCONNECTED',
        ],
        'payments': [
            '2026-09-17T14:31:00Z INFO intent=pi_ok status=authorized',
            '2026-09-17T14:32:00Z ERROR intent=pi_other status=failed component=PaymentTransport',
            f'2026-09-17T14:32:00Z INFO intent={e["payment_intent_id"]} status=authorized',
        ],
    }
    for service, rows in evidence.items():
        lines = [f'2026-09-17T14:29:00Z INFO service={service} request=background-{i} status=ok\n' for i in range(800)]
        # Spread evidence so a small context window cannot expose the whole chain.
        for i, row in enumerate(rows):
            lines.insert(100 + i * 15, row + '\n')
        (root / 'logs' / (service + '.log')).write_text(''.join(lines))
    prompt = prompt.replace('except timeout_ms, retry_count, and queue_depth', 'except timeout_ms and retry_count')
    prompt = prompt.replace('12. queue_depth: queue depth when that hold was queued.',
                            '12. payment_status: status from the payments service for the linked payment intent.')
    return prompt, expected
