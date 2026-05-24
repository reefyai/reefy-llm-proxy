"""ChatCompletions <-> Responses translation for the codex backend.

The codex API at `chatgpt.com/backend-api/codex` only speaks the
Responses API (`POST /responses`). Clients that only know
ChatCompletions (`POST /chat/completions`) - openclaw with the
default "custom OpenAI-compatible" wiring, claude-code's
OPENAI_BASE_URL path, anything built against the OpenAI SDK's chat
namespace - would otherwise need per-client config to switch to the
Responses adapter. This translator lets them work unchanged.

Request side: messages -> instructions+input array, flatten tools
to the Responses shape, force stream:true + store:false (codex
mandate). Response side: stream codex SSE events through, mapping
each one to the corresponding ChatCompletions chunk.

Scope: text + function tools + multimodal content blocks (image,
audio, file). Both APIs use content-block lists for multimodal but
with different type names and field layouts; _translate_content
maps each block to its Responses equivalent.

Not (yet) translated: reasoning summary events on the response
stream, file_search / web_search / native code-interpreter result
items - dropped silently. Add them when a client needs them; the
streaming loop's else-branch is a single ignore.
"""

from __future__ import annotations

import json
import time
from typing import AsyncIterator


# ChatCompletions content block type -> codex Responses content block type.
# Direction matters: input messages (user/system/developer/tool) use input_*
# variants; assistant output uses output_text. Plain strings (the legacy
# `content: "..."` shape) need no rewrite.
_INPUT_BLOCK_TYPE_MAP = {
    'text':        'input_text',
    'image_url':   'input_image',
    'input_audio': 'input_audio',
    'file':        'input_file',
}


def _translate_input_block(block: dict) -> dict | None:
    """Map one ChatCompletions content block to its codex Responses
    equivalent, for messages going TO the model. Returns None to
    drop unrecognised types (rather than forward something codex
    would reject)."""
    if not isinstance(block, dict):
        return None
    t = block.get('type')
    if t == 'text':
        text = block.get('text', '')
        if not text:
            return None
        return {'type': 'input_text', 'text': text}
    if t == 'image_url':
        # ChatCompletions: {type:"image_url", image_url:{url, detail?}}
        # Responses:       {type:"input_image", image_url:"<url>", detail?}
        img = block.get('image_url')
        if isinstance(img, dict):
            url = img.get('url', '')
            detail = img.get('detail')
        else:
            url, detail = (str(img) if img else ''), None
        if not url:
            return None
        out: dict = {'type': 'input_image', 'image_url': url}
        if detail:
            out['detail'] = detail
        return out
    if t == 'input_audio':
        # ChatCompletions: {type:"input_audio", input_audio:{data, format}}
        # Responses:       {type:"input_audio", input_audio:{data, format}}
        # Shape happens to align; pass through.
        return {'type': 'input_audio',
                'input_audio': block.get('input_audio', {})}
    if t == 'file':
        # ChatCompletions: {type:"file", file:{file_id} or {filename,file_data}}
        # Responses:       {type:"input_file", file_id} or {type:"input_file", filename, file_data}
        file_ = block.get('file') or {}
        if file_.get('file_id'):
            return {'type': 'input_file', 'file_id': file_['file_id']}
        if file_.get('file_data'):
            return {'type': 'input_file',
                    'filename': file_.get('filename', 'upload'),
                    'file_data': file_['file_data']}
        return None
    # Pass through anything we already produce in Responses-native shape
    # (lets clients build mixed bodies if they want).
    if isinstance(t, str) and t.startswith('input_'):
        return block
    return None


def _translate_assistant_content(content) -> object:
    """Assistant-message content -> Responses output-content shape.
    Strings stay strings (codex accepts both). List input becomes
    `output_text` blocks; non-text blocks in assistant messages are
    dropped (codex's history shape doesn't carry input_image for
    assistant turns)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for block in content:
            if isinstance(block, dict) and block.get('type') in ('text', 'output_text'):
                text = block.get('text', '')
                if text:
                    out.append({'type': 'output_text', 'text': text})
        return out
    return content


def chat_to_responses(body: dict) -> dict:
    """ChatCompletions request body -> codex Responses request body.

    The caller must have already stripped any provider prefix from
    `body['model']`."""
    out: dict = {
        'model': body.get('model', ''),
        'stream': True,        # codex mandate
        'store': False,        # codex mandate
    }

    messages = body.get('messages') or []
    instructions_parts: list[str] = []
    input_items: list[dict] = []
    for msg in messages:
        role = msg.get('role')
        content = msg.get('content')
        if role in ('system', 'developer'):
            # Codex puts the system prompt in `instructions`, not in
            # `input`. Multiple system messages get joined with a blank
            # line so order is preserved. Multimodal content blocks in
            # a system message are reduced to text-only - images in a
            # system prompt don't make sense for codex's input shape.
            if isinstance(content, str):
                instructions_parts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get('type') == 'text':
                        text = part.get('text', '')
                        if text:
                            instructions_parts.append(text)
        elif role == 'tool':
            # ChatCompletions tool result: {role:"tool", tool_call_id, content}
            # Responses: {type:"function_call_output", call_id, output}
            # `output` is always a string in the Responses spec; serialise
            # list/dict content to JSON so structured tool outputs survive.
            input_items.append({
                'type': 'function_call_output',
                'call_id': msg.get('tool_call_id', ''),
                'output': content if isinstance(content, str)
                          else json.dumps(content),
            })
        elif role == 'assistant':
            # Assistant turn may contain plain content AND/OR tool calls.
            # Tool calls in ChatCompletions live as msg.tool_calls (list);
            # in Responses each is its own function_call input item.
            tool_calls = msg.get('tool_calls') or []
            for tc in tool_calls:
                fn = tc.get('function') or {}
                input_items.append({
                    'type': 'function_call',
                    'call_id': tc.get('id', ''),
                    'name': fn.get('name', ''),
                    'arguments': fn.get('arguments', ''),
                })
            if content:
                input_items.append({
                    'role': 'assistant',
                    'content': _translate_assistant_content(content),
                })
        else:
            # user (or any other; pass through with shape translation
            # for content-block lists so images / audio / file blocks
            # land in their Responses-native form).
            if isinstance(content, list):
                translated = [
                    b for b in (_translate_input_block(p) for p in content)
                    if b is not None
                ]
                input_items.append({'role': role, 'content': translated})
            else:
                input_items.append({'role': role, 'content': content})

    # Codex requires the `instructions` field even when empty - the
    # Responses backend returns 400 "Instructions are required"
    # otherwise. Tools that probe /v1/chat/completions for endpoint
    # compatibility (openclaw's auto-detect, harness tests) typically
    # send a single user message with no system prompt; emit an empty
    # string for them so detection succeeds.
    out['instructions'] = '\n\n'.join(instructions_parts) if instructions_parts else ''
    out['input'] = input_items

    # Tools: ChatCompletions wraps the spec in a `function` sub-object;
    # Responses flattens it onto the tool dict itself.
    tools = body.get('tools') or []
    if tools:
        new_tools: list[dict] = []
        for t in tools:
            if t.get('type') == 'function' and isinstance(t.get('function'), dict):
                fn = t['function']
                new_tools.append({
                    'type': 'function',
                    'name': fn.get('name'),
                    'description': fn.get('description', ''),
                    'parameters': fn.get('parameters', {}),
                })
            else:
                # Pass through anything that's not a function tool (e.g.
                # codex's own native tool types if a caller already used
                # the Responses shape on a chat/completions path).
                new_tools.append(t)
        out['tools'] = new_tools

    # tool_choice / parallel_tool_calls / temperature / top_p etc. are
    # shape-compatible between the two APIs; pass through if present.
    for key in ('tool_choice', 'parallel_tool_calls',
                'temperature', 'top_p', 'reasoning'):
        if key in body:
            out[key] = body[key]

    return out


async def responses_sse_to_chat_sse(
    upstream: AsyncIterator[bytes],
    model: str,
) -> AsyncIterator[bytes]:
    """Consume codex Responses SSE events, emit ChatCompletions chunks.

    Codex emits a dozen event types; we care about three groups:
      - response.output_text.delta: text content streaming
      - response.output_item.added / function_call_arguments.delta:
        tool-call streaming (split into shape-compatible
        ChatCompletions tool_calls deltas)
      - response.completed: final event with usage + done marker

    Everything else is dropped silently - the client doesn't need
    them. The first chunk we emit announces the assistant role so
    downstream streaming UIs render an avatar immediately rather than
    waiting for first content delta."""
    chunk_id = f'chatcmpl-{int(time.time() * 1000)}'
    created = int(time.time())

    def make_chunk(delta: dict, finish_reason: str | None = None,
                   usage: dict | None = None) -> bytes:
        chunk: dict = {
            'id': chunk_id,
            'object': 'chat.completion.chunk',
            'created': created,
            'model': model,
            'choices': [{
                'index': 0,
                'delta': delta,
                'finish_reason': finish_reason,
            }],
        }
        if usage is not None:
            chunk['usage'] = usage
        return f'data: {json.dumps(chunk, separators=(",", ":"))}\n\n'.encode('utf-8')

    # Initial role chunk lets ChatCompletions clients show an assistant
    # bubble immediately.
    yield make_chunk({'role': 'assistant', 'content': ''})

    # Tool call accounting. Codex's function_call items have an `id`
    # (item ID, e.g. fc_*) and a `call_id` (the one tools should
    # echo back as tool_call_id). ChatCompletions tool_calls use the
    # `id` field for the call identifier - which corresponds to
    # codex's `call_id`. Track both so deltas can be routed by item_id
    # while emitting the call_id to the client.
    tool_call_idx = 0
    tool_calls: dict[str, dict] = {}   # item_id -> {index, call_id}

    buf = b''
    async for chunk in upstream:
        buf += chunk
        while b'\n\n' in buf:
            block, buf = buf.split(b'\n\n', 1)
            for line in block.split(b'\n'):
                line = line.strip()
                if not line.startswith(b'data:'):
                    continue
                payload = line[5:].strip()
                if not payload or payload == b'[DONE]':
                    continue
                try:
                    evt = json.loads(payload)
                except (ValueError, json.JSONDecodeError):
                    continue

                etype = evt.get('type')

                if etype == 'response.output_text.delta':
                    text = evt.get('delta', '')
                    if text:
                        yield make_chunk({'content': text})

                elif etype == 'response.output_item.added':
                    item = evt.get('item') or {}
                    if item.get('type') == 'function_call':
                        item_id = item.get('id', '')
                        call_id = item.get('call_id', item_id)
                        tool_calls[item_id] = {
                            'index': tool_call_idx,
                            'call_id': call_id,
                        }
                        # Emit the opening tool_call delta with id + name;
                        # arguments stream in later as function_call_arguments.delta.
                        yield make_chunk({
                            'tool_calls': [{
                                'index': tool_call_idx,
                                'id': call_id,
                                'type': 'function',
                                'function': {
                                    'name': item.get('name', ''),
                                    'arguments': '',
                                },
                            }],
                        })
                        tool_call_idx += 1

                elif etype == 'response.function_call_arguments.delta':
                    item_id = evt.get('item_id', '')
                    delta_args = evt.get('delta', '')
                    tc = tool_calls.get(item_id)
                    if tc is not None and delta_args:
                        yield make_chunk({
                            'tool_calls': [{
                                'index': tc['index'],
                                'function': {'arguments': delta_args},
                            }],
                        })

                elif etype == 'response.completed':
                    resp = evt.get('response') or {}
                    u = resp.get('usage') or {}
                    usage = {
                        'prompt_tokens':     int(u.get('input_tokens', 0)),
                        'completion_tokens': int(u.get('output_tokens', 0)),
                        'total_tokens':      int(u.get('total_tokens', 0)),
                    }
                    finish = 'tool_calls' if tool_calls else 'stop'
                    yield make_chunk({}, finish_reason=finish, usage=usage)

    yield b'data: [DONE]\n\n'
