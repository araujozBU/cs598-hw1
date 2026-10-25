"""Standard-library Chat Completions client with streaming and bounded retries."""

import json
import os
import sys
import time
import urllib.error
import urllib.request


class AgentError(Exception):
    pass


class ContextLimitError(AgentError):
    pass


def parse_completion(response):
    if 'text/event-stream' not in response.headers.get('Content-Type', ''):
        choice = json.load(response)['choices'][0]
        if choice.get('finish_reason') == 'length':
            raise AgentError('Model response exceeded its output token limit.')
        return choice['message']
    content, calls, finished = [], {}, False
    for line in response:
        line = line.decode('utf-8').strip()
        if not line.startswith('data:'):
            continue
        data = line[5:].strip()
        if data == '[DONE]':
            break
        event = json.loads(data)
        if event.get('error'):
            raise AgentError('Model endpoint reported a streaming error.')
        for choice in event.get('choices', []):
            if choice.get('index', 0) != 0:
                continue
            if choice.get('finish_reason') == 'length':
                raise AgentError('Model response exceeded its output token limit.')
            if choice.get('finish_reason'):
                finished = True
            delta = choice.get('delta', {})
            if delta.get('content'):
                content.append(delta['content'])
            for fragment in delta.get('tool_calls', []):
                index = fragment['index']
                call = calls.setdefault(index, {'id': '', 'type': 'function',
                                               'function': {'name': '', 'arguments': ''}})
                if fragment.get('id'):
                    call['id'] = fragment['id']
                for key in ('name', 'arguments'):
                    call['function'][key] += fragment.get('function', {}).get(key) or ''
    if not finished:
        raise AgentError('Model stream ended before completing its response.')
    result = {'role': 'assistant', 'content': ''.join(content) or None}
    if calls:
        result['tool_calls'] = [calls[index] for index in sorted(calls)]
    return result


def request_completion(messages, tools, deadline, temperature=0, tool_choice='auto'):
    key = os.environ.get('QWEN_API_KEY', '').strip()
    if not key:
        raise AgentError('QWEN_API_KEY is not set.')
    model = os.environ.get('MODEL', 'Qwen/Qwen3-14B')
    if model != 'Qwen/Qwen3-14B':
        raise AgentError('MODEL must be Qwen/Qwen3-14B.')
    body = {'model': model, 'messages': messages, 'tools': tools, 'tool_choice': tool_choice,
            'parallel_tool_calls': False,
            'temperature': temperature, 'top_p': 0.8 if temperature else 1.0,
            'max_tokens': 8192, 'stream': True,
            'chat_template_kwargs': {'enable_thinking': False}}
    url = os.environ.get('OPENAI_BASE_URL', 'https://llm.montii.me/v1').rstrip('/')
    request = urllib.request.Request(url + '/chat/completions', data=json.dumps(body).encode('utf-8'),
        headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json',
                 'Connection': 'close', 'Cache-Control': 'no-cache, no-store',
                 'User-Agent': 'cs598-homework/0.3'}, method='POST')
    for attempt in range(5):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AgentError('Task exceeded its time limit.')
        try:
            with urllib.request.urlopen(request, timeout=min(120, remaining)) as response:
                result = parse_completion(response)
            if not isinstance(result, dict):
                raise ValueError('Invalid message')
            return result
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read(2048).decode('utf-8', errors='replace').replace(key, '[redacted]')
            finally:
                exc.close()
            # Retry rejected requests only. Never replay a successful completion.
            transport_rejection = (exc.code == 400 and 'Bad request syntax' in detail
                                   and '<html' in detail.lower())
            if os.environ.get('AGENT_DEBUG') == '1':
                # Gateway error pages can echo another request's entire prompt.
                # Report the failure category without copying request contents.
                diagnostic = 'gateway HTTP framing rejection' if transport_rejection else detail
                print(f'Endpoint HTTP {exc.code}: {diagnostic}', file=sys.stderr)
            if exc.code == 400 and ('maximum context length' in detail.lower()
                                    or 'context_length_exceeded' in detail.lower()):
                raise ContextLimitError('Model context limit exceeded; shorten the request before retrying.') from None
            retry_limit = 4 if transport_rejection else 2
            if (exc.code in (429, 502, 503, 504) or transport_rejection) and attempt < retry_limit:
                try:
                    delay = min(30, max(2 * (attempt + 1), float(exc.headers.get('Retry-After', 0))))
                except (TypeError, ValueError):
                    delay = 2 * (attempt + 1)
                if time.monotonic() + delay + 5 < deadline:
                    time.sleep(delay)
                    continue
            if transport_rejection:
                raise AgentError('Model gateway rejected HTTP request framing (HTTP 400); '
                                 'bounded retries could not recover within the task deadline.') from None
            raise AgentError(f'Model endpoint returned HTTP {exc.code}.') from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise AgentError('Model endpoint could not be reached or timed out.') from None
        except (ValueError, KeyError, IndexError, TypeError):
            raise AgentError('Model endpoint returned an invalid response.') from None
