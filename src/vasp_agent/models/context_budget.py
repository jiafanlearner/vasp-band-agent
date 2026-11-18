"""Conservative, explicitly estimated context accounting; never infer model capacity."""
import json
import math


class ContextBudget:
    def __init__(self, window=32768, output=4000, safety=2048, trigger=.8, target=.55):
        if any(type(x) is not int for x in (window, output, safety)) or min(window, output) < 1 or safety < 0:
            raise ValueError('上下文预算必须为正整数，安全余量不能为负。')
        if window - output - safety < 4096 or not 0 < target < trigger < 1:
            raise ValueError('可用输入预算须至少4096；比例须满足0 < target < trigger < 1。')
        self.window, self.output, self.safety = window, output, safety
        self.trigger, self.target = trigger, target
        self.factor = 1.0
        self.last_reported = None
        self.overhead = 0

    def raw(self, value):
        # No model-name/tokenizer guess. JSON framing and UTF-8 bytes are included.
        return math.ceil(len(json.dumps(value, ensure_ascii=False).encode('utf-8')) / 3) + 16

    def estimate(self, value):
        return math.ceil(self.raw(value) * self.factor)

    def measure(self, history):
        return self.estimate(history) + self.overhead

    @property
    def available(self):
        return self.window - self.output - self.safety

    def calibrate(self, history, overhead_value, reported):
        if type(reported) is int and reported > 0:
            self.last_reported = reported
            base = self.raw(history) + self.raw(overhead_value)
            # Never decrease protection from a short or cached request.
            self.factor = max(self.factor, reported / max(1, base) * 1.1)

    def status(self, history):
        used = self.measure(history)
        return {'estimated_input_tokens': used, 'configured_context_window': self.window,
                'input_budget': self.available, 'output_reserve': self.output,
                'safety_reserve': self.safety, 'input_budget_percent': round(100 * used / self.available, 1),
                'compact_trigger_tokens': int(self.available * self.trigger),
                'compact_target_tokens': int(self.available * self.target),
                'last_api_input_tokens': self.last_reported,
                'measurement': 'estimate_utf8_calibrated',
                'note': '容量为手动配置预算，不是接口确认的模型上限；当前占用是估算，API用量仅对应上次请求。'}
