"""Synthetic regressions for Responses argument completion without delta events."""
import json
import unittest
from reefy_llm_proxy.codex_translator import responses_sse_to_chat_sse, collect_chat_sse


def item(index, arguments=''):
    return {'type': 'function_call', 'id': f'item-{index}', 'call_id': f'call-{index}',
            'name': f'synthetic_{index}', 'arguments': arguments}


def added(index, arguments=''):
    return {'type': 'response.output_item.added', 'item': item(index, arguments)}


def done(index, arguments):
    return {'type': 'response.function_call_arguments.done', 'item_id': f'item-{index}',
            'arguments': arguments}


async def result(events, fragment=7):
    wire = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode()
    async def stream():
        for offset in range(0, len(wire), fragment):
            yield wire[offset:offset + fragment]
    return await collect_chat_sse(responses_sse_to_chat_sse(stream(), 'synthetic'))


class ToolArgumentsTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_done_only_arguments_survive_without_duplication(self):
        args = ['{"value":"alpha"}', '{"value":"beta"}']
        events = []
        for index, arguments in enumerate(args):
            events += [added(index), done(index, arguments),
                       {'type': 'response.output_item.done', 'item': item(index, arguments)}]
        events += [{'type': 'response.completed', 'response': {
            'output': [item(i, arg) for i, arg in enumerate(args)]}}]
        for fragment in [1, 7, 100000]:
            response = await result(events, fragment)
            choice = response['choices'][0]
            self.assertEqual(choice['finish_reason'], 'tool_calls')
            for i, call in enumerate(choice['message']['tool_calls']):
                self.assertEqual(call['id'], f'call-{i}')
                self.assertEqual(call['function'], {'name': f'synthetic_{i}', 'arguments': args[i]})

    async def test_partial_stream_completion_appends_only_missing_suffix(self):
        args = '{"value":"héllo"}'
        events = [added(0), {'type': 'response.function_call_arguments.delta',
                  'item_id': 'item-0', 'delta': args[:10]}, done(0, args),
                  {'type': 'response.completed', 'response': {'output': [item(0, args)]}}]
        response = await result(events)
        self.assertEqual(response['choices'][0]['message']['tool_calls'][0]['function']['arguments'], args)

    async def test_initial_and_terminal_arguments_are_preserved(self):
        for events in [
            [added(0, '{}'), done(0, '{}'), {'type': 'response.completed'}],
            [added(0), {'type': 'response.output_item.done', 'item': item(0, '{}')}, {'type': 'response.completed'}],
            [added(0), {'type': 'response.completed', 'response': {'output': [item(0, '{}')]}}],
        ]:
            response = await result(events)
            self.assertEqual(response['choices'][0]['message']['tool_calls'][0]['function']['arguments'], '{}')

    async def test_conflicting_or_unknown_completion_fails_closed(self):
        for events in [[added(0, '{"a":'), done(0, '{"b":1}')], [done(0, '{}')]]:
            with self.assertRaises(ValueError):
                await result(events + [{'type': 'response.completed'}])

    async def test_truncated_stream_is_not_success(self):
        with self.assertRaises(ValueError):
            await result([added(0), done(0, '{}')])
