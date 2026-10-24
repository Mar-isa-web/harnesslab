# -*- coding: utf-8 -*-
"""重试策略（M2 改造点 a）：把"失败后怎么办"从主循环里抽成可插拔策略。

M0 的朴素基线 ImmediateRetry：失败立刻重试一次；
M2 的对照组 ExponentialBackoff：指数退避。
两种策略在真实基准上跑同一任务集，收益差异用数据说话（benchmark.py --retry 标记）。

sleeper 可注入：测试里传 recorder 就能断言"确实等了、等了多久"，不必真睡。
"""
import time


class RetryPolicy:
    name = "base"
    max_retries = 0

    def wait_before_retry(self, attempt: int) -> float:
        """返回第 attempt 次失败后、发起下一次尝试前的等待秒数。"""
        return 0.0


class ImmediateRetry(RetryPolicy):
    name = "immediate"

    def __init__(self, max_retries: int = 1):
        self.max_retries = max_retries

    def wait_before_retry(self, attempt):
        return 0.0


class ExponentialBackoff(RetryPolicy):
    """delay = min(base * factor**attempt, max_delay)，attempt 从 0 起。

    为什么默认 base=0.5、factor=2、上限 8s：Agent 的失败多为限流/网络抖动，
    首退避要短（别让用户干等），但要封顶（防止长任务挂死）。
    这些参数本身就是实验变量。
    """

    name = "exp-backoff"

    def __init__(self, max_retries=3, base_delay=0.5, factor=2.0,
                 max_delay=8.0, sleeper=time.sleep):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.factor = factor
        self.max_delay = max_delay
        self._sleep = sleeper

    def wait_before_retry(self, attempt):
        delay = min(self.base_delay * (self.factor ** attempt), self.max_delay)
        self._sleep(delay)   # 注入点：测试传 recorder 即可断言退避序列
        return delay
