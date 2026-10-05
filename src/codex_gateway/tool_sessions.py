"""Bounded in-memory continuations for app-server dynamic tool RPCs.

A suspended tool call owns its WS lease. Only the same API Key may return its
result. A restart or expiry fails explicitly instead of rerunning the tool.
"""
import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from .client_tools import ToolProtocolError, tool_outputs, definitions, compatible_definitions


@dataclass(eq=False)
class ToolRun:
    request: object
    target: object
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=64))
    reply: asyncio.Future | None = None
    call_id: str | None = None
    accepted_call_id: str | None = None
    result_is_error: bool = False
    task: asyncio.Task | None = None
    claimed: bool = False
    thread_id: str | None = None
    delivered_usage: tuple = (0,0,0,0)


class ToolSessions:
    def __init__(self, run_events, ttl=300, limit=512):
        self.run_events=run_events
        self.ttl=ttl
        self.limit=limit
        self.pending={}
        self.runs=set()
        self.retired=OrderedDict()

    def retire(self, key, code):
        self.retired[key]=(time.monotonic(),code)
        self.retired.move_to_end(key)
        while len(self.retired)>2048:
            self.retired.popitem(last=False)

    def has_pending(self, key, thread_id):
        return any(r.thread_id==thread_id and r.target.connection_key.split(':',1)[0]==str(key)
                   and not r.task.done() for r in self.runs)

    def can_supersede_with_user_turn(self, request, key, thread_id, checkpoint_length=0):
        """Find the active pending call output after a verified history checkpoint.

        The suspended run cannot accept both inputs atomically. Execution recovery
        may cancel it and rebuild from verified full history without dropping the
        user's new turn.
        """
        raw_items = request.input if isinstance(request.input, list) else []
        items = [item for item in raw_items
                 if not (isinstance(item, dict) and item.get('type') == 'additional_tools')]
        from .claude_helpers import dialogue_tail
        tail = dialogue_tail(items)
        if len(items) <= checkpoint_length or not isinstance(tail, dict) or tail.get('role') != 'user':
            return False
        matches = []
        for item in items[checkpoint_length:]:
            if not isinstance(item, dict) or item.get('type') not in {'function_call_output', 'custom_tool_call_output'}:
                continue
            run = self.pending.get((str(key or 'development'), item.get('call_id')))
            if (run and run.thread_id == thread_id and not run.claimed and not run.task.done()
                    and run.reply is not None and not run.reply.done()):
                matches.append(item)
        return len(matches) == 1

    async def supersede_with_user_turn(self, request, key, thread_id, checkpoint_length):
        # No await between validation and claiming: a concurrent tool reply must
        # never be accepted once cancellation has started.
        if not self.can_supersede_with_user_turn(request, key, thread_id, checkpoint_length):
            return False
        items = [i for i in request.input if i.get("type") != "additional_tools"]
        from .execution import recovery_call_id
        call_id = recovery_call_id(items, checkpoint_length)
        run = self.pending.get((str(key), call_id))
        if run is None:
            return False
        from .providers import provider_for
        if provider_for(request.model) != provider_for(run.request.model):
            raise ToolProtocolError("Cannot change provider while cancelling a pending tool")
        if provider_for(request.model) == "codex" and (
                request.model != run.request.model or definitions(request) != definitions(run.request)):
            raise ToolProtocolError("Cannot change model or tools while cancelling a pending tool")
        run.claimed = True
        self.retire((str(key), call_id), "client_tool_call_unavailable")
        run.task.cancel()
        await asyncio.gather(run.task, return_exceptions=True)
        return True

    async def abandon_thread(self, key, thread_id):
        """Retire a suspended run before starting a separate context-only turn."""
        runs = [r for r in self.runs if r.thread_id == thread_id
                and r.target.connection_key.split(':', 1)[0] == str(key)
                and not r.task.done()]
        # Never cancel a result that another request has already accepted.
        if any(r.claimed or r.reply is None or r.reply.done() for r in runs):
            return False
        for run in runs:
            run.claimed = True
            if run.call_id:
                self.retire((str(key), run.call_id), "client_tool_call_unavailable")
            run.task.cancel()
        await asyncio.gather(*(r.task for r in runs), return_exceptions=True)
        return True

    async def cancel_thread(self, key, thread_id):
        tasks=[r.task for r in self.runs if r.thread_id==thread_id and r.target.connection_key.split(':',1)[0]==str(key)]
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)

    def find(self, request, key):
        outputs=tool_outputs(request)
        if not outputs:
            if request.previous_response_id and any(r.thread_id == request.previous_response_id and r.target.connection_key.split(':',1)[0] == str(key or 'development') for r in self.runs):
                raise ToolProtocolError('This Thread is waiting for a client tool output; return its call_id first', 'conversation_waiting_tool')
            return None
        if len(outputs)!=1:
            raise ToolProtocolError('Return exactly one pending client tool output at a time')
        run=self.pending.get((str(key or 'development'),outputs[0][0]))
        if run is not None and run.claimed:
            raise ToolProtocolError('This client tool result has already been submitted', 'client_tool_result_duplicate')
        if run is None or run.task.done() or run.reply is None or run.reply.done():
            retired=self.retired.get((str(key or 'development'),outputs[0][0]))
            code=retired[1] if retired and time.monotonic()-retired[0]<3600 else 'client_tool_call_unavailable'
            message=('This client tool result has already been submitted' if code=='client_tool_result_duplicate'
                     else 'Client tool call is no longer available in this gateway; start a new user turn with full history')
            raise ToolProtocolError(message, code)
        if request.model!=run.request.model:
            raise ToolProtocolError('Cannot change model while returning a pending tool output')
        if request.previous_response_id and request.previous_response_id != run.thread_id:
            raise ToolProtocolError('Tool output and previous_response_id refer to different Threads')
        from .providers import provider_for
        compatible = (compatible_definitions(request, run.request)
                      if provider_for(request.model) in {"claude", "gemini"}
                      else definitions(request) == definitions(run.request))
        if definitions(request) and not compatible:
            raise ToolProtocolError('Cannot change tool contracts while returning a pending tool output',
                "tool_configuration_changed" if provider_for(request.model) in {"claude", "gemini"} else "invalid_client_tool")
        return run

    def target_for(self, request, key):
        run=self.find(request,key)
        return run.target if run else None

    async def pump(self,run):
        try:
            async for event in self.run_events(run.request,run.target,run):
                run.thread_id = event.thread_id or run.thread_id
                if event.tool_call:
                    from .cluster import publish_tool
                    await publish_tool(run, self.ttl)
                await run.queue.put(event)
        except asyncio.CancelledError:
            if run.accepted_call_id:
                key = run.target.connection_key.split(':', 1)[0]
                self.retire((key, run.accepted_call_id), 'client_tool_call_unavailable')
            if not run.queue.full():
                run.queue.put_nowait(ToolProtocolError('The pending execution was cancelled or invalidated', 'client_tool_call_unavailable'))
            raise
        except Exception as exc:
            if run.accepted_call_id:
                key = run.target.connection_key.split(':', 1)[0]
                self.retire((key, run.accepted_call_id), 'client_tool_call_unavailable')
            await run.queue.put(exc)
        finally:
            if run.call_id:
                self.pending.pop((run.target.connection_key.split(':',1)[0],run.call_id),None)
            self.runs.discard(run)
            # Consumers stop at done events; sentinel also covers interrupted EOF.
            if not run.queue.full():
                run.queue.put_nowait(None)

    async def await_result(self,run,call):
        run.call_id=call['call_id']
        run.reply=asyncio.get_running_loop().create_future()
        run.claimed=False
        self.pending[(run.target.connection_key.split(':',1)[0],run.call_id)]=run

    async def receive_result(self,run):
        try:
            return await asyncio.wait_for(run.reply,self.ttl)
        finally:
            self.pending.pop((run.target.connection_key.split(':',1)[0],run.call_id),None)
            run.call_id=None

    async def stream(self,request,target):
        key=target.connection_key.split(':',1)[0]
        run=self.find(request,key)
        if run:
            run.claimed=True
            run.accepted_call_id=run.call_id
            self.retire((key,run.call_id), 'client_tool_result_duplicate')
            output=tool_outputs(request)[0][1]
            if getattr(target, "provider", None) in {"claude", "gemini"}:
                items = request.input if isinstance(request.input, list) else [request.input]
                run.result_is_error = any(isinstance(item, dict) and item.get("call_id") == run.call_id
                                          and item.get("is_error") is True for item in items)
            run.reply.set_result(output)
        else:
            if len(self.runs)>=self.limit or sum(r.target.connection_key.split(":",1)[0] == key for r in self.runs) >= 8:
                raise ToolProtocolError('Too many pending client tool sessions; retry later')
            run=ToolRun(request,target)
            self.runs.add(run)
            run.task=asyncio.create_task(self.pump(run),name='gateway-client-tool')
        suspended=False
        try:
            while True:
                event=await run.queue.get()
                if event is None:
                    break
                if isinstance(event,Exception):
                    raise event
                boundary=bool(event.tool_call or event.done)
                if boundary or event.usage_accounting:
                    counts=tuple(getattr(event,n,0) for n in ('input_tokens','output_tokens','cache_read_tokens','cache_write_tokens'))
                    updates={n:max(0,v-old) for n,v,old in zip(('input_tokens','output_tokens','cache_read_tokens','cache_write_tokens'),counts,run.delivered_usage) if n in type(event).model_fields}
                    # Claude's successful result is authoritative, including downward
                    # corrections. Persistence replaces earlier provisional records.
                    if not (event.usage_accounting or {}).get('final'):
                        event=event.model_copy(update=updates)
                    if boundary:
                        run.delivered_usage=counts
                if event.tool_call:
                    suspended=True
                    run.accepted_call_id=None
                yield event
                if boundary:
                    break
        finally:
            if not suspended and not run.task.done():
                run.task.cancel()
                await asyncio.gather(run.task,return_exceptions=True)

    async def close(self):
        tasks=[r.task for r in self.runs]
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        self.pending.clear()
