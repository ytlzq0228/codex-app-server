"""Bounded in-memory continuations for app-server dynamic tool RPCs.

A suspended tool call owns its WS lease. Only the same API Key may return its
result. A restart or expiry fails explicitly instead of rerunning the tool.
"""
import asyncio
from dataclasses import dataclass, field
from .client_tools import ToolProtocolError, tool_outputs, definitions


@dataclass(eq=False)
class ToolRun:
    request: object
    target: object
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=64))
    reply: asyncio.Future | None = None
    call_id: str | None = None
    task: asyncio.Task | None = None
    claimed: bool = False
    thread_id: str | None = None
    delivered_usage: tuple = (0,0,0,0)


class ToolSessions:
    def __init__(self, run_events, ttl=300, limit=64):
        self.run_events=run_events
        self.ttl=ttl
        self.limit=limit
        self.pending={}
        self.runs=set()

    def find(self, request, key):
        outputs=tool_outputs(request)
        if not outputs:
            if request.previous_response_id and any(r.thread_id == request.previous_response_id and r.target.connection_key.split(':',1)[0] == str(key or 'development') for r in self.runs):
                raise ToolProtocolError('This Thread is waiting for a client tool output; return its call_id first')
            return None
        if len(outputs)!=1:
            raise ToolProtocolError('Return exactly one pending client tool output at a time')
        run=self.pending.get((str(key or 'development'),outputs[0][0]))
        if run is None or run.claimed or run.task.done():
            raise ToolProtocolError('Client tool call is unknown, expired, already consumed, or belongs to another API Key')
        if request.model!=run.request.model:
            raise ToolProtocolError('Cannot change model while returning a pending tool output')
        if request.previous_response_id and request.previous_response_id != run.thread_id:
            raise ToolProtocolError('Tool output and previous_response_id refer to different Threads')
        if definitions(request) and definitions(request) != definitions(run.request):
            raise ToolProtocolError('Cannot change tools while returning a pending tool output')
        return run

    def target_for(self, request, key):
        run=self.find(request,key)
        return run.target if run else None

    async def pump(self,run):
        try:
            async for event in self.run_events(run.request,run.target,run):
                run.thread_id = event.thread_id or run.thread_id
                await run.queue.put(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
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
            output=tool_outputs(request)[0][1]
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
                if boundary:
                    counts=tuple(getattr(event,n,0) for n in ('input_tokens','output_tokens','cache_read_tokens','cache_write_tokens'))
                    updates={n:max(0,v-old) for n,v,old in zip(('input_tokens','output_tokens','cache_read_tokens','cache_write_tokens'),counts,run.delivered_usage) if n in type(event).model_fields}
                    run.delivered_usage=counts
                    event=event.model_copy(update=updates)
                if event.tool_call:
                    suspended=True
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
