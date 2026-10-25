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
