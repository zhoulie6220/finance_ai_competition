"""SSE 事件总线。

任务时间线是本系统与「AI 财报摘要工具」的主要区别之一，所以它必须**不丢帧**。
丢帧的典型场景不是网络断，而是：事件在步骤开始前就发出去了、前端还没连上；
或者断线重连之后中间那几帧永远找不回来，时间线上凭空少一格 —— 而少一格
看起来和「这一步本来就没有」一模一样。

对策是给每个任务一个**单调递增的 seq**，并把最近若干条留在内存里；
前端断线重连时带上 `Last-Event-ID`，服务端从 seq+1 开始补发，补完再接实时流。

有界历史是刻意的：无界deque 在长任务上会一直涨，而演示现场不会有人断线几万条再回来。
超出上限时丢弃最旧的，并在流的开头发一条 `warning`，明确告诉前端
「历史已经被截断，你看到的时间线不完整」—— 静默丢弃等于制造一条看似完整的时间线。
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from app.agents.state import Clock, assert_event_type
from app.schemas.task import TaskEvent

#: 补发历史被截断时，事件流开头发这条 warning 的 type
TRUNCATED_WARNING = "warning"

#: 收到这些事件就意味着任务不会再变化了，流可以关掉。
#: 漏掉一个的后果是「任务已经失败/完成，前端的进度条却一直在转」。
TASK_ENDING_EVENTS = frozenset({"task.completed", "task.failed"})


def _ends_the_task(history: "deque[TaskEvent] | list[TaskEvent]") -> bool:
    """历史里最后一条是不是终态事件。"""
    return bool(history) and history[-1].type in TASK_ENDING_EVENTS


@dataclass
class _TaskChannel:
    """单个任务的事件通道：有界历史 + 当前订阅者。"""

    history: deque[TaskEvent]
    seq: int = 0
    subscribers: set[asyncio.Queue[TaskEvent | None]] = field(default_factory=set)
    #: 被截断掉的最旧 seq。前端据此判断自己拿到的时间线是否完整。
    dropped_before: int = 0


class EventBus:
    """按 task_id 分组的进程内发布订阅。

    单进程够用：本项目本地运行、任务串行执行，不引入 Redis / 消息队列——
    多一个中间件就多一处演示现场可能起不来的东西。
    """

    def __init__(self, *, history_limit: int = 1000, clock: Clock) -> None:
        self._limit = history_limit
        self._clock = clock
        self._channels: dict[str, _TaskChannel] = {}

    # ------------------------------------------------------------------ 发布

    def _channel(self, task_id: str) -> _TaskChannel:
        ch = self._channels.get(task_id)
        if ch is None:
            ch = _TaskChannel(history=deque(maxlen=self._limit))
            self._channels[task_id] = ch
        return ch

    def publish(
        self,
        task_id: str,
        type_: str,
        payload: dict[str, Any] | None = None,
        *,
        step_id: str | None = None,
    ) -> TaskEvent:
        """发一条事件。返回已落序号的事件对象（编排器据此回写数据库）。"""
        assert_event_type(type_)
        ch = self._channel(task_id)
        ch.seq += 1
        event = TaskEvent(
            seq=ch.seq,
            ts=self._clock.now(),
            task_id=task_id,
            step_id=step_id,
            type=type_,
            payload=payload or {},
        )
        # deque 满时自动丢最旧的，记下丢到哪儿了
        if len(ch.history) == self._limit:
            ch.dropped_before = ch.history[0].seq
        ch.history.append(event)

        for q in list(ch.subscribers):
            # put_nowait 而不是 await put：慢订阅者不能拖住编排器。
            # 队列本身有界，满了就丢这一帧并让该订阅者自己去补历史 ——
            # 让生产者等消费者，会让一个卡住的浏览器标签页把整个任务卡死。
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass
        return event

    def heartbeat(self, task_id: str) -> TaskEvent:
        """心跳。不占 seq 的历史语义——它照样是一个事件，但不改变业务状态。"""
        return self.publish(task_id, "heartbeat", {})

    # ------------------------------------------------------------------ 订阅

    async def subscribe(
        self,
        task_id: str,
        *,
        last_event_id: int | None = None,
        heartbeat_seconds: float = 15.0,
        already_finished: bool = False,
    ) -> AsyncIterator[TaskEvent]:
        """订阅一个任务的事件流。

        `last_event_id` 非空时先补发 seq 之后的历史，再接实时流——
        这就是浏览器自动重连带 `Last-Event-ID` 时的路径。

        **任务到终态就关流。** 订阅一个已经跑完的任务时，补发完历史即结束，
        不会一直挂着等心跳。不关的话每刷新一次页面就多一条永不结束的连接，
        浏览器那边也会一直显示「重连中」——一个已经完成的任务看起来像卡住了。
        `already_finished` 由调用方按任务的真实状态传入，因为总线本身不知道
        任务跑到哪一步了，也不该知道（那是编排器的事实）。
        """
        ch = self._channel(task_id)
        queue: asyncio.Queue[TaskEvent | None] = asyncio.Queue(maxsize=1024)
        ch.subscribers.add(queue)
        try:
            # `last_event_id is None` 表示**这个客户端什么都没有**——全新连接，
            # 那就把历史全部发过去。它不等于「不发历史」。
            #
            # ⚠ 这里曾经把 None 当成「不发历史」，后果是一整条链路静默失效：
            #   前端建完任务立刻订阅，而任务跑得快、订阅时已经结束——
            #   补发被跳过，紧接着因为 already_finished 直接关流，
            #   页面收到的是一条**空流**。任务成功了，时间线却一格都没有，
            #   而且没有任何地方会报错。
            since = -1 if last_event_id is None else last_event_id
            if ch.dropped_before and since < ch.dropped_before:
                yield self._synthetic(
                    task_id,
                    TRUNCATED_WARNING,
                    {
                        "message": "断线期间的事件已超出保留上限，时间线开头可能不完整",
                        "dropped_before": ch.dropped_before,
                        "requested_after": since,
                    },
                )
            for event in ch.history:
                if event.seq > since:
                    yield event

            # 补发完历史后，如果任务已经结束（或历史里已经带着终态事件），
            # 就没有后续可等了，直接关流。
            if already_finished or _ends_the_task(ch.history):
                return

            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=heartbeat_seconds)
                except (TimeoutError, asyncio.TimeoutError):
                    yield self.heartbeat(task_id)
                    continue
                if event is None:  # 关停信号
                    return
                yield event
                if event.type in TASK_ENDING_EVENTS:
                    return
        finally:
            ch.subscribers.discard(queue)

    def _synthetic(self, task_id: str, type_: str, payload: dict[str, Any]) -> TaskEvent:
        """构造一条**不占 seq** 的事件，只发给当前订阅者。"""
        return TaskEvent(
            seq=self._channel(task_id).seq,
            ts=self._clock.now(),
            task_id=task_id,
            step_id=None,
            type=type_,
            payload=payload,
        )

    def close(self, task_id: str) -> None:
        """任务结束，唤醒所有订阅者让它们正常收尾。"""
        for q in list(self._channel(task_id).subscribers):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass

    # ------------------------------------------------------------------ 运维

    def history(self, task_id: str) -> list[TaskEvent]:
        return list(self._channel(task_id).history)

    def forget(self, task_id: str) -> None:
        """任务结束后释放内存。**必须确认没有订阅者**，否则活跃的流会凭空断掉。"""
        ch = self._channels.get(task_id)
        if ch and ch.subscribers:
            raise RuntimeError(
                f"任务 {task_id} 仍有 {len(ch.subscribers)} 个订阅者，不能释放事件历史"
            )
        self._channels.pop(task_id, None)


def format_sse(event: TaskEvent) -> str:
    """把一个事件格式化成 SSE 报文。

    两个字段是必须的：
      - `id:` 让浏览器自动重连时能带 `Last-Event-ID`，这是补发的前提
      - `event:` 让前端能 `addEventListener(type)` 而不是在 message 回调里做 switch
    """
    import json

    return (
        f"id: {event.seq}\n"
        f"event: {event.type}\n"
        f"data: {json.dumps(event.model_dump(), ensure_ascii=False)}\n\n"
    )
